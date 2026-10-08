"""Orkiestracja jednej faktury: gate profilu (obowiązkowy) → ekstrakcja → mapowanie →
zapis pozycji; oraz walidacja zatwierdzenia (ambiguous blokuje, unmatched nie)."""

import re

import invoice_jobs as ij
from constants import InvoiceJobStatus, MatchStatus
from invoice_extractor import extract_invoice
from invoice_mapper import map_items
from packing_list_extractor import extract_weight_map, first_pages_text
from supplier_profiles import get_supplier, detect_supplier


def _resolve_supplier_code(db, job) -> str:
    """Zwraca kod dostawcy joba; gdy pusty, próbuje lekkiej detekcji z 1. strony PDF."""
    code = (job.get("supplier_code") or "").strip()
    if code:
        return code
    text = first_pages_text(job["pdf_path"], 1)
    sup = detect_supplier(text) if text else None
    return (sup or {}).get("code", "") if sup else ""


_CONTAINER_RE = re.compile(r"CONTAINER\s*NO\.?\s*[:：]?\s*([A-Z]{4}\s?\d{7})", re.IGNORECASE)
_TERMS_RE = re.compile(r"TERMS\s+OF\s+DELIVERY\s*[:：]?\s*([A-Z]{3}[^\n\r]{0,40})",
                       re.IGNORECASE)

_INCOTERMS = ("EXW", "FCA", "FAS", "FOB", "CFR", "CIF", "CPT", "CIP",
              "DAP", "DPU", "DDP", "DAT", "DAF", "DES", "DEQ", "DDU")


def _header_meta(raw_text: str) -> dict:
    """Kontener i warunki dostawy z tekstu nagłówka faktury (dla draftu SAD).
    Warunki tylko gdy zaczynają się znanym Incotermem — regex bez tej straży
    łapał przypadkowe linie, gdy wartość nie stoi tuż za etykietą."""
    t = raw_text or ""
    m = _CONTAINER_RE.search(t)
    d = _TERMS_RE.search(t)
    terms = d.group(1).strip() if d else ""
    if terms[:3].upper() not in _INCOTERMS:
        terms = ""
    return {"container_no": m.group(1).replace(" ", "").upper() if m else "",
            "delivery_terms": terms}


def _weight_map_for(db, batch_id: str, invoice_number: str) -> dict:
    """Mapa wag/kartonów z packing list batcha: najpierw PL, których tekst zawiera
    numer TEJ faktury; żadna nie pasuje → unia wszystkich PL batcha (fallback)."""
    pls = ij.list_packing_lists(db, batch_id)
    if not pls:
        return {}
    matched = []
    if invoice_number:
        for pl in pls:
            if invoice_number in first_pages_text(pl["pdf_path"], 2):
                matched.append(pl)
    out = {}
    for pl in (matched or pls):
        out.update(extract_weight_map(pl["pdf_path"]))
    return out


def process_job(db, job_id: int) -> str:
    job = ij.get_job(db, job_id)
    if not job:
        return InvoiceJobStatus.ERROR
    code = _resolve_supplier_code(db, job)
    if code and code != job.get("supplier_code"):
        ij.update_job(db, job_id, supplier_code=code)
    supplier = get_supplier(code) if code else None
    if not supplier:
        ij.update_job(db, job_id, status=InvoiceJobStatus.ERROR,
                      error=f"Brak profilu dostawcy: {job.get('supplier_code') or '—'}")
        return InvoiceJobStatus.ERROR
    try:
        extracted = extract_invoice(job["pdf_path"], supplier)
    except Exception as e:                       # ekstrakcja padła (zły skan itd.)
        ij.update_job(db, job_id, status=InvoiceJobStatus.ERROR, error=f"Nie udało się odczytać: {e}")
        return InvoiceJobStatus.ERROR
    if not extracted["items"]:
        ij.update_job(db, job_id, status=InvoiceJobStatus.ERROR,
                      error="Nie udało się odczytać tabeli pozycji")
        return InvoiceJobStatus.ERROR
    meta = _header_meta(extracted.get("raw_text") or "")
    try:
        weight_map = _weight_map_for(db, job.get("batch_id") or "",
                                     extracted.get("invoice_number") or "")
    except Exception:
        weight_map = {}   # waga to dane pomocnicze — błąd nie wywraca faktury
    mapped = map_items(db, extracted["items"], weight_map=weight_map)
    ij.save_items(db, job_id, mapped)
    ij.update_job(db, job_id, status=InvoiceJobStatus.EXTRACTED,
                  invoice_number=extracted.get("invoice_number") or "",
                  container_no=meta["container_no"],
                  delivery_terms=meta["delivery_terms"])
    return InvoiceJobStatus.EXTRACTED


def confirm_job(db, job_id: int) -> tuple:
    # Zatwierdzić można tylko fakturę po udanej ekstrakcji — nie błędną/pustą.
    job = ij.get_job(db, job_id)
    if not job or job.get("status") not in (InvoiceJobStatus.EXTRACTED, InvoiceJobStatus.CONFIRMED):
        return (False, "Faktura nie jest gotowa do zatwierdzenia (brak udanej ekstrakcji)")
    items = ij.get_items(db, job_id)
    active = [it for it in items if not int(it.get("skipped") or 0)]
    if not active:
        return (False, "Brak pozycji do zatwierdzenia")
    for it in active:
        if it.get("match_status") == MatchStatus.AMBIGUOUS:
            return (False, "Rozstrzygnij niejednoznaczne pozycje (X/X1) przed zatwierdzeniem")
    ij.update_job(db, job_id, status=InvoiceJobStatus.CONFIRMED)
    return (True, "")
