> **Portfolio project.** Company names are replaced with fictional ones; demo and test data are synthetic.
>
> Source shared for review (portfolio), all rights reserved — see [`LICENSE`](LICENSE).

# DocCompare v6

[![ci](https://github.com/TomaszWu14/doccompare/actions/workflows/ci.yml/badge.svg)](https://github.com/TomaszWu14/doccompare/actions/workflows/ci.yml)

A Flask web application for ACME that intelligently compares
international trade documents (Purchase Orders, Proforma/Commercial Invoices, Packing Lists,
Customs Declarations / SAD, B/L, WZ, FV) and artwork/packaging PDFs, and manages the end-to-end
purchasing → transport → customs → settlement workflow.

Originally deployed with Docker on **Coolify / Hetzner**; this portfolio copy ships no deploy workflow.

## At a glance

| | |
|---|---|
| **Problem** | Purchasing and logistics teams cross-checked PO ↔ Proforma ↔ Commercial Invoice ↔ Packing List ↔ SAD by hand — price, quantity and Incoterms mismatches surfaced only at customs or settlement. |
| **Solution** | PDF/table extraction with per-supplier column profiles, line-level comparison with tolerances, customs (SAD) checks, artwork validation (EAN/GS1), and a purchasing → transport → customs → settlement workflow. |
| **Stack** | Python, Flask, pdfplumber / PyMuPDF / camelot, RapidOCR + Tesseract, optional LLM validation (Anthropic API), PostgreSQL/SQLite, RQ/Redis jobs, gunicorn, Docker. |
| **Quality** | ~600 tests (`pytest tests/`), CI on GitHub Actions, scheduled security scans. |

## Common Commands

```bash
# Install dependencies (full stack incl. ML/OCR — large)
pip install -r requirements.txt
# Production subset
pip install -r requirements-prod.txt
# Dev tooling only (pytest, ruff)
pip install -r dev-requirements.txt

# Run the development server (http://localhost:5000, debug on)
python app.py

# Run with gunicorn (production-like)
gunicorn app:app -c gunicorn.conf.py

# Apply database migrations (idempotent — safe to re-run)
python migrate_db.py

# Run the test suite
python -m pytest tests/ -q
```

## Documentation

- [`CLAUDE.md`](./CLAUDE.md) — architecture overview, conventions, and environment variables.
- [`docs/DOKUMENTACJA_IT.md`](./docs/DOKUMENTACJA_IT.md) — IT runbook (Polish), hosting and
  operations summary.
- [`docs/BACKUP_RESTORE.md`](./docs/BACKUP_RESTORE.md) — database and data backup/restore
  procedure.
