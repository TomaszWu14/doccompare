"""Testy typo_detector — wykrywanie mylenia znaków (I/1, O/0), refów i liczb.

Asercje odzwierciedlają rzeczywiste zachowanie (zweryfikowane empirycznie):
check_i_vs_1/detect_character_swaps łapią WYŁĄCZNIE pary I↔1 i O↔0 (nie l↔1),
a extract_doc_refs rozpoznaje wzorce N935-, 'Invoice No:' i 'PO No:' z progami
długości. test_typo_learned.py pokrywa pary uczone — tu reszta kontraktu.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest
pytest.importorskip("rapidfuzz")   # lekkie CI bez rapidfuzz → pomiń (jak test_typo_learned)
import typo_detector as td


class TestCheckIvs1:
    def test_identical_returns_none(self):
        assert td.check_i_vs_1("ABC123", "ABC123") is None

    def test_empty_returns_none(self):
        assert td.check_i_vs_1("", "") is None

    def test_one_vs_I_detected(self):
        msg = td.check_i_vs_1("INV1", "INVI")
        assert msg and "poz.4" in msg

    def test_O_vs_zero_detected(self):
        msg = td.check_i_vs_1("O0", "0O")
        assert msg is not None

    def test_unrelated_swap_not_flagged(self):
        # 'Z' vs '3' to nie para I/1 ani O/0
        assert td.check_i_vs_1("PO123", "PO1Z3") is None


class TestDetectCharacterSwaps:
    def test_O_zero_swap(self):
        out = td.detect_character_swaps("PO12O", "PO120")
        assert out and "O" in out[0]

    def test_identical_no_swap(self):
        assert td.detect_character_swaps("SAME", "SAME") == []

    def test_l_vs_1_not_covered(self):
        # Tylko I↔1 i O↔0; 'l'↔'1' celowo poza zakresem
        assert td.detect_character_swaps("1000", "l000") == []


class TestExtractDocRefs:
    def test_invoice_no(self):
        assert td.extract_doc_refs("Invoice No: PI-2024/123") == ["PI-2024/123"]

    def test_po_no(self):
        assert td.extract_doc_refs("PO No: 456-AB") == ["456-AB"]

    def test_n935_sad_ref(self):
        assert td.extract_doc_refs("N935-NL753S40 poz. 1") == ["NL753S40"]

    def test_too_short_po_skipped(self):
        # PO wymaga >=4 znaków
        assert td.extract_doc_refs("PO No: 12") == []

    def test_no_refs_in_plain_text(self):
        assert td.extract_doc_refs("just some prose without document numbers") == []


class TestCheckMissingRefs:
    def test_all_present_no_findings(self):
        assert td.check_missing_refs(["AAA111"], ["AAA111"]) == []

    def test_missing_in_b_flagged(self):
        f = td.check_missing_refs(["ONLYA999"], [])
        assert len(f) == 1
        assert f[0].severity == "error" and f[0].category == "brak_dokumentu"

    def test_i_vs_1_confusion_in_ref(self):
        f = td.check_missing_refs(["INV12345"], ["INVI2345"])
        assert len(f) == 1
        assert f[0].category == "I_vs_1" and f[0].severity == "error"


class TestParseNumberStrict:
    def test_space_thousands_comma_decimal(self):
        assert td.parse_number_strict("1 234,56") == 1234.56

    def test_comma_thousands_dot_decimal(self):
        assert td.parse_number_strict("1,234.56") == 1234.56

    def test_dot_thousands(self):
        assert td.parse_number_strict("12.000") == 12000.0

    def test_plain_integer(self):
        assert td.parse_number_strict("99") == 99.0

    def test_zero(self):
        assert td.parse_number_strict("0") == 0.0

    def test_non_numeric_none(self):
        assert td.parse_number_strict("abc") is None

    def test_empty_none(self):
        assert td.parse_number_strict("") is None


# ── Regresja: drobne, RÓŻNE liczby nie są „tą samą wartością" (BUGFIX) ────────
class TestSmallNumberDifference:
    def test_tiny_but_different_values_flagged_as_difference(self):
        f = td.compare_numbers("0.0001", "0.0005", "qty")
        assert f is not None
        assert f.category == "ilosc"          # różnica wartości, nie 'format'

    def test_format_equal_small_values_still_same(self):
        assert td.compare_numbers("0,0002", "0.0002", "qty") is None

    def test_normal_rounding_still_format_or_none(self):
        # ten sam, różny separator → brak różnicy wartości
        assert td.compare_numbers("1 234,56", "1234.56", "qty") is None
        assert td.compare_numbers("12,5", "12.5", "qty") is None
