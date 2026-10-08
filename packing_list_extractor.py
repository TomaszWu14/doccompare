"""Packing lista → mapa REF→{weight_net, weight_gross}. Klasyfikacja PL po treści.
Import table_extractor lazy (ciągnie pdfplumber) — konwencja repo."""

import material_master as mm
from normalizer import normalize_number


def is_packing_list(header_fields: dict, raw_text: str) -> bool:
    # Delegacja do jedynego źródła prawdy klasyfikacji stron (doc_splitter) —
    # zamiast dwóch osobnych list markerów, które łatwo rozjeżdżają się w czasie.
    from doc_splitter import classify_page
    from constants import DocKind
    return classify_page(raw_text) == DocKind.PACKING_LIST


def first_pages_text(pdf_path: str, n: int = 2) -> str:
    """Lekki sniff tekstu pierwszych n stron (klasyfikacja PL / detekcja dostawcy —
    nie pełna ekstrakcja). pdfplumber lazy: CI bez niego nie ładuje go przy imporcie.
    Jedno miejsce z bezpośrednim wywołaniem pdfplumber (reszta idzie przez kaskadę)."""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            return " ".join((pg.extract_text() or "") for pg in pdf.pages[:n])
    except Exception:
        return ""


def _parse_pdf(pdf_path: str):
    # Szew testowalny + lazy import (pdfplumber ciężki, CI bez niego).
    from table_extractor import parse_pdf
    return parse_pdf(pdf_path)


def _add(acc, key, field, raw):
    v = normalize_number(str(raw)) if raw not in (None, "") else None
    if v is None:
        return
    # Decimal end-to-end (normalize_number zwraca Decimal) — bez dryfu float przy sumowaniu
    from decimal import Decimal
    prev = acc[key].get(field)
    base = Decimal(str(prev)) if prev not in (None, "") else Decimal("0")
    acc[key][field] = str(base + Decimal(str(v)))


def extract_weight_map(pdf_path: str) -> dict:
    doc = _parse_pdf(pdf_path)
    out = {}
    for it in (doc.items or []):
        ref = mm.normalize_ref(it.get("ref"))
        if not ref:
            continue
        wn, wg = it.get("weight_net"), it.get("weight_gross")
        ct = it.get("cartons")
        if (wn in (None, "")) and (wg in (None, "")) and (ct in (None, "")):
            continue
        out.setdefault(ref, {})
        _add(out, ref, "weight_net", wn)
        _add(out, ref, "weight_gross", wg)
        _add(out, ref, "cartons", ct)
    # usuń wpisy które mimo wszystko puste
    return {k: v for k, v in out.items() if v}
