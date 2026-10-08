"""tests/test_jobq_auto_compare.py — JOBQ-01: auto-porównanie CI/PL musi jechać przez
jobs.enqueue (nie przez surowy threading.Thread), a martwy _try_auto_compare_pi ma
zniknąć z modułu. Lekkie, bez zależności od redis/rq — wszystko monkeypatchowane.
"""
import os
import tempfile

import pytest

pytest.importorskip("flask")

_TMP = tempfile.mkdtemp(prefix="doccompare_jobq_")
os.environ.setdefault("SECRET_KEY", "test-jobq-secret")
os.environ["SQLITE_PATH"] = os.path.join(_TMP, "jobq.db")
os.environ.pop("DATABASE_URL", None)

import app as _app_mod  # noqa: E402


def _seed_user(db, username="jobq_user"):
    row = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if row:
        return row["id"]
    pwhash = _app_mod.generate_password_hash("jobq-pass-123")
    cur = db.execute(
        "INSERT INTO users(username, password_hash, role) VALUES (?,?,?)",
        (username, pwhash, "admin"))
    db.commit()
    return cur.lastrowid


def _seed_doc(db, po_number, doc_type, filename):
    db.execute(
        "INSERT INTO shipment_documents (po_number, filename, original_name, doc_type) "
        "VALUES (?,?,?,?)",
        (po_number, filename, filename, doc_type))
    db.commit()


def test_try_auto_compare_pair_dispatches_via_enqueue(monkeypatch):
    db = _app_mod.get_db()
    try:
        uid = _seed_user(db)
        _seed_doc(db, "JOBQ01PO", "CI", "ci.pdf")
        _seed_doc(db, "JOBQ01PO", "PL", "pl.pdf")
    finally:
        db.close()

    calls = []

    def _fake_enqueue(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return None

    import jobs
    monkeypatch.setattr(jobs, "enqueue", _fake_enqueue)

    _app_mod._try_auto_compare_pair("JOBQ01PO", "CI", "PL", "CI_PL", uid)

    assert len(calls) == 1
    func, args, kwargs = calls[0]
    assert func is _app_mod._perform_auto_compare_pair
    assert args == ("JOBQ01PO", "CI", "PL", "CI_PL", uid)


def test_perform_auto_compare_pair_runs_without_request_context(monkeypatch):
    # No Flask request/test-request context active here — proves the worker half
    # is headless (safe to run on an RQ worker process with no Flask context).
    # po_number has no seeded shipment_documents rows, so the function must
    # short-circuit before ever reaching the comparison engine. A blanket
    # try/except inside the worker would also return None, so additionally
    # assert the engine was never invoked (WR-01 from 02-REVIEW.md).
    # Patch the REAL symbol the worker resolves at call time: it does a local
    # `from enhanced_comparator import compare_enhanced` inside its body, so the
    # patch must land on enhanced_comparator, not on the app module (02-VERIFICATION).
    import enhanced_comparator
    engine_calls = []
    monkeypatch.setattr(
        enhanced_comparator, "compare_enhanced",
        lambda *a, **k: engine_calls.append(a) or {})
    result = _app_mod._perform_auto_compare_pair("JOBQ01_NODOCS", "CI", "PL", "CI_PL", 1)
    assert result is None
    assert engine_calls == []


def test_try_auto_compare_pi_deleted():
    assert not hasattr(_app_mod, "_try_auto_compare_pi")
