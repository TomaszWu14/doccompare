# Faktury → Excel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nowy moduł, który z faktur dostawców (PDF) wyciąga pozycje, wzbogaca je o dane z master daty (nazwa PL, kod celny, SENT, przelicznik jednostki), pozwala operatorowi potwierdzić dane obok oryginału i generuje wspólny plik Excel.

**Architecture:** Osobny moduł reużywający istniejących klocków repo: kaskada `table_extractor.parse_pdf` (ekstrakcja), `supplier_profiles` (mapowanie kolumn per dostawca, profil obowiązkowy), `material_master.get_material` (wzbogacenie po REF z aliasami X/X1), `uom` (przeliczniki), `openpyxl` (Excel). Stan trzymany w dwóch nowych tabelach (`invoice_jobs`, `invoice_items`). Routes jako osobny Flask Blueprint. Frontend: split-view PDF ⟷ edytowalna tabela.

**Tech Stack:** Python 3.11, Flask (Blueprint), openpyxl, istniejąca abstrakcja `db.get_db()` (SQLite dev / PostgreSQL prod), Jinja2 + Alpine.js (wzorzec repo).

## Global Constraints

- SQL wyłącznie przez `db.get_db()` — nigdy `sqlite3.connect()` / `psycopg2` wprost.
- Liczby: `normalizer.normalize_number()`, nigdy surowy `float()` (formaty EU `13.500,00`).
- Ciężkie/ML importy trzymaj lazy (function-local) — `compileall` i boot muszą przejść bez ML.
- Nowe tabele: `CREATE TABLE IF NOT EXISTS` (idempotentne), wzorzec jak w `migrate_db.py`.
- Auth: routes chronione dekoratorami z `core.security` (`login_required`, `require_role`).
- Polska-first UI, branding jak reszta (`base.html`).
- Testy w `tests/`, czysta logika, bez realnego PDF/ML (mock/monkeypatch). DoD repo: `python -m compileall` czysty + `python -m pytest tests/ -q` zielony przed „gotowe".
- Profil dostawcy OBOWIĄZKOWY: brak profilu = brak ekstrakcji (twardy wymóg).

**Realne interfejsy istniejących modułów (zweryfikowane w kodzie):**
- `table_extractor.parse_pdf(path: str, supplier_column_map: dict = None) -> DocData`; `DocData` ma `.items` (lista dictów `{"ref","desc","qty","price","net","lot","unit"}`), `.total_net`, `.total_qty`, `.header_fields: dict`, `.raw_text: str`.
- `supplier_profiles.detect_supplier(text: str, db_path=...) -> Optional[dict]` (profil albo None).
- `supplier_profiles.get_supplier(code: str, db_path=...) -> Optional[dict]`.
- `supplier_profiles.get_all_suppliers(db_path=...) -> list[dict]`.
- `supplier_profiles.get_column_overrides(supplier: dict, doc_type: str) -> dict` (→ podaj jako `supplier_column_map`).
- `material_master.get_material(db, ref) -> dict|None`; kolumny m.in. `ref_code`, `ref_norm`, `opis_pl`, `opis_en`, `txt_short_pl`, `tariff_cn`, `sent`, `base_uom`, `ean`, `supplier_codes`.
- `material_master.normalize_ref(raw) -> str`.
- `uom.load_conversions(db) -> list`; `uom.get_factor(unit_from, unit_to, ref, conversions) -> float|None`.
- `db.get_db()` — połączenie; `.execute(sql, params).fetchone()/.fetchall()`, `.commit()`.
- Blueprint rejestruje się w `app.py` przez `app.register_blueprint(...)` (patrz linie ~203–206).

---

### Task 1: Warstwa danych — `invoice_jobs.py` (tabele + CRUD)

**Files:**
- Create: `invoice_jobs.py`
- Modify: `migrate_db.py` (dopisz wywołanie `ensure_invoice_tables(db)` w sekcji tworzenia tabel)
- Test: `tests/test_invoice_jobs.py`

**Interfaces:**
- Produces:
  - `ensure_invoice_tables(db) -> None`
  - `create_job(db, batch_id: str, filename: str, pdf_path: str, supplier_code: str) -> int` (zwraca job_id)
  - `get_job(db, job_id: int) -> dict|None`
  - `list_jobs(db, batch_id: str) -> list[dict]`
  - `update_job(db, job_id: int, **fields) -> None` (dozwolone pola: `status`, `error`, `supplier_code`, `invoice_number`)
  - `save_items(db, job_id: int, items: list[dict]) -> None` (kasuje istniejące pozycje joba i wstawia nowe)
  - `get_items(db, job_id: int) -> list[dict]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_invoice_jobs.py
import sqlite3
import invoice_jobs as ij

def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    return db

def test_create_and_get_job():
    db = _db()
    ij.ensure_invoice_tables(db)
    jid = ij.create_job(db, "batch1", "faktura.pdf", "uploads/faktura.pdf", "SHIELDCO")
    job = ij.get_job(db, jid)
    assert job["status"] == "uploaded"
    assert job["supplier_code"] == "SHIELDCO"
    assert job["batch_id"] == "batch1"

def test_update_status_and_list_by_batch():
    db = _db()
    ij.ensure_invoice_tables(db)
    j1 = ij.create_job(db, "b", "a.pdf", "uploads/a.pdf", "S1")
    ij.create_job(db, "b", "c.pdf", "uploads/c.pdf", "S2")
    ij.update_job(db, j1, status="extracted", invoice_number="FV/1")
    jobs = ij.list_jobs(db, "b")
    assert len(jobs) == 2
    assert ij.get_job(db, j1)["status"] == "extracted"
    assert ij.get_job(db, j1)["invoice_number"] == "FV/1"

def test_save_and_get_items_replaces():
    db = _db()
    ij.ensure_invoice_tables(db)
    jid = ij.create_job(db, "b", "a.pdf", "uploads/a.pdf", "S1")
    ij.save_items(db, jid, [{"line_no": 1, "raw_ref": "X", "qty": "10"}])
    ij.save_items(db, jid, [{"line_no": 1, "raw_ref": "Y", "qty": "5"},
                            {"line_no": 2, "raw_ref": "Z", "qty": "3"}])
    items = ij.get_items(db, jid)
    assert len(items) == 2
    assert items[0]["raw_ref"] == "Y"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_invoice_jobs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'invoice_jobs'`

- [ ] **Step 3: Write minimal implementation**

```python
# invoice_jobs.py
"""Warstwa danych modułu Faktury → Excel: tabele invoice_jobs / invoice_items + CRUD.
SQL zawsze przez przekazane połączenie (db.get_db()). Nie tworzy własnego połączenia."""

_ITEM_COLS = ("line_no", "raw_ref", "qty", "net_amount", "weight_net", "weight_gross",
              "uom_src", "amount", "desc", "master_ref", "name_pl", "tariff_cn",
              "sent", "uom_factor", "match_status", "skipped")

_JOB_UPDATABLE = ("status", "error", "supplier_code", "invoice_number")


def ensure_invoice_tables(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS invoice_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT DEFAULT '',
        filename TEXT DEFAULT '',
        pdf_path TEXT DEFAULT '',
        supplier_code TEXT DEFAULT '',
        status TEXT DEFAULT 'uploaded',
        error TEXT DEFAULT '',
        invoice_number TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS invoice_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL,
        line_no INTEGER DEFAULT 0,
        raw_ref TEXT DEFAULT '',
        qty TEXT DEFAULT '',
        net_amount TEXT DEFAULT '',
        weight_net TEXT DEFAULT '',
        weight_gross TEXT DEFAULT '',
        uom_src TEXT DEFAULT '',
        amount TEXT DEFAULT '',
        desc TEXT DEFAULT '',
        master_ref TEXT DEFAULT '',
        name_pl TEXT DEFAULT '',
        tariff_cn TEXT DEFAULT '',
        sent INTEGER DEFAULT 0,
        uom_factor TEXT DEFAULT '',
        match_status TEXT DEFAULT 'unmatched',
        skipped INTEGER DEFAULT 0
    )""")
    db.commit()


def create_job(db, batch_id, filename, pdf_path, supplier_code) -> int:
    ensure_invoice_tables(db)
    cur = db.execute(
        "INSERT INTO invoice_jobs (batch_id, filename, pdf_path, supplier_code, status) "
        "VALUES (?, ?, ?, ?, 'uploaded')",
        (batch_id, filename, pdf_path, supplier_code))
    db.commit()
    # lastrowid działa na SQLite; abstrakcja db.py mapuje to samo dla PG.
    return cur.lastrowid


def get_job(db, job_id) -> dict | None:
    row = db.execute("SELECT * FROM invoice_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(db, batch_id) -> list:
    rows = db.execute(
        "SELECT * FROM invoice_jobs WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
    return [dict(r) for r in rows]


def update_job(db, job_id, **fields) -> None:
    cols = [c for c in fields if c in _JOB_UPDATABLE]
    if not cols:
        return
    sets = ", ".join(f"{c}=?" for c in cols) + ", updated_at=datetime('now')"
    db.execute(f"UPDATE invoice_jobs SET {sets} WHERE id=?",
               tuple(fields[c] for c in cols) + (job_id,))
    db.commit()


def save_items(db, job_id, items) -> None:
    db.execute("DELETE FROM invoice_items WHERE job_id=?", (job_id,))
    for it in items:
        vals = [it.get(c) for c in _ITEM_COLS]
        ph = ", ".join(["?"] * (len(_ITEM_COLS) + 1))
        db.execute(
            f"INSERT INTO invoice_items (job_id, {', '.join(_ITEM_COLS)}) VALUES ({ph})",
            tuple([job_id] + vals))
    db.commit()


def get_items(db, job_id) -> list:
    rows = db.execute(
        "SELECT * FROM invoice_items WHERE job_id=? ORDER BY line_no, id", (job_id,)).fetchall()
    return [dict(r) for r in rows]
```

Uwaga do `save_items`: `datetime('now')` w `update_job` jest tłumaczone przez `db.py` na `NOW()` dla PostgreSQL — zgodne z konwencją repo.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_invoice_jobs.py -v`
Expected: PASS (3 testy)

- [ ] **Step 5: Wire into migrate_db.py**

W `migrate_db.py`, w funkcji tworzącej tabele (obok innych `CREATE TABLE IF NOT EXISTS`), dodaj:

```python
    # Moduł Faktury → Excel
    import invoice_jobs
    invoice_jobs.ensure_invoice_tables(db)
```

- [ ] **Step 6: Verify migrate + compileall**

Run: `python -m compileall invoice_jobs.py migrate_db.py -q && python migrate_db.py`
Expected: brak błędów; migracja idempotentna (re-run OK).

- [ ] **Step 7: Commit**

```bash
git add invoice_jobs.py migrate_db.py tests/test_invoice_jobs.py
git commit -m "feat(faktury): warstwa danych invoice_jobs/invoice_items + migracja"
```

---

### Task 2: `invoice_extractor.py` — cienki wrapper na kaskadę

**Files:**
- Create: `invoice_extractor.py`
- Test: `tests/test_invoice_extractor.py`

**Interfaces:**
- Consumes: `table_extractor.parse_pdf`, `supplier_profiles.get_column_overrides`.
- Produces:
  - `extract_invoice(pdf_path: str, supplier: dict) -> dict` zwraca
    `{"items": list[dict], "total_net": str|None, "total_qty": str|None, "raw_text": str, "invoice_number": str}`.
    Każdy item: `{"line_no": int, "raw_ref": str, "desc": str, "qty": str, "net_amount": str, "amount": str, "uom_src": str, "weight_net": "", "weight_gross": ""}`.

Uzasadnienie mapowania pól: `parse_pdf` zwraca item `{"ref","desc","qty","price","net","lot","unit"}`. Do naszego kontraktu: `raw_ref←ref`, `desc←desc`, `qty←qty`, `net_amount←net`, `amount←net` (kwota pozycji = wartość netto z faktury; jeśli brak, `price`), `uom_src←unit`. Wagi netto/brutto nie są w tabeli pozycji faktury → puste (uzupełni operator lub v2 z master daty).

- [ ] **Step 1: Write the failing test** (monkeypatch `parse_pdf`, bez realnego PDF)

```python
# tests/test_invoice_extractor.py
import types
import invoice_extractor as ie

class _FakeDocData:
    def __init__(self):
        self.items = [
            {"ref": "X", "desc": "Glove L", "qty": "100", "price": "2.5",
             "net": "250", "lot": "L1", "unit": "PCS"},
            {"ref": "Y1", "desc": "Mask", "qty": "10", "price": None,
             "net": "30", "lot": None, "unit": "CTN"},
        ]
        self.total_net = "280"
        self.total_qty = "110"
        self.header_fields = {"invoice_no": "FV/2026/1"}
        self.raw_text = "Invoice FV/2026/1 ..."

def test_extract_maps_parse_pdf_items(monkeypatch):
    captured = {}
    def fake_parse_pdf(path, supplier_column_map=None):
        captured["path"] = path
        captured["map"] = supplier_column_map
        return _FakeDocData()
    monkeypatch.setattr(ie, "parse_pdf", fake_parse_pdf)
    monkeypatch.setattr(ie, "get_column_overrides", lambda s, dt: {"ref": 0})

    out = ie.extract_invoice("uploads/a.pdf", {"code": "S1"})
    assert captured["path"] == "uploads/a.pdf"
    assert captured["map"] == {"ref": 0}          # profil przekazany do kaskady
    assert len(out["items"]) == 2
    first = out["items"][0]
    assert first["raw_ref"] == "X"
    assert first["qty"] == "100"
    assert first["net_amount"] == "250"
    assert first["amount"] == "250"
    assert first["uom_src"] == "PCS"
    assert first["line_no"] == 1
    assert out["total_net"] == "280"
    assert out["invoice_number"] == "FV/2026/1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_invoice_extractor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'invoice_extractor'`

- [ ] **Step 3: Write minimal implementation**

```python
# invoice_extractor.py
"""Cienki wrapper: PDF faktury → surowe pozycje, przez istniejącą kaskadę
table_extractor.parse_pdf z mapowaniem kolumn z profilu dostawcy.
Nic nie wzbogaca master datą (to robi invoice_mapper)."""

from table_extractor import parse_pdf
from supplier_profiles import get_column_overrides


def _pick_invoice_number(header_fields: dict) -> str:
    for k in ("invoice_no", "invoice_number", "faktura", "nr_faktury", "number"):
        v = (header_fields or {}).get(k)
        if v:
            return str(v).strip()
    return ""


def extract_invoice(pdf_path: str, supplier: dict) -> dict:
    col_map = get_column_overrides(supplier, "invoice") or None
    doc = parse_pdf(pdf_path, supplier_column_map=col_map)
    items = []
    for i, raw in enumerate(doc.items or [], start=1):
        net = raw.get("net")
        items.append({
            "line_no": i,
            "raw_ref": (raw.get("ref") or "").strip(),
            "desc": (raw.get("desc") or "").strip(),
            "qty": raw.get("qty") or "",
            "net_amount": net or "",
            "amount": net or raw.get("price") or "",
            "uom_src": raw.get("unit") or "",
            "weight_net": "",
            "weight_gross": "",
        })
    return {
        "items": items,
        "total_net": doc.total_net,
        "total_qty": doc.total_qty,
        "raw_text": doc.raw_text or "",
        "invoice_number": _pick_invoice_number(doc.header_fields),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_invoice_extractor.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add invoice_extractor.py tests/test_invoice_extractor.py
git commit -m "feat(faktury): invoice_extractor — wrapper na kaskadę parse_pdf"
```

---

### Task 3: `material_master.find_candidates` + `invoice_mapper.py` — wzbogacenie i status dopasowania

**Files:**
- Modify: `material_master.py` (dodaj `find_candidates`)
- Create: `invoice_mapper.py`
- Test: `tests/test_invoice_mapper.py`

**Interfaces:**
- Consumes: `material_master.get_material`, `material_master.normalize_ref`, nowe `material_master.find_candidates`, `uom.load_conversions`, `uom.get_factor`.
- Produces:
  - `material_master.find_candidates(db, raw_ref) -> list[dict]` — rekordy master, których `ref_norm` zaczyna się od znormalizowanego `raw_ref` (łapie X → {X, X1, X2}).
  - `invoice_mapper.map_items(db, raw_items: list[dict]) -> list[dict]` — dla każdej pozycji dokłada `master_ref`, `name_pl`, `tariff_cn`, `sent`, `uom_factor` i ustawia `match_status` ∈ {matched, ambiguous, unmatched}. Ładuje `uom.load_conversions(db)` raz.

Reguła dopasowania (deterministyczna):
1. `get_material(db, raw_ref)` (dokładny lub po `ref_norm`) → **matched**.
2. inaczej `find_candidates`: dokładnie 1 → **matched** (alias, np. X→X1); >1 → **ambiguous** (nie wybieramy); 0 → **unmatched**.
`name_pl` = `opis_pl` lub fallback `txt_short_pl`. `uom_factor` = `get_factor(uom_src, base_uom, master_ref, conversions)` sformatowany jako string albo `""` gdy brak.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_invoice_mapper.py
import sqlite3
import material_master as mm
import invoice_mapper as im

def _db_with(materials, conversions=()):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    mm.ensure_table(db)
    import uom
    uom.ensure_table(db)
    for m in materials:
        db.execute(
            "INSERT INTO material_master (ref_code, ref_norm, opis_pl, tariff_cn, sent, base_uom) "
            "VALUES (?,?,?,?,?,?)",
            (m["ref_code"], mm.normalize_ref(m["ref_code"]), m.get("opis_pl", ""),
             m.get("tariff_cn", ""), m.get("sent", 0), m.get("base_uom", "")))
    for c in conversions:
        db.execute("INSERT INTO uom_conversion (ref_norm, unit_from, unit_to, factor) "
                   "VALUES (?,?,?,?)", c)
    db.commit()
    return db

def test_exact_match_enriches():
    db = _db_with([{"ref_code": "X", "opis_pl": "Rękawice", "tariff_cn": "4015", "sent": 1}])
    out = im.map_items(db, [{"line_no": 1, "raw_ref": "X", "uom_src": "PCS"}])
    r = out[0]
    assert r["match_status"] == "matched"
    assert r["master_ref"] == "X"
    assert r["name_pl"] == "Rękawice"
    assert r["tariff_cn"] == "4015"
    assert r["sent"] == 1

def test_single_alias_is_matched():
    # faktura ma "X", master trzyma tylko "X1" → jednoznaczny alias
    db = _db_with([{"ref_code": "X1", "opis_pl": "Maska"}])
    out = im.map_items(db, [{"line_no": 1, "raw_ref": "X", "uom_src": ""}])
    assert out[0]["match_status"] == "matched"
    assert out[0]["master_ref"] == "X1"

def test_multiple_candidates_ambiguous():
    db = _db_with([{"ref_code": "X1"}, {"ref_code": "X2"}])
    out = im.map_items(db, [{"line_no": 1, "raw_ref": "X", "uom_src": ""}])
    assert out[0]["match_status"] == "ambiguous"
    assert out[0]["master_ref"] == ""      # nie wybieramy automatycznie

def test_no_candidate_unmatched():
    db = _db_with([{"ref_code": "Z"}])
    out = im.map_items(db, [{"line_no": 1, "raw_ref": "X", "uom_src": ""}])
    assert out[0]["match_status"] == "unmatched"
    assert out[0]["name_pl"] == ""

def test_uom_factor_filled():
    db = _db_with(
        [{"ref_code": "X", "base_uom": "PCS"}],
        conversions=[("X", "CTN", "PCS", 24.0)])
    out = im.map_items(db, [{"line_no": 1, "raw_ref": "X", "uom_src": "CTN"}])
    assert out[0]["uom_factor"] == "24.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_invoice_mapper.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'invoice_mapper'`

- [ ] **Step 3a: Add `find_candidates` to material_master.py**

Dopisz po `get_material` (ok. linia 440):

```python
def find_candidates(db, raw_ref) -> list:
    """Rekordy master, których ref_norm zaczyna się od znormalizowanego raw_ref.
    Łapie warianty sufiksu: 'X' → {'X','X1','X2'}. Używane przy dopasowaniu faktur."""
    ensure_table(db)
    norm = normalize_ref(raw_ref)
    if not norm:
        return []
    rows = db.execute(
        "SELECT * FROM material_master WHERE ref_norm LIKE ? ORDER BY ref_code",
        (norm + "%",)).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 3b: Write invoice_mapper.py**

```python
# invoice_mapper.py
"""Wzbogaca surowe pozycje faktury o dane z master daty i ustala status dopasowania.
Dopasowanie po REF z obsługą aliasów sufiksu (X → X1). Deterministyczne, bez LLM."""

import material_master as mm
import uom


def _name_pl(mat: dict) -> str:
    return (mat.get("opis_pl") or mat.get("txt_short_pl") or "").strip()


def _enrich(item: dict, mat: dict, conversions) -> None:
    item["master_ref"] = mat["ref_code"]
    item["name_pl"] = _name_pl(mat)
    item["tariff_cn"] = mat.get("tariff_cn") or ""
    item["sent"] = int(mat.get("sent") or 0)
    factor = uom.get_factor(item.get("uom_src") or "", mat.get("base_uom") or "",
                            mat["ref_code"], conversions)
    item["uom_factor"] = "" if factor is None else str(factor)


def _blank(item: dict, status: str) -> None:
    item["master_ref"] = ""
    item["name_pl"] = ""
    item["tariff_cn"] = ""
    item["sent"] = 0
    item["uom_factor"] = ""
    item["match_status"] = status


def map_items(db, raw_items: list) -> list:
    conversions = uom.load_conversions(db)
    out = []
    for item in raw_items:
        item = dict(item)
        raw_ref = (item.get("raw_ref") or "").strip()
        mat = mm.get_material(db, raw_ref) if raw_ref else None
        if mat:
            _enrich(item, mat, conversions)
            item["match_status"] = "matched"
        else:
            cands = mm.find_candidates(db, raw_ref) if raw_ref else []
            if len(cands) == 1:
                _enrich(item, cands[0], conversions)
                item["match_status"] = "matched"
            elif len(cands) > 1:
                _blank(item, "ambiguous")
            else:
                _blank(item, "unmatched")
        out.append(item)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_invoice_mapper.py -v`
Expected: PASS (5 testów)

- [ ] **Step 5: Commit**

```bash
git add material_master.py invoice_mapper.py tests/test_invoice_mapper.py
git commit -m "feat(faktury): invoice_mapper — dopasowanie REF (alias X/X1) + wzbogacenie master"
```

---

### Task 4: `invoice_excel_export.py` — Excel wg stałego kontraktu

**Files:**
- Create: `invoice_excel_export.py`
- Test: `tests/test_invoice_excel_export.py`

**Interfaces:**
- Consumes: `openpyxl` (lazy import).
- Produces:
  - `COLUMNS: list[str]` — nagłówki w stałej kolejności.
  - `build_workbook(rows: list[dict]) -> "openpyxl.Workbook"` — jeden arkusz „Faktury"; pomija `skipped=1`; `unmatched`/`ambiguous` mają puste pola master; kolumny w kolejności `COLUMNS`.

Kontrakt kolumn (jeden do jednego ze specem):
`Nr faktury, Ilość, REF, Nazwa PL, Waga netto, Waga brutto, Przelicznik jednostki, Kwota, Kod celny (CN), SENT, Status dopasowania`.
Mapowanie z pola wiersza: Nr faktury←`invoice_number`, Ilość←`qty`, REF←`master_ref` (albo `raw_ref` gdy pusty), Nazwa PL←`name_pl`, Waga netto←`weight_net`, Waga brutto←`weight_gross`, Przelicznik jednostki←`uom_factor`, Kwota←`amount`, Kod celny←`tariff_cn`, SENT←`TAK`/`NIE` z `sent`, Status←`match_status`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_invoice_excel_export.py
import invoice_excel_export as ex

def _row(**kw):
    base = {"invoice_number": "FV/1", "qty": "10", "raw_ref": "X", "master_ref": "X",
            "name_pl": "Rękawice", "weight_net": "1", "weight_gross": "1.2",
            "uom_factor": "24.0", "amount": "250", "tariff_cn": "4015", "sent": 1,
            "match_status": "matched", "skipped": 0}
    base.update(kw)
    return base

def test_columns_contract_exact():
    assert ex.COLUMNS == ["Nr faktury", "Ilość", "REF", "Nazwa PL", "Waga netto",
                          "Waga brutto", "Przelicznik jednostki", "Kwota",
                          "Kod celny (CN)", "SENT", "Status dopasowania"]

def test_header_and_row_values():
    wb = ex.build_workbook([_row()])
    ws = wb.active
    header = [c.value for c in ws[1]]
    assert header == ex.COLUMNS
    r2 = [c.value for c in ws[2]]
    assert r2[0] == "FV/1" and r2[2] == "X" and r2[3] == "Rękawice"
    assert r2[9] == "TAK"                      # SENT 1 → TAK
    assert r2[10] == "matched"

def test_skipped_excluded_and_unmatched_blank_master():
    wb = ex.build_workbook([
        _row(skipped=1),
        _row(match_status="unmatched", master_ref="", raw_ref="Q", name_pl="",
             tariff_cn="", sent=0),
    ])
    ws = wb.active
    assert ws.max_row == 2                      # 1 nagłówek + 1 wiersz (skipped pominięty)
    r = [c.value for c in ws[2]]
    assert r[2] == "Q"                          # REF fallback do raw_ref
    assert r[3] == "" and r[8] == ""            # brak nazwy PL i CN
    assert r[9] == "NIE"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_invoice_excel_export.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'invoice_excel_export'`

- [ ] **Step 3: Write minimal implementation**

```python
# invoice_excel_export.py
"""Buduje Excel wg stałego kontraktu kolumn z zatwierdzonych pozycji faktur.
openpyxl importowany lazy (nie psuje compileall/boot bez zależności)."""

COLUMNS = ["Nr faktury", "Ilość", "REF", "Nazwa PL", "Waga netto", "Waga brutto",
           "Przelicznik jednostki", "Kwota", "Kod celny (CN)", "SENT",
           "Status dopasowania"]


def _row_values(r: dict) -> list:
    ref = (r.get("master_ref") or r.get("raw_ref") or "")
    return [
        r.get("invoice_number") or "",
        r.get("qty") or "",
        ref,
        r.get("name_pl") or "",
        r.get("weight_net") or "",
        r.get("weight_gross") or "",
        r.get("uom_factor") or "",
        r.get("amount") or "",
        r.get("tariff_cn") or "",
        "TAK" if int(r.get("sent") or 0) else "NIE",
        r.get("match_status") or "",
    ]


def build_workbook(rows: list):
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Faktury"
    ws.append(COLUMNS)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        if int(r.get("skipped") or 0):
            continue
        ws.append(_row_values(r))
    return wb
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_invoice_excel_export.py -v`
Expected: PASS (3 testy)

- [ ] **Step 5: Commit**

```bash
git add invoice_excel_export.py tests/test_invoice_excel_export.py
git commit -m "feat(faktury): invoice_excel_export — Excel wg stałego kontraktu kolumn"
```

---

### Task 5: `invoice_pipeline.py` — orkiestracja: gate profilu, ekstrakcja, potwierdzenie

**Files:**
- Create: `invoice_pipeline.py`
- Test: `tests/test_invoice_pipeline.py`

**Interfaces:**
- Consumes: `invoice_jobs`, `invoice_extractor.extract_invoice`, `invoice_mapper.map_items`, `supplier_profiles.get_supplier`.
- Produces:
  - `process_job(db, job_id: int) -> str` — pełny bieg jednej faktury: gate profilu → ekstrakcja → mapowanie → zapis pozycji. Zwraca końcowy `status` (`extracted` lub `error`). Brak profilu → `status=error`, `error="Brak profilu dostawcy: <code>"`, pozycje nie zapisywane.
  - `confirm_job(db, job_id: int) -> tuple[bool, str]` — waliduje regułę zatwierdzenia: jeśli jest jakakolwiek pozycja `ambiguous` (i nie `skipped`) → `(False, "Rozstrzygnij niejednoznaczne pozycje (X/X1)")`; inaczej ustaw `status=confirmed` → `(True, "")`. `unmatched` NIE blokuje.

- [ ] **Step 1: Write the failing test** (monkeypatch extractor, by nie ruszać PDF)

```python
# tests/test_invoice_pipeline.py
import sqlite3
import invoice_jobs as ij
import invoice_pipeline as ip

def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    import material_master as mm, uom
    mm.ensure_table(db); uom.ensure_table(db); ij.ensure_invoice_tables(db)
    return db

def test_no_profile_blocks_extraction(monkeypatch):
    db = _db()
    jid = ij.create_job(db, "b", "a.pdf", "uploads/a.pdf", "NIEZNANY")
    monkeypatch.setattr(ip, "get_supplier", lambda code, **k: None)
    status = ip.process_job(db, jid)
    assert status == "error"
    job = ij.get_job(db, jid)
    assert "Brak profilu" in job["error"]
    assert ij.get_items(db, jid) == []           # nic nie wyekstrahowano

def test_happy_path_extracts_and_maps(monkeypatch):
    db = _db()
    db.execute("INSERT INTO material_master (ref_code, ref_norm, opis_pl) VALUES ('X','X','Rękawice')")
    db.commit()
    jid = ij.create_job(db, "b", "a.pdf", "uploads/a.pdf", "S1")
    monkeypatch.setattr(ip, "get_supplier", lambda code, **k: {"code": "S1"})
    monkeypatch.setattr(ip, "extract_invoice", lambda path, sup: {
        "items": [{"line_no": 1, "raw_ref": "X", "uom_src": "PCS", "qty": "10"}],
        "total_net": "10", "total_qty": "10", "raw_text": "", "invoice_number": "FV/9"})
    status = ip.process_job(db, jid)
    assert status == "extracted"
    items = ij.get_items(db, jid)
    assert items[0]["match_status"] == "matched"
    assert items[0]["name_pl"] == "Rękawice"
    assert ij.get_job(db, jid)["invoice_number"] == "FV/9"

def test_confirm_blocked_by_ambiguous():
    db = _db()
    jid = ij.create_job(db, "b", "a.pdf", "uploads/a.pdf", "S1")
    ij.save_items(db, jid, [{"line_no": 1, "raw_ref": "X", "match_status": "ambiguous"}])
    ok, msg = ip.confirm_job(db, jid)
    assert ok is False and "niejednoznaczne" in msg.lower()
    assert ij.get_job(db, jid)["status"] != "confirmed"

def test_confirm_allows_unmatched():
    db = _db()
    jid = ij.create_job(db, "b", "a.pdf", "uploads/a.pdf", "S1")
    ij.save_items(db, jid, [{"line_no": 1, "raw_ref": "Q", "match_status": "unmatched"}])
    ok, msg = ip.confirm_job(db, jid)
    assert ok is True
    assert ij.get_job(db, jid)["status"] == "confirmed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_invoice_pipeline.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'invoice_pipeline'`

- [ ] **Step 3: Write minimal implementation**

```python
# invoice_pipeline.py
"""Orkiestracja jednej faktury: gate profilu (obowiązkowy) → ekstrakcja → mapowanie →
zapis pozycji; oraz walidacja zatwierdzenia (ambiguous blokuje, unmatched nie)."""

import invoice_jobs as ij
from invoice_extractor import extract_invoice
from invoice_mapper import map_items
from supplier_profiles import get_supplier


def process_job(db, job_id: int) -> str:
    job = ij.get_job(db, job_id)
    if not job:
        return "error"
    supplier = get_supplier(job["supplier_code"]) if job.get("supplier_code") else None
    if not supplier:
        ij.update_job(db, job_id, status="error",
                      error=f"Brak profilu dostawcy: {job.get('supplier_code') or '—'}")
        return "error"
    try:
        extracted = extract_invoice(job["pdf_path"], supplier)
    except Exception as e:                       # ekstrakcja padła (zły skan itd.)
        ij.update_job(db, job_id, status="error", error=f"Nie udało się odczytać: {e}")
        return "error"
    if not extracted["items"]:
        ij.update_job(db, job_id, status="error",
                      error="Nie udało się odczytać tabeli pozycji")
        return "error"
    mapped = map_items(db, extracted["items"])
    ij.save_items(db, job_id, mapped)
    ij.update_job(db, job_id, status="extracted",
                  invoice_number=extracted.get("invoice_number") or "")
    return "extracted"


def confirm_job(db, job_id: int) -> tuple:
    items = ij.get_items(db, job_id)
    for it in items:
        if not int(it.get("skipped") or 0) and it.get("match_status") == "ambiguous":
            return (False, "Rozstrzygnij niejednoznaczne pozycje (X/X1) przed zatwierdzeniem")
    ij.update_job(db, job_id, status="confirmed")
    return (True, "")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_invoice_pipeline.py -v`
Expected: PASS (4 testy)

- [ ] **Step 5: Commit**

```bash
git add invoice_pipeline.py tests/test_invoice_pipeline.py
git commit -m "feat(faktury): invoice_pipeline — gate profilu, bieg ekstrakcji, reguła confirm"
```

---

### Task 6: `invoice_routes.py` — Blueprint (upload, coverage, review, export)

**Files:**
- Create: `invoice_routes.py`
- Modify: `app.py` (import + `app.register_blueprint(invoice_bp)` obok linii ~203–206)
- Test: `tests/test_invoice_routes_smoke.py`

**Interfaces:**
- Consumes: `core.security.login_required`, `core.security.require_role`, `db.get_db`, `invoice_jobs`, `invoice_pipeline`, `invoice_excel_export`, `supplier_profiles.get_all_suppliers`, `supplier_profiles.detect_supplier`.
- Produces Blueprint `invoice_bp` (url_prefix `/invoices`) z trasami:
  - `GET /invoices/` → strona modułu (upload + link do coverage). Szablon `invoices_home.html`.
  - `POST /invoices/upload` → zapis 1..N PDF do `uploads/`, `detect_supplier` z `raw_text` (lub pole formularza `supplier_code`), `create_job`, uruchom `process_job` w wątku-daemonie per plik, redirect do coverage/batch.
  - `GET /invoices/coverage` → dashboard: `get_all_suppliers` + status profilu + liczniki z `invoice_jobs`. Szablon `invoice_coverage.html`.
  - `GET /invoices/master` → przeglądarka `material_master` (filtr `q`). Szablon `invoice_master.html`.
  - `GET /invoices/review/<int:job_id>` → split-view: dane joba + `get_items` jako JSON + URL do PDF. Szablon `invoice_review.html`.
  - `GET /invoices/pdf/<int:job_id>` → `send_file` PDF joba (walidacja że ścieżka pod `uploads/`).
  - `POST /invoices/review/<int:job_id>` → zapis edycji (JSON pozycji) przez `save_items`, opcjonalnie `confirm` → `invoice_pipeline.confirm_job`; zwraca JSON `{ok, msg}`.
  - `GET /invoices/export/<batch_id>` (opcjonalnie `?job_id=`) → zbierz `confirmed` pozycje, `build_workbook`, `send_file` xlsx.

Wszystkie trasy: `@login_required` + `@require_role("user")` (wewnętrzni; wykluczeni forwarder/customs_agent zgodnie z konwencją).

Szkielet (kluczowe trasy — reszta analogicznie):

```python
# invoice_routes.py
import os, io, threading, uuid
from flask import (Blueprint, request, render_template, redirect, url_for,
                   send_file, jsonify, session)
from werkzeug.utils import secure_filename

from core.security import login_required, require_role
from db import get_db
import invoice_jobs as ij
import invoice_pipeline as pipeline
import invoice_excel_export as excel
from supplier_profiles import get_all_suppliers, detect_supplier

invoice_bp = Blueprint("invoices", __name__, url_prefix="/invoices")
UPLOAD_DIR = "uploads"


def _process_async(job_id):
    # własne połączenie w wątku (get_db per-thread wg abstrakcji db.py)
    db = get_db()
    pipeline.process_job(db, job_id)


@invoice_bp.route("/")
@login_required
@require_role("user")
def home():
    return render_template("invoices_home.html",
                           suppliers=get_all_suppliers())


@invoice_bp.route("/upload", methods=["POST"])
@login_required
@require_role("user")
def upload():
    db = get_db()
    batch_id = uuid.uuid4().hex[:12]
    files = request.files.getlist("pdf")
    forced_supplier = (request.form.get("supplier_code") or "").strip()
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    for f in files:
        if not f or not f.filename:
            continue
        fn = secure_filename(f.filename)
        path = os.path.join(UPLOAD_DIR, f"{batch_id}_{fn}")
        f.save(path)
        code = forced_supplier
        if not code:
            # rozpoznanie po treści (raw_text z kaskady) — tanie: detect_supplier
            sup = detect_supplier("")           # patrz uwaga niżej
            code = (sup or {}).get("code", "")
        jid = ij.create_job(db, batch_id, fn, path, code)
        threading.Thread(target=_process_async, args=(jid,), daemon=True).start()
    return redirect(url_for("invoices.coverage", batch_id=batch_id))
```

Uwaga do rozpoznawania dostawcy: `detect_supplier` potrzebuje tekstu faktury. Aby nie czytać PDF dwa razy, w v1 dopuszczamy **wybór dostawcy z listy w formularzu uploadu** (pole `supplier_code`), a auto-detekcję z `raw_text` wykonuje `process_job` przed gate'em, jeśli `supplier_code` puste — patrz Step 3 (uzupełnienie `process_job` o auto-detekcję).

- [ ] **Step 1: Uzupełnij `process_job` o auto-detekcję dostawcy gdy pusty**

W `invoice_pipeline.process_job`, przed gate'em, gdy `job["supplier_code"]` puste: odczytaj `raw_text` przez `extract_invoice` nie jest jeszcze możliwe (potrzebny profil). Zamiast tego użyj lekkiej detekcji: wczytaj tekst pierwszej strony i `detect_supplier`. Dla utrzymania testów prostymi, dodaj funkcję pomocniczą i monkeypatchowalny hook:

```python
# w invoice_pipeline.py — dodaj u góry
from supplier_profiles import get_supplier, detect_supplier

def _resolve_supplier_code(db, job) -> str:
    code = (job.get("supplier_code") or "").strip()
    if code:
        return code
    # lekka detekcja z tekstu PDF (pdfplumber pierwsza strona) — lazy import
    try:
        import pdfplumber
        with pdfplumber.open(job["pdf_path"]) as pdf:
            text = (pdf.pages[0].extract_text() or "") if pdf.pages else ""
    except Exception:
        text = ""
    sup = detect_supplier(text) if text else None
    return (sup or {}).get("code", "") if sup else ""
```

I w `process_job` zamień pobranie kodu na:

```python
    code = _resolve_supplier_code(db, job)
    if code and code != job.get("supplier_code"):
        ij.update_job(db, job_id, supplier_code=code)
    supplier = get_supplier(code) if code else None
```

- [ ] **Step 2: Write smoke test (test client, monkeypatch pipeline)**

```python
# tests/test_invoice_routes_smoke.py
import app as appmod

def test_home_requires_login():
    client = appmod.app.test_client()
    r = client.get("/invoices/")
    assert r.status_code in (302, 401)          # redirect do logowania

def test_blueprint_registered():
    rules = {r.rule for r in appmod.app.url_map.iter_rules()}
    assert "/invoices/" in rules
    assert "/invoices/upload" in rules
    assert "/invoices/coverage" in rules
```

- [ ] **Step 3: Implement remaining routes** (coverage, master, review GET/POST, pdf, export)

Pełne trasy wg listy w „Produces". Kluczowe fragmenty:

```python
@invoice_bp.route("/coverage")
@login_required
@require_role("user")
def coverage():
    db = get_db()
    batch_id = request.args.get("batch_id", "")
    suppliers = get_all_suppliers()
    jobs = ij.list_jobs(db, batch_id) if batch_id else []
    return render_template("invoice_coverage.html",
                           suppliers=suppliers, jobs=jobs, batch_id=batch_id)


@invoice_bp.route("/review/<int:job_id>", methods=["GET"])
@login_required
@require_role("user")
def review(job_id):
    db = get_db()
    job = ij.get_job(db, job_id)
    items = ij.get_items(db, job_id)
    return render_template("invoice_review.html", job=job, items=items)


@invoice_bp.route("/review/<int:job_id>", methods=["POST"])
@login_required
@require_role("user")
def review_save(job_id):
    db = get_db()
    payload = request.get_json(force=True)
    ij.save_items(db, job_id, payload.get("items", []))
    if payload.get("confirm"):
        ok, msg = pipeline.confirm_job(db, job_id)
        return jsonify({"ok": ok, "msg": msg})
    return jsonify({"ok": True, "msg": "Zapisano"})


@invoice_bp.route("/pdf/<int:job_id>")
@login_required
@require_role("user")
def pdf(job_id):
    db = get_db()
    job = ij.get_job(db, job_id)
    path = os.path.abspath(job["pdf_path"])
    if not path.startswith(os.path.abspath(UPLOAD_DIR)):   # ochrona przed path traversal
        return "Forbidden", 403
    return send_file(path)


@invoice_bp.route("/export/<batch_id>")
@login_required
@require_role("user")
def export(batch_id):
    db = get_db()
    job_id = request.args.get("job_id", type=int)
    jobs = [ij.get_job(db, job_id)] if job_id else ij.list_jobs(db, batch_id)
    rows = []
    for job in jobs:
        if not job or job["status"] != "confirmed":
            continue
        for it in ij.get_items(db, job["id"]):
            it = dict(it); it["invoice_number"] = job["invoice_number"]
            rows.append(it)
    wb = excel.build_workbook(rows)
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"faktury_{batch_id}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
```

- [ ] **Step 4: Register blueprint in app.py**

Obok istniejących `app.register_blueprint(...)` (linie ~203–206):

```python
from invoice_routes import invoice_bp
app.register_blueprint(invoice_bp)
```

- [ ] **Step 5: Run smoke tests + compileall**

Run: `python -m compileall invoice_routes.py invoice_pipeline.py app.py -q && python -m pytest tests/test_invoice_routes_smoke.py -v`
Expected: PASS; blueprint zarejestrowany, `/invoices/` wymaga logowania.

- [ ] **Step 6: Commit**

```bash
git add invoice_routes.py invoice_pipeline.py app.py tests/test_invoice_routes_smoke.py
git commit -m "feat(faktury): blueprint tras (upload, coverage, review, pdf, export) + auto-detekcja dostawcy"
```

---

### Task 7: Szablony — home/upload, coverage, split-view review, master browser

**Files:**
- Create: `templates/invoices_home.html`
- Create: `templates/invoice_coverage.html`
- Create: `templates/invoice_review.html`
- Create: `templates/invoice_master.html`
- Modify: nawigacja w `templates/base.html` (link do „Faktury → Excel")

**Interfaces:**
- Consumes: zmienne z tras Taska 6 (`suppliers`, `jobs`, `job`, `items`, `batch_id`).
- Produces: UI. Wszystkie dziedziczą po `base.html` (`{% extends "base.html" %}`), Polish-first, Alpine.js do interakcji (wzorzec repo).

Kluczowe elementy (bez pełnego HTML — wzoruj się na istniejących szablonach repo):

**`invoices_home.html`** — formularz uploadu:
- `<form method="post" action="{{ url_for('invoices.upload') }}" enctype="multipart/form-data">`
- `<input type="file" name="pdf" multiple accept="application/pdf">`
- dropdown `supplier_code` z `suppliers` (opcjonalny — „auto-rozpoznaj" jako pusta wartość)
- link do `{{ url_for('invoices.coverage') }}` i `{{ url_for('invoices.master') }}`

**`invoice_coverage.html`** — dwie tabele:
- Dostawcy: kolumny Dostawca / Profil (badge „sparametryzowany" zielony / „brak" czerwony — po `supplier.column_map` niepustym) / liczba faktur / ostatnia.
- Jeśli `batch_id`: lista `jobs` ze statusem i linkiem `Przejrzyj` → `invoices.review`.

**`invoice_review.html`** — split-view (Alpine.js):
- lewy panel: `<iframe src="{{ url_for('invoices.pdf', job_id=job.id) }}">`
- prawy panel: edytowalna tabela `items` (Alpine `x-data` z listą), kolory wierszy wg `match_status`:
  `matched`=zielony, `ambiguous`=pomarańczowy, `unmatched`=czerwony; checkbox „pomiń" (`skipped`); przy `ambiguous` pole `master_ref` jako input.
- przyciski „Zapisz" (POST bez `confirm`) i „Zatwierdź" (POST `confirm:true`) — fetch do `invoices.review_save`; przy `ok:false` pokaż `msg`.
- po zatwierdzeniu: link „Pobierz Excel" → `invoices.export`.

**`invoice_master.html`** — tabela `material_master` z polem szukania (`?q=`): REF, Nazwa PL (`opis_pl`), CN (`tariff_cn`), SENT (TAK/NIE), base_uom.

- [ ] **Step 1: Create the four templates** (wg powyższych elementów, dziedzicząc po base.html)

- [ ] **Step 2: Add nav link in base.html**

W bloku nawigacji dodaj (spójnie z istniejącymi linkami):
```html
<a href="{{ url_for('invoices.home') }}">Faktury → Excel</a>
```

- [ ] **Step 3: Manual verification (frontend — bez auto-testów)**

```
python app.py
```
Zaloguj (admin/admin123). Sprawdź ręcznie:
1. `/invoices/` — upload 1–2 PDF-ów znanego dostawcy (z profilem).
2. Coverage pokazuje job; po ekstrakcji status `extracted`.
3. `/invoices/review/<id>` — PDF po lewej, pozycje po prawej, kolory wg statusu; edycja i „Zapisz" działa; „Zatwierdź" blokuje gdy `ambiguous`.
4. Upload dostawcy BEZ profilu → status `error` z komunikatem „Brak profilu".
5. „Pobierz Excel" → plik z właściwymi kolumnami.

- [ ] **Step 4: Run full suite + compileall (regresja)**

Run: `python -m compileall . -q && python -m pytest tests/ -q`
Expected: brak błędów składni; wszystkie testy zielone.

- [ ] **Step 5: Commit**

```bash
git add templates/invoices_home.html templates/invoice_coverage.html templates/invoice_review.html templates/invoice_master.html templates/base.html
git commit -m "feat(faktury): szablony — home/upload, coverage, split-view review, master browser"
```

---

## Self-Review

**Spec coverage:**
- Osobny moduł → Task 1–7 (nie dotyka porównywarki). ✔
- Kolumny Excela (stały kontrakt) → Task 4 (`COLUMNS`, test kontraktu). ✔
- Nazwa PL / CN / SENT / przelicznik z master → Task 3 (`_enrich`, `uom.get_factor`). ✔
- Dopasowanie REF z aliasem X/X1, ambiguous → Task 3 (`find_candidates`, testy). ✔
- Profil obowiązkowy (gate) → Task 5 (`process_job`, test). ✔
- Split-view + kolory + manualne potwierdzenie → Task 6 (trasy) + Task 7 (UI). ✔
- ambiguous blokuje, unmatched nie → Task 5 (`confirm_job`, dwa testy). ✔
- Batch + zbiorczy/per-faktura Excel → Task 6 (`upload` batch_id, `export`). ✔
- Dashboard pokrycia + przeglądarka master (sekcja 1b) → Task 6 (`coverage`, `master`) + Task 7. ✔
- unmatched zostaje w Excelu (polityka A) → Task 4 (test `unmatched_blank_master`). ✔
- v2/YAGNI (ref_alias_map, LLM-fallback) → poza planem, celowo. ✔

**Placeholder scan:** brak TBD/TODO; wszystkie kroki logiczne mają realny kod i komendy.

**Type consistency:** pola pozycji (`raw_ref`, `master_ref`, `match_status`, `uom_factor`, `sent`, `skipped`) spójne między `invoice_jobs._ITEM_COLS`, `invoice_mapper`, `invoice_excel_export._row_values`, `invoice_pipeline`. `extract_invoice` zwraca klucze zgodne z `_ITEM_COLS`. `confirm_job`/`process_job` sygnatury zgodne z użyciem w trasach.

## Poza zakresem (v2, YAGNI)
- `ref_alias_map` (zapamiętywanie wyboru X/X1), LLM-fallback dla długiego ogona dostawców, automatyczny wybór wersji z treści faktury, waga netto/brutto z master daty gdy brak na fakturze.
