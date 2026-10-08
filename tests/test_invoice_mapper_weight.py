"""Tests for weight map enrichment in invoice_mapper."""

import sqlite3
import material_master as mm
import uom
import invoice_mapper as im


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    mm.ensure_table(db)
    uom.ensure_table(db)
    return db


def test_weight_filled_from_map_exact():
    """Weight map with exact normalized REF fills empty item weights."""
    db = _db()
    wm = {mm.normalize_ref("NL753-S-40"): {"weight_net": "12.5", "weight_gross": "13.2"}}
    out = im.map_items(
        db,
        [{"line_no": 1, "raw_ref": "NL753-S-40", "weight_net": "", "weight_gross": ""}],
        weight_map=wm
    )
    r = out[0]
    assert r["weight_net"] == "12.5" and r["weight_gross"] == "13.2"
    assert r["weight_source"] == "pl"


def test_weight_alias_suffix():
    """Weight map with digit-suffix alias (X1 matches X) fills weights."""
    db = _db()
    wm = {mm.normalize_ref("X1"): {"weight_net": "9", "weight_gross": "10"}}
    out = im.map_items(
        db,
        [{"line_no": 1, "raw_ref": "X1", "weight_net": "", "weight_gross": ""}],
        weight_map=wm
    )
    assert out[0]["weight_net"] == "9"
    assert out[0]["weight_source"] == "pl"


def test_weight_missing_flagged():
    """When weight map is empty, weight_source is set to 'brak'."""
    db = _db()
    out = im.map_items(
        db,
        [{"line_no": 1, "raw_ref": "ZZZ", "weight_net": "", "weight_gross": ""}],
        weight_map={}
    )
    assert out[0]["weight_net"] == "" and out[0]["weight_source"] == "brak"


def test_no_weight_map_backwards_compatible():
    """When weight_map is not provided, defaults to None and sets weight_source='brak'."""
    db = _db()
    out = im.map_items(
        db,
        [{"line_no": 1, "raw_ref": "X", "weight_net": "", "weight_gross": ""}]
    )
    assert out[0]["weight_source"] == "brak"


def test_multi_lot_same_ref_not_double_counted():
    """Bug: PL stores a per-REF TOTAL. An invoice with two lines for the same REF
    (different LOTs) must NOT get that total copied into both — only the first
    line is filled; the second stays empty with weight_source='brak', so
    downstream SAD aggregation does not sum the PL total N times."""
    db = _db()
    wm = {"X1": {"weight_net": "15", "weight_gross": "18", "cartons": "30"}}
    out = im.map_items(
        db,
        [
            {"line_no": 1, "raw_ref": "X1", "weight_net": "", "weight_gross": "", "cartons": ""},
            {"line_no": 2, "raw_ref": "X1", "weight_net": "", "weight_gross": "", "cartons": ""},
        ],
        weight_map=wm,
    )
    first, second = out
    assert (first["weight_net"], first["weight_gross"], first["cartons"]) == ("15", "18", "30")
    assert first["weight_source"] == "pl"
    assert (second["weight_net"], second["weight_gross"], second["cartons"]) == ("", "", "")
    assert second["weight_source"] == "brak"


def test_single_line_behavior_unchanged():
    """Single-line-per-REF behavior stays byte-identical to before the multi-LOT fix."""
    db = _db()
    wm = {mm.normalize_ref("NL753-S-40"): {"weight_net": "12.5", "weight_gross": "13.2",
                                            "cartons": "3"}}
    out = im.map_items(
        db,
        [{"line_no": 1, "raw_ref": "NL753-S-40", "weight_net": "", "weight_gross": "",
          "cartons": ""}],
        weight_map=wm,
    )
    r = out[0]
    assert (r["weight_net"], r["weight_gross"], r["cartons"]) == ("12.5", "13.2", "3")
    assert r["weight_source"] == "pl"
