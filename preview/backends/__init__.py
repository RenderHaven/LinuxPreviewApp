"""Document backends."""

from .base import Document, PageBox, SearchHit
from .image import ImageDocument
from .pdf import PdfDocument, pdf_backend_name

__all__ = [
    "Document",
    "PageBox",
    "SearchHit",
    "ImageDocument",
    "PdfDocument",
    "pdf_backend_name",
]
