"""
artwork_naming.py — parser nazw plików masterów artworków ACME.

Nazwa pliku ACME koduje: REF produktu + typ opakowania + (czasem) rewizję i EAN,
np. ``GS1710123-SS_carton_sticker.pdf`` → REF ``GS1710123-SS``, typ ``carton sticker``.
``805016_pouch_2 Rev.00_5900010800810.pdf`` → REF ``805016``, typ ``pouch``,
rewizja ``Rev.00``, EAN ``5900010800810``.

Moduł jest czysty (bez zależności) i pokryty testami — używany przez bulk-upload
masterów oraz przez lokalny uploader.
"""

import os
import re

# Słownik typów opakowań (kontrolowany). Kolejność = od najdłuższych dopasowań,
# żeby "carton sticker" złapało się przed "carton".
PACKAGING_TYPES = [
    "carton sticker", "carton_sticker", "carton print", "carton_print",
    "box print", "box_print",
    "pouch", "box", "carton", "sticker", "print",
    "label", "etykieta", "tyvek", "blister", "insert", "ulotka",
    "instrukcja", "ifu",
]

_EAN_RE = re.compile(r'(\d{13})')


def _ean13_ok(code: str) -> bool:
    """Sprawdza sumę kontrolną EAN-13 — odróżnia prawdziwy EAN od przypadkowego
    13-cyfrowego ciągu (np. numeru zamówienia/telefonu) w nazwie pliku."""
    if not (code.isdigit() and len(code) == 13):
        return False
    d = [int(c) for c in code]
    chk = (10 - (sum(d[i] * (1 if i % 2 == 0 else 3) for i in range(12)) % 10)) % 10
    return chk == d[12]
# Rewizja: Rev.02 / Rev 2 / v3 / Ed.1 / Wyd.III (rzymskie). Bez końcowego \b,
# bo po numerze często stoi '_' (np. "Rev.00_5900010800810") i \b by zawiódł.
# Uwaga: gałąź samego 'v<n>' wymaga POPRZEDZAJĄCEJ SPACJI (?<=\s), a nie tylko
# granicy słowa \b — inaczej wariant kodu produktu po myślniku (np. "NL753-V40")
# zostałby błędnie zinterpretowany jako rewizja "V40", korumpując REF i ranking.
_REV_RE = re.compile(
    r'(?:(?:(?<=[\s_])|^)rev\.?\s*(\d+)'
    r'|(?<=\s)v\.?\s*(\d+)'
    r'|(?:(?<=[\s_])|^)ed\.?\s*(\d+)'
    r'|(?:(?<=[\s_])|^)wyd\.?\s*([ivxlcdm]+))',
    re.IGNORECASE,
)
# Pozostałe „śmieci" do wycięcia z REF: liczniki kopii "(2)", luźne wersje
_NOISE_RE = re.compile(r'\(\d+\)|_\d+\s*$')


def _roman_to_int(s: str) -> int:
    vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    s = s.lower()
    total, prev = 0, 0
    for ch in reversed(s):
        v = vals.get(ch, 0)
        if v < prev:
            total -= v
        else:
            total += v
            prev = v
    return total


def parse_master_filename(filename: str) -> dict:
    """Rozkłada nazwę pliku mastera na składniki.

    Zwraca dict: ref, packaging_type, revision (surowy token albo ''),
    revision_rank (int do wyboru najnowszej; 0 gdy brak), ean.
    """
    stem = os.path.splitext(os.path.basename(filename or ""))[0]

    # EAN (13 cyfr z poprawną sumą kontrolną) — wytnij z dalszego przetwarzania.
    # Bierzemy pierwszy ciąg, który faktycznie jest EAN-em, a nie dowolne 13 cyfr
    # (numer zamówienia/telefonu nie zostanie błędnie potraktowany jako EAN/REF).
    ean = ""
    for m in _EAN_RE.finditer(stem):
        if _ean13_ok(m.group(1)):
            ean = m.group(1)
            stem = stem[:m.start(1)] + " " + stem[m.end(1):]
            break

    # Rewizja
    revision = ""
    revision_rank = 0
    mr = _REV_RE.search(stem)
    if mr:
        revision = mr.group(0).strip()
        num = mr.group(1) or mr.group(2) or mr.group(3)
        if num:
            revision_rank = int(num)
        elif mr.group(4):
            revision_rank = _roman_to_int(mr.group(4))
        stem = stem[:mr.start()] + " " + stem[mr.end():]

    # Typ opakowania — REF to wszystko PRZED tokenem typu
    low = stem.lower()
    packaging_type = ""
    cut = len(stem)
    for t in PACKAGING_TYPES:
        idx = low.find(t)
        if idx != -1 and idx < cut:
            packaging_type = t.replace("_", " ")
            cut = idx
    if packaging_type:
        stem = stem[:cut]

    # Czyszczenie REF
    stem = _NOISE_RE.sub(" ", stem)
    ref = re.sub(r'[_\s]+', " ", stem).strip(" _-")

    return {
        "ref": ref,
        "packaging_type": packaging_type,
        "revision": revision,
        "revision_rank": revision_rank,
        "ean": ean,
    }


def revision_sort_key(parsed: dict, last_write_time: str = "") -> tuple:
    """Klucz sortowania do wyboru AKTUALNEJ rewizji w grupie REF+opakowanie.

    Wybrana decyzja: najwyższy znacznik Rev w nazwie, a przy braku — najnowsza
    data modyfikacji pliku (fallback). Większy klucz = nowsza rewizja.
    """
    return (parsed.get("revision_rank", 0), last_write_time or "")
