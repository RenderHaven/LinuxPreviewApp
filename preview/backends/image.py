"""Raster and vector image documents, backed by GdkPixbuf.

GdkPixbuf gives us whatever loaders the host has installed, which on a current
Ubuntu means PNG, JPEG, WebP, AVIF, HEIC, JXL, TIFF, SVG and friends.
"""

from __future__ import annotations

import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gdk", "4.0")

from gi.repository import GdkPixbuf, GLib  # noqa: E402

from ..surface import pixbuf_to_surface
from .base import Document


def supported_extensions() -> set[str]:
    exts: set[str] = set()
    for fmt in GdkPixbuf.Pixbuf.get_formats():
        for ext in fmt.get_extensions():
            exts.add("." + ext.lower())
    return exts


def supported_mime_types() -> set[str]:
    mimes: set[str] = set()
    for fmt in GdkPixbuf.Pixbuf.get_formats():
        mimes.update(fmt.get_mime_types())
    return mimes


def writable_formats() -> list[tuple[str, str]]:
    """``(format_name, primary_extension)`` for every format we can export."""
    out: list[tuple[str, str]] = []
    for fmt in GdkPixbuf.Pixbuf.get_formats():
        if not fmt.is_writable():
            continue
        exts = fmt.get_extensions()
        if exts:
            out.append((fmt.get_name(), exts[0]))
    return sorted(out)


class ImageDocument(Document):
    """A single-page document wrapping one image file."""

    kind = "Image"
    searchable = False

    def __init__(self, path: str) -> None:
        super().__init__(path)
        try:
            self.pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
        except GLib.Error as exc:  # pragma: no cover - surfaced in the UI
            raise OSError(exc.message) from exc
        self._width = float(self.pixbuf.get_width())
        self._height = float(self.pixbuf.get_height())
        self._surface = None

    @property
    def page_count(self) -> int:
        return 1

    def page_size(self, index: int) -> tuple[float, float]:
        return self._width, self._height

    def _cairo_surface(self):
        # The pixel shuffle is only worth doing once per open document.
        if self._surface is None:
            self._surface = pixbuf_to_surface(self.pixbuf)
        return self._surface

    def render_page(self, index: int, cr, scale: float) -> None:
        import cairo

        cr.save()
        cr.scale(scale, scale)
        cr.set_source_surface(self._cairo_surface(), 0, 0)
        # Smooth when shrinking, keep pixels crisp when zoomed way in so that
        # inspecting an image at 800% shows real pixels rather than mush.
        cr.get_source().set_filter(
            cairo.Filter.NEAREST if scale >= 2.5 else cairo.Filter.GOOD
        )
        cr.paint()
        cr.restore()

    # Export helpers -------------------------------------------------------
    def megapixels(self) -> float:
        return (self._width * self._height) / 1_000_000.0
