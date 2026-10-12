"""Tests for the document, export and settings layers.

These avoid constructing widgets, so they run without a display.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cairo
import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

from preview.backends.image import ImageDocument, supported_extensions  # noqa: E402
from preview.backends.pdf import PdfDocument, pdf_backend_name  # noqa: E402
from preview.document import UnsupportedFile, is_supported, open_document  # noqa: E402
from preview.export import (  # noqa: E402
    available_export_formats, export_page, render_page_to_pixbuf,
)
from preview.surface import (  # noqa: E402
    pixbuf_to_surface, surface_to_pixbuf, surface_to_texture,
)

TMP = tempfile.mkdtemp(prefix="preview-tests-")


def _make_image(path, width, height, alpha=False):
    fmt = cairo.FORMAT_ARGB32 if alpha else cairo.FORMAT_RGB24
    surface = cairo.ImageSurface(fmt, width, height)
    cr = cairo.Context(surface)
    if not alpha:
        cr.set_source_rgb(0.1, 0.4, 0.7)
        cr.paint()
    cr.set_source_rgb(0.95, 0.75, 0.1)
    cr.rectangle(width * 0.25, height * 0.25, width * 0.5, height * 0.5)
    cr.fill()
    surface.write_to_png(path)
    return path


def _make_pdf(path, pages=3):
    surface = cairo.PDFSurface(path, 595, 842)
    cr = cairo.Context(surface)
    for n in range(1, pages + 1):
        cr.set_source_rgb(1, 1, 1)
        cr.paint()
        cr.set_source_rgb(0, 0, 0)
        cr.select_font_face("Sans")
        cr.set_font_size(24)
        cr.move_to(60, 120)
        cr.show_text(f"Chapter {n} about widgets")
        cr.move_to(60, 160)
        cr.show_text("The quick brown fox jumps over the lazy dog.")
        cr.show_page()
    surface.finish()
    return path


class TestDispatch(unittest.TestCase):
    def test_image_opens(self):
        path = _make_image(os.path.join(TMP, "a.png"), 300, 200)
        doc = open_document(path)
        self.assertIsInstance(doc, ImageDocument)
        self.assertEqual(doc.page_count, 1)
        self.assertEqual(doc.page_size(0), (300.0, 200.0))

    def test_pdf_opens(self):
        path = _make_pdf(os.path.join(TMP, "a.pdf"))
        doc = open_document(path)
        self.assertIsInstance(doc, PdfDocument)
        self.assertEqual(doc.page_count, 3)
        width, height = doc.page_size(0)
        self.assertAlmostEqual(width, 595, delta=1)
        self.assertAlmostEqual(height, 842, delta=1)

    def test_missing_file(self):
        with self.assertRaises(UnsupportedFile):
            open_document(os.path.join(TMP, "nope.png"))

    def test_directory_rejected(self):
        with self.assertRaises(UnsupportedFile):
            open_document(TMP)

    def test_garbage_rejected(self):
        path = os.path.join(TMP, "junk.png")
        with open(path, "wb") as fh:
            fh.write(b"this is definitely not a png")
        with self.assertRaises(UnsupportedFile):
            open_document(path)

    def test_is_supported(self):
        self.assertTrue(is_supported("/x/y.png"))
        self.assertTrue(is_supported("/x/y.JPEG"))
        self.assertFalse(is_supported("/x/y.tar.gz"))
        self.assertIn(".png", supported_extensions())


class TestSurfaceBridge(unittest.TestCase):
    def test_roundtrip_is_lossless(self):
        path = _make_image(os.path.join(TMP, "rt.png"), 123, 77)
        pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
        surface = pixbuf_to_surface(pixbuf)
        back = surface_to_pixbuf(surface)
        self.assertEqual(back.get_width(), 123)
        self.assertEqual(back.get_height(), 77)
        src, dst = pixbuf.get_pixels(), back.get_pixels()
        ss, ds = pixbuf.get_rowstride(), back.get_rowstride()
        nch = pixbuf.get_n_channels()
        for y in range(0, 77, 9):
            for x in range(0, 123, 7):
                a = bytes(src[y * ss + x * nch : y * ss + x * nch + 3])
                b = bytes(dst[y * ds + x * 3 : y * ds + x * 3 + 3])
                self.assertEqual(a, b, f"pixel {x},{y}")

    def test_alpha_is_flattened_onto_white(self):
        path = _make_image(os.path.join(TMP, "alpha.png"), 40, 40, alpha=True)
        pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
        self.assertTrue(pixbuf.get_has_alpha())
        surface = pixbuf_to_surface(pixbuf)
        back = surface_to_pixbuf(surface)
        self.assertFalse(back.get_has_alpha())
        # A fully transparent corner must come back white, not black.
        corner = bytes(back.get_pixels()[0:3])
        self.assertEqual(corner, b"\xff\xff\xff")

    def test_texture_from_surface(self):
        surface = cairo.ImageSurface(cairo.FORMAT_RGB24, 32, 16)
        texture = surface_to_texture(surface)
        self.assertEqual((texture.get_width(), texture.get_height()), (32, 16))


@unittest.skipIf(pdf_backend_name() is None, "no PDF backend installed")
class TestPdf(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = PdfDocument(_make_pdf(os.path.join(TMP, "search.pdf"), pages=3))

    def test_text_extraction(self):
        text = self.doc.page_text(1)
        self.assertIn("Chapter 2", text)

    def test_search_finds_every_page(self):
        hits = list(self.doc.search("widgets"))
        self.assertEqual(len(hits), 3)
        self.assertEqual(sorted(h.page for h in hits), [0, 1, 2])

    def test_search_is_case_insensitive(self):
        self.assertTrue(list(self.doc.search("CHAPTER")))

    def test_search_empty_needle(self):
        self.assertEqual(list(self.doc.search("   ")), [])

    def test_render_produces_ink(self):
        pixbuf = render_page_to_pixbuf(self.doc, 0, 1.0)
        self.assertGreater(pixbuf.get_width(), 500)
        # The page must not be uniformly white, or nothing was drawn. Scan
        # the whole page: the top of a PDF page is margin, so sampling only
        # the first rows proves nothing.
        self.assertTrue(any(b != 0xFF for b in pixbuf.get_pixels()))


class TestExport(unittest.TestCase):
    def setUp(self):
        self.doc = open_document(_make_image(os.path.join(TMP, "e.png"), 400, 300))

    def test_every_advertised_format_writes_a_loadable_file(self):
        for fmt, ext, _label in available_export_formats():
            with self.subTest(fmt=fmt):
                out = os.path.join(TMP, f"out.{ext}")
                export_page(self.doc, 0, out, fmt)
                self.assertTrue(os.path.getsize(out) > 0)
                reloaded = GdkPixbuf.Pixbuf.new_from_file(out)
                self.assertEqual(reloaded.get_width(), 400)
                self.assertEqual(reloaded.get_height(), 300)

    def test_crop_geometry(self):
        out = os.path.join(TMP, "crop.png")
        export_page(self.doc, 0, out, "png", crop=(50.0, 40.0, 120.0, 90.0))
        cropped = GdkPixbuf.Pixbuf.new_from_file(out)
        self.assertEqual((cropped.get_width(), cropped.get_height()), (120, 90))

    def test_crop_clamped_to_page(self):
        out = os.path.join(TMP, "crop2.png")
        export_page(self.doc, 0, out, "png", crop=(380.0, 280.0, 500.0, 500.0))
        cropped = GdkPixbuf.Pixbuf.new_from_file(out)
        self.assertEqual((cropped.get_width(), cropped.get_height()), (20, 20))

    def test_rotation_swaps_dimensions(self):
        out = os.path.join(TMP, "rot.png")
        export_page(self.doc, 0, out, "png", rotation=90)
        rotated = GdkPixbuf.Pixbuf.new_from_file(out)
        self.assertEqual((rotated.get_width(), rotated.get_height()), (300, 400))

    def test_rotation_180_keeps_dimensions(self):
        out = os.path.join(TMP, "rot180.png")
        export_page(self.doc, 0, out, "png", rotation=180)
        rotated = GdkPixbuf.Pixbuf.new_from_file(out)
        self.assertEqual((rotated.get_width(), rotated.get_height()), (400, 300))

    def test_jpeg_from_transparent_source_is_opaque(self):
        doc = open_document(_make_image(os.path.join(TMP, "t.png"), 60, 60, alpha=True))
        out = os.path.join(TMP, "t.jpg")
        export_page(doc, 0, out, "jpeg")
        reloaded = GdkPixbuf.Pixbuf.new_from_file(out)
        self.assertFalse(reloaded.get_has_alpha())


class TestSettings(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="preview-cfg-")
        os.environ["XDG_CONFIG_HOME"] = self.home
        # Reimport so the module picks up the patched config dir.
        from gi.repository import GLib
        GLib.set_application_name("preview-test")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)
        os.environ.pop("XDG_CONFIG_HOME", None)

    def test_defaults_and_roundtrip(self):
        from preview.config import Settings
        settings = Settings()
        self.assertEqual(settings["zoom_mode"], "fit")
        settings["zoom_mode"] = "actual"
        settings.push_recent(__file__)
        settings.save()
        reloaded = Settings()
        self.assertEqual(reloaded["zoom_mode"], "actual")
        self.assertIn(os.path.abspath(__file__), reloaded.recent)

    def test_recent_drops_missing_files(self):
        from preview.config import Settings
        settings = Settings()
        settings.push_recent("/definitely/not/here.png")
        self.assertNotIn("/definitely/not/here.png", settings.recent)

    def test_corrupt_config_falls_back_to_defaults(self):
        from preview.config import Settings
        path = os.path.join(self.home, "preview", "settings.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("{ not json at all")
        self.assertEqual(Settings()["zoom_mode"], "fit")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
