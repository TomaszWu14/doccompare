"""Wzbogaca surowe pozycje faktury o dane z master daty i ustala status dopasowania.
Dopasowanie po REF z obsługą aliasów sufiksu (X → X1). Deterministyczne, bez LLM."""

import material_master as mm
import uom
from constants import MatchStatus


def _name_pl(mat: dict) -> str:
    return (mat.get("opis_pl") or mat.get("txt_short_pl") or "").strip()


def _enrich(item: dict, mat: dict, conversions) -> None:
    item["master_ref"] = mat["ref_code"]
    item["name_pl"] = _name_pl(mat)
    item["tariff_cn"] = mat.get("tariff_cn") or ""
    item["sent"] = int(mat.get("sent") or 0)
    factor = uom.get_factor(item.get("uom_src") or "", mat.get("base_uom") or "",
                            mat["ref_code"], conversions)
    item["uom_factor"] = "" if factor is None else str(factor)


def _blank(item: dict, status: str) -> None:
    item["master_ref"] = ""
    item["name_pl"] = ""
    item["tariff_cn"] = ""
    item["sent"] = 0
    item["uom_factor"] = ""
    item["match_status"] = status


def _fill_weight(item: dict, weight_map, filled_refs: set | None = None) -> None:
    """Fills weight_net/weight_gross from weight_map by normalized REF (exact or digit-suffix alias).
    Sets weight_source='pl' if filled, 'brak' if not found or weight_map is None.

    filled_refs: normalized REFs already consumed by an earlier line in this map_items() call.
    The PL map stores a per-REF TOTAL, so only the FIRST line for a given REF may draw from it —
    otherwise a multi-LOT invoice (several lines, same REF) would copy the total into every line
    and downstream SAD aggregation would sum it N times."""
    if not weight_map:
        item["weight_source"] = "brak"
        return

    ref = mm.normalize_ref(item.get("raw_ref"))
    if filled_refs is not None and ref and ref in filled_refs:
        item["weight_source"] = item.get("weight_source") or "brak"
        return

    entry = weight_map.get(ref)

    if entry is None and ref:
        # Alias sufiks-cyfrowy: klucz mapy zaczyna się od ref + cyfry
        cands = [k for k in weight_map
                 if k.startswith(ref) and (k[len(ref):] == "" or k[len(ref):].isdigit())]
        if len(cands) == 1:
            entry = weight_map[cands[0]]

    # Pierwszeństwo: waga z tabeli faktury (jeśli była) wygrywa; PL uzupełnia TYLKO puste.
    if entry and (not item.get("weight_net")) and (not item.get("weight_gross")):
        item["weight_net"] = entry.get("weight_net", "") or item.get("weight_net", "")
        item["weight_gross"] = entry.get("weight_gross", "") or item.get("weight_gross", "")
        item["weight_source"] = "pl"
    else:
        item["weight_source"] = item.get("weight_source") or "brak"
    # Kartony analogicznie, ale niezależnie od wag (faktury zwykle ich nie mają).
    if entry and not item.get("cartons"):
        item["cartons"] = entry.get("cartons", "") or ""
    if ref and filled_refs is not None:
        filled_refs.add(ref)


def resolve_chosen_refs(db, job_id) -> None:
    """Po review_save: dla pozycji ambiguous, gdzie operator wpisał master_ref,
    sprawdza czy taki REF istnieje w master i jeśli tak — dogrywa dane i flippuje
    status na 'matched'. Nie istnieje w master → zostaje ambiguous (confirm nadal blokuje)."""
    import invoice_jobs as ij
    conversions = uom.load_conversions(db)
    items = ij.get_items(db, job_id)
    changed = False
    for item in items:
        if item.get("match_status") != MatchStatus.AMBIGUOUS:
            continue
        ref = (item.get("master_ref") or "").strip()
        if not ref:
            continue
        mat = mm.get_material(db, ref)
        if not mat:
            continue
        _enrich(item, mat, conversions)
        item["match_status"] = MatchStatus.MATCHED
        changed = True
    if changed:
        ij.save_items(db, job_id, items)


def map_items(db, raw_items: list, weight_map=None) -> list:
    conversions = uom.load_conversions(db)
    filled_refs: set = set()   # REFs already given PL weights/cartons — dedup multi-LOT lines
    out = []
    for item in raw_items:
        item = dict(item)
        raw_ref = (item.get("raw_ref") or "").strip()
        mat = mm.get_material(db, raw_ref) if raw_ref else None
        if mat:
            _enrich(item, mat, conversions)
            item["match_status"] = MatchStatus.MATCHED
        else:
            cands = mm.find_candidates(db, raw_ref) if raw_ref else []
            if len(cands) == 1:
                _enrich(item, cands[0], conversions)
                item["match_status"] = MatchStatus.MATCHED
            elif len(cands) > 1:
                _blank(item, MatchStatus.AMBIGUOUS)
            else:
                _blank(item, MatchStatus.UNMATCHED)
        _fill_weight(item, weight_map, filled_refs)
        out.append(item)
    return out
