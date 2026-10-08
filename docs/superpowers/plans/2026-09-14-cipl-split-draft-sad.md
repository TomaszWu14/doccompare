# Zestawy CIPL + eksport „Draft SAD" — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Moduł Faktury → Excel przetwarza PDF-zestawy (CI+PL+Proforma w jednym pliku, także wielu dostawców) przez splitter stron i generuje per batch nowy eksport „Draft SAD" (Excel: Nagłówek + Pozycje per eksporter × kod CN) dla agencji celnej.

**Architecture:** Nowy `doc_splitter.py` klasyfikuje strony po tekście i tnie zestaw na osobne PDF-y (PyMuPDF); istniejący pipeline (profil obowiązkowy → ekstrakcja → mapowanie → split-view → confirm) działa bez zmian na wyciętych plikach. Nowy `sad_draft_export.py` agreguje zatwierdzone pozycje batcha per (eksporter × CN) i buduje Excel wg pól WinSAD. Spec: `docs/superpowers/specs/2026-09-14-cipl-split-draft-sad-design.md`.

**Tech Stack:** Python 3.11, Flask Blueprint (`invoice_routes.py`), PyMuPDF (fitz, lazy), openpyxl (lazy), `db.get_db()` (SQLite/PostgreSQL), pytest.

## Global Constraints

- SQL wyłącznie przez przekazane połączenie / `db.get_db()` — nigdy `sqlite3.connect()` w kodzie produkcyjnym (w testach in-memory SQLite jest OK, wzorzec repo).
- Liczby: `normalizer.normalize_number()` (zwraca `Decimal`), nigdy surowy `float()`.
- Ciężkie importy (fitz, pdfplumber, openpyxl, table_extractor) **lazy** (function-local) — `compileall` i boot bez ML muszą przejść.
- Migracje idempotentne: `CREATE TABLE IF NOT EXISTS` + `add_column` w `migrate_db.py`.
- Trasy chronione `core.security` (`login_required`, `require_role("user")`, `csrf_protect` dla POST); audyt przez `core.audit.log_audit`.
- UI Polish-first, dziedziczy po `base.html`.
- Enumy statusów z `constants.py` (`InvoiceJobStatus`, `MatchStatus`, nowy `DocKind`) — bez magic strings.
- Profil dostawcy OBOWIĄZKOWY (gate bez zmian).
- Realne PDF-y (próbka QA) trzymać POZA repo — dane handlowe; nie commitować.
- DoD: `python -m compileall . -q` czysty + `python -m pytest tests/ -q` zielony.
- Praca na gałęzi `claude/faktury-cipl-sad` (od HEAD `claude/faktury-standards`). **Nie pushować** bez decyzji — push `claude/**` uruchamia auto-merge → deploy.

**Realne interfejsy istniejącego kodu (zweryfikowane 2026-09-14):**
- `invoice_jobs`: `_ITEM_COLS = ("line_no","raw_ref","qty","net_amount","weight_net","weight_gross","uom_src","amount","descr","master_ref","name_pl","tariff_cn","sent","uom_factor","match_status","skipped")`; `_JOB_UPDATABLE = ("status","error","supplier_code","invoice_number")`; `create_job(db, batch_id, filename, pdf_path, supplier_code) -> int`; `list_jobs` / `list_invoice_jobs` (filtruje `status<>'packing_list'`) / `list_packing_lists` (filtruje `status='packing_list'`); `save_items` kasuje i wstawia; `_plain()` mapuje Enum→str; `_INT_COLS=("skipped","sent")`.
- `invoice_pipeline`: `process_job(db, job_id) -> str`; `_resolve_supplier_code(db, job)`; `confirm_job(db, job_id) -> (bool, str)`; importy top-level: `from packing_list_extractor import extract_weight_map`, `from supplier_profiles import get_supplier, detect_supplier`.
- `invoice_extractor.extract_invoice(pdf_path, supplier) -> {"items","total_net","total_qty","raw_text","invoice_number"}`; item ma klucze `line_no,raw_ref,descr,qty,net_amount,amount,uom_src,weight_net,weight_gross`; wewnętrzny szew `_parse_pdf(pdf_path, col_map)`.
- `invoice_mapper.map_items(db, raw_items, weight_map=None)`; `_fill_weight(item, weight_map)` (wypełnia wagi TYLKO gdy obie puste; alias sufiks-cyfrowy); `resolve_chosen_refs(db, job_id)`.
- `packing_list_extractor`: `is_packing_list(header_fields, raw_text)`, `first_pages_text(pdf_path, n=2)`, `_parse_pdf(pdf_path)` (szew), `_add(acc, key, field, raw)` (sumuje Decimal), `extract_weight_map(pdf_path) -> {ref_norm: {"weight_net","weight_gross"}}`.
- `table_extractor.parse_pdf(path, supplier_column_map=None) -> DocData` (`.items` — dicty `{"ref","desc","qty","price","net","lot","unit","weight_net","weight_gross"}`, `.total_net`, `.total_qty`, `.header_fields`, `.raw_text`); reguły kolumn: lista `rules` z krotkami `(ctype, keywords, priority)` — `weight_net`/`weight_gross` na ~L233-240; budowa itemu ~L786-794.
- `material_master.get_material(db, ref) -> dict|None` (kolumny m.in. `ref_code, opis_pl, txt_short_pl, tariff_cn, customs_code, sent, base_uom`); `normalize_ref(raw)`; `ensure_table(db)`; `find_candidates(db, raw_ref)`.
- `constants`: `InvoiceJobStatus(str, Enum)` — `UPLOADED/EXTRACTED/CONFIRMED/EXPORTED/ERROR/PACKING_LIST` (L48-55); `MatchStatus` (L58-62).
- `migrate_db.add_column(db, table, col, definition)` (L64); `invoice_jobs.ensure_invoice_tables(db)` wołane w L676.
- `invoice_routes`: Blueprint `invoice_bp` prefix `/invoices`; `upload()` klasyfikuje cały plik przez `is_packing_list` (L60-69); `coverage()` renderuje `invoice_coverage.html`; eksport Excela w `export()`.
- `templates/invoice_coverage.html`: link eksportu L35; thead jobs L37; wiersz joba L40-50.
- `uom.get_factor(unit_from, unit_to, ref, conversions) -> float|None` (kierunek: src→base).

---

### Task 1: Constants + schemat danych (`DocKind`, nowe kolumny, migracja)

**Files:**
- Modify: `constants.py` (po `MatchStatus`, ~L63)
- Modify: `invoice_jobs.py`
- Modify: `migrate_db.py` (~L676, po `ensure_invoice_tables`)
- Test: `tests/test_invoice_jobs_dockind.py`

**Interfaces:**
- Produces: `constants.DocKind(str, Enum)` — `INVOICE="invoice"`, `PROFORMA="proforma"`, `PACKING_LIST="packing_list"`, `OTHER="other"`; `InvoiceJobStatus.IGNORED = "ignored"`.
- Produces: `invoice_jobs.create_job(db, batch_id, filename, pdf_path, supplier_code, doc_kind=DocKind.INVOICE, source_file="", page_from=0, page_to=0) -> int`; kolumny `invoice_jobs`: `doc_kind, source_file, page_from, page_to, container_no, delivery_terms`; kolumna `invoice_items.cartons`; `update_job` przyjmuje dodatkowo `doc_kind, container_no, delivery_terms`; `list_packing_lists` filtruje po `doc_kind='packing_list' OR status='packing_list'`; `list_invoice_jobs` wyklucza też `doc_kind='packing_list'`.

- [ ] **Step 0: Gałąź robocza**

```bash
git checkout -b claude/faktury-cipl-sad
```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_invoice_jobs_dockind.py
import sqlite3
import invoice_jobs as ij
from constants import DocKind, InvoiceJobStatus


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    ij.ensure_invoice_tables(db)
    return db


def test_create_job_defaults_and_dockind():
    db = _db()
    jid = ij.create_job(db, "b", "a.pdf", "uploads/a.pdf", "S1")
    job = ij.get_job(db, jid)
    assert job["doc_kind"] == "invoice"
    assert job["source_file"] == ""
    jid2 = ij.create_job(db, "b", "a.pdf", "uploads/a_doc2.pdf", "",
                         doc_kind=DocKind.PROFORMA, source_file="a.pdf",
                         page_from=3, page_to=3)
    j2 = ij.get_job(db, jid2)
    assert j2["doc_kind"] == "proforma"
    assert j2["page_from"] == 3


def test_update_job_new_fields():
    db = _db()
    jid = ij.create_job(db, "b", "a.pdf", "p.pdf", "S1")
    ij.update_job(db, jid, container_no="ABCU1234567", delivery_terms="FOB SHANGHAI")
    job = ij.get_job(db, jid)
    assert job["container_no"] == "ABCU1234567"
    assert job["delivery_terms"] == "FOB SHANGHAI"


def test_items_cartons_roundtrip():
    db = _db()
    jid = ij.create_job(db, "b", "a.pdf", "p.pdf", "S1")
    ij.save_items(db, jid, [{"line_no": 1, "raw_ref": "X", "cartons": "25"}])
    assert ij.get_items(db, jid)[0]["cartons"] == "25"


def test_list_packing_lists_by_dockind():
    db = _db()
    plid = ij.create_job(db, "b", "pl.pdf", "p.pdf", "", doc_kind=DocKind.PACKING_LIST)
    ij.update_job(db, plid, status=InvoiceJobStatus.PACKING_LIST)
    ij.create_job(db, "b", "ci.pdf", "c.pdf", "S1")
    pls = ij.list_packing_lists(db, "b")
    assert len(pls) == 1 and pls[0]["id"] == plid
    assert len(ij.list_invoice_jobs(db, "b")) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_invoice_jobs_dockind.py -v`
Expected: FAIL — `ImportError: cannot import name 'DocKind'`

- [ ] **Step 3: constants.py — dodaj DocKind i IGNORED**

W `constants.py` dopisz do `InvoiceJobStatus` (po `PACKING_LIST`, L55):

```python
    IGNORED      = "ignored"        # B/L, skan bez tekstu itp. — poza pipeline'em faktur
```

Po klasie `MatchStatus` (po L62) dodaj:

```python
class DocKind(str, Enum):
    """Typ dokumentu wyciętego z PDF-zestawu (CIPL / komplet kontenerowy)."""
    INVOICE      = "invoice"        # Commercial Invoice
    PROFORMA     = "proforma"       # Proforma -S = próbki (por. spec 2026-09-14)
    PACKING_LIST = "packing_list"
    OTHER        = "other"          # B/L, strona-skan, nierozpoznane
```

- [ ] **Step 4: invoice_jobs.py — kolumny + sygnatury**

Zmiany w `invoice_jobs.py`:

1. `_ITEM_COLS` — dodaj `"cartons"` na końcu krotki:

```python
_ITEM_COLS = ("line_no", "raw_ref", "qty", "net_amount", "weight_net", "weight_gross",
              "uom_src", "amount", "descr", "master_ref", "name_pl", "tariff_cn",
              "sent", "uom_factor", "match_status", "skipped", "cartons")
```

2. `_JOB_UPDATABLE`:

```python
_JOB_UPDATABLE = ("status", "error", "supplier_code", "invoice_number",
                  "doc_kind", "container_no", "delivery_terms")
```

3. W `ensure_invoice_tables` — w `CREATE TABLE ... invoice_jobs` dopisz po `invoice_number TEXT DEFAULT '',`:

```python
        doc_kind TEXT DEFAULT 'invoice',
        source_file TEXT DEFAULT '',
        page_from INTEGER DEFAULT 0,
        page_to INTEGER DEFAULT 0,
        container_no TEXT DEFAULT '',
        delivery_terms TEXT DEFAULT '',
```

   a w `CREATE TABLE ... invoice_items` po `skipped INTEGER DEFAULT 0` (z przecinkiem po `skipped`):

```python
        cartons TEXT DEFAULT ''
```

4. `create_job` — nowa sygnatura (kompatybilna wstecz):

```python
def create_job(db, batch_id, filename, pdf_path, supplier_code,
               doc_kind="invoice", source_file="", page_from=0, page_to=0) -> int:
    ensure_invoice_tables(db)
    cur = db.execute(
        "INSERT INTO invoice_jobs (batch_id, filename, pdf_path, supplier_code, status, "
        "doc_kind, source_file, page_from, page_to) VALUES (?, ?, ?, ?, 'uploaded', ?, ?, ?, ?)",
        (batch_id, filename, pdf_path, supplier_code, _plain(doc_kind),
         source_file, page_from, page_to))
    db.commit()
    # lastrowid działa na SQLite; abstrakcja db.py mapuje to samo dla PG.
    return cur.lastrowid
```

5. `list_invoice_jobs` i `list_packing_lists` — filtr po obu polach (kompatybilność ze starymi wierszami, gdzie typ siedział w `status`):

```python
def list_invoice_jobs(db, batch_id) -> list:
    """Joby faktur/proform batcha — bez packing-list (te służą tylko jako źródło wag)."""
    rows = db.execute(
        "SELECT * FROM invoice_jobs WHERE batch_id=? AND status<>'packing_list' "
        "AND COALESCE(doc_kind,'invoice')<>'packing_list' ORDER BY id",
        (batch_id,)).fetchall()
    return [dict(r) for r in rows]


def list_packing_lists(db, batch_id) -> list:
    rows = db.execute(
        "SELECT * FROM invoice_jobs WHERE batch_id=? AND "
        "(COALESCE(doc_kind,'')='packing_list' OR status='packing_list') ORDER BY id",
        (batch_id,)).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 5: migrate_db.py — add_column dla istniejących baz**

W `migrate_db.py`, zaraz po `invoice_jobs.ensure_invoice_tables(db)` (L676):

```python
    add_column(db, "invoice_jobs", "doc_kind",       "TEXT DEFAULT 'invoice'")
    add_column(db, "invoice_jobs", "source_file",    "TEXT DEFAULT ''")
    add_column(db, "invoice_jobs", "page_from",      "INTEGER DEFAULT 0")
    add_column(db, "invoice_jobs", "page_to",        "INTEGER DEFAULT 0")
    add_column(db, "invoice_jobs", "container_no",   "TEXT DEFAULT ''")
    add_column(db, "invoice_jobs", "delivery_terms", "TEXT DEFAULT ''")
    add_column(db, "invoice_items", "cartons",       "TEXT DEFAULT ''")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_invoice_jobs_dockind.py tests/ -q`
Expected: nowe 4 testy PASS, reszta suite bez regresji.

- [ ] **Step 7: Migracja + compileall**

Run: `python -m compileall constants.py invoice_jobs.py migrate_db.py -q; python migrate_db.py; python migrate_db.py`
Expected: brak błędów; drugi run pokazuje `⏭ ... już istnieje` (idempotencja).

- [ ] **Step 8: Commit**

```bash
git add constants.py invoice_jobs.py migrate_db.py tests/test_invoice_jobs_dockind.py
git commit -m "feat(faktury): DocKind + kolumny zestawów (doc_kind/strony/kontener/warunki) i cartons"
```

---

### Task 2: `doc_splitter.py` — klasyfikacja stron i cięcie zestawów

**Files:**
- Create: `doc_splitter.py`
- Test: `tests/test_doc_splitter.py`

**Interfaces:**
- Consumes: `constants.DocKind`.
- Produces:
  - `classify_page(text: str) -> DocKind | None` — `None` = strona-kontynuacja (tekst bez nagłówka typu); pusta/prawie pusta strona (skan) → `DocKind.OTHER`.
  - `split_pdf(pdf_path: str) -> list[dict]` — `[{"kind": DocKind, "page_from": int, "page_to": int, "out_path": str}]`, strony 1-indeksowane. Zestaw jednodokumentowy → jedna część z `out_path == pdf_path` (bez kopii). Wycięte pliki: `<base>_doc<N>_<kind>.pdf` obok źródła.
  - Szwy testowalne: `_page_texts(pdf_path) -> list[str]`, `_write_range(pdf_path, page_from, page_to, out_path)` (oba lazy-importują fitz).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_doc_splitter.py
import doc_splitter as ds
from constants import DocKind

# Realne nagłówki z próbki QA: podwójna spacja (Kestrel), literówki
# PROFOMA (Kestrel) i PORFORMA (Tidewell); strona PL zawiera słowo "invoice".
CI = "ACME CO COMMERCIAL  INVOICE Shipper No. & date of Invoice A1 2026"
PL = "ACME CO PACKING LIST Shipper No. & date of invoice A1 2026 CTNS"
PROF = "ACME CO PROFORMA INVOICE No. & date of Invoice A1-S 2026"
PROF_TYPO1 = "ACME CO PROFOMA  INVOICE No. & date of Invoice A1-S 2026"
PROF_TYPO2 = "ACME CO PORFORMA INVOICE No. & date of Invoice A1-S 2026"
BL = "CORVAN MEDICAL BILL OF LADING BLNO0000001 SHANGHAI GDANSK POLAND"
CONT = "RNBL10001 Disposable gloves 987654321N 4,400 19.000 83,600.00 page 2"
SCAN = "DRAFT"


def test_classify_variants():
    assert ds.classify_page(CI) == DocKind.INVOICE
    assert ds.classify_page(PL) == DocKind.PACKING_LIST
    assert ds.classify_page(PROF) == DocKind.PROFORMA
    assert ds.classify_page(PROF_TYPO1) == DocKind.PROFORMA
    assert ds.classify_page(PROF_TYPO2) == DocKind.PROFORMA
    assert ds.classify_page(BL) == DocKind.OTHER
    assert ds.classify_page(SCAN) == DocKind.OTHER      # skan/pusta → other
    assert ds.classify_page("") == DocKind.OTHER
    assert ds.classify_page(CONT) is None               # kontynuacja


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_doc_splitter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'doc_splitter'`

- [ ] **Step 3: Write minimal implementation**

```python
# doc_splitter.py
"""Tnie PDF-zestaw (CIPL / komplet kontenerowy) na osobne dokumenty po klasyfikacji
stron. Klasyfikacja po tekście strony; PyMuPDF importowany lazy (konwencja repo)."""

import os
import re

from constants import DocKind

# Kolejność ma znaczenie: strona packing listy zawiera słowo "invoice" w polach
# nagłówka ("No. & date of invoice"), więc PACKING LIST sprawdzamy przed fakturami.
# Literówki z realnych dokumentów: PROFOMA (Kestrel), PORFORMA (Tidewell).
_MARKERS = [
    (DocKind.PACKING_LIST, ("PACKING LIST", "PACKING NOTE", "LISTA PAKOWANIA")),
    (DocKind.PROFORMA, ("PROFORMA INVOICE", "PROFOMA INVOICE", "PORFORMA INVOICE")),
    (DocKind.INVOICE, ("COMMERCIAL INVOICE",)),
    (DocKind.OTHER, ("BILL OF LADING", "OCEAN BILL", "SEA WAYBILL")),
]


def classify_page(text: str):
    """DocKind strony albo None (kontynuacja — tekst bez nagłówka typu).
    Strona bez sensownego tekstu (skan) → DocKind.OTHER."""
    t = re.sub(r"\s+", " ", (text or "")).upper().strip()
    if len(t) < 30:
        return DocKind.OTHER
    for kind, markers in _MARKERS:
        if any(m in t for m in markers):
            return kind
    return None


def _page_texts(pdf_path: str) -> list:
    # Szew testowalny + lazy import PyMuPDF.
    import fitz
    doc = fitz.open(pdf_path)
    try:
        return [(pg.get_text() or "") for pg in doc]
    finally:
        doc.close()


def _write_range(pdf_path: str, page_from: int, page_to: int, out_path: str) -> None:
    import fitz
    src = fitz.open(pdf_path)
    out = fitz.open()
    try:
        out.insert_pdf(src, from_page=page_from - 1, to_page=page_to - 1)
        out.save(out_path)
    finally:
        out.close()
        src.close()


def split_pdf(pdf_path: str) -> list:
    """[{kind, page_from, page_to, out_path}] — strony 1-indeksowane.
    Jednodokumentowy PDF → jedna część wskazująca ORYGINALNY plik (bez kopii)."""
    texts = _page_texts(pdf_path)
    parts = []
    for i, text in enumerate(texts, start=1):
        kind = classify_page(text)
        if kind is None and parts:
            parts[-1]["page_to"] = i            # kontynuacja poprzedniego dokumentu
            continue
        parts.append({"kind": kind or DocKind.OTHER, "page_from": i, "page_to": i})
    if len(parts) <= 1:
        if parts:
            parts[0]["out_path"] = pdf_path
        return parts
    base, _ = os.path.splitext(pdf_path)
    for n, p in enumerate(parts, start=1):
        p["out_path"] = f"{base}_doc{n}_{p['kind'].value}.pdf"
        _write_range(pdf_path, p["page_from"], p["page_to"], p["out_path"])
    return parts
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_doc_splitter.py -v`
Expected: PASS (4 testy)

- [ ] **Step 5: Commit**

```bash
git add doc_splitter.py tests/test_doc_splitter.py
git commit -m "feat(faktury): doc_splitter — klasyfikacja stron CIPL i cięcie zestawów"
```

---

### Task 3: Kartony — kaskada, packing lista, mapper

**Files:**
- Modify: `table_extractor.py` (reguły kolumn ~L240, item ~L793)
- Modify: `invoice_extractor.py` (przelot `cartons`)
- Modify: `packing_list_extractor.py` (`extract_weight_map`)
- Modify: `invoice_mapper.py` (`_fill_weight`)
- Test: `tests/test_cartons.py`

**Interfaces:**
- Produces: item kaskady (`parse_pdf(...).items[i]`) ma dodatkowy klucz `"cartons"`; `extract_invoice(...)["items"][i]["cartons"]`; `extract_weight_map(pdf_path) -> {ref_norm: {"weight_net","weight_gross","cartons"}}` (sumy Decimal jako str); `_fill_weight` uzupełnia też `cartons` (tylko gdy na pozycji puste).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cartons.py
import packing_list_extractor as ple
import invoice_mapper as im


class _Doc:
    def __init__(self, items):
        self.items = items


def test_weight_map_includes_cartons(monkeypatch):
    monkeypatch.setattr(ple, "_parse_pdf", lambda p: _Doc([
        {"ref": "X1", "weight_net": "10", "weight_gross": "12", "cartons": "25"},
        {"ref": "X1", "weight_net": "5", "weight_gross": "6", "cartons": "5"},
        {"ref": "Y", "cartons": "7"},          # PL bez wag, same kartony
    ]))
    wm = ple.extract_weight_map("pl.pdf")
    assert wm["X1"]["cartons"] == "30"
    assert wm["X1"]["weight_net"] == "15"
    assert wm["Y"]["cartons"] == "7"


def test_fill_weight_fills_cartons_when_empty():
    entry = {"X1": {"weight_net": "15", "weight_gross": "18", "cartons": "30"}}
    item = {"raw_ref": "X1", "weight_net": "", "weight_gross": "", "cartons": ""}
    im._fill_weight(item, entry)
    assert item["cartons"] == "30"
    item2 = {"raw_ref": "X1", "weight_net": "", "weight_gross": "", "cartons": "9"}
    im._fill_weight(item2, entry)
    assert item2["cartons"] == "9"             # wartość z faktury wygrywa
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cartons.py -v`
Expected: FAIL — `KeyError: 'cartons'` (weight_map nie zna kartonów)

- [ ] **Step 3: table_extractor.py — rola kolumny cartons**

W liście `rules` po wpisie `weight_net` (po ~L240) dodaj:

```python
        ("cartons", [
            "ctns", "cartons", "carton qty", "no. of cartons",
            "number of cartons", "liczba kartonów", "kartony",
        ], 7),
```

W budowie itemu (po `"weight_gross": get_cell(row, ci, "weight_gross"),` ~L793) dodaj:

```python
            "cartons": get_cell(row, ci, "cartons"),
```

- [ ] **Step 4: invoice_extractor.py — przelot cartons**

W `extract_invoice`, w dict itemu po `"weight_gross": ...` dodaj:

```python
            "cartons": raw.get("cartons") or "",
```

- [ ] **Step 5: packing_list_extractor.py — kartony w weight_map**

W `extract_weight_map` zamień pętlę na:

```python
    for it in (doc.items or []):
        ref = mm.normalize_ref(it.get("ref"))
        if not ref:
            continue
        wn, wg = it.get("weight_net"), it.get("weight_gross")
        ct = it.get("cartons")
        if (wn in (None, "")) and (wg in (None, "")) and (ct in (None, "")):
            continue
        out.setdefault(ref, {})
        _add(out, ref, "weight_net", wn)
        _add(out, ref, "weight_gross", wg)
        _add(out, ref, "cartons", ct)
```

- [ ] **Step 6: invoice_mapper.py — _fill_weight uzupełnia cartons**

W `_fill_weight`, zamień końcówkę funkcji (od komentarza „Pierwszeństwo") na:

```python
    # Pierwszeństwo: waga z tabeli faktury (jeśli była) wygrywa; PL uzupełnia TYLKO puste.
    if entry and (not item.get("weight_net")) and (not item.get("weight_gross")):
        item["weight_net"] = entry.get("weight_net", "") or item.get("weight_net", "")
        item["weight_gross"] = entry.get("weight_gross", "") or item.get("weight_gross", "")
        item["weight_source"] = "pl"
    else:
        item["weight_source"] = item.get("weight_source") or "brak"
    # Kartony analogicznie, ale niezależnie od wag (faktury zwykle ich nie mają).
    if entry and not item.get("cartons"):
        item["cartons"] = entry.get("cartons", "") or ""
```

- [ ] **Step 7: Run tests to verify they pass (+ regresja)**

Run: `python -m pytest tests/test_cartons.py tests/ -q`
Expected: PASS; brak regresji (w szczególności `tests/test_packing_list_extractor.py` i `tests/test_invoice_pipeline_weight.py`).

- [ ] **Step 8: Commit**

```bash
git add table_extractor.py invoice_extractor.py packing_list_extractor.py invoice_mapper.py tests/test_cartons.py
git commit -m "feat(faktury): kartony (CTNS) z packing list — kaskada, weight_map, mapper"
```

---

### Task 4: Pipeline — metadane nagłówka (kontener/warunki) + PL per faktura

**Files:**
- Modify: `invoice_pipeline.py`
- Test: `tests/test_invoice_pipeline_meta.py`

**Interfaces:**
- Consumes: `invoice_jobs.list_packing_lists`, `packing_list_extractor.first_pages_text` / `extract_weight_map`, `invoice_jobs.update_job(..., container_no=, delivery_terms=)`.
- Produces:
  - `_header_meta(raw_text: str) -> {"container_no": str, "delivery_terms": str}` (kontener bez spacji, np. `ABCU1234567`).
  - `_weight_map_for(db, batch_id: str, invoice_number: str) -> dict` — najpierw PL, których tekst zawiera numer faktury; brak dopasowania → unia wszystkich PL batcha (fallback).
  - `process_job` zapisuje `container_no`/`delivery_terms` i używa `_weight_map_for`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_invoice_pipeline_meta.py
import sqlite3
import invoice_jobs as ij
import invoice_pipeline as ip
from constants import DocKind


def test_header_meta_variants():
    # Formaty z realnej próbki: "CONTAINER No.:XYZU7654321" (Kestrel, bez spacji),
    # "Container NO. : ABCU1234567" (Corvan, spacja przed dwukropkiem),
    # warunki dostawy łamane do nowej linii.
    t1 = "CONTAINER No.:XYZU7654321 ... Terms of Delivery:\nFOB QINGDAO"
    t2 = "Container NO. : ABCU1234567 ... TERMS OF DELIVERY: FOB SHANGHAI"
    m1, m2 = ip._header_meta(t1), ip._header_meta(t2)
    assert m1["container_no"] == "XYZU7654321"
    assert m1["delivery_terms"].startswith("FOB QINGDAO")
    assert m2["container_no"] == "ABCU1234567"
    assert m2["delivery_terms"].startswith("FOB SHANGHAI")
    assert ip._header_meta("brak danych") == {"container_no": "", "delivery_terms": ""}


def test_weight_map_prefers_matching_pl(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    ij.ensure_invoice_tables(db)
    ij.create_job(db, "b", "pl1.pdf", "pl1.pdf", "", doc_kind=DocKind.PACKING_LIST)
    ij.create_job(db, "b", "pl2.pdf", "pl2.pdf", "", doc_kind=DocKind.PACKING_LIST)
    texts = {"pl1.pdf": "PACKING LIST No.& date of invoice FV/1",
             "pl2.pdf": "PACKING LIST No.& date of invoice FV/2"}
    maps = {"pl1.pdf": {"X": {"weight_net": "1"}},
            "pl2.pdf": {"X": {"weight_net": "9"}}}
    monkeypatch.setattr(ip, "first_pages_text", lambda p, n=2: texts[p])
    monkeypatch.setattr(ip, "extract_weight_map", lambda p: maps[p])
    wm = ip._weight_map_for(db, "b", "FV/2")
    assert wm["X"]["weight_net"] == "9"        # tylko pasująca PL
    wm_fb = ip._weight_map_for(db, "b", "NIEZNANY")
    assert wm_fb["X"]["weight_net"] == "9"     # fallback: unia wszystkich (ostatnia wygrywa)
    assert ip._weight_map_for(db, "pusta-partia", "FV/1") == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_invoice_pipeline_meta.py -v`
Expected: FAIL — `AttributeError: ... has no attribute '_header_meta'`

- [ ] **Step 3: invoice_pipeline.py — implementacja**

Na górze pliku: dodaj `import re` i rozszerz import z packing_list_extractor:

```python
import re

import invoice_jobs as ij
from constants import InvoiceJobStatus, MatchStatus
from invoice_extractor import extract_invoice
from invoice_mapper import map_items
from packing_list_extractor import extract_weight_map, first_pages_text
from supplier_profiles import get_supplier, detect_supplier
```

(`_resolve_supplier_code` przestaje robić lokalny import `first_pages_text` — użyj tego z góry.)

Dodaj po `_resolve_supplier_code`:

```python
_CONTAINER_RE = re.compile(r"CONTAINER\s*NO\.?\s*[:：]?\s*([A-Z]{4}\s?\d{7})", re.IGNORECASE)
_TERMS_RE = re.compile(r"TERMS\s+OF\s+DELIVERY\s*[:：]?\s*([A-Z]{3}[^\n\r]{0,40})",
                       re.IGNORECASE)


def _header_meta(raw_text: str) -> dict:
    """Kontener i warunki dostawy z tekstu nagłówka faktury (dla draftu SAD)."""
    t = raw_text or ""
    m = _CONTAINER_RE.search(t)
    d = _TERMS_RE.search(t)
    return {"container_no": m.group(1).replace(" ", "").upper() if m else "",
            "delivery_terms": d.group(1).strip() if d else ""}


def _weight_map_for(db, batch_id: str, invoice_number: str) -> dict:
    """Mapa wag/kartonów z packing list batcha: najpierw PL, których tekst zawiera
    numer TEJ faktury; żadna nie pasuje → unia wszystkich PL batcha (fallback)."""
    pls = ij.list_packing_lists(db, batch_id)
    if not pls:
        return {}
    matched = []
    if invoice_number:
        for pl in pls:
            if invoice_number in first_pages_text(pl["pdf_path"], 2):
                matched.append(pl)
    out = {}
    for pl in (matched or pls):
        out.update(extract_weight_map(pl["pdf_path"]))
    return out
```

W `process_job` zamień blok budowy `weight_map` (komentarz „Mapa wag z packing list…" i pętla `for pl in ij.list_packing_lists(...)`) oraz końcowy `update_job` na:

```python
    meta = _header_meta(extracted.get("raw_text") or "")
    try:
        weight_map = _weight_map_for(db, job.get("batch_id") or "",
                                     extracted.get("invoice_number") or "")
    except Exception:
        weight_map = {}   # waga to dane pomocnicze — błąd nie wywraca faktury
    mapped = map_items(db, extracted["items"], weight_map=weight_map)
    ij.save_items(db, job_id, mapped)
    ij.update_job(db, job_id, status=InvoiceJobStatus.EXTRACTED,
                  invoice_number=extracted.get("invoice_number") or "",
                  container_no=meta["container_no"],
                  delivery_terms=meta["delivery_terms"])
    return InvoiceJobStatus.EXTRACTED
```

- [ ] **Step 4: Run tests to verify they pass (+ regresja pipeline)**

Run: `python -m pytest tests/test_invoice_pipeline_meta.py tests/test_invoice_pipeline.py tests/test_invoice_pipeline_weight.py -v`
Expected: PASS. Uwaga: jeśli stare testy monkeypatchują `packing_list_extractor.first_pages_text` przez lokalny import w `_resolve_supplier_code` — po przeniesieniu importu na górę patchują teraz `ip.first_pages_text`; dostosuj istniejące testy, jeżeli czerwone (zmiana miejsca patcha, nie logiki).

- [ ] **Step 5: Commit**

```bash
git add invoice_pipeline.py tests/test_invoice_pipeline_meta.py
git commit -m "feat(faktury): kontener+Incoterms z nagłówka CI, wagi PL dopasowane po nr faktury"
```

---

### Task 5: `sad_draft_export.py` — agregacja eksporter × CN + workbook + gating

**Files:**
- Create: `sad_draft_export.py`
- Test: `tests/test_sad_draft_export.py`

**Interfaces:**
- Consumes: `invoice_jobs.list_invoice_jobs/get_items`, `material_master.get_material` (po `customs_code`), `normalizer.normalize_number`, `constants.DocKind/InvoiceJobStatus/MatchStatus`.
- Produces:
  - `SAD_COLUMNS: list[str]` — kontrakt arkusza „Pozycje".
  - `aggregate_positions(db, jobs_with_items: list[{"job": dict, "items": list}]) -> list[dict]` — wiersze per `(supplier_code, tariff_cn)`; pola: `lp, exporter, tariff_cn, customs_code, opis_pl, qty_szt: Decimal|None, weight_net: Decimal|None, cartons: Decimal|None, value: Decimal|None, country, invoice_numbers: str, proforma_numbers: str, sent: int, uwagi: str`.
  - `collect_batch(db, batch_id) -> (header: dict, positions: list)`; `ValueError` gdy nie wszystkie faktury/proformy batcha `confirmed` (komunikat z licznikiem `X/Y`).
  - `build_sad_workbook(header, positions) -> openpyxl.Workbook` — arkusze `Nagłówek` + `Pozycje`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sad_draft_export.py
import sqlite3
from decimal import Decimal

import pytest

import invoice_jobs as ij
import sad_draft_export as sad
from constants import DocKind, InvoiceJobStatus


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    import material_master as mm
    mm.ensure_table(db)
    ij.ensure_invoice_tables(db)
    return db


def _mk_job(db, num, doc_kind=DocKind.INVOICE, supplier="CORVAN",
            status=InvoiceJobStatus.CONFIRMED, items=()):
    jid = ij.create_job(db, "b1", f"{num}.pdf", f"{num}.pdf", supplier, doc_kind=doc_kind)
    ij.update_job(db, jid, status=status, invoice_number=num)
    ij.save_items(db, jid, list(items))
    return ij.get_job(db, jid)


IT = dict(line_no=1, raw_ref="S001", master_ref="S001", name_pl="Staza",
          tariff_cn="90189084", qty="100", uom_factor="", uom_src="PCS",
          amount="96.50", weight_net="10", weight_gross="12", cartons="2",
          sent=0, match_status="matched", skipped=0)


def _jw(db, *jobs):
    return [{"job": j, "items": ij.get_items(db, j["id"])} for j in jobs]


def test_two_invoices_same_exporter_cn_merge():
    db = _db()
    j1 = _mk_job(db, "26H00001", items=[IT])
    j2 = _mk_job(db, "26H30514", items=[dict(IT, qty="50", amount="48.25")])
    rows = sad.aggregate_positions(db, _jw(db, j1, j2))
    assert len(rows) == 1
    r = rows[0]
    assert r["qty_szt"] == Decimal("150")
    assert r["value"] == Decimal("144.75")
    assert r["weight_net"] == Decimal("20")
    assert r["cartons"] == Decimal("4")
    assert "26H00001" in r["invoice_numbers"] and "26H30514" in r["invoice_numbers"]


def test_proforma_adds_samples_note_and_qty():
    db = _db()
    j1 = _mk_job(db, "26H00001", items=[IT])
    j2 = _mk_job(db, "26H00001S", doc_kind=DocKind.PROFORMA,
                 items=[dict(IT, qty="2", amount="0.02")])
    rows = sad.aggregate_positions(db, _jw(db, j1, j2))
    r = rows[0]
    assert r["qty_szt"] == Decimal("102")
    assert "WRAZ Z PRÓBKAMI" in r["opis_pl"]
    assert r["proforma_numbers"] == "26H00001S"
    assert r["invoice_numbers"] == "26H00001"


def test_unmatched_separate_row_with_note():
    db = _db()
    j = _mk_job(db, "F1", items=[
        IT,
        dict(IT, line_no=2, raw_ref="Q", master_ref="", name_pl="",
             tariff_cn="", match_status="unmatched")])
    rows = sad.aggregate_positions(db, _jw(db, j))
    assert len(rows) == 2
    brak = [r for r in rows if not r["tariff_cn"]][0]
    assert "BRAK CN" in brak["uwagi"]


def test_uom_factor_multiplies_qty_and_missing_factor_notes():
    db = _db()
    j = _mk_job(db, "F1", items=[
        dict(IT, qty="10", uom_factor="24.0", uom_src="CTN"),
        dict(IT, line_no=2, qty="5", uom_factor="", uom_src="CTN")])
    rows = sad.aggregate_positions(db, _jw(db, j))
    r = rows[0]
    assert r["qty_szt"] == Decimal("245")      # 10*24 + 5*1
    assert "jednostki źródłowe" in r["uwagi"]


def test_skipped_items_excluded():
    db = _db()
    j = _mk_job(db, "F1", items=[IT, dict(IT, line_no=2, skipped=1, qty="999")])
    rows = sad.aggregate_positions(db, _jw(db, j))
    assert rows[0]["qty_szt"] == Decimal("100")


def test_collect_batch_gating_and_ok():
    db = _db()
    _mk_job(db, "F1", items=[IT])
    _mk_job(db, "F2", status=InvoiceJobStatus.EXTRACTED, items=[IT])
    with pytest.raises(ValueError, match="1/2"):
        sad.collect_batch(db, "b1")


def test_collect_batch_header_sums():
    db = _db()
    j1 = _mk_job(db, "F1", items=[IT])
    ij.update_job(db, j1["id"], container_no="ABCU1234567",
                  delivery_terms="FOB SHANGHAI")
    header, positions = sad.collect_batch(db, "b1")
    assert header["container"] == "ABCU1234567"
    assert header["delivery_terms"] == "FOB SHANGHAI"
    assert header["total_value"] == Decimal("96.5")
    assert header["total_gross"] == Decimal("12")
    assert len(positions) == 1


def test_workbook_contract():
    header = {"container": "ABCU1234567", "container_warning": False,
              "delivery_terms": "FOB SHANGHAI", "country_dispatch": "CN",
              "currency": "USD", "total_value": Decimal("144.75"),
              "total_net": Decimal("20"), "total_gross": Decimal("24"),
              "total_cartons": Decimal("4"),
              "documents": [{"number": "26H00001", "supplier": "CORVAN",
                             "kind": "invoice"}]}
    pos = [{"lp": 1, "exporter": "CORVAN", "tariff_cn": "90189084",
            "customs_code": "V020", "opis_pl": "Staza",
            "qty_szt": Decimal("150"), "weight_net": Decimal("20"),
            "cartons": Decimal("4"), "value": Decimal("144.75"), "country": "CN",
            "invoice_numbers": "26H00001", "proforma_numbers": "",
            "sent": 1, "uwagi": ""}]
    wb = sad.build_sad_workbook(header, pos)
    assert wb.sheetnames == ["Nagłówek", "Pozycje"]
    wp = wb["Pozycje"]
    assert [c.value for c in wp[1]] == sad.SAD_COLUMNS
    assert wp.cell(row=2, column=2).value == "CORVAN"
    assert wp.cell(row=2, column=13).value == "TAK"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sad_draft_export.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sad_draft_export'`

- [ ] **Step 3: Write minimal implementation**

```python
# sad_draft_export.py
"""Draft SAD dla agencji celnej: agregacja zatwierdzonych pozycji batcha per
(eksporter × kod CN) — jak pozycje zgłoszenia w WinSAD — i budowa Excela
(arkusze Nagłówek + Pozycje). openpyxl importowany lazy (konwencja repo).
Wzorzec pól: wydruk WinSAD „Podgląd danych zgłoszenia" (spec 2026-09-14)."""

from decimal import Decimal

import material_master as mm
from constants import DocKind, InvoiceJobStatus, MatchStatus
from normalizer import normalize_number

SAD_COLUMNS = ["Lp", "Eksporter", "Kod CN", "Kod dod.", "Opis PL", "Ilość SZT",
               "Masa netto", "Kartony", "Wartość [USD]", "Kraj poch.",
               "Nr faktur", "Nr proform", "SENT", "Uwagi"]

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _dec(v):
    if v in (None, ""):
        return None
    return normalize_number(str(v))


def _add(acc, d):
    return (acc if acc is not None else Decimal(0)) + d


def aggregate_positions(db, jobs_with_items: list) -> list:
    """jobs_with_items: [{'job': dict, 'items': list[dict]}] (tylko confirmed).
    Wiersz per (supplier_code, tariff_cn); pozycje bez CN → osobny wiersz z uwagą."""
    groups = {}
    for jw in jobs_with_items:
        job, items = jw["job"], jw["items"]
        is_proforma = (job.get("doc_kind") or "") == DocKind.PROFORMA
        doc_no = (job.get("invoice_number") or "").strip() or (job.get("filename") or "")
        for it in items:
            if int(it.get("skipped") or 0):
                continue
            cn = (it.get("tariff_cn") or "").strip()
            key = (job.get("supplier_code") or "", cn)
            g = groups.setdefault(key, {
                "exporter": key[0], "tariff_cn": cn, "customs_code": "",
                "names": [], "qty": None, "qty_note": False, "weight_net": None,
                "cartons": None, "value": None, "sent": 0,
                "invoices": set(), "proformas": set(), "has_samples": False,
            })
            qty = _dec(it.get("qty"))
            factor = _dec(it.get("uom_factor"))
            if qty is not None:
                g["qty"] = _add(g["qty"], qty * (factor if factor else Decimal(1)))
                if not factor and (it.get("uom_src") or "").upper() not in ("", "PCS", "SZT"):
                    g["qty_note"] = True
            for field, col in (("weight_net", "weight_net"), ("cartons", "cartons"),
                               ("value", "amount")):
                d = _dec(it.get(col))
                if d is not None:
                    g[field] = _add(g[field], d)
            name = (it.get("name_pl") or "").strip()
            if name and name not in g["names"]:
                g["names"].append(name)
            g["sent"] = max(g["sent"], int(it.get("sent") or 0))
            if is_proforma:
                g["proformas"].add(doc_no)
                g["has_samples"] = True
            else:
                g["invoices"].add(doc_no)
            if cn and not g["customs_code"] and it.get("master_ref"):
                mat = mm.get_material(db, it["master_ref"]) or {}
                g["customs_code"] = mat.get("customs_code") or ""
    rows = []
    for i, key in enumerate(sorted(groups), start=1):
        g = groups[key]
        opis = "; ".join(g["names"])
        if g["has_samples"]:
            opis = (opis + " WRAZ Z PRÓBKAMI").strip()
        uwagi = []
        if not g["tariff_cn"]:
            uwagi.append("BRAK CN — uzupełnij master data")
        if g["qty_note"]:
            uwagi.append("ilość zawiera jednostki źródłowe (brak przelicznika)")
        rows.append({
            "lp": i, "exporter": g["exporter"], "tariff_cn": g["tariff_cn"],
            "customs_code": g["customs_code"], "opis_pl": opis,
            "qty_szt": g["qty"], "weight_net": g["weight_net"],
            "cartons": g["cartons"], "value": g["value"],
            "country": "CN",   # v1: kraj wysyłki (spec — rewizja przy dostawcy spoza Chin)
            "invoice_numbers": ", ".join(sorted(g["invoices"])),
            "proforma_numbers": ", ".join(sorted(g["proformas"])),
            "sent": g["sent"], "uwagi": "; ".join(uwagi),
        })
    return rows


def collect_batch(db, batch_id: str):
    """(header, positions) batcha. ValueError, gdy nie wszystkie faktury/proformy
    zatwierdzone — niepełny draft = błędny SAD."""
    import invoice_jobs as ij
    docs = [j for j in ij.list_invoice_jobs(db, batch_id)
            if (j.get("doc_kind") or "invoice") in (DocKind.INVOICE, DocKind.PROFORMA)]
    if not docs:
        raise ValueError("Brak faktur w partii")
    confirmed = [j for j in docs if j.get("status") == InvoiceJobStatus.CONFIRMED]
    if len(confirmed) != len(docs):
        raise ValueError(f"Zatwierdzono {len(confirmed)}/{len(docs)} dokumentów — "
                         "dokończ przegląd przed generowaniem draftu SAD")
    jw = [{"job": j, "items": ij.get_items(db, j["id"])} for j in confirmed]
    positions = aggregate_positions(db, jw)

    def _total(field):
        vals = [r[field] for r in positions if r[field] is not None]
        return sum(vals, Decimal(0)) if vals else None

    gross = None
    for e in jw:
        for it in e["items"]:
            if int(it.get("skipped") or 0):
                continue
            d = _dec(it.get("weight_gross"))
            if d is not None:
                gross = _add(gross, d)
    containers = sorted({(j.get("container_no") or "").strip() for j in confirmed} - {""})
    terms = sorted({(j.get("delivery_terms") or "").strip() for j in confirmed} - {""})
    header = {
        "container": ", ".join(containers),
        "container_warning": len(containers) > 1,
        "delivery_terms": ", ".join(terms),
        "country_dispatch": "CN",
        "currency": "USD",
        "total_value": _total("value"),
        "total_net": _total("weight_net"),
        "total_gross": gross,
        "total_cartons": _total("cartons"),
        "documents": [{"number": (j.get("invoice_number") or j.get("filename") or ""),
                       "supplier": j.get("supplier_code") or "",
                       "kind": j.get("doc_kind") or "invoice"} for j in confirmed],
    }
    return header, positions


def _num(v):
    return "" if v is None else float(v)


def build_sad_workbook(header: dict, positions: list):
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Nagłówek"
    rows = [
        ("Kontener", header.get("container") or ""),
        ("Warunki dostawy", header.get("delivery_terms") or ""),
        ("Kraj wysyłki", header.get("country_dispatch") or ""),
        ("Waluta", header.get("currency") or ""),
        ("Wartość faktur razem", _num(header.get("total_value"))),
        ("Masa netto razem", _num(header.get("total_net"))),
        ("Masa brutto razem", _num(header.get("total_gross"))),
        ("Liczba kartonów razem", _num(header.get("total_cartons"))),
    ]
    if header.get("container_warning"):
        rows.insert(1, ("UWAGA", "Różne numery kontenerów w partii!"))
    for k, v in rows:
        ws.append([k, v])
    ws.append([])
    ws.append(["Dokumenty:"])
    for d in header.get("documents", []):
        ws.append([d.get("kind", ""), d.get("number", ""), d.get("supplier", "")])
    for c in ws["A"]:
        c.font = Font(bold=True)
    wp = wb.create_sheet("Pozycje")
    wp.append(SAD_COLUMNS)
    for c in wp[1]:
        c.font = Font(bold=True)
    for r in positions:
        wp.append([r["lp"], r["exporter"], r["tariff_cn"], r["customs_code"],
                   r["opis_pl"], _num(r["qty_szt"]), _num(r["weight_net"]),
                   _num(r["cartons"]), _num(r["value"]), r["country"],
                   r["invoice_numbers"], r["proforma_numbers"],
                   "TAK" if r["sent"] else "NIE", r["uwagi"]])
    return wb
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_sad_draft_export.py -v`
Expected: PASS (8 testów)

- [ ] **Step 5: Commit**

```bash
git add sad_draft_export.py tests/test_sad_draft_export.py
git commit -m "feat(faktury): sad_draft_export — agregacja eksporter×CN, gating, workbook WinSAD"
```

---

### Task 6: Routes — upload przez splitter, trasa `/invoices/sad/<batch_id>`, coverage UI

**Files:**
- Modify: `invoice_routes.py` (upload L44-72; nowa trasa po `export`)
- Modify: `templates/invoice_coverage.html` (L35 link; L37 thead; L40-50 wiersz joba)
- Test: `tests/test_invoice_routes_sad_smoke.py`

**Interfaces:**
- Consumes: `doc_splitter.split_pdf`, `sad_draft_export.collect_batch/build_sad_workbook/_XLSX_MIME`, `constants.DocKind/InvoiceJobStatus`, `invoice_jobs.create_job(...)` z Task 1.
- Produces: trasa `GET /invoices/sad/<batch_id>` (endpoint `invoices.sad_export`) — 200 xlsx `draft_sad_<batch_id>.xlsx` albo 409 z komunikatem gatingu; upload tworzy joby per wycięty dokument.

- [ ] **Step 1: Write the failing smoke test**

```python
# tests/test_invoice_routes_sad_smoke.py
import app as appmod


def test_sad_route_registered():
    rules = {r.rule for r in appmod.app.url_map.iter_rules()}
    assert "/invoices/sad/<batch_id>" in rules


def test_sad_route_requires_login():
    client = appmod.app.test_client()
    r = client.get("/invoices/sad/abc123")
    assert r.status_code in (302, 401)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_invoice_routes_sad_smoke.py -v`
Expected: FAIL — brak `/invoices/sad/<batch_id>` w url_map.

- [ ] **Step 3: invoice_routes.py — upload przez splitter**

Do importów u góry dodaj:

```python
from constants import InvoiceJobStatus, DocKind
import doc_splitter
```

(zamiast dotychczasowego `from constants import InvoiceJobStatus`).

Dodaj funkcję pomocniczą nad `upload()`:

```python
def _split_or_whole(path):
    """Części z doc_splittera; gdy cięcie padnie (uszkodzony/zaszyfrowany PDF) —
    cały plik jako jedna część z klasyfikacją starą metodą. Nie blokuje uploadu."""
    try:
        parts = doc_splitter.split_pdf(path)
        if parts:
            return parts
    except Exception:
        pass
    from packing_list_extractor import is_packing_list, first_pages_text
    kind = (DocKind.PACKING_LIST if is_packing_list({}, first_pages_text(path))
            else DocKind.INVOICE)
    return [{"kind": kind, "page_from": 1, "page_to": 1, "out_path": path}]
```

W `upload()` zamień wnętrze pętli `for idx, f in enumerate(files):` — od komentarza
„Klasyfikacja: packing lista vs faktura…" do `threading.Thread(...).start()` — na:

```python
        for part in _split_or_whole(path):
            kind = part["kind"]
            sup = forced_supplier if kind in (DocKind.INVOICE, DocKind.PROFORMA) else ""
            jid = ij.create_job(db, batch_id, fn, part["out_path"], sup,
                                doc_kind=kind, source_file=path,
                                page_from=part["page_from"], page_to=part["page_to"])
            if kind == DocKind.PACKING_LIST:
                ij.update_job(db, jid, status=InvoiceJobStatus.PACKING_LIST)
            elif kind == DocKind.OTHER:
                ij.update_job(db, jid, status=InvoiceJobStatus.IGNORED)
            else:
                threading.Thread(target=_process_async, args=(jid,), daemon=True).start()
```

- [ ] **Step 4: invoice_routes.py — trasa Draft SAD**

Po funkcji `export()` dodaj:

```python
@invoice_bp.route("/sad/<batch_id>")
@login_required
@require_role("user")
def sad_export(batch_id):
    db = get_db()
    import sad_draft_export as sad
    try:
        header, positions = sad.collect_batch(db, batch_id)
    except ValueError as e:
        return str(e), 409
    log_audit("invoice_sad_export", session.get("username"),
              detail=f"batch {batch_id}: {len(positions)} pozycji")
    wb = sad.build_sad_workbook(header, positions)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f"draft_sad_{batch_id}.xlsx",
                     mimetype=sad._XLSX_MIME)
```

- [ ] **Step 5: templates/invoice_coverage.html — link + kolumna Typ**

Linia 35 — zamień na:

```html
    <p class="mb-3">
      <a class="text-indigo-600 underline" href="{{ url_for('invoices.export', batch_id=batch_id) }}">Eksportuj do Excela</a>
      · <a class="text-indigo-600 underline" href="{{ url_for('invoices.sad_export', batch_id=batch_id) }}">Draft SAD (WinSAD)</a>
    </p>
```

Linia 37 (thead) — dodaj `<th>Typ</th>` po `<th class="py-1">Plik</th>`:

```html
      <thead><tr class="text-left border-b"><th class="py-1">Plik</th><th>Typ</th><th>Dostawca</th><th>Status</th><th>Nr faktury</th><th></th></tr></thead>
```

W wierszu joba — po `<td class="py-1">{{ j.filename }}</td>` (L41) dodaj:

```html
          <td>{{ j.doc_kind or 'invoice' }}{% if j.page_from %} (s.{{ j.page_from }}–{{ j.page_to }}){% endif %}</td>
```

Uwaga: gating draftu jest po stronie serwera (409 z komunikatem „Zatwierdzono X/Y") —
link zawsze widoczny, klik przy niepełnej partii pokazuje komunikat. Świadome
uproszczenie zamiast disabled-state w JS.

- [ ] **Step 6: Run tests + compileall**

Run: `python -m compileall invoice_routes.py doc_splitter.py sad_draft_export.py -q; python -m pytest tests/test_invoice_routes_sad_smoke.py tests/ -q`
Expected: PASS, brak regresji.

- [ ] **Step 7: Commit**

```bash
git add invoice_routes.py templates/invoice_coverage.html tests/test_invoice_routes_sad_smoke.py
git commit -m "feat(faktury): upload przez splitter + trasa Draft SAD + typ dokumentu w coverage"
```

---

### Task 7: QA-runner + pełna regresja + QA na realnej próbce

**Files:**
- Create: `tools/qa_invoice_pdfs.py`

**Interfaces:**
- Consumes: `doc_splitter.split_pdf`, `packing_list_extractor.first_pages_text`, `supplier_profiles.detect_supplier/get_supplier`, opcjonalnie `invoice_extractor.extract_invoice`.
- Produces: raport tekstowy na stdout (per plik: części, typ, strony, dostawca, profil, liczba pozycji).

- [ ] **Step 1: Napisz QA-runner**

```python
# tools/qa_invoice_pdfs.py
"""QA-runner: przepuszcza katalog realnych PDF-ów przez splitter i raportuje
strukturę oraz gotowość pipeline'u (dostawca, profil, pozycje).

Ręczne narzędzie diagnostyczne — wymaga PyMuPDF/pdfplumber; NIE w CI.
Realne PDF-y trzymać POZA repo (dane handlowe). Wycięte pliki _docN_*.pdf
powstają obok źródeł — katalog roboczy, nie oryginalny share.

Użycie: python tools/qa_invoice_pdfs.py <katalog_z_pdf> [--parse]
  --parse: dodatkowo pełna ekstrakcja pozycji (wymaga profili dostawców w DB).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from constants import DocKind                       # noqa: E402
from doc_splitter import split_pdf                  # noqa: E402
from packing_list_extractor import first_pages_text  # noqa: E402
from supplier_profiles import detect_supplier, get_supplier  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    folder = sys.argv[1]
    do_parse = "--parse" in sys.argv
    pdfs = sorted(f for f in os.listdir(folder)
                  if f.lower().endswith(".pdf") and "_doc" not in f)
    ok = bad = 0
    for fn in pdfs:
        path = os.path.join(folder, fn)
        print(f"\n=== {fn} ===")
        try:
            parts = split_pdf(path)
        except Exception as e:
            print(f"  BŁĄD SPLITTERA: {e}")
            bad += 1
            continue
        ok += 1
        for p in parts:
            text = first_pages_text(p["out_path"], 1)
            sup = detect_supplier(text) if text else None
            code = (sup or {}).get("code", "")
            profile = "profil OK" if (code and get_supplier(code)) else "BRAK PROFILU"
            line = (f"  s.{p['page_from']}-{p['page_to']:<3} {p['kind'].value:<13} "
                    f"dostawca={code or '?':<14} {profile}")
            if do_parse and p["kind"] in (DocKind.INVOICE, DocKind.PROFORMA) and code:
                try:
                    from invoice_extractor import extract_invoice
                    out = extract_invoice(p["out_path"], get_supplier(code))
                    line += f"  pozycji={len(out['items'])} nr={out['invoice_number']}"
                except Exception as e:
                    line += f"  EKSTRAKCJA PADŁA: {e}"
            print(line)
    print(f"\nPliki: {ok} OK, {bad} z błędem splittera.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Pełna regresja**

Run: `python -m compileall . -q; python -m pytest tests/ -q`
Expected: compileall czysty; cała suita zielona.

- [ ] **Step 3: QA na realnej próbce (poza repo)**

Skopiuj 6 plików próbki do katalogu roboczego (żeby wycinki nie zaśmieciły źródła), np.:

```bash
mkdir -p /c/Users/tomas/qa-cipl && cp /c/Users/tomas/.claude/uploads/bc9ff44a-aa72-42b2-acad-75242dcd2026/*.pdf /c/Users/tomas/qa-cipl/
python tools/qa_invoice_pdfs.py C:/Users/tomas/qa-cipl
```

Oczekiwania (ze specu):
- `..._4500000101-0001_CIPL.pdf` → 3 części: invoice / packing_list / proforma
- `...CI-E0001-S.pdf` → 1 część: proforma (bez kopii pliku)
- `..._4500000102-1___CIPL.pdf` → 2 części: invoice / packing_list
- `...ABCU1234567_ETD...pdf` → 19 części: 2×other (B/L + skan) + 17×CI/PL/PI
- `...clearance_docs_for_E0001.pdf` → 2 części: invoice / packing_list
- `...SAD_1000001.pdf` → wzorzec (klasyfikacja dowolna — nie jest wejściem produkcyjnym)
- Dostawcy bez profili → `BRAK PROFILU` (praca operatorska w wizardzie, nie kod).

Rozbieżności klasyfikacji → popraw markery w `doc_splitter._MARKERS` (+ przypadek do
`tests/test_doc_splitter.py`), aż realna próbka klasyfikuje się zgodnie z oczekiwaniami.

- [ ] **Step 4: Commit + raport**

```bash
git add tools/qa_invoice_pdfs.py
git commit -m "feat(faktury): QA-runner zestawów CIPL (splitter + gotowość pipeline)"
```

Pokaż userowi wynik QA (tabela per plik) — DoD wymaga pokazania wyników przed „gotowe".

---

## Self-Review

**Spec coverage:**
- Splitter stron, markery z literówkami, kontynuacje, skan→other → Task 2. ✔
- Pipeline bez zmian merytorycznych; proforma jak faktura z `doc_kind` → Task 1 (schemat) + Task 6 (upload nie rozróżnia przetwarzania CI/PI). ✔
- Kartony z PL (rola w kaskadzie, weight_map, mapper) → Task 3. ✔
- PL→faktura po numerze faktury + fallback batch → Task 4. ✔
- Kontener + warunki dostawy z nagłówka CI → Task 4 (`_header_meta`). ✔
- Draft SAD: agregacja eksporter×CN, próbki „WRAZ Z PRÓBKAMI", BRAK CN, przelicznik SZT, Kraj poch. v1=CN, kod dod. z `customs_code` → Task 5. ✔
- Gating „Zatwierdzono X/Y" → Task 5 (`collect_batch`) + Task 6 (409). ✔
- Ostrzeżenie o różnych kontenerach → Task 5 (`container_warning` w Nagłówku). ✔
- Coverage: typ dokumentu + strony, link Draft SAD → Task 6. ✔
- QA-runner + oczekiwania per plik próbki → Task 7. ✔
- Model danych (add_column, idempotencja) → Task 1. ✔

**Placeholder scan:** brak TBD/TODO; każdy krok kodowy ma pełny kod; komendy z oczekiwanym wynikiem.

**Type consistency:** `DocKind` (str Enum) porównywalny ze stringami z DB — użycia w `list_packing_lists` (SQL po wartości), `aggregate_positions`, `upload`, `qa_invoice_pdfs` spójne. `create_job(..., doc_kind, source_file, page_from, page_to)` zgodne między Task 1/2/6. `extract_weight_map` zwraca też `cartons` — konsumenci: `_fill_weight` (Task 3), `_weight_map_for` (Task 4). `SAD_COLUMNS`/pola wierszy zgodne między `aggregate_positions`, `build_sad_workbook` i testami (SENT = kolumna 13). `_XLSX_MIME` zdefiniowany w Task 5, użyty w Task 6.

## Poza zakresem (YAGNI — ze specu)
Import XML do WinSAD; OCR klasyfikacji skanów; adres eksportera; kursy walut/PLN; automatyczny kraj pochodzenia z treści.
