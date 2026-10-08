"""
tests/test_sap_extract.py — ekstrakcja numeru dostawy SAP ("45"+10 cyfr)
z nazwy pliku / treści PDF.

Uruchom: pytest tests/
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sap_extract import extract_delivery_number


class TestDeliveryNumber:
    def test_filename_with_spaces(self):
        # przypadek z zgłoszenia
        assert extract_delivery_number("4500000204 Inspection Report.pdf") == "4500000204"

    def test_filename_without_spaces(self):
        assert extract_delivery_number("4500000204Raport.pdf") == "4500000204"

    def test_longer_than_ten_digits(self):
        assert extract_delivery_number("450000020499 packing.pdf") == "450000020499"

    def test_priority_filename_over_content(self):
        # pierwszy argument (nazwa pliku) ma pierwszeństwo nad treścią
        assert extract_delivery_number("4500000204 PL.pdf", "Delivery 4500009999") == "4500000204"

    def test_falls_back_to_content(self):
        assert extract_delivery_number("Inspection Report.pdf", "Dostawa nr 4500009999 zatwierdzona") == "4500009999"

    def test_not_embedded_in_longer_digit_run(self):
        # 45 w środku dłuższego ciągu cyfr nie jest numerem dostawy
        assert extract_delivery_number("12345000002049999000.pdf") == ""

    def test_too_short(self):
        # 45 + 7 cyfr = 9 znaków < 10 → brak dopasowania
        assert extract_delivery_number("450000220.pdf") == ""

    def test_wrong_prefix(self):
        assert extract_delivery_number("1800002204 delivery.pdf") == ""

    def test_no_match(self):
        assert extract_delivery_number("Inspection Report.pdf") == ""

    def test_empty_inputs(self):
        assert extract_delivery_number("", None) == ""
