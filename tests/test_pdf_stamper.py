"""tests/test_pdf_stamper.py — unit tests for pdf_stamper (Phase 5 Plan 01 Task 2).

Proves: page count preserved, stamp is real extractable PDF text, source file
bytes never mutated, and default_stamp_text carries order/MRN + date + company.
"""
import hashlib
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("reportlab")
pytest.importorskip("pypdf")

from pypdf import PdfReader  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

import pdf_stamper  # noqa: E402


def _make_pdf(path, pages=1):
    c = canvas.Canvas(path, pagesize=(300, 300))
    for i in range(pages):
        c.drawString(10, 10, f"page {i}")
        c.showPage()
    c.save()


def test_stamp_preserves_page_count_and_text_extractable(tmp_path):
    src = str(tmp_path / "src.pdf")
    dest = str(tmp_path / "dest.pdf")
    _make_pdf(src, pages=2)

    pdf_stamper.stamp_pdf(src, dest, "STAMP-XYZ-123")

    assert len(PdfReader(dest).pages) == len(PdfReader(src).pages)
    assert "STAMP-XYZ-123" in PdfReader(dest).pages[0].extract_text()


def test_stamp_never_mutates_source(tmp_path):
    src = str(tmp_path / "src.pdf")
    dest = str(tmp_path / "dest.pdf")
    _make_pdf(src)
    before = hashlib.sha256(open(src, "rb").read()).hexdigest()

    pdf_stamper.stamp_pdf(src, dest, "anything")

    after = hashlib.sha256(open(src, "rb").read()).hexdigest()
    assert before == after


def test_default_stamp_text_contains_ref_date_company():
    txt = pdf_stamper.default_stamp_text("PO123", "MRN456", when=date(2026, 9, 17))
    assert "MRN456" in txt          # MRN wins over order ref
    assert "17.09.2026" in txt
    assert pdf_stamper._STAMP_COMPANY in txt

    txt2 = pdf_stamper.default_stamp_text("PO123", None, when=date(2026, 9, 17))
    assert "PO123" in txt2          # falls back to order ref
