"""tests/test_suppliers_blueprint_validation.py — REFACTOR-03 coverage for
blueprints/suppliers.py's Pydantic @validate_body routes (Phase 3, plan 03-02).

Covers the two routes 03-02-PLAN.md calls out explicitly:
  - PATCH /api/sup-master/<code>/contact (SupplierContactUpdate)
  - POST /api/suppliers (SupplierCreate)
Both: bad body -> 400 {"error": ...} + no DB write; good body -> unchanged
success response shape.
"""
import pytest

pytest.importorskip("flask")

from tests.conftest import CSRF_TOKEN, set_session_csrf  # noqa: E402


def _csrf_headers(client):
    set_session_csrf(client, CSRF_TOKEN)
    return {"X-CSRF-Token": CSRF_TOKEN}


# ── PATCH /api/sup-master/<code>/contact ───────────────────────────────────────

def _seed_supplier(app_mod, code="SUPTEST01", email="orig@example.com"):
    import supplier_master as _sm
    db = app_mod.get_db()
    try:
        _sm.ensure_columns(db)  # create the email/contact_person/phone columns up front
        db.execute("DELETE FROM suppliers WHERE code=?", (code,))
        db.execute(
            "INSERT INTO suppliers(code, name, email) VALUES (?,?,?)",
            (code, "Test Supplier", email),
        )
        db.commit()
    finally:
        db.close()


def test_contact_update_invalid_email_rejected_no_db_write(manager_client):
    import app as _app_mod

    _seed_supplier(_app_mod, code="SUPTEST01", email="orig@example.com")
    headers = _csrf_headers(manager_client)
    resp = manager_client.patch(
        "/api/sup-master/SUPTEST01/contact",
        json={"email": "not-an-email"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()

    db = _app_mod.get_db()
    try:
        row = db.execute("SELECT email FROM suppliers WHERE code=?", ("SUPTEST01",)).fetchone()
    finally:
        db.close()
    assert row["email"] == "orig@example.com"  # untouched


def test_contact_update_valid_body_updates_email(manager_client):
    import app as _app_mod

    _seed_supplier(_app_mod, code="SUPTEST02", email="orig2@example.com")
    headers = _csrf_headers(manager_client)
    resp = manager_client.patch(
        "/api/sup-master/SUPTEST02/contact",
        json={"email": "new@example.com", "contact_person": "Jan Kowalski"},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body.get("ok") is True

    db = _app_mod.get_db()
    try:
        row = db.execute(
            "SELECT email, contact_person FROM suppliers WHERE code=?", ("SUPTEST02",)
        ).fetchone()
    finally:
        db.close()
    assert row["email"] == "new@example.com"
    assert row["contact_person"] == "Jan Kowalski"


# ── POST /api/suppliers ─────────────────────────────────────────────────────────

def test_suppliers_add_missing_name_rejected_no_db_write(admin_client):
    import app as _app_mod

    db = _app_mod.get_db()
    try:
        db.execute("DELETE FROM suppliers WHERE code=?", ("NEWSUP01",))
        db.commit()
    finally:
        db.close()

    headers = _csrf_headers(admin_client)
    resp = admin_client.post(
        "/api/suppliers",
        json={"code": "NEWSUP01"},  # missing required 'name'
        headers=headers,
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()

    db = _app_mod.get_db()
    try:
        row = db.execute("SELECT id FROM suppliers WHERE code=?", ("NEWSUP01",)).fetchone()
    finally:
        db.close()
    assert row is None  # never inserted


def test_suppliers_add_valid_body_creates_supplier(admin_client):
    import app as _app_mod
    import migrate_db as _migrate_db

    db = _app_mod.get_db()
    try:
        # Pre-existing, unrelated gap (app.py's inline migration list runs the
        # `column_mapping_json` ALTER before the suppliers CREATE TABLE — the
        # ALTER silently no-ops on a brand-new DB): backfill it here rather
        # than touching app.py's migration list, which is out of this plan's
        # scope. `migrate_db.add_column` is the same idempotent helper
        # `python migrate_db.py` uses in production.
        _migrate_db.add_column(db, "suppliers", "column_mapping_json", "TEXT DEFAULT '{}'")
        db.execute("DELETE FROM suppliers WHERE code=?", ("NEWSUP02",))
        db.commit()
    finally:
        db.close()

    headers = _csrf_headers(admin_client)
    resp = admin_client.post(
        "/api/suppliers",
        json={"code": "NEWSUP02", "name": "New Supplier"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.data
    body = resp.get_json()
    assert body.get("ok") is True
    assert body["supplier"]["code"] == "NEWSUP02"

    db = _app_mod.get_db()
    try:
        row = db.execute("SELECT name FROM suppliers WHERE code=?", ("NEWSUP02",)).fetchone()
    finally:
        db.close()
    assert row["name"] == "New Supplier"


# ── PATCH /api/suppliers/<sid> — 404 vs 400 priority (WR-01) ────────────────────

def test_update_nonexistent_supplier_returns_404_not_400(admin_client):
    """WR-01 regression: a nonexistent id + empty body must return 404 (row
    lookup), not 400 (body validation) — restores pre-refactor priority."""
    headers = _csrf_headers(admin_client)
    resp = admin_client.patch(
        "/api/suppliers/9999999",
        json={},
        headers=headers,
    )
    assert resp.status_code == 404
    assert "error" in resp.get_json()


# ── Multipart routes stay untouched (no @validate_body) ─────────────────────────

def test_preview_table_and_sup_master_import_unaffected():
    """Both multipart routes still read request.files, not a Pydantic body —
    smoke-check via source inspection (out of REFACTOR-03's Pydantic scope)."""
    import inspect

    import blueprints.suppliers as _suppliers_mod

    src = inspect.getsource(_suppliers_mod.api_suppliers_preview_table)
    assert "request.files" in src
    src2 = inspect.getsource(_suppliers_mod.api_sup_master_import)
    assert "request.files" in src2
