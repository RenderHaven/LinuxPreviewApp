"""Thumbnail sidebar.

A :class:`Gtk.ListView` recycles its rows, so only the thumbnails actually on
screen are ever requested.  That matters because generating one thumbnail can
mean a ``pdftoppm`` fork when the CLI backend is in use.
"""

from __future__ import annotations

import math
import threading

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, GObject, Gtk  # noqa: E402

from .surface import surface_to_pixbuf

THUMB_WIDTH = 116


class PageThumbnails(Gtk.Box):
    """Scrollable list of page thumbnails with the current page highlighted."""

    __gtype_name__ = "PreviewPageThumbnails"

    __gsignals__ = {
        "page-activated": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.document = None
        self._textures: dict[int, Gdk.Texture] = {}
        self._pending: set[int] = set()
        self._lock = threading.Lock()
        self._queue: list[int] = []
        self._cond = threading.Condition()
        self._stopping = False
        self._worker: threading.Thread | None = None
        self._suppress_select = False
        self._rows: dict[int, Gtk.Picture] = {}

        self.model = Gtk.StringList()
        self.selection = Gtk.SingleSelection(model=self.model)
        self.selection.set_can_unselect(False)
        self.selection.connect("notify::selected", self._on_selected)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_setup)
        factory.connect("bind", self._on_bind)

        self.view = Gtk.ListView(model=self.selection, factory=factory)
        self.view.add_css_class("navigation-sidebar")
        self.view.set_single_click_activate(True)

        scroller = Gtk.ScrolledWindow(hexpand=False, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.view)
        self.append(scroller)
        self.set_size_request(THUMB_WIDTH + 44, -1)

    # -- document ----------------------------------------------------------
    def set_document(self, document) -> None:
        self._stop_worker()
        with self._lock:
            self._textures.clear()
            self._pending.clear()
        self.document = document
        self._rows.clear()
        self._suppress_select = True
        self.model.splice(0, self.model.get_n_items(), [])
        if document is not None:
            self.model.splice(
                0, 0, [str(i + 1) for i in range(document.page_count)]
            )
        self._suppress_select = False
        if document is not None:
            self._start_worker()

    def shutdown(self) -> None:
        self._stop_worker()

    def set_current_page(self, index: int) -> None:
        if self.selection.get_selected() == index:
            return
        self._suppress_select = True
        self.selection.set_selected(index)
        self._suppress_select = False

    def _on_selected(self, selection, _param) -> None:
        if self._suppress_select:
            return
        index = selection.get_selected()
        if index != Gtk.INVALID_LIST_POSITION:
            self.emit("page-activated", index)

    # -- row factory -------------------------------------------------------
    def _on_setup(self, factory, item) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        frame = Gtk.Frame()
        frame.add_css_class("preview-thumb")
        picture = Gtk.Picture()
        picture.set_size_request(THUMB_WIDTH, -1)
        picture.set_can_shrink(True)
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        frame.set_child(picture)
        label = Gtk.Label()
        label.add_css_class("caption")
        label.add_css_class("dim-label")
        box.append(frame)
        box.append(label)
        item.set_child(box)

    def _on_bind(self, factory, item) -> None:
        index = item.get_position()
        box = item.get_child()
        frame = box.get_first_child()
        picture = frame.get_child()
        label = box.get_last_child()
        label.set_text(str(index + 1))

        if self.document is None:
            return
        width, height = self.document.page_size(index)
        ratio = (height / width) if width else 1.4
        picture.set_size_request(THUMB_WIDTH, int(THUMB_WIDTH * ratio))

        # Rows are recycled, so stamp the page this picture currently shows.
        # _deliver checks the stamp before painting, otherwise a slow thumbnail
        # could land in a row that has since scrolled onto a different page.
        picture.preview_page = index
        self._rows[index] = picture

        with self._lock:
            texture = self._textures.get(index)
        if texture is not None:
            picture.set_paintable(texture)
        else:
            picture.set_paintable(None)
            self._request(index)

    # -- rendering ---------------------------------------------------------
    def _start_worker(self) -> None:
        self._stopping = False
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def _stop_worker(self) -> None:
        with self._cond:
            self._stopping = True
            self._queue.clear()
            self._cond.notify_all()
        worker, self._worker = self._worker, None
        if worker and worker.is_alive():
            worker.join(timeout=2.0)

    def _request(self, index: int) -> None:
        with self._lock:
            if index in self._textures or index in self._pending:
                return
            self._pending.add(index)
        with self._cond:
            self._queue.append(index)
            self._cond.notify()

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    return
                index = self._queue.pop()
            document = self.document
            if document is None:
                continue
            try:
                width, height = document.page_size(index)
                scale = THUMB_WIDTH / max(width, 1.0)
                pw = max(1, int(math.ceil(width * scale)))
                ph = max(1, int(math.ceil(height * scale)))
                surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, pw, ph)
                cr = cairo.Context(surface)
                cr.set_source_rgb(1, 1, 1)
                cr.paint()
                document.render_page(index, cr, scale)
                surface.flush()
            except Exception:
                surface = None
            GLib.idle_add(
                self._deliver, index, surface, priority=GLib.PRIORITY_LOW
            )

    def _deliver(self, index: int, surface) -> bool:
        if surface is None:
            with self._lock:
                self._pending.discard(index)
            return False
        try:
            pixbuf = surface_to_pixbuf(surface)
        except Exception:
            pixbuf = None
        texture = Gdk.Texture.new_for_pixbuf(pixbuf) if pixbuf else None
        with self._lock:
            self._pending.discard(index)
            if texture is not None:
                self._textures[index] = texture
        picture = self._rows.get(index)
        if (
            texture is not None
            and picture is not None
            and getattr(picture, "preview_page", None) == index
        ):
            picture.set_paintable(texture)
        return False
