"""Tests for weight column recognition in table_extractor."""
import pytest
# table_extractor ciągnie pdfplumber+rapidfuzz (ciężkie); minimalne CI ich nie ma.
pytest.importorskip("pdfplumber")
pytest.importorskip("rapidfuzz")
from table_extractor import identify_columns, _rows_to_items


def test_identify_weight_columns():
    ci = identify_columns(["REF", "Qty", "Net Weight", "Gross Weight"])
    assert ci.get("weight_net") == 2
    assert ci.get("weight_gross") == 3


def test_identify_weight_polish_abbrev():
    ci = identify_columns(["Kod", "Waga netto (kg)", "Waga brutto (kg)"])
    assert ci.get("weight_net") == 1
    assert ci.get("weight_gross") == 2


def test_weight_does_not_hijack_net_amount():
    # "Net value" to kwota (net), nie waga — nie może stać się weight_net
    ci = identify_columns(["REF", "Qty", "Net value"])
    assert ci.get("net") == 2
    assert "weight_net" not in ci


def test_rows_to_items_emits_weights():
    rows = [["Code", "Qty(pcs)", "N.W.", "G.W."],
            ["NL753", "100", "12.5", "13.2"]]
    items, _, _ = _rows_to_items(rows)
    assert items and items[0]["ref"] == "NL753"
    assert items[0]["weight_net"] == "12.5"
    assert items[0]["weight_gross"] == "13.2"


def test_existing_columns_unaffected():
    ci = identify_columns(["Code", "Qty", "Unit price (USD)", "Amount (USD)"])
    assert "ref" in ci and "qty" in ci and "price" in ci and "net" in ci
