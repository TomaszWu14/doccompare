"""
tests/test_typo_learned.py — rozszerzalne („nauczone") pary znaków w typo_detector.
Pomijany, gdy rapidfuzz nie jest zainstalowany (lekkie CI).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest
pytest.importorskip("rapidfuzz")
import typo_detector as td


def setup_function(_):
    td.set_learned_confusable_pairs([])   # reset między testami


def test_default_unchanged():
    # Bez nauczonych par: Q↔9 nie jest rozpoznawane jako zamiana
    assert td.detect_character_swaps("Q123", "9123") == []


def test_learned_pair_detected():
    assert td.add_learned_confusable_pair("Q", "9") is True
    res = td.detect_character_swaps("Q123", "9123")
    assert res and "zamiana" in res[0].lower()


def test_base_pairs_still_work():
    # I↔1 to para bazowa
    assert td.detect_character_swaps("REF1", "REFI")


def test_invalid_pairs_ignored():
    td.set_learned_confusable_pairs([("AB", "9"), ("x",), 5, ("q", "q")])
    assert td._LEARNED_CONFUSABLE_PAIRS == []


def test_add_duplicate_returns_false():
    td.set_learned_confusable_pairs([])
    assert td.add_learned_confusable_pair("Q", "9") is True
    assert td.add_learned_confusable_pair("9", "Q") is False   # reverse duplicate
