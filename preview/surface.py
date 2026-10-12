"""Conversion between GdkPixbuf and Cairo image surfaces.

``Gdk.cairo_set_source_pixbuf`` and ``Gdk.pixbuf_get_from_surface`` would do
this for us, but both are deprecated and — more importantly — they need the
cairo/GObject foreign-struct bridge from ``python3-gi-cairo``, which is not
installed by default.  Doing the byte shuffling ourselves keeps the dependency
list short and the code working everywhere.

Both directions use whole-buffer slice assignment, which runs in C inside
CPython: converting a 1400x900 image costs a few milliseconds.
"""

from __future__ import annotations

import cairo
import gi

gi.require_version("GdkPixbuf", "2.0")

from gi.repository import GdkPixbuf, GLib  # noqa: E402


def pixbuf_to_surface(pixbuf: GdkPixbuf.Pixbuf) -> cairo.ImageSurface:
    """Return an opaque ``RGB24`` surface holding ``pixbuf``'s pixels.

    Transparent images are composited onto white first.  That matches what the
    viewer wants anyway, since every page is drawn on a white sheet, and it
    avoids having to premultiply alpha in Python.
    """
    if pixbuf.get_has_alpha():
        width, height = pixbuf.get_width(), pixbuf.get_height()
        flat = GdkPixbuf.Pixbuf.new(
            GdkPixbuf.Colorspace.RGB, False, 8, width, height
        )
        flat.fill(0xFFFFFFFF)
        # composite() is implemented in C, so this stays cheap.
        pixbuf.composite(
            flat, 0, 0, width, height, 0, 0, 1, 1,
            GdkPixbuf.InterpType.NEAREST, 255,
        )
        pixbuf = flat

    width = pixbuf.get_width()
    height = pixbuf.get_height()
    src_stride = pixbuf.get_rowstride()
    src = pixbuf.get_pixels()
    dst_stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_RGB24, width)
    dst = bytearray(dst_stride * height)
    row_bytes = width * 3

    if src_stride == row_bytes and dst_stride == width * 4:
        # No padding anywhere: one pass over the whole image.
        dst[0::4] = src[2::3]  # B
        dst[1::4] = src[1::3]  # G
        dst[2::4] = src[0::3]  # R
    else:
        for y in range(height):
            s0 = y * src_stride
            row = src[s0 : s0 + row_bytes]
            d0 = y * dst_stride
            d1 = d0 + width * 4
            dst[d0 + 0 : d1 : 4] = row[2::3]
            dst[d0 + 1 : d1 : 4] = row[1::3]
            dst[d0 + 2 : d1 : 4] = row[0::3]

    # create_for_data does not copy; pycairo keeps a reference to the buffer
    # for us, so the surface stays valid after this function returns.
    return cairo.ImageSurface.create_for_data(
        dst, cairo.FORMAT_RGB24, width, height, dst_stride
    )


def surface_to_pixbuf(surface: cairo.ImageSurface) -> GdkPixbuf.Pixbuf:
    """Return an opaque RGB pixbuf holding ``surface``'s pixels.

    The surfaces we hand in are painted onto white before anything else is
    drawn, so alpha is 255 throughout and no un-premultiplying is needed.
    """
    surface.flush()
    fmt = surface.get_format()
    if fmt not in (cairo.FORMAT_ARGB32, cairo.FORMAT_RGB24):
        raise ValueError(f"unsupported surface format {fmt}")

    width = surface.get_width()
    height = surface.get_height()
    src_stride = surface.get_stride()
    src = bytes(surface.get_data())
    dst_stride = width * 3
    dst = bytearray(dst_stride * height)

    if src_stride == width * 4:
        dst[0::3] = src[2::4]  # R <- B
        dst[1::3] = src[1::4]  # G
        dst[2::3] = src[0::4]  # B <- R
    else:
        for y in range(height):
            s0 = y * src_stride
            row = src[s0 : s0 + width * 4]
            d0 = y * dst_stride
            d1 = d0 + dst_stride
            dst[d0 + 0 : d1 : 3] = row[2::4]
            dst[d0 + 1 : d1 : 3] = row[1::4]
            dst[d0 + 2 : d1 : 3] = row[0::4]

    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(bytes(dst)),
        GdkPixbuf.Colorspace.RGB,
        False, 8, width, height, dst_stride,
    )


def paint_pixbuf(cr: cairo.Context, pixbuf: GdkPixbuf.Pixbuf, filter_) -> None:
    """Paint ``pixbuf`` at the context's current origin and scale."""
    surface = pixbuf_to_surface(pixbuf)
    cr.set_source_surface(surface, 0, 0)
    cr.get_source().set_filter(filter_)
    cr.paint()


def surface_to_texture(surface: cairo.ImageSurface):
    """Wrap a Cairo image surface as a :class:`Gdk.Texture` without copying rows.

    Cairo's ``RGB24``/``ARGB32`` pixels are 32-bit native-endian words, so on a
    little-endian host the bytes already sit in the order GDK calls
    ``B8G8R8X8``/``B8G8R8A8_PREMULTIPLIED``.  That makes this a straight handoff
    of the pixel buffer; on a big-endian host we fall back to the slower
    pixbuf path, which does the shuffling explicitly.
    """
    import sys

    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk

    surface.flush()
    fmt = surface.get_format()
    if sys.byteorder == "little" and fmt in (cairo.FORMAT_RGB24, cairo.FORMAT_ARGB32):
        memory_format = (
            Gdk.MemoryFormat.B8G8R8X8
            if fmt == cairo.FORMAT_RGB24
            else Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED
        )
        return Gdk.MemoryTexture.new(
            surface.get_width(),
            surface.get_height(),
            memory_format,
            GLib.Bytes.new(bytes(surface.get_data())),
            surface.get_stride(),
        )

    pixbuf = surface_to_pixbuf(surface)
    return Gdk.MemoryTexture.new(
        pixbuf.get_width(),
        pixbuf.get_height(),
        Gdk.MemoryFormat.R8G8B8,
        GLib.Bytes.new(pixbuf.get_pixels()),
        pixbuf.get_rowstride(),
    )
