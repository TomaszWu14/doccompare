import sqlite3
from decimal import Decimal

import pytest

import invoice_jobs as ij
import sad_draft_export as sad
from constants import DocKind, InvoiceJobStatus


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    import material_master as mm
    mm.ensure_table(db)
    ij.ensure_invoice_tables(db)
    return db


def _mk_job(db, num, doc_kind=DocKind.INVOICE, supplier="CORVAN",
            status=InvoiceJobStatus.CONFIRMED, items=()):
    jid = ij.create_job(db, "b1", f"{num}.pdf", f"{num}.pdf", supplier, doc_kind=doc_kind)
    ij.update_job(db, jid, status=status, invoice_number=num)
    ij.save_items(db, jid, list(items))
    return ij.get_job(db, jid)


IT = dict(line_no=1, raw_ref="S001", master_ref="S001", name_pl="Staza",
          tariff_cn="90189084", qty="100", uom_factor="", uom_src="PCS",
          amount="96.50", weight_net="10", weight_gross="12", cartons="2",
          sent=0, match_status="matched", skipped=0)


def _jw(db, *jobs):
    return [{"job": j, "items": ij.get_items(db, j["id"])} for j in jobs]


def test_two_invoices_same_exporter_cn_merge():
    db = _db()
    j1 = _mk_job(db, "26H00001", items=[IT])
    j2 = _mk_job(db, "26H30514", items=[dict(IT, qty="50", amount="48.25")])
    rows = sad.aggregate_positions(db, _jw(db, j1, j2))
    assert len(rows) == 1
    r = rows[0]
    assert r["qty_szt"] == Decimal("150")
    assert r["value"] == Decimal("144.75")
    assert r["weight_net"] == Decimal("20")
    assert r["cartons"] == Decimal("4")
    assert "26H00001" in r["invoice_numbers"] and "26H30514" in r["invoice_numbers"]


def test_proforma_adds_samples_note_and_qty():
    db = _db()
    j1 = _mk_job(db, "26H00001", items=[IT])
    j2 = _mk_job(db, "26H00001S", doc_kind=DocKind.PROFORMA,
                 items=[dict(IT, qty="2", amount="0.02")])
    rows = sad.aggregate_positions(db, _jw(db, j1, j2))
    r = rows[0]
    assert r["qty_szt"] == Decimal("102")
    assert "WRAZ Z PRÓBKAMI" in r["opis_pl"]
    assert r["proforma_numbers"] == "26H00001S"
    assert r["invoice_numbers"] == "26H00001"


def test_standalone_proforma_without_s_suffix_no_samples_note():
    """P6: proforma bez sufiksu -S i bez CI o numerze bazowym w batchu — nie jest
    próbką (spec 2026-09-14: heurystyka sufiks -S lub numer CI + 'S')."""
    db = _db()
    j = _mk_job(db, "PF-2026-11", doc_kind=DocKind.PROFORMA, items=[IT])
    rows = sad.aggregate_positions(db, _jw(db, j))
    r = rows[0]
    assert "WRAZ Z PRÓBKAMI" not in r["opis_pl"]
    assert r["proforma_numbers"] == "PF-2026-11"
    assert r["qty_szt"] == Decimal("100")   # ilość i wartość zawsze wliczane


def test_standalone_proforma_with_dash_s_suffix_is_samples():
    """P6: samodzielna proforma z sufiksem -S jest próbką, nawet bez CI w batchu."""
    db = _db()
    j = _mk_job(db, "A260709291H-S", doc_kind=DocKind.PROFORMA, items=[IT])
    rows = sad.aggregate_positions(db, _jw(db, j))
    r = rows[0]
    assert "WRAZ Z PRÓBKAMI" in r["opis_pl"]
    assert r["proforma_numbers"] == "A260709291H-S"


def test_unmatched_separate_row_with_note():
    db = _db()
    j = _mk_job(db, "F1", items=[
        IT,
        dict(IT, line_no=2, raw_ref="Q", master_ref="", name_pl="",
             tariff_cn="", match_status="unmatched")])
    rows = sad.aggregate_positions(db, _jw(db, j))
    assert len(rows) == 2
    brak = [r for r in rows if not r["tariff_cn"]][0]
    assert "BRAK CN" in brak["uwagi"]


def test_uom_factor_multiplies_qty_and_missing_factor_notes():
    db = _db()
    j = _mk_job(db, "F1", items=[
        dict(IT, qty="10", uom_factor="24.0", uom_src="CTN"),
        dict(IT, line_no=2, qty="5", uom_factor="", uom_src="CTN")])
    rows = sad.aggregate_positions(db, _jw(db, j))
    r = rows[0]
    assert r["qty_szt"] == Decimal("245")      # 10*24 + 5*1
    assert "jednostki źródłowe" in r["uwagi"]


def test_skipped_items_excluded():
    db = _db()
    j = _mk_job(db, "F1", items=[IT, dict(IT, line_no=2, skipped=1, qty="999")])
    rows = sad.aggregate_positions(db, _jw(db, j))
    assert rows[0]["qty_szt"] == Decimal("100")


def test_price_fallback_note_added_once_per_group():
    db = _db()
    j = _mk_job(db, "F1", items=[
        dict(IT, net_amount="", amount="5"),
        dict(IT, line_no=2, net_amount="", amount="5"),
    ])
    rows = sad.aggregate_positions(db, _jw(db, j))
    assert len(rows) == 1
    assert rows[0]["uwagi"].count("ceny jednostkowej") == 1


def test_no_price_fallback_note_when_net_amount_present():
    db = _db()
    j = _mk_job(db, "F1", items=[dict(IT, net_amount="96.50")])
    rows = sad.aggregate_positions(db, _jw(db, j))
    assert "ceny jednostkowej" not in rows[0]["uwagi"]


def test_collect_batch_gating_and_ok():
    db = _db()
    _mk_job(db, "F1", items=[IT])
    _mk_job(db, "F2", status=InvoiceJobStatus.EXTRACTED, items=[IT])
    with pytest.raises(ValueError, match="1/2"):
        sad.collect_batch(db, "b1")


def test_collect_batch_header_sums():
    db = _db()
    j1 = _mk_job(db, "F1", items=[IT])
    ij.update_job(db, j1["id"], container_no="ABCU1234567",
                  delivery_terms="FOB SHANGHAI")
    header, positions = sad.collect_batch(db, "b1")
    assert header["container"] == "ABCU1234567"
    assert header["delivery_terms"] == "FOB SHANGHAI"
    assert header["total_value"] == Decimal("96.5")
    assert header["total_gross"] == Decimal("12")
    assert len(positions) == 1
    assert header["documents"][0]["status"] == "confirmed"   # P2


def test_workbook_contract():
    pytest.importorskip("openpyxl")   # CI (minimalne zależności) nie ma openpyxl
    header = {"container": "ABCU1234567", "container_warning": False,
              "delivery_terms": "FOB SHANGHAI", "country_dispatch": "CN",
              "currency": "USD", "total_value": Decimal("144.75"),
              "total_net": Decimal("20"), "total_gross": Decimal("24"),
              "total_cartons": Decimal("4"),
              "documents": [{"number": "26H00001", "supplier": "CORVAN",
                             "kind": "invoice", "status": "confirmed"}]}
    pos = [{"lp": 1, "exporter": "CORVAN", "tariff_cn": "90189084",
            "customs_code": "V020", "opis_pl": "Staza",
            "qty_szt": Decimal("150"), "weight_net": Decimal("20"),
            "cartons": Decimal("4"), "value": Decimal("144.75"), "country": "CN",
            "invoice_numbers": "26H00001", "proforma_numbers": "",
            "sent": 1, "uwagi": ""}]
    wb = sad.build_sad_workbook(header, pos)
    assert wb.sheetnames == ["Nagłówek", "Pozycje"]
    wp = wb["Pozycje"]
    assert [c.value for c in wp[1]] == sad.SAD_COLUMNS
    assert wp.cell(row=2, column=2).value == "CORVAN"
    assert wp.cell(row=2, column=13).value == "TAK"
    ws = wb["Nagłówek"]
    doc_row = next(r for r in ws.iter_rows(values_only=True) if r[0] == "invoice")
    assert doc_row[3] == "confirmed"   # P2: status jako 4. kolumna listy dokumentów
