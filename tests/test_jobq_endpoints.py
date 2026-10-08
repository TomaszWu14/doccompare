"""tests/test_jobq_endpoints.py — JOBQ-02/JOBQ-03: /api/compare and /api/table_compare
return an identical JSON response shape whether jobs.run takes the RQ-enqueue branch
(via a monkeypatched fake queue) or the no-Redis inline fallback.

No `rq`/`redis` import anywhere — the RQ branch is proven with a small synchronous fake
queue substituted via `monkeypatch.setattr(jobs, "get_queue", ...)`, mirroring the style
already established in tests/test_jobs_queue.py.

Fixtures: tests/conftest.py (admin_client, CSRF_TOKEN, set_session_csrf). Skips without Flask.
"""
import io

import pytest

pytest.importorskip("flask")

import app  # noqa: E402
import jobs  # noqa: E402

CSRF_TOKEN = "test-csrf-token-jobq"


def set_session_csrf(client, token):
    with client.session_transaction() as sess:
        sess["_csrf_token"] = token


class _FakeJob:
    """Mirrors an RQ Job just enough for jobs.run()'s result-extraction branch.

    jobs.run() does: rv = getattr(job, "return_value", None); return rv() if
    callable(rv) else job.result — so `.return_value` must NOT be callable (None
    is fine) and `.result` must carry the actual value.
    """

    def __init__(self, result):
        self.result = result
        self.return_value = None

    def get_status(self, refresh=True):
        return "finished"


class _FakeQueue:
    """Executes synchronously — proves the RQ *branch* runs without a real broker."""

    def enqueue(self, func, *args, **kwargs):
        kwargs.pop("job_timeout", None)
        return _FakeJob(func(*args, **kwargs))


def _pdf(marker: bytes) -> bytes:
    """Unique-per-call minimal PDF bytes so file_hash_a/file_hash_b differ between
    the two POSTs for the same user (avoids the duplicate-hash short-circuit)."""
    return b"%PDF-1.4\n%% " + marker + b"\n...%%EOF"


def test_api_compare_identical_shape_rq_vs_fallback(admin_client, monkeypatch):
    monkeypatch.setattr(app, "_check_and_record_api_rate", lambda uid: False)
    monkeypatch.setattr(app, "extract_pdf_text", lambda p: "PO 123 canned")

    calls = {"documents_job": 0}

    def _fake_documents_job(*a, **k):
        calls["documents_job"] += 1
        return {"summary": {"status": "ok"}, "line_items": [],
                "doc_type_detected": "PO", "po_number": ""}

    def _fake_enhanced_job(*a, **k):
        return {"items": [], "warnings": [], "headers": [], "doc_type_a": "PO"}

    monkeypatch.setattr(app, "_compare_documents_job", _fake_documents_job)
    monkeypatch.setattr(app, "_compare_enhanced_job", _fake_enhanced_job)

    set_session_csrf(admin_client, CSRF_TOKEN)

    # Fallback (no Redis) — existing default behavior.
    monkeypatch.delenv("REDIS_URL", raising=False)
    resp_fallback = admin_client.post("/api/compare", data={
        "file_a": (io.BytesIO(_pdf(b"a1")), "a1.pdf"),
        "file_b": (io.BytesIO(_pdf(b"b1")), "b1.pdf"),
        "doc_type": "PO",
        "_csrf_token": CSRF_TOKEN,
    }, content_type="multipart/form-data").get_json()

    # RQ branch — force jobs.get_queue() to return the fake synchronous queue.
    monkeypatch.setattr(jobs, "get_queue", lambda: _FakeQueue())
    resp_rq = admin_client.post("/api/compare", data={
        "file_a": (io.BytesIO(_pdf(b"a2")), "a2.pdf"),
        "file_b": (io.BytesIO(_pdf(b"b2")), "b2.pdf"),
        "doc_type": "PO",
        "_csrf_token": CSRF_TOKEN,
    }, content_type="multipart/form-data").get_json()

    assert resp_fallback is not None and resp_rq is not None
    assert "error" not in resp_fallback, resp_fallback
    assert "error" not in resp_rq, resp_rq
    assert "duplicate" not in resp_fallback
    assert "duplicate" not in resp_rq
    assert calls["documents_job"] == 2, "comparison path must actually run on both calls"
    assert set(resp_fallback.keys()) == set(resp_rq.keys())


def test_api_table_compare_identical_shape_rq_vs_fallback(admin_client, monkeypatch):
    monkeypatch.setattr(app, "_check_and_record_api_rate", lambda uid: False)

    calls = {"tables_job": 0}

    def _fake_tables_job(*a, **k):
        calls["tables_job"] += 1
        return {"table_found": True, "items": [], "doc_type_a": "CI", "doc_type_b": "PL"}

    monkeypatch.setattr(app, "_compare_tables_job", _fake_tables_job)

    set_session_csrf(admin_client, CSRF_TOKEN)

    # Fallback (no Redis).
    monkeypatch.delenv("REDIS_URL", raising=False)
    resp_fallback = admin_client.post("/api/table_compare", data={
        "file_a": (io.BytesIO(_pdf(b"t1a")), "t1a.pdf"),
        "file_b": (io.BytesIO(_pdf(b"t1b")), "t1b.pdf"),
        "_csrf_token": CSRF_TOKEN,
    }, content_type="multipart/form-data").get_json()

    # RQ branch.
    monkeypatch.setattr(jobs, "get_queue", lambda: _FakeQueue())
    resp_rq = admin_client.post("/api/table_compare", data={
        "file_a": (io.BytesIO(_pdf(b"t2a")), "t2a.pdf"),
        "file_b": (io.BytesIO(_pdf(b"t2b")), "t2b.pdf"),
        "_csrf_token": CSRF_TOKEN,
    }, content_type="multipart/form-data").get_json()

    assert resp_fallback is not None and resp_rq is not None
    assert "error" not in resp_fallback, resp_fallback
    assert "error" not in resp_rq, resp_rq
    assert calls["tables_job"] == 2, "table comparison path must actually run on both calls"
    assert set(resp_fallback.keys()) == set(resp_rq.keys())
