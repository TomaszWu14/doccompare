"""tests/test_warehouse_blueprint_validation.py — REFACTOR-03 coverage for
blueprints/warehouse.py's Pydantic @validate_body route (Phase 3, plan 03-03).

Covers PUT /api/warehouse/receipts/<rid> (WarehouseReceiptUpdate):
  - invalid `status` -> 400 {"error": ...} + no DB write (even alongside an
    otherwise-valid field — the whole body is rejected, not just the bad field)
  - negative `unload_norm_min` -> 400 (int >= 0 enforced by the model; the
    pre-Pydantic behavior silently clamped negative values to 0 instead)
  - valid partial body -> 200, updates only the supplied column
  - a body with none of the allowed fields -> 400 "Brak pól" (pre-existing
    behavior, preserved)
"""
import pytest

pytest.importorskip("flask")

from tests.conftest import CSRF_TOKEN, set_session_csrf  # noqa: E402


def _csrf_headers(client):
    set_session_csrf(client, CSRF_TOKEN)
    return {"X-CSRF-Token": CSRF_TOKEN}


def _seed_receipt(app_mod, container_number="WHTEST01", notes="original notes"):
    db = app_mod.get_db()
    try:
        db.execute("DELETE FROM warehouse_receipts WHERE container_number=?", (container_number,))
        db.execute(
            "INSERT INTO warehouse_receipts(container_number, status, notes) VALUES (?,?,?)",
            (container_number, "przybyle", notes),
        )
        db.commit()
        row = db.execute(
            "SELECT id, status, notes FROM warehouse_receipts WHERE container_number=?",
            (container_number,),
        ).fetchone()
    finally:
        db.close()
    return row["id"]


def _fetch_receipt(app_mod, rid):
    db = app_mod.get_db()
    try:
        return db.execute(
            "SELECT status, notes, unload_norm_min FROM warehouse_receipts WHERE id=?", (rid,)
        ).fetchone()
    finally:
        db.close()


def test_invalid_status_rejected_no_db_write(manager_client):
    import app as _app_mod

    rid = _seed_receipt(_app_mod, container_number="WHTEST01", notes="original notes")
    headers = _csrf_headers(manager_client)
    resp = manager_client.put(
        f"/api/warehouse/receipts/{rid}",
        json={"status": "not-a-real-status", "notes": "changed notes"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()

    row = _fetch_receipt(_app_mod, rid)
    assert row["status"] == "przybyle"
    assert row["notes"] == "original notes"  # untouched


def test_negative_unload_norm_min_rejected(manager_client):
    import app as _app_mod

    rid = _seed_receipt(_app_mod, container_number="WHTEST02")
    headers = _csrf_headers(manager_client)
    resp = manager_client.put(
        f"/api/warehouse/receipts/{rid}",
        json={"unload_norm_min": -5},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_valid_partial_update_changes_only_supplied_field(manager_client):
    import app as _app_mod

    rid = _seed_receipt(_app_mod, container_number="WHTEST03", notes="original notes")
    headers = _csrf_headers(manager_client)
    resp = manager_client.put(
        f"/api/warehouse/receipts/{rid}",
        json={"notes": "updated notes only"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.data
    body = resp.get_json()
    assert body.get("ok") is True

    row = _fetch_receipt(_app_mod, rid)
    assert row["notes"] == "updated notes only"
    assert row["status"] == "przybyle"  # unchanged — partial update


def test_explicit_null_status_ignored_not_500(manager_client):
    """CR-01 regression: explicit JSON null for `status` must be treated as
    "not provided" (partial-update semantics), not crash with a 500 from the
    DB CHECK constraint on status=''."""
    import app as _app_mod

    rid = _seed_receipt(_app_mod, container_number="WHTEST05")
    headers = _csrf_headers(manager_client)
    resp = manager_client.put(
        f"/api/warehouse/receipts/{rid}",
        json={"status": None, "notes": "still works"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.data

    row = _fetch_receipt(_app_mod, rid)
    assert row["status"] == "przybyle"  # untouched — null treated as absent
    assert row["notes"] == "still works"


def test_explicit_null_unload_norm_min_ignored_not_500(manager_client):
    """CR-01 regression: explicit JSON null for `unload_norm_min` must not
    reach `int(None)` and crash with a 500."""
    import app as _app_mod

    rid = _seed_receipt(_app_mod, container_number="WHTEST06")
    original = _fetch_receipt(_app_mod, rid)["unload_norm_min"]
    headers = _csrf_headers(manager_client)
    resp = manager_client.put(
        f"/api/warehouse/receipts/{rid}",
        json={"unload_norm_min": None, "notes": "still works too"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.data

    row = _fetch_receipt(_app_mod, rid)
    assert row["unload_norm_min"] == original  # untouched — null treated as absent
    assert row["notes"] == "still works too"


def test_empty_body_rejected_brak_pol(manager_client):
    import app as _app_mod

    rid = _seed_receipt(_app_mod, container_number="WHTEST04")
    headers = _csrf_headers(manager_client)
    resp = manager_client.put(
        f"/api/warehouse/receipts/{rid}",
        json={},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_stock_import_route_unaffected():
    """/api/warehouse/stock/import stays on request.files, not a Pydantic body —
    smoke-check via source inspection (out of REFACTOR-03's Pydantic scope)."""
    import inspect

    import blueprints.warehouse as _warehouse_mod

    src = inspect.getsource(_warehouse_mod.api_warehouse_stock_import)
    assert "request.files" in src
    assert "validate_body" not in src
