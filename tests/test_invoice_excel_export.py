import pytest
pytest.importorskip("openpyxl")  # build_workbook używa openpyxl; minimalne CI go nie ma
import invoice_excel_export as ex

def _row(**kw):
    base = {"invoice_number": "FV/1", "qty": "10", "raw_ref": "X", "master_ref": "X",
            "name_pl": "Rękawice", "weight_net": "1", "weight_gross": "1.2",
            "uom_factor": "24.0", "amount": "250", "tariff_cn": "4015", "sent": 1,
            "match_status": "matched", "skipped": 0}
    base.update(kw)
    return base

def test_columns_contract_exact():
    assert ex.COLUMNS == ["Nr faktury", "Ilość", "REF", "Nazwa PL", "Waga netto",
                          "Waga brutto", "Przelicznik jednostki", "Kwota",
                          "Kod celny (CN)", "SENT", "Status dopasowania"]

def test_header_and_row_values():
    wb = ex.build_workbook([_row()])
    ws = wb.active
    header = [c.value for c in ws[1]]
    assert header == ex.COLUMNS
    r2 = [c.value for c in ws[2]]
    assert r2[0] == "FV/1" and r2[2] == "X" and r2[3] == "Rękawice"
    assert r2[9] == "TAK"                      # SENT 1 → TAK
    assert r2[10] == "matched"

def test_skipped_excluded_and_unmatched_blank_master():
    wb = ex.build_workbook([
        _row(skipped=1),
        _row(match_status="unmatched", master_ref="", raw_ref="Q", name_pl="",
             tariff_cn="", sent=0),
    ])
    ws = wb.active
    assert ws.max_row == 2                      # 1 nagłówek + 1 wiersz (skipped pominięty)
    r = [c.value for c in ws[2]]
    assert r[2] == "Q"                          # REF fallback do raw_ref
    assert r[3] == "" and r[8] == ""            # brak nazwy PL i CN
    assert r[9] == "NIE"
