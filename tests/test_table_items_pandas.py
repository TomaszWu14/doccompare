"""Regresja #1: ekstrakcja pozycji z tabeli PDF nie może po cichu zwracać 0.

Bez pandas pdf_extractor oddaje tabele jako list[list], a _parse_document_tables
pomija wszystko bez `iloc` — porównanie pokazywało „brak pozycji” zamiast błędu.
Ten test jest czerwony w środowisku bez pandas (np. CI bez tej zależności).
"""
import pytest

pytest.importorskip("reportlab")
pytest.importorskip("pdfplumber")

from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

from enhanced_comparator import _parse_document_tables
from pdf_extractor import extract_tables

ROWS = [
    ["Item No.", "Description", "Quantity", "Unit Price", "Amount"],
    ["ACM-1001", "Steel shelf bracket 300 mm", "1200", "1.85", "2220.00"],
    ["ACM-1002", "Steel shelf bracket 400 mm", "800", "2.10", "1680.00"],
    ["ACM-2040", "Plastic cable clip, white", "5000", "0.12", "600.00"],
]


def _synthetic_pi(path):
    table = Table(ROWS)
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, (0, 0, 0))]))
    SimpleDocTemplate(str(path), pagesize=A4).build([table])


def test_line_items_extracted_from_pdf_table(tmp_path):
    pdf = tmp_path / "pi.pdf"
    _synthetic_pi(pdf)

    items, _total = _parse_document_tables(extract_tables(str(pdf)), doc_type="PI")

    refs = {i["ref"] for i in items}
    assert {"ACM-1001", "ACM-1002", "ACM-2040"} <= refs, items


def test_non_dataframe_table_is_logged_not_silent(caplog):
    tables = [{"dataframe": ROWS, "page": 1, "method": "pdfplumber"}]
    with caplog.at_level("WARNING"):
        _parse_document_tables(tables, doc_type="PI")
    assert "pandas" in caplog.text
