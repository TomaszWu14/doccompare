# tools/pilot_ml/pilot_docling.py
"""Pilot ML: IBM Docling vs obecna kaskada (table_extractor.parse_pdf) na realnych CIPL.

Dla każdego źródłowego PDF-a w katalogu (pomijając wycięte _docN_*):
tnie zestaw splitterem (doc_splitter.split_pdf), a dla każdej części typu
invoice/proforma/packing_list uruchamia:
  a) obecną kaskadę  -> liczba pozycji + ile ma niepusty ref,
  b) Docling         -> liczba tabel + max wierszy największej tabeli (bez nagłówka).
Mierzy czas obu ekstraktorów (perf_counter) — koszt decyduje o miejscu w kaskadzie.
Wynik: tabela per część + podsumowanie (gdzie Docling wygrywa / remis / nic nie znalazł).

Ręczne narzędzie diagnostyczne — NIE w CI, NIE na prodzie.
Wymaga zależności pilotażowych: pip install -r requirements-pilot.txt
Realne PDF-y trzymać POZA repo (dane handlowe) — skrypt tylko czyta i drukuje na stdout.

Użycie: python tools/pilot_ml/pilot_docling.py [katalog_z_pdf]
  domyślny katalog: C:/Users/tomas/qa-cipl
"""
import io
import os
import re
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# bootstrap: skrypt leży w tools/pilot_ml/ -> repo root dwa poziomy wyżej
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from constants import DocKind          # noqa: E402
from doc_splitter import split_pdf     # noqa: E402

DEFAULT_FOLDER = "C:/Users/tomas/qa-cipl"
# Nie polegamy na constants.INVOICE_LIKE_KINDS (może jeszcze nie istnieć w tej gałęzi).
PILOT_KINDS = (DocKind.INVOICE, DocKind.PROFORMA, DocKind.PACKING_LIST)


def _docling_table_rows(table):
    """Liczba wierszy DANYCH tabeli Docling (bez nagłówka) — defensywnie.

    API Doclinga bywa różne między wersjami, więc: najpierw export_to_dataframe()
    (nagłówek ląduje w kolumnach df, len(df) = wiersze danych), potem fallback na
    table.data.num_rows / table.data.grid (tam nagłówek jest wierszem -> minus 1).
    Zwraca int albo None gdy kształt API nierozpoznany (wtedy drukuje co dostał).
    """
    try:
        df = table.export_to_dataframe()
        return len(df)
    except Exception as e:  # brak pandas / inna wersja API — próbujemy niżej
        print(f"      [docling] export_to_dataframe() padł: {type(e).__name__}: {e}")

    data = getattr(table, "data", None)
    if data is not None:
        num_rows = getattr(data, "num_rows", None)
        if isinstance(num_rows, int):
            return max(num_rows - 1, 0)
        grid = getattr(data, "grid", None)
        if grid is not None:
            try:
                return max(len(grid) - 1, 0)
            except TypeError:
                pass
    print(f"      [docling] nieznany kształt tabeli: {type(table).__name__}, "
          f"atrybuty: {[a for a in dir(table) if not a.startswith('_')][:15]}")
    return None


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FOLDER
    if not os.path.isdir(folder):
        print(f"Katalog nie istnieje: {folder}")
        sys.exit(1)

    # Zależność pilotażowa — import w miejscu użycia, z podpowiedzią instalacji.
    try:
        import docling
        from docling.document_converter import DocumentConverter
    except ImportError:
        print("Brak zależności pilotażowych (docling).")
        print("Zainstaluj: pip install -r requirements-pilot.txt")
        sys.exit(1)
    print(f"docling {getattr(docling, '__version__', '(wersja nieznana)')}")

    # Ciężkie importy repo (pdfplumber itd.) dopiero tutaj.
    from table_extractor import parse_pdf

    conv = DocumentConverter()  # jeden konwerter na cały bieg (ładowanie modeli jest drogie)

    pdfs = sorted(f for f in os.listdir(folder)
                  if f.lower().endswith(".pdf") and not re.search(r"_doc\d+_", f))
    if not pdfs:
        print(f"Brak źródłowych PDF-ów w {folder}")
        sys.exit(1)

    results = []  # dict per część
    hdr = (f"{'część':<42} {'typ':<13} {'kask_poz':>8} {'z_ref':>6} {'t_kask':>7} "
           f"{'dl_tab':>6} {'dl_max':>6} {'t_dl':>7}")

    for fn in pdfs:
        path = os.path.join(folder, fn)
        print(f"\n=== {fn} ===")
        try:
            parts = split_pdf(path)
        except Exception as e:
            print(f"  BŁĄD SPLITTERA: {type(e).__name__}: {e}")
            continue
        print(hdr)
        for p in parts:
            if p["kind"] not in PILOT_KINDS:
                continue
            part_path = p["out_path"]
            label = f"{os.path.basename(part_path)[:38]} s.{p['page_from']}-{p['page_to']}"

            # a) obecna kaskada
            kaskada_items = kaskada_with_ref = -1
            t0 = time.perf_counter()
            try:
                doc = parse_pdf(part_path)
                kaskada_items = len(doc.items)
                kaskada_with_ref = sum(1 for it in doc.items if (it.get("ref") or "").strip())
            except Exception as e:
                print(f"      [kaskada] padła: {type(e).__name__}: {e}")
            t_kask = time.perf_counter() - t0

            # b) Docling na tym samym pliku części
            docling_tables = -1
            docling_max_rows = 0
            t0 = time.perf_counter()
            try:
                res = conv.convert(part_path)
                tables = getattr(res.document, "tables", None)
                if tables is None:
                    print(f"      [docling] document bez .tables — atrybuty: "
                          f"{[a for a in dir(res.document) if not a.startswith('_')][:15]}")
                    tables = []
                docling_tables = len(tables)
                for tab in tables:
                    rows = _docling_table_rows(tab)
                    if rows is not None and rows > docling_max_rows:
                        docling_max_rows = rows
            except Exception as e:
                print(f"      [docling] convert padł: {type(e).__name__}: {e}")
            t_dl = time.perf_counter() - t0

            print(f"{label:<42} {p['kind'].value:<13} {kaskada_items:>8} {kaskada_with_ref:>6} "
                  f"{t_kask:>6.1f}s {docling_tables:>6} {docling_max_rows:>6} {t_dl:>6.1f}s")
            results.append({
                "part": label, "kind": p["kind"].value,
                "kaskada_items": kaskada_items, "kaskada_with_ref": kaskada_with_ref,
                "docling_tables": docling_tables, "docling_max_rows": docling_max_rows,
                "t_kaskada": t_kask, "t_docling": t_dl,
            })

    # Podsumowanie
    print(f"\n=== PODSUMOWANIE ({len(results)} części) ===")
    wins = [r for r in results if r["docling_tables"] > 0
            and r["docling_max_rows"] > max(r["kaskada_items"], 0)]
    equal = [r for r in results if r["docling_tables"] > 0
             and r["docling_max_rows"] == max(r["kaskada_items"], 0)]
    nothing = [r for r in results if r["docling_tables"] <= 0]
    worse = [r for r in results if r not in wins and r not in equal and r not in nothing]

    print(f"Docling znalazł WIĘKSZĄ tabelę niż kaskada pozycji (kandydat na wygraną): {len(wins)}")
    for r in wins:
        print(f"  + {r['part']} ({r['kind']}): docling {r['docling_max_rows']} wierszy "
              f"vs kaskada {r['kaskada_items']} pozycji")
    print(f"Remis (docling max wierszy == kaskada pozycji): {len(equal)}")
    for r in equal:
        print(f"  = {r['part']} ({r['kind']}): {r['docling_max_rows']}")
    print(f"Docling nie znalazł żadnej tabeli (lub padł): {len(nothing)}")
    for r in nothing:
        print(f"  - {r['part']} ({r['kind']})")
    if worse:
        print(f"Docling znalazł mniej niż kaskada: {len(worse)}")
        for r in worse:
            print(f"  < {r['part']} ({r['kind']}): docling {r['docling_max_rows']} "
                  f"vs kaskada {r['kaskada_items']}")

    if results:
        sk = sum(r["t_kaskada"] for r in results)
        sd = sum(r["t_docling"] for r in results)
        print(f"\nCzas łącznie: kaskada {sk:.1f}s ({sk / len(results):.1f}s/część), "
              f"docling {sd:.1f}s ({sd / len(results):.1f}s/część)")


if __name__ == "__main__":
    main()
