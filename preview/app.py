"""Application object and command-line entry point."""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, __version__
from .config import Settings
from .window import PreviewWindow

_CSS = b"""
.preview-thumb {
  background: #ffffff;
  border-radius: 3px;
}
.preview-thumb > picture {
  border-radius: 2px;
}
"""


class PreviewApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            # Files arrive via the "open" signal; one window serves them all.
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        self.settings = Settings()
        self._window: PreviewWindow | None = None

        self.add_main_option(
            "version", ord("v"), GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE, "Show the version and exit", None,
        )
        self.connect("handle-local-options", self._on_local_options)

    def _on_local_options(self, _app, options) -> int:
        if options.contains("version"):
            print(f"Preview {__version__}")
            return 0
        return -1  # Carry on with normal startup.

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(_CSS)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def _ensure_window(self) -> PreviewWindow:
        if self._window is None:
            self._window = PreviewWindow(self, self.settings)
            self._window.connect("destroy", self._on_window_destroyed)
        return self._window

    def _on_window_destroyed(self, *_args) -> None:
        self._window = None

    def do_activate(self) -> None:
        window = self._ensure_window()
        window.present()

    def do_open(self, files, n_files, hint) -> None:
        window = self._ensure_window()
        opened = 0
        for gfile in files[:n_files]:
            path = gfile.get_path()
            if path and window.open_path(path):
                opened += 1
        window.present()
        if opened == 0 and n_files:
            window.toast("Nothing could be opened")


def main(argv: list[str] | None = None) -> int:
    return PreviewApplication().run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
