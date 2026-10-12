"""Printing, via the GTK print dialog."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # noqa: E402


def cairo_bridge_available() -> bool:
    """Whether GI can hand us a Cairo context.

    Printing is the one path that cannot avoid it: GTK gives the page context
    to us rather than the other way round.  The viewer itself renders through
    GSK render nodes and does not care.
    """
    try:
        gi.require_foreign("cairo")
        return True
    except Exception:
        return False


def print_document(document, parent, rotation: int = 0, on_error=None) -> None:
    """Run the print dialog for ``document``, scaling each page to the sheet."""
    if not cairo_bridge_available():
        if on_error:
            on_error(
                "Printing needs the python3-gi-cairo package. "
                "Install it, or use Export As instead."
            )
        return
    operation = Gtk.PrintOperation()
    operation.set_n_pages(document.page_count)
    operation.set_job_name("Preview")
    operation.set_embed_page_setup(True)
    operation.set_use_full_page(False)
    operation.set_unit(Gtk.Unit.POINTS)

    def on_draw_page(_op, context, page_number):
        cr = context.get_cairo_context()
        sheet_w = context.get_width()
        sheet_h = context.get_height()
        page_w, page_h = document.page_size(page_number)
        if rotation in (90, 270):
            page_w, page_h = page_h, page_w
        if page_w <= 0 or page_h <= 0:
            return
        # Fit to the sheet without distorting, then centre it.
        scale = min(sheet_w / page_w, sheet_h / page_h)
        cr.save()
        cr.translate(
            (sheet_w - page_w * scale) / 2.0,
            (sheet_h - page_h * scale) / 2.0,
        )
        if rotation == 90:
            cr.translate(page_w * scale, 0)
            cr.rotate(1.5707963267948966)
        elif rotation == 180:
            cr.translate(page_w * scale, page_h * scale)
            cr.rotate(3.141592653589793)
        elif rotation == 270:
            cr.translate(0, page_h * scale)
            cr.rotate(4.712388980384690)
        document.render_page(page_number, cr, scale)
        cr.restore()

    def on_done(_op, result):
        if result == Gtk.PrintOperationResult.ERROR and on_error:
            on_error("The print job could not be completed.")

    operation.connect("draw-page", on_draw_page)
    operation.connect("done", on_done)
    try:
        operation.run(Gtk.PrintOperationAction.PRINT_DIALOG, parent)
    except Exception as exc:  # pragma: no cover - printer backends vary
        if on_error:
            on_error(str(exc))
