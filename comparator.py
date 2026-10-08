"""
comparator.py — DEPRECATED: zastąpiony przez enhanced_comparator.py.

Ten moduł jest zachowany wyłącznie dla kompatybilności wstecznej z odwołaniami
w app.py (extract_pdf_text, compare_documents). Nie dodawaj tu nowych funkcji.
Użyj enhanced_comparator.compare_enhanced() dla nowego kodu.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional
try:
    from rapidfuzz import fuzz
except ImportError:   # opcjonalna zależność — bramka CI jej nie instaluje
    fuzz = None


def _token_sort_ratio(a: str, b: str) -> int:
    """Fuzzy similarity 0-100 — rapidfuzz gdy dostępny, inaczej difflib fallback.
    Bez tego cały moduł rzucał AttributeError, gdy rapidfuzz nie był zainstalowany."""
    if fuzz is not None:
        return int(fuzz.token_sort_ratio(a, b))
    import difflib
    ta = " ".join(sorted((a or "").split()))
    tb = " ".join(sorted((b or "").split()))
    return int(round(difflib.SequenceMatcher(None, ta, tb).ratio() * 100))

# ─────────────────────────────────────────────────────────────────────────────
# MODULE-LEVEL CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# FIX 7: Named constant for fuzzy matching threshold
_FUZZY_THRESHOLD_ITEMS = 55

# FIX 8: Compile TYPE_PATTERNS once at module level for detect_doc_type()
_COMPILED_TYPE_PATTERNS: dict = {}

# ─────────────────────────────────────────────────────────────────────────────
# STRUKTURY DANYCH
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FieldResult:
    field: str
    doc_a: Optional[str]
    doc_b: Optional[str]
    status: str        # "ok" | "format" | "watpliwe" | "roznica" | "brak_w_a" | "brak_w_b"
    severity: str      # "info" | "warning" | "error"
    category: str      # "identyfikator" | "data" | "kontrahent" | "kwota" | "platnosc" | "dostawa" | "pozycja"
    similarity: int    # 0–100
    comment: str = ""


@dataclass
class LineItem:
    lp: int
    name: str
    qty_a: Optional[str]
    qty_b: Optional[str]
    price_a: Optional[str]
    price_b: Optional[str]
    value_a: Optional[str]
    value_b: Optional[str]
    vat_a: Optional[str]
    vat_b: Optional[str]
    status: str        # "ok" | "format" | "watpliwe" | "roznica"
    comment: str = ""


@dataclass
class CompareResult:
    doc_type_detected: str
    summary: str
    diff_count: int
    warn_count: int
    format_count: int
    ok_count: int
    risk_level: str
    fields: list
    line_items: list

    def to_dict(self):
        return {
            "doc_type_detected": self.doc_type_detected,
            "summary": self.summary,
            "diff_count": self.diff_count,
            "warn_count": self.warn_count,
            "format_count": self.format_count,
            "ok_count": self.ok_count,
            "risk_level": self.risk_level,
            "fields": [vars(f) for f in self.fields],
            "line_items": [vars(i) for i in self.line_items],
        }


# ─────────────────────────────────────────────────────────────────────────────
# NORMALIZACJA
# ─────────────────────────────────────────────────────────────────────────────

def normalize_text(text: str) -> str:
    """Małe litery, bez diakrytyków, bez nadmiarowych spacji."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower().strip())


def parse_number(text: str) -> Optional[float]:
    """
    Parsuje liczbę z dowolnego formatu PL/EN:
    '10 500,00 PLN' → 10500.0
    '10.500,00'     → 10500.0
    '10,500.00'     → 10500.0
    '10500'         → 10500.0
    """
    if not text:
        return None
    t = text.upper()
    # Usuń litery (waluty/jednostki: PLN/EUR/USD…) jednym przejściem, unicode-aware.
    # Wcześniejszy replace podciągów ('ZT','ZL') mógł psuć zawartość alfanumeryczną.
    t = re.sub(r"[^\W\d_]+", "", t)
    t = re.sub(r"[\s\u00a0]+", "", t)   # remove all whitespace + NBSP (replaces .replace x2 + .strip)

    if not t:
        return None

    # Delegate to the canonical normalizer for app-wide invariants
    # (1.500=1500, "5%"→None, European thousands).
    from normalizer import normalize_number
    val = normalize_number(t)
    return round(float(val), 4) if val is not None else None


def format_number_diff(a: str, b: str) -> Optional[str]:
    """
    Sprawdza czy dwie liczby są tą samą wartością zapisaną inaczej.
    Zwraca opis różnicy formatowania lub None jeśli wartości się różnią.
    """
    fa, fb = parse_number(a), parse_number(b)
    if fa is None or fb is None:
        return None
    if abs(fa - fb) < 0.005:
        # Taka sama wartość — sprawdź czy format się różni
        na, nb = normalize_text(a), normalize_text(b)
        if na != nb:
            return f"Różny format liczby: '{a.strip()}' vs '{b.strip()}' — wartość taka sama ({fa:,.2f})"
        return "ok"
    return None


def detect_currency_rate_diff(a: str, b: str) -> Optional[str]:
    """
    Wykrywa czy różnica kwot wynika z kursu walutowego.
    Np. 1000 EUR vs 4321 PLN → kurs ~4.32
    """
    fa, fb = parse_number(a), parse_number(b)
    if fa is None or fb is None or fa == 0 or fb == 0:
        return None
    ratio = fb / fa
    # Sprawdzaj też kierunek odwrotny (PLN vs EUR), normalizując do >1.
    # ratio jest niezerowy (fa,fb != 0 wyżej), więc 1/ratio jest bezpieczne (ujemny → odpada niżej).
    r = ratio if ratio >= 1.0 else 1.0 / ratio
    if 1.0 < r < 100:
        # Typowe kursy do PLN: EUR ~4.2–4.6, USD ~3.8–4.2, GBP ~5.0, CHF ~4.6.
        # UWAGA: NIE dodawać 1.0 do listy — z oknem ±0.15 oznaczałoby każdą parę
        # kwot różniących się o <15% jako „różnicę kursu" (masowe false-positive).
        for typical in (3.8, 3.9, 4.0, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 5.0):
            if abs(r - typical) < 0.15:
                return f"⚠ Możliwa różnica kursu walutowego: przelicznik ≈ {ratio:.4f}"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# PORÓWNANIE PÓL
# ─────────────────────────────────────────────────────────────────────────────

def compare_field(a: Optional[str], b: Optional[str],
                  is_numeric: bool = False,
                  base_severity: str = "error") -> tuple[str, str, int, str]:
    """
    Porównuje dwie wartości pola.
    Zwraca (status, severity, similarity, comment).

    Statusy:
      ok        — identyczne lub numerycznie równoważne
      format    — ta sama wartość, inny format (przecinek/kropka, spacje, wielkie litery)
      watpliwe  — podobne (fuzzy ≥ 75%), wymaga weryfikacji
      roznica   — różne wartości
      brak_w_a  — tylko w B
      brak_w_b  — tylko w A
    """
    if a is None and b is None:
        return "ok", "info", 100, ""
    if a is None:
        return "brak_w_a", base_severity, 0, "Pole obecne tylko w dokumencie B"
    if b is None:
        return "brak_w_b", base_severity, 0, "Pole obecne tylko w dokumencie A"

    # Porównanie tekstowe — najpierw dokładne
    if normalize_text(a) == normalize_text(b):
        return "ok", "info", 100, ""

    # Porównanie numeryczne
    if is_numeric:
        fa, fb = parse_number(a), parse_number(b)
        if fa is not None and fb is not None:
            if abs(fa - fb) < 0.005:
                # Wartość ta sama, format różny
                diff_fmt = format_number_diff(a, b)
                if diff_fmt and diff_fmt != "ok":
                    return "format", "info", 99, diff_fmt
                return "ok", "info", 100, ""
            else:
                delta = fb - fa
                # Nie zawyżaj mianownika dla znikomej bazy — to dawałoby
                # fałszywie niski % przy realnej różnicy. Gdy baza < epsilon,
                # procent nie jest miarodajny → 0% (różnica bezwzględna jest w opisie).
                _eps = 0.01
                pct = (abs(delta) / abs(fa) * 100) if abs(fa) >= _eps else 0.0
                comment = f"Różnica: {delta:+,.2f} ({pct:.1f}%)"
                # Sprawdź czy to może kurs walutowy
                cur = detect_currency_rate_diff(a, b)
                if cur:
                    comment += f" | {cur}"
                    return "watpliwe", "warning", 50, comment
                sev = "error" if pct > 1 else "warning"
                return "roznica", sev, 0, comment

    # Fuzzy matching tekstowy
    sim = _token_sort_ratio(normalize_text(a), normalize_text(b))

    if sim >= 95:
        return "format", "info", sim, f"Drobna różnica formatu: '{a.strip()}' vs '{b.strip()}'"
    if sim >= 80:
        return "watpliwe", "warning", sim, f"Podobieństwo {sim}% — sprawdź ręcznie"
    if sim >= 60:
        return "roznica", base_severity, sim, f"Znaczna różnica (podobieństwo {sim}%)"
    return "roznica", base_severity, sim, f"Różne wartości (podobieństwo {sim}%)"


# ─────────────────────────────────────────────────────────────────────────────
# EKSTRAKCJA PDF
# ─────────────────────────────────────────────────────────────────────────────

def extract_pdf_text(filepath: str) -> str:
    import pdfplumber
    pages = []
    with pdfplumber.open(filepath) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text()
            if text and text.strip():
                pages.append(f"[STRONA {i+1}]\n{text.strip()}")
    if not pages:
        raise ValueError(
            "PDF nie zawiera tekstu. Plik może być skanem — wymagane OCR (np. tesseract)."
        )
    return "\n\n".join(pages)


# ─────────────────────────────────────────────────────────────────────────────
# DETEKCJA TYPU DOKUMENTU
# ─────────────────────────────────────────────────────────────────────────────

TYPE_PATTERNS = {
    "faktura_vat": [
        r"faktura\s*(vat|koryguj|pro\s*forma)?",
        r"nr\s+faktury", r"podatek\s+vat",
        r"kwota\s+brutto", r"stawka\s+vat",
    ],
    "wz": [
        r"\bwz\b", r"wydanie\s+zewn",
        r"dokument\s+wz", r"wydano\s+na\s+zewn",
    ],
    "zamowienie": [
        r"zamówienie\s*(zakupu|sprzedaży|nr)?",
        r"purchase\s+order", r"\bpo\s+nr\b",
        r"potwierdzenie\s+zamówienia",
    ],
    "list_przewozowy": [
        r"list\s+przewozowy", r"\bcmr\b",
        r"nadawca.*odbiorca", r"miejsce\s+(za|roz)ładunku", r"kierowca",
    ],
}

def _build_compiled_type_patterns():
    """Build compiled regex patterns for detect_doc_type() — called once."""
    global _COMPILED_TYPE_PATTERNS
    if not _COMPILED_TYPE_PATTERNS:
        _COMPILED_TYPE_PATTERNS = {
            dt: [re.compile(p, re.IGNORECASE) for p in pats]
            for dt, pats in TYPE_PATTERNS.items()
        }


def detect_doc_type(text: str) -> str:
    # FIX 8: Use pre-compiled patterns; build them on first call
    if not _COMPILED_TYPE_PATTERNS:
        _build_compiled_type_patterns()
    text_lower = text.lower()
    scores = {dt: sum(1 for cp in cpats if cp.search(text_lower))
              for dt, cpats in _COMPILED_TYPE_PATTERNS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "inny"


# ─────────────────────────────────────────────────────────────────────────────
# DEFINICJE PÓL — KOMPLETNA LISTA
# ─────────────────────────────────────────────────────────────────────────────
# Format: (nazwa, kategoria, [wzorce_regex], is_numeric, severity_if_diff)

FIELD_PATTERNS = [
    # ── IDENTYFIKATORY ──────────────────────────────────────────────────────
    ("Numer dokumentu", "identyfikator", [
        # FV/WZ PL
        r"(?:nr|numer)\s+(?:faktury|dokumentu|fv|wz)[^\n:]{0,15}?[:\s]+([A-Z0-9\/\-]{3,30})",
        # Invoice/FD PI No
        r"(?:FD\s+PI\s+NO|invoice\s+no|invoice\s+number)\s*[:\s]+([A-Z0-9\/\-]{5,30})",
    ], False, "error"),

    ("Numer zamówienia / PO", "identyfikator", [
        # PO: "Purchase Order4500000474" (bez spacji) lub "Purchase Order 4500000474"
        r"Purchase Order\s*(\d{7,12})",
        r"(?:nr\s+zamówienia|numer\s+po)\s+(\d{7,12})",
        r"customer\s+order\s+no[:\s]+(\d{7,12})",
    ], False, "error"),

    ("Numer referencyjny", "identyfikator", [
        r"(?:referencja|nr\s+ref)\s*[:\s]+([A-Z0-9\/\-]{3,30})",
    ], False, "warning"),

    # ── DATY ────────────────────────────────────────────────────────────────
    ("Data wystawienia", "data", [
        r"(?:data\s+wystawienia|wystawiono|issue\s+date|date\s+of\s+issue)[^\n:]*?[:\s]+(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})",
    ], False, "error"),

    ("Data sprzedaży", "data", [
        r"(?:data\s+sprzedaży|data\s+wykonania|sale\s+date)[^\n:]*?[:\s]+(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})",
    ], False, "error"),

    ("Data dostawy", "data", [
        r"(?:data\s+dostawy|termin\s+dostawy|delivery\s+date)[^\n:]*?[:\s]+(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})",
    ], False, "warning"),

    ("Termin płatności", "data", [
        r"(?:termin\s+płatności|płatność\s+do|payment\s+due|due\s+date)[^\n:]*?[:\s]+(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}|\d+\s*dni)",
    ], False, "warning"),

    # ── SPRZEDAWCA ──────────────────────────────────────────────────────────
    ("Nazwa sprzedawcy", "kontrahent", [
        r"(?:sprzedawca|wystawca|vendor|seller|dostawca)[^\n:\r]{0,5}[\r\n:]+\s*([^\n\r]{5,80})",
    ], False, "error"),

    ("NIP sprzedawcy", "kontrahent", [
        r"(?:nip\s*(?:sprzedawcy|wystawcy|dostawcy))[^\n:]*?[:\s]+(\d[\d\s\-]{8,14})",
        r"(?<!nabywcy\s)(?<!klienta\s)\bnip\b[^\n:]*?[:\s]+(\d[\d\s\-]{8,14})",
    ], False, "error"),

    ("REGON sprzedawcy", "kontrahent", [
        r"(?:regon)[^\n:]*?[:\s]+(\d{9,14})",
    ], False, "warning"),

    ("Adres sprzedawcy", "kontrahent", [
        r"(?:sprzedawca|wystawca|vendor)[^\n:\r]{0,5}[\r\n:]+[^\n\r]{5,80}[\r\n]+\s*([^\n\r]{5,80})",
    ], False, "warning"),

    # ── NABYWCA ─────────────────────────────────────────────────────────────
    ("Nazwa nabywcy", "kontrahent", [
        # PL dokumenty
        r"(?:nabywca|kupujący|odbiorca|zamawiający)\s*[:\n\r]+\s*([^\n\r]{5,60})",
        # EN — "Buyer:\n...ACME..." (tekst między Buyer a ACME może być boilerplate)
        r"Buyer\s*:\s*\n(?:[^\n]*\n){0,2}(ACME[^\n]{0,60})",
        r"^The Buyer\s*:\s*\n\s*([A-Z][^\n\r]{4,60})",
    ], False, "error"),

    ("NIP nabywcy", "kontrahent", [
        r"(?:nip\s*(?:nabywcy|kupującego|klienta|odbiorcy))[^\n:]*?[:\s]+(\d[\d\s\-]{8,14})",
    ], False, "error"),

    ("Adres nabywcy", "kontrahent", [
        r"(?:nabywca|kupujący)\s*[:\n\r]+[^\n\r]{5,80}[\r\n]+\s*([^\n\r]{5,80})",
    ], False, "warning"),

    # ── KWOTY ───────────────────────────────────────────────────────────────
    ("Kwota netto", "kwota", [
        r"(?:wartość\s+netto|suma\s+netto|netto\s+razem|razem\s+netto|net\s+(?:total|amount|value))[^\n:]*?[:\s]+([\d\s.,]+(?:\s*(?:PLN|EUR|USD|zł))?)",
        r"(?:razem|suma|total)[^\n:]*?netto[^\n:]*?[:\s]+([\d\s.,]+)",
    ], True, "error"),

    ("Kwota VAT 23%", "kwota", [
        r"(?:vat\s*23|23\s*%\s*vat)[^\n:]*?[:\s]+([\d\s.,]+(?:\s*(?:PLN|zł))?)",
        r"23[,.]00\s*%[^\n]*?([\d\s.,]+(?:\s*PLN)?)",
    ], True, "error"),

    ("Kwota VAT 8%", "kwota", [
        r"(?:vat\s*8|8\s*%\s*vat)[^\n:]*?[:\s]+([\d\s.,]+(?:\s*(?:PLN|zł))?)",
    ], True, "warning"),

    ("Kwota VAT 5%", "kwota", [
        r"(?:vat\s*5|5\s*%\s*vat)[^\n:]*?[:\s]+([\d\s.,]+(?:\s*(?:PLN|zł))?)",
    ], True, "warning"),

    ("Kwota VAT (łącznie)", "kwota", [
        r"(?:podatek\s+vat|kwota\s+vat|vat\s+razem|vat\s+total|suma\s+vat)[^\n:]*?[:\s]+([\d\s.,]+(?:\s*(?:PLN|zł))?)",
        r"(?:razem|suma)[^\n:]*?vat[^\n:]*?[:\s]+([\d\s.,]+)",
    ], True, "error"),

    ("Kwota brutto", "kwota", [
        r"(?:wartość\s+brutto|suma\s+brutto|razem\s+brutto|brutto\s+razem|do\s+zapłaty|kwota\s+do\s+zapłaty|gross\s+total|amount\s+due)[^\n:]*?[:\s]+([\d\s.,]+(?:\s*(?:PLN|EUR|USD|zł))?)",
    ], True, "error"),

    ("Waluta", "kwota", [
        r"(?:waluta|currency)[^\n:]*?[:\s]+([A-Z]{3})",
        r"\b(EUR|USD|GBP|CHF|SEK|NOK)\b",
    ], False, "warning"),

    ("Kurs waluty", "kwota", [
        r"(?:kurs|exchange\s+rate|rate)[^\n:]*?[:\s]+([\d.,]+)",
    ], True, "warning"),

    # ── PŁATNOŚĆ ────────────────────────────────────────────────────────────
    ("Forma płatności", "platnosc", [
        r"(?:forma\s+płatności|sposób\s+płatności|metoda\s+płatności|payment\s+(?:method|terms))[^\n:]*?[:\s]+([^\n\r]{3,50})",
    ], False, "warning"),

    ("Numer konta bankowego", "platnosc", [
        r"(?:nr\s+konta|numer\s+konta|konto|iban|rachunek\s+bankowy)[^\n:]*?[:\s]+([A-Z]{0,2}[\d\s]{15,40})",
    ], False, "error"),

    ("Bank", "platnosc", [
        r"(?:bank|bank\s+sprzedawcy)[^\n:]*?[:\s]+([^\n\r]{3,50})",
    ], False, "info"),

    ("BIC/SWIFT", "platnosc", [
        r"(?:bic|swift)[^\n:]*?[:\s]+([A-Z0-9]{8,11})",
    ], False, "warning"),

    # ── DOSTAWA ─────────────────────────────────────────────────────────────
    ("Miejsce dostawy", "dostawa", [
        r"(?:miejsce\s+dostawy|adres\s+dostawy|deliver(?:y)?\s+(?:to|address)|ship\s+to)[^\n:\r]{0,5}[\r\n:]+\s*([^\n\r]{5,80})",
    ], False, "warning"),

    ("Warunki dostawy (Incoterms)", "dostawa", [
        r"(?:incoterms?|warunki\s+dostawy)[^\n:]*?[:\s]+([A-Z]{3}(?:\s+\w+)?)",
    ], False, "warning"),

    ("Przewoźnik", "dostawa", [
        r"(?:przewoźnik|carrier|spedytor|forwarder)[^\n:]*?[:\s]+([^\n\r]{3,50})",
    ], False, "info"),

    ("Nr listu przewozowego", "dostawa", [
        r"(?:nr\s+(?:listu\s+przewozowego|cmr|awb|bl)|tracking)[^\n:]*?[:\s]+([A-Z0-9\-]{5,30})",
    ], False, "warning"),

    # ── DODATKOWE ───────────────────────────────────────────────────────────
    ("Uwagi / komentarz", "inne", [
        r"(?:uwagi|komentarz|remarks?|notes?|comments?)[^\n:]*?[:\s]+([^\n\r]{5,120})",
    ], False, "info"),
]


# ─────────────────────────────────────────────────────────────────────────────
# EKSTRAKCJA WARTOŚCI PÓL
# ─────────────────────────────────────────────────────────────────────────────

def extract_field(text: str, patterns: list) -> Optional[str]:
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if m:
            val = m.group(1).strip()
            # Odetnij doklejoną sąsiednią kolumnę (separator OCR to zwykle tabulator
            # lub 3+ spacji). NIE tnij na podwójnej spacji — legalne nazwy/adresy
            # bywają OCR-owane z szerokim odstępem ("ACME  INTERNATIONAL  GROUP")
            # i wcześniej traciły wszystko poza pierwszym słowem.
            val = re.sub(r"(?:\t+| {3,}).*$", "", val).strip()
            if len(val) >= 2:
                return val
    return None


# ─────────────────────────────────────────────────────────────────────────────
# EKSTRAKCJA POZYCJI TOWAROWYCH
# ─────────────────────────────────────────────────────────────────────────────

LINE_ITEM_RE = re.compile(
    r"^\s*(\d{1,3})[.\)]\s+"
    r"(.{3,60}?)\s{2,}"  # FIX 6: removed spurious ? after 60 (was {3,60?})
    r"([\d\s.,]+(?:\s*(?:szt|m2?|mb|kg|l|t|godz|h|pcs?|pc|kpl|op|pal|rol))?)?\s*"
    r"([\d\s.,]+(?:\s*%)?)\s*"   # VAT%
    r"([\d\s.,]+)\s*"            # cena jedn.
    r"([\d\s.,]+)\s*$",          # wartość
    re.IGNORECASE | re.MULTILINE
)

LINE_SIMPLE_RE = re.compile(
    r"^\s*(\d{1,3})[.\)]\s+([A-Za-z\u00C0-\u024F\d][^\n]{3,60}?)\s{2,}([\d.,]+)\s+([\d.,]+)\s*$",
    re.MULTILINE
)


def extract_line_items(text: str) -> list[dict]:
    items = []
    # znajdź sekcję pozycji
    sec = re.search(
        r"(?:lp|l\.p\.|poz(?:ycja)?|nr)\s+(?:nazwa|opis|towar|usługa|artykuł)",
        text, re.IGNORECASE
    )
    if sec:
        text = text[sec.start():]

    for m in LINE_ITEM_RE.finditer(text):
        name = (m.group(2) or "").strip()
        if len(name) < 3:
            continue
        items.append({
            "lp": int(m.group(1)),
            "name": name,
            "qty": (m.group(3) or "").strip() or None,
            "vat": (m.group(4) or "").strip() or None,
            "price": (m.group(5) or "").strip() or None,
            "value": (m.group(6) or "").strip() or None,
        })

    if not items:
        for m in LINE_SIMPLE_RE.finditer(text):
            name = (m.group(2) or "").strip()
            if len(name) < 3:
                continue
            items.append({
                "lp": int(m.group(1)),
                "name": name,
                "qty": m.group(3),
                "vat": None,
                "price": None,
                "value": m.group(4),
            })

    return items[:100]


def match_line_items(items_a: list, items_b: list) -> list[LineItem]:
    results = []
    used_b = set()

    for item_a in items_a:
        best_score = 0
        best_b = None
        for i, item_b in enumerate(items_b):
            if i in used_b:
                continue
            score = _token_sort_ratio(
                normalize_text(item_a["name"]),
                normalize_text(item_b["name"])
            )
            if score > best_score:
                best_score = score
                best_b = (i, item_b)

        if best_b and best_score >= _FUZZY_THRESHOLD_ITEMS:  # FIX 7: use named constant
            idx_b, ib = best_b
            used_b.add(idx_b)

            comments = []
            issue = False
            fmt_only = False

            # Porównaj ilość
            qty_s, _, _, qty_c = compare_field(item_a.get("qty"), ib.get("qty"), True, "error")
            if qty_s not in ("ok",) and item_a.get("qty") and ib.get("qty"):
                if qty_s == "format":
                    fmt_only = True
                    comments.append(f"Ilość — {qty_c}")
                else:
                    issue = True
                    comments.append(f"Ilość — {qty_c}")

            # Porównaj cenę
            price_s, _, _, price_c = compare_field(item_a.get("price"), ib.get("price"), True, "error")
            if price_s not in ("ok",) and item_a.get("price") and ib.get("price"):
                if price_s == "format":
                    fmt_only = True
                    comments.append(f"Cena — {price_c}")
                else:
                    issue = True
                    comments.append(f"Cena — {price_c}")

            # Porównaj wartość
            val_s, _, _, val_c = compare_field(item_a.get("value"), ib.get("value"), True, "error")
            if val_s not in ("ok",) and item_a.get("value") and ib.get("value"):
                if val_s == "format":
                    fmt_only = True
                    comments.append(f"Wartość — {val_c}")
                else:
                    issue = True
                    comments.append(f"Wartość — {val_c}")

            # Porównaj VAT
            vat_s, _, _, vat_c = compare_field(item_a.get("vat"), ib.get("vat"), False, "warning")
            if vat_s not in ("ok",) and item_a.get("vat") and ib.get("vat"):
                comments.append(f"VAT — {vat_c}")

            # Porównaj nazwy (fuzzy)
            if best_score < 90:
                comments.append(f"Nazwa podobna w {best_score}%")
                if best_score < 75:
                    issue = True

            if issue:
                status = "roznica"
            elif fmt_only and not issue:
                status = "format"
            elif comments:
                status = "watpliwe"
            else:
                status = "ok"

            results.append(LineItem(
                lp=item_a["lp"],
                name=item_a["name"],
                qty_a=item_a.get("qty"),
                qty_b=ib.get("qty"),
                price_a=item_a.get("price"),
                price_b=ib.get("price"),
                value_a=item_a.get("value"),
                value_b=ib.get("value"),
                vat_a=item_a.get("vat"),
                vat_b=ib.get("vat"),
                status=status,
                comment=" | ".join(comments),
            ))
        else:
            results.append(LineItem(
                lp=item_a["lp"],
                name=item_a["name"],
                qty_a=item_a.get("qty"),
                qty_b=None,
                price_a=item_a.get("price"),
                price_b=None,
                value_a=item_a.get("value"),
                value_b=None,
                vat_a=item_a.get("vat"),
                vat_b=None,
                status="roznica",
                comment="Pozycja nie odnaleziona w dokumencie B",
            ))

    for i, ib in enumerate(items_b):
        if i not in used_b:
            results.append(LineItem(
                lp=len(items_a) + i + 1,
                name=f"[tylko dok. B] {ib['name']}",
                qty_a=None,
                qty_b=ib.get("qty"),
                price_a=None,
                price_b=ib.get("price"),
                value_a=None,
                value_b=ib.get("value"),
                vat_a=None,
                vat_b=ib.get("vat"),
                status="roznica",
                comment="Pozycja obecna tylko w dokumencie B",
            ))

    return results


# ─────────────────────────────────────────────────────────────────────────────
# GŁÓWNA FUNKCJA
# ─────────────────────────────────────────────────────────────────────────────

def compare_documents(text_a: str, text_b: str, doc_type_hint: str = "auto") -> CompareResult:

    # Detekcja typu
    if doc_type_hint in ("auto", None, ""):
        doc_type = detect_doc_type(text_a)
    else:
        doc_type = doc_type_hint

    # ── Porównanie wszystkich pól ─────────────────────────────────────────
    field_results: list[FieldResult] = []

    for fname, category, patterns, is_numeric, base_severity in FIELD_PATTERNS:
        val_a = extract_field(text_a, patterns)
        val_b = extract_field(text_b, patterns)

        # Pokaż pole tylko jeśli znaleziono je w przynajmniej jednym dokumencie
        if val_a is None and val_b is None:
            continue

        status, severity, sim, comment = compare_field(val_a, val_b, is_numeric, base_severity)

        field_results.append(FieldResult(
            field=fname,
            doc_a=val_a,
            doc_b=val_b,
            status=status,
            severity=severity,
            category=category,
            similarity=sim,
            comment=comment,
        ))

    # ── Pozycje towarowe ──────────────────────────────────────────────────
    items_a = extract_line_items(text_a)
    items_b = extract_line_items(text_b)
    line_items = match_line_items(items_a, items_b)

    # ── Statystyki ────────────────────────────────────────────────────────
    diff_count   = sum(1 for f in field_results if f.status in ("roznica", "brak_w_a", "brak_w_b"))
    warn_count   = sum(1 for f in field_results if f.status == "watpliwe")
    format_count = sum(1 for f in field_results if f.status == "format")
    ok_count     = sum(1 for f in field_results if f.status == "ok")

    item_errors  = sum(1 for i in line_items if i.status == "roznica")
    item_warns   = sum(1 for i in line_items if i.status == "watpliwe")
    item_fmts    = sum(1 for i in line_items if i.status == "format")

    diff_count  += item_errors
    warn_count  += item_warns
    format_count += item_fmts

    # ── Poziom ryzyka ─────────────────────────────────────────────────────
    error_fields = [f for f in field_results if f.status in ("roznica","brak_w_a","brak_w_b") and f.severity == "error"]
    if diff_count > 0 or error_fields:
        risk = "blad"
    elif warn_count > 0:
        risk = "ostrzezenie"
    elif format_count > 0:
        risk = "format"
    else:
        risk = "ok"

    # ── Podsumowanie ──────────────────────────────────────────────────────
    labels = {
        "faktura_vat": "Faktura VAT",
        "wz": "Dokument WZ",
        "zamowienie": "Zamówienie",
        "list_przewozowy": "List przewozowy",
        "inny": "Dokument",
    }
    label = labels.get(doc_type, "Dokument")
    total_fields = len(field_results) + len(line_items)

    if diff_count == 0 and warn_count == 0 and format_count == 0:
        summary = (
            f"{label}: dokumenty w pełni zgodne. "
            f"Sprawdzono {total_fields} pól — brak rozbieżności."
        )
    elif diff_count > 0:
        summary = (
            f"{label}: wykryto {diff_count} rozbieżności, {warn_count} wątpliwych, "
            f"{format_count} różnic formatowania. "
            f"Sprawdzono {total_fields} pól. Wymagana weryfikacja."
        )
    elif warn_count > 0:
        summary = (
            f"{label}: brak krytycznych różnic, ale {warn_count} pól wymaga weryfikacji. "
            f"Różnice formatowania: {format_count}. Sprawdzono {total_fields} pól."
        )
    else:
        summary = (
            f"{label}: wartości zgodne, wykryto {format_count} różnic formatowania "
            f"(np. przecinek/kropka, spacje). Sprawdzono {total_fields} pól."
        )

    return CompareResult(
        doc_type_detected=doc_type,
        summary=summary,
        diff_count=diff_count,
        warn_count=warn_count,
        format_count=format_count,
        ok_count=ok_count,
        risk_level=risk,
        fields=field_results,
        line_items=line_items,
    )
