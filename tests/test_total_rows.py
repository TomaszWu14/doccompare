"""tests/test_total_rows.py — rozróżnianie wierszy total vs subtotal."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from enhanced_comparator import _is_total_row, _is_subtotal_row


def test_total_row_detected():
    assert _is_total_row(["", "TOTAL", "12 345.00"])
    assert _is_total_row(["Razem:", "999.00"])
    assert _is_total_row(["Grand Total", "5000"])


def test_subtotal_detected():
    assert _is_subtotal_row(["Subtotal", "100.00"])
    assert _is_subtotal_row(["Sub-total", "100.00"])
    assert _is_subtotal_row(["Suma częściowa", "100.00"])
    assert _is_subtotal_row(["Podsuma", "100.00"])


def test_subtotal_is_not_confused_with_plain_total():
    # zwykły wiersz total nie jest subtotalem
    assert not _is_subtotal_row(["TOTAL", "200.00"])
    assert not _is_subtotal_row(["Razem:", "200.00"])


def test_plain_item_row_is_neither():
    row = ["1", "Cewnik Foleya", "100", "23%", "1.50", "150.00"]
    assert not _is_total_row(row)
    assert not _is_subtotal_row(row)
