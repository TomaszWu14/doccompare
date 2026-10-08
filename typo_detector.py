"""
typo_detector.py — silnik wykrywania literówek, błędów I/1, O/0,
brakujących dokumentów, rozbieżności ilości/wartości.

Działa na każdym typie dokumentu: SAD, CI, PO, PI, WZ, CMR.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional
from rapidfuzz import fuzz
from normalizer import normalize_number


# ─────────────────────────────────────────────────────────────────────────────
# STRUKTURY
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TypoFinding:
    """Jedno znalezisko — literówka, rozbieżność, brakujący element."""
    severity: str          # "error" | "warning" | "info"
    category: str          # "literowka" | "I_vs_1" | "brak_dokumentu" | "ilosc" | "wartosc" | "format" | "warunki"
    field_name: str        # nazwa pola gdzie znaleziono
    val_a: Optional[str]   # wartość w dok A
    val_b: Optional[str]   # wartość w dok B
    description: str       # opis błędu po polsku
    suggestion: str = ""   # sugestia poprawki


@dataclass
class TypoReport:
    """Pełny raport z analizy literówek i rozbieżności."""
    findings: list          # list[TypoFinding]
    error_count: int = 0
    warning_count: int = 0
    info_count: int = 0
    summary: str = ""

    def to_dict(self):
        return {
            "findings": [vars(f) for f in self.findings],
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "info_count": self.info_count,
            "summary": self.summary,
        }


# ─────────────────────────────────────────────────────────────────────────────
# WYKRYWANIE I vs 1, O vs 0
# ─────────────────────────────────────────────────────────────────────────────

def check_i_vs_1(val_a: str, val_b: str) -> Optional[str]:
    """
    Sprawdza czy dwa stringi różnią się tylko zamianą I↔1 lub O↔0.
    Zwraca opis błędu lub None jeśli brak tego problemu.
    """
    if not val_a or not val_b:
        return None
    if val_a == val_b:
        return None

    # Normalizuj: zamień 1→I i 0→O w obu stringach i porównaj
    def normalize_io(s: str) -> str:
        return s.replace('1', 'I').replace('0', 'O').upper()

    def normalize_10(s: str) -> str:
        return s.replace('I', '1').replace('O', '0').upper()

    na = normalize_io(val_a.strip())
    nb = normalize_io(val_b.strip())

    if na == nb:
        # Znaleźliśmy zamianę I↔1 lub O↔0
        diffs = []
        # Iterujemy po tych samych (przyciętych) wartościach, na których
        # zapadła decyzja o równości na/nb — inaczej wiodąca spacja w jednym
        # stringu przesuwa wszystkie pozycje i gubi różnice.
        for i, (ca, cb) in enumerate(zip(val_a.strip(), val_b.strip())):
            if ca != cb:
                if (ca.upper() in 'I1' and cb.upper() in 'I1') or (ca.upper() in 'O0' and cb.upper() in 'O0'):
                    diffs.append(f"poz.{i+1}: '{ca}'→'{cb}'")
        return f"Zamiana I↔1 lub O↔0: {', '.join(diffs) if diffs else 'wykryto'}"

    return None


# Bazowe pary znaków łatwych do pomylenia w numerach (OCR / wpisanie ręczne).
_BASE_CONFUSABLE_PAIRS = [
    ('I', '1'), ('i', '1'), ('l', '1'),
    ('O', '0'), ('o', '0'),
    ('S', '5'), ('s', '5'),
    ('B', '8'),
    ('Z', '2'), ('G', '6'),
]
# Pary „nauczone" — rozszerzalne w runtime z potwierdzonych korekt (feedback)
# lub z konfiguracji, bez zmiany kodu. Domyślnie puste → zachowanie jak dotąd.
_LEARNED_CONFUSABLE_PAIRS: list = []


def set_learned_confusable_pairs(pairs) -> None:
    """Ustawia listę nauczonych par (np. wczytanych z ustawień/feedbacku).
    Każda para to (a, b) — pojedyncze, różne znaki. Niepoprawne wpisy pomijane."""
    global _LEARNED_CONFUSABLE_PAIRS
    cleaned = []
    for p in (pairs or []):
        try:
            a, b = p
        except Exception:
            continue
        if isinstance(a, str) and isinstance(b, str) and len(a) == 1 and len(b) == 1 and a != b:
            cleaned.append((a, b))
    _LEARNED_CONFUSABLE_PAIRS = cleaned


def add_learned_confusable_pair(a: str, b: str) -> bool:
    """Dodaje pojedynczą nauczoną parę, jeśli poprawna i jeszcze nieobecna.
    Zwraca True gdy dodano."""
    if (isinstance(a, str) and isinstance(b, str) and len(a) == 1 and len(b) == 1 and a != b
            and (a, b) not in _LEARNED_CONFUSABLE_PAIRS
            and (b, a) not in _LEARNED_CONFUSABLE_PAIRS):
        _LEARNED_CONFUSABLE_PAIRS.append((a, b))
        return True
    return False


def detect_character_swaps(val_a: str, val_b: str) -> list[str]:
    """
    Wykrywa typowe zamiany znaków:
    - I↔1 (litera i vs cyfra jeden)
    - O↔0 (litera o vs cyfra zero)
    - l↔1 (małe L vs cyfra jeden)
    - S↔5 (litera S vs cyfra 5)
    - B↔8 (litera B vs cyfra 8)

    WAŻNE: Zwraca wyniki tylko dla wartości wyglądających jak kody/numery
    (minimum 4 znaki, mix liter i cyfr), żeby unikać false positives w tekście.
    """
    if not val_a or not val_b or val_a == val_b:
        return []

    va, vb = val_a.strip(), val_b.strip()

    # Tylko dla krótkich kodów/numerów — unikaj false positives w długim tekście
    if len(va) < 4 or len(va) > 30 or len(va) != len(vb):
        return []

    # Sprawdź czy wyglądają jak kod (zawierają cyfry i litery jednocześnie)
    has_digit_a = any(c.isdigit() for c in va)
    has_letter_a = any(c.isalpha() for c in va)
    if not (has_digit_a and has_letter_a):
        return []

    CONFUSABLE_PAIRS = _BASE_CONFUSABLE_PAIRS + _LEARNED_CONFUSABLE_PAIRS

    results = []
    swaps = []
    all_swaps = True
    for ca, cb in zip(va, vb):
        if ca == cb:
            continue
        found = False
        for p1, p2 in CONFUSABLE_PAIRS:
            if (ca == p1 and cb == p2) or (ca == p2 and cb == p1):
                swaps.append(f"'{ca}'↔'{cb}'")
                found = True
                break
        if not found:
            all_swaps = False

    if swaps:
        suffix = "" if all_swaps else " (wśród innych różnic)"
        results.append(f"Prawdopodobna zamiana znaków{suffix}: {', '.join(swaps)}")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# WYKRYWANIE LITERÓWEK W TEKŚCIE
# ─────────────────────────────────────────────────────────────────────────────

COMMON_TYPOS = [
    # (wzorzec błędny, prawidłowy, opis)
    (r'\bdrren\b', 'dren', "podwójne 'r' w słowie 'dren'"),
    (r'\bcombii\b', 'combi', "podwójne 'i' w słowie 'combi'"),
    (r'pobiarania', 'pobierania', "błąd 'a'→'e' w 'pobierania'"),
    (r'piersiwej', 'piersiowej', "brak 'o' w 'piersiowej'"),
    (r'\bprzedluzacz\b', 'przedłużacz', "brak polskich liter"),
    (r'\bdrenazu\b', 'drenażu', "brak polskich liter"),
    (r'\bzoladkowy\b', 'żołądkowy', "brak polskich liter"),
    (r'\bzglebnik\b', 'zgłębnik', "brak polskich liter"),
    (r'\bprzewodem\b', 'przewodem', "sprawdź"),
    (r'rektoskopii\s+i\s+rektoskopii', 'cystoskopii i rektoskopii', "powtórzenie słowa"),
    # podwójne litery
    (r'([a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ])\1\1', "", "potrójna litera — prawdopodobna literówka"),
]

DOUBLE_LETTER_RE = re.compile(r'([a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ])\1{2,}')


def check_text_typos(text: str, field_name: str = "") -> list[TypoFinding]:
    """Skanuje tekst pod kątem typowych literówek."""
    if not text:
        return []
    findings = []
    text_lower = text.lower()

    # Znane literówki
    for pattern, correct, desc in COMMON_TYPOS:
        m = re.search(pattern, text_lower)
        if m:
            wrong = m.group(0)
            findings.append(TypoFinding(
                severity="warning",
                category="literowka",
                field_name=field_name,
                val_a=wrong,
                val_b=correct,
                description=f"Literówka w opisie: '{wrong}' — {desc}",
                suggestion=f"Poprawna forma: '{correct}'" if correct else "",
            ))

    # Potrójne lub więcej liter
    for m in DOUBLE_LETTER_RE.finditer(text):
        char = m.group(1)
        word_start = max(0, m.start()-5)
        word_end = min(len(text), m.end()+5)
        context = text[word_start:word_end]
        findings.append(TypoFinding(
            severity="warning",
            category="literowka",
            field_name=field_name,
            val_a=m.group(0),
            val_b="",
            description=f"Podejrzana wielokrotna litera '{char}' w: '...{context}...'",
            suggestion="Sprawdź ręcznie",
        ))

    return findings


# ─────────────────────────────────────────────────────────────────────────────
# WYKRYWANIE BRAKUJĄCYCH NUMERÓW DOKUMENTÓW
# ─────────────────────────────────────────────────────────────────────────────

def extract_doc_refs(text: str) -> list[str]:
    """
    Wyciąga numery referencyjne dokumentów z tekstu.
    Obsługuje N935, numery faktur, PO, WZ itp.
    """
    refs = []

    # N935 refs z SADu
    for m in re.finditer(r'N935-([A-Z0-9\-,\s]+?)(?:\s+poz\.|;|$)', text, re.IGNORECASE):
        raw = m.group(1)
        for ref in re.split(r'[,\s]+', raw):
            ref = ref.strip().strip(',').strip()
            if len(ref) >= 5:
                refs.append(ref)

    # Numery faktur (Invoice No)
    for m in re.finditer(r'(?:invoice\s*no\.?|inv\.?\s*no\.?)[:\s]+([A-Z0-9\-\/]+)', text, re.IGNORECASE):
        ref = m.group(1).strip()
        if len(ref) >= 5:
            refs.append(ref)

    # PO numbers
    for m in re.finditer(r'(?:purchase\s*order|po\s*no\.?|order\s*no\.?)[:\s]+([A-Z0-9\-\/]+)', text, re.IGNORECASE):
        ref = m.group(1).strip()
        if len(ref) >= 4:
            refs.append(ref)

    return list(set(refs))


def check_missing_refs(refs_a: list[str], refs_b: list[str], context: str = "") -> list[TypoFinding]:
    """
    Sprawdza czy wszystkie numery z A są w B i odwrotnie.
    Wykrywa też zamiany I↔1 w numerach.
    """
    findings = []

    for ref in refs_a:
        if ref in refs_b:
            continue

        # Szukaj fuzzy match
        best_match = None
        best_score = 0
        for ref_b in refs_b:
            score = fuzz.ratio(ref, ref_b)
            if score > best_score:
                best_score = score
                best_match = ref_b

        if best_score >= 85 and best_match:
            # Bliskie dopasowanie — sprawdź czy to I vs 1
            swap_desc = check_i_vs_1(ref, best_match)
            if swap_desc:
                findings.append(TypoFinding(
                    severity="error",
                    category="I_vs_1",
                    field_name=f"Numer dokumentu {context}",
                    val_a=ref,
                    val_b=best_match,
                    description=f"Prawdopodobna zamiana I↔1 lub O↔0 w numerze dokumentu: '{ref}' vs '{best_match}'",
                    suggestion=f"Sprawdź czy '{ref}' to ten sam dokument co '{best_match}'",
                ))
            else:
                char_swaps = detect_character_swaps(ref, best_match)
                if char_swaps:
                    findings.append(TypoFinding(
                        severity="error",
                        category="I_vs_1",
                        field_name=f"Numer dokumentu {context}",
                        val_a=ref,
                        val_b=best_match,
                        description=f"Podobne numery dokumentów z zamianą znaków: '{ref}' vs '{best_match}' — {'; '.join(char_swaps)}",
                        suggestion="Weryfikacja ręczna wymagana",
                    ))
                else:
                    findings.append(TypoFinding(
                        severity="warning",
                        category="literowka",
                        field_name=f"Numer dokumentu {context}",
                        val_a=ref,
                        val_b=best_match,
                        description=f"Podobne ale różne numery: '{ref}' vs '{best_match}' (podobieństwo {best_score}%)",
                        suggestion="Sprawdź czy to ten sam dokument",
                    ))
        elif best_score < 85:
            findings.append(TypoFinding(
                severity="error",
                category="brak_dokumentu",
                field_name=f"Numer dokumentu {context}",
                val_a=ref,
                val_b=None,
                description=f"Numer '{ref}' z dok. A nie odnaleziony w dok. B",
                suggestion="Sprawdź czy dokument jest załączony",
            ))

    return findings


# ─────────────────────────────────────────────────────────────────────────────
# PORÓWNANIE LICZB Z TOLERANCJĄ
# ─────────────────────────────────────────────────────────────────────────────

def parse_number_strict(text: str) -> Optional[float]:
    """Parsuje liczbę z tekstu, obsługuje format PL i EN."""
    if not text:
        return None
    # Wartości liczbowe (float/int) NIE mogą iść przez string-normalizer:
    # str(13500.001) → "13500.001", a normalize_number traktuje '.' jako separator
    # tysięcy → 13500001. Liczbę zwracamy wprost.
    if isinstance(text, (int, float)):
        return round(float(text), 4)
    t = str(text).upper()
    # Usuwamy tokeny jednostek/walut. Sortujemy malejąco po długości, aby
    # dłuższy token miał pierwszeństwo (np. "KGS" przed "KG") — inaczej
    # "100 KGS" → "100 S" → None i porównanie masy zostaje wyciszone.
    for tok in sorted(["EUR", "USD", "PLN", "GBP", "ZŁ", "ZL", "SZT", "PCS", "NO", "KG", "KGS"],
                      key=len, reverse=True):
        t = t.replace(tok, "")
    t = t.replace("\u00a0", "").replace(" ", "").strip()
    if not t or t in ["-", "—", "", "N/A"]:
        return None
    # Delegate the actual numeric parse to the canonical normalizer so the
    # app-wide invariants hold (1.500=1500, "5%"→None, European thousands).
    # (normalize_number jest importowane na poziomie modułu — bez per-call importu.)
    val = normalize_number(t)
    return round(float(val), 4) if val is not None else None


def compare_numbers(val_a: str, val_b: str, field_name: str,
                    severity: str = "error", tolerance: float = 0.005) -> Optional[TypoFinding]:
    """Porównuje dwie liczby i zwraca finding jeśli różne."""
    fa = parse_number_strict(val_a)
    fb = parse_number_strict(val_b)

    if fa is None or fb is None:
        return None

    # BUGFIX: sama tolerancja BEZWZGLĘDNA (0.005) myliła się dla małych liczb —
    # 0.0001 vs 0.0005 (różnica 400%) była klasyfikowana jako „ta sama wartość,
    # różny format". Dokładamy warunek względny: normalne liczby bez zmian, ale
    # dwie drobne liczby różniące się o >50% NIE są „tą samą wartością".
    _rel = (abs(fa - fb) / max(abs(fa), abs(fb))) if max(abs(fa), abs(fb)) > 0 else 0.0
    if abs(fa - fb) <= tolerance and _rel <= 0.5:
        # Ta sama wartość — sprawdź format
        va_clean = str(val_a).strip().replace(",", ".").replace(" ", "")
        vb_clean = str(val_b).strip().replace(",", ".").replace(" ", "")
        if va_clean != vb_clean:
            return TypoFinding(
                severity="info",
                category="format",
                field_name=field_name,
                val_a=val_a,
                val_b=val_b,
                description=f"Różny format liczby, ta sama wartość: '{val_a}' vs '{val_b}'",
                suggestion="Ujednolicić format (przecinek/kropka, spacje)",
            )
        return None

    delta = fb - fa
    # Dla normalnych wartości liczymy % względem dok.A. Gdy baza jest znikoma
    # (< epsilon), nie zawyżamy mianownika — to dawałoby fałszywie niskie %
    # przy realnej różnicy. Wtedy procent nie ma sensu, więc raportujemy 0%
    # (sama różnica bezwzględna w opisie jest miarodajna).
    _eps = 0.001
    if abs(fa) >= _eps:
        pct = abs(delta) / abs(fa) * 100
    else:
        pct = 0.0

    return TypoFinding(
        severity=severity,
        # Kategoria musi być spójna z wagą: to różnica WARTOŚCI (przeszła próg
        # tolerancji), więc dla severity="error" nigdy nie oznaczamy jej jako
        # kosmetyczny "format" (downstream traktuje "format" jako nieistotne).
        category="ilosc" if severity == "error" else "format",
        field_name=field_name,
        val_a=val_a,
        val_b=val_b,
        description=f"Różnica: {delta:+,.4f} ({pct:.2f}%) — dok.A={fa:,.4f} vs dok.B={fb:,.4f}",
        suggestion="Wymagana weryfikacja z dostawcą" if pct > 1 else "Drobna rozbieżność — sprawdź",
    )


# ─────────────────────────────────────────────────────────────────────────────
# PORÓWNANIE DWÓCH DOWOLNYCH TEKSTÓW
# ─────────────────────────────────────────────────────────────────────────────

def compare_text_fields(val_a: Optional[str], val_b: Optional[str],
                        field_name: str, severity: str = "warning",
                        check_typos: bool = True) -> list[TypoFinding]:
    """
    Kompleksowe porównanie dwóch pól tekstowych.
    Wykrywa: brak pola, literówki, zamiany I/1, różnice formatu.
    """
    findings = []

    if not val_a and not val_b:
        return findings

    if not val_a:
        findings.append(TypoFinding(
            severity=severity, category="brak_dokumentu",
            field_name=field_name, val_a=None, val_b=val_b,
            description=f"Pole '{field_name}' obecne tylko w dok. B: '{val_b}'",
        ))
        return findings

    if not val_b:
        findings.append(TypoFinding(
            severity=severity, category="brak_dokumentu",
            field_name=field_name, val_a=val_a, val_b=None,
            description=f"Pole '{field_name}' obecne tylko w dok. A: '{val_a}'",
        ))
        return findings

    va = val_a.strip()
    vb = val_b.strip()

    if va.upper() == vb.upper():
        return findings  # identyczne

    # Sprawdź zamiany I↔1, O↔0
    swap_desc = check_i_vs_1(va, vb)
    if swap_desc:
        findings.append(TypoFinding(
            severity="error", category="I_vs_1",
            field_name=field_name, val_a=va, val_b=vb,
            description=f"Zamiana I↔1/O↔0 w polu '{field_name}': {swap_desc}",
            suggestion="Sprawdź który zapis jest prawidłowy",
        ))
        return findings

    # Sprawdź inne zamiany znaków
    char_swaps = detect_character_swaps(va, vb)
    if char_swaps:
        findings.append(TypoFinding(
            severity="error", category="I_vs_1",
            field_name=field_name, val_a=va, val_b=vb,
            description=f"Zamiana znaków w '{field_name}': {'; '.join(char_swaps)}",
            suggestion="Weryfikacja ręczna",
        ))
        return findings

    # Fuzzy similarity
    sim = fuzz.ratio(va.upper(), vb.upper())

    if sim >= 95:
        # Drobna różnica formatu (spacja, wielkość liter)
        findings.append(TypoFinding(
            severity="info", category="format",
            field_name=field_name, val_a=va, val_b=vb,
            description=f"Drobna różnica formatu w '{field_name}' (podobieństwo {sim}%)",
        ))
    elif sim >= 85:
        # Wysoka podobieństwo — sprawdź literówki
        typo_a = check_text_typos(va, field_name) if check_typos else []
        typo_b = check_text_typos(vb, field_name) if check_typos else []
        all_typos = typo_a + typo_b

        if all_typos:
            findings.extend(all_typos)
        else:
            findings.append(TypoFinding(
                severity="warning", category="literowka",
                field_name=field_name, val_a=va, val_b=vb,
                description=f"Podobne ale różne wartości w '{field_name}' (podobieństwo {sim}%) — możliwa literówka",
                suggestion="Sprawdź ręcznie",
            ))
    else:
        # Poniżej 85% — to rzeczywista różnica, nie literówka
        findings.append(TypoFinding(
            severity=severity, category="wartosc",
            field_name=field_name, val_a=va, val_b=vb,
            description=f"Różne wartości w '{field_name}' (podobieństwo {sim}%)",
        ))

    return findings


# ─────────────────────────────────────────────────────────────────────────────
# ANALIZA PEŁNYCH TEKSTÓW DOKUMENTÓW
# ─────────────────────────────────────────────────────────────────────────────

def analyze_full_texts(text_a: str, text_b: str,
                       label_a: str = "Dok. A", label_b: str = "Dok. B",
                       doc_type_a: str = "auto", doc_type_b: str = "auto") -> TypoReport:
    """
    Pełna analiza dwóch tekstów PDF pod kątem literówek i rozbieżności.
    Główna funkcja — wywołaj ją zamiast compare_documents gdy chcesz
    maksymalnie szczegółową analizę.
    """
    findings: list[TypoFinding] = []

    # 1. Literówki w tekście A
    typos_a = check_text_typos(text_a, label_a)
    for t in typos_a:
        t.description = f"[{label_a}] " + t.description
    findings.extend(typos_a)

    # 2. Literówki w tekście B
    typos_b = check_text_typos(text_b, label_b)
    for t in typos_b:
        t.description = f"[{label_b}] " + t.description
    findings.extend(typos_b)

    # 3. Porównanie numerów referencyjnych
    refs_a = extract_doc_refs(text_a)
    refs_b = extract_doc_refs(text_b)

    if refs_a:
        missing_in_b = check_missing_refs(refs_a, refs_b, f"{label_a}→{label_b}")
        for f in missing_in_b:
            f.description = f"[{label_a}→{label_b}] " + f.description
        findings.extend(missing_in_b)
    if refs_b:
        missing_in_a = check_missing_refs(refs_b, refs_a, f"{label_b}→{label_a}")
        for f in missing_in_a:
            f.description = f"[{label_b}→{label_a}] " + f.description
        findings.extend(missing_in_a)

    # 4. Porównanie kluczowych pól liczbowych z obu dokumentów
    number_patterns = [
        (r'(?:total|razem|suma)[^\n:]*?[:\s]+([\d\s.,]+(?:\s*(?:EUR|USD|PLN))?)', "Suma łączna"),
        (r'(?:gross\s*weight|masa\s*brutto)[^\n:]*?[:\s]+([\d.,]+)', "Masa brutto"),
        (r'(?:net\s*weight|masa\s*netto)[^\n:]*?[:\s]+([\d.,]+)', "Masa netto"),
        (r'(?:cbm|objętość)[^\n:]*?[:\s]+([\d.,]+)', "Objętość CBM"),
        (r'(?:packages?|pkgs?|opakowań)[^\n:]*?[:\s]+(\d+)', "Liczba opakowań"),
    ]

    for pattern, fname in number_patterns:
        m_a = re.search(pattern, text_a, re.IGNORECASE)
        m_b = re.search(pattern, text_b, re.IGNORECASE)
        if m_a and m_b:
            finding = compare_numbers(m_a.group(1), m_b.group(1), fname)
            if finding:
                findings.append(finding)

    # 5. Porównanie warunków dostawy i płatności
    delivery_patterns = [
        (r'(?:terms?\s*of\s*(?:delivery|payment)|warunki\s*(?:dostawy|płatności))[^\n:]*?[:\n]+([^\n]{3,60})',
         "Warunki dostawy/płatności"),
        (r'(?:incoterms?)[^\n:]*?[:\s]+([A-Z]{3}[^\n]{0,30})', "Incoterms"),
    ]

    for pattern, fname in delivery_patterns:
        m_a = re.search(pattern, text_a, re.IGNORECASE)
        m_b = re.search(pattern, text_b, re.IGNORECASE)
        if m_a and m_b:
            va, vb = m_a.group(1).strip(), m_b.group(1).strip()
            if va.upper() != vb.upper():
                sim = fuzz.ratio(va.upper(), vb.upper())
                findings.append(TypoFinding(
                    severity="warning",
                    category="warunki",
                    field_name=fname,
                    val_a=va,
                    val_b=vb,
                    description=f"Różne {fname}: '{va}' vs '{vb}' (podobieństwo {sim}%)",
                    suggestion="Sprawdź czy warunki są uzgodnione",
                ))

    # 6. Deduplikacja — usuń podobne findings
    findings = _deduplicate(findings)

    # Statystyki
    error_count = sum(1 for f in findings if f.severity == "error")
    warning_count = sum(1 for f in findings if f.severity == "warning")
    info_count = sum(1 for f in findings if f.severity == "info")

    # Summary
    if not findings:
        summary = f"Brak wykrytych literówek ani rozbieżności między {label_a} a {label_b}."
    elif error_count > 0:
        summary = (f"Wykryto {error_count} błędów krytycznych (w tym możliwe zamiany I↔1/O↔0), "
                   f"{warning_count} ostrzeżeń, {info_count} uwag.")
    else:
        summary = f"Wykryto {warning_count} ostrzeżeń i {info_count} uwag. Brak błędów krytycznych."

    return TypoReport(
        findings=findings,
        error_count=error_count,
        warning_count=warning_count,
        info_count=info_count,
        summary=summary,
    )


def _deduplicate(findings: list[TypoFinding]) -> list[TypoFinding]:
    """Usuwa zduplikowane findings."""
    seen = set()
    result = []
    for f in findings:
        key = (f.category, f.field_name, str(f.val_a), str(f.val_b))
        if key not in seen:
            seen.add(key)
            result.append(f)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# ANALIZA POZYCJI TOWAROWYCH — DETEKCJA LITERÓWEK W OPISACH
# ─────────────────────────────────────────────────────────────────────────────

def analyze_item_descriptions(items_a: list[dict], items_b: list[dict]) -> list[TypoFinding]:
    """
    Porównuje opisy pozycji towarowych pod kątem literówek i zamian I↔1.
    items_a/b: list of dicts with keys: ref, desc, qty, price, net
    """
    findings = []

    # Buduj mapę ref → item. Klucz znormalizowany (bez separatorów/wielkości liter),
    # by „NL753-S-40" == „NL753S40" — inaczej dopasowane pozycje uchodzą za brakujące.
    def _nref(r):
        return re.sub(r"[^A-Z0-9]", "", str(r or "").upper())
    map_a = {}
    for i in items_a:
        if i.get("ref"):
            map_a.setdefault(_nref(i["ref"]), i)
    map_b = {}
    for i in items_b:
        if i.get("ref"):
            map_b.setdefault(_nref(i["ref"]), i)

    # Sprawdź REFy pod kątem I↔1
    refs_a = list(map_a.keys())
    refs_b = list(map_b.keys())

    for ref_a in refs_a:
        if ref_a in refs_b:
            # Identyczny ref — sprawdź opis i wartości
            ia, ib = map_a[ref_a], map_b[ref_a]
            ref_lbl = ia.get("ref", ref_a)   # oryginalny REF do etykiet

            # Opis
            desc_a = ia.get("desc", "") or ""
            desc_b = ib.get("desc", "") or ""
            if desc_a and desc_b:
                desc_findings = compare_text_fields(desc_a, desc_b,
                                                    f"Opis pozycji {ref_lbl}", "info")
                findings.extend(desc_findings)

            # Pomocnik: emituje finding "brak pola po jednej stronie" gdy
            # wartość występuje tylko w jednym dokumencie.
            def _one_sided(va, vb, fld, sev):
                if va and not vb:
                    return TypoFinding(
                        severity=sev, category="brak_dokumentu",
                        field_name=fld, val_a=va, val_b=None,
                        description=f"Pole '{fld}' obecne tylko w dok. A: '{va}'",
                    )
                if vb and not va:
                    return TypoFinding(
                        severity=sev, category="brak_dokumentu",
                        field_name=fld, val_a=None, val_b=vb,
                        description=f"Pole '{fld}' obecne tylko w dok. B: '{vb}'",
                    )
                return None

            # Ilość
            if ia.get("qty") and ib.get("qty"):
                n = compare_numbers(ia["qty"], ib["qty"],
                                    f"Ilość pozycji {ref_lbl}", "error")
                if n:
                    findings.append(n)
            else:
                m = _one_sided(ia.get("qty"), ib.get("qty"),
                               f"Ilość pozycji {ref_lbl}", "warning")
                if m:
                    findings.append(m)

            # Cena
            if ia.get("price") and ib.get("price"):
                n = compare_numbers(ia["price"], ib["price"],
                                    f"Cena pozycji {ref_lbl}", "warning", 0.0001)
                if n:
                    findings.append(n)
            else:
                m = _one_sided(ia.get("price"), ib.get("price"),
                               f"Cena pozycji {ref_lbl}", "warning")
                if m:
                    findings.append(m)

            # Wartość netto
            if ia.get("net") and ib.get("net"):
                n = compare_numbers(ia["net"], ib["net"],
                                    f"Wartość netto pozycji {ref_lbl}", "error")
                if n:
                    findings.append(n)
            else:
                m = _one_sided(ia.get("net"), ib.get("net"),
                               f"Wartość netto pozycji {ref_lbl}", "warning")
                if m:
                    findings.append(m)

        else:
            # Brak dokładnego dopasowania — szukaj I↔1
            swap_found = False
            for ref_b in refs_b:
                swap = check_i_vs_1(ref_a, ref_b)
                if swap:
                    findings.append(TypoFinding(
                        severity="error",
                        category="I_vs_1",
                        field_name="Kod REF pozycji",
                        val_a=ref_a,
                        val_b=ref_b,
                        description=f"Zamiana I↔1 w kodzie REF: '{ref_a}' vs '{ref_b}' — {swap}",
                        suggestion="Sprawdź który kod jest prawidłowy",
                    ))
                    swap_found = True
                    break

                char_swaps = detect_character_swaps(ref_a, ref_b)
                if char_swaps:
                    findings.append(TypoFinding(
                        severity="error",
                        category="I_vs_1",
                        field_name="Kod REF pozycji",
                        val_a=ref_a,
                        val_b=ref_b,
                        description=f"Zamiana znaków w kodzie REF: '{ref_a}' vs '{ref_b}' — {'; '.join(char_swaps)}",
                        suggestion="Weryfikacja wymagana",
                    ))
                    swap_found = True
                    break

            if not swap_found:
                findings.append(TypoFinding(
                    severity="warning",
                    category="brak_dokumentu",
                    field_name="Kod REF pozycji",
                    val_a=ref_a,
                    val_b=None,
                    description=f"Pozycja '{ref_a}' z dok. A nie odnaleziona w dok. B",
                ))

    return findings
