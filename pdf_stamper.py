"""
pdf_stamper.py — auto-stamp an existing shipping/customs PDF with a text+date
overlay (order/MRN number, date, company name) — KOLEJKA-04 (05-CONTEXT.md D-03).

Server-side file-I/O utility only: reads the source page's real mediabox size,
draws a one-page reportlab overlay sized to match, merges it onto page 0 via
pypdf, and writes the result to a NEW file. `stamp_pdf()` never mutates
`src_path` — callers record the stamped output as a new `shipment_documents`
row via `app._save_doc_to_shipment(..., source="stamped")` so the original
upload remains an audit trail.
"""
from __future__ import annotations

import io
from datetime import date

from pypdf import PdfReader, PdfWriter

try:
    from reportlab.pdfgen import canvas
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False

# Stamp company literal (05-CONTEXT.md D-03) — no graphic stamp asset this phase.
_STAMP_COMPANY = "ACME"


def default_stamp_text(order_ref: str, mrn: str | None = None,
                       company: str = _STAMP_COMPANY,
                       when: date | None = None) -> str:
    """D-03 overlay content: order/MRN | date | company."""
    ref = (mrn or "").strip() or order_ref
    d = (when or date.today()).strftime("%d.%m.%Y")
    return f"{ref} | {d} | {company}"


def stamp_pdf(src_path: str, dest_path: str, stamp_text: str,
              x: float = 400, y: float = 20) -> None:
    """Write a stamped copy of `src_path` to `dest_path`. Never mutates `src_path`.

    The overlay is real, selectable PDF text (reportlab canvas + pypdf
    merge_page) — never a rasterized image page.
    """
    if not HAS_REPORTLAB:
        raise ImportError("reportlab nie jest zainstalowany. pip install reportlab")

    reader = PdfReader(src_path)
    page0 = reader.pages[0]
    page_w = float(page0.mediabox.width)
    page_h = float(page0.mediabox.height)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_w, page_h))
    c.setFont("Helvetica", 9)
    c.drawString(x, y, stamp_text)
    c.save()
    buf.seek(0)
    overlay = PdfReader(buf).pages[0]

    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        if i == 0:
            page.merge_page(overlay)
        writer.add_page(page)
    with open(dest_path, "wb") as f:
        writer.write(f)
