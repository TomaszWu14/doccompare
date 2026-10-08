"""tests/test_customs_checklist.py — czysta bramka celna (KOLEJKA-03).

missing_hard_required(rows): tylko dokładny status 'zatwierdzono' zdejmuje
wymagany doc_type z listy braków; 'wgrano'/'oczekuje'/brak wiersza = brak.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import customs_checklist as cc


def _rows(**statuses):
    return [{"doc_type": t, "status": s} for t, s in statuses.items()]


def test_all_approved_returns_empty():
    rows = _rows(**{t: "zatwierdzono" for t in cc.CUSTOMS_DOC_TYPES})
    assert cc.missing_hard_required(rows) == []


def test_wgrano_still_counts_as_missing():
    statuses = {t: "zatwierdzono" for t in cc.CUSTOMS_DOC_TYPES}
    statuses["CI"] = "wgrano"
    assert cc.missing_hard_required(_rows(**statuses)) == ["CI"]


def test_oczekuje_still_counts_as_missing():
    statuses = {t: "zatwierdzono" for t in cc.CUSTOMS_DOC_TYPES}
    statuses["SAD"] = "oczekuje"
    assert cc.missing_hard_required(_rows(**statuses)) == ["SAD"]


def test_absent_row_counts_as_missing():
    statuses = {t: "zatwierdzono" for t in cc.CUSTOMS_DOC_TYPES if t != "BL"}
    assert cc.missing_hard_required(_rows(**statuses)) == ["BL"]


def test_empty_rows_returns_all_required():
    assert cc.missing_hard_required([]) == list(cc.CUSTOMS_DOC_TYPES)
    assert cc.missing_hard_required(None) == list(cc.CUSTOMS_DOC_TYPES)


def test_irrelevant_doc_types_ignored():
    rows = _rows(**{t: "zatwierdzono" for t in cc.CUSTOMS_DOC_TYPES})
    rows.append({"doc_type": "PO", "status": "brak"})
    assert cc.missing_hard_required(rows) == []
