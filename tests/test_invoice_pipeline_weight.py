import sqlite3, invoice_jobs as ij, invoice_pipeline as ip

def _db():
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    import material_master as mm, uom
    mm.ensure_table(db); uom.ensure_table(db); ij.ensure_invoice_tables(db)
    db.execute("INSERT INTO material_master (ref_code,ref_norm,opis_pl) VALUES ('NL753','NL753','Rękawice')")
    db.commit(); return db

def test_process_job_merges_pl_weights(monkeypatch):
    db = _db()
    # PL job w tym samym batchu
    plid = ij.create_job(db, "b", "pl.pdf", "uploads/pl.pdf", "")
    ij.update_job(db, plid, status="packing_list")
    jid = ij.create_job(db, "b", "inv.pdf", "uploads/inv.pdf", "SHIELDCO")
    monkeypatch.setattr(ip, "get_supplier", lambda code, **k: {"code": "SHIELDCO"})
    monkeypatch.setattr(ip, "extract_invoice", lambda path, sup: {
        "items": [{"line_no": 1, "raw_ref": "NL753", "uom_src": "PCS", "qty": "100",
                   "weight_net": "", "weight_gross": ""}],
        "total_net": "1", "total_qty": "100", "raw_text": "", "invoice_number": "FV/1"})
    import material_master as mm
    def fake_map(path):
        if "pl.pdf" in path:
            return {mm.normalize_ref("NL753"): {"weight_net": "12.5", "weight_gross": "13.2"}}
        return {}
    monkeypatch.setattr(ip, "extract_weight_map", fake_map)
    status = ip.process_job(db, jid)
    assert status == "extracted"
    it = ij.get_items(db, jid)[0]
    assert it["weight_net"] == "12.5" and it["weight_gross"] == "13.2"
