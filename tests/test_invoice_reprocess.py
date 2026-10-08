"""tests/test_invoice_reprocess.py — Fix E: manual-retry route for errored invoice
jobs. Before this route, an errored doc permanently blocked Draft SAD gating
(collect_batch requires ALL docs of a batch confirmed) with no way to retry.

Fixtures: tests/conftest.py (admin_client). Pomija się bez Flaska.
"""
import pytest

pytest.importorskip("flask")

import app as appmod
import invoice_routes
import invoice_jobs as ij
from constants import InvoiceJobStatus
from db import get_db

CSRF_TOKEN = "test-csrf-token-reprocess"


def _set_csrf(client, token=CSRF_TOKEN):
    with client.session_transaction() as sess:
        sess["_csrf_token"] = token


class _FakeThread:
    started = []

    def __init__(self, target=None, args=(), daemon=None):
        pass

    def start(self):
        _FakeThread.started.append(True)


def test_reprocess_route_registered():
    rules = {r.rule for r in appmod.app.url_map.iter_rules()}
    assert "/invoices/reprocess/<int:job_id>" in rules


def test_reprocess_requires_login():
    client = appmod.app.test_client()
    resp = client.post("/invoices/reprocess/1")
    assert resp.status_code in (302, 401)


def test_reprocess_non_error_job_returns_409(admin_client):
    db = get_db()
    jid = ij.create_job(db, "b1", "f.pdf", "uploads/f.pdf", "SUP")
    db.close()

    _set_csrf(admin_client)
    resp = admin_client.post(f"/invoices/reprocess/{jid}",
                              headers={"X-CSRF-Token": CSRF_TOKEN})
    assert resp.status_code == 409
    assert resp.get_json()["ok"] is False


def test_reprocess_missing_job_404(admin_client):
    _set_csrf(admin_client)
    resp = admin_client.post("/invoices/reprocess/999999999",
                              headers={"X-CSRF-Token": CSRF_TOKEN})
    assert resp.status_code == 404


def test_reprocess_error_job_resets_status_and_spawns_thread(admin_client, monkeypatch):
    _FakeThread.started = []
    monkeypatch.setattr(invoice_routes.threading, "Thread", _FakeThread)

    db = get_db()
    jid = ij.create_job(db, "b1", "f.pdf", "uploads/f.pdf", "SUP")
    ij.update_job(db, jid, status=InvoiceJobStatus.ERROR, error="ekstrakcja padła")
    db.close()

    _set_csrf(admin_client)
    resp = admin_client.post(f"/invoices/reprocess/{jid}",
                              headers={"X-CSRF-Token": CSRF_TOKEN})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True

    db = get_db()
    job = ij.get_job(db, jid)
    db.close()
    assert job["status"] == InvoiceJobStatus.UPLOADED
    assert job["error"] == ""
    assert _FakeThread.started == [True]
