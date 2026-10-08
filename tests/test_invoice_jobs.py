import sqlite3
import invoice_jobs as ij

def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    return db

def test_create_and_get_job():
    db = _db()
    ij.ensure_invoice_tables(db)
    jid = ij.create_job(db, "batch1", "faktura.pdf", "uploads/faktura.pdf", "SHIELDCO")
    job = ij.get_job(db, jid)
    assert job["status"] == "uploaded"
    assert job["supplier_code"] == "SHIELDCO"
    assert job["batch_id"] == "batch1"

def test_update_status_and_list_by_batch():
    db = _db()
    ij.ensure_invoice_tables(db)
    j1 = ij.create_job(db, "b", "a.pdf", "uploads/a.pdf", "S1")
    ij.create_job(db, "b", "c.pdf", "uploads/c.pdf", "S2")
    ij.update_job(db, j1, status="extracted", invoice_number="FV/1")
    jobs = ij.list_jobs(db, "b")
    assert len(jobs) == 2
    assert ij.get_job(db, j1)["status"] == "extracted"
    assert ij.get_job(db, j1)["invoice_number"] == "FV/1"

def test_save_and_get_items_replaces():
    db = _db()
    ij.ensure_invoice_tables(db)
    jid = ij.create_job(db, "b", "a.pdf", "uploads/a.pdf", "S1")
    ij.save_items(db, jid, [{"line_no": 1, "raw_ref": "X", "qty": "10"}])
    ij.save_items(db, jid, [{"line_no": 1, "raw_ref": "Y", "qty": "5"},
                            {"line_no": 2, "raw_ref": "Z", "qty": "3"}])
    items = ij.get_items(db, jid)
    assert len(items) == 2
    assert items[0]["raw_ref"] == "Y"

def test_save_items_coerces_bool_to_int():
    db = _db()
    ij.ensure_invoice_tables(db)
    jid = ij.create_job(db, "b", "a.pdf", "uploads/a.pdf", "S1")
    ij.save_items(db, jid, [{"line_no": 1, "raw_ref": "X", "skipped": True, "sent": True}])
    item = ij.get_items(db, jid)[0]
    assert item["skipped"] == 1 and isinstance(item["skipped"], int)
    assert item["sent"] == 1 and isinstance(item["sent"], int)
