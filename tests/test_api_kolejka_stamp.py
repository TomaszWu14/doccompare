"""tests/test_api_kolejka_stamp.py — end-to-end proof for KOLEJKA-04's PDF
auto-stamp route (POST /api/dostawy/<nr>/stamp), Phase 5 Plan 01 tracer.

Covers:
  - 200 + a NEW shipment_documents row (source='stamped') + a NEW file on disk
    for a manager stamping an existing (nr, doc_type) document.
  - 400 when no document of the requested doc_type exists for the order.
"""
import os

import pytest

pytest.importorskip("flask")
pytest.importorskip("reportlab")
pytest.importorskip("pypdf")

from tests.conftest import CSRF_TOKEN, set_session_csrf  # noqa: E402

_NR = "KOLSTAMP01"


def _csrf_headers(client):
    set_session_csrf(client, CSRF_TOKEN)
    return {"X-CSRF-Token": CSRF_TOKEN}


def _make_source_pdf(path):
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(path, pagesize=(300, 300))
    c.drawString(10, 10, "source document")
    c.save()


def _ensure_kolejka_zlecenia_table(db):
    """kolejka_zlecenia is created by `python migrate_db.py` (a separate step
    from app.py's own init_db/_db_run_migrations, see AGENTS.md/CLAUDE.md
    Common Commands) — not present in the isolated temp DB conftest.py builds
    for integration tests. Same pattern as
    test_suppliers_blueprint_validation.py's migrate_db.add_column backfill:
    create the minimal subset of columns this route needs, out of app.py's
    migration-list scope. Idempotent — no-ops against a real full schema."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS kolejka_zlecenia (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nr_zamowienia TEXT NOT NULL,
            supplier_name TEXT DEFAULT '',
            status TEXT DEFAULT 'utworzone',
            mir7_number TEXT DEFAULT '',
            department TEXT DEFAULT '',
            UNIQUE(nr_zamowienia)
        )
    """)
    db.commit()


def _seed_order(app_mod):
    db = app_mod.get_db()
    try:
        _ensure_kolejka_zlecenia_table(db)
        db.execute("DELETE FROM shipment_documents WHERE po_number=?", (_NR,))
        db.execute("DELETE FROM kolejka_zlecenia WHERE nr_zamowienia=?", (_NR,))
        db.execute(
            "INSERT INTO kolejka_zlecenia(nr_zamowienia, supplier_name, status) "
            "VALUES (?, ?, 'utworzone')",
            (_NR, "Test Supplier"),
        )
        db.commit()
    finally:
        db.close()


def _seed_doc(app_mod, tmp_path, doc_type="CI"):
    src_pdf = os.path.join(str(tmp_path), "source_ci.pdf")
    _make_source_pdf(src_pdf)
    ok = app_mod._save_doc_to_shipment(_NR, src_pdf, "faktura.pdf", doc_type, 1, source="manual")
    assert ok


def _fetch_stamped_row(app_mod):
    db = app_mod.get_db()
    try:
        return db.execute(
            "SELECT * FROM shipment_documents WHERE po_number=? AND source='stamped'",
            (_NR,),
        ).fetchone()
    finally:
        db.close()


def test_stamp_creates_new_file_and_row(manager_client, tmp_path, monkeypatch):
    import app as _app_mod

    monkeypatch.setattr(_app_mod, "_SHIPMENT_DOCS_PATH", str(tmp_path))
    _seed_order(_app_mod)
    _seed_doc(_app_mod, tmp_path, doc_type="CI")

    headers = _csrf_headers(manager_client)
    resp = manager_client.post(
        f"/api/dostawy/{_NR}/stamp",
        json={"doc_type": "CI"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.data
    body = resp.get_json()
    assert body.get("ok") is True

    row = _fetch_stamped_row(_app_mod)
    assert row is not None
    folder = _app_mod._ensure_shipment_folder(_NR)
    assert os.path.exists(os.path.join(folder, row["filename"]))


def test_stamp_missing_document_returns_400(manager_client, tmp_path, monkeypatch):
    import app as _app_mod

    monkeypatch.setattr(_app_mod, "_SHIPMENT_DOCS_PATH", str(tmp_path))
    _seed_order(_app_mod)

    headers = _csrf_headers(manager_client)
    resp = manager_client.post(
        f"/api/dostawy/{_NR}/stamp",
        json={"doc_type": "SAD"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()
