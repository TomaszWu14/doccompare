"""Testy _build_column_map — rdzeniowa detekcja ról kolumn tabel handlowych.

Asercje odzwierciedlają RZECZYWISTY kontrakt (zweryfikowany empirycznie), w tym
celowe ograniczenia: rola 'ref' łapie 'product code'/'code'/'item'/'nr ref', ale
NIE samo 'ref' — polskie/nietypowe nagłówki ratuje profil dostawcy (supplier_map
w formacie {nazwa_nagłówka: rola}).
"""
import enhanced_comparator as ec


class TestStandardHeaders:
    def test_full_english_header(self):
        h = ["Product Code", "Description", "Quantity", "Unit Price", "Net Value"]
        assert ec._build_column_map(h) == {"ref": 0, "desc": 1, "qty": 2, "price": 3, "net": 4}

    def test_code_and_name(self):
        assert ec._build_column_map(["Code", "Name", "Qty"]) == {"ref": 0, "desc": 1, "qty": 2}

    def test_item_no_and_pcs(self):
        assert ec._build_column_map(["Item No", "Description", "Pcs"]) == {"ref": 0, "desc": 1, "qty": 2}

    def test_empty_header(self):
        assert ec._build_column_map([]) == {}

    def test_unrecognized_header_yields_empty(self):
        assert ec._build_column_map(["A", "B", "C", "D"]) == {}


class TestKnownLimitations:
    """Dokumentuje świadome granice detekcji — strażnik przed regresją."""

    def test_bare_ref_keyword_not_detected(self):
        # 'ref' samodzielnie NIE jest keywordem (lista: product code/code/item/nr ref).
        # Plik PI z samym 'REF' wymaga profilu dostawcy — to celowe.
        m = ec._build_column_map(["REF", "Description", "Qty"])
        assert "ref" not in m
        assert m.get("desc") == 1 and m.get("qty") == 2

    def test_polish_headers_barely_detected_without_profile(self):
        # Bez profilu polskie nagłówki łapie tylko 'netto' (zawiera 'net').
        m = ec._build_column_map(["Nr kat.", "Nazwa", "Ilość", "Cena", "Wartość netto"])
        assert m == {"net": 4}


class TestSupplierProfileOverride:
    def test_profile_maps_polish_headers(self):
        h = ["Nr katalogowy", "Nazwa", "Ilość"]
        sm = {"Nr katalogowy": "ref", "Nazwa": "desc", "Ilość": "qty"}
        assert ec._build_column_map(h, sm) == {"ref": 0, "desc": 1, "qty": 2}

    def test_profile_contains_match(self):
        # Dopasowanie po fragmencie: 'Nr kat.' jest podłańcuchem 'Kolumna: Nr kat.'
        m = ec._build_column_map(["Kolumna: Nr kat.", "X"], {"Nr kat.": "ref"})
        assert m == {"ref": 0}


class TestConflictResolution:
    def test_roles_take_distinct_best_columns(self):
        # 'Price/Amount' vs 'Net Value': net woli wyraźniejsze 'Net Value',
        # price zostaje przy 'Price/Amount' — bez kradzieży kolumny.
        m = ec._build_column_map(["Price/Amount", "Net Value", "Qty"])
        assert m == {"price": 0, "net": 1, "qty": 2}
