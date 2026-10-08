"""Regresja: rozdzielone parsery markdown (row-lists vs dicts) + _dict_rows_to_items + guard.
Bug: _parse_markdown_table było zdefiniowane 2× — dict-wersja przesłaniała row-list, przez co
warstwy Mistral/GLM cicho zwracały 0 pozycji (wynik szedł do _rows_to_items o złym shape)."""
import pytest
pytest.importorskip("pdfplumber")
pytest.importorskip("rapidfuzz")
from table_extractor import (_parse_markdown_table, _parse_markdown_table_dicts,
                             _dict_rows_to_items, _rows_to_items)

# "Code" (nie "REF") — bare "REF" nie jest słowem kluczowym ref w identify_columns
_MD = "| Code | Qty | Amount |\n|---|---|---|\n| NL753 | 100 | 50.00 |\n| ML20 | 5 | 9.00 |"


def test_row_list_parser_returns_lists():
    # Wersja dla Mistral/OCR-cascade → surowe wiersze (list[list[str]]), z nagłówkiem
    rows = _parse_markdown_table(_MD)
    assert isinstance(rows, list) and rows and isinstance(rows[0], list)
    assert rows[0] == ["Code", "Qty", "Amount"]
    assert ["NL753", "100", "50.00"] in rows


def test_mistral_path_row_list_feeds_rows_to_items():
    # Dowód end-to-end: wynik row-list parsera daje pozycje w _rows_to_items (nie 0)
    items, _, _ = _rows_to_items(_parse_markdown_table(_MD))
    refs = [it["ref"] for it in items]
    assert "NL753" in refs and "ML20" in refs


def test_dict_parser_returns_dicts():
    out = _parse_markdown_table_dicts(_MD)
    assert isinstance(out, list) and out and isinstance(out[0], dict)
    assert out[0]["ref"] == "NL753" and out[0]["qty"] == "100"


def test_dict_rows_to_items_and_total():
    rows = [{"ref": "NL753", "qty": "100", "net": "50"},
            {"ref": "TOTAL", "qty": "105", "net": "59"}]
    items, tnet, tqty = _dict_rows_to_items(rows)
    assert len(items) == 1 and items[0]["ref"] == "NL753"
    assert tnet == "59" and tqty == "105"


def test_rows_to_items_guards_against_dict_shape():
    # Guard: podanie list[dict] do row-list parsera nie sypie garbage, zwraca puste
    assert _rows_to_items([{"ref": "X"}, {"ref": "Y"}]) == ([], None, None)
