"""
demurrage.py — czysta logika ryzyka postojowego (demurrage + detention).

Rozdzielone od Flaska/DB, więc w pełni testowalne. Moduł:
  1. wyłuskuje daty kamieni milowych (rozładunek, odbiór, zwrot pustego) ze zdarzeń
     trackingu (SafeCube: {date, desc}; 17track: {a, z}),
  2. liczy fazę demurrage (rozładunek→odbiór) i detention (odbiór→zwrot pustego),
  3. wyznacza poziom ryzyka 🟢/🟡/🔴 i szacowany koszt po przekroczeniu free time.

Zegar demurrage startuje od FAKTYCZNEGO rozładunku, zatrzymuje się przy odbiorze.
Zegar detention startuje od odbioru, zatrzymuje się przy zwrocie pustego.
"""
from __future__ import annotations
from datetime import date, datetime
import re

# ── Słowniki zdarzeń trackingu (opis → kamień milowy) ────────────────────────
# Dopasowanie po fragmencie, bez rozróżniania wielkości liter. Kolejność list
# nie ma znaczenia — liczy się trafienie któregokolwiek wzorca.
DISCHARGE_KEYWORDS = [
    "discharge", "discharged", "unload", "unloaded", "rozładun",
    "discharged from vessel", "container discharged",
]
GATE_OUT_KEYWORDS = [
    "gate out", "gate-out", "gated out", "picked up", "pickup", "pick-up",
    "full out", "delivered to consignee", "wydanie", "odbiór", "odebrano",
]
EMPTY_RETURN_KEYWORDS = [
    "empty return", "empty returned", "empty in", "empty equipment returned",
    "return empty", "zwrot pust", "pusty zwr",
]

DEFAULTS = {
    "demurrage_free_days": 14,
    "detention_free_days": 7,
    "warn_days": 3,
    "demurrage_rate_per_day": 0.0,
    "detention_rate_per_day": 0.0,
}


# ── Parsowanie pól zdarzenia (tolerancyjne na różne API) ─────────────────────

def _event_desc(ev) -> str:
    if not isinstance(ev, dict):
        return ""
    for k in ("desc", "z", "description", "status", "event", "eventType", "name"):
        v = ev.get(k)
        if v:
            return str(v)
    return ""


def _event_date(ev) -> str:
    if not isinstance(ev, dict):
        return ""
    for k in ("date", "a", "eventDate", "dt", "datetime", "time"):
        v = ev.get(k)
        if v:
            return str(v)
    return ""


def parse_date(s) -> date | None:
    """Tolerancyjne parsowanie daty ze stringa zdarzenia → date (lub None).

    Obsługuje ISO (z czasem/strefą), 'YYYY-MM-DD', 'DD/MM/YYYY', 'DD.MM.YYYY'.
    """
    if not s:
        return None
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    if isinstance(s, datetime):
        return s.date()
    txt = str(s).strip()
    if not txt:
        return None
    # ISO z 'T' i ewentualną strefą/Z
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", txt)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    # DD/MM/YYYY lub DD.MM.YYYY
    m = re.match(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", txt)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def _matches(desc: str, keywords: list) -> bool:
    low = desc.lower()
    return any(k in low for k in keywords)


def find_event_date(events, keywords) -> date | None:
    """Zwraca NAJWCZEŚNIEJSZĄ datę zdarzenia pasującego do słów kluczowych.

    Najwcześniejsza, bo np. rozładunek to pierwsze takie zdarzenie — kolejne
    'discharge' (przeładunki) nie powinny przesuwać startu zegara w przyszłość.
    """
    if not isinstance(events, list):
        return None
    hits = []
    for ev in events:
        if _matches(_event_desc(ev), keywords):
            d = parse_date(_event_date(ev))
            if d:
                hits.append(d)
    return min(hits) if hits else None


def extract_milestone_dates(events) -> dict:
    """Wyłuskuje {discharge, gate_out, empty_return} ze zdarzeń trackingu."""
    return {
        "discharge": find_event_date(events, DISCHARGE_KEYWORDS),
        "gate_out": find_event_date(events, GATE_OUT_KEYWORDS),
        "empty_return": find_event_date(events, EMPTY_RETURN_KEYWORDS),
    }


# ── Liczenie fazy (demurrage albo detention) ─────────────────────────────────

def compute_phase(start: date | None, stop: date | None, free_days: int,
                  warn_days: int, rate: float, today: date) -> dict:
    """
    Liczy stan jednej fazy postojowej.

    start  — początek naliczania free time (None → faza nieaktywna)
    stop   — koniec naliczania (None → zegar wciąż biegnie wg 'today')
    Zwraca dict z polami:
      active, closed, days_used, free_days, days_remaining,
      over_days, level ('none'|'green'|'amber'|'red'), cost
    """
    base = {
        "active": False, "closed": False, "days_used": 0, "free_days": free_days,
        "days_remaining": None, "over_days": 0, "level": "none", "cost": 0.0,
    }
    if start is None:
        return base

    end = stop if stop is not None else today
    days_used = (end - start).days
    if days_used < 0:
        days_used = 0
    over_days = max(0, days_used - free_days)
    remaining = free_days - days_used
    cost = round(over_days * float(rate or 0), 2)
    closed = stop is not None

    if closed:
        # Faza zamknięta: czerwona tylko jeśli przekroczono free time.
        level = "red" if over_days > 0 else "green"
    else:
        if remaining <= 0:
            level = "red"
        elif remaining <= warn_days:
            level = "amber"
        else:
            level = "green"

    base.update({
        "active": True, "closed": closed, "days_used": days_used,
        "days_remaining": remaining, "over_days": over_days,
        "level": level, "cost": cost,
    })
    return base


_LEVEL_RANK = {"none": 0, "green": 1, "amber": 2, "red": 3}


def _worst(*levels) -> str:
    best = "none"
    for l in levels:
        if _LEVEL_RANK.get(l, 0) > _LEVEL_RANK[best]:
            best = l
    return best


def assess_container(dates: dict, settings: dict | None = None,
                     today: date | None = None) -> dict:
    """
    Pełna ocena kontenera: faza demurrage + detention + zbiorczy poziom i koszt.

    dates    — {discharge, gate_out, empty_return} (date|None)
    settings — nadpisania DEFAULTS (dni wolne, warn_days, stawki)
    today    — data odniesienia (domyślnie dziś)
    """
    cfg = dict(DEFAULTS)
    if settings:
        for k in cfg:
            if settings.get(k) is not None:
                cfg[k] = settings[k]
    if today is None:
        today = date.today()

    demurrage = compute_phase(
        dates.get("discharge"), dates.get("gate_out"),
        int(cfg["demurrage_free_days"]), int(cfg["warn_days"]),
        float(cfg["demurrage_rate_per_day"]), today,
    )
    detention = compute_phase(
        dates.get("gate_out"), dates.get("empty_return"),
        int(cfg["detention_free_days"]), int(cfg["warn_days"]),
        float(cfg["detention_rate_per_day"]), today,
    )
    overall = _worst(demurrage["level"], detention["level"])
    total_cost = round(demurrage["cost"] + detention["cost"], 2)
    return {
        "demurrage": demurrage,
        "detention": detention,
        "overall_level": overall,
        "total_cost": total_cost,
        "at_risk": overall in ("amber", "red"),
    }
