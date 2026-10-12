"""The Preview window: tabs, header bar, actions and dialogs."""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, __version__
from .docview import DocumentView
from .document import UnsupportedFile, open_document
from .export import available_export_formats, default_export_name, export_page
from .printing import print_document

ZOOM_LABEL_WIDTH = 52


class PreviewWindow(Adw.ApplicationWindow):
    __gtype_name__ = "PreviewWindow"

    def __init__(self, application, settings) -> None:
        super().__init__(application=application, title="Preview")
        self.settings = settings
        self._slideshow_source = 0

        self.set_default_size(
            int(settings["window_width"]), int(settings["window_height"])
        )
        if settings["window_maximized"]:
            self.maximize()

        self.toasts = Adw.ToastOverlay()
        self.toolbar = Adw.ToolbarView()
        self.toasts.set_child(self.toolbar)
        self.set_content(self.toasts)

        self.tabs = Adw.TabView(vexpand=True)
        self.tabs.connect("notify::selected-page", lambda *_: self._sync_header())
        self.tabs.connect("close-page", self._on_close_page)
        self.tabs.connect("page-attached", lambda *_: self._sync_header())
        self.tabs.connect("page-detached", lambda *_: self._sync_header())

        self.tab_bar = Adw.TabBar(view=self.tabs, autohide=True)

        self.stack = Gtk.Stack()
        self.stack.add_named(self._build_empty_state(), "empty")
        self.stack.add_named(self.tabs, "documents")
        self.stack.set_visible_child_name("empty")

        self.toolbar.add_top_bar(self._build_header())
        self.toolbar.add_top_bar(self.tab_bar)
        self.toolbar.set_content(self.stack)

        self._install_actions()
        self._install_shortcuts()
        self._setup_drop_target()
        self.connect("close-request", self._on_close_request)
        self._sync_header()

    # -- construction ------------------------------------------------------
    def _build_empty_state(self) -> Gtk.Widget:
        page = Adw.StatusPage(
            icon_name="image-x-generic-symbolic",
            title="No Document Open",
            description="Open an image or PDF, or drop one here.",
        )
        button = Gtk.Button(label="Open File…")
        button.add_css_class("suggested-action")
        button.add_css_class("pill")
        button.set_halign(Gtk.Align.CENTER)
        button.connect("clicked", lambda *_: self.action_open())
        page.set_child(button)
        return page

    def _build_header(self) -> Gtk.Widget:
        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title="Preview", subtitle="")
        header.set_title_widget(self.window_title)

        self.sidebar_button = Gtk.ToggleButton(
            icon_name="sidebar-show-symbolic", tooltip_text="Thumbnails (F9)"
        )
        self.sidebar_button.connect("toggled", self._on_sidebar_toggled)
        header.pack_start(self.sidebar_button)

        open_button = Gtk.Button(
            icon_name="document-open-symbolic", tooltip_text="Open… (Ctrl+O)"
        )
        open_button.connect("clicked", lambda *_: self.action_open())
        header.pack_start(open_button)

        menu = Gio.Menu()
        doc_section = Gio.Menu()
        doc_section.append("Export As…", "win.export")
        doc_section.append("Print…", "win.print")
        menu.append_section(None, doc_section)

        view_section = Gio.Menu()
        view_section.append("Actual Size", "win.zoom-actual")
        view_section.append("Zoom to Fit", "win.zoom-fit")
        view_section.append("Zoom to Fill", "win.zoom-fill")
        view_section.append("Continuous Scroll", "win.toggle-continuous")
        view_section.append("Slideshow", "win.slideshow")
        view_section.append("Fullscreen", "win.fullscreen")
        menu.append_section(None, view_section)

        app_section = Gio.Menu()
        app_section.append("Keyboard Shortcuts", "win.shortcuts")
        app_section.append("About Preview", "win.about")
        menu.append_section(None, app_section)

        self.menu_button = Gtk.MenuButton(
            icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main Menu"
        )
        header.pack_end(self.menu_button)

        self.search_button = Gtk.ToggleButton(
            icon_name="system-search-symbolic", tooltip_text="Find (Ctrl+F)"
        )
        self.search_button.connect("toggled", self._on_search_toggled)
        header.pack_end(self.search_button)

        self.select_button = Gtk.ToggleButton(
            icon_name="edit-select-all-symbolic",
            tooltip_text="Rectangular selection, for cropping",
        )
        self.select_button.connect("toggled", self._on_select_toggled)
        header.pack_end(self.select_button)

        rotate_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        rotate_box.add_css_class("linked")
        left = Gtk.Button(
            icon_name="object-rotate-left-symbolic", tooltip_text="Rotate Left (Ctrl+L)"
        )
        right = Gtk.Button(
            icon_name="object-rotate-right-symbolic", tooltip_text="Rotate Right (Ctrl+R)"
        )
        left.connect("clicked", lambda *_: self._with_view(lambda v: v.rotate(-90)))
        right.connect("clicked", lambda *_: self._with_view(lambda v: v.rotate(90)))
        rotate_box.append(left)
        rotate_box.append(right)
        header.pack_end(rotate_box)

        zoom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        zoom_box.add_css_class("linked")
        zoom_out = Gtk.Button(icon_name="zoom-out-symbolic", tooltip_text="Zoom Out (Ctrl+−)")
        zoom_in = Gtk.Button(icon_name="zoom-in-symbolic", tooltip_text="Zoom In (Ctrl++)")
        zoom_out.connect("clicked", lambda *_: self._with_view(lambda v: v.zoom_out()))
        zoom_in.connect("clicked", lambda *_: self._with_view(lambda v: v.zoom_in()))
        self.zoom_label = Gtk.Button(label="100%", tooltip_text="Reset to 100% (Ctrl+0)")
        self.zoom_label.add_css_class("flat")
        self.zoom_label.set_size_request(ZOOM_LABEL_WIDTH, -1)
        self.zoom_label.connect(
            "clicked", lambda *_: self._with_view(lambda v: v.set_zoom_mode("actual"))
        )
        zoom_box.append(zoom_out)
        zoom_box.append(self.zoom_label)
        zoom_box.append(zoom_in)
        header.pack_end(zoom_box)

        self.page_label = Gtk.Label()
        self.page_label.add_css_class("numeric")
        self.page_label.add_css_class("dim-label")
        header.pack_end(self.page_label)
        return header

    def _setup_drop_target(self) -> None:
        drop = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)

        def on_drop(_target, value, _x, _y):
            if isinstance(value, Gio.File):
                path = value.get_path()
                if path:
                    self.open_path(path)
                    return True
            return False

        drop.connect("drop", on_drop)
        self.add_controller(drop)

    # -- actions -----------------------------------------------------------
    def _install_actions(self) -> None:
        simple = {
            "open": lambda *_: self.action_open(),
            "close-tab": lambda *_: self.action_close_tab(),
            "export": lambda *_: self.action_export(),
            "print": lambda *_: self.action_print(),
            "zoom-in": lambda *_: self._with_view(lambda v: v.zoom_in()),
            "zoom-out": lambda *_: self._with_view(lambda v: v.zoom_out()),
            "zoom-actual": lambda *_: self._with_view(lambda v: v.set_zoom_mode("actual")),
            "zoom-fit": lambda *_: self._with_view(lambda v: v.set_zoom_mode("fit")),
            "zoom-fill": lambda *_: self._with_view(lambda v: v.set_zoom_mode("fill")),
            "rotate-left": lambda *_: self._with_view(lambda v: v.rotate(-90)),
            "rotate-right": lambda *_: self._with_view(lambda v: v.rotate(90)),
            "toggle-sidebar": lambda *_: self.action_toggle_sidebar(),
            "toggle-continuous": lambda *_: self.action_toggle_continuous(),
            "next-page": lambda *_: self._with_view(lambda v: v.next_page()),
            "prev-page": lambda *_: self._with_view(lambda v: v.previous_page()),
            "first-page": lambda *_: self._with_view(lambda v: v.goto_page(0)),
            "last-page": lambda *_: self._with_view(
                lambda v: v.goto_page(v.document.page_count - 1)
            ),
            "goto-page": lambda *_: self.action_goto_page(),
            "page-down": lambda *_: self._with_view(lambda v: v.scroll_by_page(1)),
            "page-up": lambda *_: self._with_view(lambda v: v.scroll_by_page(-1)),
            "find": lambda *_: self.action_find(),
            "find-next": lambda *_: self._with_doc(lambda d: d.find_next()),
            "find-prev": lambda *_: self._with_doc(lambda d: d.find_previous()),
            "escape": lambda *_: self.action_escape(),
            "fullscreen": lambda *_: self.action_fullscreen(),
            "slideshow": lambda *_: self.action_slideshow(),
            "crop": lambda *_: self.action_crop(),
            "select-mode": lambda *_: self.select_button.set_active(
                not self.select_button.get_active()
            ),
            "shortcuts": lambda *_: self.action_shortcuts(),
            "about": lambda *_: self.action_about(),
        }
        for name, callback in simple.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

    def _install_shortcuts(self) -> None:
        accels = {
            "<Control>o": "win.open",
            "<Control>w": "win.close-tab",
            "<Control>plus": "win.zoom-in",
            "<Control>equal": "win.zoom-in",
            "<Control>KP_Add": "win.zoom-in",
            "<Control>minus": "win.zoom-out",
            "<Control>KP_Subtract": "win.zoom-out",
            "<Control>0": "win.zoom-actual",
            "<Control>9": "win.zoom-fit",
            "<Control>8": "win.zoom-fill",
            "<Control>l": "win.rotate-left",
            "<Control>r": "win.rotate-right",
            "F9": "win.toggle-sidebar",
            "<Control><Shift>d": "win.toggle-sidebar",
            "<Control>f": "win.find",
            "<Control>g": "win.find-next",
            "<Control><Shift>g": "win.find-prev",
            "<Control>p": "win.print",
            "<Control><Shift>s": "win.export",
            "<Control>k": "win.crop",
            "<Control>a": "win.select-mode",
            "<Control><Shift>j": "win.goto-page",
            "F11": "win.fullscreen",
            "<Control><Shift>f": "win.slideshow",
            "Escape": "win.escape",
            "Page_Down": "win.page-down",
            "Page_Up": "win.page-up",
            "<Control>Home": "win.first-page",
            "<Control>End": "win.last-page",
            "<Alt>Right": "win.next-page",
            "<Alt>Left": "win.prev-page",
        }
        controller = Gtk.ShortcutController()
        controller.set_scope(Gtk.ShortcutScope.GLOBAL)
        for accel, target in accels.items():
            controller.add_shortcut(
                Gtk.Shortcut(
                    trigger=Gtk.ShortcutTrigger.parse_string(accel),
                    action=Gtk.NamedAction.new(target),
                )
            )
        self.add_controller(controller)

    # -- helpers -----------------------------------------------------------
    @property
    def current(self) -> DocumentView | None:
        page = self.tabs.get_selected_page()
        return page.get_child() if page else None

    def _with_doc(self, func) -> None:
        doc = self.current
        if doc is not None:
            func(doc)

    def _with_view(self, func) -> None:
        doc = self.current
        if doc is not None:
            func(doc.view)
            self._sync_header()

    def toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=message, timeout=4))

    # -- opening -----------------------------------------------------------
    def open_path(self, path: str) -> bool:
        path = os.path.abspath(path)
        for page in self._all_pages():
            if page.get_child().path == path:
                self.tabs.set_selected_page(page)
                return True
        try:
            document = open_document(path)
        except (UnsupportedFile, OSError) as exc:
            self.toast(f"Could not open {os.path.basename(path)}: {exc}")
            return False

        view = DocumentView(document, self.settings)
        view.connect("state-changed", lambda *_: self._sync_header())
        view.connect("search-state-changed", lambda *_: self._sync_header())
        page = self.tabs.append(view)
        page.set_title(view.title)
        page.set_tooltip(path)
        self.tabs.set_selected_page(page)
        self.stack.set_visible_child_name("documents")
        self.settings.push_recent(path)
        self._sync_header()
        return True

    def _all_pages(self) -> list:
        return [self.tabs.get_nth_page(i) for i in range(self.tabs.get_n_pages())]

    def action_open(self) -> None:
        dialog = Gtk.FileDialog(title="Open")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        from .document import file_filters

        for label, mimes, globs in file_filters():
            f = Gtk.FileFilter()
            f.set_name(label)
            for mime in mimes:
                f.add_mime_type(mime)
            for pattern in globs:
                f.add_pattern(pattern)
            filters.append(f)
        every = Gtk.FileFilter()
        every.set_name("All Files")
        every.add_pattern("*")
        filters.append(every)
        dialog.set_filters(filters)

        def on_done(dlg, result):
            try:
                files = dlg.open_multiple_finish(result)
            except GLib.Error:
                return
            for index in range(files.get_n_items()):
                path = files.get_item(index).get_path()
                if path:
                    self.open_path(path)

        dialog.open_multiple(self, None, on_done)

    def action_close_tab(self) -> None:
        page = self.tabs.get_selected_page()
        if page:
            self.tabs.close_page(page)
        else:
            self.close()

    def _on_close_page(self, tabs, page) -> bool:
        view = page.get_child()
        view.shutdown()
        tabs.close_page_finish(page, True)
        if tabs.get_n_pages() <= 1:
            # The page is still attached while this handler runs.
            GLib.idle_add(self._maybe_show_empty)
        return True

    def _maybe_show_empty(self) -> bool:
        if self.tabs.get_n_pages() == 0:
            self.stack.set_visible_child_name("empty")
            self._sync_header()
        return False

    # -- view actions ------------------------------------------------------
    def action_toggle_sidebar(self) -> None:
        self._with_doc(lambda d: d.toggle_sidebar())
        self._sync_header()

    def _on_sidebar_toggled(self, button) -> None:
        doc = self.current
        if doc and doc.split.get_show_sidebar() != button.get_active():
            doc.split.set_show_sidebar(button.get_active())
            self.settings["sidebar_visible"] = button.get_active()

    def action_toggle_continuous(self) -> None:
        doc = self.current
        if not doc:
            return
        new_value = not doc.view.continuous
        doc.view.set_continuous(new_value)
        self.settings["continuous_scroll"] = new_value
        self.toast("Continuous scroll on" if new_value else "Single page")

    def _on_select_toggled(self, button) -> None:
        self._with_doc(lambda d: d.view.set_select_mode(button.get_active()))

    def _on_search_toggled(self, button) -> None:
        doc = self.current
        if not doc:
            return
        if button.get_active():
            doc.start_search()
        else:
            doc.stop_search()

    def action_find(self) -> None:
        doc = self.current
        if not doc:
            return
        if not doc.document.searchable:
            self.toast("This document has no searchable text")
            return
        self.search_button.set_active(True)
        doc.start_search()

    def action_escape(self) -> None:
        if self._slideshow_source:
            self.action_slideshow()
            return
        if self.is_fullscreen():
            self.unfullscreen()
            return
        doc = self.current
        if doc and doc.stop_search():
            self.search_button.set_active(False)
            return
        if doc:
            doc.view.clear_selection()

    def action_fullscreen(self) -> None:
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def action_slideshow(self) -> None:
        if self._slideshow_source:
            GLib.source_remove(self._slideshow_source)
            self._slideshow_source = 0
            self.unfullscreen()
            self.toast("Slideshow stopped")
            return
        doc = self.current
        if not doc:
            return
        self.fullscreen()
        doc.view.set_zoom_mode("fit")
        interval = max(1.0, float(self.settings["slideshow_interval"]))
        self._slideshow_source = GLib.timeout_add(
            int(interval * 1000), self._slideshow_tick
        )
        self.toast("Slideshow started — Esc to stop")

    def _slideshow_tick(self) -> bool:
        doc = self.current
        if not doc:
            self._slideshow_source = 0
            return False
        if doc.view.current_page + 1 >= doc.document.page_count:
            doc.view.goto_page(0)
        else:
            doc.view.next_page()
        return True

    def action_goto_page(self) -> None:
        doc = self.current
        if not doc or doc.document.page_count < 2:
            return
        dialog = Adw.AlertDialog(heading="Go to Page", body="")
        spin = Gtk.SpinButton.new_with_range(1, doc.document.page_count, 1)
        spin.set_value(doc.view.current_page + 1)
        spin.set_margin_top(8)
        spin.set_margin_start(12)
        spin.set_margin_end(12)
        dialog.set_extra_child(spin)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("go", "Go")
        dialog.set_default_response("go")
        dialog.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_dlg, response):
            if response == "go":
                doc.view.goto_page(int(spin.get_value()) - 1)

        dialog.connect("response", on_response)
        dialog.present(self)

    def action_crop(self) -> None:
        doc = self.current
        if not doc:
            return
        if not doc.has_selection:
            self.toast("Select a region first (toggle the selection tool)")
            return
        self.action_export(crop=True)

    # -- export / print ----------------------------------------------------
    def action_export(self, crop: bool = False) -> None:
        doc = self.current
        if not doc:
            return
        formats = available_export_formats()
        dialog = Gtk.FileDialog(title="Export As")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        for fmt, ext, label in formats:
            f = Gtk.FileFilter()
            f.set_name(f"{label} (*.{ext})")
            f.add_pattern(f"*.{ext}")
            filters.append(f)
        dialog.set_filters(filters)
        suffix = "-cropped" if crop else ""
        stem = os.path.splitext(os.path.basename(doc.path))[0]
        dialog.set_initial_name(f"{stem}{suffix}.png")

        def on_done(dlg, result):
            try:
                gfile = dlg.save_finish(result)
            except GLib.Error:
                return
            path = gfile.get_path()
            if not path:
                return
            ext = os.path.splitext(path)[1].lower().lstrip(".")
            fmt = next((f for f, e, _ in formats if e == ext or f == ext), None)
            if fmt is None:
                fmt, ext = "png", "png"
                path = os.path.splitext(path)[0] + ".png"
            selection = doc.view.selection if crop else None
            crop_rect = selection[1:] if selection else None
            # PDFs have no pixels of their own, so rasterise at 2× for print-ish
            # quality; images export at their native resolution.
            scale = 1.0 if doc.document.kind == "Image" else 2.0
            try:
                export_page(
                    doc.document,
                    doc.view.current_page,
                    path,
                    fmt,
                    scale=scale,
                    rotation=doc.view.rotation,
                    crop=crop_rect,
                )
            except OSError as exc:
                self.toast(f"Export failed: {exc}")
                return
            self.toast(f"Exported to {os.path.basename(path)}")

        dialog.save(self, None, on_done)

    def action_print(self) -> None:
        doc = self.current
        if not doc:
            return
        print_document(doc.document, self, doc.view.rotation, on_error=self.toast)

    # -- about / shortcuts -------------------------------------------------
    def action_about(self) -> None:
        from .backends.pdf import pdf_backend_name

        about = Adw.AboutDialog(
            application_name="Preview",
            application_icon=APP_ID,
            version=__version__,
            developer_name="Built for Linux desktops",
            comments=(
                "A fast image and PDF viewer for Linux, in the spirit of the "
                "macOS app of the same name.\n\n"
                f"PDF backend: {pdf_backend_name() or 'unavailable'}"
            ),
            license_type=Gtk.License.MIT_X11,
        )
        about.present(self)

    def action_shortcuts(self) -> None:
        groups = [
            ("Documents", [
                ("Ctrl+O", "Open"), ("Ctrl+W", "Close tab"),
                ("Ctrl+Shift+S", "Export as"), ("Ctrl+P", "Print"),
                ("Ctrl+K", "Crop to selection"), ("Ctrl+A", "Selection tool"),
            ]),
            ("View", [
                ("Ctrl++ / Ctrl+−", "Zoom in / out"), ("Ctrl+0", "Actual size"),
                ("Ctrl+9", "Zoom to fit"), ("Ctrl+8", "Zoom to fill"),
                ("Ctrl+scroll", "Zoom at pointer"),
                ("Ctrl+L / Ctrl+R", "Rotate left / right"),
                ("F9", "Thumbnails"), ("F11", "Fullscreen"),
                ("Ctrl+Shift+F", "Slideshow"),
            ]),
            ("Navigation", [
                ("Page Up / Page Down", "Scroll by screen"),
                ("Alt+← / Alt+→", "Previous / next page"),
                ("Ctrl+Home / Ctrl+End", "First / last page"),
                ("Ctrl+Shift+J", "Go to page"),
                ("Drag", "Pan the document"),
            ]),
            ("Find", [
                ("Ctrl+F", "Find in document"),
                ("Ctrl+G / Ctrl+Shift+G", "Next / previous match"),
                ("Esc", "Dismiss"),
            ]),
        ]
        window = Adw.Dialog(title="Keyboard Shortcuts", content_width=560)
        page = Adw.PreferencesPage()
        for name, rows in groups:
            group = Adw.PreferencesGroup(title=name)
            for accel, label in rows:
                row = Adw.ActionRow(title=label)
                chip = Gtk.Label(label=accel)
                chip.add_css_class("dim-label")
                chip.add_css_class("numeric")
                row.add_suffix(chip)
                group.add(row)
            page.add(group)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(page)
        window.set_child(toolbar)
        window.present(self)

    # -- header sync -------------------------------------------------------
    def _sync_header(self) -> None:
        doc = self.current
        has_doc = doc is not None
        for widget in (
            self.search_button, self.select_button, self.zoom_label,
            self.sidebar_button,
        ):
            widget.set_sensitive(has_doc)
        for name in (
            "export", "print", "zoom-in", "zoom-out", "zoom-actual", "zoom-fit",
            "zoom-fill", "rotate-left", "rotate-right", "toggle-sidebar",
            "toggle-continuous", "next-page", "prev-page", "first-page",
            "last-page", "goto-page", "page-down", "page-up", "find",
            "find-next", "find-prev", "slideshow", "crop", "select-mode",
            "close-tab",
        ):
            action = self.lookup_action(name)
            if action:
                action.set_enabled(has_doc)

        if not has_doc:
            self.window_title.set_title("Preview")
            self.window_title.set_subtitle("")
            self.page_label.set_text("")
            self.zoom_label.set_label("—")
            self.stack.set_visible_child_name("empty")
            return

        self.stack.set_visible_child_name("documents")
        self.window_title.set_title(doc.title)
        self.window_title.set_subtitle(doc.subtitle())
        self.zoom_label.set_label(f"{doc.view.scale * 100:.0f}%")
        if doc.document.page_count > 1:
            self.page_label.set_text(
                f"{doc.view.current_page + 1}/{doc.document.page_count}"
            )
        else:
            self.page_label.set_text("")

        if self.sidebar_button.get_active() != doc.split.get_show_sidebar():
            self.sidebar_button.handler_block_by_func(self._on_sidebar_toggled)
            self.sidebar_button.set_active(doc.split.get_show_sidebar())
            self.sidebar_button.handler_unblock_by_func(self._on_sidebar_toggled)
        self.sidebar_button.set_sensitive(doc.document.page_count > 1)
        self.search_button.set_sensitive(doc.document.searchable)
        self.select_button.set_sensitive(doc.document.page_count == 1)
        if self.select_button.get_active() != doc.view.select_mode:
            self.select_button.handler_block_by_func(self._on_select_toggled)
            self.select_button.set_active(doc.view.select_mode)
            self.select_button.handler_unblock_by_func(self._on_select_toggled)
        crop_action = self.lookup_action("crop")
        if crop_action:
            crop_action.set_enabled(doc.has_selection)

    # -- shutdown ----------------------------------------------------------
    def _on_close_request(self, *_args) -> bool:
        if self._slideshow_source:
            GLib.source_remove(self._slideshow_source)
            self._slideshow_source = 0
        width, height = self.get_default_size()
        self.settings["window_width"] = width
        self.settings["window_height"] = height
        self.settings["window_maximized"] = self.is_maximized()
        doc = self.current
        if doc:
            self.settings["zoom_mode"] = doc.view.zoom_mode
        for page in self._all_pages():
            page.get_child().shutdown()
        self.settings.save()
        return False
