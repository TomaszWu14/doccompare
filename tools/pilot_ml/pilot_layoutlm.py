# tools/pilot_ml/pilot_layoutlm.py
"""Pilot ML: document-QA (LayoutLM, model impira/layoutlm-invoices) vs obecne regexy
na polach nagłówka faktury (numer faktury, kontener, warunki dostawy, kwota total).

Przepływ: dla każdego źródłowego PDF-a (pomija wycięte pliki _docN_*) uruchamia
doc_splitter.split_pdf(), a dla części typu invoice/proforma renderuje PIERWSZĄ
stronę części do obrazu (PyMuPDF, dpi=200) i zadaje pipeline'owi
"document-question-answering" cztery pytania po angielsku (dokumenty są angielskie).
Obok odpowiedzi DQA drukuje wynik obecnych regexów (invoice_pipeline._header_meta:
kontener + warunki dostawy). Dla numeru faktury i kwoty total baseline'u regexowego
tu nie ma — ocena ręczna (eyeball) na podstawie wydruku.

Ręczne narzędzie diagnostyczne — NIE w CI, nie dotyka bazy, tylko CZYTA PDF-y
i pisze na stdout. Realne PDF-y trzymać POZA repo (dane handlowe).

Użycie: python tools/pilot_ml/pilot_layoutlm.py [katalog_z_pdf]
  domyślny katalog: C:/Users/tomas/qa-cipl

Zależności pilota (poza requirements repo): transformers + torch + pytesseract
  pip install -r requirements-pilot.txt
oraz systemowy tesseract-ocr (word-boxy dla LayoutLM).
"""
import io
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DEFAULT_FOLDER = "C:/Users/tomas/qa-cipl"
INSTALL_HINT = "Zainstaluj zależności pilota: pip install -r requirements-pilot.txt"

# (etykieta pola, pytanie DQA, klucz baseline'u regexowego lub None)
QUESTIONS = [
    ("invoice_number", "What is the invoice number?", None),
    ("container_no", "What is the container number?", "container_no"),
    ("delivery_terms", "What are the terms of delivery?", "delivery_terms"),
    ("total_amount", "What is the total amount?", None),
]

SCORE_OK = 0.5  # próg "DQA pewne"


def _load_dqa():
    """Ładuje pipeline DQA raz; brak zależności = czytelne wyjście, nie traceback."""
    try:
        from transformers import pipeline as hf_pipeline
    except ImportError:
        print("Brak biblioteki 'transformers' (pilot LayoutLM). " + INSTALL_HINT)
        sys.exit(2)
    try:
        import pytesseract  # noqa: F401 — DQA wymaga word-boxów z Tesseracta
    except ImportError:
        print("Brak biblioteki 'pytesseract' (word-boxy dla LayoutLM). " + INSTALL_HINT)
        sys.exit(2)
    print("Ładowanie pipeline'u DQA (impira/layoutlm-invoices)...")
    t0 = time.perf_counter()
    try:
        dqa = hf_pipeline("document-question-answering", model="impira/layoutlm-invoices")
    except ImportError as e:
        print(f"Niekompletne zależności pilota ({e}). " + INSTALL_HINT)
        sys.exit(2)
    print(f"  załadowano w {time.perf_counter() - t0:.1f}s")
    return dqa


def _first_page_image(pdf_path: str):
    """Pierwsza strona wyciętej części jako obraz PIL (dpi=200)."""
    import fitz
    from PIL import Image

    doc = fitz.open(pdf_path)
    try:
        pix = doc[0].get_pixmap(dpi=200)
        mode = "RGBA" if pix.alpha else "RGB"
        img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
        return img.convert("RGB")
    finally:
        doc.close()


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).upper()


def _classify(field: str, answer: str, score: float, regex_val) -> str:
    """Kategoria porównania DQA vs regex dla podsumowania.

    regex_val is None = pole bez baseline'u regexowego (ocena ręczna).
    """
    if regex_val is None:
        return "manual" if score >= SCORE_OK else "low_score"
    a, r = _norm(answer), _norm(regex_val)
    if a and r and (a == r or a in r or r in a):
        return "agree"
    if score >= SCORE_OK and not r:
        return "dqa_wins"
    return "dqa_wrong_or_low"


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FOLDER
    if not os.path.isdir(folder):
        print(f"Katalog nie istnieje: {folder}")
        sys.exit(1)

    from constants import DocKind                        # noqa: E402
    from doc_splitter import split_pdf                   # noqa: E402
    from invoice_pipeline import _header_meta            # noqa: E402
    from packing_list_extractor import first_pages_text  # noqa: E402

    invoice_like = (DocKind.INVOICE, DocKind.PROFORMA)

    pdfs = sorted(f for f in os.listdir(folder)
                  if f.lower().endswith(".pdf") and not re.search(r"_doc\d+_", f))
    if not pdfs:
        print(f"Brak źródłowych PDF-ów w {folder}")
        sys.exit(1)

    dqa = _load_dqa()

    results = []   # (part_label, field, answer, score, regex_val, seconds, category)
    timings = []

    for fn in pdfs:
        path = os.path.join(folder, fn)
        print(f"\n=== {fn} ===")
        try:
            parts = split_pdf(path)
        except Exception as e:
            print(f"  BŁĄD SPLITTERA: {e}")
            continue
        inv_parts = [p for p in parts if p["kind"] in invoice_like]
        if not inv_parts:
            print("  (brak części invoice/proforma)")
            continue

        for p in inv_parts:
            part_label = f"{fn} s.{p['page_from']}-{p['page_to']} [{p['kind'].value}]"
            print(f"\n--- {part_label} ---")
            try:
                img = _first_page_image(p["out_path"])
            except Exception as e:
                print(f"  BŁĄD RENDEROWANIA: {e}")
                continue

            raw = first_pages_text(p["out_path"], 2)
            meta = _header_meta(raw)

            print(f"  {'pole':<16} | {'DQA (score)':<46} | regex/obecnie")
            print(f"  {'-' * 16}-+-{'-' * 46}-+-{'-' * 30}")
            for field, question, meta_key in QUESTIONS:
                t0 = time.perf_counter()
                try:
                    out = dqa(image=img, question=question)
                except Exception as e:
                    if type(e).__name__ == "TesseractNotFoundError":
                        print("\nBrak systemowego tesseract-ocr (binarka nie znaleziona"
                              " w PATH) — DQA nie dostanie word-boxów. Zainstaluj"
                              " tesseract-ocr i spróbuj ponownie.")
                        sys.exit(2)
                    print(f"  {field:<16} | BŁĄD DQA: {e}")
                    continue
                dt = time.perf_counter() - t0
                timings.append(dt)
                best = out[0] if isinstance(out, list) and out else (out or {})
                answer = str(best.get("answer", "")).strip()
                score = float(best.get("score", 0.0))
                regex_val = meta.get(meta_key) if meta_key else None
                baseline = regex_val if meta_key else "(ocena ręczna)"
                cat = _classify(field, answer, score, regex_val)
                dqa_cell = f"{answer or '—'} ({score:.3f})"
                print(f"  {field:<16} | {dqa_cell:<46} | {baseline or '—'}"
                      f"   [{dt:.2f}s]")
                results.append((part_label, field, answer, score, regex_val, dt, cat))

    if not results:
        print("\nBrak wyników — żadna część invoice/proforma nie przeszła DQA.")
        sys.exit(1)

    by_cat = {}
    for r in results:
        by_cat.setdefault(r[6], []).append(r)

    print("\n" + "=" * 70)
    print("PODSUMOWANIE (DQA vs regex)")
    print("=" * 70)
    print(f"Odpowiedzi razem: {len(results)}; średni czas inferencji/pytanie: "
          f"{sum(timings) / len(timings):.2f}s (min {min(timings):.2f}s, "
          f"max {max(timings):.2f}s)")

    def _section(cat, title):
        rows = by_cat.get(cat, [])
        print(f"\n{title}: {len(rows)}")
        for part_label, field, answer, score, regex_val, _dt, _c in rows:
            extra = f" | regex='{regex_val}'" if regex_val is not None else ""
            print(f"  - {part_label} :: {field} = '{answer}' ({score:.3f}){extra}")

    _section("dqa_wins", "DQA pewne (score>=0.5), regex pusty — kandydat na wygraną DQA")
    _section("agree", "Zgoda DQA i regexa (pola z baseline'em)")
    _section("dqa_wrong_or_low", "DQA błędne lub niepewne vs regex")
    _section("manual", "Bez baseline'u regexowego, DQA pewne — do oceny ręcznej "
                       "(numer faktury / total)")
    _section("low_score", "Bez baseline'u, DQA niepewne (score<0.5)")


if __name__ == "__main__":
    main()
