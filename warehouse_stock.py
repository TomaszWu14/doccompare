"""Stany magazynowe per asortyment (REF) — import z XLSX/CSV.

Trzyma bieżący stan na magazynie i średnie zużycie miesięczne per REF, żeby panel
analityczny pod /dostawy mógł policzyć „na ile wystarczy" (zapas + w drodze ÷ zużycie).
Dopasowanie do pozycji PO po znormalizowanym REF (jak material_master/artwork_index).
"""
from __future__ import annotations

import re

from material_master import normalize_ref


def ensure_table(db) -> None:
    if getattr(db, "_ws_ensured", False):
        return
    db.execute("""CREATE TABLE IF NOT EXISTS warehouse_stock (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ref_code TEXT DEFAULT '',
        ref_norm TEXT NOT NULL,
        on_hand REAL DEFAULT 0,
        monthly_usage REAL DEFAULT 0,
        out_deliveries REAL DEFAULT 0,
        out_confirmed REAL DEFAULT 0,
        out_unconfirmed REAL DEFAULT 0,
        updated_at TEXT DEFAULT (datetime('now'))
    )""")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_warehouse_stock_norm "
               "ON warehouse_stock(ref_norm)")
    db.commit()
    # Dla istniejących tabel — dodaj nowe kolumny wychodzące (idempotentnie).
    for _col in ("out_deliveries", "out_confirmed", "out_unconfirmed"):
        try:
            db.execute(f"ALTER TABLE warehouse_stock ADD COLUMN {_col} REAL DEFAULT 0")
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
    try:
        db._ws_ensured = True
    except (AttributeError, TypeError):
        pass


def _num(v):
    """Parsuje liczbę z formatów PL/EN ('1 200,5' / '1,200.5' / '300'). None gdy brak."""
    s = str(v or "").strip()
    if not s:
        return None
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s:
        return None
    # Jeśli są i kropka, i przecinek — ostatni separator jest dziesiętny.
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _hdr(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower()).replace(".", "")


_REF_HDRS = {"ref", "ref_code", "ref code", "indeks", "kod", "kod produktu",
             "kod_produktu", "material", "materiał", "sku"}
_STOCK_HDRS = {"stan", "stan magazynowy", "on_hand", "on hand", "ilosc", "ilość",
               "zapas", "quantity", "qty", "stan magazynu", "na stanie"}
_USAGE_HDRS = {"zuzycie", "zużycie", "zuzycie miesieczne", "zużycie miesięczne",
               "monthly_usage", "monthly usage", "zapotrzebowanie", "rotacja",
               "srednie zuzycie", "średnie zużycie", "usage", "zuzycie/mies",
               "zużycie/mies", "zuzycie mies", "miesieczne zuzycie"}


def import_rows(db, raw_rows: list) -> dict:
    """Import z listy wierszy (header 1-wierszowy). Kolumny (tolerancyjne nazwy):
    REF, stan magazynowy, zużycie miesięczne, oraz wychodzące: w dostawach,
    w zleceniach potwierdzonych, w zleceniach niepotwierdzonych. Upsert po REF."""
    ensure_table(db)
    rows = [list(r) for r in raw_rows if isinstance(r, (list, tuple))]
    if not rows:
        return {"imported": 0, "skipped": 0}
    headers = [_hdr(c) for c in rows[0]]
    ci_ref = ci_stock = ci_usage = None
    ci_out_dlv = ci_out_conf = ci_out_unconf = None
    for i, h in enumerate(headers):
        # Wychodzące — dopasowanie po fragmentach (odporne na literówki w nagłówkach).
        if "wychodz" in h:
            if "niepotwierdz" in h and ci_out_unconf is None:
                ci_out_unconf = i
            elif "potwierdz" in h and ci_out_conf is None:
                ci_out_conf = i
            elif "dostaw" in h and ci_out_dlv is None:
                ci_out_dlv = i
            continue
        if ci_ref is None and h in _REF_HDRS:
            ci_ref = i
        elif ci_stock is None and h in _STOCK_HDRS:
            ci_stock = i
        elif ci_usage is None and h in _USAGE_HDRS:
            ci_usage = i
    if ci_ref is None:
        return {"imported": 0, "skipped": 0,
                "error": "Brak kolumny REF (np. 'REF'/'indeks'/'kod')"}

    def cell(row, ci):
        return _num(row[ci]) if (ci is not None and ci < len(row)) else None

    def num(row, ci):
        # Zachowaj rzeczywiste 0.0 — `cell(...) or 0` zgubiłoby poprawne zero.
        v = cell(row, ci)
        return v if v is not None else 0

    imported, skipped = 0, 0
    for row in rows[1:]:
        if ci_ref is None or ci_ref >= len(row):   # BUGFIX: None >= int → TypeError
            skipped += 1
            continue
        ref = str(row[ci_ref] or "").strip()
        rn = normalize_ref(ref)
        if not rn:
            skipped += 1
            continue
        db.execute(
            "INSERT INTO warehouse_stock(ref_code, ref_norm, on_hand, monthly_usage, "
            "out_deliveries, out_confirmed, out_unconfirmed, updated_at) "
            "VALUES(?,?,?,?,?,?,?, datetime('now')) "
            "ON CONFLICT(ref_norm) DO UPDATE SET ref_code=excluded.ref_code, "
            "on_hand=excluded.on_hand, monthly_usage=excluded.monthly_usage, "
            "out_deliveries=excluded.out_deliveries, out_confirmed=excluded.out_confirmed, "
            "out_unconfirmed=excluded.out_unconfirmed, updated_at=datetime('now')",
            (ref[:100], rn, num(row, ci_stock), num(row, ci_usage),
             num(row, ci_out_dlv), num(row, ci_out_conf),
             num(row, ci_out_unconf)))
        imported += 1
    db.commit()
    return {"imported": imported, "skipped": skipped}


def get_stock_map(db) -> dict:
    """{ref_norm: {on_hand, monthly_usage, out_deliveries, out_confirmed, out_unconfirmed}}."""
    ensure_table(db)
    out = {}
    for r in db.execute(
            "SELECT ref_norm, on_hand, monthly_usage, out_deliveries, out_confirmed, "
            "out_unconfirmed FROM warehouse_stock").fetchall():
        out[r["ref_norm"]] = {
            "on_hand": r["on_hand"] or 0,
            "monthly_usage": r["monthly_usage"] or 0,
            "out_deliveries": r["out_deliveries"] or 0,
            "out_confirmed": r["out_confirmed"] or 0,
            "out_unconfirmed": r["out_unconfirmed"] or 0,
        }
    return out


def list_stock(db, limit: int = 1000) -> list:
    ensure_table(db)
    return [dict(r) for r in db.execute(
        "SELECT ref_code, ref_norm, on_hand, monthly_usage, out_deliveries, "
        "out_confirmed, out_unconfirmed, updated_at "
        "FROM warehouse_stock ORDER BY ref_code LIMIT ?", (limit,)).fetchall()]
