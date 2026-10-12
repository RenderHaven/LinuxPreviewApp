"""Opening files and deciding which backend handles them."""

from __future__ import annotations

import os

from .backends import Document, ImageDocument, PdfDocument, pdf_backend_name
from .backends.image import supported_extensions, supported_mime_types

PDF_EXTENSIONS = {".pdf"}


class UnsupportedFile(Exception):
    pass


def is_supported(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext in PDF_EXTENSIONS:
        return pdf_backend_name() is not None
    return ext in supported_extensions()


def open_document(path: str) -> Document:
    """Return a :class:`Document` for ``path``.

    Dispatch is by extension first because it is cheap and correct for the
    formats we care about; anything unknown still gets handed to GdkPixbuf,
    which sniffs the real content and fails loudly if it cannot cope.
    """
    if not os.path.exists(path):
        raise UnsupportedFile(f"{os.path.basename(path)} does not exist")
    if os.path.isdir(path):
        raise UnsupportedFile(f"{os.path.basename(path)} is a folder")

    ext = os.path.splitext(path)[1].lower()
    if ext in PDF_EXTENSIONS:
        return PdfDocument(path)

    try:
        return ImageDocument(path)
    except OSError as exc:
        raise UnsupportedFile(str(exc)) from exc


def file_filters() -> list[tuple[str, list[str], list[str]]]:
    """``(label, mime_types, glob_patterns)`` tuples for the open dialog."""
    image_globs = sorted("*" + e for e in supported_extensions())
    image_mimes = sorted(supported_mime_types())
    everything = list(image_globs)
    filters: list[tuple[str, list[str], list[str]]] = []
    if pdf_backend_name():
        everything = ["*.pdf"] + everything
    filters.append(("All Supported Files", [], everything))
    if pdf_backend_name():
        filters.append(("PDF Documents", ["application/pdf"], ["*.pdf"]))
    filters.append(("Images", image_mimes, image_globs))
    return filters
