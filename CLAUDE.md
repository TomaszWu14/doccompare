# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> See also `AGENTS.md` for environment-specific run notes and default test accounts.

## Project Overview

**DocCompare v6** — A Flask web application for ACME that intelligently
compares international trade documents (Purchase Orders, Proforma/Commercial Invoices, Packing
Lists, Customs Declarations / SAD, B/L, WZ, FV) and artwork/packaging PDFs, and manages the
end-to-end purchasing → transport → customs → settlement workflow.

The app is organized into several business areas, all served from one Flask process:

- **Zakupy (Purchasing)** — document comparison (PO vs PI, CI vs PL, SAD checks), AI validation,
  supplier profiles, master data.
- **Transport / Kolejka** — container/order queue, transport orders, delivery workflow stepper,
  demurrage & detention tracking, shipment tracking via carrier APIs.
- **Spedycja (Forwarding portal)** — external portal for forwarders (`forwarder` role).
- **Agencja (Customs portal)** — external portal for customs agencies (`customs_agent` role).
- **Artwork** — visual/packaging PDF comparison, barcode validation, and a master-artwork library
  index (31k+ files synced from a network drive).

## Common Commands

```bash
# Install dependencies (full stack incl. ML/OCR — large)
pip install -r requirements.txt
# Production subset
pip install -r requirements-prod.txt
# Dev tooling only (pytest, ruff)
pip install -r dev-requirements.txt

# Run the development server (http://localhost:5000, debug on)
python app.py            # use python3 on environments without a `python` alias

# Run with gunicorn (production-like)
gunicorn app:app -c gunicorn.conf.py

# Apply database migrations (idempotent — safe to re-run)
python migrate_db.py

# Run the test suite (fast — pure-logic tests, no heavy ML deps needed)
python -m pytest tests/ -q

# Lint / format (advisory only — NOT a deploy gate)
ruff check .
ruff format .
```

### Default dev accounts (seeded on first run when no `admin` exists)

| Username | Password | Role |
|---|---|---|
| admin | admin123 | admin |
| superuser | super123 | superuser |
| jan.kowalski | haslo123 | user |
| anna.nowak | haslo123 | manager |

The login form field is `identifier` (accepts username **or** email).

## Testing & CI

There **is** a pytest suite under `tests/` (~35 files) covering pure business logic — normalizer,
barcode/GS1 validation, comparator heuristics, typo detection, UOM conversion, delivery workflow,
demurrage, CN audit, material/supplier master, semantic matching, plus an app-boot smoke test
(`test_app_boots.py`). Tests deliberately avoid heavy ML deps (torch/paddleocr/transformers) so CI
runs in seconds; heavy modules are lazy-imported.

GitHub Actions workflows in `.github/workflows/`:
- **`ci.yml`** — runs `compileall` (syntax check of *every* module) + `pytest tests/` on push/PR.
- **`security.yml`** — scheduled/manual security scans (Bandit, gitleaks, Trivy, pip-audit).
  When adding code, keep `python -m compileall` clean and tests passing.

Linting (ruff, configured in `pyproject.toml` + `.pre-commit-config.yaml`) is intentionally lenient
(`select = ["F", "E9", "W6"]`, `E501`/line-length ignored) and is **not** wired to the deploy gate —
it's an advisory developer aid on the large legacy `app.py`.

## Environment Setup

Copy `.env.example` to `.env` and fill in the values. Required at minimum: `SECRET_KEY` and one of
`DATABASE_URL` / `SQLITE_PATH`. If neither DB var is set, the app defaults to SQLite at
`instance/doccompare.db`.

| Variable | Notes |
|---|---|
| `SECRET_KEY` | Flask session key (required) |
| `DATABASE_URL` | PostgreSQL URL (production — Supabase/Neon) |
| `SQLITE_PATH` | SQLite path (local dev, e.g. `instance/doccompare.db`) |
| `ANTHROPIC_API_KEY` | Claude API — AI validation, learning, Vision OCR, scan extraction |
| `MISTRAL_API_KEY` | Mistral OCR — primary API OCR engine (~$0.001/page) |
| `RESEND_API_KEY` / `EMAIL_FROM` | Transactional email (password reset, alerts) |
| `SAFECUBE_API_KEY` / `TRACKCARGO_API_KEY` / `MAERSK_CONSUMER_KEY` | Shipment tracking providers |
| `SENTRY_DSN` | Error tracking (optional) |
| `APP_BASE_URL` | Used for password reset links |
| `DATA_DIR` | Persistent data dir for master artwork PDFs (prod) |
| `DISABLE_QWEN` | `1` disables the Qwen2.5-VL local OCR model (~12 GB RAM) |
| `LOG_DIR` | Log output directory (e.g. `/tmp/logs`) |
| `WEB_CONCURRENCY` / `THREADS_PER_WORKER` / `DB_POOL_SIZE` / `LOG_LEVEL` | Gunicorn / pool tuning |

The app starts without `ANTHROPIC_API_KEY`/`MISTRAL_API_KEY`, but AI/OCR-dependent routes error at
runtime. System packages assumed present: `tesseract-ocr` (+ `tesseract-ocr-pol`), `poppler-utils`,
`ghostscript`.

## Architecture

### Entry Point & Routing

`app.py` is the monolithic entry point (~20,000 lines, ~360 routes). `app = Flask(__name__)` is
created at module top; there is no `create_app()` factory. Routes are grouped by area (auth, core
comparison, artwork, AI learning, suppliers, transport/kolejka, spedycja, agencja, admin, export).

Some logic has been extracted into packages to break `app.py` apart incrementally:
- **`core/`** — shared infrastructure imported by everything (no cycles): `security.py`
  (`login_required`, `require_role`, CSRF, session validation) and `audit.py` (`log_audit` →
  `audit_log` table).
- **`blueprints/`** — real Flask blueprints registered in `app.py` (`incoterms`, `translation_dict`,
  `transit_countries`) **plus** data-dictionary modules. Dependency direction is strictly
  `blueprint → core/db`, never back into `app`.

`docs/PLAN_REFAKTORU.md` documents the roadmap for further extraction (RQ workers, ML microservice,
more blueprints).

### Database Abstraction (`db.py`)

The app supports both SQLite (dev) and PostgreSQL (production) through a unified abstraction in
`db.py`. It transparently translates SQL dialects (`?` → `%s`, `datetime('now')` → `NOW()`, etc.),
provides a compatible row factory, and manages a connection pool (`DB_POOL_SIZE`). **Always** use
`db.get_db()` — never call `sqlite3.connect()` or `psycopg2.connect()` directly. Schema lives in
`migrate_db.py` (~33 tables, `CREATE TABLE IF NOT EXISTS` + idempotent `add_column` migrations);
`check_db.py` is a quick inspection helper.

### Document Comparison Pipeline

The core flow for trade documents (PO vs PI, CI vs PL, etc.):

1. **PDF / table extraction** — `table_extractor.py` and `pdf_extractor.py` run a multi-strategy
   cascade (see below).
2. **Normalization** — `normalizer.py` unifies number formats (`13,500.00` / `13.500,00`) and mixed
   locale date formats. `uom.py` converts between packaging units (PCS/CTN/OP/OPZ/PAL).
3. **Semantic matching** — `semantic_matcher.py` uses rapidfuzz + sklearn TF-IDF (and optional SBERT
   + FAISS) to match product codes across supplier naming variants (`NL753-S-40` ≈ `NL753S40`).
4. **Comparison** — `enhanced_comparator.py` is the main engine: detects payment-term equivalents
   (`SAP_PAYMENT_MAP`), validates arithmetic (qty × price = net), applies supplier tolerances.
5. **Typo detection** — `typo_detector.py` catches `I/1`, `O/0` confusions and missing fields.
6. **AI validation** — `ai_validator.py` calls the Claude API to risk-score results
   (`overall_risk` ok/warning/error/critical, `risk_score` 0–100).
7. **Export** — `export_engine.py` generates PDF (ReportLab), Excel (openpyxl), or Word (python-docx)
   reports; `pdf_annotator.py` overlays colored finding boxes onto source pages.

`enhanced_comparator.py` is the primary comparison engine. `comparator.py` is **older but
NOT dead** — its `extract_pdf_text` is the primary PDF-text helper (called throughout `app.py`)
and `compare_documents` is the regex-compare path; both have live callers. Do not delete it.
`batch_processor.py` runs many document pairs at once; `po_parsing.py`/`sap_extract.py`/`scan_extractor.py`
handle PO line items, SAP delivery numbers, and warehouse-scan (handwriting) extraction respectively.

### Extraction & OCR Cascade

Extraction is a cost/accuracy waterfall — each layer is tried only if the previous returns nothing.

**Table/item extraction** (`table_extractor.py`): pdfplumber → Camelot (lattice+stream) →
Claude Vision → Mistral OCR → img2table → Tesseract (PSM 6). See
`docs/kaskada_ekstrakcji_diagram.svg`.

**OCR engines** are integrated as fallbacks (most gated behind flags to avoid RAM/time blowups):
Mistral OCR (primary API), Claude Vision, Tesseract (always-available last resort), and optional
local neural engines RapidOCR → PaddleOCR → EasyOCR → DocTR → Surya → Qwen2.5-VL / GOT-OCR. Local
neural OCR for artwork is off by default (`ARTWORK_OCR_LOCAL`); Qwen is gated by `DISABLE_QWEN`.

### Artwork Comparison Pipeline

`artwork_comparator.py` (the largest module, ~7,500 lines) handles visual/packaging PDF comparison:
- Renders PDFs to images (150 DPI preview, 220 DPI OCR) via PyMuPDF.
- Computes pixel/SSIM diffs with region-based intensity mapping (red/yellow/blue zones); uses
  imagehash for fast "identical?" pre-filtering and optional CV models (DINOv2, LightGlue,
  GroundingDINO, Table Transformer, YOLOv8 via `yolo_detector.py`).
- Falls back through the OCR cascade when PyMuPDF text extraction fails.
- Extracts structured packaging fields (EAN, REF, Revision, Date, Gauge, Color, Format).
- Calls `barcode_validator.py` for barcode checks.
- `artwork_report_engine.py` builds the structured report dict consumed by `artwork_report.html`.

`artwork_3d.py` generates a 3D package model (GLB) from a dieline artwork PDF: reads W×H×D from
the artwork's metadata table, locates panels via crease-line clustering (PyMuPDF `get_drawings`),
renders panel textures and builds a textured box mesh with trimesh; viewed in-browser via
`<model-viewer>` (vendored in `static/vendor/model-viewer/`) at `/artwork/3d`.

Supporting artwork modules: `artwork_index.py` (master-file index from `Z:\`, aliases, mapping
groups, packaging-level detection), `artwork_naming.py` (filename-convention parser / revision
ranking), `library_sync.py` (CLI agent syncing metadata + thumbnails from the network drive),
`artwork_batch_processor.py` (up to 50 pairs → ZIP), `artwork_zone_comparator.py` (user-marked zone
diffs). CLI tooling lives in `tools/` (e.g. `scan_artwork_index.py`, `upload_masters.py`).

### Barcode Validation (`barcode_validator.py`)

Validates barcodes on medical packaging artworks:
- `validate_ean13()` — EAN-13 checksum via the GS1 algorithm.
- `parse_gs1_128()` — GS1-128 Application Identifiers: `(01)` GTIN, `(10)` LOT, `(17)` EXP, `(11)` prod date.
- `read_barcodes_from_image()` — reads from PIL images using `pyzbar` (active) / `zxingcpp`.
- `validate_artwork_barcodes()` — full validation: EAN checksum, A↔B consistency, barcode↔OCR match.
- Results stored in `ArtworkCompareResult.barcode_report`, shown in the report's
  "Weryfikacja kodów kreskowych" section.

### AI / Self-Learning System

- `ai_learning.py` — suggestion-based learning loop: a user uploads a PDF → `learn_from_document()`
  sends it to Claude → column-mapping/supplier suggestions are stored in `pending_updates` (never
  auto-applied) → a manager approves via `/api/ai/pending-updates`.
- `ai_validator.py` — post-hoc risk scoring of comparison results.
- `api_usage_tracker.py` — tracks Claude spend per model (Haiku/Sonnet/Opus) against a monthly USD
  budget (`api_usage` table); warns/blocks when the budget is near/exceeded.

### Transport / Logistics Domain

- `delivery_workflow.py` — the multi-step delivery stepper (order → warehouse → settlement) with
  status labels, auto-advance rules, SLA limits, and document-slot mappings.
- `demurrage.py` — demurrage (unload→pickup) and detention (pickup→empty-return) risk levels
  (green/amber/red), free-day thresholds, cost estimates, milestone extraction from tracking events.
- `alert_engine.py` — HTML email alerts for critical findings (e.g. >5% qty variance, I↔1 subs);
  config is DB/env driven with SMTP test + TTL cache.
- `cn_audit.py` — audits customs tariff (CN) codes from SAD declarations against
  `material_master.tariff_cn`.
- Queue/warehouse tables: `kolejka_*` (containers/orders/status log/notifications),
  `intake_queue`, `zlecenia_transportowe`, `warehouse_stock` (via `warehouse_stock.py`).

### Master Data

- `material_master.py` — materials (REF, description, EAN, family, base UOM, producer codes,
  per-packaging-level dims/EANs); auto-imports conversion factors into `uom_conversion`.
- `supplier_master.py` — suppliers (codes, names, producer-code links, Incoterms, lead/payment terms).
- `supplier_profiles.py` — per-supplier comparison profiles: column-role assignments per doc type
  (`ref`/`qty`/`price`/`net`/`lot`), price tolerance %, payment-term equivalents, product synonyms.
  `DEFAULT_PROFILES` ships pre-built profiles (Shieldco, Eastport, etc.); the Supplier Wizard UI
  (`/suppliers/wizard`) edits them interactively.

### Background Processing

Heavy work (artwork batches, large extractions) runs in **daemon threads**, with progress tracked in
the `job_progress` table and polled by the frontend (`_update_progress`; allowed columns in
`constants.PROGRESS_ALLOWED_COLS`). A Redis + RQ worker migration is **planned but not yet deployed**
— see `docs/KOLEJKOWANIE.md`. Do not introduce a hard Redis dependency without following that plan.

### Internationalization

The app is Polish-first with English support. `translations.py` provides a `t(key, lang)` key-value
map; `translation_en.py` post-processes rendered HTML to swap Polish phrases for English when
`session lang == 'en'`. `blueprints/translation_dict.py` manages a DB-backed term dictionary.

### Frontend

Jinja2 templates (60+ in `templates/`) with Alpine.js for reactivity, Chart.js for KPI charts,
Dropzone.js for uploads, and Tailwind CSS. `base.html` is the master layout. Design experiments live
in `static/design/` (excluded from lint).

## Key Conventions

- **Shared constants**: import from `constants.py` (`ComparisonStatus`, `DocType`, `Severity`,
  `UserRole`, limits) — avoid magic strings. App version is `constants.APP_VERSION`.
- **SQL**: always parameterized queries through `db.get_db()`; the abstraction handles dialects.
- **Number parsing**: use `normalizer.normalize_number()` — raw `float()` fails on European numbers.
- **Date parsing**: use `normalizer.normalize_date()` — documents arrive in mixed locales.
- **UOM**: use `uom.py` to convert between packaging units; don't hard-code factors.
- **Product matching**: use `semantic_matcher.match_items_semantic()`, not exact string compares.
- **PDF reading**: prefer the `table_extractor.py` / `pdf_extractor.py` cascade over calling
  pdfplumber/camelot/OCR engines directly.
- **Auth/roles**: gate routes with `core.security` decorators. Internal hierarchy is
  `user(1) < manager(2) < superuser(3) < admin(4)`. `forwarder` and `customs_agent` are **external**
  roles (level 0 — blocked from internal pages); they are confined to their portals via
  `FORWARDER_ALLOWED_PREFIXES` / `CUSTOMS_AGENT_ALLOWED_PREFIXES` and redirected by
  `EXTERNAL_ROLE_HOME`.
- **Audit**: record significant actions with `core.audit.log_audit(...)`.
- **Heavy/ML imports**: keep them lazy (function-local) so CI and app boot stay fast and the syntax
  gate (`compileall`) passes without ML deps installed.
- **File uploads**: PDFs go to `uploads/` (gitignored, auto-created); clean up after processing.
  Max upload `constants.MAX_UPLOAD_MB` (200 MB); allowed MIMEs in `ALLOWED_PDF_MIMES`.
- **Polish characters**: export code uses a DejaVu/Liberation/FreeSans font fallback for ą ę ó ź ż.

## Deployment

The original setup deployed `main` to Coolify on Hetzner (see `docs/DOKUMENTACJA_IT.md`);
this portfolio copy has no deploy workflow.
Gunicorn runs via `gunicorn.conf.py`; `Dockerfile` is available for container builds. Health
check path is `/login`. Sentry and Resend are optional, configured via env vars.
