"""Common document interface shared by every backend.

A document is a sequence of pages.  Sizes are reported in *natural units*
(CSS-like pixels for rasters, PostScript points for PDFs) and the view layer
multiplies them by a zoom factor.  Backends only have to know how to paint a
page onto a Cairo context at a given scale.
"""

from __future__ import annotations

import dataclasses
from typing import Iterator


@dataclasses.dataclass(frozen=True)
class PageBox:
    """A rectangle in unrotated page coordinates."""

    x: float
    y: float
    width: float
    height: float


@dataclasses.dataclass(frozen=True)
class SearchHit:
    page: int
    box: PageBox | None
    context: str = ""


class Document:
    """Abstract base class for everything Preview can display."""

    #: Human readable description used in the window subtitle.
    kind: str = "Document"

    #: True when pages can carry selectable text (enables Find).
    searchable: bool = False

    def __init__(self, path: str) -> None:
        self.path = path

    # -- geometry ---------------------------------------------------------
    @property
    def page_count(self) -> int:
        raise NotImplementedError

    def page_size(self, index: int) -> tuple[float, float]:
        """Natural ``(width, height)`` of a page, ignoring view rotation."""
        raise NotImplementedError

    # -- painting ---------------------------------------------------------
    def render_page(self, index: int, cr, scale: float) -> None:
        """Paint page ``index`` at ``scale`` with its top-left at the origin."""
        raise NotImplementedError

    # -- text -------------------------------------------------------------
    def page_text(self, index: int) -> str:
        return ""

    def search(self, needle: str) -> Iterator[SearchHit]:
        return iter(())

    # -- housekeeping -----------------------------------------------------
    def close(self) -> None:
        pass

    def pages(self) -> range:
        return range(self.page_count)
