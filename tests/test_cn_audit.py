"""
tests/test_cn_audit.py — testy walidacji strukturalnej kodu CN/TARIC oraz
jej integracji z audytem E8-01 (cn_audit.py).

Uruchom: pytest tests/
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from cn_audit import (
    validate_cn_code, validate_cn_reference, audit_items_cn, compare_cn,
)


# ── validate_cn_reference: lokalna tabela TARIC ───────────────────────────────

class TestValidateCnReference:
    REF = {
        "6505009090": {"description": "Czepki", "restrictions": "", "active": 1},
        "33049900":   {"description": "Kosmetyki", "restrictions": "anty-dumping", "active": 1},
        "84713000":   {"description": "Stare", "restrictions": "", "active": 0},
    }

    def _lookup(self, code):
        return self.REF.get(code)

    def test_no_lookup_returns_none(self):
        assert validate_cn_reference("6505009090", None) is None

    def test_empty_code_returns_none(self):
        assert validate_cn_reference("", self._lookup) is None

    def test_known_clean_code_ok(self):
        st, _ = validate_cn_reference("6505009090", self._lookup)
        assert st == "ok"

    def test_unknown_code_uwaga(self):
        st, note = validate_cn_reference("99887766", self._lookup)
        assert st == "uwaga" and "nieobecny" in note

    def test_restricted_code_uwaga(self):
        st, note = validate_cn_reference("33049900", self._lookup)
        assert st == "uwaga" and "anty-dumping" in note

    def test_inactive_code_uwaga(self):
        st, note = validate_cn_reference("84713000", self._lookup)
        assert st == "uwaga" and "NIEAKTYWNY" in note

    def test_normalizes_separators_before_lookup(self):
        st, _ = validate_cn_reference("6505 00 90 90", self._lookup)
        assert st == "ok"


# ── audit_items_cn: integracja ref_lookup (nieinwazyjna) ──────────────────────

class TestAuditReferenceIntegration:
    SAD = (
        "Pozycja 1\nTowar: Opis [18 05]: CZEPEK, kod CN\n"
        "[18 09]: 65050090, kod TARIC: 90\n"
    )

    def test_default_no_ref_lookup_no_reference_issue(self):
        items = [{"ref": "WC1", "desc_b": "CZEPEK"}]
        f = audit_items_cn(items, self.SAD, sad_is_b=True, cn_lookup=lambda r: "6505009090")
        assert f[0]["reference_issue"] is None
        assert items[0]["cn_reference_issue"] is None

    def test_unknown_sad_code_flagged(self):
        items = [{"ref": "WC1", "desc_b": "CZEPEK"}]
        f = audit_items_cn(items, self.SAD, sad_is_b=True,
                           cn_lookup=lambda r: "6505009090",
                           ref_lookup=lambda code: None)  # pusta taryfa -> nieobecny
        assert f[0]["reference_issue"] is not None
        assert f[0]["reference_issue"]["status"] == "uwaga"


# ── validate_cn_code: poprawne kody ───────────────────────────────────────────

class TestValidateCnValid:
    def test_cn8_valid(self):
        st, _ = validate_cn_code("65050090")
        assert st == "ok"

    def test_taric10_valid(self):
        st, _ = validate_cn_code("6505009090")
        assert st == "ok"

    def test_hs6_valid(self):
        st, _ = validate_cn_code("650500")
        assert st == "ok"

    def test_separators_tolerated(self):
        # spacje / kropki / myslniki nie sa bledem zapisu
        assert validate_cn_code("6505 00 90")[0] == "ok"
        assert validate_cn_code("6307.90.98")[0] == "ok"
        assert validate_cn_code("6307-90-98")[0] == "ok"


# ── validate_cn_code: bledy ───────────────────────────────────────────────────

class TestValidateCnErrors:
    def test_letters_rejected(self):
        # litera z OCR (O zamiast 0) -> blad zapisu
        assert validate_cn_code("6505OO90")[0] == "blad"

    def test_bad_length(self):
        assert validate_cn_code("12345")[0] == "blad"      # 5 cyfr
        assert validate_cn_code("123456789")[0] == "blad"  # 9 cyfr
        assert validate_cn_code("1234567")[0] == "blad"    # 7 cyfr

    def test_chapter_zero(self):
        assert validate_cn_code("00123456")[0] == "blad"

    def test_empty_is_uwaga(self):
        assert validate_cn_code("")[0] == "uwaga"
        assert validate_cn_code(None)[0] == "uwaga"


# ── validate_cn_code: przypadki 'uwaga' (dzialy specjalne) ─────────────────────

class TestValidateCnWarn:
    def test_reserved_chapter_77(self):
        assert validate_cn_code("77010000")[0] == "uwaga"

    def test_special_chapters_98_99(self):
        assert validate_cn_code("98000000")[0] == "uwaga"
        assert validate_cn_code("99000000")[0] == "uwaga"


# ── integracja z audit_items_cn ───────────────────────────────────────────────

class TestAuditFormatIssues:
    SAD = (
        "Pozycja 1\nTowar: Opis [18 05]: CZEPEK, kod CN\n"
        "[18 09]: 65050090, kod TARIC: 90\n"
    )

    def test_clean_codes_no_format_issues(self):
        items = [{"ref": "WC1", "desc_b": "CZEPEK"}]
        f = audit_items_cn(items, self.SAD, sad_is_b=True, cn_lookup=lambda r: "6505009090")
        assert f[0]["status"] == "ok"            # zgodne (8 vs 10, ten sam prefiks)
        assert f[0]["format_issues"] == []       # oba kody poprawne strukturalnie
        assert items[0]["cn_format_issues"] == []

    def test_malformed_master_flagged_even_when_status_ok(self):
        # Master ma zly kod (7 cyfr), ale SAD pasuje na wspolnym prefiksie ->
        # zgodnosc moze byc 'ok', a mimo to sygnalizujemy wadliwy zapis mastera.
        items = [{"ref": "WC1", "desc_b": "CZEPEK"}]
        f = audit_items_cn(items, self.SAD, sad_is_b=True, cn_lookup=lambda r: "6505009")
        sides = {fi["side"] for fi in f[0]["format_issues"]}
        assert "master" in sides

    def test_format_issues_do_not_change_status(self):
        # Dodanie walidacji formatu nie zmienia logiki zgodnosci.
        assert compare_cn("65050090", "65050090")[0] == "ok"
