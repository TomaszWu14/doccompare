import sqlite3
import material_master as mm
import invoice_mapper as im

def _db_with(materials, conversions=()):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    mm.ensure_table(db)
    import uom
    uom.ensure_table(db)
    for m in materials:
        db.execute(
            "INSERT INTO material_master (ref_code, ref_norm, opis_pl, tariff_cn, sent, base_uom) "
            "VALUES (?,?,?,?,?,?)",
            (m["ref_code"], mm.normalize_ref(m["ref_code"]), m.get("opis_pl", ""),
             m.get("tariff_cn", ""), m.get("sent", 0), m.get("base_uom", "")))
    for c in conversions:
        db.execute("INSERT INTO uom_conversion (ref_norm, unit_from, unit_to, factor) "
                   "VALUES (?,?,?,?)", c)
    db.commit()
    return db

def test_exact_match_enriches():
    db = _db_with([{"ref_code": "X", "opis_pl": "Rękawice", "tariff_cn": "4015", "sent": 1}])
    out = im.map_items(db, [{"line_no": 1, "raw_ref": "X", "uom_src": "PCS"}])
    r = out[0]
    assert r["match_status"] == "matched"
    assert r["master_ref"] == "X"
    assert r["name_pl"] == "Rękawice"
    assert r["tariff_cn"] == "4015"
    assert r["sent"] == 1

def test_single_alias_is_matched():
    # faktura ma "X", master trzyma tylko "X1" → jednoznaczny alias
    db = _db_with([{"ref_code": "X1", "opis_pl": "Maska"}])
    out = im.map_items(db, [{"line_no": 1, "raw_ref": "X", "uom_src": ""}])
    assert out[0]["match_status"] == "matched"
    assert out[0]["master_ref"] == "X1"

def test_multiple_candidates_ambiguous():
    db = _db_with([{"ref_code": "X1"}, {"ref_code": "X2"}])
    out = im.map_items(db, [{"line_no": 1, "raw_ref": "X", "uom_src": ""}])
    assert out[0]["match_status"] == "ambiguous"
    assert out[0]["master_ref"] == ""      # nie wybieramy automatycznie

def test_no_candidate_unmatched():
    db = _db_with([{"ref_code": "Z"}])
    out = im.map_items(db, [{"line_no": 1, "raw_ref": "X", "uom_src": ""}])
    assert out[0]["match_status"] == "unmatched"
    assert out[0]["name_pl"] == ""

def test_uom_factor_filled():
    db = _db_with(
        [{"ref_code": "X", "base_uom": "PCS"}],
        conversions=[("X", "CTN", "PCS", 24.0)])
    out = im.map_items(db, [{"line_no": 1, "raw_ref": "X", "uom_src": "CTN"}])
    assert out[0]["uom_factor"] == "24.0"

def test_prefix_not_overmatched():
    # invoice "X" should NOT match master "XYLO" — only prefix+digits allowed
    db = _db_with([{"ref_code": "XYLO", "opis_pl": "Xylophone"}])
    out = im.map_items(db, [{"line_no": 1, "raw_ref": "X", "uom_src": ""}])
    assert out[0]["match_status"] == "unmatched"
    assert out[0]["master_ref"] == ""

def test_digit_suffix_still_matches():
    # invoice "X" SHOULD match master "X10" — suffix is all digits
    db = _db_with([{"ref_code": "X10", "opis_pl": "Ten pieces"}])
    out = im.map_items(db, [{"line_no": 1, "raw_ref": "X", "uom_src": ""}])
    assert out[0]["match_status"] == "matched"
    assert out[0]["master_ref"] == "X10"

def test_resolve_chosen_refs_valid_ref_becomes_matched():
    import invoice_jobs as ij
    db = _db_with([{"ref_code": "X1"}, {"ref_code": "X2"}])
    ij.ensure_invoice_tables(db)
    jid = ij.create_job(db, "b", "a.pdf", "uploads/a.pdf", "S1")
    ij.save_items(db, jid, [{"line_no": 1, "raw_ref": "X", "uom_src": "",
                              "match_status": "ambiguous", "master_ref": "X1"}])
    im.resolve_chosen_refs(db, jid)
    item = ij.get_items(db, jid)[0]
    assert item["match_status"] == "matched"
    assert item["master_ref"] == "X1"

def test_resolve_chosen_refs_invalid_ref_stays_ambiguous():
    import invoice_jobs as ij
    db = _db_with([{"ref_code": "X1"}, {"ref_code": "X2"}])
    ij.ensure_invoice_tables(db)
    jid = ij.create_job(db, "b", "a.pdf", "uploads/a.pdf", "S1")
    ij.save_items(db, jid, [{"line_no": 1, "raw_ref": "X", "uom_src": "",
                              "match_status": "ambiguous", "master_ref": "NOPE"}])
    im.resolve_chosen_refs(db, jid)
    item = ij.get_items(db, jid)[0]
    assert item["match_status"] == "ambiguous"
