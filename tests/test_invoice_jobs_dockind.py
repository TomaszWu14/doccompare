import sqlite3
import invoice_jobs as ij
from constants import DocKind, InvoiceJobStatus


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    ij.ensure_invoice_tables(db)
    return db


def test_create_job_defaults_and_dockind():
    db = _db()
    jid = ij.create_job(db, "b", "a.pdf", "uploads/a.pdf", "S1")
    job = ij.get_job(db, jid)
    assert job["doc_kind"] == "invoice"
    assert job["source_file"] == ""
    jid2 = ij.create_job(db, "b", "a.pdf", "uploads/a_doc2.pdf", "",
                         doc_kind=DocKind.PROFORMA, source_file="a.pdf",
                         page_from=3, page_to=3)
    j2 = ij.get_job(db, jid2)
    assert j2["doc_kind"] == "proforma"
    assert j2["page_from"] == 3


def test_update_job_new_fields():
    db = _db()
    jid = ij.create_job(db, "b", "a.pdf", "p.pdf", "S1")
    ij.update_job(db, jid, container_no="ABCU1234567", delivery_terms="FOB SHANGHAI")
    job = ij.get_job(db, jid)
    assert job["container_no"] == "ABCU1234567"
    assert job["delivery_terms"] == "FOB SHANGHAI"


def test_items_cartons_roundtrip():
    db = _db()
    jid = ij.create_job(db, "b", "a.pdf", "p.pdf", "S1")
    ij.save_items(db, jid, [{"line_no": 1, "raw_ref": "X", "cartons": "25"}])
    assert ij.get_items(db, jid)[0]["cartons"] == "25"


def test_list_packing_lists_by_dockind():
    db = _db()
    plid = ij.create_job(db, "b", "pl.pdf", "p.pdf", "", doc_kind=DocKind.PACKING_LIST)
    ij.update_job(db, plid, status=InvoiceJobStatus.PACKING_LIST)
    ij.create_job(db, "b", "ci.pdf", "c.pdf", "S1")
    pls = ij.list_packing_lists(db, "b")
    assert len(pls) == 1 and pls[0]["id"] == plid
    assert len(ij.list_invoice_jobs(db, "b")) == 1
