"""Testy wiązania taryfy referencyjnej w enhanced_comparator.

_make_cn_reference_lookup wczytuje tabelę cn_reference RAZ do pamięci i zwraca
callable(code)->dict|None. Kluczowy kontrakt: brak/pusta tabela → None (walidacja
pomijana, NIE raportuje błędów), populacja → szybki lookup bez zapytań per-pozycja.
"""
import sqlite3
import pytest
import enhanced_comparator as ec


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _make_table(c, rows):
    c.execute("CREATE TABLE cn_reference(code TEXT, description TEXT, "
              "duty_rate TEXT, restrictions TEXT, active INTEGER)")
    c.executemany("INSERT INTO cn_reference VALUES(?,?,?,?,?)", rows)
    c.commit()


class TestMakeCnReferenceLookup:
    def test_db_none_returns_none(self):
        assert ec._make_cn_reference_lookup(None) is None

    def test_missing_table_returns_none(self, conn):
        # Tabeli brak → wyjątek przechwycony → None (nie wywala porównania)
        assert ec._make_cn_reference_lookup(conn) is None

    def test_empty_table_returns_none(self, conn):
        _make_table(conn, [])
        # Pusta taryfa = brak danych referencyjnych → pomijamy walidację
        assert ec._make_cn_reference_lookup(conn) is None

    def test_populated_returns_callable(self, conn):
        _make_table(conn, [("6505009090", "Czepki", "6.3%", "", 1)])
        lk = ec._make_cn_reference_lookup(conn)
        assert callable(lk)

    def test_lookup_hit_returns_full_dict(self, conn):
        _make_table(conn, [("6505009090", "Czepki", "6.3%", "", 1)])
        lk = ec._make_cn_reference_lookup(conn)
        entry = lk("6505009090")
        assert entry["description"] == "Czepki"
        assert entry["duty_rate"] == "6.3%"
        assert entry["active"] == 1

    def test_lookup_miss_returns_none(self, conn):
        _make_table(conn, [("6505009090", "Czepki", "6.3%", "", 1)])
        lk = ec._make_cn_reference_lookup(conn)
        assert lk("99999999") is None

    def test_restrictions_preserved(self, conn):
        _make_table(conn, [("33049900", "Kosmetyki", "0%", "anty-dumping", 1)])
        lk = ec._make_cn_reference_lookup(conn)
        assert lk("33049900")["restrictions"] == "anty-dumping"


class TestReferenceLookupIntegratesWithAudit:
    """Lookup z enhanced_comparator współgra z czystą walidacją w cn_audit."""

    def test_unknown_code_flagged_via_cn_audit(self, conn):
        import cn_audit
        _make_table(conn, [("6505009090", "Czepki", "6.3%", "", 1)])
        lk = ec._make_cn_reference_lookup(conn)
        # kod spoza taryfy → uwaga
        st, note = cn_audit.validate_cn_reference("11112222", lk)
        assert st == "uwaga" and "nieobecny" in note

    def test_known_code_ok_via_cn_audit(self, conn):
        import cn_audit
        _make_table(conn, [("6505009090", "Czepki", "6.3%", "", 1)])
        lk = ec._make_cn_reference_lookup(conn)
        st, _ = cn_audit.validate_cn_reference("6505 00 90 90", lk)
        assert st == "ok"
