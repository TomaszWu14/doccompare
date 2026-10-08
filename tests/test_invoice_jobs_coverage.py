"""list_invoice_jobs pomija joby packing-list (nie zaśmiecają dashboardu coverage)."""
import sqlite3
import invoice_jobs as ij


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    ij.ensure_invoice_tables(db)
    return db


def test_list_invoice_jobs_excludes_packing_lists():
    db = _db()
    inv = ij.create_job(db, "b", "inv.pdf", "uploads/inv.pdf", "S1")
    pl = ij.create_job(db, "b", "pl.pdf", "uploads/pl.pdf", "")
    ij.update_job(db, pl, status="packing_list")
    ids = [j["id"] for j in ij.list_invoice_jobs(db, "b")]
    assert inv in ids
    assert pl not in ids
    # list_jobs (surowe) nadal zwraca oba — helper filtruje tylko widok coverage
    assert len(ij.list_jobs(db, "b")) == 2
