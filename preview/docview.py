"""One open document: thumbnails, page view and the find bar.

Each tab owns an instance of this, so switching tabs keeps every document's
zoom, page and search state exactly where the user left it.
"""

from __future__ import annotations

import os
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, GObject, Gtk  # noqa: E402

from .canvas import PageView
from .sidebar import PageThumbnails


class DocumentView(Adw.Bin):
    """Thumbnail sidebar + page view + find bar for a single document."""

    __gtype_name__ = "PreviewDocumentView"

    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "search-state-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, document, settings) -> None:
        super().__init__()
        self.document = document
        self.settings = settings
        self.hits: list = []
        self.hit_index = -1
        self._search_generation = 0
        self._search_lock = threading.Lock()

        self.view = PageView()
        self.thumbs = PageThumbnails()

        self.split = Adw.OverlaySplitView()
        self.split.set_sidebar(self.thumbs)
        self.split.set_content(self._build_content())
        self.split.set_max_sidebar_width(200)
        self.split.set_min_sidebar_width(150)
        self.split.set_sidebar_width_fraction(0.16)
        self.set_child(self.split)

        self.view.connect("page-changed", self._on_page_changed)
        self.view.connect("zoom-changed", lambda *_: self.emit("state-changed"))
        self.view.connect("selection-changed", self._on_selection_changed)
        self.thumbs.connect("page-activated", self._on_thumb_activated)

        self.view.continuous = bool(settings["continuous_scroll"])
        self.view.zoom_mode = str(settings["zoom_mode"])
        self.view.set_document(document)
        self.thumbs.set_document(document)
        self.split.set_show_sidebar(
            bool(settings["sidebar_visible"]) and document.page_count > 1
        )
        self.has_selection = False

    def _build_content(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.search_bar = Gtk.SearchBar()
        self.search_bar.set_show_close_button(True)

        entry_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.search_entry = Gtk.SearchEntry(hexpand=True)
        self.search_entry.set_placeholder_text("Find in document")
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.connect("activate", lambda *_: self.find_next())
        self.search_label = Gtk.Label()
        self.search_label.add_css_class("dim-label")
        prev_btn = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Previous match")
        next_btn = Gtk.Button(icon_name="go-down-symbolic", tooltip_text="Next match")
        prev_btn.connect("clicked", lambda *_: self.find_previous())
        next_btn.connect("clicked", lambda *_: self.find_next())
        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        nav.add_css_class("linked")
        nav.append(prev_btn)
        nav.append(next_btn)
        entry_box.append(self.search_entry)
        entry_box.append(self.search_label)
        entry_box.append(nav)
        self.search_bar.set_child(entry_box)
        self.search_bar.connect_entry(self.search_entry)

        box.append(self.search_bar)
        box.append(self.view)
        return box

    # -- state -------------------------------------------------------------
    @property
    def path(self) -> str:
        return self.document.path

    @property
    def title(self) -> str:
        return os.path.basename(self.document.path)

    def subtitle(self) -> str:
        parts = []
        if self.document.page_count > 1:
            parts.append(
                f"Page {self.view.current_page + 1} of {self.document.page_count}"
            )
        else:
            width, height = self.document.page_size(0)
            parts.append(f"{int(round(width))} × {int(round(height))}")
        parts.append(f"{self.view.scale * 100:.0f}%")
        return "  ·  ".join(parts)

    def _on_page_changed(self, _view, index: int) -> None:
        self.thumbs.set_current_page(index)
        self.emit("state-changed")

    def _on_thumb_activated(self, _thumbs, index: int) -> None:
        if index != self.view.current_page:
            self.view.goto_page(index)

    def _on_selection_changed(self, _view, has_selection: bool) -> None:
        self.has_selection = has_selection
        self.emit("state-changed")

    def shutdown(self) -> None:
        self.view.shutdown()
        self.thumbs.shutdown()
        try:
            self.document.close()
        except Exception:
            pass

    # -- sidebar -----------------------------------------------------------
    def toggle_sidebar(self) -> None:
        self.split.set_show_sidebar(not self.split.get_show_sidebar())
        self.settings["sidebar_visible"] = self.split.get_show_sidebar()

    # -- search ------------------------------------------------------------
    def start_search(self) -> None:
        if not self.document.searchable:
            return
        self.search_bar.set_search_mode(True)
        self.search_entry.grab_focus()

    def stop_search(self) -> bool:
        if self.search_bar.get_search_mode():
            self.search_bar.set_search_mode(False)
            self.view.clear_highlights()
            self.hits = []
            self.hit_index = -1
            self.emit("search-state-changed")
            return True
        return False

    def _on_search_changed(self, entry) -> None:
        needle = entry.get_text().strip()
        self._search_generation += 1
        generation = self._search_generation
        if len(needle) < 2:
            self.hits = []
            self.hit_index = -1
            self.view.clear_highlights()
            self.search_label.set_text("")
            self.emit("search-state-changed")
            return
        self.search_label.set_text("Searching…")

        def work():
            try:
                found = list(self.document.search(needle))
            except Exception:
                found = []
            GLib.idle_add(self._search_done, generation, found)

        threading.Thread(target=work, daemon=True).start()

    def _search_done(self, generation: int, found: list) -> bool:
        if generation != self._search_generation:
            return False  # A newer query superseded this one.
        self.hits = found
        self.hit_index = -1
        self.view.set_highlights(found)
        if not found:
            self.search_label.set_text("No matches")
        else:
            self.search_label.set_text(f"{len(found)} match" + ("es" if len(found) != 1 else ""))
            self.find_next()
        self.emit("search-state-changed")
        return False

    def _focus_hit(self) -> None:
        if not self.hits:
            return
        hit = self.hits[self.hit_index]
        self.view.goto_page(hit.page)
        self.view.set_active_highlight(hit.page, hit.box)
        self.search_label.set_text(f"{self.hit_index + 1} of {len(self.hits)}")

    def find_next(self) -> None:
        if not self.hits:
            return
        self.hit_index = (self.hit_index + 1) % len(self.hits)
        self._focus_hit()

    def find_previous(self) -> None:
        if not self.hits:
            return
        self.hit_index = (self.hit_index - 1) % len(self.hits)
        self._focus_hit()
