"""Testy walidacji arytmetycznej qty×price=net w compare_items (FIX #2).

Reguła celna: dla dokumentu B będącego fakturą (PI/CI/CI_SET/FV) sprawdzamy, czy
ilość×cena=wartość netto z tolerancją 2%. PO jest pomijane (zaokrągla ceny →
fałszywe alarmy). Asercje zweryfikowane empirycznie, wykonują się pod lekkim
pytest (enhanced_comparator nie wymaga pdfplumber/rapidfuzz do importu).
"""
import enhanced_comparator as ec


def _run(doc_type_b, qty, price, net, ref="R1"):
    """compare_items na parze identycznych pozycji — izoluje walidację B."""
    item = {"ref": ref, "qty": qty, "price": price, "net": net, "description": "x"}
    compared, findings = ec.compare_items([dict(item)], [dict(item)],
                                          doc_type_a="PO", doc_type_b=doc_type_b)
    return compared[0], findings


def _arith(findings):
    return [f for f in findings if f.category == "arytmetyka"]


class TestArithmeticInvoice:
    def test_inconsistent_pi_flagged(self):
        item, findings = _run("PI", "10", "2.00", "25.00")   # 10×2=20 ≠ 25
        assert item.get("arithmetic_issues")
        a = _arith(findings)
        assert len(a) == 1
        assert a[0].severity == "warning"
        assert "R1" in a[0].field

    def test_consistent_pi_not_flagged(self):
        item, findings = _run("PI", "10", "2.00", "20.00")   # 10×2=20 = 20
        assert item.get("arithmetic_issues") is None
        assert _arith(findings) == []

    def test_ci_also_validated(self):
        item, _ = _run("CI", "10", "2.00", "25.00")
        assert item.get("arithmetic_issues")

    def test_european_number_format_normalized(self):
        # price '2,00' i net '25,00' — przecinek dziesiętny musi być rozpoznany
        item, _ = _run("PI", "10", "2,00", "25,00")
        assert item.get("arithmetic_issues")


class TestTolerance:
    def test_within_2pct_not_flagged(self):
        # 10×2=20 vs 20.30 → Δ1.5% < 2% → bez alarmu
        item, findings = _run("PI", "10", "2.00", "20.30")
        assert item.get("arithmetic_issues") is None
        assert _arith(findings) == []

    def test_above_2pct_flagged(self):
        # 10×2=20 vs 20.50 → Δ2.4% > 2% → alarm
        item, findings = _run("PI", "10", "2.00", "20.50")
        assert item.get("arithmetic_issues")
        assert len(_arith(findings)) == 1


class TestSkippedCases:
    def test_purchase_order_b_not_validated(self):
        # B niebędące fakturą (PO) — walidacja pominięta mimo niespójności
        item, findings = _run("PO", "10", "2.00", "25.00")
        assert item.get("arithmetic_issues") is None
        assert _arith(findings) == []

    def test_missing_price_skipped(self):
        item, findings = _run("PI", "10", "", "20.00")
        assert item.get("arithmetic_issues") is None
        assert _arith(findings) == []

    def test_missing_qty_skipped(self):
        item, findings = _run("PI", "", "2.00", "20.00")
        assert item.get("arithmetic_issues") is None
        assert _arith(findings) == []
