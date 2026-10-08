import pytest

import app as appmod
import invoice_routes


def _ensure_invoice_tables():
    # coverage() lista dostawców czyta bezpośrednio z invoice_jobs — tabela
    # powstaje leniwie (ij.create_job); testy monkeypatchujące list_jobs muszą
    # ją mieć jawnie, inaczej zapytanie grupujące w coverage() wybuchnie.
    db = appmod.get_db()
    try:
        invoice_routes.ij.ensure_invoice_tables(db)
    finally:
        db.close()


def test_sad_route_registered():
    rules = {r.rule for r in appmod.app.url_map.iter_rules()}
    assert "/invoices/sad/<batch_id>" in rules


def test_sad_route_requires_login():
    client = appmod.app.test_client()
    r = client.get("/invoices/sad/abc123")
    assert r.status_code in (302, 401)


def test_sad_route_409_when_batch_incomplete(admin_client, monkeypatch):
    import sad_draft_export

    def _raise(db, batch_id):
        raise ValueError("Zatwierdzono 1/2 dokumentów — dokończ przegląd przed generowaniem draftu SAD")

    monkeypatch.setattr(sad_draft_export, "collect_batch", _raise)
    resp = admin_client.get("/invoices/sad/b1")
    assert resp.status_code == 409
    assert "1/2" in resp.get_data(as_text=True)


def test_coverage_gates_sad_link_until_all_confirmed(admin_client, monkeypatch):
    """P1: link Draft SAD zablokowany, dopóki nie wszystkie dokumenty faktura-podobne
    batcha są zatwierdzone; licznik 'zatwierdzono X/Y' widoczny zamiast linku."""
    _ensure_invoice_tables()
    jobs = [
        {"id": 1, "filename": "a.pdf", "doc_kind": "invoice", "status": "confirmed",
         "supplier_code": "S1", "invoice_number": "A1", "page_from": 0, "page_to": 0},
        {"id": 2, "filename": "b.pdf", "doc_kind": "invoice", "status": "extracted",
         "supplier_code": "S1", "invoice_number": "A2", "page_from": 0, "page_to": 0},
    ]
    monkeypatch.setattr(invoice_routes.ij, "list_jobs", lambda db, batch_id: jobs)
    monkeypatch.setattr(invoice_routes, "get_all_suppliers", lambda: [])
    resp = admin_client.get("/invoices/coverage?batch_id=b1")
    body = resp.get_data(as_text=True)
    assert "zatwierdzono 1/2" in body
    assert "/invoices/sad/" not in body


def test_coverage_shows_sad_link_when_all_confirmed(admin_client, monkeypatch):
    _ensure_invoice_tables()
    jobs = [
        {"id": 1, "filename": "a.pdf", "doc_kind": "invoice", "status": "confirmed",
         "supplier_code": "S1", "invoice_number": "A1", "page_from": 0, "page_to": 0},
        {"id": 2, "filename": "b.pdf", "doc_kind": "proforma", "status": "confirmed",
         "supplier_code": "S1", "invoice_number": "A1-S", "page_from": 0, "page_to": 0},
    ]
    monkeypatch.setattr(invoice_routes.ij, "list_jobs", lambda db, batch_id: jobs)
    monkeypatch.setattr(invoice_routes, "get_all_suppliers", lambda: [])
    resp = admin_client.get("/invoices/coverage?batch_id=b1")
    body = resp.get_data(as_text=True)
    assert "/invoices/sad/b1" in body
    assert "zatwierdzono" not in body


def test_sad_route_200_xlsx(admin_client, monkeypatch):
    pytest.importorskip("openpyxl")
    import sad_draft_export

    header = {
        "container": "X", "container_warning": False, "delivery_terms": "",
        "country_dispatch": "CN", "currency": "USD",
        "total_value": None, "total_net": None, "total_gross": None,
        "total_cartons": None, "documents": [],
    }
    monkeypatch.setattr(sad_draft_export, "collect_batch", lambda db, batch_id: (header, []))
    resp = admin_client.get("/invoices/sad/b1")
    assert resp.status_code == 200
    assert resp.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert "draft_sad_b1.xlsx" in resp.headers.get("Content-Disposition", "")
