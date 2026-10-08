"""
delivery_workflow.py — czysta logika workflow dostaw (statusy, kroki, dokumenty).

Model: PŁASKI stepper 20 kroków (STEPS). Dwa kroki to gałęzie wyniku —
„Proforma sprawdzona" (zgodne/do weryfikacji/niezgodne) i „Artwork sprawdzony"
(zgodne/niezgodne/błędne) — w stepperze jeden krok, kolorowany wynikiem.

Moduł zawiera wyłącznie dane i czyste funkcje — żadnych efektów ubocznych.
"""

# ── Sloty dokumentów ──────────────────────────────────────────────────────────
DOC_SLOTS = [
    {"type": "PO",         "label": "Purchase Order",     "color": "#1d4ed8", "bg": "#dbeafe", "icon": "file-text"},
    {"type": "PI",         "label": "Proforma Invoice",   "color": "#7c3aed", "bg": "#ede9fe", "icon": "file-check"},
    {"type": "CI",         "label": "Faktura handlowa",   "color": "#c026d3", "bg": "#fae8ff", "icon": "receipt"},
    {"type": "PL",         "label": "Packing List",       "color": "#b45309", "bg": "#fef3c7", "icon": "list-checks"},
    {"type": "BL",         "label": "Konosament (B/L)",   "color": "#0f766e", "bg": "#ccfbf1", "icon": "anchor"},
    {"type": "ARTWORK",    "label": "Artwork wzorzec",    "color": "#9333ea", "bg": "#f3e8ff", "icon": "palette"},
    {"type": "ARTWORK_B",  "label": "Artwork fabryczny",  "color": "#7c3aed", "bg": "#ede9fe", "icon": "image"},
    {"type": "SAD",        "label": "Deklaracja celna",   "color": "#dc2626", "bg": "#fee2e2", "icon": "stamp"},
]
REQUIRED_SLOT_TYPES = [s["type"] for s in DOC_SLOTS]

COMP_TYPE_TO_SLOTS = {
    "PI": ("PO", "PI"), "PI_PO": ("PO", "PI"), "PROFORMA": ("PO", "PI"),
    "PO_VS_PI": ("PO", "PI"),
    "PL": ("CI", "PL"), "CI_PL": ("CI", "PL"),
    "SAD": ("SAD", "CI"), "SAD_CI": ("SAD", "CI"),
    "PO_VS_CI": ("CI", "CI"),
    "ARTWORK": ("ARTWORK", "ARTWORK_B"),
}
PAIRS = [("PO", "PI"), ("CI", "PL"), ("ARTWORK", "ARTWORK_B")]
NONADJACENT_COMPARE = {"SAD": "CI"}

# ── PŁASKI stepper: 20 kroków ─────────────────────────────────────────────────
# Każdy krok: key, label, statuses (rzeczywiste wartości statusu mapujące się na
# ten krok — zwykle 1; kroki-gałęzie mapują wynik + statusy legacy).
STEPS = [
    {"key": "utworzone",                 "label": "Nowe zamówienie",
     "statuses": ["utworzone"]},
    {"key": "wyslane_do_dostawcy",       "label": "Wysłane do dostawcy",
     "statuses": ["wyslane_do_dostawcy"]},
    {"key": "proforma_oczekiwana",       "label": "Oczekiwanie na proformę",
     "statuses": ["proforma_oczekiwana"]},
    {"key": "proforma_otrzymana",        "label": "Proforma otrzymana — czeka na porównanie",
     "statuses": ["proforma_otrzymana", "proforma_zaladowana"]},
    {"key": "proforma_sprawdzona",       "label": "Proforma sprawdzona",
     "statuses": ["zweryfikowane", "sprawdzone_zgodne",
                  "sprawdzone_do_weryfikacji", "sprawdzone_niezgodne"]},
    {"key": "artwork_oczekiwanie",       "label": "Oczekiwanie na artwork",   "track": "artwork",
     "statuses": ["artwork_oczekiwanie", "zatwierdzone_do_wyplyniecia",
                  "artwork_master_zaladowany", "artwork_czeka_fabryczny",
                  "artwork_brak_mastera"]},
    {"key": "artwork_sprawdzony",        "label": "Artwork sprawdzony",       "track": "artwork",
     "statuses": ["artwork_zgodne", "artwork_niezgodne", "artwork_bledne"]},
    {"key": "etd_sap",                   "label": "Porównanie ETD — SAP",
     "statuses": ["etd_sap", "zaokretowane"]},
    {"key": "zlecenie_spedycja",         "label": "Wysłanie zlecenia do spedycji",
     "statuses": ["zlecenie_spedycja"]},
    {"key": "agent_przypisany",          "label": "Przypisanie agenta",
     "statuses": ["agent_przypisany", "w_transporcie"]},
    {"key": "packing_list_oczekiwanie",  "label": "Oczekiwanie na packing list",
     "statuses": ["packing_list_oczekiwanie", "na_miejscu"]},
    {"key": "packing_list_otrzymany",    "label": "Packing list otrzymany",
     "statuses": ["packing_list_otrzymany"]},
    {"key": "dokumenty_odprawa",         "label": "Dokumenty do odprawy celnej",
     "statuses": ["dokumenty_odprawa", "w_odprawie"]},
    {"key": "dostawa_przychodzaca",      "label": "Utworzenie dostawy przychodzącej",
     "statuses": ["dostawa_przychodzaca"]},
    {"key": "dokumenty_wyslane_odprawa", "label": "Dokumenty wysłane do odprawy + faktura transportowa + tłumaczenia",
     "statuses": ["dokumenty_wyslane_odprawa"]},
    {"key": "sad_draft",                 "label": "Draft SAD do potwierdzenia",
     "statuses": ["sad_draft"]},
    {"key": "pzc_agencja",               "label": "PZC od agencji",
     "statuses": ["pzc_agencja", "po_odprawie"]},
    {"key": "dostawa_magazyn",           "label": "Potwierdzenie dostawy przez magazyn",
     "statuses": ["dostawa_magazyn", "w_magazynie", "dostarczone"]},
    {"key": "rozliczenie_fiori",         "label": "Rozliczenie faktury we FIORI + MIR7",
     "statuses": ["rozliczenie_fiori", "do_rozliczenia"]},
    {"key": "rozliczenie_transportu",    "label": "Potwierdzenie rozliczenia transportu",
     "statuses": ["rozliczenie_transportu", "rozliczone"]},
]

# Coarse fazy (do paska statystyk/filtra) — grupują kroki w 8 logicznych etapów.
PHASES = [
    {"key": "zamowienie",  "label": "Zamówienie",  "icon": "file-text",
     "statuses": ["utworzone", "wyslane_do_dostawcy"]},
    {"key": "proforma",    "label": "Proforma",    "icon": "file-check",
     "statuses": ["proforma_oczekiwana", "proforma_otrzymana", "proforma_zaladowana",
                  "zweryfikowane", "sprawdzone_zgodne", "sprawdzone_do_weryfikacji",
                  "sprawdzone_niezgodne"]},
    {"key": "artwork",     "label": "Artwork",     "icon": "palette",
     "statuses": ["artwork_oczekiwanie", "zatwierdzone_do_wyplyniecia",
                  "artwork_master_zaladowany", "artwork_czeka_fabryczny",
                  "artwork_brak_mastera",
                  "artwork_zgodne", "artwork_niezgodne", "artwork_bledne"]},
    {"key": "spedycja",    "label": "Spedycja",    "icon": "ship",
     "statuses": ["etd_sap", "zaokretowane", "zlecenie_spedycja",
                  "agent_przypisany", "w_transporcie"]},
    {"key": "packing",     "label": "Packing list", "icon": "list-checks",
     "statuses": ["packing_list_oczekiwanie", "na_miejscu", "packing_list_otrzymany"]},
    {"key": "odprawa",     "label": "Odprawa",     "icon": "stamp",
     "statuses": ["dokumenty_odprawa", "w_odprawie", "dostawa_przychodzaca",
                  "dokumenty_wyslane_odprawa", "sad_draft", "pzc_agencja", "po_odprawie"]},
    {"key": "magazyn",     "label": "Magazyn",     "icon": "warehouse",
     "statuses": ["dostawa_magazyn", "w_magazynie", "dostarczone"]},
    {"key": "rozliczenie", "label": "Rozliczenie", "icon": "check-circle",
     "statuses": ["rozliczenie_fiori", "do_rozliczenia",
                  "rozliczenie_transportu", "rozliczone"]},
]

STATUS_LABELS = {
    "utworzone":                   "Nowe zamówienie",
    "wyslane_do_dostawcy":         "Zamówienie wysłane do dostawcy",
    "proforma_oczekiwana":         "Oczekiwanie na proformę",
    "proforma_otrzymana":          "Proforma otrzymana — czeka na porównanie",
    "proforma_zaladowana":         "Proforma załadowana — czeka na porównanie",
    "zweryfikowane":               "Proforma sprawdzona",
    "sprawdzone_zgodne":           "Proforma sprawdzona — zgodne",
    "sprawdzone_do_weryfikacji":   "Proforma sprawdzona — do weryfikacji",
    "sprawdzone_niezgodne":        "Proforma sprawdzona — niezgodne",
    "artwork_oczekiwanie":         "Oczekiwanie na artwork",
    "artwork_master_zaladowany":   "Artwork master załadowany",
    "artwork_czeka_fabryczny":     "Czeka na artwork fabryczny",
    "artwork_brak_mastera":        "Brak mastera artworku — wymaga uwagi",
    "zatwierdzone_do_wyplyniecia": "Zatwierdzone do wypłynięcia",
    "artwork_zgodne":              "Artwork sprawdzony — zgodne",
    "artwork_niezgodne":           "Artwork sprawdzony — niezgodne",
    "artwork_bledne":              "Artwork sprawdzony — błędne",
    "etd_sap":                     "Porównanie ETD — SAP",
    "zlecenie_spedycja":           "Wysłanie zlecenia do spedycji",
    "agent_przypisany":            "Przypisanie agenta",
    "packing_list_oczekiwanie":    "Oczekiwanie na packing list",
    "packing_list_otrzymany":      "Packing list otrzymany",
    "dokumenty_odprawa":           "Dokumenty do odprawy celnej",
    "dostawa_przychodzaca":        "Utworzenie dostawy przychodzącej",
    "dokumenty_wyslane_odprawa":   "Dokumenty wysłane do odprawy + faktura transportowa + tłumaczenia",
    "sad_draft":                   "Draft SAD do potwierdzenia",
    "pzc_agencja":                 "PZC od agencji",
    "dostawa_magazyn":             "Potwierdzenie dostawy przez magazyn",
    "rozliczenie_fiori":           "Rozliczenie faktury we FIORI + MIR7",
    "rozliczenie_transportu":      "Potwierdzenie rozliczenia transportu",
    # Legacy (stare dostawy) — etykiety zachowane dla czytelności.
    "zaokretowane":                "Zaokrętowane (legacy)",
    "w_transporcie":               "W transporcie (legacy)",
    "na_miejscu":                  "Na miejscu (legacy)",
    "w_odprawie":                  "W odprawie celnej (legacy)",
    "po_odprawie":                 "Po odprawie (legacy)",
    "w_magazynie":                 "W magazynie (legacy)",
    "dostarczone":                 "Dostarczone (legacy)",
    "do_rozliczenia":              "Do rozliczenia (legacy)",
    "rozliczone":                  "Rozliczone (legacy)",
}

# Krótkie opisy statusów — co dany stan oznacza i na co czekamy. Pokazywane
# w „kwadracikach" (dymkach) steppera, żeby operator od razu wiedział, co dalej.
STATUS_DESCRIPTIONS = {
    "utworzone":                   "Zamówienie utworzone w systemie. Następny krok: wysłanie do dostawcy.",
    "wyslane_do_dostawcy":         "Zamówienie wysłane do dostawcy. Czekamy na potwierdzenie i proformę.",
    "proforma_oczekiwana":         "Oczekiwanie na proformę od dostawcy.",
    "proforma_otrzymana":          "Proforma otrzymana. Czeka na porównanie z zamówieniem (PO ↔ PI).",
    "proforma_zaladowana":         "Proforma załadowana do systemu. Czeka na porównanie z zamówieniem.",
    "zweryfikowane":               "Proforma sprawdzona i zweryfikowana.",
    "sprawdzone_zgodne":           "Proforma zgodna z zamówieniem — brak rozbieżności.",
    "sprawdzone_do_weryfikacji":   "Proforma sprawdzona — są punkty do ręcznej weryfikacji.",
    "sprawdzone_niezgodne":        "Proforma niezgodna z zamówieniem — wymaga wyjaśnienia z dostawcą.",
    "artwork_oczekiwanie":         "Oczekiwanie na artwork / etykiety od dostawcy do zatwierdzenia.",
    "artwork_master_zaladowany":   "Wzorzec (master) podpięty automatycznie z bazy artworków. Następny krok: artwork fabryczny od dostawcy.",
    "artwork_czeka_fabryczny":     "Master jest na miejscu — oczekiwanie na artwork fabryczny do porównania ze wzorcem.",
    "artwork_brak_mastera":        "Nie znaleziono POTWIERDZONEGO mastera dla REF-ów zamówienia — dograj i potwierdź wzorzec w bazie artworków, potem cofnij do „Oczekiwanie na artwork”, by podpiąć go automatycznie.",
    "zatwierdzone_do_wyplyniecia": "Zatwierdzone do wypłynięcia — gotowe do wysyłki.",
    "artwork_zgodne":              "Artwork zgodny ze wzorcem — zatwierdzony.",
    "artwork_niezgodne":           "Artwork niezgodny ze wzorcem — wymaga poprawy u dostawcy.",
    "artwork_bledne":              "Artwork błędny — krytyczne rozbieżności, proces wstrzymany.",
    "etd_sap":                     "Porównanie planowanej daty wypłynięcia (ETD) z danymi w SAP.",
    "zlecenie_spedycja":           "Wysłanie zlecenia transportowego do spedycji.",
    "agent_przypisany":            "Agent / spedytor przypisany do dostawy.",
    "packing_list_oczekiwanie":    "Oczekiwanie na packing list od dostawcy.",
    "packing_list_otrzymany":      "Packing list otrzymany i zweryfikowany.",
    "dokumenty_odprawa":           "Dokumenty przygotowane do odprawy celnej.",
    "dostawa_przychodzaca":        "Utworzono dostawę przychodzącą w SAP.",
    "dokumenty_wyslane_odprawa":   "Dokumenty wysłane do odprawy (faktura transportowa + tłumaczenia).",
    "sad_draft":                   "Draft SAD do potwierdzenia przez agencję celną.",
    "pzc_agencja":                 "PZC (poświadczenie zgłoszenia celnego) otrzymane od agencji.",
    "dostawa_magazyn":             "Oczekiwanie na potwierdzenie przyjęcia dostawy przez magazyn.",
    "rozliczenie_fiori":           "Rozliczenie faktury we FIORI + MIR7.",
    "rozliczenie_transportu":      "Potwierdzenie rozliczenia transportu — proces zakończony.",
    # Legacy (stare dostawy)
    "zaokretowane":                "Towar zaokrętowany (status archiwalny).",
    "w_transporcie":               "Przesyłka w transporcie (status archiwalny).",
    "na_miejscu":                  "Przesyłka na miejscu (status archiwalny).",
    "w_odprawie":                  "W trakcie odprawy celnej (status archiwalny).",
    "po_odprawie":                 "Po odprawie celnej (status archiwalny).",
    "w_magazynie":                 "Towar w magazynie (status archiwalny).",
    "dostarczone":                 "Dostawa dostarczona (status archiwalny).",
    "do_rozliczenia":              "Do rozliczenia (status archiwalny).",
    "rozliczone":                  "Rozliczone (status archiwalny).",
}

# Spine (canoniczna kolejność liniowa kroków).
_SPINE = [s["key"] for s in STEPS]

NEXT_STATUS = {_SPINE[i]: _SPINE[i + 1] for i in range(len(_SPINE) - 1)}
# Status proforma_otrzymana w spine ma key == "proforma_otrzymana"; kolejny to
# krok „proforma_sprawdzona" (key), ale to gałąź — advance ręczny pomija check i
# idzie do artwork_oczekiwanie. Ustawiamy NEXT tak, by spine prowadził dalej.
NEXT_STATUS["proforma_otrzymana"] = "artwork_oczekiwanie"
NEXT_STATUS["proforma_zaladowana"] = "artwork_oczekiwanie"
# Wyniki weryfikacji PO/PI → dalej do artworku.
for _vs in ("zweryfikowane", "sprawdzone_zgodne", "sprawdzone_do_weryfikacji", "sprawdzone_niezgodne"):
    NEXT_STATUS[_vs] = "artwork_oczekiwanie"
# Krok artwork_oczekiwanie/zatwierdzone → ETD-SAP; wyniki artworku też.
NEXT_STATUS["artwork_oczekiwanie"] = "etd_sap"
NEXT_STATUS["zatwierdzone_do_wyplyniecia"] = "etd_sap"
# Auto-master: master załadowany → czeka na fabryczny → (weryfikacja ustawia wynik artworku;
# ręczny advance kieruje dalej jak artwork_oczekiwanie).
NEXT_STATUS["artwork_master_zaladowany"] = "artwork_czeka_fabryczny"
NEXT_STATUS["artwork_czeka_fabryczny"] = "etd_sap"
# Brak mastera → po dograniu wzorca wraca do oczekiwania (auto-ładowanie spróbuje ponownie).
NEXT_STATUS["artwork_brak_mastera"] = "artwork_oczekiwanie"
for _vs in ("artwork_zgodne", "artwork_niezgodne", "artwork_bledne"):
    NEXT_STATUS[_vs] = "etd_sap"
# Legacy statusy → wpięcie w nowy łańcuch (żeby stare dostawy mogły iść dalej).
NEXT_STATUS["zaokretowane"]   = "zlecenie_spedycja"
NEXT_STATUS["w_transporcie"]  = "packing_list_oczekiwanie"
NEXT_STATUS["na_miejscu"]     = "packing_list_otrzymany"
NEXT_STATUS["w_odprawie"]     = "dostawa_przychodzaca"
NEXT_STATUS["po_odprawie"]    = "dostawa_magazyn"
NEXT_STATUS["w_magazynie"]    = "rozliczenie_fiori"
NEXT_STATUS["dostarczone"]    = "rozliczenie_fiori"
NEXT_STATUS["do_rozliczenia"] = "rozliczenie_transportu"
# rozliczenie_transportu / rozliczone — koniec (brak NEXT).

NEXT_LABELS = {
    "utworzone":                 "Wyślij do dostawcy",
    "wyslane_do_dostawcy":       "Czekam na proformę",
    "proforma_oczekiwana":       "Proforma otrzymana",
    "proforma_otrzymana":        "Dalej (artwork)",
    "proforma_zaladowana":       "Dalej (artwork)",
    "zweryfikowane":             "Dalej (artwork)",
    "sprawdzone_zgodne":         "Dalej (artwork)",
    "sprawdzone_do_weryfikacji": "Dalej (artwork)",
    "sprawdzone_niezgodne":      "Dalej (artwork)",
    "artwork_oczekiwanie":       "Dalej (ETD — SAP)",
    "artwork_master_zaladowany": "Czeka na artwork fabryczny",
    "artwork_czeka_fabryczny":   "Dalej (ETD — SAP)",
    "artwork_brak_mastera":      "Ponów — oczekiwanie na artwork",
    "zatwierdzone_do_wyplyniecia": "Dalej (ETD — SAP)",
    "artwork_zgodne":            "Dalej (ETD — SAP)",
    "artwork_niezgodne":         "Dalej (ETD — SAP)",
    "artwork_bledne":            "Dalej (ETD — SAP)",
    "etd_sap":                   "Wyślij zlecenie do spedycji",
    "zlecenie_spedycja":         "Przypisz agenta",
    "agent_przypisany":          "Czekaj na packing list",
    "packing_list_oczekiwanie":  "Packing list otrzymany",
    "packing_list_otrzymany":    "Przygotuj dokumenty do odprawy",
    "dokumenty_odprawa":         "Utwórz dostawę przychodzącą",
    "dostawa_przychodzaca":      "Wyślij dokumenty do odprawy",
    "dokumenty_wyslane_odprawa": "Draft SAD do potwierdzenia",
    "sad_draft":                 "PZC od agencji",
    "pzc_agencja":               "Potwierdzenie dostawy (magazyn)",
    "dostawa_magazyn":           "Rozliczenie FIORI + MIR7",
    "rozliczenie_fiori":         "Potwierdź rozliczenie transportu",
}

# Kroki wymagające świadomego zatwierdzenia managera.
# (Ustalenia: Draft SAD i Rozliczenie transportu BEZ specjalnego zatwierdzenia;
#  zostaje tylko potwierdzenie dostawy przez magazyn.)
APPROVAL_STEPS = {"dostawa_magazyn"}

PROFORMA_LOADED_STATUS = "proforma_zaladowana"
VERIFY_RESULT_STATUSES = {"sprawdzone_zgodne", "sprawdzone_do_weryfikacji", "sprawdzone_niezgodne"}
ARTWORK_AWAITING_STATUS = "artwork_oczekiwanie"
ARTWORK_RESULT_STATUSES = {"artwork_zgodne", "artwork_niezgodne", "artwork_bledne"}

# Liniowa kolejność (spine) — do auto-advance i wyznaczania pozycji.
STATUS_ORDER = list(_SPINE)

# Poprzednik statusu (spine) + gałęzie/legacy ręcznie.
PREV_STATUS = {_SPINE[i]: _SPINE[i - 1] for i in range(1, len(_SPINE))}
# _SPINE to klucze KROKÓW, a kroki 4 i 6 to gałęzie ("proforma_sprawdzona",
# "artwork_sprawdzony") — NIE są realnymi statusami (brak w STATUS_LABELS,
# step_index=-1). Bez nadpisania revert z "artwork_oczekiwanie"/"etd_sap" lądował
# w statusie-widmie. Wskaż realnych poprzedników (zgodnie z NEXT_STATUS).
PREV_STATUS["artwork_oczekiwanie"] = "proforma_otrzymana"
PREV_STATUS["etd_sap"] = "artwork_oczekiwanie"
PREV_STATUS["proforma_zaladowana"] = "proforma_otrzymana"
for _vs in VERIFY_RESULT_STATUSES | {"zweryfikowane"}:
    PREV_STATUS[_vs] = "proforma_otrzymana"   # revert z weryfikacji → przed sprawdzeniem (#22)
PREV_STATUS["zatwierdzone_do_wyplyniecia"] = "artwork_oczekiwanie"
for _vs in ARTWORK_RESULT_STATUSES:
    PREV_STATUS[_vs] = "artwork_oczekiwanie"
# Legacy — poprzednik = nowy odpowiednik wcześniejszego kroku.
PREV_STATUS["zaokretowane"]   = "etd_sap"
PREV_STATUS["w_transporcie"]  = "agent_przypisany"
PREV_STATUS["na_miejscu"]     = "packing_list_oczekiwanie"
PREV_STATUS["w_odprawie"]     = "dokumenty_odprawa"
PREV_STATUS["po_odprawie"]    = "pzc_agencja"
PREV_STATUS["w_magazynie"]    = "dostawa_magazyn"
PREV_STATUS["dostarczone"]    = "dostawa_magazyn"
PREV_STATUS["do_rozliczenia"] = "rozliczenie_fiori"
PREV_STATUS["rozliczone"]     = "rozliczenie_transportu"

# Dokumenty wymagane do WEJŚCIA w dany status (miękka bramka).
REQUIRED_DOCS = {
    "proforma_otrzymana":        ["PI"],
    "proforma_sprawdzona":       ["PO", "PI"],
    "packing_list_otrzymany":    ["PL"],
    "dokumenty_wyslane_odprawa": ["CI", "PL"],
    "sad_draft":                 ["SAD"],
}

# Upload dokumentu → status docelowy auto-przesunięcia (kroki bez zatwierdzenia).
AUTOADVANCE_TARGET = {
    "PL":  "packing_list_otrzymany",
}


# ── Czyste funkcje ────────────────────────────────────────────────────────────
def step_index(status: str) -> int:
    """Indeks kroku (0..19) dla statusu w PŁASKIM stepperze; -1 dla nieznanego."""
    for i, st in enumerate(STEPS):
        if status in st["statuses"]:
            return i
    return -1


def phase_index(status: str) -> int:
    """Indeks coarse-fazy (0..7) dla statusu; 0 dla pustego/nieznanego."""
    for i, ph in enumerate(PHASES):
        if status in ph["statuses"]:
            return i
    return 0


def next_status(status: str):
    return NEXT_STATUS.get(status)


def prev_status(status: str):
    return PREV_STATUS.get(status)


def auto_advance_target(doc_type: str, current: str):
    """Status docelowy auto-przesunięcia po wgraniu doc_type, albo None.
    Przesuwa wyłącznie w przód i NIE przekracza kroku zatwierdzenia."""
    target = AUTOADVANCE_TARGET.get((doc_type or "").upper())
    if not target or current not in STATUS_ORDER or target not in STATUS_ORDER:
        return None
    ci, ti = STATUS_ORDER.index(current), STATUS_ORDER.index(target)
    if ti <= ci:
        return None
    for s in STATUS_ORDER[ci + 1: ti + 1]:
        if s in APPROVAL_STEPS:
            return None
    return target


def missing_required_docs(target_status: str, present_types) -> list:
    req = REQUIRED_DOCS.get(target_status, [])
    present = {(t or "").upper() for t in (present_types or [])}
    return [d for d in req if d not in present]


def missing_slots(present_types) -> list:
    present = {(t or "").upper() for t in (present_types or [])}
    return [t for t in REQUIRED_SLOT_TYPES if t not in present]


def status_from_comparison(comp_status: str) -> str:
    """Wynik porównania PO/PI → status (gałąź Weryfikacja)."""
    s = (comp_status or "").strip().lower()
    if s in ("error", "critical", "niezgodne", "roznica", "blad"):
        return "sprawdzone_niezgodne"
    if s in ("warning", "watpliwe", "do_weryfikacji", "ostrzezenie"):
        return "sprawdzone_do_weryfikacji"
    return "sprawdzone_zgodne"


def proforma_loaded_target(current: str):
    """Po wgraniu PI → PROFORMA_LOADED_STATUS, gdy dostawa jeszcze wczesna."""
    early = {"utworzone", "wyslane_do_dostawcy", "proforma_oczekiwana",
             "proforma_otrzymana"}
    return PROFORMA_LOADED_STATUS if current in early else None


def status_from_artwork_comparison(risk_level: str) -> str:
    s = (risk_level or "").strip().lower()
    if s in ("error", "critical", "bledne", "blad"):
        return "artwork_bledne"
    if s in ("warning", "niezgodne", "roznica"):
        return "artwork_niezgodne"
    return "artwork_zgodne"


def artwork_awaiting_target(current: str):
    """Po wgraniu artworku fabrycznego: ARTWORK_AWAITING_STATUS, gdy dostawa nie
    wyszła poza fazę artworku."""
    if current in ARTWORK_RESULT_STATUSES:
        return None
    return ARTWORK_AWAITING_STATUS if phase_index(current) <= 2 else None


# ── Proste SLA: maks. dni w danym statusie ────────────────────────────────────
SLA_LIMITS = {
    "proforma_oczekiwana":       7,
    "proforma_otrzymana":        5,
    "sprawdzone_do_weryfikacji": 5,
    "artwork_oczekiwanie":       7,
    "etd_sap":                   3,
    "zlecenie_spedycja":         3,
    "agent_przypisany":          5,
    "packing_list_oczekiwanie": 30,
    "dokumenty_odprawa":         5,
    "sad_draft":                 5,
    "pzc_agencja":               7,
    "rozliczenie_fiori":        14,
}


def sla_overdue(status: str, days_in_status):
    limit = SLA_LIMITS.get(status)
    if limit is None or days_in_status is None:
        return (False, limit)
    # Próg włącznie: po osiągnięciu limitu dni status jest już przeterminowany
    # (>= zamiast >, inaczej alert SLA odpalał dzień za późno).
    return (days_in_status >= limit, limit)
