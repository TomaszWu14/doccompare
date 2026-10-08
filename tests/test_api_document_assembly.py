"""tests/test_api_document_assembly.py — e2e dla KOLEJKA-05 (Phase 5 Plan 04):
POST /api/dostawy/<nr>/assemble-customs-set.

Covers:
  - 400 + zero artefaktów (wiersz/plik SET) dla zlecenia bez pasujących dokumentów.
  - 200 dla zlecenia z wgranymi dokumentami: dokładnie JEDEN nowy wiersz
    shipment_documents (doc_type='SET', source='assembled'), plik SET w folderze
    per-PO, liczba stron == suma stron wejściowych, oryginalne wiersze/pliki
    NIETKNIĘTE.
  - 404 dla nieistniejącej dostawy.
"""
import io
import os

import pytest

pytest.importorskip("flask")
pytest.importorskip("pypdf")

from pypdf import PdfReader, PdfWriter  # noqa: E402

from tests.conftest import CSRF_TOKEN, set_session_csrf  # noqa: E402

import customs_checklist as cc  # noqa: E402

_NR = "KOLASM01"


def _csrf_headers(client):
    set_session_csrf(client, CSRF_TOKEN)
    return {"X-CSRF-Token": CSRF_TOKEN}


def _ensure_kolejka_tables(db):
    """kolejka_* tworzy `python migrate_db.py` — brak ich w izolowanej bazie
    testowej. Ten plik jest PIERWSZY alfabetycznie wśród testów kolejka_*
    (wspólna baza per sesja pytest), więc tworzy PEŁNY superset kolumn
    potrzebnych też w test_api_dostawa_advance_customs_gate.py,
    test_api_forwarding_order.py i test_api_kolejka_stamp.py; backfill
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


def _seed_order(app_mod):
    db = app_mod.get_db()
    try:
        _ensure_kolejka_tables(db)
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


def _make_pdf(path, pages):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=300, height=300)
    with open(path, "wb") as f:
        writer.write(f)


def _seed_docs(app_mod, tmp_path, pages_per_type):
    """Wgrywa po jednym dokumencie per doc_type przez _save_doc_to_shipment
    (ten sam substrat zapisu co produkcja). Zwraca sumę stron."""
    total = 0
    for doc_type, pages in pages_per_type.items():
        src = os.path.join(str(tmp_path), f"src_{doc_type}.pdf")
        _make_pdf(src, pages)
        ok = app_mod._save_doc_to_shipment(
            _NR, src, f"{doc_type.lower()}_doc.pdf", doc_type, 1, source="manual"
        )
        assert ok
        total += pages
    return total


def _fetch_rows(app_mod, source=None):
    db = app_mod.get_db()
    try:
        if source:
            return db.execute(
                "SELECT * FROM shipment_documents WHERE po_number=? AND source=?",
                (_NR, source),
            ).fetchall()
        return db.execute(
            "SELECT * FROM shipment_documents WHERE po_number=? ORDER BY id",
            (_NR,),
        ).fetchall()
    finally:
        db.close()


def test_no_documents_returns_400_and_writes_nothing(manager_client, tmp_path, monkeypatch):
    import app as _app_mod

    monkeypatch.setattr(_app_mod, "_SHIPMENT_DOCS_PATH", str(tmp_path))
    _seed_order(_app_mod)

    headers = _csrf_headers(manager_client)
    resp = manager_client.post(f"/api/dostawy/{_NR}/assemble-customs-set", headers=headers)
    assert resp.status_code == 400, resp.data
    assert "error" in resp.get_json()

    # Zero artefaktów: brak wiersza SET i brak pliku SET w folderze per-PO.
    assert _fetch_rows(_app_mod, source="assembled") == []
    folder = _app_mod._ensure_shipment_folder(_NR)  # makedirs — zawsze istnieje
    assert not any(f.startswith("SET__") for f in os.listdir(folder))


def test_unknown_order_returns_404(manager_client, tmp_path, monkeypatch):
    import app as _app_mod

    monkeypatch.setattr(_app_mod, "_SHIPMENT_DOCS_PATH", str(tmp_path))
    _seed_order(_app_mod)

    headers = _csrf_headers(manager_client)
    resp = manager_client.post("/api/dostawy/NOPE-404/assemble-customs-set", headers=headers)
    assert resp.status_code == 404


def test_assembles_set_from_latest_docs_and_preserves_originals(manager_client, tmp_path, monkeypatch):
    import app as _app_mod

    monkeypatch.setattr(_app_mod, "_SHIPMENT_DOCS_PATH", str(tmp_path))
    _seed_order(_app_mod)
    pages = {t: i + 1 for i, t in enumerate(cc.CUSTOMS_DOC_TYPES)}  # CI=1, PL=2, BL=3, SAD=4
    total_pages = _seed_docs(_app_mod, tmp_path, pages)

    before_rows = _fetch_rows(_app_mod)
    folder = _app_mod._ensure_shipment_folder(_NR)
    before_files = {
        f: os.path.getsize(os.path.join(folder, f)) for f in os.listdir(folder)
    }

    headers = _csrf_headers(manager_client)
    resp = manager_client.post(f"/api/dostawy/{_NR}/assemble-customs-set", headers=headers)
    assert resp.status_code == 200, resp.data
    body = resp.get_json()
    assert body.get("ok") is True
    assert sorted(body.get("included", [])) == sorted(cc.CUSTOMS_DOC_TYPES)

    # Dokładnie JEDEN nowy wiersz doc_type='SET'/source='assembled' + plik.
    set_rows = _fetch_rows(_app_mod, source="assembled")
    assert len(set_rows) == 1
    assert set_rows[0]["doc_type"] == "SET"
    set_path = os.path.join(folder, set_rows[0]["filename"])
    assert os.path.exists(set_path)
    with open(set_path, "rb") as f:
        set_bytes = f.read()
    assert set_bytes.startswith(b"%PDF")
    assert len(PdfReader(io.BytesIO(set_bytes)).pages) == total_pages

    # Oryginalne wiersze i pliki NIETKNIĘTE (nowy wiersz tylko dochodzi).
    after_rows = _fetch_rows(_app_mod)
    assert len(after_rows) == len(before_rows) + 1
    originals = {r["id"]: r for r in after_rows if r["source"] == "manual"}
    assert len(originals) == len(before_rows)
    for r in before_rows:
        assert r["id"] in originals
        assert originals[r["id"]]["filename"] == r["filename"]
    for fname, size in before_files.items():
        p = os.path.join(folder, fname)
        assert os.path.exists(p)
        assert os.path.getsize(p) == size
