import doc_splitter as ds
from constants import DocKind

# Realne nagłówki z próbki QA: podwójna spacja (Kestrel), literówki
# PROFOMA (Kestrel) i PORFORMA (Tidewell); strona PL zawiera słowo "invoice".
CI = "ACME CO COMMERCIAL  INVOICE Shipper No. & date of Invoice A1 2026"
PL = "ACME CO PACKING LIST Shipper No. & date of invoice A1 2026 CTNS"
PL_SPEC = "ACME CO SPECYFIKACJA PAKOWANIA Nr faktury A1 2026 CTNS"
PROF = "ACME CO PROFORMA INVOICE No. & date of Invoice A1-S 2026"
PROF_TYPO1 = "ACME CO PROFOMA  INVOICE No. & date of Invoice A1-S 2026"
PROF_TYPO2 = "ACME CO PORFORMA INVOICE No. & date of Invoice A1-S 2026"
BL = "CORVAN MEDICAL BILL OF LADING BLNO0000001 SHANGHAI GDANSK POLAND"
CONT = "RNBL10001 Disposable gloves 987654321N 4,400 19.000 83,600.00 page 2"
SCAN = "DRAFT"


def test_classify_variants():
    assert ds.classify_page(CI) == DocKind.INVOICE
    assert ds.classify_page(PL) == DocKind.PACKING_LIST
    assert ds.classify_page(PL_SPEC) == DocKind.PACKING_LIST
    assert ds.classify_page(PROF) == DocKind.PROFORMA
    assert ds.classify_page(PROF_TYPO1) == DocKind.PROFORMA
    assert ds.classify_page(PROF_TYPO2) == DocKind.PROFORMA
    assert ds.classify_page(BL) == DocKind.OTHER
    assert ds.classify_page(SCAN) == DocKind.OTHER      # skan/pusta → other
    assert ds.classify_page("") == DocKind.OTHER
    assert ds.classify_page(CONT) is None               # kontynuacja
    assert ds.classify_page("PACKING LIST") == DocKind.PACKING_LIST   # marker < 30 znaków
    assert ds.classify_page("BILL OF LADING") == DocKind.OTHER        # B/L marker < 30 znaków


def test_split_groups_and_cuts(monkeypatch):
    written = []
    monkeypatch.setattr(ds, "_page_texts", lambda p: [CI, CONT, PL, PROF_TYPO1])
    monkeypatch.setattr(ds, "_write_range",
                        lambda p, a, b, out: written.append((a, b, out)))
    parts = ds.split_pdf("uploads/zestaw.pdf")
    assert [(p["kind"], p["page_from"], p["page_to"]) for p in parts] == [
        (DocKind.INVOICE, 1, 2), (DocKind.PACKING_LIST, 3, 3),
        (DocKind.PROFORMA, 4, 4)]
    assert len(written) == 3
    assert parts[0]["out_path"].endswith("_doc1_invoice.pdf")


def test_single_doc_returns_original_without_copy(monkeypatch):
    monkeypatch.setattr(ds, "_page_texts", lambda p: [PROF])
    def _boom(*a):
        raise AssertionError("nie powinno ciąć jednodokumentowego PDF")
    monkeypatch.setattr(ds, "_write_range", _boom)
    parts = ds.split_pdf("uploads/proforma.pdf")
    assert parts == [{"kind": DocKind.PROFORMA, "page_from": 1, "page_to": 1,
                      "out_path": "uploads/proforma.pdf"}]


def test_leading_continuation_becomes_other(monkeypatch):
    monkeypatch.setattr(ds, "_page_texts", lambda p: [CONT, CI])
    monkeypatch.setattr(ds, "_write_range", lambda p, a, b, out: None)
    parts = ds.split_pdf("uploads/x.pdf")
    assert parts[0]["kind"] == DocKind.OTHER
    assert parts[1]["kind"] == DocKind.INVOICE
