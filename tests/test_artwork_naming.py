"""tests/test_artwork_naming.py — parser nazw masterów ACME."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from artwork_naming import parse_master_filename as P, revision_sort_key, _roman_to_int


def test_basic_ref_and_type():
    r = P("GS1710123-SS_carton_sticker.pdf")
    assert r["ref"] == "GS1710123-SS"
    assert r["packaging_type"] == "carton sticker"


def test_longest_type_wins():
    # "carton sticker" musi złapać się przed "carton"
    assert P("KDP-17-45_carton sticker.pdf")["packaging_type"] == "carton sticker"
    assert P("AT-SD-LAP3-SP_carton.pdf")["packaging_type"] == "carton"


def test_ean_and_revision():
    r = P("805016_pouch_2 Rev.00_5900010800810.pdf")
    assert r["ref"] == "805016"
    assert r["packaging_type"] == "pouch"
    assert r["ean"] == "5900010800810"
    assert r["revision"].lower().startswith("rev")
    assert r["revision_rank"] == 0


def test_revision_rank_numeric():
    assert P("GS1310081_carton_2 Rev.02.pdf")["revision_rank"] == 2
    assert P("X-1_box v3.pdf")["revision_rank"] == 3


def test_revision_roman():
    r = P("Instrukcja GAZA lux S Wyd.III.pdf")
    assert r["revision_rank"] == 3


def test_copy_counter_stripped():
    assert P("CN-14-40_pouch (2).pdf")["ref"] == "CN-14-40"


def test_no_type_still_ref():
    r = P("Poly Spike V Plus_B & G.pdf")
    assert r["ref"]                      # niepuste
    assert r["packaging_type"] == ""


def test_roman_helper():
    assert _roman_to_int("III") == 3
    assert _roman_to_int("IV") == 4
    assert _roman_to_int("IX") == 9


def test_revision_sort_picks_highest():
    a = P("REF1_box Rev.00.pdf"); b = P("REF1_box Rev.02.pdf")
    chosen = max([a, b], key=lambda p: revision_sort_key(p))
    assert chosen["revision_rank"] == 2
    # fallback po dacie gdy brak znacznika
    c = P("REF2_box.pdf"); d = P("REF2_box.pdf")
    assert revision_sort_key(c, "2026-01-01") > revision_sort_key(d, "2025-01-01")
