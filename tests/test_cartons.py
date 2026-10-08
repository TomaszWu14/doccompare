# tests/test_cartons.py
import packing_list_extractor as ple
import invoice_mapper as im


class _Doc:
    def __init__(self, items):
        self.items = items


def test_weight_map_includes_cartons(monkeypatch):
    monkeypatch.setattr(ple, "_parse_pdf", lambda p: _Doc([
        {"ref": "X1", "weight_net": "10", "weight_gross": "12", "cartons": "25"},
        {"ref": "X1", "weight_net": "5", "weight_gross": "6", "cartons": "5"},
        {"ref": "Y", "cartons": "7"},          # PL bez wag, same kartony
    ]))
    wm = ple.extract_weight_map("pl.pdf")
    assert wm["X1"]["cartons"] == "30"
    assert wm["X1"]["weight_net"] == "15"
    assert wm["Y"]["cartons"] == "7"


def test_fill_weight_fills_cartons_when_empty():
    entry = {"X1": {"weight_net": "15", "weight_gross": "18", "cartons": "30"}}
    item = {"raw_ref": "X1", "weight_net": "", "weight_gross": "", "cartons": ""}
    im._fill_weight(item, entry)
    assert item["cartons"] == "30"
    item2 = {"raw_ref": "X1", "weight_net": "", "weight_gross": "", "cartons": "9"}
    im._fill_weight(item2, entry)
    assert item2["cartons"] == "9"             # wartość z faktury wygrywa


def test_fill_weight_second_lot_line_not_double_counted():
    """Multi-LOT invoice: two lines share raw_ref X1. weight_map stores the PL's
    per-REF TOTAL — only the first line may draw from it, else SAD aggregation
    would sum the total twice."""
    weight_map = {"X1": {"weight_net": "15", "weight_gross": "18", "cartons": "30"}}
    filled_refs = set()
    item1 = {"raw_ref": "X1", "weight_net": "", "weight_gross": "", "cartons": ""}
    item2 = {"raw_ref": "X1", "weight_net": "", "weight_gross": "", "cartons": ""}
    im._fill_weight(item1, weight_map, filled_refs)
    im._fill_weight(item2, weight_map, filled_refs)
    assert (item1["weight_net"], item1["weight_gross"], item1["cartons"]) == ("15", "18", "30")
    assert item1["weight_source"] == "pl"
    assert (item2["weight_net"], item2["weight_gross"], item2["cartons"]) == ("", "", "")
    assert item2["weight_source"] == "brak"
