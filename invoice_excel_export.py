"""Buduje Excel wg stałego kontraktu kolumn z zatwierdzonych pozycji faktur.
openpyxl importowany lazy (nie psuje compileall/boot bez zależności)."""

COLUMNS = ["Nr faktury", "Ilość", "REF", "Nazwa PL", "Waga netto", "Waga brutto",
           "Przelicznik jednostki", "Kwota", "Kod celny (CN)", "SENT",
           "Status dopasowania"]


def _row_values(r: dict) -> list:
    ref = (r.get("master_ref") or r.get("raw_ref") or "")
    return [
        r.get("invoice_number") or "",
        r.get("qty") or "",
        ref,
        r.get("name_pl") or "",
        r.get("weight_net") or "",
        r.get("weight_gross") or "",
        r.get("uom_factor") or "",
        r.get("amount") or "",
        r.get("tariff_cn") or "",
        "TAK" if int(r.get("sent") or 0) else "NIE",
        r.get("match_status") or "",
    ]


def build_workbook(rows: list):
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Faktury"
    ws.append(COLUMNS)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        if int(r.get("skipped") or 0):
            continue
        ws.append(_row_values(r))
    return wb
