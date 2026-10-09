"""
enhanced_comparator.py — główny silnik porównania dokumentów handlowych ACME.

Obsługuje pary: PO↔PI, PO↔CI, CI↔PL, CI↔SAD, BL↔CI, itd.

ZMIANY vs poprzednia wersja:
  [FIX #1] Ujednolicony parser tabel z priorytetowym wykrywaniem kolumn —
           poprawia mapowanie PI ALPHAMED (Product Code / LOT numbers / Unit-price)
  [FIX #2] Walidacja arytmetyczna qty×price=net tylko dla dokumentu B (PI/CI),
           nie dla A (PO), który zaokrągla ceny — eliminuje fałszywe alarmy
  [FIX #3] Usunięto duplikat wywołania _parse_pi_tables bez supplier_profile
           który nadpisywał total_b złym wynikiem
  [FIX #4] SAP_PAYMENT_MAP z pełnymi ekwiwalentami IW04/IW60/IZ31
  [FIX #5] Adresy ACME (Przemysłowa 10 / Logistyczna 2 / Magazynowa 20) → ok
  [FIX #6] Rozbieżność warunków płatności = WARNING nie ERROR
"""

import re
import math
import logging
from typing import Optional

from normalizer import (
    normalize_number, numbers_equal, numbers_differ_only_by_rounding,
    normalize_date, dates_possibly_equal,
    payment_terms_equal, format_number_diff, validate_checksum,
)
from semantic_matcher import match_items_semantic
from uom import convert_qty

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# STAŁE
# ─────────────────────────────────────────────────────────────────────────────

# Mapowanie kodów SAP → ekwiwalenty tekstowe
# Klucz = kod SAP (wielkie litery), wartość = lista akceptowanych fraz
SAP_PAYMENT_MAP: dict[str, list[str]] = {
    "IW04": [
        "30 days after departure", "30 days after dispatch",
        "30 days after shipment", "30 days", "net 30", "30 days after etd",
        "t/t 30 days", "30 days bl copy",
    ],
    "IW30": [
        "30 days after bl", "30 days after bill of lading",
        "30 days bl", "bl 30",
    ],
    "IW60": [
        "60 days after shipment", "60 days after bl",
        "60 days after etd", "net 60", "t/t 60 days",
        "100% t/t 60 days after shipment", "60 days bl copy",
    ],
    "IZ31": [
        "30% in advance", "30% adv", "30% advance",
        "30% deposit", "70% before shipment",
        "30% in advance, 70% before shipment",
    ],
    "IW07": ["60 days", "net 60"],
    "IW90": ["90 days", "net 90"],
    # FIX 14: additional common SAP codes
    "IW02": ["payment in advance", "advance payment", "prepayment"],
    "IW03": ["cash on delivery", "cod", "cash on delivery (cod)"],
    "IW31": ["30 days from invoice", "30 days from invoice date", "net 30 from invoice"],
}

# Wszystkie znane adresy ACME — porównanie obu adresów z tej listy = ok
ACME_ADDRESSES: list[str] = [
    "przemysłowa 10", "logistyczna 2", "magazynowa 20",
    "26-608 radom", "26-607 radom", "26-603 radom",
]

# Słownik priorytetów wykrywania kolumn.
# Format: {rola: [(fragment_nagłówka, priorytet), ...]}
# UWAGA: 'unit' celowo NIE jest w price bo pasuje do 'quantity/unit'
COLUMN_DETECTION: dict[str, list[tuple[str, int]]] = {
    "ref": [
        ("product code", 10), ("ref / description", 10), ("ref/description", 10),
        ("ref/desc", 10), ("s.no", 9), ("s. no", 9), ("sno", 8),
        ("article no", 7), ("item no", 7), ("nr ref", 7),
        ("code", 6), ("item", 5),
    ],
    "qty": [
        ("quantity/unit", 10), ("order qty", 10), ("qty/unit", 10),
        ("quantity / unit", 10), ("quantity", 8),
        ("qty(pcs)", 8), ("qty", 7), ("pcs", 5), ("menge", 5),
    ],
    "price": [
        ("unit-price", 10), ("unit price", 10), ("unitprice", 10),
        ("price per", 9), ("rate in eur", 9), ("rate in usd", 9),
        ("unit-price\n", 10), ("price\n", 8), ("price", 7),
        # NIE 'unit' — pasuje do 'quantity/unit'
    ],
    "net": [
        ("net value", 10), ("net amount", 10), ("amount in eur", 10),
        ("amount in usd", 10), ("amount\n(usd)", 10), ("amount (usd)", 10),
        ("net", 8), ("amount", 7),
        # NIE 'amount' gdy kolumna zawiera 'unit' (unit amount)
    ],
    "lot": [
        ("lot numbers", 10), ("lot number", 10), ("lot no.", 10),
        ("lot no", 9), ("b/no", 9), ("batch no", 8),
        ("batch", 7), ("lot", 7), ("seria", 6),
    ],
    "desc": [
        ("description of goods", 10), ("descriptions of goods", 10),
        ("description", 9), ("opis towaru", 9), ("opis", 8),
        ("product name", 6), ("name", 5),
    ],
    "exp": [
        ("exp. date", 10), ("expiry date", 10), ("exp date", 10),
        ("exp.date", 10), ("valid through", 8), ("valid until", 8),
    ],
    "mfg": [
        ("mfg. date", 10), ("mfg date", 10), ("mfg.date", 10),
        ("manufacture date", 9), ("production date", 8),
    ],
    "unit": [
        ("order unit", 8), ("base unit", 7), ("unit of measure", 7),
        ("uom", 6),
    ],
}

# Spłaszczona lista wszystkich słów kluczowych nagłówków — budowana raz
# (wykorzystywane przez _find_header_row; wcześniej budowane przy każdym
# wywołaniu tej funkcji, co dawało narzut przy batch processingu).
HEADER_SIGNALS: set[str] = {
    kw for role_rules in COLUMN_DETECTION.values() for kw, _ in role_rules
}


# ─────────────────────────────────────────────────────────────────────────────
# KLASY WYNIKÓW
# ─────────────────────────────────────────────────────────────────────────────

class ComparisonFinding:
    """Jedno znalezisko porównania — błąd, ostrzeżenie lub informacja."""

    def __init__(self, category: str, severity: str, field: str,
                 val_a, val_b, description: str,
                 suggestion: str = "", confidence: float = 1.0):
        self.category    = category
        self.severity    = severity      # 'error' | 'warning' | 'info'
        self.field       = field
        self.val_a       = str(val_a) if val_a is not None else ""
        self.val_b       = str(val_b) if val_b is not None else ""
        self.description = description
        self.suggestion  = suggestion
        self.confidence  = confidence

    def to_dict(self) -> dict:
        return {
            "category":    self.category,
            "severity":    self.severity,
            "field_name":  self.field,
            "val_a":       self.val_a,
            "val_b":       self.val_b,
            "description": self.description,
            "suggestion":  self.suggestion,
            "confidence":  self.confidence,
        }


class EnhancedResult:
    """Wynik porównania dwóch dokumentów."""

    def __init__(self):
        self.items:    list[dict]              = []
        self.headers:  list[dict]              = []
        self.findings: list[ComparisonFinding] = []
        self.doc_type_a = "auto"
        self.doc_type_b = "auto"
        self.extraction_method_a = "fitz"
        self.extraction_method_b = "fitz"
        self.is_scan_a  = False
        self.is_scan_b  = False
        self.warnings:  list[str] = []
        self.total_a:   Optional[str] = None
        self.total_b:   Optional[str] = None

    @property
    def diff_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")

    @property
    def warn_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "warning")

    @property
    def format_count(self) -> int:
        return sum(1 for i in self.items if i.get("status") == "format")

    @property
    def ok_count(self) -> int:
        return sum(1 for i in self.items if i.get("status") == "ok")

    @property
    def risk_level(self) -> str:
        if self.diff_count > 0:  return "blad"
        if self.warn_count > 0:  return "ostrzezenie"
        if self.format_count > 0: return "format"
        return "ok"

    @property
    def summary(self) -> str:
        parts = []
        if self.diff_count:   parts.append(f"{self.diff_count} rozbieżności")
        if self.warn_count:   parts.append(f"{self.warn_count} ostrzeżeń")
        if self.format_count: parts.append(f"{self.format_count} różnic formatu")
        if self.ok_count:     parts.append(f"{self.ok_count} zgodnych")
        return ", ".join(parts) if parts else "Brak rozbieżności"

    def to_dict(self) -> dict:
        return {
            "doc_type_a": self.doc_type_a,
            "doc_type_b": self.doc_type_b,
            "items":      self.items,
            "headers":    self.headers,
            "findings":   [f.to_dict() for f in self.findings],
            "diff_count": self.diff_count,
            "warn_count": self.warn_count,
            "format_count": self.format_count,
            "ok_count":   self.ok_count,
            "risk_level": self.risk_level,
            "summary":    self.summary,
            "total_a":    self.total_a,
            "total_b":    self.total_b,
            "extraction_method_a": self.extraction_method_a,
            "extraction_method_b": self.extraction_method_b,
            "is_scan_a":  self.is_scan_a,
            "is_scan_b":  self.is_scan_b,
            "warnings":   self.warnings,
            # Aliasy dla kompatybilności z app.py
            "error_count":   self.diff_count,
            "warning_count": self.warn_count,
            "checksum_a": None,
            "checksum_b": None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# WYKRYWANIE KOLUMN — serce nowego parsera
# ─────────────────────────────────────────────────────────────────────────────

def _detect_column_index(header_cells: list[str], role: str,
                          supplier_map: Optional[dict] = None,
                          exclude: Optional[set] = None) -> Optional[int]:
    """
    Znajduje indeks kolumny dla danej roli używając COLUMN_DETECTION z priorytetami.

    Jeśli supplier_map podany → szuka w nim najpierw (exact match nazwy nagłówka).
    Supplier_map format: {'Product Code': 'ref', 'Quantity/Unit...': 'qty', ...}
    exclude — zbiór indeksów kolumn do pominięcia (już zajętych przez inne role).

    Zwraca indeks kolumny lub None.
    """
    _excl = exclude or set()
    # 1. Profil dostawcy ma pierwszeństwo
    if supplier_map:
        for idx, cell in enumerate(header_cells):
            if idx in _excl:
                continue
            cell_clean = str(cell).strip()
            # Szukaj exact match i contains match
            for map_key, map_role in supplier_map.items():
                if map_role == role:
                    key_clean = str(map_key).strip()
                    if cell_clean == key_clean or key_clean in cell_clean:
                        return idx

    # 2. Auto-detekcja z priorytetami
    rules = COLUMN_DETECTION.get(role, [])
    best_idx: Optional[int] = None
    best_score = 0

    for idx, cell in enumerate(header_cells):
        if idx in _excl:
            continue
        cell_norm = str(cell).lower().replace("\n", " ").replace("\\n", " ").strip()
        for keyword, priority in rules:
            if keyword in cell_norm:
                score = len(keyword) * priority
                if score > best_score:
                    best_score = score
                    best_idx = idx

    return best_idx


def _build_column_map(header_cells: list[str],
                       supplier_map: Optional[dict] = None) -> dict[str, int]:
    """
    Buduje kompletną mapę {rola: indeks_kolumny} dla wiersza nagłówkowego.

    Zapobiega konfliktom: jeśli dwie role wskazują na tę samą kolumnę,
    wygrywa rola z wyższym priorytetem (np. 'qty' wygrywa z 'net' dla 'amount').
    """
    roles = ["ref", "qty", "price", "net", "lot", "desc", "exp", "mfg", "unit"]
    assignments: dict[str, int] = {}
    col_to_role: dict[int, tuple[str, int]] = {}  # col → (role, score)

    for role in roles:
        idx = _detect_column_index(header_cells, role, supplier_map)
        if idx is None:
            continue

        # Oblicz score dla konfliktu
        rules = COLUMN_DETECTION.get(role, [])
        cell_norm = header_cells[idx].lower().replace("\n", " ").strip()
        score = 0
        for keyword, priority in rules:
            if keyword in cell_norm:
                score = max(score, len(keyword) * priority)

        # Sprawdź konflikt
        if idx in col_to_role:
            existing_role, existing_score = col_to_role[idx]
            if score > existing_score:
                # Nowa rola wygrywa — usuń poprzednią
                del assignments[existing_role]
                assignments[role] = idx
                col_to_role[idx] = (role, score)
        else:
            assignments[role] = idx
            col_to_role[idx] = (role, score)

    # Druga tura: rola wyparta z kolumny (lub przegrana w konflikcie) próbuje zająć
    # swoją następną najlepszą WOLNĄ kolumnę, zamiast zostać pominięta — inaczej
    # np. 'net' znikał, gdy 'qty' wygrał wspólną kolumnę „amount", mimo że istniała
    # osobna kolumna wartości netto.
    taken = set(assignments.values())
    for role in roles:
        if role in assignments:
            continue
        idx = _detect_column_index(header_cells, role, supplier_map, exclude=taken)
        if idx is not None and idx not in taken:
            assignments[role] = idx
            taken.add(idx)

    return assignments


def _find_header_row(df, max_scan: int = 15) -> int:
    """
    Znajduje wiersz nagłówkowy tabeli (indeks 0-max_scan).
    Zwraca -1 jeśli nie znaleziono.

    Heurystyka: wiersz nagłówkowy zawiera co najmniej 2 słowa kluczowe kolumn.
    Używa modułowej stałej HEADER_SIGNALS (zbudowanej raz przy imporcie).
    """
    for ri in range(min(max_scan, df.shape[0])):
        row_text = " ".join(
            str(df.iloc[ri, c]).lower().replace("\n", " ")
            for c in range(df.shape[1])
        )
        hits = sum(1 for sig in HEADER_SIGNALS if sig in row_text)
        if hits >= 2:
            return ri

    return -1


# ─────────────────────────────────────────────────────────────────────────────
# NORMALIZACJA REF
# ─────────────────────────────────────────────────────────────────────────────

# FIX 13: compile _is_valid_ref() regex once at module level
_VALID_REF_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9\-_\.]{1,34}$')


def _normalize_ref_code(raw: str) -> str:
    """
    Normalizuje kod REF:
    - Usuwa prefiks numeryczny '1.' (format S.No z PI Omega Medical)
    - Normalizuje separatory
    - Wielkie litery
    """
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none", ""):
        return ""
    # Usuń prefiks numeryczny "1." lub "10."
    s = re.sub(r"^\d+\.\s*", "", s).strip()
    # Normalizuj wielokrotne spacje
    s = re.sub(r"\s+", " ", s).strip().upper()
    return s


def _is_valid_ref(val: str) -> bool:
    """Sprawdza czy wartość wygląda jak kod REF (nie nagłówek, nie pusty)."""
    if not val or len(val) < 2 or len(val) > 60:
        return False
    v = val.strip().split("\n")[0].strip()
    # FIX 13: use pre-compiled _VALID_REF_RE
    if _VALID_REF_RE.match(v):
        return v.upper() not in {
            "NO", "REF", "CODE", "ITEM", "LOT", "QTY", "PRICE",
            "NET", "TOTAL", "USD", "EUR", "PLN", "S.NO", "SN",
        }
    return False


def _row_text(row_values: list[str]) -> str:
    """Złączony, małymi literami tekst wiersza — wspólne dla detektorów total/subtotal."""
    return " ".join(str(v) for v in row_values).lower()


def _is_subtotal_row(row_values: list[str]) -> bool:
    """Czy wiersz to SUBTOTAL (podsuma) — nie może nadpisać sumy całkowitej."""
    text = _row_text(row_values)
    return any(t in text for t in
               ("subtotal", "sub total", "sub-total", "suma częściowa",
                "suma czesciowa", "podsuma"))


def _is_total_row(row_values: list[str]) -> bool:
    """Sprawdza czy wiersz to wiersz Total."""
    text = _row_text(row_values)
    if any(t in text for t in ["total:", "total :", "razem:", "suma:", "grand total"]):
        return True
    # Bez dwukropka: krótka komórka-etykieta zaczynająca się od słowa sumy
    # (np. „TOTAL", „TOTAL 12 345.00", „RAZEM"). Limit długości chroni przed
    # złapaniem opisu produktu zaczynającego się od „Total…".
    for v in row_values:
        c = str(v).strip().lower()
        if len(c) <= 25 and re.match(r"^(grand\s+total|total\s+amount|total|razem|suma)\b", c):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# UJEDNOLICONY PARSER TABEL — FIX #1
# ─────────────────────────────────────────────────────────────────────────────

def _parse_document_tables(tables: list[dict],
                             doc_type: str = "auto",
                             supplier_profile: Optional[dict] = None
                             ) -> tuple[list[dict], Optional[str]]:
    """
    Ujednolicony parser tabel PDF.

    Zastępuje stare _parse_po_tables() + _parse_pi_tables().
    Obsługuje wszystkie formaty: PO ACME, PI ALPHAMED, PI OmegaMedical, CI, PL.

    Args:
        tables:           Lista tabel z pdf_extractor.extract_tables()
        doc_type:         'PO'|'PI'|'CI'|'PL'|'auto'
        supplier_profile: Profil dostawcy z bazy (zawiera column_mappings)

    Returns:
        (items, total_str) gdzie items = lista dicts z polami:
        {ref, description, qty, price, net, lot, exp, mfg, unit}
    """
    all_items: list[dict] = []
    total_str: Optional[str] = None

    def _clean_num(s: Optional[str]) -> Optional[str]:
        # FIX 12: explicit None guard before string operations
        if s is None:
            return None
        if not s:
            return None
        return re.sub(r'^[\s$€£]+|[\s$€£]+$', '', s) or None

    # Pobierz mapowanie kolumn z profilu dostawcy jeśli dostępny
    supplier_col_map: Optional[dict] = None
    if supplier_profile:
        col_mappings = supplier_profile.get("column_mappings", {})
        if isinstance(col_mappings, str):
            import json
            try:
                col_mappings = json.loads(col_mappings)
            except Exception:
                col_mappings = {}
        # Szukaj mapy dla tego doc_type, fallback na PI lub PO
        supplier_col_map = (
            col_mappings.get(doc_type) or
            col_mappings.get("PI") or
            col_mappings.get("PO") or
            None
        )

    for tbl in tables:
        df = tbl.get("dataframe")
        if df is None:
            continue
        if not hasattr(df, "iloc"):
            # Bez pandas pdf_extractor oddaje list[list] — pominięcie musi być widoczne (#1).
            logger.warning("Pominięto tabelę (str. %s, %s): to nie DataFrame — brak pandas?",
                           tbl.get("page"), tbl.get("method"))
            continue
        if df.shape[0] < 2 or df.shape[1] < 3:
            continue

        # Znajdź wiersz nagłówkowy
        header_row_idx = _find_header_row(df)
        if header_row_idx < 0:
            continue

        # Zbuduj listę wartości nagłówka
        header_cells = [
            str(df.iloc[header_row_idx, c]).strip()
            for c in range(df.shape[1])
        ]

        # Zbuduj mapę kolumn
        ci = _build_column_map(header_cells, supplier_col_map)

        # Musimy mieć przynajmniej ref i (qty lub net)
        if "ref" not in ci:
            continue
        if "qty" not in ci and "net" not in ci:
            continue

        ref_col  = ci["ref"]
        desc_col = ci.get("desc")
        qty_col  = ci.get("qty")
        price_col = ci.get("price")
        net_col  = ci.get("net")
        lot_col  = ci.get("lot")
        exp_col  = ci.get("exp")
        mfg_col  = ci.get("mfg")
        unit_col = ci.get("unit")

        # Flagi specjalnego formatu
        ref_header = header_cells[ref_col].lower()
        is_combined_ref_desc = ("ref" in ref_header and "desc" in ref_header)
        is_sno_format = ("s.no" in ref_header or "sno" in ref_header)

        def _get(row, col_idx) -> Optional[str]:
            if col_idx is None or col_idx >= df.shape[1]:
                return None
            v = str(row.iloc[col_idx]).strip()
            v = v.replace("\n", " ").strip()
            return v if v and v.lower() not in ("nan", "none", "") else None

        # Iteruj wiersze danych (od wiersza po nagłówku)
        _grand_locked = False   # czy złapaliśmy jawny „grand total" (najwyższy priorytet)
        for ri in range(header_row_idx + 1, df.shape[0]):
            row = df.iloc[ri]
            row_vals = [str(row.iloc[c]) for c in range(df.shape[1])]

            # Wiersz Total → wyciągnij sumę
            if _is_total_row(row_vals):
                if net_col is not None:
                    raw_total = _get(row, net_col)
                    if raw_total:
                        _tv = normalize_number(raw_total.strip())
                        if _tv is not None:
                            _is_grand = any("grand total" in str(v).lower() for v in row_vals)
                            if _is_grand:
                                # Jawny grand total wygrywa i blokuje dalsze nadpisania.
                                total_str = str(_tv)
                                _grand_locked = True
                            elif _is_subtotal_row(row_vals):
                                # Subtotal NIE nadpisuje sumy — użyj tylko gdy nic
                                # innego nie złapaliśmy (ostateczność).
                                if total_str is None:
                                    total_str = str(_tv)
                            elif not _grand_locked:
                                # Inaczej trzymaj NAJWIĘKSZĄ sumę (grand total ≈ suma >
                                # subtotale), żeby subtotal po grand totalu jej nie nadpisał.
                                try:
                                    if total_str is None or float(_tv) > float(total_str):
                                        total_str = str(_tv)
                                except (TypeError, ValueError):
                                    total_str = str(_tv)
                continue

            # REF
            ref_cell = str(row.iloc[ref_col]).strip() if ref_col < df.shape[1] else ""
            ref_lines = ref_cell.replace("\\n", "\n").split("\n")
            ref_raw = ref_lines[0].strip()

            # Opis — z drugiej linii jeśli REF/Description combined
            desc = None
            if is_combined_ref_desc and len(ref_lines) > 1:
                desc = " ".join(ref_lines[1:]).strip()
            elif desc_col is not None:
                desc = _get(row, desc_col)

            # Normalizuj REF
            ref = _normalize_ref_code(ref_raw)
            if not ref or not _is_valid_ref(ref):
                continue

            # Pomijaj nagłówki w danych (błędne wiersze)
            if ref.lower() in ("product code", "ref", "s.no", "item", "description"):
                continue

            # Wyciągnij wartości
            qty_raw   = _get(row, qty_col)
            price_raw = _get(row, price_col)
            net_raw   = _get(row, net_col)
            lot_raw   = _get(row, lot_col)
            exp_raw   = _get(row, exp_col)
            mfg_raw   = _get(row, mfg_col)
            unit_raw  = _get(row, unit_col)

            # Wyczyść ceny i wartości
            qty_clean   = _clean_num(qty_raw)
            price_clean = _clean_num(price_raw)
            net_clean   = _clean_num(net_raw)

            # Pomiń wiersze bez liczb w qty lub net
            has_qty  = qty_clean and any(c.isdigit() for c in qty_clean)
            has_net  = net_clean and any(c.isdigit() for c in net_clean)
            if not has_qty and not has_net:
                continue

            all_items.append({
                "ref":         ref,
                "description": desc or "",
                "qty":         qty_clean,
                "price":       price_clean,
                "net":         net_clean,
                "lot":         lot_raw,
                "exp":         exp_raw,
                "mfg":         mfg_raw,
                "unit":        unit_raw,
            })

    return all_items, total_str


# ─────────────────────────────────────────────────────────────────────────────
# DUPLIKATY
# ─────────────────────────────────────────────────────────────────────────────

def detect_duplicates(items: list[dict]) -> list[dict]:
    """Wykrywa duplikaty i quasi-duplikaty kodów REF w liście pozycji."""
    if not items:
        return []

    issues = []
    refs = [str(it.get("ref", "")).strip() for it in items if it.get("ref")]

    from collections import Counter
    counts = Counter(refs)
    for ref, cnt in counts.items():
        if cnt > 1:
            issues.append({
                "type": "exact_duplicate",
                "refs": [ref],
                "description": f"REF '{ref}' pojawia się {cnt}× w dokumencie",
                "severity": "error",
            })

    # Fuzzy quasi-duplikaty
    unique_refs = list(counts.keys())
    try:
        from rapidfuzz import fuzz
        for i, r1 in enumerate(unique_refs):
            for r2 in unique_refs[i+1:]:
                if r1 == r2:
                    continue
                ratio = fuzz.ratio(r1, r2)
                if ratio >= 92 and len(r1) > 4:
                    issues.append({
                        "type": "fuzzy_duplicate",
                        "refs": [r1, r2],
                        "description": f"Podobne kody ({ratio}%): {r1} vs {r2} — możliwa literówka",
                        "severity": "warning",
                    })
    except ImportError:
        pass

    return issues


# ─────────────────────────────────────────────────────────────────────────────
# PORÓWNANIE POZYCJI — FIX #2 (walidacja arytmetyczna tylko dla PI/CI)
# ─────────────────────────────────────────────────────────────────────────────

def compare_items(items_a: list[dict], items_b: list[dict],
                  doc_type_a: str = "PO", doc_type_b: str = "PI",
                  supplier_profile: Optional[dict] = None
                  ) -> tuple[list[dict], list[ComparisonFinding]]:
    """
    Porównuje pozycje towarowe między dwoma dokumentami.

    Kluczowa logika cen dla pary PO+PI:
    - Jeśli net_a ≈ net_b (±0.05 USD) → różnica ceny = ZAOKRĄGLENIE → format
    - Jeśli net różne i cena różna <2% → watpliwe
    - Jeśli net różne i cena różna ≥2% → roznica/error

    Walidacja arytmetyczna qty×price=net:
    - TYLKO dla dokumentu B (PI/CI/CI_SET) — FIX #2
    - PO zaokrągla ceny → walidacja dla A generuje fałszywe alarmy
    """
    price_tol_pct = 0.0
    po_round = 2
    if supplier_profile:
        price_tol_pct = max(0.0, min(100.0, float(supplier_profile.get("price_tolerance_pct", 0.0) or 0.0)))
        _pr = supplier_profile.get("po_rounding")
        try:
            po_round = max(0, min(10, int(_pr))) if _pr is not None else 2
        except (TypeError, ValueError):
            po_round = 2

    # Przeliczniki jednostek (#4): lista reguł {ref_norm, unit_from, unit_to, factor}
    # przekazana z app.py przez supplier_profile["uom_conversions"].
    uom_conversions = (supplier_profile or {}).get("uom_conversions") or []

    is_b_invoice = doc_type_b in ("PI", "CI", "CI_SET", "FV")

    matches = match_items_semantic(items_a, items_b,
                                    fuzzy_ref_threshold=82,
                                    desc_similarity_threshold=0.50)
    compared: list[dict] = []
    findings: list[ComparisonFinding] = []

    for m in matches:
        ia       = m["item_a"]
        ib       = m["item_b"]
        mtype    = m["match_type"]
        conf     = m["confidence"]
        match_note = "" if mtype == "ref_exact" else f" [{mtype} {conf*100:.0f}%]"

        # Pozycja tylko w A
        if mtype == "only_in_a":
            ref = ia.get("ref", "?") if ia else "?"
            compared.append({
                **{k: ia.get(k, "") for k in ("ref","description","qty","price","net","lot")},
                "qty_a": ia.get("qty",""), "qty_b": "",
                "price_a": ia.get("price",""), "price_b": "",
                "net_a": ia.get("net",""), "net_b": "",
                "desc_a": ia.get("description",""), "desc_b": "",
                "lot_b": "", "status": "brak_w_b",
                "issues": ["Pozycja tylko w dok. A — brak w dok. B"],
                "match_type": mtype, "match_confidence": conf,
            })
            findings.append(ComparisonFinding(
                "pozycja", "error", f"REF {ref}", ref, None,
                f"Pozycja {ref} tylko w dok. A — brak w dok. B",
            ))
            continue

        # Pozycja tylko w B
        if mtype == "only_in_b":
            ref = ib.get("ref", "?") if ib else "?"
            compared.append({
                **{k: ib.get(k, "") for k in ("ref","description","qty","price","net","lot")},
                "qty_a": "", "qty_b": ib.get("qty",""),
                "price_a": "", "price_b": ib.get("price",""),
                "net_a": "", "net_b": ib.get("net",""),
                "desc_a": "", "desc_b": ib.get("description",""),
                "lot_b": ib.get("lot",""), "status": "brak_w_a",
                "issues": ["Pozycja tylko w dok. B — brak w dok. A"],
                "match_type": mtype, "match_confidence": conf,
            })
            findings.append(ComparisonFinding(
                "pozycja", "error", f"REF {ref}", None, ref,
                f"Pozycja {ref} tylko w dok. B — brak w dok. A",
            ))
            continue

        # Para dopasowana
        ref = (ia.get("ref") or "") or (ib.get("ref") or "")
        issues: list[str] = []
        status = "ok"

        # --- Jednostka miary + Ilość (z auto-przeliczaniem #4) ---
        ua = str(ia.get("unit") or "").strip().upper()
        ub = str(ib.get("unit") or "").strip().upper()
        qa = str(ia.get("qty") or "")
        qb = str(ib.get("qty") or "")

        # Gdy jednostki różne, spróbuj przeliczyć ilość A na jednostkę B.
        _uom_converted = False
        _uom_factor = None
        _qa_cmp = qa
        if uom_conversions and ua and ub and ua != ub and qa:
            try:
                # convert_qty / normalize_number są importowane na poziomie modułu —
                # tu jesteśmy w pętli per-pozycja, więc nie powtarzamy importów.
                _qav = normalize_number(qa)
                if _qav is not None:
                    _conv, _f = convert_qty(float(_qav), ua, ub, ref, uom_conversions)
                    if _conv is not None:
                        _qa_cmp = str(_conv)
                        _uom_converted = True
                        _uom_factor = _f
                        issues.append(f"Przeliczono ilość: {qa} {ua} = {_conv:g} {ub} (×{_f:g})")
            except Exception:
                pass

        if qa and qb:
            eq_qty = numbers_equal(_qa_cmp, qb)
            if eq_qty is False:
                status = "roznica"
                diff = format_number_diff(_qa_cmp, qb)
                _shown_a = f"{_qa_cmp} {ub} (z {qa} {ua})" if _uom_converted else qa
                issues.append(f"Ilość: {_shown_a} → {qb} ({diff})")
                findings.append(ComparisonFinding(
                    "ilosc", "error", f"Ilość {ref}", _shown_a, qb,
                    f"Rozbieżność ilości{match_note}: {_shown_a} → {qb} ({diff})",
                ))

        # Różna jednostka BEZ dostępnego przelicznika → ostrzeżenie (jak dawniej).
        if ua and ub and ua != ub and not _uom_converted:
            if status == "ok":
                status = "watpliwe"
            issues.append(f"Jednostka: {ua} → {ub}")
            findings.append(ComparisonFinding(
                "jednostka", "warning", f"JM {ref}", ua, ub,
                f"Różna jednostka miary{match_note}: {ua} ≠ {ub}",
            ))

        # --- Wartość netto (oblicz najpierw — potrzebna do oceny ceny) ---
        na = str(ia.get("net") or "")
        nb = str(ib.get("net") or "")
        net_agrees = False
        if na and nb:
            try:
                _na = normalize_number(na)
                _nb = normalize_number(nb)
                if _na is not None and _nb is not None:
                    _na_f, _nb_f = float(_na), float(_nb)
                    _bigger = max(abs(_na_f), abs(_nb_f))
                    # Zgodność netto wymaga DOKŁADNEJ równości — nie używamy tu
                    # tolerancji CENY, bo maskowałaby realną różnicę wartości netto
                    # (a tym samym tłumiła kontrolę netto poniżej, opartą o `not
                    # net_agrees`). Faktyczne zaokrąglenia i tak wyłapuje
                    # numbers_differ_only_by_rounding w sekcji „Net value".
                    net_agrees = (_bigger == 0) or (_na_f == _nb_f)
            except Exception:
                net_agrees = na.strip() == nb.strip()

        # --- Cena ---
        pa = re.sub(r'^[\s$€£]+|[\s$€£]+$', '', str(ia.get("price") or ""))
        pb = re.sub(r'^[\s$€£]+|[\s$€£]+$', '', str(ib.get("price") or ""))
        # Gdy ilość przeliczono między jednostkami (np. kartony→szt.), cena A jest
        # wyrażona PER jednostkę A. Sprowadzamy ją do jednostki B (÷ współczynnik),
        # żeby porównanie cen nie pokazywało fałszywej różnicy wynikającej z różnicy
        # jednostek (np. 1000/karton vs 1,00/szt. przy 1 karton = 1000 szt.).
        pa_cmp = pa
        if _uom_converted and _uom_factor:
            try:
                _pav = normalize_number(pa)
                if _pav is not None and _uom_factor:
                    pa_cmp = f"{float(_pav) / float(_uom_factor):.6f}"
            except Exception:
                pa_cmp = pa
        if pa and pb:
            eq_price = numbers_equal(pa_cmp, pb, price_tol_pct)

            if eq_price is False:
                try:
                    _pan = normalize_number(pa_cmp)
                    _pbn = normalize_number(pb)
                    if _pan is None or _pbn is None:
                        raise ValueError("unparseable price")
                    _pa = float(_pan)
                    _pb = float(_pbn)
                    diff_pct_price = abs(_pa - _pb) / max(abs(_pa), abs(_pb), 0.0001) * 100
                except Exception:
                    diff_pct_price = 99.0

                # Wartość do pokazania: po konwersji jednostki dodaj kontekst.
                _pa_show = f"{pa_cmp} {ub} (z {pa} {ua})" if _uom_converted else pa

                if net_agrees:
                    # Net się zgadza → różnica ceny = zaokrąglenie PO do 2 miejsc → FORMAT
                    if status == "ok":
                        status = "format"
                    issues.append(f"Cena: PO={_pa_show} PI/CI={pb} — zaokrąglenie (net zgodny: {na})")

                elif diff_pct_price < 0.5 or numbers_differ_only_by_rounding(pa_cmp, pb, po_round):
                    if status == "ok":
                        status = "format"
                    # Nie wyciszaj całkowicie drobnej rozbieżności ceny — zostaw
                    # ślad informacyjny, by nie znikła bez śladu z raportu.
                    diff_str = format_number_diff(pa_cmp, pb)
                    issues.append(f"Cena: {_pa_show} → {pb} ({diff_str}) — drobna różnica (≈zaokrąglenie)")

                elif diff_pct_price < 2.0:
                    if status == "ok":
                        status = "watpliwe"
                    diff_str = format_number_diff(pa_cmp, pb)
                    issues.append(f"Cena: {_pa_show} → {pb} ({diff_str})")
                    findings.append(ComparisonFinding(
                        "cena", "warning", f"Cena {ref}", _pa_show, pb,
                        f"Rozbieżność ceny{match_note}: {_pa_show} → {pb} ({diff_str})",
                    ))
                else:
                    status = "roznica"
                    diff_str = format_number_diff(pa_cmp, pb)
                    issues.append(f"Cena: {_pa_show} → {pb} ({diff_str})")
                    findings.append(ComparisonFinding(
                        "cena", "error", f"Cena {ref}", _pa_show, pb,
                        f"Rozbieżność ceny{match_note}: {_pa_show} → {pb} ({diff_str})",
                    ))

            elif eq_price is None and pa_cmp and pb and pa_cmp != pb:
                if numbers_differ_only_by_rounding(pa_cmp, pb, po_round):
                    if status == "ok":
                        status = "format"

        # --- Net value ---
        if na and nb and not net_agrees:
            eq_net = numbers_equal(na, nb, price_tol_pct)
            if eq_net is False:
                if not numbers_differ_only_by_rounding(na, nb, po_round):
                    status = "roznica"
                    diff_str = format_number_diff(na, nb)
                    issues.append(f"Net: {na} → {nb} ({diff_str})")
                    findings.append(ComparisonFinding(
                        "wartosc", "error", f"Net {ref}", na, nb,
                        f"Rozbieżność wartości netto{match_note}: {na} → {nb}",
                    ))
                elif status == "ok":
                    status = "format"
        elif na and not nb:
            issues.append(f"Net brak w B (A={na})")
            if status == "ok":
                status = "format"
        elif nb and not na:
            issues.append(f"Net brak w A (B={nb})")
            if status == "ok":
                status = "format"

        # --- Opis (info) ---
        desc_a = str(ia.get("description") or "").strip()
        desc_b = str(ib.get("description") or "").strip()
        desc_note = ""
        if desc_a and desc_b and desc_a.lower() != desc_b.lower():
            desc_note = f'Opis A: "{desc_a[:50]}" | B: "{desc_b[:50]}"'

        compared.append({
            "ref":     ref,
            "desc_a":  desc_a,  "desc_b":  desc_b,
            "qty_a":   qa,      "qty_b":   qb,
            "price_a": pa,      "price_b": pb,
            "net_a":   na,      "net_b":   nb,
            # A-side lot/exp/mfg stored for completeness/export; the trade
            # comparison UI currently only displays the B-side (invoice) values.
            "lot_a":   ia.get("lot") or "",
            "exp_a":   ia.get("exp") or "",
            "mfg_a":   ia.get("mfg") or "",
            "lot_b":   ib.get("lot") or "",
            "exp_b":   ib.get("exp") or "",
            "mfg_b":   ib.get("mfg") or "",
            "status":  status,
            "issues":  issues,
            "desc_note": desc_note,
            "match_type": mtype,
            "match_confidence": conf,
        })

    # --- Walidacja arytmetyczna qty×price=net — TYLKO dla dok. B (FIX #2) ---
    if is_b_invoice:
        for item in compared:
            try:
                qb_str = str(item.get("qty_b") or "").strip()
                pb_str = re.sub(r'^[\s$€£]+|[\s$€£]+$', '', str(item.get("price_b") or ""))
                nb_str = re.sub(r'^[\s$€£]+|[\s$€£]+$', '', str(item.get("net_b") or ""))
                if not qb_str or not pb_str or not nb_str:
                    continue
                qb_f = float(normalize_number(qb_str) or 0)
                pb_f = float(normalize_number(pb_str) or 0)
                nb_f = float(normalize_number(nb_str) or 0)
                if qb_f > 0 and pb_f > 0 and nb_f > 0:
                    calc = round(qb_f * pb_f, 2)
                    diff_pct = abs(calc - nb_f) / nb_f * 100 if nb_f else 0
                    if diff_pct > 2.0:
                        item.setdefault("arithmetic_issues", []).append(
                            f"[B] {qb_f}×{pb_f}={calc}≠{nb_f} (Δ{diff_pct:.1f}%)"
                        )
                        findings.append(ComparisonFinding(
                            "arytmetyka", "warning",
                            f"Błąd arytm. {item.get('ref','?')} [B]",
                            str(calc), str(nb_f),
                            f"qty×price≠net: {qb_f}×{pb_f}={calc} ≠ {nb_f} (Δ{diff_pct:.1f}%)",
                        ))
            except (ValueError, TypeError, ZeroDivisionError):
                pass

    return compared, findings


# ─────────────────────────────────────────────────────────────────────────────
# PORÓWNANIE NAGŁÓWKÓW — FIX #4, #5, #6
# ─────────────────────────────────────────────────────────────────────────────

HEADER_PATTERNS: dict[str, list[str]] = {
    "Numer PO": [
        r"\b(45\d{8})\b",
        r"(?:purchase\s+order|order)\s*(?:no\.?|nr\.?|number)?\s*[:\s]+([0-9]{7,12})",
        r"customer\s+order\s*(?:no\.?|nr\.?)\s*[:\s]+([0-9]{7,12})",
    ],
    "Nr PI dostawcy": [
        r"\b([A-Z]\d{3}-\d{6,9})\b",                       # np. A521-25080030
        r"invoice\s+no\.?\s*[:\s]+([A-Z0-9][A-Z0-9\-\./]{3,28})",
        r"proforma\s+invoice\s*(?:no\.?)?\s*[:\n]+([A-Z0-9][A-Z0-9\-\./]{3,28})",
    ],
    "Data dokumentu": [
        r"^Date\s*[:\s]+(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
        r"Invoice\s+Date\s*[:\s]+([^\n]{5,25})",
        r"(\d{1,2}[./]\d{1,2}[./]\d{4})",
    ],
    "Data dostawy": [
        r"Required\s+shipping\s+date\s*[:\s]+(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
        r"Delivery\s+Date\s*[:\s]+([^\n]{5,25})",
        r"DELIVERY\s+DATE[:\s]+([^\n]{5,25})",
    ],
    "Waluta": [
        # 1) Jawna etykieta „Currency:".
        r"\bCurrency\s*[:\s]+(USD|EUR|PLN|GBP|CNY|CHF)\b",
        # 2) Kod waluty przy słowie kwoty (Total/Amount/Price/Value/in …).
        r"(?:Total|Amount|Grand\s+Total|Sum|Price|Value|in)\b[^\n]{0,15}?\b(USD|EUR|PLN|CNY|GBP|CHF)\b",
        # 3) Kod waluty bezpośrednio przy liczbie (USD 1,234.56 / 1,234.56 USD).
        r"\b(USD|EUR|PLN|CNY|GBP|CHF)\b\s*[\d.,]{2,}",
        r"[\d.,]{2,}\s*\b(USD|EUR|PLN|CNY|GBP|CHF)\b",
        # 4) Pełne nazwy (rzadko w adresach). NIE 'EUR' luzem — wymagamy 'EURO'.
        r"\b(U\.?\s?S\.?\s*DOLLARS?|US\s*DOLLARS?|EURO\b|RENMINBI|YUAN|POUNDS?)\b",
    ],
    "Warunki dostawy": [
        # Po etykiecie MUSI stać prawdziwy incoterm (wcześniej [A-Z]{3} pod IGNORECASE
        # łapał 3 dowolne litery → np. "Final destination").
        r"Terms?\s+of\s+delivery\s*[:\s]*((?:FOB|CIF|EXW|DAP|CFR|CPT|DDP|FCA|FAS|CIP|DDU|DAT)\b[^\n]{0,25})",
        r"\b((?:FOB|CIF|EXW|DAP|CFR|CPT|DDP|FCA|FAS|CIP|DDU|DAT)\s+[A-Z][^\n]{0,25})",
        r"\b(FOB|CIF|EXW|DAP|CFR|CPT|DDP|FCA|FAS|CIP|DDU|DAT)\b",
    ],
    "Warunki płatności": [
        # Etykieta + wartość MUSI być w tej samej linii (bez przeskoku przez \n,
        # inaczej "Payment Terms:\nTerms of Delivery:" łapie kolejną etykietę).
        r"Terms?\s+of\s+payment[ \t]*:?[ \t]*([^\n]{5,80})",
        r"Payment\s+Terms?[ \t]*:?[ \t]*([^\n]{5,80})",
        # Wzorce „po wartości" — łapią termin nawet gdy etykieta jest gdzie indziej.
        r"((?:\d{1,3}\s*%\s*)?(?:T/T|L/C|D/P|D/A)\s*[^\n]{0,40}?\d+\s*days?[^\n]{0,25})",
        r"(\d+\s*days?\s+after\s+(?:shipment|departure|dispatch|delivery|b/?l)[^\n]{0,20})",
        r"\b((?:IW\d{2}|IZ\d{2})\b)",
    ],
    "Port załadunku": [
        r"Port\s+of\s+(?:Loading|Departure)\s*[:\s]+([^\n]{2,40})",
    ],
    "Port rozładunku": [
        r"Port\s+of\s+(?:Discharge|Destination)\s*[:\s]+([^\n]{2,40})",
    ],
    "ETD": [
        r"ETD\s*[:\s]+([^\n]{4,25})",
        r"Estimated\s+[Tt]ime\s+of\s+[Dd]eparture\s*[:\s]+([^\n]{4,25})",
        r"Shipment\s+Date\s*[:\s]+([^\n]{4,25})",
        r"Ship\s+Date\s*[:\s]+([^\n]{4,25})",
        r"Date\s+of\s+[Ss]hipment\s*[:\s]+([^\n]{4,25})",
    ],
    "ETA": [
        r"ETA\s*[:\s]+([^\n]{4,25})",
        r"Estimated\s+[Tt]ime\s+of\s+[Aa]rrival\s*[:\s]+([^\n]{4,25})",
        r"Arrival\s+Date\s*[:\s]+([^\n]{4,25})",
        r"Expected\s+[Aa]rrival\s*[:\s]+([^\n]{4,25})",
    ],
}


def _normalize_currency(v: str) -> str:
    """Sprowadza zapisy waluty do kodu ISO (U.S DOLLARS → USD itd.)."""
    u = re.sub(r"[.\s]", "", v.upper())
    if u.startswith(("USD", "USDOLLAR", "DOLLAR")):
        return "USD"
    if u.startswith(("EUR", "EURO")):
        return "EUR"
    if u.startswith(("PLN", "ZLOTY", "ZŁOTY")):
        return "PLN"
    if u.startswith(("CNY", "RENMINBI", "YUAN", "RMB")):
        return "CNY"
    if u.startswith(("GBP", "POUND")):
        return "GBP"
    if u.startswith("CHF"):
        return "CHF"
    return v.upper()[:6]


def extract_header_fields(text: str, doc_type: str = "auto") -> dict[str, str]:
    """Wyciąga pola nagłówkowe z tekstu dokumentu przez regex."""
    result: dict[str, str] = {}
    for name, patterns in HEADER_PATTERNS.items():
        for p in patterns:
            m = re.search(p, text, re.IGNORECASE | re.MULTILINE)
            if m:
                # BUGFIX: wzorzec bez grupy przechwytującej → group(1) rzuca; użyj całości
                val = (m.group(1) if m.lastindex else m.group(0)).strip().rstrip(".,;")
                if name == "Waluta":
                    val = _normalize_currency(val)
                if val and len(val) >= 2:
                    result[name] = val
                    break
    return result


def _is_acme_address(text: str) -> bool:
    """Sprawdza czy tekst zawiera któryś ze znanych adresów ACME."""
    t = text.lower()
    return any(addr in t for addr in ACME_ADDRESSES)


def _expand_sap_code(code: str) -> list[str]:
    """Zwraca listę ekwiwalentów tekstowych dla kodu SAP lub pusty list."""
    return SAP_PAYMENT_MAP.get(code.upper(), [])


def _payment_terms_severity(va: str, vb: str) -> str:
    """
    Ocenia czy rozbieżność warunków płatności to WARNING czy ERROR.

    ERROR tylko gdy:
    - Jeden dokument wymaga zaliczki (advance), drugi nie
    - Różnica w dniach > 30 (np. 30 vs 90)
    """
    va_l = va.lower()
    vb_l = vb.lower()

    has_advance_a = any(w in va_l for w in ["advance", "adv", "prepay", "in advance"])
    has_advance_b = any(w in vb_l for w in ["advance", "adv", "prepay", "in advance"])
    if has_advance_a != has_advance_b:
        return "error"  # Jeden wymaga zaliczki, drugi nie

    # Wyciągnij WSZYSTKIE liczby dni (termin może mieć kilka, np.
    # "30 days advance, 60 days balance") — branie tylko pierwszej maskowałoby
    # realną różnicę.
    def _days(s):
        return [int(m) for m in re.findall(r"(\d+)\s*days?", s, re.IGNORECASE)]

    days_a = _days(va)
    days_b = _days(vb)
    # is not None — inaczej "0 days" (płatność natychmiastowa) traktowane jak brak,
    # więc "0 days" vs "60 days" nie eskaluje do error. Porównujemy największą
    # rozbieżność między dowolnymi dniami z A i B.
    if days_a and days_b:
        max_gap = max(abs(a - b) for a in days_a for b in days_b)
        if max_gap > 30:
            return "error"

    return "warning"  # FIX #6 — domyślnie WARNING nie ERROR


def compare_header_fields(fields_a: dict, fields_b: dict,
                           supplier_profile: Optional[dict] = None
                           ) -> tuple[list[dict], list[ComparisonFinding]]:
    """
    Porównuje pola nagłówkowe dwóch dokumentów.

    Zwraca (headers_list, findings_list).
    """
    # Mapowanie płatności z profilu dostawcy
    extra_payment_map: dict[str, str] = {}
    if supplier_profile:
        pt = supplier_profile.get("payment_terms_map", {}) or {}
        if isinstance(pt, dict):
            extra_payment_map = pt

    headers: list[dict] = []
    findings: list[ComparisonFinding] = []
    all_keys = sorted(set(fields_a.keys()) | set(fields_b.keys()))

    for key in all_keys:
        va = str(fields_a.get(key) or "").strip()
        vb = str(fields_b.get(key) or "").strip()
        if not va and not vb:
            continue

        if not va:
            headers.append({"key": key, "val_a": "", "val_b": vb,
                             "status": "brak_w_a", "comment": "Tylko w dok. B"})
            continue
        if not vb:
            headers.append({"key": key, "val_a": va, "val_b": "",
                             "status": "brak_w_b", "comment": "Tylko w dok. A"})
            continue

        kl = key.lower()
        status  = "ok"
        comment = ""

        # === Numer PO ===
        if "numer po" in kl:
            if va.strip() == vb.strip():
                status = "ok"
            else:
                status = "roznica"
                comment = f"{va} ≠ {vb}"
                findings.append(ComparisonFinding(
                    "numer", "error", key, va, vb,
                    f"Numer PO różny: {va} ≠ {vb} — KRYTYCZNY"
                ))

        # === Daty ===
        elif "data" in kl or "date" in kl:
            da = normalize_date(va)
            db_ = normalize_date(vb)
            if da and db_:
                if da == db_:
                    if va != vb:
                        status  = "format"
                        comment = f"Ta sama data, różny format: {va} = {vb}"
                    else:
                        status = "ok"
                elif dates_possibly_equal(va, vb):
                    # Ta sama data możliwa po zamianie D/M↔M/D (dostawca US/CN z
                    # zapisem MM/DD) — nie twierdź twardo „różne", oznacz do weryfikacji.
                    status  = "watpliwe"
                    comment = f"Możliwa rozbieżność formatu daty (D/M vs M/D): {va} vs {vb}"
                    findings.append(ComparisonFinding(
                        "data", "warning", key, va, vb,
                        f"{key}: niejednoznaczny format daty — {va} vs {vb} (sprawdź D/M vs M/D)"
                    ))
                else:
                    status  = "roznica"
                    comment = f"Różne daty: {va} vs {vb}"
                    findings.append(ComparisonFinding(
                        "data", "warning", key, va, vb,
                        f"{key}: różne daty — {va} vs {vb}"
                    ))
            elif va.lower().replace(" ", "") == vb.lower().replace(" ", ""):
                status = "ok"
            else:
                status  = "roznica"
                comment = f"{va} ≠ {vb}"

        # === Warunki płatności === (FIX #4 i #6)
        elif "płatno" in kl or "payment" in kl:
            # Czy jeden jest kodem SAP, a drugi jego rozwinięciem?
            def _matches_sap(code_val: str, text_val: str) -> bool:
                expansions = _expand_sap_code(code_val)
                if not expansions:
                    return False
                text_lower = text_val.lower()
                return any(exp.lower() in text_lower for exp in expansions)

            if va.upper() == vb.upper():
                status = "ok"
            elif _matches_sap(va, vb) or _matches_sap(vb, va):
                # Kod SAP = jego rozwinięcie → format
                status  = "format"
                comment = f"SAP {va} = '{vb}'"
            elif payment_terms_equal(va, vb, extra_payment_map):
                status = "ok"
            else:
                # Faktycznie różne — FIX #6: severity zależy od typu rozbieżności
                sev = _payment_terms_severity(va, vb)
                status  = "roznica"
                comment = f'PO: "{va}" ≠ PI: "{vb}"'
                findings.append(ComparisonFinding(
                    "warunki", sev, key, va, vb,
                    f"Warunki płatności różnią się: PO=\"{va}\" vs PI=\"{vb}\"",
                    suggestion="Sprawdź czy różnica wynika z wewnętrznego kodu SAP."
                ))

        # === Waluta ===
        elif "walut" in kl or "currency" in kl:
            if va.upper() == vb.upper():
                status = "ok"
            else:
                status  = "roznica"
                comment = f"{va} ≠ {vb}"
                findings.append(ComparisonFinding(
                    "waluta", "error", key, va, vb,
                    f"Różna waluta: {va} ≠ {vb} — KRYTYCZNY błąd rozliczeniowy"
                ))

        # === Warunki dostawy (Incoterms) ===
        elif any(w in kl for w in ("incoterms", "terms of delivery", "trade terms")) or \
             (("dostawy" in kl or "delivery" in kl) and
              not any(w in kl for w in ("adres", "address", "ship", "miejsce"))):
            _INC = r"\b(FOB|CIF|EXW|DAP|CFR|CPT|DDP|FCA|FAS|CIP|DDU|DAT)\b"
            inc_a = re.search(_INC, va.upper())
            inc_b = re.search(_INC, vb.upper())
            if inc_a and inc_b:
                if inc_a.group() == inc_b.group():
                    status  = "ok"
                    comment = f"Incoterms zgodne: {inc_a.group()}"
                else:
                    status  = "roznica"
                    findings.append(ComparisonFinding(
                        "incoterms", "warning", key, va, vb,
                        f"Różne Incoterms: {inc_a.group()} ≠ {inc_b.group()}"
                    ))
            elif re.sub(r"\s+", " ", va.strip().upper()) == re.sub(r"\s+", " ", vb.strip().upper()):
                # Pełna równość (po normalizacji spacji) zamiast pierwszych 10 znaków —
                # „DELIVERED TO WAREHOUSE A" ≠ „…B" nie jest już mylnie uznawane za zgodne.
                status = "ok"
            else:
                status  = "watpliwe"
                comment = f"{va} ≠ {vb}"

        # === Adresy — FIX #5 ===
        elif any(w in kl for w in ("adres", "address", "ship to", "ship-to",
                                    "odbiorca", "consignee", "dostawy")):
            # Sprawdź oba adresy — jeśli oba są ACME → ok
            if _is_acme_address(va) and _is_acme_address(vb):
                status  = "ok"
                comment = "Oba adresy należą do ACME"
            # Jeden jest ACME, drugi nie → watpliwe (może być adres dostawcy)
            elif _is_acme_address(va) or _is_acme_address(vb):
                if va.lower().strip() == vb.lower().strip():
                    status = "ok"
                else:
                    status  = "watpliwe"
                    comment = "Różne adresy — sprawdź czy to prawidłowy adres dostawy"
            elif va.lower().strip() == vb.lower().strip():
                status = "ok"
            else:
                status  = "watpliwe"
                comment = f"{va[:40]} ≠ {vb[:40]}"

        # === Kwoty sumaryczne ===
        elif any(w in kl for w in ("total", "amount", "value", "suma")):
            eq = numbers_equal(va, vb)
            if eq is True:
                status = "ok"
            elif eq is False:
                status = "roznica"
                findings.append(ComparisonFinding(
                    "kwota", "error", key, va, vb,
                    f"{key}: {va} ≠ {vb}"
                ))
            else:
                status = "watpliwe"

        # === Ogólne ===
        else:
            if va.strip().lower() == vb.strip().lower():
                status = "ok"
            else:
                eq = numbers_equal(va, vb)
                if eq is True:
                    status  = "format"
                    comment = "Ta sama wartość, różny format"
                elif eq is False:
                    status  = "roznica"
                    comment = f"{va} ≠ {vb}"
                else:
                    status  = "roznica"
                    comment = f"{va} ≠ {vb}"

        headers.append({"key": key, "val_a": va, "val_b": vb,
                         "status": status, "comment": comment})

    return headers, findings


# ─────────────────────────────────────────────────────────────────────────────
# DETEKCJA I↔1
# ─────────────────────────────────────────────────────────────────────────────

_CHAR_SUBS: list[tuple[str, str]] = [
    ("I", "1"), ("1", "I"),
    ("O", "0"), ("0", "O"),
    # Tekst jest porównywany po .upper(), więc 'l' nigdy nie wystąpi — używamy
    # tylko wielkiego 'L' (typowa pomyłka L/1).
    ("L", "1"), ("1", "L"),
    ("S", "5"), ("5", "S"),
    ("B", "8"), ("8", "B"),
]

def check_i1(text_a: str, text_b: str) -> list[ComparisonFinding]:
    """Wykrywa krytyczne zamiany znaków I↔1, O↔0 w numerach dokumentów i LOT."""
    findings: list[ComparisonFinding] = []
    # Token alfanumeryczny 8–16 znaków zawierający min. 1 cyfrę — łapie też czysto
    # numeryczne numery PO (np. 4500001234), które wzorzec „litery-potem-cyfry"
    # pomijał, a to właśnie na nich zależy przy zamianie O↔0 / I↔1 / S↔5.
    pattern = r"\b((?=[A-Z0-9]*\d)[A-Z0-9]{8,16})\b"
    nums_a = set(re.findall(pattern, text_a.upper()))
    nums_b = set(re.findall(pattern, text_b.upper()))

    for na in nums_a:
        if na in nums_b:
            continue
        for nb in nums_b:
            if len(na) != len(nb):
                continue
            diffs = [(a, b) for a, b in zip(na, nb) if a != b]
            if 0 < len(diffs) <= 2:
                all_subs = all((a, b) in _CHAR_SUBS for a, b in diffs)
                if all_subs:
                    desc = ", ".join(f"{a}→{b}" for a, b in diffs)
                    findings.append(ComparisonFinding(
                        "I_vs_1", "error", "Numer dokumentu", na, nb,
                        f"KRYTYCZNA zamiana znaków: {na} vs {nb} ({desc})",
                        suggestion=f"Sprawdź czy {na} = {nb} — błąd celny!",
                    ))
    return findings


# ─────────────────────────────────────────────────────────────────────────────
# GŁÓWNA FUNKCJA — FIX #3 (usunięto duplikat wywołania parsera)
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_db(db):
    """Zwraca połączenie z bazą — przekazane jawnie lub z kontekstu aplikacji."""
    if db is not None:
        return db
    try:
        from db import get_db
        return get_db()
    except Exception:
        return None


def _make_cn_lookup(db):
    """Buduje callable(ref) -> tariff_cn z material_master (lub None)."""
    if db is None:
        return None
    try:
        import material_master
    except Exception:
        return None

    def _lookup(ref):
        row = material_master.get_material(db, ref)
        if not row:
            return None
        return row.get("tariff_cn") or ""

    return _lookup


def _make_cn_reference_lookup(db):
    """Wczytuje lokalna tabele referencyjna TARIC (cn_reference) do pamieci i
    zwraca callable(code) -> dict | None. Zwraca None, gdy tabeli brak lub jest
    pusta — wtedy walidacja istnienia kodu jest pomijana (a nie raportuje bledow).
    Wczytanie raz unika zapytania per-pozycja."""
    if db is None:
        return None
    try:
        rows = db.execute(
            "SELECT code, description, duty_rate, restrictions, active FROM cn_reference"
        ).fetchall()
    except Exception:
        return None
    table = {}
    for r in rows:
        try:
            table[str(r["code"])] = {
                "description": r["description"],
                "duty_rate": r["duty_rate"],
                "restrictions": r["restrictions"],
                "active": r["active"],
            }
        except Exception:
            continue
    if not table:
        return None
    return lambda code: table.get(str(code))


def _audit_cn_codes(result: "EnhancedResult", text_a: str, text_b: str,
                    dt_a: str, dt_b: str, db) -> None:
    """
    Regula E8-01 — kod CN z SAD vs material_master.tariff_cn.

    Mutuje pozycje wyniku (cn_sad/cn_master/cn_status) i dopisuje findingi:
      * niezgodny kod CN  -> 'error' (BŁĄD, pole krytyczne)
      * brak wzorca / brak kodu w SAD -> 'warning' (UWAGA)
    """
    import cn_audit

    sad_is_b = (dt_b == "SAD")
    sad_text = text_b if sad_is_b else text_a
    resolved = _resolve_db(db)
    cn_lookup = _make_cn_lookup(resolved)
    ref_lookup = _make_cn_reference_lookup(resolved)

    findings = cn_audit.audit_items_cn(result.items, sad_text, sad_is_b,
                                       cn_lookup, ref_lookup)
    for f in findings:
        status = f["status"]
        ref = f["ref"] or "?"
        # Niezgodnosc kodu vs master (logika dotychczasowa)
        if status != "ok":
            severity = "error" if status == "blad" else "warning"
            result.findings.append(ComparisonFinding(
                "kod_cn", severity, f"Kod CN {ref}",
                f["sad_cn"], f["master_cn"], f["description"],
                suggestion="Zweryfikuj klasyfikację taryfową w SAD wobec karty produktu",
            ))
        # Wadliwy ZAPIS kodu — sygnalizujemy nawet przy zgodnosci z masterem
        for fi in f.get("format_issues", []):
            sev = "error" if fi["status"] == "blad" else "warning"
            result.findings.append(ComparisonFinding(
                "kod_cn_format", sev, f"Format kodu CN {ref} ({fi['side']})",
                f["sad_cn"], f["master_cn"], fi["note"],
                suggestion="Popraw zapis kodu CN/TARIC (6/8/10 cyfr, bez liter)",
            ))
        # Kod spoza taryfy referencyjnej lub z ograniczeniami (gdy wczytano tabele)
        ri = f.get("reference_issue")
        if ri:
            result.findings.append(ComparisonFinding(
                "kod_cn_taryfa", "warning", f"Taryfa CN {ref}",
                f["sad_cn"], "", ri["note"],
                suggestion="Zweryfikuj kod wobec aktualnej taryfy celnej (TARIC/ISZTAR)",
            ))


def compare_enhanced(path_a: str, path_b: str,
                     type_hint_a: str = "auto",
                     type_hint_b: str = "auto",
                     supplier_profile: Optional[dict] = None,
                     db=None) -> EnhancedResult:
    """
    Główna funkcja porównania dwóch dokumentów PDF.

    Args:
        path_a:           Ścieżka do dokumentu A (zazwyczaj PO)
        path_b:           Ścieżka do dokumentu B (zazwyczaj PI/CI)
        type_hint_a:      Typ A: 'PO'|'PI'|'CI'|'PL'|'SAD'|'BL'|'auto'
        type_hint_b:      Typ B: jak wyżej
        supplier_profile: Profil dostawcy z bazy (lub None)
        db:               Połączenie z bazą dla audytu kodu CN (E8-01) wobec
                          material_master. Gdy None — rozwiązywane leniwie z
                          kontekstu aplikacji (db.get_db).

    Returns:
        EnhancedResult z pozycjami, nagłówkami i listą znalezisk
    """
    from pdf_extractor import extract_text, extract_tables, detect_doc_type

    result = EnhancedResult()

    # --- Ekstrakcja tekstu ---
    ta = extract_text(path_a)
    tb = extract_text(path_b)
    text_a = ta["text"]
    text_b = tb["text"]
    result.extraction_method_a = ta["method"]
    result.extraction_method_b = tb["method"]
    result.is_scan_a = ta["is_scan"]
    result.is_scan_b = tb["is_scan"]

    if ta["is_scan"]:
        result.warnings.append(f"Dok. A: skan (OCR {ta['confidence']*100:.0f}%)")
    if tb["is_scan"]:
        result.warnings.append(f"Dok. B: skan (OCR {tb['confidence']*100:.0f}%)")

    # --- Typ dokumentu ---
    dt_a = type_hint_a if type_hint_a != "auto" else detect_doc_type(text_a)[0]
    dt_b = type_hint_b if type_hint_b != "auto" else detect_doc_type(text_b)[0]
    result.doc_type_a = dt_a
    result.doc_type_b = dt_b

    # --- Profil dostawcy ---
    if not supplier_profile:
        try:
            from supplier_profiles import detect_supplier
            supplier_profile = detect_supplier(text_a + " " + text_b)
        except Exception:
            import logging as _log
            _log.getLogger(__name__).debug("detect_supplier failed", exc_info=True)

    # --- Ekstrakcja tabel ---
    tbls_a = extract_tables(path_a)
    tbls_b = extract_tables(path_b)

    # --- Parsowanie pozycji — FIX #3: JEDNO wywołanie per dokument ---
    items_a, total_a = _parse_document_tables(tbls_a, dt_a, supplier_profile)
    items_b, total_b = _parse_document_tables(tbls_b, dt_b, supplier_profile)

    if total_a:
        result.total_a = total_a
    if total_b:
        result.total_b = total_b

    # --- Quality gate: tabele wyekstrahowane ale 0 pozycji po parsowaniu ---
    # Gdy Camelot/fitz zwrócił tabele ale parsowanie nic nie znalazło
    # (śmieciowe tabele, scalane komórki, zły układ), spróbuj Vision.
    from pdf_extractor import extract_tables_vision
    for _which, _path, _tbls, _items, _dt in [
        ("A", path_a, tbls_a, items_a, dt_a),
        ("B", path_b, tbls_b, items_b, dt_b),
    ]:
        # Pomiń jeśli: już są pozycje, 0 tabel (Vision był już wywołany w
        # extract_tables), lub Vision był metodą (unikaj podwójnego wywołania)
        if _items:
            continue
        if not _tbls:
            continue
        if any(t.get("method") == "claude_vision" for t in _tbls):
            continue
        vision_tbls = extract_tables_vision(_path)
        if not vision_tbls:
            continue
        vision_items, vision_total = _parse_document_tables(vision_tbls, _dt, supplier_profile)
        if not vision_items:
            continue
        if _which == "A":
            items_a = vision_items
            if vision_total:
                result.total_a = vision_total
            # Use _tbls (the loop variable) rather than tbls_a directly to
            # avoid an IndexError if tbls_a were ever empty at this point.
            _tbl_method = _tbls[0]['method'] if _tbls else "brak"
            result.warnings.append(
                f"Dok. A: Vision quality-gate — tabele z {_tbl_method} "
                f"nieparsowalne ({len(_tbls)} tabel, 0 pozycji), Vision znalazł {len(vision_items)}"
            )
        else:
            items_b = vision_items
            if vision_total:
                result.total_b = vision_total
            _tbl_method = _tbls[0]['method'] if _tbls else "brak"
            result.warnings.append(
                f"Dok. B: Vision quality-gate — tabele z {_tbl_method} "
                f"nieparsowalne ({len(_tbls)} tabel, 0 pozycji), Vision znalazł {len(vision_items)}"
            )

    tbl_method_a = tbls_a[0]["method"] if tbls_a else "brak"
    tbl_method_b = tbls_b[0]["method"] if tbls_b else "brak"
    result.warnings.append(
        f"Pozycji wyekstrahowanych: A={len(items_a)}, B={len(items_b)} "
        f"(metoda tabel: {tbl_method_a}/{tbl_method_b})"
    )
    if tbl_method_a == "claude_vision":
        result.warnings.append("Dok. A: tabela wyekstrahowana przez Claude Vision (standardowe metody zawiodły)")
    if tbl_method_b == "claude_vision":
        result.warnings.append("Dok. B: tabela wyekstrahowana przez Claude Vision (standardowe metody zawiodły)")

    # --- Duplikaty ---
    for dup in detect_duplicates(items_a):
        _refs_a = dup.get("refs") or []
        result.findings.append(ComparisonFinding(
            "duplikat_a", dup["severity"], "Dok. A — duplikat",
            _refs_a[0] if _refs_a else "", "",
            dup["description"],
        ))
        result.warnings.append(f"[Dok. A] {dup['description']}")
    for dup in detect_duplicates(items_b):
        _refs_b = dup.get("refs") or []
        result.findings.append(ComparisonFinding(
            "duplikat_b", dup["severity"], "Dok. B — duplikat",
            "", _refs_b[0] if _refs_b else "",
            dup["description"],
        ))

    # --- Porównanie pozycji ---
    if items_a and items_b:
        compared, item_findings = compare_items(
            items_a, items_b, dt_a, dt_b, supplier_profile
        )
        result.items = compared
        result.findings.extend(item_findings)
    elif not items_a:
        result.warnings.append("Nie znaleziono pozycji towarowych w dok. A")
    elif not items_b:
        result.warnings.append("Nie znaleziono pozycji towarowych w dok. B")

    # --- E8-01: audyt kodu CN (SAD pole 33/[18 09] vs material_master.tariff_cn) ---
    if result.items and "SAD" in (dt_a, dt_b):
        try:
            _audit_cn_codes(result, text_a, text_b, dt_a, dt_b, db)
        except Exception as _e:
            # Nie wyciszamy cicho — audyt kodu CN to reguła krytyczna; sygnalizujemy
            # że się nie wykonał (zamiast wyglądać jak czysty wynik bez rozbieżności).
            logging.getLogger(__name__).warning("Audyt kodu CN nie wykonał się: %s", _e, exc_info=True)
            result.warnings.append(f"Audyt kodu CN nie wykonał się ({type(_e).__name__}) — wynik niepełny")

    # --- Suma kontrolna ---
    if result.total_b and result.items:
        items_b_for_check = [
            {"qty": i.get("qty_b",""), "price": i.get("price_b",""),
             "net": i.get("net_b",""), "ref": i.get("ref","")}
            for i in result.items
        ]
        # Per-supplier checksum tolerance (never tighter than the safe 0.1%/0.5% defaults).
        _chk_tol = 0.1
        if supplier_profile:
            _chk_tol = max(0.1, float(supplier_profile.get("checksum_tolerance_pct", 0.0) or 0.0))
        chk = validate_checksum(items_b_for_check, result.total_b, tolerance_pct=_chk_tol)
        if chk["valid"] is False and (chk.get("diff_pct") is None or (chk.get("diff_pct") or 0) > max(0.5, _chk_tol)):
            # diff_pct may be None when declared/calculated totals can't be compared
            # numerically; default to 0.0 to avoid TypeError in the :.2f format.
            _decl = chk.get("declared")
            _calc = chk.get("calculated")
            _diff_pct = chk.get("diff_pct") or 0.0
            result.findings.append(ComparisonFinding(
                "suma_kontrolna", "warning", "Total dok. B",
                str(_decl), str(_calc),
                f"Suma PI: deklarowana {float(_decl or 0):.2f}, "
                f"obliczona {float(_calc or 0):.2f} "
                f"(Δ {_diff_pct:.2f}%)",
            ))

    # --- Nagłówki ---
    fields_a = extract_header_fields(text_a, dt_a)
    fields_b = extract_header_fields(text_b, dt_b)
    headers, hdr_findings = compare_header_fields(fields_a, fields_b, supplier_profile)
    result.headers = headers
    result.findings.extend(hdr_findings)

    # --- I↔1 ---
    result.findings.extend(check_i1(text_a, text_b))

    return result


# ─────────────────────────────────────────────────────────────────────────────
# TESTY JEDNOSTKOWE
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    def _test(name: str, condition: bool, info: str = ""):
        icon = "✅" if condition else "❌"
        print(f"  {icon} {name}" + (f" — {info}" if info else ""))
        if not condition:
            sys.exit(1)

    print("\n═══ TESTY enhanced_comparator.py ═══\n")

    # TEST 1 — Mapowanie kolumn PI ALPHAMED
    print("TEST 1: Mapowanie kolumn PI ALPHAMED")
    hdr_alphamed = [
        "Product Code", "Description of goods",
        "Quantity/Unit\n(pouch/bag)", "LOT numbers",
        "Unit-price\n(pouch/bag)", "Amount"
    ]
    ci = _build_column_map(hdr_alphamed)
    _test("ref → col 0",   ci.get("ref")   == 0, str(ci))
    _test("desc → col 1",  ci.get("desc")  == 1, str(ci))
    _test("qty → col 2",   ci.get("qty")   == 2, str(ci))
    _test("lot → col 3",   ci.get("lot")   == 3, str(ci))
    _test("price → col 4", ci.get("price") == 4, str(ci))
    _test("net → col 5",   ci.get("net")   == 5, str(ci))

    # TEST 2 — Porównanie cen PO vs PI (norma — net zgodny)
    print("\nTEST 2: Porównanie cen PO vs PI (zaokrąglenie, net zgodny)")
    items_po = [{"ref": "QX0510-S", "qty": "10240", "price": "0.03",   "net": "263.17", "description": ""}]
    items_pi = [{"ref": "QX0510-S", "qty": "10240", "price": "0.0257", "net": "263.17", "description": ""}]
    compared, findings = compare_items(items_po, items_pi, "PO", "PI")
    _test("status = format (nie roznica)", compared[0]["status"] == "format", compared[0]["status"])
    _test("brak finding error dla ceny",
          not any(f.category == "cena" and f.severity == "error" for f in findings))

    # TEST 3 — Prawdziwa rozbieżność ilości
    print("\nTEST 3: Rozbieżność ilości")
    items_po2 = [{"ref": "NL753-S-40", "qty": "13500", "price": "0.02", "net": "257.85", "description": ""}]
    items_pi2 = [{"ref": "NL753-S-40", "qty": "13800", "price": "0.0191", "net": "263.58", "description": ""}]
    compared2, findings2 = compare_items(items_po2, items_pi2, "PO", "PI")
    _test("status = roznica", compared2[0]["status"] == "roznica", compared2[0]["status"])
    _test("finding error dla ilości",
          any(f.category == "ilosc" and f.severity == "error" for f in findings2))

    # TEST 4 — Warunki płatności (warning nie error)
    print("\nTEST 4: Warunki płatności — warning nie error")
    fields_a = {"Warunki płatności": "IW04"}
    fields_b = {"Warunki płatności": "100% T/T 60 days after shipment"}
    hdrs, f4 = compare_header_fields(fields_a, fields_b)
    _test("status = roznica (są różne)",
          any(h["key"] == "Warunki płatności" and h["status"] == "roznica" for h in hdrs))
    _test("severity = warning (nie error)",
          all(f.severity != "error" for f in f4 if f.category == "warunki"),
          str([f.severity for f in f4]))

    # TEST 5 — Adresy ACME (ok)
    print("\nTEST 5: Adresy ACME")
    fa5 = {"Adres dostawy": "26-608 Radom Logistyczna 2"}
    fb5 = {"Adres dostawy": "ul. Przemysłowa 10, 26-608 Radom"}
    hdrs5, _ = compare_header_fields(fa5, fb5)
    _test("status = ok (oba adresy ACME)",
          any(h["key"] == "Adres dostawy" and h["status"] == "ok" for h in hdrs5),
          str(hdrs5))

    # TEST 6 — Brak fałszywej walidacji arytmetycznej dla PO
    print("\nTEST 6: Brak walidacji arytmetycznej dla dok. A (PO)")
    items_a6 = [{"ref": "X", "qty": "10240", "price": "0.03", "net": "263.17", "description": ""}]
    items_b6 = [{"ref": "X", "qty": "10240", "price": "0.0257", "net": "263.17", "description": ""}]
    _, f6 = compare_items(items_a6, items_b6, "PO", "PI")
    _test("brak warning arytmetyka dla PO",
          not any(f.category == "arytmetyka" for f in f6),
          str([f.category for f in f6]))

    # TEST 7 — Kolumna LOT poprawnie wyciągana
    print("\nTEST 7: Mapowanie kolumn PI OmegaMedical (S.No)")
    hdr_poly = ["S.No", "Product Name", "Description", "Unit price\n(EUR)", "Qty(pcs)", "Amount\n(USD)"]
    ci7 = _build_column_map(hdr_poly)
    _test("ref → col 0 (S.No)", ci7.get("ref") == 0, str(ci7))
    _test("price → col 3", ci7.get("price") == 3, str(ci7))
    _test("qty → col 4",   ci7.get("qty")   == 4, str(ci7))
    _test("net → col 5",   ci7.get("net")   == 5, str(ci7))

    print("\n✅ Wszystkie testy przeszły pomyślnie.\n")
