"""tests/test_api_dostawa_advance_customs_gate.py — e2e dla KOLEJKA-03 (Phase 5
Plan 03): TWARDA bramka celna na przejściu do 'dokumenty_wyslane_odprawa'.

Covers:
  - 409 + status niezmieniony przy niekompletnej checkliście celnej, MIMO
    confirm=true (bramka bezwarunkowa, nie przez needs_confirm).
  - 200 + przejście do 'dokumenty_wyslane_odprawa', gdy każdy doc_type
    z CUSTOMS_DOC_TYPES jest 'zatwierdzono'.
  - Miękka bramka innych przejść nietknięta (utworzone → advance działa).
"""
import pytest

pytest.importorskip("flask")

from tests.conftest import CSRF_TOKEN, set_session_csrf  # noqa: E402

import customs_checklist as cc  # noqa: E402

_NR = "KOLGATE01"


def _csrf_headers(client):
    set_session_csrf(client, CSRF_TOKEN)
    return {"X-CSRF-Token": CSRF_TOKEN}


def _ensure_kolejka_tables(db):
    """kolejka_* tworzy `python migrate_db.py` — brak ich w izolowanej bazie
    testowej. SUPERSET kolumn potrzebnych tu ORAZ w test_api_kolejka_stamp.py
    i test_api_forwarding_order.py (wspólna baza per sesja pytest) + backfill
    ALTER TABLE w try/except, gdyby węższa wersja już istniała."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS kolejka_zlecenia (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nr_zamowienia TEXT NOT NULL,
            supplier_name TEXT DEFAULT '',
            status TEXT DEFAULT 'utworzone',
            mir7_number TEXT DEFAULT '',
            department TEXT DEFAULT '',
            kontener_id INTEGER,
            rodzaj_transportu TEXT DEFAULT '',
            planowane_etd TEXT DEFAULT '',
            data_dostawy TEXT DEFAULT '',
            zgoda_wyplyniecie INTEGER DEFAULT 0,
            uwagi TEXT DEFAULT '',
            updated_at TEXT DEFAULT '',
            UNIQUE(nr_zamowienia)
        )
    """)
    for col, definition in [
        ("kontener_id", "INTEGER"),
        ("rodzaj_transportu", "TEXT DEFAULT ''"),
        ("planowane_etd", "TEXT DEFAULT ''"),
        ("data_dostawy", "TEXT DEFAULT ''"),
        ("zgoda_wyplyniecie", "INTEGER DEFAULT 0"),
        ("uwagi", "TEXT DEFAULT ''"),
        ("updated_at", "TEXT DEFAULT ''"),
    ]:
        try:
            db.execute(f"ALTER TABLE kolejka_zlecenia ADD COLUMN {col} {definition}")
        except Exception:
            pass  # kolumna już istnieje
    db.execute("""
        CREATE TABLE IF NOT EXISTS kolejka_status_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zlecenie_id    INTEGER,
            kontener_id    INTEGER,
            pole           TEXT NOT NULL,
            stara_wartosc  TEXT DEFAULT '',
            nowa_wartosc   TEXT DEFAULT '',
            krok           INTEGER,
            changed_by     INTEGER,
            changed_at     TEXT DEFAULT (datetime('now'))
        )
    """)
    db.commit()


def _seed(app_mod, checklist_statuses):
    """Zlecenie w statusie 'dostawa_przychodzaca' (next → dokumenty_wyslane_odprawa)
    + checklista celna z zadanymi statusami per doc_type."""
    db = app_mod.get_db()
    try:
        _ensure_kolejka_tables(db)
        db.execute("DELETE FROM kolejka_zlecenia WHERE nr_zamowienia=?", (_NR,))
        db.execute("DELETE FROM delivery_checklists WHERE po_number=?", (_NR,))
        db.execute(
            "INSERT INTO kolejka_zlecenia(nr_zamowienia, supplier_name, status) "
            "VALUES (?, ?, 'dostawa_przychodzaca')",
            (_NR, "Test Supplier"),
        )
        for doc, st in checklist_statuses.items():
            db.execute(
                "INSERT INTO delivery_checklists(po_number, supplier_name, "
                "container_number, doc_type, status, created_by) "
                "VALUES (?,?,?,?,?,1)",
                (_NR, "Test Supplier", "", doc, st),
            )
        db.commit()
    finally:
        db.close()


def _status(app_mod):
    db = app_mod.get_db()
    try:
        row = db.execute(
            "SELECT status FROM kolejka_zlecenia WHERE nr_zamowienia=?", (_NR,)
        ).fetchone()
        return row["status"] if row else None
    finally:
        db.close()


def test_incomplete_checklist_blocks_despite_confirm_true(manager_client):
    import app as _app_mod

    statuses = {t: "zatwierdzono" for t in cc.CUSTOMS_DOC_TYPES}
    statuses["SAD"] = "wgrano"  # wgrane ≠ zatwierdzone
    _seed(_app_mod, statuses)

    headers = _csrf_headers(manager_client)
    resp = manager_client.post(
        f"/api/dostawy/{_NR}/advance", json={"confirm": True}, headers=headers
    )
    assert resp.status_code == 409, resp.data
    body = resp.get_json()
    assert body.get("blocked") is True
    assert "SAD" in body.get("missing", [])
    assert _status(_app_mod) == "dostawa_przychodzaca"  # status niezmieniony


def test_complete_checklist_allows_advance(manager_client):
    import app as _app_mod

    _seed(_app_mod, {t: "zatwierdzono" for t in cc.CUSTOMS_DOC_TYPES})

    headers = _csrf_headers(manager_client)
    resp = manager_client.post(
        f"/api/dostawy/{_NR}/advance", json={"confirm": True}, headers=headers
    )
    assert resp.status_code == 200, resp.data
    assert resp.get_json().get("status") == "dokumenty_wyslane_odprawa"
    assert _status(_app_mod) == "dokumenty_wyslane_odprawa"


def test_soft_gate_for_other_transitions_untouched(manager_client):
    import app as _app_mod

    _seed(_app_mod, {})  # brak checklisty — dla innych przejść bez znaczenia
    db = _app_mod.get_db()
    try:
        db.execute(
            "UPDATE kolejka_zlecenia SET status='utworzone' WHERE nr_zamowienia=?",
            (_NR,),
        )
        db.commit()
    finally:
        db.close()

    headers = _csrf_headers(manager_client)
    resp = manager_client.post(
        f"/api/dostawy/{_NR}/advance", json={"confirm": True}, headers=headers
    )
    assert resp.status_code == 200, resp.data
    assert _status(_app_mod) == "wyslane_do_dostawcy"
