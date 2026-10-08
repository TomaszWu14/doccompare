"""tests/test_api_forwarding_order.py — e2e dla KOLEJKA-02 (Phase 5 Plan 02):
POST /api/dostawy/<nr>/forwarding-order.

Covers:
  - 409 (bez wiersza shipment_documents) dla NIEPOTWIERDZONEGO zlecenia
    (zgoda_wyplyniecie != 1) — bez furtki confirm (D-02).
  - 200 dla potwierdzonego zlecenia: nowy wiersz shipment_documents
    (doc_type='ZS', source='forwarding_order') + plik na dysku + bridge do
    transport_queue (forwarder_id, sent_to_forwarder_at) → zlecenie widoczne
    w /spedycja dla przypisanego spedytora.
  - 404 dla nieistniejącej dostawy.
"""
import os

import pytest

pytest.importorskip("flask")
pytest.importorskip("reportlab")
pytest.importorskip("pypdf")

from tests.conftest import CSRF_TOKEN, set_session_csrf, login  # noqa: E402

_NR = "KOLFWD01"


def _csrf_headers(client):
    set_session_csrf(client, CSRF_TOKEN)
    return {"X-CSRF-Token": CSRF_TOKEN}


def _ensure_kolejka_tables(db):
    """kolejka_* tworzy `python migrate_db.py` (osobny krok od init_db app.py) —
    w izolowanej bazie testowej ich nie ma. Superset kolumn potrzebnych tu
    ORAZ w test_api_kolejka_stamp.py (wspólna baza per sesja pytest, kolejność
    alfabetyczna → ten plik tworzy tabelę pierwszy); backfill ALTER TABLE w
    try/except na wypadek, gdyby istniała już węższa wersja."""
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
            UNIQUE(nr_zamowienia)
        )
    """)
    for col, definition in [
        ("kontener_id", "INTEGER"),
        ("rodzaj_transportu", "TEXT DEFAULT ''"),
        ("planowane_etd", "TEXT DEFAULT ''"),
        ("data_dostawy", "TEXT DEFAULT ''"),
        ("zgoda_wyplyniecie", "INTEGER DEFAULT 0"),
    ]:
        try:
            db.execute(f"ALTER TABLE kolejka_zlecenia ADD COLUMN {col} {definition}")
        except Exception:
            pass  # kolumna już istnieje
    db.execute("""
        CREATE TABLE IF NOT EXISTS kolejka_kontenery (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numer_kontenera TEXT NOT NULL,
            typ_transportu TEXT DEFAULT '',
            forwarder_id INTEGER,
            etd_plan TEXT DEFAULT '',
            eta TEXT DEFAULT '',
            origin_port TEXT DEFAULT '',
            dest_port TEXT DEFAULT '',
            UNIQUE(numer_kontenera)
        )
    """)
    db.commit()


def _seed(app_mod, confirmed=True, with_forwarder=True):
    """Seeduje spedytora + kontener + zlecenie. Zwraca forwarder_id."""
    db = app_mod.get_db()
    try:
        _ensure_kolejka_tables(db)
        db.execute("DELETE FROM shipment_documents WHERE po_number=?", (_NR,))
        db.execute("DELETE FROM kolejka_zlecenia WHERE nr_zamowienia=?", (_NR,))
        db.execute("DELETE FROM kolejka_kontenery WHERE numer_kontenera=?", ("TESTU7654321",))
        db.execute("DELETE FROM transport_queue WHERE po_numbers LIKE ?", (f"%{_NR}%",))

        cur = db.execute(
            "INSERT INTO freight_forwarders(name, company, email, phone, address) "
            "VALUES (?,?,?,?,?)",
            ("Jan Spedytor", "Sped-Alfa Sp. z o.o.", "jan@spedalfa.pl",
             "+48 123 456 789", "ul. Portowa 1, Gdańsk"),
        )
        fwd_id = cur.lastrowid

        cur = db.execute(
            "INSERT INTO kolejka_kontenery(numer_kontenera, typ_transportu, forwarder_id, "
            "etd_plan, eta, origin_port, dest_port) VALUES (?,?,?,?,?,?,?)",
            ("TESTU7654321", "40HQ", fwd_id if with_forwarder else None,
             "2026-10-01", "2026-11-10", "Shanghai", "Gdansk"),
        )
        kont_id = cur.lastrowid

        db.execute(
            "INSERT INTO kolejka_zlecenia(nr_zamowienia, supplier_name, status, "
            "kontener_id, rodzaj_transportu, planowane_etd, data_dostawy, zgoda_wyplyniecie) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (_NR, "Eastport Medical", "utworzone", kont_id, "40HQ",
             "2026-10-01", "2026-11-15", 1 if confirmed else 0),
        )
        db.commit()
        return fwd_id
    finally:
        db.close()


def _fetch_zs_row(app_mod):
    db = app_mod.get_db()
    try:
        return db.execute(
            "SELECT * FROM shipment_documents WHERE po_number=? AND source='forwarding_order'",
            (_NR,),
        ).fetchone()
    finally:
        db.close()


def test_unconfirmed_order_returns_409_and_writes_nothing(manager_client, tmp_path, monkeypatch):
    import app as _app_mod

    monkeypatch.setattr(_app_mod, "_SHIPMENT_DOCS_PATH", str(tmp_path))
    _seed(_app_mod, confirmed=False)

    headers = _csrf_headers(manager_client)
    resp = manager_client.post(f"/api/dostawy/{_NR}/forwarding-order", headers=headers)
    assert resp.status_code == 409, resp.data
    body = resp.get_json()
    assert "zgoda_wyplyniecie" in body.get("missing", [])
    assert _fetch_zs_row(_app_mod) is None


def test_unknown_order_returns_404(manager_client, tmp_path, monkeypatch):
    import app as _app_mod

    monkeypatch.setattr(_app_mod, "_SHIPMENT_DOCS_PATH", str(tmp_path))
    _seed(_app_mod)

    headers = _csrf_headers(manager_client)
    resp = manager_client.post("/api/dostawy/NOPE-404/forwarding-order", headers=headers)
    assert resp.status_code == 404


def test_confirmed_order_generates_zs_and_bridges_to_spedycja(manager_client, tmp_path, monkeypatch):
    import app as _app_mod

    monkeypatch.setattr(_app_mod, "_SHIPMENT_DOCS_PATH", str(tmp_path))
    fwd_id = _seed(_app_mod, confirmed=True)

    headers = _csrf_headers(manager_client)
    resp = manager_client.post(f"/api/dostawy/{_NR}/forwarding-order", headers=headers)
    assert resp.status_code == 200, resp.data
    assert resp.get_json().get("ok") is True

    # Nowy wiersz shipment_documents (doc_type='ZS', source='forwarding_order') + plik.
    row = _fetch_zs_row(_app_mod)
    assert row is not None
    assert row["doc_type"] == "ZS"
    folder = _app_mod._ensure_shipment_folder(_NR)
    pdf_path = os.path.join(folder, row["filename"])
    assert os.path.exists(pdf_path)
    with open(pdf_path, "rb") as f:
        assert f.read(4) == b"%PDF"

    # Bridge: transport_queue z przypisanym spedytorem i datą wysłania.
    db = _app_mod.get_db()
    try:
        tq = db.execute(
            "SELECT forwarder_id, sent_to_forwarder_at FROM transport_queue "
            "WHERE po_numbers LIKE ?", (f"%{_NR}%",)
        ).fetchone()
        # Przypisz konto it_forwarder do firmy spedycyjnej PRZED logowaniem
        # (sesja czyta users.forwarder_id przy logowaniu).
        db.execute("UPDATE users SET forwarder_id=? WHERE username='it_forwarder'", (fwd_id,))
        db.commit()
    finally:
        db.close()
    assert tq is not None
    assert tq["forwarder_id"] == fwd_id
    assert (tq["sent_to_forwarder_at"] or "") != ""

    # Widoczność w /spedycja dla przypisanego spedytora (osobny klient —
    # fixtures manager/forwarder współdzielą jeden `client` w ramach testu).
    fwd_client = _app_mod.app.test_client()
    login_resp = login(fwd_client, "it_forwarder")
    assert login_resp.status_code in (302, 303)
    page = fwd_client.get("/spedycja")
    assert page.status_code == 200
    assert _NR.encode() in page.data
