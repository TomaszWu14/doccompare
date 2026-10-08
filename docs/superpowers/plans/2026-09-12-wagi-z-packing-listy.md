# Wagi z packing listy — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wypełniać kolumny Waga netto/brutto w Excelu danymi z packing listy (osobny plik lub sekcja tego samego PDF), dopasowanymi do pozycji faktury po REF.

**Architecture:** `table_extractor.identify_columns` uczy się rozpoznawać kolumny wagi (dodatkowo). Nowy `packing_list_extractor.py` buduje z PL mapę `REF→{weight_net, weight_gross}` (per batch). `invoice_mapper.map_items` wzbogaca pozycje faktury wagą z tej mapy po REF (alias X/X1). `invoice_pipeline`/`invoice_routes` klasyfikują pliki batcha i budują mapę wag.

**Tech Stack:** Python 3.11, istniejąca kaskada `table_extractor`, `db.get_db()`, pytest.

## Global Constraints

- SQL wyłącznie przez `db.get_db()`; parametryzowane.
- Liczby: `normalizer.normalize_number()`, nigdy surowy `float()`.
- **Ciężkie importy lazy (function-local)** — `table_extractor` ciągnie `pdfplumber`; CI instaluje minimalny zestaw (`pytest python-dateutil dateparser babel flask werkzeug`, BEZ pdfplumber/openpyxl). Import `table_extractor` MUSI być w funkcji, nie na górze modułu (inaczej conftest/`import app` pada w CI). Test wymagający `openpyxl` → `pytest.importorskip("openpyxl")`.
- Testy w `tests/`, czysta logika, bez realnego PDF (monkeypatch). DoD: `python -m compileall .` czysty + `python -m pytest tests/ -q` zielony.
- **Zmiana `table_extractor` musi być ADDYTYWNA** — istniejące typy kolumn (ref/qty/price/net/lot/desc/unit/no/exp) i porównywarka nietknięte; regresję potwierdzają istniejące testy.
- Waga to dane pomocnicze — jej brak NIGDY nie blokuje zatwierdzenia (inaczej niż `ambiguous`).

**Zweryfikowane interfejsy (istniejący kod):**
- `table_extractor.identify_columns(header_row) -> dict[str,int]` — lista reguł `(col_type, [synonimy], priorytet)`; scoring `len(kw)*priorytet` (exact ×2). Zwraca `{col_type: idx}`.
- `table_extractor._rows_to_items(raw_rows, supplier_column_map=None) -> (items, total_net, total_qty)`; item dict: `{"ref","desc","qty","price","net","lot","unit"}`; wartości przez `get_cell(row, ci, ctype)`.
- `table_extractor.parse_pdf(path, supplier_column_map=None) -> DocData` (.items, .total_net, .total_qty, .header_fields, .raw_text).
- `material_master.normalize_ref(raw) -> str`.
- `invoice_mapper.map_items(db, raw_items) -> list` — obecnie bez wag; `_enrich`/`_blank` ustawiają pola master.
- `invoice_extractor.extract_invoice(pdf_path, supplier) -> dict` — item ma `weight_net`/`weight_gross` = `""` (hardwired, do zmiany).
- `invoice_pipeline.process_job(db, job_id) -> str`; woła `map_items(db, extracted["items"])`.
- `invoice_jobs`: tabela `invoice_jobs` (status ∈ uploaded/extracted/confirmed/exported/error) + `invoice_items`.
- `invoice_routes.upload` — zapisuje pliki, tworzy job per plik, odpala `_process_async`.

---

### Task 1: `table_extractor` — rozpoznawanie kolumn wagi (addytywne)

**Files:**
- Modify: `table_extractor.py` (dodaj reguły w `identify_columns`; emituj pola w `_rows_to_items`)
- Test: `tests/test_table_extractor_weight.py`

**Interfaces:**
- Produces: `identify_columns` dodatkowo zwraca `weight_net`/`weight_gross` gdy nagłówki pasują; `_rows_to_items` item dict zyskuje klucze `weight_net`, `weight_gross`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_table_extractor_weight.py
from table_extractor import identify_columns, _rows_to_items

def test_identify_weight_columns():
    ci = identify_columns(["REF", "Qty", "Net Weight", "Gross Weight"])
    assert ci.get("weight_net") == 2
    assert ci.get("weight_gross") == 3

def test_identify_weight_polish_abbrev():
    ci = identify_columns(["Kod", "Waga netto (kg)", "Waga brutto (kg)"])
    assert ci.get("weight_net") == 1
    assert ci.get("weight_gross") == 2

def test_weight_does_not_hijack_net_amount():
    # "Net value" to kwota (net), nie waga — nie może stać się weight_net
    ci = identify_columns(["REF", "Qty", "Net value"])
    assert ci.get("net") == 2
    assert "weight_net" not in ci

def test_rows_to_items_emits_weights():
    rows = [["REF", "Qty(pcs)", "N.W.", "G.W."],
            ["NL753", "100", "12.5", "13.2"]]
    items, _, _ = _rows_to_items(rows)
    assert items and items[0]["ref"] == "NL753"
    assert items[0]["weight_net"] == "12.5"
    assert items[0]["weight_gross"] == "13.2"

def test_existing_columns_unaffected():
    ci = identify_columns(["REF", "Qty", "Unit price (USD)", "Amount (USD)"])
    assert "ref" in ci and "qty" in ci and "price" in ci and "net" in ci
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_table_extractor_weight.py -v`
Expected: FAIL (weight_net/weight_gross not in ci)

- [ ] **Step 3: Add weight rules to `identify_columns`**

W `table_extractor.py`, w liście `rules` (po regule `"exp"`, przed zamykającym `]`), dodaj DWIE reguły. Priorytet 9 i wieloczłonowe słowa kluczowe, by wygrywały z `net`/`amount` przez długość:

```python
        ("weight_gross", [
            "gross weight", "gross wt", "g.w.", "g/w", "gw (kg)",
            "waga brutto", "brutto (kg)", "waga brutto (kg)",
        ], 9),
        ("weight_net", [
            "net weight", "nett weight", "net wt", "n.w.", "n/w", "nw (kg)",
            "waga netto", "netto (kg)", "waga netto (kg)",
        ], 9),
```

Uwaga: `weight_gross` PRZED `weight_net`, i oba używają wieloczłonowych fraz — słowo „net weight" nie zawiera żadnego słowa kluczowego typu `net` (`net value`/`net amount`/`netto`/`amount`), więc nie ma kolizji. „Net value" (kwota) nie zawiera „net weight" → pozostaje `net`.

- [ ] **Step 4: Emit weights in `_rows_to_items`**

W `_rows_to_items`, w słowniku `all_items.append({...})`, dodaj dwa pola (obok istniejących):

```python
            "weight_net": get_cell(row, ci, "weight_net"),
            "weight_gross": get_cell(row, ci, "weight_gross"),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_table_extractor_weight.py -v`
Expected: PASS (5 testów)

- [ ] **Step 6: Regression — existing extractor/comparator tests**

Run: `python -m pytest tests/ -q -k "table_extractor or comparator or extract or business"`
Expected: PASS (dodanie typów kolumn nie zepsuło istniejącej identyfikacji ani porównywarki)

- [ ] **Step 7: Commit**

```bash
git add table_extractor.py tests/test_table_extractor_weight.py
git commit -m "feat(faktury): table_extractor rozpoznaje kolumny wagi netto/brutto (addytywnie)"
```

---

### Task 2: `packing_list_extractor.py` — klasyfikacja PL + mapa wag

**Files:**
- Create: `packing_list_extractor.py`
- Test: `tests/test_packing_list_extractor.py`

**Interfaces:**
- Consumes: `table_extractor.parse_pdf` (lazy import), `material_master.normalize_ref`.
- Produces:
  - `is_packing_list(header_fields: dict, raw_text: str) -> bool` — True gdy tekst zawiera „packing list"/„lista pakowania" LUB (implikowane przez obecność wag — sprawdzane przez wołającego). W v1: dopasowanie po `raw_text`.
  - `extract_weight_map(pdf_path: str) -> dict` — zwraca `{ref_norm: {"weight_net": str, "weight_gross": str}}`; sumuje przy powtórzonym `ref_norm`. Puste pozycje wagi pomija. Import `parse_pdf` lazy (function-local).

Uzasadnienie: `parse_pdf` zwraca item z (po Tasku 1) `weight_net`/`weight_gross`. `extract_weight_map` filtruje pozycje mające jakąkolwiek wagę, normalizuje REF, sumuje liczby przez `normalizer.normalize_number`.

- [ ] **Step 1: Write the failing test** (monkeypatch parse_pdf)

```python
# tests/test_packing_list_extractor.py
import packing_list_extractor as pl

class _Doc:
    def __init__(self, items, raw_text=""):
        self.items = items; self.raw_text = raw_text
        self.total_net = None; self.total_qty = None; self.header_fields = {}

def test_is_packing_list_by_text():
    assert pl.is_packing_list({}, "PACKING LIST No. 5") is True
    assert pl.is_packing_list({}, "Commercial Invoice") is False

def test_extract_weight_map_per_ref(monkeypatch):
    items = [
        {"ref": "NL753-S-40", "weight_net": "12.5", "weight_gross": "13.2"},
        {"ref": "NL753-M-40", "weight_net": "20", "weight_gross": "21"},
    ]
    monkeypatch.setattr(pl, "_parse_pdf", lambda p: _Doc(items))
    m = pl.extract_weight_map("uploads/pl.pdf")
    import material_master as mm
    assert m[mm.normalize_ref("NL753-S-40")] == {"weight_net": "12.5", "weight_gross": "13.2"}
    assert mm.normalize_ref("NL753-M-40") in m

def test_extract_weight_map_sums_repeated_ref(monkeypatch):
    items = [
        {"ref": "X", "weight_net": "10", "weight_gross": "11"},
        {"ref": "X", "weight_net": "5", "weight_gross": "6"},
    ]
    monkeypatch.setattr(pl, "_parse_pdf", lambda p: _Doc(items))
    import material_master as mm
    m = pl.extract_weight_map("uploads/pl.pdf")
    assert m[mm.normalize_ref("X")]["weight_net"] == "15.0"
    assert m[mm.normalize_ref("X")]["weight_gross"] == "17.0"

def test_empty_map_when_no_weights(monkeypatch):
    items = [{"ref": "X", "weight_net": None, "weight_gross": None}]
    monkeypatch.setattr(pl, "_parse_pdf", lambda p: _Doc(items))
    assert pl.extract_weight_map("uploads/pl.pdf") == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_packing_list_extractor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'packing_list_extractor'`

- [ ] **Step 3: Write minimal implementation**

```python
# packing_list_extractor.py
"""Packing lista → mapa REF→{weight_net, weight_gross}. Klasyfikacja PL po treści.
Import table_extractor lazy (ciągnie pdfplumber) — konwencja repo."""

import material_master as mm
from normalizer import normalize_number

_PL_MARKERS = ("packing list", "lista pakowania", "packing note", "specyfikacja pakowania")


def is_packing_list(header_fields: dict, raw_text: str) -> bool:
    t = (raw_text or "").lower()
    return any(m in t for m in _PL_MARKERS)


def _parse_pdf(pdf_path: str):
    # Szew testowalny + lazy import (pdfplumber ciężki, CI bez niego).
    from table_extractor import parse_pdf
    return parse_pdf(pdf_path)


def _add(acc, key, field, raw):
    v = normalize_number(str(raw)) if raw not in (None, "") else None
    if v is None:
        return
    acc[key][field] = str((float(acc[key].get(field) or 0)) + float(v))


def extract_weight_map(pdf_path: str) -> dict:
    doc = _parse_pdf(pdf_path)
    out = {}
    for it in (doc.items or []):
        ref = mm.normalize_ref(it.get("ref"))
        if not ref:
            continue
        wn, wg = it.get("weight_net"), it.get("weight_gross")
        if (wn in (None, "")) and (wg in (None, "")):
            continue
        out.setdefault(ref, {})
        _add(out, ref, "weight_net", wn)
        _add(out, ref, "weight_gross", wg)
    # usuń wpisy które mimo wszystko puste
    return {k: v for k, v in out.items() if v}
```

Uwaga: `_add` sumuje jako float i zapisuje string (spójnie z resztą modułu trzymającego wartości jako str). `12.5`→„12.5"; przy pojedynczej wartości wynik „12.5" (float("0")+12.5=12.5 → „12.5"). W teście `test_extract_weight_map_per_ref` oczekiwane „12.5"/„13.2" — `str(0.0+12.5)`=="12.5" ✓.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_packing_list_extractor.py -v`
Expected: PASS (4 testy)

- [ ] **Step 5: Commit**

```bash
git add packing_list_extractor.py tests/test_packing_list_extractor.py
git commit -m "feat(faktury): packing_list_extractor — klasyfikacja PL + mapa REF→waga"
```

---

### Task 3: `invoice_mapper` — wzbogacenie wagą z mapy PL

**Files:**
- Modify: `invoice_mapper.py`
- Modify: `invoice_extractor.py` (przepuść wagę z faktury jeśli obecna zamiast hardwired "")
- Test: `tests/test_invoice_mapper_weight.py`

**Interfaces:**
- Produces:
  - `invoice_mapper.map_items(db, raw_items, weight_map=None) -> list` — po dopasowaniu REF, jeśli pozycja nie ma wagi (`weight_net`/`weight_gross` puste) i `weight_map` zawiera REF (dokł. lub alias sufiks-cyfrowy przez `normalize_ref`), wypełnia je i ustawia `weight_source="pl"`. Brak → `weight_source="brak"`, wagi puste. Sygnatura z domyślnym `weight_map=None` (istniejący wołający `map_items(db, items)` działa bez zmian).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_invoice_mapper_weight.py
import sqlite3, material_master as mm, uom, invoice_mapper as im

def _db():
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    mm.ensure_table(db); uom.ensure_table(db); return db

def test_weight_filled_from_map_exact():
    db = _db()
    wm = {mm.normalize_ref("NL753-S-40"): {"weight_net": "12.5", "weight_gross": "13.2"}}
    out = im.map_items(db, [{"line_no": 1, "raw_ref": "NL753-S-40", "weight_net": "", "weight_gross": ""}], weight_map=wm)
    r = out[0]
    assert r["weight_net"] == "12.5" and r["weight_gross"] == "13.2"
    assert r["weight_source"] == "pl"

def test_weight_alias_suffix():
    db = _db()
    wm = {mm.normalize_ref("X1"): {"weight_net": "9", "weight_gross": "10"}}
    out = im.map_items(db, [{"line_no": 1, "raw_ref": "X1", "weight_net": "", "weight_gross": ""}], weight_map=wm)
    assert out[0]["weight_net"] == "9"

def test_weight_missing_flagged():
    db = _db()
    out = im.map_items(db, [{"line_no": 1, "raw_ref": "ZZZ", "weight_net": "", "weight_gross": ""}], weight_map={})
    assert out[0]["weight_net"] == "" and out[0]["weight_source"] == "brak"

def test_no_weight_map_backwards_compatible():
    db = _db()
    out = im.map_items(db, [{"line_no": 1, "raw_ref": "X", "weight_net": "", "weight_gross": ""}])
    assert out[0]["weight_source"] == "brak"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_invoice_mapper_weight.py -v`
Expected: FAIL (map_items nie przyjmuje weight_map / brak weight_source)

- [ ] **Step 3: Implement in `invoice_mapper.py`**

Dodaj helper i rozszerz `map_items`:

```python
def _fill_weight(item, weight_map):
    if not weight_map:
        item["weight_source"] = "brak"
        return
    ref = mm.normalize_ref(item.get("raw_ref"))
    entry = weight_map.get(ref)
    if entry is None and ref:
        # alias sufiks-cyfrowy: klucz mapy zaczyna się od ref + cyfry
        cands = [k for k in weight_map
                 if k.startswith(ref) and (k[len(ref):] == "" or k[len(ref):].isdigit())]
        if len(cands) == 1:
            entry = weight_map[cands[0]]
    if entry and (not item.get("weight_net")) and (not item.get("weight_gross")):
        item["weight_net"] = entry.get("weight_net", "") or item.get("weight_net", "")
        item["weight_gross"] = entry.get("weight_gross", "") or item.get("weight_gross", "")
        item["weight_source"] = "pl"
    else:
        item["weight_source"] = item.get("weight_source") or "brak"
```

W `map_items` zmień sygnaturę na `def map_items(db, raw_items, weight_map=None):` i po ustaleniu `match_status`/wzbogaceniu master, PRZED `out.append(item)` wywołaj `_fill_weight(item, weight_map)`.

- [ ] **Step 4: Pass-through invoice weights in `invoice_extractor.py`**

W `extract_invoice`, zamień hardwired puste wagi na przepuszczenie z faktury (gdy tabela faktury je miała po Tasku 1):

```python
            "weight_net": raw.get("weight_net") or "",
            "weight_gross": raw.get("weight_gross") or "",
```
(zastępuje obecne `"weight_net": "", "weight_gross": "",`). Waga z faktury (gdy jest) ma pierwszeństwo; PL uzupełnia puste.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_invoice_mapper_weight.py tests/test_invoice_mapper.py tests/test_invoice_extractor.py -v`
Expected: PASS (nowe 4 + istniejące dalej zielone)

- [ ] **Step 6: Commit**

```bash
git add invoice_mapper.py invoice_extractor.py tests/test_invoice_mapper_weight.py
git commit -m "feat(faktury): map_items wzbogaca wagę z mapy PL po REF (alias X/X1) + flaga weight_source"
```

---

### Task 4: Excel — wypełnianie kolumn wagi (weryfikacja kontraktu)

**Files:**
- Test: `tests/test_invoice_excel_weight.py`

**Interfaces:**
- Consumes: istniejący `invoice_excel_export.build_workbook` (kolumny Waga netto/brutto już są w `COLUMNS`, mapują `weight_net`/`weight_gross`). Ten task tylko potwierdza, że wagi z mapera trafiają do Excela — bez zmian kodu, chyba że test wykryje lukę.

- [ ] **Step 1: Write the test**

```python
# tests/test_invoice_excel_weight.py
import pytest
pytest.importorskip("openpyxl")
import invoice_excel_export as ex

def test_weights_written_to_excel():
    rows = [{"invoice_number": "FV/1", "qty": "10", "master_ref": "X", "name_pl": "Rękawice",
             "weight_net": "12.5", "weight_gross": "13.2", "uom_factor": "", "amount": "50",
             "tariff_cn": "4015", "sent": 1, "match_status": "matched", "skipped": 0}]
    wb = ex.build_workbook(rows)
    ws = wb.active
    header = [c.value for c in ws[1]]
    ni, gi = header.index("Waga netto"), header.index("Waga brutto")
    r2 = [c.value for c in ws[2]]
    assert r2[ni] == "12.5" and r2[gi] == "13.2"
```

- [ ] **Step 2: Run test**

Run: `python -m pytest tests/test_invoice_excel_weight.py -v`
Expected: PASS (kolumny wagi już w kontrakcie; jeśli FAIL — napraw `_row_values` mapowanie weight_net/gross w `invoice_excel_export.py`)

- [ ] **Step 3: Commit**

```bash
git add tests/test_invoice_excel_weight.py
git commit -m "test(faktury): Excel wypełnia kolumny Waga netto/brutto z pozycji"
```

---

### Task 5: Orkiestracja — klasyfikacja plików batcha i budowa mapy wag

**Files:**
- Modify: `invoice_pipeline.py` (`process_job` buduje weight_map i przekazuje do map_items)
- Modify: `invoice_routes.py` (`upload` klasyfikuje pliki; PL → job ze statusem `packing_list`)
- Modify: `invoice_jobs.py` (helper `list_packing_lists(db, batch_id)`)
- Test: `tests/test_invoice_pipeline_weight.py`

**Interfaces:**
- `invoice_jobs.list_packing_lists(db, batch_id) -> list[dict]` — joby batcha ze `status='packing_list'` (zwraca ich `pdf_path`).
- `invoice_pipeline.process_job` — po ekstrakcji faktury buduje `weight_map`:
  1. z własnego PDF faktury (sekcje PL w tym samym pliku): `packing_list_extractor.extract_weight_map(job["pdf_path"])`,
  2. z każdego PL-joba w batchu: `extract_weight_map(pl["pdf_path"])`,
  scala (dict.update; sekcje własne jako baza), przekazuje do `map_items(db, items, weight_map)`.
- `invoice_routes.upload` — dla każdego pliku, przed utworzeniem joba, sklasyfikuj: wczytaj pierwsze strony (lazy `pdfplumber`), jeśli `packing_list_extractor.is_packing_list(text)` → utwórz job ze `status='packing_list'` (bez `_process_async`); inaczej job faktury + `_process_async`.

Uzasadnienie: PL nie przechodzi pipeline faktury (nie ma profilu, nie generuje pozycji); służy tylko jako źródło wag. `weight_map` liczymy w `process_job`, bo wtedy wszystkie pliki batcha są już na dysku.

- [ ] **Step 1: Write the failing test** (monkeypatch extract_invoice + extract_weight_map)

```python
# tests/test_invoice_pipeline_weight.py
import sqlite3, invoice_jobs as ij, invoice_pipeline as ip

def _db():
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    import material_master as mm, uom
    mm.ensure_table(db); uom.ensure_table(db); ij.ensure_invoice_tables(db)
    db.execute("INSERT INTO material_master (ref_code,ref_norm,opis_pl) VALUES ('NL753','NL753','Rękawice')")
    db.commit(); return db

def test_process_job_merges_pl_weights(monkeypatch):
    db = _db()
    # PL job w tym samym batchu
    plid = ij.create_job(db, "b", "pl.pdf", "uploads/pl.pdf", "")
    ij.update_job(db, plid, status="packing_list")
    jid = ij.create_job(db, "b", "inv.pdf", "uploads/inv.pdf", "SHIELDCO")
    monkeypatch.setattr(ip, "get_supplier", lambda code, **k: {"code": "SHIELDCO"})
    monkeypatch.setattr(ip, "extract_invoice", lambda path, sup: {
        "items": [{"line_no": 1, "raw_ref": "NL753", "uom_src": "PCS", "qty": "100",
                   "weight_net": "", "weight_gross": ""}],
        "total_net": "1", "total_qty": "100", "raw_text": "", "invoice_number": "FV/1"})
    import material_master as mm
    def fake_map(path):
        if "pl.pdf" in path:
            return {mm.normalize_ref("NL753"): {"weight_net": "12.5", "weight_gross": "13.2"}}
        return {}
    monkeypatch.setattr(ip, "extract_weight_map", fake_map)
    status = ip.process_job(db, jid)
    assert status == "extracted"
    it = ij.get_items(db, jid)[0]
    assert it["weight_net"] == "12.5" and it["weight_gross"] == "13.2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_invoice_pipeline_weight.py -v`
Expected: FAIL (process_job nie buduje weight_map / brak extract_weight_map w module)

- [ ] **Step 3: Add `list_packing_lists` to invoice_jobs.py**

```python
def list_packing_lists(db, batch_id) -> list:
    rows = db.execute(
        "SELECT * FROM invoice_jobs WHERE batch_id=? AND status='packing_list' ORDER BY id",
        (batch_id,)).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Wire weight_map into `process_job`**

W `invoice_pipeline.py` dodaj import na górze: `from packing_list_extractor import extract_weight_map` — UWAGA: `packing_list_extractor` importuje `material_master`/`normalizer` (lekkie) i `table_extractor` lazy w środku, więc top-level import jest CI-safe. Następnie w `process_job`, po udanej ekstrakcji faktury i PRZED `map_items`:

```python
    # Mapa wag z packing list: sekcje własnego PDF + PL-e w batchu.
    weight_map = {}
    try:
        weight_map.update(extract_weight_map(job["pdf_path"]))
        for pl in ij.list_packing_lists(db, job.get("batch_id") or ""):
            weight_map.update(extract_weight_map(pl["pdf_path"]))
    except Exception:
        weight_map = {}   # waga to dane pomocnicze — błąd nie wywraca faktury
    mapped = map_items(db, extracted["items"], weight_map=weight_map)
```
(zastępuje istniejące `mapped = map_items(db, extracted["items"])`).

- [ ] **Step 5: Classify uploads in `invoice_routes.py`**

W `upload`, dla każdego zapisanego pliku, przed utworzeniem joba faktury, sklasyfikuj (lazy import):

```python
        # Klasyfikacja: packing lista vs faktura (po treści).
        text = ""
        try:
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                text = " ".join((pg.extract_text() or "") for pg in pdf.pages[:2])
        except Exception:
            text = ""
        from packing_list_extractor import is_packing_list
        if is_packing_list({}, text):
            plid = ij.create_job(db, batch_id, fn, path, "")
            ij.update_job(db, plid, status="packing_list")
            continue   # PL nie przechodzi pipeline faktury
        code = forced_supplier
        jid = ij.create_job(db, batch_id, fn, path, code)
        threading.Thread(target=_process_async, args=(jid,), daemon=True).start()
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_invoice_pipeline_weight.py tests/test_invoice_pipeline.py -v` and `python -m compileall invoice_pipeline.py invoice_routes.py invoice_jobs.py packing_list_extractor.py -q`
Expected: PASS; compileall clean.

- [ ] **Step 7: Commit**

```bash
git add invoice_pipeline.py invoice_routes.py invoice_jobs.py tests/test_invoice_pipeline_weight.py
git commit -m "feat(faktury): klasyfikacja PL w batchu + budowa mapy wag w process_job"
```

---

## Self-Review

**Spec coverage:**
- Rozpoznawanie kolumn wagi (współdzielony extractor, addytywnie) → Task 1. ✔
- PL osobny plik LUB sekcja tego samego PDF → Task 2 (`extract_weight_map` na dowolnym pdf_path) + Task 5 (własny PDF + PL-e batcha). ✔
- Dopasowanie po REF + alias X/X1 → Task 3 (`_fill_weight`). ✔
- Per pozycja + sumowanie powtórzonego REF → Task 2 (`_add`). ✔
- Auto-detekcja PL z treści → Task 2 (`is_packing_list`) + Task 5 (upload). ✔
- Batch = jedna przesyłka, mapa REF→waga → Task 5 (`process_job` scala). ✔
- Waga nie blokuje (dane pomocnicze) → Task 3/5 (brak → flaga, `try/except` w process_job). ✔
- Excel wypełnia wagę → Task 4. ✔
- Lazy import table_extractor (CI) → Task 2 (`_parse_pdf`), Task 5 (pdfplumber lazy). ✔

**Placeholder scan:** brak TBD; każdy krok z realnym kodem i komendą.

**Type consistency:** `weight_net`/`weight_gross`/`weight_source` spójne: `_rows_to_items` (str/None) → `extract_invoice` (str) → `extract_weight_map` (str) → `_fill_weight` → `invoice_jobs._ITEM_COLS` (zawiera `weight_net`,`weight_gross`; **`weight_source` NIE jest kolumną** — to pole tymczasowe w dict, nie zapisywane; Excel go nie używa). `map_items(db, items, weight_map=None)` — domyślny arg zachowuje istniejących wołających.

## Poza zakresem (v1)
- Konwersja jednostek wagi (kg/g/lb).
- PL z samymi sumami zbiorczymi.
- Fallback wagi z master daty (levels_json).
- Sztywne parowanie dokumentów po numerze/pokryciu REF.
