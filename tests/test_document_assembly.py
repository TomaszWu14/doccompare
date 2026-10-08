"""tests/test_document_assembly.py — unit tests for KOLEJKA-05 (Phase 5 Plan 04):
document_assembly.assemble_pdf — merge latest-per-doc_type PDFs into one combined PDF.

Covers:
  - combined PDF page count == sum of input PDFs' page counts,
  - empty input → ValueError (never bytes / partial output),
  - an unreadable path among valid ones is skipped, not fatal,
  - all paths unreadable → same empty-input signal (no partial artifact).
"""
import io
import os

import pytest

pytest.importorskip("pypdf")

from pypdf import PdfReader, PdfWriter  # noqa: E402

from document_assembly import assemble_pdf  # noqa: E402


def _make_pdf(path, pages):
    """Throwaway multi-page PDF (blank pages — page count is all that matters)."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=300, height=300)
    with open(path, "wb") as f:
        writer.write(f)


def test_combined_page_count_equals_sum_of_inputs(tmp_path):
    p1 = os.path.join(str(tmp_path), "a.pdf")
    p2 = os.path.join(str(tmp_path), "b.pdf")
    _make_pdf(p1, 2)
    _make_pdf(p2, 3)

    out = assemble_pdf([p1, p2])
    assert isinstance(out, bytes) and out.startswith(b"%PDF")
    assert len(PdfReader(io.BytesIO(out)).pages) == 5


def test_empty_input_raises_and_yields_no_bytes():
    with pytest.raises(ValueError):
        assemble_pdf([])


def test_unreadable_path_is_skipped_when_valid_pdfs_remain(tmp_path):
    good = os.path.join(str(tmp_path), "good.pdf")
    _make_pdf(good, 2)
    not_pdf = os.path.join(str(tmp_path), "not_a.pdf")
    with open(not_pdf, "wb") as f:
        f.write(b"definitely not a pdf")
    missing = os.path.join(str(tmp_path), "missing.pdf")

    out = assemble_pdf([not_pdf, missing, good])
    assert len(PdfReader(io.BytesIO(out)).pages) == 2


def test_all_paths_unreadable_raises(tmp_path):
    not_pdf = os.path.join(str(tmp_path), "junk.pdf")
    with open(not_pdf, "wb") as f:
        f.write(b"junk")

    with pytest.raises(ValueError):
        assemble_pdf([not_pdf])
