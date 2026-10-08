# tools/pilot_ml/pilot_dit.py
"""Pilot DiT: ocena microsoft/dit-base-finetuned-rvlcdip jako klasyfikatora STRON-SKANÓW,
których doc_splitter nie umie sklasyfikować z tekstu (brak warstwy tekstowej).

Dla każdego źródłowego PDF-a (pomija wycięte _docN_*) i każdej strony:
  - klasyfikacja TEKSTOWA: doc_splitter.classify_page(page.get_text())  (None = kontynuacja),
  - klasyfikacja OBRAZOWA: DiT top-1/top-3 na renderze strony (150 DPI),
  - znacznik zgodności (invoice/proforma ↔ etykieta RVL-CDIP "invoice").
Na końcu: zgodność % dla stron o znanym typie tekstowym, rozkład etykiet DiT dla
packing_list, oraz WYRÓŻNIONE odpowiedzi DiT dla stron BEZ warstwy tekstowej
(<30 znaków — właściwy cel pilota, np. skan s.2 zestawu ABCU) + czasy (load, per strona).

Ręczne narzędzie diagnostyczne — NIE w CI. Realne PDF-y trzymać POZA repo.
Użycie: python tools/pilot_ml/pilot_dit.py [katalog_z_pdf]   (domyślnie C:/Users/tomas/qa-cipl)
Wymaga zależności pilota: pip install -r requirements-pilot.txt
"""
import io
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from constants import DocKind          # noqa: E402
from doc_splitter import classify_page  # noqa: E402

MODEL = "microsoft/dit-base-finetuned-rvlcdip"
NO_TEXT_THRESHOLD = 30  # znaków — poniżej traktujemy stronę jako skan bez warstwy tekstowej
INVOICE_LIKE = (DocKind.INVOICE, DocKind.PROFORMA)


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else "C:/Users/tomas/qa-cipl"
    if not os.path.isdir(folder):
        print(f"Brak katalogu: {folder}")
        sys.exit(1)

    import fitz  # PyMuPDF — jest w requirements repo
    from PIL import Image

    try:
        from transformers import pipeline
    except ImportError:
        print("Brak zależności pilota (transformers/torch).")
        print("Zainstaluj: pip install -r requirements-pilot.txt")
        sys.exit(1)

    t0 = time.perf_counter()
    clf = pipeline("image-classification", model=MODEL)
    load_s = time.perf_counter() - t0
    print(f"Model {MODEL} załadowany w {load_s:.1f}s\n")

    pdfs = sorted(f for f in os.listdir(folder)
                  if f.lower().endswith(".pdf") and not re.search(r"_doc\d+_", f))

    # zbiorcze liczniki
    invoice_like_total = invoice_like_agree = 0
    pl_labels = {}          # etykiety DiT dla stron packing_list
    no_text_pages = []      # (plik, strona, top1, score)
    infer_times = []

    for fn in pdfs:
        path = os.path.join(folder, fn)
        print(f"=== {fn} ===")
        doc = fitz.open(path)
        for i, page in enumerate(doc, start=1):
            text = page.get_text()
            kind = classify_page(text)
            no_text = len(text.strip()) < NO_TEXT_THRESHOLD

            png = page.get_pixmap(dpi=150).tobytes("png")
            img = Image.open(io.BytesIO(png)).convert("RGB")
            t1 = time.perf_counter()
            preds = clf(img, top_k=3)
            infer_times.append(time.perf_counter() - t1)

            top1 = preds[0]
            top3 = ", ".join(f"{p['label']}:{p['score']:.2f}" for p in preds)

            if kind in INVOICE_LIKE:
                invoice_like_total += 1
                agree = top1["label"] == "invoice"
                invoice_like_agree += agree
                marker = "ZGODA" if agree else "ROZJAZD"
            elif kind == DocKind.PACKING_LIST:
                pl_labels[top1["label"]] = pl_labels.get(top1["label"], 0) + 1
                marker = "PL→?"
            elif kind is None:
                marker = "cont."
            else:
                marker = "-"

            kind_s = kind.value if kind else ("SKAN-BEZ-TEKSTU" if no_text else "CONT")
            print(f"  s.{i:<3} tekst={kind_s:<16} DiT={top1['label']}"
                  f" ({top1['score']:.2f})  [{top3}]  {marker}")
            if no_text:
                no_text_pages.append((fn, i, top1["label"], top1["score"]))
        doc.close()
        print()

    print("=" * 60)
    print("PODSUMOWANIE")
    if invoice_like_total:
        pct = 100.0 * invoice_like_agree / invoice_like_total
        print(f"invoice/proforma → DiT 'invoice': {invoice_like_agree}/{invoice_like_total} ({pct:.0f}%)")
    if pl_labels:
        dist = ", ".join(f"{k}={v}" for k, v in sorted(pl_labels.items(), key=lambda x: -x[1]))
        print(f"packing_list → etykiety DiT: {dist}")

    print(f"\nSTRONY BEZ WARSTWY TEKSTOWEJ (<{NO_TEXT_THRESHOLD} zn.) — właściwy cel pilota:")
    if no_text_pages:
        for fn, i, label, score in no_text_pages:
            print(f"  >>> {fn} s.{i}: DiT = {label} ({score:.2f})")
    else:
        print("  (brak — wszystkie strony miały warstwę tekstową)")

    if infer_times:
        avg = sum(infer_times) / len(infer_times)
        print(f"\nCzasy: load modelu {load_s:.1f}s, inferencja śr. {avg:.2f}s/strona"
              f" (min {min(infer_times):.2f}, max {max(infer_times):.2f}, stron {len(infer_times)})")


if __name__ == "__main__":
    main()
