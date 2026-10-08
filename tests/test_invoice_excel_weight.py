import pytest
pytest.importorskip("openpyxl")
import invoice_excel_export as ex

def test_weights_written_to_excel():
    rows = [{"invoice_number": "FV/1", "qty": "10", "master_ref": "X", "name_pl": "Rękawice",
             "weight_net": "12.5", "weight_gross": "13.2", "uom_factor": "", "amount": "50",
             "tariff_cn": "4015", "sent": 1, "match_status": "matched", "skipped": 0}]
    wb = ex.build_workbook(rows)
    ws = wb.active
    header = [c.value for c in ws[1]]
    ni, gi = header.index("Waga netto"), header.index("Waga brutto")
    r2 = [c.value for c in ws[2]]
    assert r2[ni] == "12.5" and r2[gi] == "13.2"
