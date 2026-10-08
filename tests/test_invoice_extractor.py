import invoice_extractor as ie


class _FakeDocData:
    def __init__(self):
        self.items = [
            {"ref": "X", "desc": "Glove L", "qty": "100", "price": "2.5",
             "net": "250", "lot": "L1", "unit": "PCS"},
            {"ref": "Y1", "desc": "Mask", "qty": "10", "price": None,
             "net": "30", "lot": None, "unit": "CTN"},
        ]
        self.total_net = "280"
        self.total_qty = "110"
        self.header_fields = {"invoice_no": "FV/2026/1"}
        self.raw_text = "Invoice FV/2026/1 ..."


def test_extract_maps_parse_pdf_items(monkeypatch):
    captured = {}
    # Patchujemy szew _parse_pdf — nie importuje table_extractor/pdfplumber (CI-safe).
    def fake_parse_pdf(path, col_map=None):
        captured["path"] = path
        captured["map"] = col_map
        return _FakeDocData()
    monkeypatch.setattr(ie, "_parse_pdf", fake_parse_pdf)
    def fake_overrides(s, dt):
        captured["doc_type"] = dt
        return {"ref": 0}
    monkeypatch.setattr(ie, "get_column_overrides", fake_overrides)

    out = ie.extract_invoice("uploads/a.pdf", {"code": "S1"})
    assert captured["path"] == "uploads/a.pdf"
    assert captured["map"] == {"ref": 0}          # profil przekazany do kaskady
    assert captured["doc_type"] == "PI"           # faktura reużywa mapę kolumn PI
    assert len(out["items"]) == 2
    first = out["items"][0]
    assert first["raw_ref"] == "X"
    assert first["qty"] == "100"
    assert first["net_amount"] == "250"
    assert first["amount"] == "250"
    assert first["uom_src"] == "PCS"
    assert first["line_no"] == 1
    assert out["total_net"] == "280"
    assert out["invoice_number"] == "FV/2026/1"
