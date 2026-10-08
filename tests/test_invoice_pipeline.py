import sqlite3
import invoice_jobs as ij
import invoice_pipeline as ip

def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    import material_master as mm, uom
    mm.ensure_table(db); uom.ensure_table(db); ij.ensure_invoice_tables(db)
    return db

def test_no_profile_blocks_extraction(monkeypatch):
    db = _db()
    jid = ij.create_job(db, "b", "a.pdf", "uploads/a.pdf", "NIEZNANY")
    monkeypatch.setattr(ip, "get_supplier", lambda code, **k: None)
    status = ip.process_job(db, jid)
    assert status == "error"
    job = ij.get_job(db, jid)
    assert "Brak profilu" in job["error"]
    assert ij.get_items(db, jid) == []           # nic nie wyekstrahowano

def test_happy_path_extracts_and_maps(monkeypatch):
    db = _db()
    db.execute("INSERT INTO material_master (ref_code, ref_norm, opis_pl) VALUES ('X','X','Rękawice')")
    db.commit()
    jid = ij.create_job(db, "b", "a.pdf", "uploads/a.pdf", "S1")
    monkeypatch.setattr(ip, "get_supplier", lambda code, **k: {"code": "S1"})
    monkeypatch.setattr(ip, "extract_invoice", lambda path, sup: {
        "items": [{"line_no": 1, "raw_ref": "X", "uom_src": "PCS", "qty": "10"}],
        "total_net": "10", "total_qty": "10", "raw_text": "", "invoice_number": "FV/9"})
    status = ip.process_job(db, jid)
    assert status == "extracted"
    items = ij.get_items(db, jid)
    assert items[0]["match_status"] == "matched"
    assert items[0]["name_pl"] == "Rękawice"
    assert ij.get_job(db, jid)["invoice_number"] == "FV/9"

def test_confirm_blocked_by_ambiguous():
    db = _db()
    jid = ij.create_job(db, "b", "a.pdf", "uploads/a.pdf", "S1")
    ij.update_job(db, jid, status="extracted")  # precondition: po udanej ekstrakcji
    ij.save_items(db, jid, [{"line_no": 1, "raw_ref": "X", "match_status": "ambiguous"}])
    ok, msg = ip.confirm_job(db, jid)
    assert ok is False and "niejednoznaczne" in msg.lower()
    assert ij.get_job(db, jid)["status"] != "confirmed"

def test_confirm_allows_unmatched():
    db = _db()
    jid = ij.create_job(db, "b", "a.pdf", "uploads/a.pdf", "S1")
    ij.update_job(db, jid, status="extracted")  # precondition: po udanej ekstrakcji
    ij.save_items(db, jid, [{"line_no": 1, "raw_ref": "Q", "match_status": "unmatched"}])
    ok, msg = ip.confirm_job(db, jid)
    assert ok is True
    assert ij.get_job(db, jid)["status"] == "confirmed"

def test_confirm_refuses_errored_or_empty_job():
    # Job który nie przeszedł ekstrakcji (status=error, 0 pozycji) NIE może być zatwierdzony.
    db = _db()
    jid = ij.create_job(db, "b", "a.pdf", "uploads/a.pdf", "S1")
    ij.update_job(db, jid, status="error", error="Nie udało się odczytać tabeli pozycji")
    ok, msg = ip.confirm_job(db, jid)
    assert ok is False
    assert ij.get_job(db, jid)["status"] == "error"  # nie przeskoczył na confirmed
    # Nawet extracted, ale bez aktywnych pozycji (wszystkie pominięte) → brak zatwierdzenia
    jid2 = ij.create_job(db, "b", "c.pdf", "uploads/c.pdf", "S1")
    ij.update_job(db, jid2, status="extracted")
    ij.save_items(db, jid2, [{"line_no": 1, "raw_ref": "Q", "match_status": "matched", "skipped": 1}])
    ok2, msg2 = ip.confirm_job(db, jid2)
    assert ok2 is False and "pozycji" in msg2.lower()
