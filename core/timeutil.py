"""
core/timeutil.py — generyczny helper czasowy, wydzielony z app.py (Phase 3, REFACTOR-01).

Zero zależności poza stdlib. Używany przez app.py (11 miejsc) i blueprints/auth.py
(forgot_password) — stąd wspólny moduł zamiast duplikacji lub importu z app.py
(blueprint → app jest zabronione, patrz ARCHITECTURE.md).
"""
from __future__ import annotations

from datetime import datetime as _d, timedelta as _t, timezone as _z


def ts_ago(**kwargs) -> str:
    """'YYYY-MM-DD HH:MM:SS' dla teraz minus delta. Używać zamiast SQL
    datetime('now','-N ...') przy porównaniach z kolumnami TEXT — na PostgreSQL
    SQL-owy wariant tłumaczy się na NOW()+INTERVAL (timestamptz) i porównanie
    z TEXT rzuca 'operator does not exist'."""
    return (_d.now(_z.utc) - _t(**kwargs)).strftime("%Y-%m-%d %H:%M:%S")
