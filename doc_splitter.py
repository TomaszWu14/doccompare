"""Tnie PDF-zestaw (CIPL / komplet kontenerowy) na osobne dokumenty po klasyfikacji
stron. Klasyfikacja po tekście strony; PyMuPDF importowany lazy (konwencja repo)."""

import os
import re
from contextlib import contextmanager

from constants import DocKind

# Kolejność ma znaczenie: strona packing listy zawiera słowo "invoice" w polach
# nagłówka ("No. & date of invoice"), więc PACKING LIST sprawdzamy przed fakturami.
# Literówki z realnych dokumentów: PROFOMA (Kestrel), PORFORMA (Tidewell).
_MARKERS = [
    (DocKind.PACKING_LIST, ("PACKING LIST", "PACKING NOTE", "LISTA PAKOWANIA",
                            "SPECYFIKACJA PAKOWANIA")),
    (DocKind.PROFORMA, ("PROFORMA INVOICE", "PROFOMA INVOICE", "PORFORMA INVOICE")),
    (DocKind.INVOICE, ("COMMERCIAL INVOICE",)),
    (DocKind.OTHER, ("BILL OF LADING", "OCEAN BILL", "SEA WAYBILL")),
]


def classify_page(text: str):
    """DocKind strony albo None (kontynuacja — tekst bez nagłówka typu).
    Strona bez sensownego tekstu (skan) → DocKind.OTHER."""
    t = re.sub(r"\s+", " ", (text or "")).upper().strip()
    for kind, markers in _MARKERS:
        if any(m in t for m in markers):
            return kind
    if len(t) < 30:
        return DocKind.OTHER
    return None


@contextmanager
def _open_fitz(path: str):
    # Szew testowalny + lazy import PyMuPDF, wspólny dla _page_texts/_write_range.
    import fitz
    doc = fitz.open(path)
    try:
        yield doc
    finally:
        doc.close()


def _page_texts(pdf_path: str) -> list:
    with _open_fitz(pdf_path) as doc:
        return [(pg.get_text() or "") for pg in doc]


def _write_range(pdf_path: str, page_from: int, page_to: int, out_path: str) -> None:
    import fitz
    with _open_fitz(pdf_path) as src:
        out = fitz.open()
        try:
            out.insert_pdf(src, from_page=page_from - 1, to_page=page_to - 1)
            out.save(out_path)
        finally:
            out.close()


def split_pdf(pdf_path: str) -> list:
    """[{kind, page_from, page_to, out_path}] — strony 1-indeksowane.
    Jednodokumentowy PDF → jedna część wskazująca ORYGINALNY plik (bez kopii)."""
    texts = _page_texts(pdf_path)
    parts = []
    for i, text in enumerate(texts, start=1):
        kind = classify_page(text)
        if kind is None and parts:
            parts[-1]["page_to"] = i            # kontynuacja poprzedniego dokumentu
            continue
        parts.append({"kind": kind or DocKind.OTHER, "page_from": i, "page_to": i})
    if len(parts) <= 1:
        if parts:
            parts[0]["out_path"] = pdf_path
        return parts
    base, _ = os.path.splitext(pdf_path)
    for n, p in enumerate(parts, start=1):
        p["out_path"] = f"{base}_doc{n}_{p['kind'].value}.pdf"
        _write_range(pdf_path, p["page_from"], p["page_to"], p["out_path"])
    return parts
