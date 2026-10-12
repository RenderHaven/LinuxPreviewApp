"""PDF documents.

Two interchangeable implementations sit behind :class:`PdfDocument`:

``poppler``
    The Poppler GObject bindings (``gir1.2-poppler-0.18``).  Pages are painted
    straight onto our Cairo context, text extraction and word-level search
    rectangles come for free.  This is the path we want.

``poppler-utils``
    A fallback that shells out to ``pdftoppm``/``pdfinfo``/``pdftotext``.  It
    needs no typelib, so Preview still opens PDFs on a box where only the
    command line tools are installed.  Search finds pages but cannot highlight
    individual words, because the CLI does not report glyph boxes.

The backend is chosen once, at import time, and reported by
:func:`pdf_backend_name` so the UI can explain any reduced capability.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from typing import Iterator

import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gdk", "4.0")

from gi.repository import GdkPixbuf, GLib  # noqa: E402

from ..surface import pixbuf_to_surface
from .base import Document, PageBox, SearchHit

_POPPLER = None
try:  # pragma: no cover - depends on host packages
    gi.require_version("Poppler", "0.18")
    from gi.repository import Poppler as _POPPLER  # type: ignore
except (ValueError, ImportError):
    _POPPLER = None

_CLI_RENDER = shutil.which("pdftoppm")
_CLI_INFO = shutil.which("pdfinfo")
_CLI_TEXT = shutil.which("pdftotext")

# Rendering a page through the CLI costs a fork plus a PNG decode, so keep the
# resolution sane; the view never asks for more than it can show anyway.
_MAX_CLI_DPI = 600.0


def pdf_backend_name() -> str | None:
    if _POPPLER is not None:
        return "poppler"
    if _CLI_RENDER and _CLI_INFO:
        return "poppler-utils"
    return None


def pdf_available() -> bool:
    return pdf_backend_name() is not None


class _PopplerBackend:
    name = "poppler"
    searchable = True

    def __init__(self, path: str) -> None:
        uri = GLib.filename_to_uri(os.path.abspath(path), None)
        try:
            self.doc = _POPPLER.Document.new_from_file(uri, None)
        except GLib.Error as exc:
            raise OSError(exc.message) from exc
        self._pages: dict[int, object] = {}

    def _page(self, index: int):
        page = self._pages.get(index)
        if page is None:
            page = self.doc.get_page(index)
            self._pages[index] = page
        return page

    @property
    def page_count(self) -> int:
        return self.doc.get_n_pages()

    def page_size(self, index: int) -> tuple[float, float]:
        return self._page(index).get_size()

    def render_page(self, index: int, cr, scale: float) -> None:
        page = self._page(index)
        width, height = page.get_size()
        cr.save()
        cr.scale(scale, scale)
        # PDF pages are transparent; Preview shows them on white like paper.
        cr.set_source_rgb(1, 1, 1)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        page.render(cr)
        cr.restore()

    def page_text(self, index: int) -> str:
        return self._page(index).get_text() or ""

    def search(self, needle: str) -> Iterator[SearchHit]:
        for index in range(self.page_count):
            page = self._page(index)
            _, height = page.get_size()
            for rect in page.find_text(needle) or []:
                # Poppler reports find_text results with the origin at the
                # bottom-left of the page; our view works top-down.
                yield SearchHit(
                    page=index,
                    box=PageBox(
                        x=rect.x1,
                        y=height - rect.y2,
                        width=max(rect.x2 - rect.x1, 1.0),
                        height=max(rect.y2 - rect.y1, 1.0),
                    ),
                )

    def close(self) -> None:
        self._pages.clear()


class _CliBackend:
    name = "poppler-utils"
    searchable = bool(_CLI_TEXT)

    _SIZE_RE = re.compile(r"^Page\s+(\d+)\s+size:\s+([\d.]+)\s+x\s+([\d.]+)\s+pts")
    _PAGES_RE = re.compile(r"^Pages:\s+(\d+)", re.MULTILINE)

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        info = self._run([_CLI_INFO, self.path])
        match = self._PAGES_RE.search(info)
        if not match:
            raise OSError("pdfinfo could not read this file")
        self._count = int(match.group(1))
        self._sizes: dict[int, tuple[float, float]] = {}
        # A single pdfinfo pass gives us every page box up front.
        listing = self._run(
            [_CLI_INFO, "-f", "1", "-l", str(self._count), self.path], check=False
        )
        for line in listing.splitlines():
            m = self._SIZE_RE.match(line.strip())
            if m:
                self._sizes[int(m.group(1)) - 1] = (float(m.group(2)), float(m.group(3)))
        if 0 not in self._sizes:
            default = re.search(r"Page size:\s+([\d.]+)\s+x\s+([\d.]+)\s+pts", info)
            if default:
                self._sizes[0] = (float(default.group(1)), float(default.group(2)))
            else:
                self._sizes[0] = (612.0, 792.0)

    @staticmethod
    def _run(argv: list[str], check: bool = True) -> str:
        proc = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=60
        )
        if check and proc.returncode != 0:
            raise OSError(proc.stderr.strip() or f"{argv[0]} failed")
        return proc.stdout

    @property
    def page_count(self) -> int:
        return self._count

    def page_size(self, index: int) -> tuple[float, float]:
        return self._sizes.get(index, self._sizes[0])

    def _render_pixbuf(self, index: int, scale: float):
        dpi = min(max(72.0 * scale, 18.0), _MAX_CLI_DPI)
        with tempfile.TemporaryDirectory(prefix="preview-pdf-") as tmp:
            stem = os.path.join(tmp, "page")
            self._run(
                [
                    _CLI_RENDER, "-f", str(index + 1), "-l", str(index + 1),
                    "-r", f"{dpi:.2f}", "-png", "-singlefile", self.path, stem,
                ]
            )
            png = stem + ".png"
            if not os.path.exists(png):
                raise OSError("pdftoppm produced no output")
            return GdkPixbuf.Pixbuf.new_from_file(png), dpi

    def render_page(self, index: int, cr, scale: float) -> None:
        try:
            pixbuf, dpi = self._render_pixbuf(index, scale)
        except (OSError, GLib.Error):
            width, height = self.page_size(index)
            cr.save()
            cr.scale(scale, scale)
            cr.set_source_rgb(1, 1, 1)
            cr.rectangle(0, 0, width, height)
            cr.fill()
            cr.restore()
            return
        # pdftoppm already rasterised at the requested zoom, so undo any
        # residual difference between the DPI we asked for and the one we got.
        effective = (72.0 / dpi) * scale
        cr.save()
        cr.scale(effective, effective)
        cr.set_source_surface(pixbuf_to_surface(pixbuf), 0, 0)
        cr.paint()
        cr.restore()

    def page_text(self, index: int) -> str:
        if not _CLI_TEXT:
            return ""
        try:
            return self._run(
                [_CLI_TEXT, "-f", str(index + 1), "-l", str(index + 1), self.path, "-"],
                check=False,
            )
        except OSError:
            return ""

    def search(self, needle: str) -> Iterator[SearchHit]:
        folded = needle.casefold()
        for index in range(self.page_count):
            text = self.page_text(index)
            lowered = text.casefold()
            start = 0
            while True:
                at = lowered.find(folded, start)
                if at < 0:
                    break
                snippet = text[max(0, at - 40) : at + len(needle) + 40].replace("\n", " ")
                # No glyph boxes from the CLI, so the hit carries context only.
                yield SearchHit(page=index, box=None, context=snippet.strip())
                start = at + max(len(folded), 1)

    def close(self) -> None:
        pass


class PdfDocument(Document):
    kind = "PDF"

    def __init__(self, path: str) -> None:
        super().__init__(path)
        backend = pdf_backend_name()
        if backend == "poppler":
            self._impl = _PopplerBackend(path)
        elif backend == "poppler-utils":
            self._impl = _CliBackend(path)
        else:
            raise OSError(
                "No PDF support found. Install gir1.2-poppler-0.18 or poppler-utils."
            )
        self.backend_name = self._impl.name
        self.searchable = self._impl.searchable

    @property
    def page_count(self) -> int:
        return self._impl.page_count

    def page_size(self, index: int) -> tuple[float, float]:
        return self._impl.page_size(index)

    def render_page(self, index: int, cr, scale: float) -> None:
        self._impl.render_page(index, cr, scale)

    def page_text(self, index: int) -> str:
        return self._impl.page_text(index)

    def search(self, needle: str) -> Iterator[SearchHit]:
        if not needle.strip():
            return iter(())
        return self._impl.search(needle)

    def close(self) -> None:
        self._impl.close()
