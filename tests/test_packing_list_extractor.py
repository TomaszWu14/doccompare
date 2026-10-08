import packing_list_extractor as pl

class _Doc:
    def __init__(self, items, raw_text=""):
        self.items = items; self.raw_text = raw_text
        self.total_net = None; self.total_qty = None; self.header_fields = {}

def test_is_packing_list_by_text():
    assert pl.is_packing_list({}, "PACKING LIST No. 5") is True
    assert pl.is_packing_list({}, "Commercial Invoice") is False

def test_extract_weight_map_per_ref(monkeypatch):
    items = [
        {"ref": "NL753-S-40", "weight_net": "12.5", "weight_gross": "13.2"},
        {"ref": "NL753-M-40", "weight_net": "20", "weight_gross": "21"},
    ]
    monkeypatch.setattr(pl, "_parse_pdf", lambda p: _Doc(items))
    m = pl.extract_weight_map("uploads/pl.pdf")
    import material_master as mm
    assert m[mm.normalize_ref("NL753-S-40")] == {"weight_net": "12.5", "weight_gross": "13.2"}
    assert mm.normalize_ref("NL753-M-40") in m

def test_extract_weight_map_sums_repeated_ref(monkeypatch):
    items = [
        {"ref": "X", "weight_net": "10", "weight_gross": "11"},
        {"ref": "X", "weight_net": "5", "weight_gross": "6"},
    ]
    monkeypatch.setattr(pl, "_parse_pdf", lambda p: _Doc(items))
    import material_master as mm
    m = pl.extract_weight_map("uploads/pl.pdf")
    assert m[mm.normalize_ref("X")]["weight_net"] == "15"
    assert m[mm.normalize_ref("X")]["weight_gross"] == "17"

def test_empty_map_when_no_weights(monkeypatch):
    items = [{"ref": "X", "weight_net": None, "weight_gross": None}]
    monkeypatch.setattr(pl, "_parse_pdf", lambda p: _Doc(items))
    assert pl.extract_weight_map("uploads/pl.pdf") == {}
