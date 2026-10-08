"""
scan_extractor.py — ekstrakcja danych dostaw ze skanów dokumentów magazynowych.

Obsługuje:
  - PDF wielostronicowy (każda strona → osobna ekstrakcja)
  - Obrazy JPG/PNG/TIFF
  - Dokumenty z pismem ręcznym i nadrukiem (Claude Vision)
  - Wynik: lista wierszy gotowych do eksportu SAP (Excel)
"""

import base64
import io
import json
import os
import re
import urllib.request
import urllib.error   # potrzebne dla except urllib.error.HTTPError (osobny submoduł)
from typing import Optional

try:
    import fitz          # PyMuPDF
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# ─── POLA WYNIKU ──────────────────────────────────────────────────────────────

EMPTY_ROW = {
    "plik":            "",
    "strona":          "",
    "typ_dok":         "",   # WZ / CMR / PZ / faktura / inne
    "nr_dok":          "",   # numer dokumentu dostawy
    "dostawca":        "",
    "data":            "",
    "ref":             "",   # indeks / nr katalogowy / SAP material
    "opis":            "",   # nazwa produktu
    "lot":             "",   # nr partii (SAP CHARG)
    "jm":              "",   # jednostka miary
    "ilosc_zam":       "",   # ilość zamówiona / na dok.
    "ilosc_dost":      "",   # ilość faktycznie dostarczona / odebrana
    "rozbiez":         "",   # rozbieżność (ręcznie wpisana lub obliczona)
    "uwagi":           "",   # uwagi ręczne, adnotacje magazynu
}

COLUMNS_PL = {
    "plik":       "Plik",
    "strona":     "Strona",
    "typ_dok":    "Typ dok.",
    "nr_dok":     "Nr dokumentu",
    "dostawca":   "Dostawca",
    "data":       "Data",
    "ref":        "REF / Indeks SAP",
    "opis":       "Opis produktu",
    "lot":        "LOT / Partia",
    "jm":         "J.m.",
    "ilosc_zam":  "Ilość na dok.",
    "ilosc_dost": "Ilość odebrana",
    "rozbiez":    "Rozbieżność",
    "uwagi":      "Uwagi",
}


# ─── CLAUDE VISION PROMPT ─────────────────────────────────────────────────────

_SYSTEM = """Jesteś systemem ekstrakcji danych z dokumentów magazynowych wyrobów medycznych.
Wyciągasz dane z dokumentów dostaw: WZ, CMR, PZ, faktur, listów przewozowych.
Dokumenty mogą zawierać zarówno tekst nadrukowany jak i adnotacje ręczne.
Adnotacje ręczne (np. numer LOT, ilość rzeczywiście odebrana, rozbieżności) są WAŻNE — koniecznie je wyciągnij.
Odpowiadaj WYŁĄCZNIE czystym JSON, bez markdown, bez komentarzy."""

_PROMPT = """Wyciągnij wszystkie pozycje z tego dokumentu dostawy.

Zwróć JSON w postaci:
{
  "typ_dok": "WZ|CMR|PZ|faktura|list_przewozowy|inne",
  "nr_dok": "numer dokumentu",
  "dostawca": "nazwa dostawcy",
  "data": "data dokumentu w formacie RRRR-MM-DD lub jak na dokumencie",
  "pozycje": [
    {
      "ref": "indeks / nr katalogowy / SAP material number",
      "opis": "nazwa / opis produktu",
      "lot": "numer LOT / partii (ręczny lub nadrukowany)",
      "jm": "szt|op|krt|kg|m|l|inne",
      "ilosc_zam": "ilość na dokumencie / zamówiona (liczba)",
      "ilosc_dost": "ilość faktycznie odebrana (z adnotacji ręcznej lub z dok.)",
      "rozbiez": "rozbieżność jeśli zaznaczona (np. -5, brak, OK)",
      "uwagi": "ręczne adnotacje, pieczątki, komentarze magazynu"
    }
  ]
}

Zasady:
- Jeśli pole nie istnieje na dokumencie → zostaw ""
- Ręczne dopiski traktuj jako wartość pola ilosc_dost lub lot
- Jeśli brak rozbieżności → rozbiez = ""
- Wyciągnij WSZYSTKIE pozycje z dokumentu, nawet jeśli jest ich wiele
- ilosc_zam i ilosc_dost to liczby (może być decimal, np. 12.5)"""


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _get_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    try:
        from db import get_db
        db = get_db()
        try:
            row = db.execute("SELECT value FROM settings WHERE key='anthropic_api_key'").fetchone()
        finally:
            db.close()
        if row:
            return (row[0] or "").strip()
    except Exception:
        pass
    return ""


def _img_to_b64_for_api(img: "Image.Image", max_px: int = 1568) -> str:
    """Skaluje obraz do max_px (Claude limit) i koduje JPEG base64."""
    w, h = img.size
    if max(w, h) > max_px:
        scale = max_px / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img = img.convert("RGB")
    img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def _render_pdf_pages(path: str, dpi: int = 180) -> list:
    """Renderuje strony PDF do obrazów PIL. Zwraca [(page_num, img), ...]."""
    if not HAS_FITZ:
        return []
    pages = []
    try:
        doc = fitz.open(path)
        try:
            for i, page in enumerate(doc):
                mat = fitz.Matrix(dpi / 72, dpi / 72)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                pages.append((i + 1, img))
        finally:
            doc.close()
    except Exception:
        pass
    return pages


def _load_image(path: str) -> Optional["Image.Image"]:
    if not HAS_PIL:
        return None
    try:
        # BUGFIX: zamknij uchwyt pliku — Image.open(...).convert() zostawia go otwarty
        with Image.open(path) as raw:
            return raw.convert("RGB")
    except Exception:
        return None


def _call_claude_vision(img_b64: str, api_key: str) -> dict:
    """Wysyła obraz do Claude i zwraca sparsowany JSON lub {"error": ...}."""
    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 4096,
        "system": _SYSTEM,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64",
                                              "media_type": "image/jpeg",
                                              "data": img_b64}},
                {"type": "text", "text": _PROMPT},
            ],
        }],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        # bandit: URL to stała https:// w kodzie
        with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310
            data = json.loads(resp.read().decode())
        _content = data.get("content") or []
        if not _content:
            return {"error": "Empty API response content"}
        _block = next((b for b in _content if b.get("type") == "text"), None)
        if _block is None:
            return {"error": "Brak bloku tekstowego w odpowiedzi"}
        text = (_block.get("text") or "").strip()
        text = re.sub(r"^```(?:json)?\s*", "", text).rstrip("`").strip()
        # Najpierw spróbuj sparsować całość (model często zwraca czysty JSON).
        try:
            return json.loads(text)
        except Exception:
            pass
        # Fallback: wytnij pierwszy obiekt {...} lub tablicę [...] (non-greedy).
        m = re.search(r"\{[\s\S]*\}|\[[\s\S]*\]", text)
        return json.loads(m.group()) if m else {"error": "Brak JSON w odpowiedzi"}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            body = str(e.reason)
        return {"error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"error": str(e)[:200]}


# ─── GŁÓWNA FUNKCJA ───────────────────────────────────────────────────────────

def extract_from_file(path: str, filename: str) -> list[dict]:
    """
    Ekstraktuje dane dostawy z jednego pliku (PDF lub obraz).
    Zwraca listę wierszy (dict wg EMPTY_ROW).
    Dla PDF wielostronicowego — osobny wiersz na stronę (lub pozycję).
    """
    api_key = _get_api_key()
    if not api_key:
        return [{**EMPTY_ROW, "plik": filename, "uwagi": "BŁĄD: brak klucza API"}]

    ext = os.path.splitext(filename)[1].lower()
    pages_imgs = []

    if ext == ".pdf":
        pages_imgs = _render_pdf_pages(path)
        if not pages_imgs:
            return [{**EMPTY_ROW, "plik": filename, "uwagi": "BŁĄD: nie można renderować PDF"}]
    elif ext in (".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp"):
        img = _load_image(path)
        if img:
            pages_imgs = [(1, img)]
        else:
            return [{**EMPTY_ROW, "plik": filename, "uwagi": "BŁĄD: nie można otworzyć obrazu"}]
    else:
        return [{**EMPTY_ROW, "plik": filename, "uwagi": f"BŁĄD: nieobsługiwany format {ext}"}]

    rows = []
    for page_num, img in pages_imgs:
        img_b64 = _img_to_b64_for_api(img)
        result  = _call_claude_vision(img_b64, api_key)

        if "error" in result:
            rows.append({**EMPTY_ROW, "plik": filename, "strona": str(page_num),
                         "uwagi": f"BŁĄD AI: {result['error']}"})
            continue

        header = {
            "typ_dok":  result.get("typ_dok", ""),
            "nr_dok":   result.get("nr_dok", ""),
            "dostawca": result.get("dostawca", ""),
            "data":     result.get("data", ""),
        }

        pozycje = result.get("pozycje", [])
        if not pozycje:
            rows.append({**EMPTY_ROW, **header, "plik": filename, "strona": str(page_num),
                         "uwagi": "Brak pozycji wykrytych na stronie"})
            continue

        for poz in pozycje:
            row = {**EMPTY_ROW, **header}
            row["plik"]       = filename
            row["strona"]     = str(page_num)
            row["ref"]        = poz.get("ref", "")
            row["opis"]       = poz.get("opis", "")
            row["lot"]        = poz.get("lot", "")
            row["jm"]         = poz.get("jm", "")
            row["ilosc_zam"]  = poz.get("ilosc_zam", "")
            row["ilosc_dost"] = poz.get("ilosc_dost", "")
            row["rozbiez"]    = poz.get("rozbiez", "")
            row["uwagi"]      = poz.get("uwagi", "")
            rows.append(row)

    return rows


# ─── EXCEL EXPORT ─────────────────────────────────────────────────────────────

def export_to_excel(rows: list[dict]) -> bytes:
    """Generuje plik Excel gotowy do wklejenia do SAP."""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    except ImportError:
        raise RuntimeError("openpyxl nie jest zainstalowany")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Dane dostaw"

    # Styl nagłówka
    hdr_fill = PatternFill("solid", fgColor="1F4E79")
    hdr_font = Font(bold=True, color="FFFFFF", size=10)
    hdr_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin = Side(style="thin", color="BBBBBB")
    cell_border = Border(left=thin, right=thin, top=thin, bottom=thin)

    keys   = list(COLUMNS_PL.keys())
    labels = list(COLUMNS_PL.values())

    # Nagłówek
    for col, label in enumerate(labels, 1):
        c = ws.cell(row=1, column=col, value=label)
        c.font      = hdr_font
        c.fill      = hdr_fill
        c.alignment = hdr_align
        c.border    = cell_border

    ws.row_dimensions[1].height = 30

    # Dane
    alt_fill = PatternFill("solid", fgColor="EEF2FF")
    err_fill = PatternFill("solid", fgColor="FFE4E4")

    for r_idx, row in enumerate(rows, 2):
        is_err = str(row.get("uwagi", "")).startswith("BŁĄD")
        fill   = err_fill if is_err else (alt_fill if r_idx % 2 == 0 else None)
        for col, key in enumerate(keys, 1):
            val = row.get(key, "")
            # Spróbuj konwertować liczby (kanoniczny parser: obsługuje 1.234,5 /
            # 1,234.5 / spacje — naiwne replace(',','.') psuło europejskie tysiące).
            if key in ("ilosc_zam", "ilosc_dost", "rozbiez") and val:
                try:
                    from normalizer import normalize_number as _nn
                    _v = _nn(str(val))
                    if _v is not None:
                        val = float(_v)
                except Exception:
                    pass
            c = ws.cell(row=r_idx, column=col, value=val if val != "" else None)
            c.border = cell_border
            c.alignment = Alignment(vertical="center", wrap_text=False)
            if fill:
                c.fill = fill

    # Szerokości kolumn
    col_widths = {
        "plik": 28, "strona": 7, "typ_dok": 10, "nr_dok": 18,
        "dostawca": 22, "data": 12, "ref": 16, "opis": 28,
        "lot": 16, "jm": 6, "ilosc_zam": 12, "ilosc_dost": 12,
        "rozbiez": 12, "uwagi": 32,
    }
    for col, key in enumerate(keys, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = col_widths.get(key, 14)

    # Zamróź nagłówek
    ws.freeze_panes = "A2"

    # Auto-filter
    ws.auto_filter.ref = ws.dimensions

    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()
