"""
po_parsing.py — czyste funkcje parsujące dane z dokumentów PO.

Wydzielone z app.py, by były testowalne bez Flaska (bramka CI uruchamia testy
bez ciężkich zależności webowych) i odciążyć monolit. Operują na połączeniu DB
przekazanym przez wywołującego — żadnych globalnych efektów ubocznych.
"""
from __future__ import annotations

import re


def clean_po_ref(db, raw, opis: str = ""):
    """Wyodrębnia czysty REF z komórki, w której PDF skleił REF + opis
    (np. 'RNBS10001 easyCARE nitr. gloves…' → 'RNBS10001').

    Strategia:
      1) dopasowanie do master daty (najdłuższy znany REF będący prefiksem
         znormalizowanego kodu),
      2) fallback: pierwszy token, jeśli zawiera cyfrę i dalej jest tekst (opis).

    Zwraca krotkę (ref_code, opis). Gdy `raw` jest puste — zwraca ('', opis).
    """
    s = str(raw or "").strip()
    if not s:
        return "", opis
    # Pierwszy token = kod, reszta = opis ze sklejonej komórki „REF opis".
    _msplit = re.match(r"^(\S+)\s+(.*)$", s)
    tok0 = _msplit.group(1) if _msplit else s
    desc_from_raw = _msplit.group(2).strip() if _msplit else ""

    def _desc():
        # Zachowaj jawnie podany opis — chyba że to ta sama sklejona komórka co
        # raw (opis zaczyna się od REF). Wtedy odetnij REF (część po 1. tokenie),
        # żeby w OPISIE nie dublował się kod z kolumny REF.
        if opis and not str(opis).lstrip().startswith(tok0):
            return opis
        return desc_from_raw or opis or ""

    try:
        import material_master as _mm
        norm = _mm.normalize_ref(s)
        if norm:
            row = db.execute(
                "SELECT ref_code FROM material_master "
                "WHERE active=1 AND ref_norm<>'' AND ? LIKE ref_norm || '%' "
                "ORDER BY LENGTH(ref_norm) DESC, ref_code LIMIT 1", (norm,)
            ).fetchone()
            if row and row["ref_code"]:
                return row["ref_code"], _desc()
    except Exception:
        pass
    if re.search(r"\d", tok0) and len(s) > len(tok0):
        return tok0[:50], _desc()
    return s[:50], opis
