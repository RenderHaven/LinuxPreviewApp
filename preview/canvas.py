"""The page view: zoom, rotation, scrolling and page rendering.

Pages are rasterised on a background thread with Cairo and handed to the
widget layer as :class:`Gdk.Texture` objects, which ``do_snapshot`` composites
through GTK's render nodes.  Nothing in the widget path touches a Cairo
context, so Preview does not need the ``python3-gi-cairo`` foreign-struct
bridge to draw.

Textures are cached per ``(page, scale)``; rotation is a transform applied at
paint time, so turning a document does not invalidate the cache.  While a page
is still rendering at the requested scale we stretch the nearest cached scale
in its place, which keeps zooming responsive even when the PDF backend is the
slow ``pdftoppm`` fallback.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")

from gi.repository import Adw, Gdk, GLib, GObject, Graphene, Gsk, Gtk  # noqa: E402

from .surface import surface_to_texture

PAGE_GAP = 18
PAGE_MARGIN = 18
MIN_SCALE = 0.05
MAX_SCALE = 16.0
ZOOM_STEPS = (
    0.05, 0.08, 0.125, 0.25, 0.33, 0.5, 0.66, 0.75, 1.0,
    1.25, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0,
)
# Roughly 320 MB of 32-bit pixels before the cache starts evicting.
_CACHE_PIXEL_BUDGET = 80_000_000
# How far outside the viewport we keep rendering, so scrolling has pages ready.
_OVERSCAN = 400

_SHADOW = Gdk.RGBA(red=0.0, green=0.0, blue=0.0, alpha=0.28)
_PAPER = Gdk.RGBA(red=1.0, green=1.0, blue=1.0, alpha=1.0)
_CANVAS_LIGHT = Gdk.RGBA(red=0.42, green=0.43, blue=0.45, alpha=1.0)
_CANVAS_DARK = Gdk.RGBA(red=0.12, green=0.12, blue=0.13, alpha=1.0)
_HIT = Gdk.RGBA(red=1.0, green=0.92, blue=0.23, alpha=0.42)
_HIT_ACTIVE = Gdk.RGBA(red=1.0, green=0.56, blue=0.0, alpha=0.55)
_SEL_FILL = Gdk.RGBA(red=0.22, green=0.52, blue=0.94, alpha=0.22)
_SEL_EDGE = Gdk.RGBA(red=0.22, green=0.52, blue=0.94, alpha=1.0)


def _rect(x: float, y: float, w: float, h: float) -> Graphene.Rect:
    return Graphene.Rect().init(x, y, w, h)


def _square(rect: Graphene.Rect) -> Gsk.RoundedRect:
    """A zero-radius rounded rect, which is what append_border wants."""
    rounded = Gsk.RoundedRect()
    rounded.init_from_rect(rect, 0.0)
    return rounded


class _PageCanvas(Gtk.Widget):
    """Bare widget that composites the laid-out pages via render nodes."""

    __gtype_name__ = "PreviewPageCanvas"

    def __init__(self, owner: "PageView") -> None:
        super().__init__()
        self._owner = owner

    def do_measure(self, orientation, for_size):
        width, height = self._owner.content_size
        size = int(math.ceil(width if orientation == Gtk.Orientation.HORIZONTAL else height))
        size = max(size, 1)
        return (size, size, -1, -1)

    def do_snapshot(self, snapshot):
        self._owner.snapshot_pages(self, snapshot)


class PageView(Gtk.Box):
    """Scrollable, zoomable view of a :class:`~preview.backends.base.Document`."""

    __gtype_name__ = "PreviewPageView"

    __gsignals__ = {
        "page-changed": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "zoom-changed": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "selection-changed": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.document = None
        self.scale = 1.0
        self.rotation = 0            # degrees, one of 0/90/180/270
        self.zoom_mode = "fit"       # fit | fill | actual | custom
        self.continuous = True
        self.select_mode = False
        self.current_page = 0
        self.content_size = (1.0, 1.0)

        self._highlights: dict[int, list] = {}
        self._active_highlight = None
        self._selection = None       # (page, x, y, w, h) in natural page units
        self._sel_anchor = None
        self._selecting = False
        self._drag_origin = None
        self._layout: list[tuple[float, float, float, float]] = []
        self._pointer_pos = (0.0, 0.0)
        self._reflowing = False

        self._cache: OrderedDict[tuple[int, float], Gdk.Texture] = OrderedDict()
        self._pending: set[tuple[int, float]] = set()
        self._cache_lock = threading.Lock()
        self._queue: list[tuple[int, float]] = []
        self._queue_cond = threading.Condition()
        self._stopping = False
        self._worker: threading.Thread | None = None

        self.scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.area = _PageCanvas(self)
        self.area.set_hexpand(True)
        self.area.set_vexpand(True)
        self.scroller.set_child(self.area)
        self.append(self.scroller)

        self.scroller.get_vadjustment().connect("value-changed", self._on_scrolled)
        self.scroller.get_hadjustment().connect(
            "value-changed", lambda *_: self.area.queue_draw()
        )
        for adjustment in (
            self.scroller.get_hadjustment(), self.scroller.get_vadjustment()
        ):
            adjustment.connect("notify::page-size", self._on_viewport_resized)

        zoom_ctl = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.BOTH_AXES
        )
        zoom_ctl.connect("scroll", self._on_scroll)
        self.area.add_controller(zoom_ctl)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.area.add_controller(drag)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.area.add_controller(motion)

    # -- document lifecycle -----------------------------------------------
    def set_document(self, document) -> None:
        self._stop_worker()
        with self._cache_lock:
            self._cache.clear()
            self._pending.clear()
        self.document = document
        self.current_page = 0
        self.rotation = 0
        self._highlights.clear()
        self._active_highlight = None
        self._selection = None
        self.emit("selection-changed", False)
        self._start_worker()
        self._reflow_for_mode(force=True)
        self.scroller.get_vadjustment().set_value(0)
        self.emit("page-changed", 0)

    def shutdown(self) -> None:
        self._stop_worker()

    # -- zoom --------------------------------------------------------------
    def set_zoom_mode(self, mode: str) -> None:
        self.zoom_mode = mode
        self._reflow_for_mode(force=True)

    def set_scale(self, scale: float, anchor: tuple[float, float] | None = None) -> None:
        scale = max(MIN_SCALE, min(MAX_SCALE, scale))
        if abs(scale - self.scale) < 1e-6:
            return
        old = self.scale
        hadj = self.scroller.get_hadjustment()
        vadj = self.scroller.get_vadjustment()
        if anchor is None:
            view_w, view_h = self.viewport_size()
            anchor = (view_w / 2.0, view_h / 2.0)
        # Keep the document point under the anchor fixed across the zoom.
        doc_x = (hadj.get_value() + anchor[0]) / max(old, 1e-6)
        doc_y = (vadj.get_value() + anchor[1]) / max(old, 1e-6)

        self.scale = scale
        self.zoom_mode = "custom"
        self._rebuild_layout()
        hadj.set_value(max(0.0, doc_x * scale - anchor[0]))
        vadj.set_value(max(0.0, doc_y * scale - anchor[1]))
        self.emit("zoom-changed", self.scale)
        self.area.queue_draw()

    def zoom_in(self) -> None:
        for step in ZOOM_STEPS:
            if step > self.scale + 1e-4:
                self.set_scale(step)
                return
        self.set_scale(MAX_SCALE)

    def zoom_out(self) -> None:
        for step in reversed(ZOOM_STEPS):
            if step < self.scale - 1e-4:
                self.set_scale(step)
                return
        self.set_scale(MIN_SCALE)

    def viewport_size(self) -> tuple[float, float]:
        """Visible extent of the scroller, excluding any scrollbars."""
        page_w = self.scroller.get_hadjustment().get_page_size()
        page_h = self.scroller.get_vadjustment().get_page_size()
        if page_w <= 1:
            page_w = self.scroller.get_width()
        if page_h <= 1:
            page_h = self.scroller.get_height()
        return float(page_w), float(page_h)

    def _on_viewport_resized(self, *_args) -> None:
        # Only "fit"/"fill" depend on the viewport, and _reflow_for_mode is a
        # no-op unless the scale actually moves.
        if self.zoom_mode in ("fit", "fill"):
            self._reflow_for_mode()

    def _fit_scale(self, fill: bool = False) -> float:
        if not self.document or not self.document.page_count:
            return 1.0
        width, height = self.document.page_size(self.current_page)
        if self.rotation in (90, 270):
            width, height = height, width
        view_w, view_h = self.viewport_size()
        avail_w = max(view_w - 2 * PAGE_MARGIN, 80)
        avail_h = max(view_h - 2 * PAGE_MARGIN, 80)
        if width <= 0 or height <= 0:
            return 1.0
        sx, sy = avail_w / width, avail_h / height
        return max(MIN_SCALE, min(MAX_SCALE, max(sx, sy) if fill else min(sx, sy)))

    def _reflow_for_mode(self, force: bool = False) -> None:
        if not self.document or self._reflowing:
            return
        if self.zoom_mode == "fit":
            scale = self._fit_scale(False)
        elif self.zoom_mode == "fill":
            scale = self._fit_scale(True)
        elif self.zoom_mode == "actual":
            scale = 1.0
        else:
            scale = self.scale
        changed = abs(scale - self.scale) > 1e-6
        self.scale = scale
        if changed or force:
            self._reflowing = True
            try:
                self._rebuild_layout()
                self.emit("zoom-changed", self.scale)
                self.area.queue_draw()
            finally:
                self._reflowing = False

    # -- rotation ----------------------------------------------------------
    def rotate(self, degrees: int) -> None:
        self.rotation = (self.rotation + degrees) % 360
        self._selection = None
        self.emit("selection-changed", False)
        self._reflow_for_mode(force=True)

    # -- layout ------------------------------------------------------------
    def _page_display_size(self, index: int) -> tuple[float, float]:
        width, height = self.document.page_size(index)
        width *= self.scale
        height *= self.scale
        if self.rotation in (90, 270):
            width, height = height, width
        return width, height

    def _laid_out_pages(self) -> list[int]:
        if not self.document:
            return []
        if self.continuous:
            return list(range(self.document.page_count))
        return [self.current_page]

    def _rebuild_layout(self) -> None:
        if not self.document:
            self._layout = []
            self.content_size = (1.0, 1.0)
            self.area.queue_resize()
            return
        indices = self._laid_out_pages()
        shown = set(indices)
        widths = [self._page_display_size(i)[0] for i in indices]
        content_w = max(widths) if widths else 0.0
        y = float(PAGE_MARGIN)
        layout: list[tuple[float, float, float, float]] = []
        for index in range(self.document.page_count):
            if index not in shown:
                layout.append((0.0, 0.0, 0.0, 0.0))
                continue
            width, height = self._page_display_size(index)
            layout.append(((content_w - width) / 2.0, y, width, height))
            y += height + PAGE_GAP
        self._layout = layout
        self.content_size = (
            max(content_w + 2 * PAGE_MARGIN, 1.0),
            max(y - PAGE_GAP + PAGE_MARGIN, 1.0),
        )
        self.area.queue_resize()
        self.area.queue_draw()

    def _origin_offset(self) -> float:
        """Horizontal offset that centres content narrower than the viewport."""
        allocated = self.area.get_width()
        if allocated > self.content_size[0]:
            return (allocated - self.content_size[0]) / 2.0
        return 0.0

    # -- rendering ---------------------------------------------------------
    def _start_worker(self) -> None:
        self._stopping = False
        self._worker = threading.Thread(target=self._render_loop, daemon=True)
        self._worker.start()

    def _stop_worker(self) -> None:
        with self._queue_cond:
            self._stopping = True
            self._queue.clear()
            self._queue_cond.notify_all()
        worker, self._worker = self._worker, None
        if worker and worker.is_alive():
            worker.join(timeout=2.0)

    def _render_loop(self) -> None:
        while True:
            with self._queue_cond:
                while not self._queue and not self._stopping:
                    self._queue_cond.wait()
                if self._stopping:
                    return
                # Newest request first: it is what the user is looking at.
                key = self._queue.pop()
            document = self.document
            if document is None:
                continue
            index, scale = key
            try:
                width, height = document.page_size(index)
                pw = max(1, int(math.ceil(width * scale)))
                ph = max(1, int(math.ceil(height * scale)))
                surface = cairo.ImageSurface(cairo.FORMAT_RGB24, pw, ph)
                cr = cairo.Context(surface)
                cr.set_source_rgb(1, 1, 1)
                cr.paint()
                document.render_page(index, cr, scale)
                surface.flush()
            except Exception:
                surface = None
            GLib.idle_add(
                self._store_render, key, surface, priority=GLib.PRIORITY_DEFAULT_IDLE
            )

    def _store_render(self, key, surface) -> bool:
        # Textures are GDK objects, so they are built here on the main thread.
        texture = None
        if surface is not None:
            try:
                texture = surface_to_texture(surface)
            except Exception:
                texture = None
        with self._cache_lock:
            self._pending.discard(key)
            if texture is not None:
                self._cache[key] = texture
                self._trim_cache()
        self.area.queue_draw()
        return False

    def _trim_cache(self) -> None:
        total = sum(t.get_width() * t.get_height() for t in self._cache.values())
        while total > _CACHE_PIXEL_BUDGET and len(self._cache) > 1:
            _, dropped = self._cache.popitem(last=False)
            total -= dropped.get_width() * dropped.get_height()

    def _request(self, index: int, scale: float) -> None:
        key = (index, scale)
        with self._cache_lock:
            if key in self._cache or key in self._pending:
                return
            self._pending.add(key)
        with self._queue_cond:
            if key not in self._queue:
                self._queue.append(key)
            self._queue_cond.notify()

    def _lookup(self, index: int, scale: float):
        """Exact texture if cached, else the nearest scale as a stand-in."""
        with self._cache_lock:
            exact = self._cache.get((index, scale))
            if exact is not None:
                self._cache.move_to_end((index, scale))
                return exact
            best, best_delta = None, None
            for (idx, sc), texture in self._cache.items():
                if idx != index:
                    continue
                delta = abs(sc - scale)
                if best_delta is None or delta < best_delta:
                    best, best_delta = texture, delta
            return best

    @staticmethod
    def _quantise(scale: float) -> float:
        return round(scale, 3)

    # -- snapshot ----------------------------------------------------------
    def snapshot_pages(self, widget, snapshot) -> None:
        width = widget.get_width()
        height = widget.get_height()
        dark = Adw.StyleManager.get_default().get_dark()
        snapshot.append_color(
            _CANVAS_DARK if dark else _CANVAS_LIGHT, _rect(0, 0, width, height)
        )
        if not self.document or not self._layout:
            return

        offset = self._origin_offset()
        vadj = self.scroller.get_vadjustment()
        view_top = vadj.get_value()
        view_bottom = view_top + max(self.viewport_size()[1], 1.0)
        scale = self._quantise(self.scale)

        for index in self._laid_out_pages():
            px, py, pw, ph = self._layout[index]
            if pw <= 0 or ph <= 0:
                continue
            if py + ph < view_top - _OVERSCAN or py > view_bottom + _OVERSCAN:
                continue
            self._snapshot_page(
                snapshot, index, px + PAGE_MARGIN + offset, py, pw, ph, scale
            )

        self._snapshot_selection(snapshot, offset)

    def _snapshot_page(self, snapshot, index, x, y, pw, ph, scale) -> None:
        snapshot.append_color(_SHADOW, _rect(x + 2, y + 3, pw, ph))
        snapshot.append_color(_PAPER, _rect(x, y, pw, ph))

        self._request(index, scale)
        texture = self._lookup(index, scale)

        snapshot.push_clip(_rect(x, y, pw, ph))
        snapshot.save()
        snapshot.translate(Graphene.Point().init(x, y))
        self._apply_rotation(snapshot, pw, ph)
        if texture is not None:
            # Draw into the unrotated page box; a stand-in texture rendered at
            # a different scale is stretched to fit, so it lands in the right
            # place until the exact one arrives.
            natural_w, natural_h = self.document.page_size(index)
            target = _rect(0, 0, natural_w * scale, natural_h * scale)
            exact = texture.get_width() == int(math.ceil(natural_w * scale))
            snapshot.append_scaled_texture(
                texture,
                Gsk.ScalingFilter.NEAREST if exact and scale >= 2.5
                else Gsk.ScalingFilter.TRILINEAR,
                target,
            )
        snapshot.restore()
        snapshot.pop()

        self._snapshot_highlights(snapshot, index, x, y, pw, ph, scale)
        snapshot.append_border(
            _square(_rect(x, y, pw, ph)),
            [1.0, 1.0, 1.0, 1.0],
            [_SHADOW, _SHADOW, _SHADOW, _SHADOW],
        )

    def _apply_rotation(self, snapshot, pw: float, ph: float) -> None:
        if self.rotation == 90:
            snapshot.translate(Graphene.Point().init(pw, 0))
            snapshot.rotate(90.0)
        elif self.rotation == 180:
            snapshot.translate(Graphene.Point().init(pw, ph))
            snapshot.rotate(180.0)
        elif self.rotation == 270:
            snapshot.translate(Graphene.Point().init(0, ph))
            snapshot.rotate(270.0)

    # -- search highlights -------------------------------------------------
    def set_highlights(self, hits) -> None:
        self._highlights = {}
        for hit in hits:
            if hit.box is not None:
                self._highlights.setdefault(hit.page, []).append(hit.box)
        self.area.queue_draw()

    def set_active_highlight(self, page: int, box) -> None:
        self._active_highlight = (page, box)
        self.area.queue_draw()

    def clear_highlights(self) -> None:
        self._highlights = {}
        self._active_highlight = None
        self.area.queue_draw()

    def _snapshot_highlights(self, snapshot, index, x, y, pw, ph, scale) -> None:
        boxes = self._highlights.get(index)
        if not boxes:
            return
        snapshot.save()
        snapshot.translate(Graphene.Point().init(x, y))
        self._apply_rotation(snapshot, pw, ph)
        for box in boxes:
            active = (
                self._active_highlight is not None
                and self._active_highlight[0] == index
                and self._active_highlight[1] is box
            )
            snapshot.append_color(
                _HIT_ACTIVE if active else _HIT,
                _rect(
                    box.x * scale, box.y * scale,
                    max(box.width * scale, 1.0), max(box.height * scale, 1.0),
                ),
            )
        snapshot.restore()

    # -- selection / crop --------------------------------------------------
    @property
    def selection(self):
        return self._selection

    def clear_selection(self) -> None:
        if self._selection is None:
            return
        self._selection = None
        self.emit("selection-changed", False)
        self.area.queue_draw()

    def set_select_mode(self, enabled: bool) -> None:
        self.select_mode = enabled
        if not enabled:
            self.clear_selection()

    def _selectable(self) -> bool:
        # Dragging pans by default; it only selects while the select tool is
        # active, and only for single-page documents (images), which is all we
        # can crop and export.
        return (
            self.select_mode
            and bool(self.document)
            and self.document.page_count == 1
        )

    def _snapshot_selection(self, snapshot, offset) -> None:
        if not self._selection or not self._layout:
            return
        index, sx, sy, sw, sh = self._selection
        if sw <= 0 or sh <= 0:
            return
        px, py, pw, ph = self._layout[index]
        snapshot.save()
        snapshot.translate(Graphene.Point().init(px + PAGE_MARGIN + offset, py))
        self._apply_rotation(snapshot, pw, ph)
        box = _rect(sx * self.scale, sy * self.scale, sw * self.scale, sh * self.scale)
        snapshot.append_color(_SEL_FILL, box)
        snapshot.append_border(
            _square(box), [1.0, 1.0, 1.0, 1.0],
            [_SEL_EDGE, _SEL_EDGE, _SEL_EDGE, _SEL_EDGE],
        )
        snapshot.restore()

    def _display_to_page(self, index: int, dx: float, dy: float):
        px, py, pw, ph = self._layout[index]
        x = dx - (px + PAGE_MARGIN + self._origin_offset())
        y = dy - py
        # Undo the view rotation to land in natural page coordinates.
        if self.rotation == 90:
            x, y = y, pw - x
        elif self.rotation == 180:
            x, y = pw - x, ph - y
        elif self.rotation == 270:
            x, y = ph - y, x
        return x / self.scale, y / self.scale

    def _on_drag_begin(self, gesture, start_x, start_y) -> None:
        hadj = self.scroller.get_hadjustment()
        vadj = self.scroller.get_vadjustment()
        self._drag_origin = (hadj.get_value(), vadj.get_value(), start_x, start_y)
        self._selecting = False
        if self._selectable():
            px, py, pw, ph = self._layout[0]
            x0 = px + PAGE_MARGIN + self._origin_offset()
            if x0 <= start_x <= x0 + pw and py <= start_y <= py + ph:
                self._selecting = True
                self._sel_anchor = self._display_to_page(0, start_x, start_y)

    def _on_drag_update(self, gesture, dx, dy) -> None:
        if not self._drag_origin:
            return
        ox, oy, sx, sy = self._drag_origin
        if self._selecting:
            cur = self._display_to_page(0, sx + dx, sy + dy)
            ax, ay = self._sel_anchor
            width, height = self.document.page_size(0)
            x0, x1 = sorted(
                (max(0.0, min(ax, width)), max(0.0, min(cur[0], width)))
            )
            y0, y1 = sorted(
                (max(0.0, min(ay, height)), max(0.0, min(cur[1], height)))
            )
            self._selection = (0, x0, y0, x1 - x0, y1 - y0)
            self.emit("selection-changed", (x1 - x0) > 2 and (y1 - y0) > 2)
            self.area.queue_draw()
            return
        # Otherwise the drag pans the document.
        self.scroller.get_hadjustment().set_value(ox - dx)
        self.scroller.get_vadjustment().set_value(oy - dy)

    def _on_drag_end(self, gesture, dx, dy) -> None:
        self._drag_origin = None
        self._selecting = False

    # -- navigation --------------------------------------------------------
    def _on_motion(self, controller, x, y) -> None:
        # Widget coordinates; the zoom anchor wants them relative to the
        # viewport, which is the frame the scroll adjustments use.
        self._pointer_pos = (
            x - self.scroller.get_hadjustment().get_value(),
            y - self.scroller.get_vadjustment().get_value(),
        )

    def _pointer(self) -> tuple[float, float]:
        px, py = self._pointer_pos
        if px <= 0 or py <= 0:
            view_w, view_h = self.viewport_size()
            return view_w / 2.0, view_h / 2.0
        return px, py

    def _on_scroll(self, controller, dx, dy) -> bool:
        event = controller.get_current_event()
        state = event.get_modifier_state() if event else 0
        if state & Gdk.ModifierType.CONTROL_MASK:
            self.set_scale(self.scale * (1.0 - dy * 0.12), anchor=self._pointer())
            return True
        return False

    def _on_scrolled(self, adjustment) -> None:
        self.area.queue_draw()
        if not self.document or not self.continuous:
            return
        centre = adjustment.get_value() + self.viewport_size()[1] / 2.0
        best, best_dist = self.current_page, None
        for index in range(self.document.page_count):
            _, py, _, ph = self._layout[index]
            if ph <= 0:
                continue
            dist = abs((py + ph / 2.0) - centre)
            if best_dist is None or dist < best_dist:
                best, best_dist = index, dist
        if best != self.current_page:
            self.current_page = best
            self.emit("page-changed", best)

    def goto_page(self, index: int, animate: bool = False) -> None:
        if not self.document:
            return
        index = max(0, min(self.document.page_count - 1, index))
        self.current_page = index
        if not self.continuous:
            self._rebuild_layout()
            self.scroller.get_vadjustment().set_value(0)
        else:
            _, py, _, _ = self._layout[index]
            self.scroller.get_vadjustment().set_value(max(0.0, py - PAGE_MARGIN))
        self.emit("page-changed", index)
        if self.zoom_mode in ("fit", "fill"):
            self._reflow_for_mode(force=True)
        self.area.queue_draw()

    def next_page(self) -> None:
        self.goto_page(self.current_page + 1)

    def previous_page(self) -> None:
        self.goto_page(self.current_page - 1)

    def set_continuous(self, continuous: bool) -> None:
        self.continuous = continuous
        self._rebuild_layout()
        self.goto_page(self.current_page)

    def scroll_by_page(self, direction: int) -> None:
        """Page Up/Down: scroll a screenful, stepping pages at the edges."""
        vadj = self.scroller.get_vadjustment()
        step = vadj.get_page_size() * 0.92
        target = vadj.get_value() + step * direction
        limit = max(0.0, vadj.get_upper() - vadj.get_page_size())
        if self.continuous:
            vadj.set_value(max(0.0, min(target, limit)))
            return
        if direction > 0 and vadj.get_value() >= limit - 1:
            self.next_page()
        elif direction < 0 and vadj.get_value() <= 1:
            self.previous_page()
        else:
            vadj.set_value(max(0.0, min(target, limit)))
