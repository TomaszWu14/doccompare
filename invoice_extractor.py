"""Cienki wrapper: PDF faktury → surowe pozycje, przez istniejącą kaskadę
table_extractor.parse_pdf z mapowaniem kolumn z profilu dostawcy.
Nic nie wzbogaca master datą (to robi invoice_mapper)."""

from supplier_profiles import get_column_overrides


def _pick_invoice_number(header_fields: dict) -> str:
    for k in ("invoice_no", "invoice_number", "faktura", "nr_faktury", "number"):
        v = (header_fields or {}).get(k)
        if v:
            return str(v).strip()
    return ""


def _parse_pdf(pdf_path: str, col_map):
    # Szew testowalny + import lazy: table_extractor ciągnie pdfplumber (ciężkie), więc
    # trzymamy import w funkcji, by app boot / CI bez tej zależności nie padały (konwencja
    # repo). Testy monkeypatchują tę funkcję, nie importując table_extractor.
    from table_extractor import parse_pdf
    return parse_pdf(pdf_path, supplier_column_map=col_map)


def extract_invoice(pdf_path: str, supplier: dict) -> dict:
    # Faktura handlowa dzieli układ kolumn z proformą (PI) — reużywamy mapę PI dostawcy.
    col_map = get_column_overrides(supplier, "PI") or None
    doc = _parse_pdf(pdf_path, col_map)
    items = []
    for i, raw in enumerate(doc.items or [], start=1):
        net = raw.get("net")
        items.append({
            "line_no": i,
            "raw_ref": (raw.get("ref") or "").strip(),
            "descr": (raw.get("desc") or "").strip(),
            "qty": raw.get("qty") or "",
            "net_amount": net or "",
            "amount": net or raw.get("price") or "",
            "uom_src": raw.get("unit") or "",
            "weight_net": raw.get("weight_net") or "",
            "weight_gross": raw.get("weight_gross") or "",
            "cartons": raw.get("cartons") or "",
        })
    return {
        "items": items,
        "total_net": doc.total_net,
        "total_qty": doc.total_qty,
        "raw_text": doc.raw_text or "",
        "invoice_number": _pick_invoice_number(doc.header_fields),
    }
