"""tests/test_po_parsing.py — czyszczenie REF z pozycji PO (REF + sklejony opis)."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import material_master as mm
from po_parsing import clean_po_ref


def _db(materials=None):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    mm.ensure_table(db)
    for ref in (materials or []):
        mm.upsert_materials(db, [{"ref_code": ref}])
    return db


# ── Ścieżka master daty (najdłuższy znany REF jako prefiks) ──────────────────
def test_master_match_strips_description():
    db = _db(["RNBS10001"])
    ref, opis = clean_po_ref(db, "RNBS10001 easyCARE nitrile gloves S")
    assert ref == "RNBS10001"
    # gdy opis nie podany — całość trafia do opisu (zachowujemy oryginał)
    assert "easyCARE" in opis


def test_master_match_with_separators_in_ref():
    # master ma myślniki, PDF skleił bez nich — normalizacja je zrównuje
    db = _db(["NL753-S-40"])
    ref, _ = clean_po_ref(db, "NL753S40 cewnik Foleya")
    assert ref == "NL753-S-40"


def test_master_match_prefers_longest_ref():
    db = _db(["RN", "RNBS10001"])
    ref, _ = clean_po_ref(db, "RNBS10001 opis")
    assert ref == "RNBS10001"


def test_master_match_keeps_explicit_opis():
    db = _db(["RNBS10001"])
    ref, opis = clean_po_ref(db, "RNBS10001 sklejony", opis="właściwy opis")
    assert ref == "RNBS10001"
    assert opis == "właściwy opis"


# ── Fallback regex (brak w master dacie) ─────────────────────────────────────
def test_fallback_first_token_with_digit():
    db = _db([])
    ref, opis = clean_po_ref(db, "ABC123 jakiś opis produktu")
    assert ref == "ABC123"
    assert opis == "jakiś opis produktu"


def test_fallback_pure_token_no_description_unchanged():
    db = _db([])
    # pojedynczy token z cyfrą, bez dalszego opisu → zostaje jak jest
    ref, opis = clean_po_ref(db, "ABC123")
    assert ref == "ABC123"
    assert opis == ""


def test_fallback_token_without_digit_kept_whole():
    db = _db([])
    # pierwszy token bez cyfry → nie traktujemy jako REF+opis, zwracamy całość
    ref, _ = clean_po_ref(db, "OPIS bez kodu")
    assert ref == "OPIS bez kodu"[:50]


def test_empty_input():
    db = _db([])
    assert clean_po_ref(db, "") == ("", "")
    assert clean_po_ref(db, None) == ("", "")
    assert clean_po_ref(db, "   ") == ("", "")


def test_ref_truncated_to_50_chars():
    db = _db([])
    long_tok = "X" + "9" * 80
    ref, _ = clean_po_ref(db, long_tok + " opis")
    assert len(ref) == 50


def test_whitespace_stripped():
    db = _db(["RNBS10001"])
    ref, _ = clean_po_ref(db, "  RNBS10001 opis  ")
    assert ref == "RNBS10001"
