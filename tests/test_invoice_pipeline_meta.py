import sqlite3
import invoice_jobs as ij
import invoice_pipeline as ip
from constants import DocKind


def test_header_meta_variants():
    # Formaty z realnej próbki: "CONTAINER No.:XYZU7654321" (Kestrel, bez spacji),
    # "Container NO. : ABCU1234567" (Corvan, spacja przed dwukropkiem),
    # warunki dostawy łamane do nowej linii.
    t1 = "CONTAINER No.:XYZU7654321 ... Terms of Delivery:\nFOB QINGDAO"
    t2 = "Container NO. : ABCU1234567 ... TERMS OF DELIVERY: FOB SHANGHAI"
    m1, m2 = ip._header_meta(t1), ip._header_meta(t2)
    assert m1["container_no"] == "XYZU7654321"
    assert m1["delivery_terms"].startswith("FOB QINGDAO")
    assert m2["container_no"] == "ABCU1234567"
    assert m2["delivery_terms"].startswith("FOB SHANGHAI")
    assert ip._header_meta("brak danych") == {"container_no": "", "delivery_terms": ""}


def test_weight_map_prefers_matching_pl(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    ij.ensure_invoice_tables(db)
    ij.create_job(db, "b", "pl1.pdf", "pl1.pdf", "", doc_kind=DocKind.PACKING_LIST)
    ij.create_job(db, "b", "pl2.pdf", "pl2.pdf", "", doc_kind=DocKind.PACKING_LIST)
    texts = {"pl1.pdf": "PACKING LIST No.& date of invoice FV/1",
             "pl2.pdf": "PACKING LIST No.& date of invoice FV/2"}
    maps = {"pl1.pdf": {"X": {"weight_net": "1"}},
            "pl2.pdf": {"X": {"weight_net": "9"}}}
    monkeypatch.setattr(ip, "first_pages_text", lambda p, n=2: texts[p])
    monkeypatch.setattr(ip, "extract_weight_map", lambda p: maps[p])
    wm = ip._weight_map_for(db, "b", "FV/2")
    assert wm["X"]["weight_net"] == "9"        # tylko pasująca PL
    wm_fb = ip._weight_map_for(db, "b", "NIEZNANY")
    assert wm_fb["X"]["weight_net"] == "9"     # fallback: unia wszystkich (ostatnia wygrywa)
    assert ip._weight_map_for(db, "pusta-partia", "FV/1") == {}


def test_header_meta_rejects_non_incoterm():
    t = "Terms of Delivery:\n\nInvoice No: INV12345\nDate: 2026-01-01"
    assert ip._header_meta(t)["delivery_terms"] == ""


def test_process_job_persists_container_and_terms(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    import material_master as mm
    import uom
    mm.ensure_table(db)
    uom.ensure_table(db)
    ij.ensure_invoice_tables(db)
    jid = ij.create_job(db, "b", "a.pdf", "a.pdf", "S1")
    monkeypatch.setattr(ip, "get_supplier", lambda code, **k: {"code": "S1"})
    monkeypatch.setattr(ip, "extract_invoice", lambda path, sup: {
        "items": [{"line_no": 1, "raw_ref": "X", "uom_src": "PCS", "qty": "10"}],
        "total_net": "10", "total_qty": "10",
        "raw_text": "Container No.: ABCU1234567 Terms of Delivery: FOB SHANGHAI",
        "invoice_number": "FV/9"})
    assert ip.process_job(db, jid) == "extracted"
    job = ij.get_job(db, jid)
    assert job["container_no"] == "ABCU1234567"
    assert job["delivery_terms"].startswith("FOB SHANGHAI")
