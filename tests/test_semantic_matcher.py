"""Testy semantic_matcher — rdzeń dopasowania pozycji towarowych.

Asercje odzwierciedlają RZECZYWISTE zachowanie modułu (zweryfikowane empirycznie),
m.in. zwijanie jednostek, tłumaczenie PL→EN oparte o odwrócony słownik medyczny
oraz dopasowanie REF po normalizacji (NL753-S-40 ≈ NL753S40 z CLAUDE.md).
"""
import semantic_matcher as sm


# ── normalize_product_description ─────────────────────────────────────────────

class TestNormalizeProductDescription:
    def test_empty(self):
        assert sm.normalize_product_description("") == ""

    def test_lowercases_and_collapses_spaces(self):
        assert sm.normalize_product_description("  Foley   Catheter ") == "foley catheter"

    def test_hyphen_becomes_space(self):
        # FIX 10 — "non-woven" musi zostać dopasowywalne jako "non woven"
        assert sm.normalize_product_description("Non-Woven Swab 10 cm") == "non woven swab 10cm"

    def test_unit_cm_collapsed(self):
        assert "10cm" in sm.normalize_product_description("Compress 10 cm")

    def test_gauge_normalized_to_g(self):
        assert sm.normalize_product_description("Needle 20 gauge") == "needle 20g"

    def test_ml_collapsed(self):
        assert sm.normalize_product_description("Syringe 5 ml") == "syringe 5ml"

    def test_punctuation_removed(self):
        out = sm.normalize_product_description("Catheter, (size: 14)")
        assert "," not in out and "(" not in out and ":" not in out


# ── translate_to_common ───────────────────────────────────────────────────────

class TestTranslateToCommon:
    def test_polish_to_english_base(self):
        # 'kaniula' mapuje na ang. termin (cannula/cannulae — odwrócony słownik)
        assert "cannula" in sm.translate_to_common("kaniula")

    def test_partial_translation_keeps_unknown_words(self):
        out = sm.translate_to_common("strzykawka dożylna")
        assert "syringe" in out          # przetłumaczone
        assert "dożylna" in out          # spoza słownika — zachowane

    def test_gauze_term(self):
        assert "gauze" in sm.translate_to_common("gaza")

    def test_english_input_passes_through_normalized(self):
        # opis już po angielsku — translate nie psuje, tylko normalizuje
        assert sm.translate_to_common("Syringe 5 ml") == "syringe 5ml"


# ── match_items_semantic ──────────────────────────────────────────────────────

def _types(results):
    return sorted(r["match_type"] for r in results)


class TestMatchItemsSemantic:
    def test_empty_inputs(self):
        assert sm.match_items_semantic([], []) == []

    def test_exact_ref_after_normalization(self):
        # NL753-S-40 ≈ NL753S40 — separatory pomijane przy normalizacji REF
        a = [{"ref": "NL753-S-40", "description": "cannula"}]
        b = [{"ref": "NL753S40", "description": "kaniula"}]
        r = sm.match_items_semantic(a, b)
        assert len(r) == 1
        assert r[0]["match_type"] == "ref_exact"
        assert r[0]["confidence"] == 1.0
        assert r[0]["idx_a"] == 0 and r[0]["idx_b"] == 0

    def test_completely_different_unmatched(self):
        a = [{"ref": "AAA111", "description": ""}]
        b = [{"ref": "ZZZ999", "description": ""}]
        assert _types(sm.match_items_semantic(a, b)) == ["only_in_a", "only_in_b"]

    def test_only_in_a_when_b_empty(self):
        a = [{"ref": "X1", "description": "foo"}, {"ref": "X2", "description": "bar"}]
        r = sm.match_items_semantic(a, [])
        assert _types(r) == ["only_in_a", "only_in_a"]
        assert all(x["item_b"] is None and x["idx_b"] is None for x in r)

    def test_every_index_covered_exactly_once(self):
        # Niezmiennik strukturalny: każda pozycja A i B występuje dokładnie raz
        a = [{"ref": "R1", "description": "cannula"},
             {"ref": "R2", "description": "syringe"},
             {"ref": "QQ", "description": "totally different thing"}]
        b = [{"ref": "R1", "description": "kaniula"},
             {"ref": "R2", "description": "strzykawka"},
             {"ref": "WW", "description": "unrelated other item"}]
        r = sm.match_items_semantic(a, b)

        seen_a, seen_b = [], []
        for x in r:
            if x["idx_a"] is not None:
                seen_a.append(x["idx_a"])
            if x["idx_b"] is not None:
                seen_b.append(x["idx_b"])
        assert sorted(seen_a) == [0, 1, 2]
        assert sorted(seen_b) == [0, 1, 2]

    def test_exact_refs_match_in_mixed_set(self):
        a = [{"ref": "R1", "description": "x"}, {"ref": "R2", "description": "y"}]
        b = [{"ref": "R2", "description": "y"}, {"ref": "R1", "description": "x"}]
        r = sm.match_items_semantic(a, b)
        pairs = {(x["idx_a"], x["idx_b"]) for x in r if x["match_type"] == "ref_exact"}
        assert (0, 1) in pairs   # R1 (a0) ↔ R1 (b1)
        assert (1, 0) in pairs   # R2 (a1) ↔ R2 (b0)
