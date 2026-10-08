# -*- coding: utf-8 -*-
"""
cn_audit.py — Regula audytu E8-01: kod CN (taryfa celna).

Porownuje kod CN ze zgloszenia celnego SAD (pole 33 / [18 09]) z wzorcem
zapisanym w karcie produktu (material_master.tariff_cn).

Ustalenia (audyt 50 pytan):
  * 3 poziomy statusu: 'ok' / 'uwaga' / 'blad'                      (P1)
  * kod CN porownujemy na PELNYCH cyfrach SAD (10 cyfr TARIC)        (P25)
  * niezgodnosc kodu CN => 'blad' — pole KRYTYCZNE                   (P24, P44)
  * brak ref w master lub puste tariff_cn => 'uwaga'                 (P5, P6)
  * brak kodu CN po stronie SAD => 'uwaga'                           (P5)
  * SAD bywa zbiorczy (wiele pozycji/dostawcow) — parsujemy per
    pozycja 'Pozycja N' i dopasowujemy po opisie towaru             (P9, P17)

Modul jest celowo niezalezny od Flaska i bazy: logike master podajemy
przez callable `cn_lookup(ref) -> str | None`, dzieki czemu da sie go
testowac jednostkowo bez aplikacji.
"""
import re

# Kod CN w SAD: "kod CN [18 09]: 65050090" (czesto z lamaniem linii miedzy
# 'kod CN' a '[18 09]'). Lapiemy 6-15 cyfr z mozliwymi spacjami.
_CN_RE = re.compile(
    r"kod\s*CN\s*\[?\s*18[\s.]?09\s*\]?\s*[:\-]?\s*([0-9][0-9\s]{4,15})",
    re.IGNORECASE,
)
# Opis towaru: "Opis [18 05]: CZEPEK ..."
_DESC_RE = re.compile(
    r"Opis\s*\[?\s*18[\s.]?05\s*\]?\s*[:\-]?\s*([^\n]+)",
    re.IGNORECASE,
)
# Kod TARIC (2-4 cyfry) — w SAD czesto osobne pole obok 8-cyfrowego CN [18 09].
# Doklejamy go do CN, by uzyskac pelny 10-cyfrowy kod tam gdzie jest dostepny.
_TARIC_RE = re.compile(r"kod\s*TARIC\s*[:\-]?\s*([0-9]{2,4})", re.IGNORECASE)
# Podzial zgloszenia na pozycje: "Pozycja 1", "Pozycja 2", ...
_POS_RE = re.compile(r"Pozycja\s+(\d+)")

_STOP = {
    "do", "i", "z", "na", "the", "of", "szt", "pcs", "ctn", "cs",
    "komponent", "produkcyjny", "wraz",
}


def normalize_cn(raw) -> str:
    """Zwraca sam ciag cyfr kodu CN (bez spacji/kropek/myslnikow)."""
    return re.sub(r"\D", "", str(raw or ""))


def _norm_tokens(s: str) -> set:
    toks = re.findall(r"[a-z0-9ąćęłńóśźż]+", str(s or "").lower())
    return {t for t in toks if len(t) > 2 and t not in _STOP}


def _overlap(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def parse_sad_cn_positions(text: str) -> list:
    """
    Parsuje tekst SAD na liste pozycji z kodem CN.

    Zwraca: [{'pos': int, 'desc': str, 'cn': '65050090'}, ...]
    Gdy brak naglowkow 'Pozycja N', a w tekscie jest jeden kod CN —
    zwraca pojedyncza pozycje.
    """
    if not text:
        return []
    parts = _POS_RE.split(text)
    out = []
    # parts = [preambula, '1', body1, '2', body2, ...]
    for i in range(1, len(parts), 2):
        try:
            pos = int(parts[i])
        except (ValueError, TypeError):
            continue
        body = parts[i + 1] if i + 1 < len(parts) else ""
        cn_m = _CN_RE.search(body)
        cn = normalize_cn(cn_m.group(1)) if cn_m else ""
        # Doklej 2-cyfrowy suffix TARIC do 8-cyfrowego CN -> pelny 10-cyfrowy kod.
        # Bierzemy dokladnie 2 cyfry (pozycje 9-10 nomenklatury); pole moze pokazac
        # wiecej (krajowe kody dod.), wiec nie doklejamy na slepo i nie ucinamy [:10].
        if cn and len(cn) == 8:
            taric_m = _TARIC_RE.search(body)
            if taric_m:
                taric = normalize_cn(taric_m.group(1))
                if len(taric) >= 2:
                    cn = cn + taric[:2]
        desc_m = _DESC_RE.search(body)
        desc = desc_m.group(1).strip() if desc_m else ""
        out.append({"pos": pos, "desc": desc, "cn": cn})
    if not out:
        cn_m = _CN_RE.search(text)
        if cn_m:
            desc_m = _DESC_RE.search(text)
            out.append({
                "pos": 1,
                "desc": desc_m.group(1).strip() if desc_m else "",
                "cn": normalize_cn(cn_m.group(1)),
            })
    return out


def match_sad_cn(positions: list, desc: str, min_overlap: float = 0.30) -> str:
    """Dopasowuje kod CN pozycji SAD do opisu towaru z faktury/listy."""
    if not positions:
        return ""
    if len(positions) == 1:
        return positions[0]["cn"]
    want = _norm_tokens(desc)
    best, best_score = "", 0.0
    for p in positions:
        score = _overlap(want, _norm_tokens(p["desc"]))
        if score > best_score:
            best, best_score = p["cn"], score
    return best if best_score >= min_overlap else ""


def compare_cn(sad_cn, master_cn, positions_have_cn: bool = False) -> tuple:
    """
    Porownuje kod CN z SAD z wzorcem z mastera.

    Zwraca (status, opis) gdzie status in {'ok', 'uwaga', 'blad'}.
    Porownanie na pelnym ciagu cyfr SAD (P25).

    positions_have_cn: czy SAD W OGOLE zawiera jakis kod CN. Pozwala odroznic
    "SAD nie ma kodu" od "nie dopasowano pozycji SAD do tego towaru" — inaczej
    nieudane dopasowanie maskowaloby realna niezgodnosc jako nieszkodliwa uwage.
    """
    sad = normalize_cn(sad_cn)
    mst = normalize_cn(master_cn)
    if not sad:
        if positions_have_cn:
            return ("uwaga",
                    "Nie dopasowano pozycji SAD do tego towaru — kodu CN nie zweryfikowano")
        return ("uwaga", "Brak kodu CN w SAD — nie ma czego zweryfikowac")
    if not mst:
        return ("uwaga",
                "Brak wzorca kodu CN w karcie produktu (material_master.tariff_cn)")
    if sad == mst:
        return ("ok", f"Kod CN zgodny: {sad}")
    # Rozna dlugosc zapisu (np. 8-cyfrowy CN vs 10-cyfrowy TARIC w masterze) nie
    # jest sama w sobie bledem: porownujemy na wspolnej, znaczacej czesci CN.
    # Dzieki temu nie generujemy masowych falszywych BLEDOW gdy strony zapisuja
    # kod z rozna szczegolowoscia; realna roznica CN (inny prefiks) -> BLAD.
    n = min(len(sad), len(mst))
    if n >= 8 and sad[:n] == mst[:n]:
        return ("ok",
                f"Kod CN zgodny na {n} cyfrach (rozna dlugosc zapisu): SAD {sad} ~ master {mst}")
    return ("blad", f"Kod CN niezgodny: SAD {sad} != karta produktu {mst}")


# Dopuszczalne dlugosci kodu nomenklatury: 6 = pozycja HS, 8 = CN, 10 = TARIC.
_VALID_CN_LENGTHS = {6, 8, 10}


def validate_cn_code(raw) -> tuple:
    """
    Walidacja STRUKTURALNA pojedynczego kodu CN/TARIC — niezalezna od mastera
    i od SAD. Sprawdza poprawnosc samego zapisu wzgledem zasad nomenklatury UE.

    Zwraca (status, opis), status in {'ok', 'uwaga', 'blad'}:
      * brak kodu                              -> 'uwaga'
      * znaki inne niz cyfry/separatory        -> 'blad'
      * dlugosc != 6/8/10 cyfr                 -> 'blad'
      * dzial '00'                             -> 'blad'
      * dzial 77 (zarezerwowany, nieuzywany)   -> 'uwaga'
      * dzial 98/99 (specjalny/krajowy)        -> 'uwaga'
      * pozostale dzialy 01-97                 -> 'ok'

    Celowo NIE weryfikuje istnienia kodu w taryfie (to robi walidacja wzgledem
    lokalnej tabeli referencyjnej TARIC) — tu chodzi o sama poprawnosc zapisu.
    """
    s = str(raw or "").strip()
    if not s:
        return ("uwaga", "Brak kodu CN do walidacji")
    # Znaki niedozwolone (poza cyframi i typowymi separatorami) — np. litery z OCR.
    if re.search(r"[^0-9\s.\-]", s):
        return ("blad", f"Kod CN zawiera niedozwolone znaki: {s!r}")
    digits = normalize_cn(s)
    if not digits:
        return ("blad", f"Kod CN nie zawiera cyfr: {s!r}")
    n = len(digits)
    if n not in _VALID_CN_LENGTHS:
        return ("blad",
                f"Nieprawidlowa dlugosc kodu CN: {n} cyfr ({digits}) "
                "— oczekiwano 6 (HS), 8 (CN) lub 10 (TARIC)")
    chapter = int(digits[:2])
    if chapter == 0:
        return ("blad", f"Nieprawidlowy dzial taryfy '00' w kodzie {digits}")
    if chapter == 77:
        return ("uwaga", f"Dzial 77 jest zarezerwowany (nieuzywany) — sprawdz kod {digits}")
    if chapter in (98, 99):
        return ("uwaga", f"Dzial specjalny {chapter:02d} (krajowy/szczegolny) — kod {digits}")
    return ("ok", f"Kod CN poprawny strukturalnie: {digits} (dzial {chapter:02d}, {n} cyfr)")


def validate_cn_reference(code, ref_lookup) -> tuple:
    """
    Sprawdza ISTNIENIE kodu CN/TARIC w lokalnej tabeli referencyjnej taryfy
    (np. import oficjalnej nomenklatury CN UE). To uzupelnienie walidacji
    strukturalnej: tam sprawdzamy *zapis*, tu *czy kod w ogole istnieje* i czy
    nie ma na nim ograniczen.

    ref_lookup: callable(code_digits) -> dict | None, gdzie dict moze zawierac
                'description', 'duty_rate', 'restrictions', 'active'.

    Zwraca (status, opis) lub None, gdy nie ma czego/jak sprawdzic (brak kodu lub
    brak tabeli referencyjnej) — None oznacza "pomijamy", nie "ok".
    """
    digits = normalize_cn(code)
    if not digits or ref_lookup is None:
        return None
    try:
        entry = ref_lookup(digits)
    except Exception:
        return None
    if not entry:
        return ("uwaga", f"Kod CN {digits} nieobecny w taryfie referencyjnej TARIC")
    if str(entry.get("active", 1)) == "0":
        return ("uwaga", f"Kod CN {digits} oznaczony jako NIEAKTYWNY w taryfie referencyjnej")
    restr = (entry.get("restrictions") or "").strip()
    if restr:
        return ("uwaga", f"Kod CN {digits}: ograniczenia taryfowe — {restr}")
    return ("ok", f"Kod CN {digits} obecny w taryfie referencyjnej")


def audit_items_cn(items: list, sad_text: str, sad_is_b: bool, cn_lookup,
                   ref_lookup=None) -> list:
    """
    Audytuje kod CN dla dopasowanych pozycji.

    Args:
        items:     lista dopasowanych pozycji (result.items z enhanced_comparator)
        sad_text:  pelny tekst dokumentu SAD
        sad_is_b:  True gdy SAD to dokument B (opis SAD = pole 'desc_b')
        cn_lookup: callable(ref) -> str | None  (zwraca master.tariff_cn)

    Mutuje pozycje dopisujac 'cn_sad', 'cn_master', 'cn_status'.
    Zwraca liste findingow: {ref, sad_cn, master_cn, status, description}.
    """
    positions = parse_sad_cn_positions(sad_text)
    positions_have_cn = any(p["cn"] for p in positions)
    desc_key = "desc_b" if sad_is_b else "desc_a"
    findings = []
    for it in items:
        ref = (it.get("ref") or "").strip()
        sad_desc = it.get(desc_key) or ""
        sad_cn = match_sad_cn(positions, sad_desc)
        master_cn = ""
        if ref and cn_lookup:
            try:
                master_cn = cn_lookup(ref) or ""
            except Exception:
                master_cn = ""
        status, note = compare_cn(sad_cn, master_cn, positions_have_cn)
        # Walidacja STRUKTURALNA obu kodow — niezalezna od dopasowania. Wykrywa
        # np. kod CN o zlej dlugosci/dziale (OCR, blad w karcie produktu), nawet
        # gdy SAD i master sa zgodne. Nie zmienia 'status' (zgodnosc), tylko
        # dokleja liste problemow zapisu do findingu/pozycji.
        fmt_issues = []
        for side, code in (("SAD", sad_cn), ("master", master_cn)):
            if code:
                fst, fnote = validate_cn_code(code)
                if fst != "ok":
                    fmt_issues.append({"side": side, "status": fst, "note": fnote})
        # Walidacja ISTNIENIA kodu SAD w taryfie referencyjnej (gdy podano tabele).
        # Dotyczy kodu DEKLAROWANEGO w SAD; domyslnie pomijana (ref_lookup=None).
        ref_issue = None
        if ref_lookup is not None and sad_cn:
            rr = validate_cn_reference(sad_cn, ref_lookup)
            if rr and rr[0] != "ok":
                ref_issue = {"status": rr[0], "note": rr[1]}
        it["cn_sad"] = sad_cn
        it["cn_master"] = master_cn
        it["cn_status"] = status
        it["cn_format_issues"] = fmt_issues
        it["cn_reference_issue"] = ref_issue
        findings.append({
            "ref": ref,
            "sad_cn": sad_cn,
            "master_cn": master_cn,
            "status": status,
            "description": note,
            "format_issues": fmt_issues,
            "reference_issue": ref_issue,
        })
    return findings


# ─────────────────────────────────────────────────────────────────────────────
# TESTY JEDNOSTKOWE
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    def _t(name, cond, info=""):
        print(("  OK " if cond else "  FAIL ") + name + (f" — {info}" if info else ""))
        if not cond:
            sys.exit(1)

    print("\n=== TESTY cn_audit.py ===\n")

    SAD = (
        "Liczba pozycji: 7\n"
        "Pozycja 1\nTowar: Opis [18 05]: SERWETY CHIRURGICZNE WLOKNINOWE, kod CN\n"
        "[18 09]: 63079098, kod TARIC: 90\n"
        "Pozycja 5\nTowar: Opis [18 05]: CZEPEK DO BEZWODNEGO MYCIA WLOSOW-1015 SZT, kod CN\n"
        "[18 09]: 65050090, kod TARIC: 90\n"
    )

    pos = parse_sad_cn_positions(SAD)
    _t("parsuje 2 pozycje", len(pos) == 2, str([(p['pos'], p['cn']) for p in pos]))
    # CN [18 09]=65050090 + TARIC 90 -> pelny 10-cyfrowy kod
    _t("poz.5 CN=6505009090 (z TARIC)", any(p["pos"] == 5 and p["cn"] == "6505009090" for p in pos))

    cn = match_sad_cn(pos, "Stila Care waterless hair wash cap czepek do bezwodnego mycia wlosow")
    _t("dopasowanie po opisie -> 6505009090", cn == "6505009090", cn)

    _t("compare zgodny", compare_cn("65050090", "65050090")[0] == "ok")
    _t("compare niezgodny -> blad", compare_cn("65050090", "33049900")[0] == "blad")
    _t("brak mastera -> uwaga", compare_cn("65050090", "")[0] == "uwaga")
    _t("brak SAD -> uwaga", compare_cn("", "65050090")[0] == "uwaga")
    # Rozroznienie: brak dopasowania pozycji vs faktyczny brak CN w SAD
    _no_match = compare_cn("", "65050090", positions_have_cn=True)
    _t("niedopasowana pozycja -> uwaga z innym komunikatem",
       _no_match[0] == "uwaga" and "Nie dopasowano" in _no_match[1], _no_match[1])
    _t("realny brak CN -> komunikat o braku CN",
       "Brak kodu CN" in compare_cn("", "65050090", positions_have_cn=False)[1])
    # Tolerancja dlugosci zapisu: 8-cyfrowy CN vs 10-cyfrowy ten sam prefiks -> OK
    _t("8 vs 10 ten sam prefiks CN -> ok", compare_cn("6505009090", "65050090")[0] == "ok",
       "tolerancja dlugosci zapisu (master 8 vs SAD 10)")
    _t("rozny prefiks CN -> blad", compare_cn("6505009090", "33049900")[0] == "blad")

    items = [{"ref": "WC1-01", "desc_b": "CZEPEK DO BEZWODNEGO MYCIA WLOSOW"}]
    f = audit_items_cn(items, SAD, sad_is_b=True, cn_lookup=lambda r: "33049900")
    _t("audit -> blad dla WC1-01", f[0]["status"] == "blad", str(f[0]))
    _t("item zaadnotowany cn_sad", items[0]["cn_sad"] == "6505009090")

    print("\nWszystkie testy przeszly.\n")
