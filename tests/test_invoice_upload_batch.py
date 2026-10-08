"""tests/test_invoice_upload_batch.py — upload() batch-wiring regressions:
Fix C (thread-start race: an invoice job's thread must not fire before ALL jobs
of the batch, including packing-list jobs, are inserted) and Fix D
(forced_supplier must only apply when a file yields exactly one INVOICE/PROFORMA
part — otherwise a multi-supplier container set would get one supplier's
profile forced onto every document).

Fixtures: tests/conftest.py (admin_client). Pomija się bez Flaska.
"""
import io

import pytest

pytest.importorskip("flask")

import invoice_routes
import invoice_jobs as ij
from constants import DocKind
from db import get_db

CSRF_TOKEN = "test-csrf-token-upload"


def _set_csrf(client, token=CSRF_TOKEN):
    with client.session_transaction() as sess:
        sess["_csrf_token"] = token


def _post_upload(client, tmp_path, monkeypatch, parts_factory, supplier_code=""):
    monkeypatch.setattr(invoice_routes, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(invoice_routes.doc_splitter, "split_pdf",
                         lambda path: parts_factory(path))
    _set_csrf(client)
    data = {"pdf": (io.BytesIO(b"%PDF-1.4 fake"), "test.pdf"),
            "_csrf_token": CSRF_TOKEN}
    if supplier_code:
        data["supplier_code"] = supplier_code
    resp = client.post("/invoices/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 302, resp.get_data(as_text=True)
    batch_id = resp.headers["Location"].rsplit("batch_id=", 1)[-1]
    return batch_id


class _FakeThread:
    """Records, at construction time, how many packing-list jobs already exist
    for the job's batch. Old (buggy) code starts an invoice's thread inline —
    before later packing_list jobs in the same upload are inserted — so this
    test fails on the pre-fix code and passes once threads are deferred."""
    seen_pl_counts = []

    def __init__(self, target=None, args=(), daemon=None):
        jid = args[0]
        db2 = get_db()
        try:
            job = ij.get_job(db2, jid)
            pls = ij.list_packing_lists(db2, job["batch_id"])
        finally:
            db2.close()
        _FakeThread.seen_pl_counts.append(len(pls))

    def start(self):
        pass


def test_invoice_thread_starts_after_all_batch_jobs_inserted(admin_client, tmp_path, monkeypatch):
    _FakeThread.seen_pl_counts = []
    monkeypatch.setattr(invoice_routes.threading, "Thread", _FakeThread)

    def parts_factory(path):
        return [
            {"kind": DocKind.INVOICE, "page_from": 1, "page_to": 1, "out_path": path},
            {"kind": DocKind.PACKING_LIST, "page_from": 2, "page_to": 2, "out_path": path},
        ]

    _post_upload(admin_client, tmp_path, monkeypatch, parts_factory)
    assert _FakeThread.seen_pl_counts, "brak wątku — fake Thread nigdy nie skonstruowany"
    assert _FakeThread.seen_pl_counts[0] >= 1, (
        "wątek faktury wystartował zanim packing_list joba tej partii zapisano w DB")


def test_forced_supplier_scoped_to_single_invoice_part(admin_client, tmp_path, monkeypatch):
    monkeypatch.setattr(invoice_routes.threading, "Thread", _FakeThread)
    _FakeThread.seen_pl_counts = []

    def parts_factory(path):
        return [{"kind": DocKind.INVOICE, "page_from": 1, "page_to": 1, "out_path": path}]

    batch_id = _post_upload(admin_client, tmp_path, monkeypatch, parts_factory,
                             supplier_code="CORVAN")
    db = get_db()
    jobs = ij.list_jobs(db, batch_id)
    db.close()
    invoices = [j for j in jobs if j["doc_kind"] == DocKind.INVOICE]
    assert len(invoices) == 1
    assert invoices[0]["supplier_code"] == "CORVAN"


def test_forced_supplier_not_applied_when_multiple_invoice_parts(admin_client, tmp_path,
                                                                   monkeypatch):
    monkeypatch.setattr(invoice_routes.threading, "Thread", _FakeThread)
    _FakeThread.seen_pl_counts = []

    def parts_factory(path):
        return [
            {"kind": DocKind.INVOICE, "page_from": 1, "page_to": 1, "out_path": path},
            {"kind": DocKind.INVOICE, "page_from": 2, "page_to": 2, "out_path": path},
        ]

    batch_id = _post_upload(admin_client, tmp_path, monkeypatch, parts_factory,
                             supplier_code="CORVAN")
    db = get_db()
    jobs = ij.list_jobs(db, batch_id)
    db.close()
    invoices = [j for j in jobs if j["doc_kind"] == DocKind.INVOICE]
    assert len(invoices) == 2
    assert all(j["supplier_code"] == "" for j in invoices)
