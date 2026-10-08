"""tests/test_comparator_heuristics.py — parse_number i detekcja kursu walut."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from comparator import parse_number, detect_currency_rate_diff


# ── parse_number: usuwanie walut/jednostek bez psucia liczby ─────────────────
def test_parse_number_strips_currency_tokens():
    assert parse_number("10 500,00 PLN") == 10500.0
    assert parse_number("10.500,00") == 10500.0
    assert parse_number("10,500.00") == 10500.0
    assert parse_number("1234 EUR") == 1234.0
    assert parse_number("USD 99.50") == 99.5


def test_parse_number_letters_stripped_uniformly():
    # litery (kody walut/jednostki) usuwane jednym przejściem, nie podciągami
    assert parse_number("4321 CHF") == 4321.0
    assert parse_number("500SZT") == 500.0


def test_parse_number_empty_and_none():
    assert parse_number("") is None
    assert parse_number(None) is None
    assert parse_number("brak") is None


# ── detect_currency_rate_diff: bez masowych false-positive wokół 1.0 ──────────
def test_no_false_positive_near_ratio_one():
    # 100 vs 120 (ratio 1.2) NIE może być raportowane jako różnica kursu
    assert detect_currency_rate_diff("100", "120") is None
    assert detect_currency_rate_diff("1000", "1100") is None
    assert detect_currency_rate_diff("100", "100") is None


def test_detects_plausible_fx_rate():
    # 1000 EUR vs 4300 PLN → kurs ≈ 4.30
    r = detect_currency_rate_diff("1000", "4300")
    assert r is not None and "kurs" in r.lower()


def test_detects_fx_rate_reverse_direction():
    # PLN vs EUR (kierunek odwrotny) — 4300 vs 1000 → 1/0.2326 ≈ 4.30
    assert detect_currency_rate_diff("4300", "1000") is not None


def test_unrelated_large_difference_not_flagged():
    # 100 vs 700 (ratio 7) — poza zakresem typowych kursów do PLN
    assert detect_currency_rate_diff("100", "700") is None
