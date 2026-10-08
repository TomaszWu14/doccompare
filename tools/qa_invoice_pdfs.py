# tools/qa_invoice_pdfs.py
"""QA-runner: przepuszcza katalog realnych PDF-ów przez splitter i raportuje
strukturę oraz gotowość pipeline'u (dostawca, profil, pozycje).

Ręczne narzędzie diagnostyczne — wymaga PyMuPDF/pdfplumber; NIE w CI.
Realne PDF-y trzymać POZA repo (dane handlowe). Wycięte pliki _docN_*.pdf
powstają obok źródeł — katalog roboczy, nie oryginalny share.

Użycie: python tools/qa_invoice_pdfs.py <katalog_z_pdf> [--parse]
  --parse: dodatkowo pełna ekstrakcja pozycji (wymaga profili dostawców w DB).
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from constants import INVOICE_LIKE_KINDS             # noqa: E402
from doc_splitter import split_pdf                  # noqa: E402
from packing_list_extractor import first_pages_text  # noqa: E402
from supplier_profiles import detect_supplier, get_supplier  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    folder = sys.argv[1]
    do_parse = "--parse" in sys.argv
    pdfs = sorted(f for f in os.listdir(folder)
                  if f.lower().endswith(".pdf") and not re.search(r"_doc\d+_", f))
    ok = bad = 0
    for fn in pdfs:
        path = os.path.join(folder, fn)
        print(f"\n=== {fn} ===")
        try:
            parts = split_pdf(path)
        except Exception as e:
            print(f"  BŁĄD SPLITTERA: {e}")
            bad += 1
            continue
        ok += 1
        for p in parts:
            text = first_pages_text(p["out_path"], 1)
            sup = detect_supplier(text) if text else None
            code = (sup or {}).get("code", "")
            profile = "profil OK" if (code and get_supplier(code)) else "BRAK PROFILU"
            line = (f"  s.{p['page_from']}-{p['page_to']:<3} {p['kind'].value:<13} "
                    f"dostawca={code or '?':<14} {profile}")
            if do_parse and p["kind"] in INVOICE_LIKE_KINDS and code:
                try:
                    from invoice_extractor import extract_invoice
                    out = extract_invoice(p["out_path"], get_supplier(code))
                    line += f"  pozycji={len(out['items'])} nr={out['invoice_number']}"
                except Exception as e:
                    line += f"  EKSTRAKCJA PADŁA: {e}"
            print(line)
    print(f"\nPliki: {ok} OK, {bad} z błędem splittera.")


if __name__ == "__main__":
    main()
