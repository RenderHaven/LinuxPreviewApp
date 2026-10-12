"""Saving and converting: Export As, crop, and rendering PDF pages to images."""

from __future__ import annotations

import functools
import math
import os

import cairo
import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gdk", "4.0")

from gi.repository import GdkPixbuf, GLib  # noqa: E402

from .backends.image import ImageDocument
from .surface import surface_to_pixbuf

# GdkPixbuf's writable set, mapped to the extension and label we show.
EXPORT_FORMATS: list[tuple[str, str, str]] = [
    ("png", "png", "PNG Image"),
    ("jpeg", "jpg", "JPEG Image"),
    ("webp", "webp", "WebP Image"),
    ("tiff", "tiff", "TIFF Image"),
    ("bmp", "bmp", "BMP Image"),
    ("avif", "avif", "AVIF Image"),
    ("jxl", "jxl", "JPEG XL Image"),
]


@functools.lru_cache(maxsize=1)
def available_export_formats() -> tuple[tuple[str, str, str], ...]:
    """Formats this gdk-pixbuf build can genuinely save.

    ``is_writable()`` is not enough on its own: a loader can advertise write
    support that the installed build does not actually implement (JPEG XL does
    exactly this on Ubuntu), and we would only find out when the user has
    already picked a filename.  So each candidate is probed once by saving a
    1x1 pixbuf to memory.
    """
    writable = {
        fmt.get_name() for fmt in GdkPixbuf.Pixbuf.get_formats() if fmt.is_writable()
    }
    probe = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 1, 1)
    probe.fill(0x000000FF)
    usable = []
    for entry in EXPORT_FORMATS:
        fmt = entry[0]
        if fmt not in writable:
            continue
        keys, values = _save_options(fmt, 90)
        try:
            ok, _buffer = probe.save_to_bufferv(fmt, keys, values)
        except GLib.Error:
            continue
        if ok:
            usable.append(entry)
    return tuple(usable)


def _save_options(fmt: str, quality: int) -> tuple[list[str], list[str]]:
    if fmt in ("jpeg", "webp", "avif"):
        return ["quality"], [str(quality)]
    if fmt == "png":
        return ["compression"], ["6"]
    return [], []


def render_page_to_pixbuf(document, index: int, scale: float) -> GdkPixbuf.Pixbuf:
    """Rasterise any page (image or PDF) into a pixbuf at ``scale``."""
    width, height = document.page_size(index)
    pw = max(1, int(math.ceil(width * scale)))
    ph = max(1, int(math.ceil(height * scale)))
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, pw, ph)
    cr = cairo.Context(surface)
    cr.set_source_rgb(1, 1, 1)
    cr.paint()
    document.render_page(index, cr, scale)
    surface.flush()
    try:
        return surface_to_pixbuf(surface)
    except (ValueError, MemoryError) as exc:
        raise OSError(f"Could not rasterise the page: {exc}") from exc


def _apply_rotation(pixbuf: GdkPixbuf.Pixbuf, rotation: int) -> GdkPixbuf.Pixbuf:
    if rotation % 360 == 0:
        return pixbuf
    mapping = {
        90: GdkPixbuf.PixbufRotation.CLOCKWISE,
        180: GdkPixbuf.PixbufRotation.UPSIDEDOWN,
        270: GdkPixbuf.PixbufRotation.COUNTERCLOCKWISE,
    }
    rotated = pixbuf.rotate_simple(mapping[rotation % 360])
    return rotated or pixbuf


def export_page(
    document,
    index: int,
    path: str,
    fmt: str,
    *,
    scale: float = 1.0,
    rotation: int = 0,
    crop: tuple[float, float, float, float] | None = None,
    quality: int = 92,
) -> None:
    """Write one page to ``path`` in ``fmt``, honouring rotation and crop.

    Images are exported from their own pixbuf so no quality is lost to an
    intermediate rasterisation; PDF pages are rendered at ``scale``.
    """
    if isinstance(document, ImageDocument) and abs(scale - 1.0) < 1e-6:
        pixbuf = document.pixbuf
    else:
        pixbuf = render_page_to_pixbuf(document, index, scale)

    if crop:
        cx, cy, cw, ch = crop
        cx = int(max(0, round(cx * scale)))
        cy = int(max(0, round(cy * scale)))
        cw = int(min(pixbuf.get_width() - cx, round(cw * scale)))
        ch = int(min(pixbuf.get_height() - cy, round(ch * scale)))
        if cw > 0 and ch > 0:
            pixbuf = pixbuf.new_subpixbuf(cx, cy, cw, ch)

    pixbuf = _apply_rotation(pixbuf, rotation)

    if fmt in ("jpeg", "bmp") and pixbuf.get_has_alpha():
        # These formats have no alpha channel; composite onto white rather
        # than letting the loader turn transparency into noise.
        flat = GdkPixbuf.Pixbuf.new(
            GdkPixbuf.Colorspace.RGB, False, 8,
            pixbuf.get_width(), pixbuf.get_height(),
        )
        flat.fill(0xFFFFFFFF)
        pixbuf.composite(
            flat, 0, 0, pixbuf.get_width(), pixbuf.get_height(),
            0, 0, 1, 1, GdkPixbuf.InterpType.NEAREST, 255,
        )
        pixbuf = flat

    keys, values = _save_options(fmt, quality)
    try:
        pixbuf.savev(path, fmt, keys, values)
    except GLib.Error as exc:
        raise OSError(exc.message) from exc


def default_export_name(source: str, ext: str) -> str:
    stem = os.path.splitext(os.path.basename(source))[0]
    return f"{stem}.{ext}"
