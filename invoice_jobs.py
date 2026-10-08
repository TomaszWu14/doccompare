"""Warstwa danych modułu Faktury → Excel: tabele invoice_jobs / invoice_items + CRUD.
SQL zawsze przez przekazane połączenie (db.get_db()). Nie tworzy własnego połączenia."""

from enum import Enum

from constants import DocKind, InvoiceJobStatus


def _plain(v):
    # Enum (np. InvoiceJobStatus/MatchStatus) → jego wartość str; psycopg2 nie adaptuje Enum.
    return v.value if isinstance(v, Enum) else v


_ITEM_COLS = ("line_no", "raw_ref", "qty", "net_amount", "weight_net", "weight_gross",
              "uom_src", "amount", "descr", "master_ref", "name_pl", "tariff_cn",
              "sent", "uom_factor", "match_status", "skipped", "cartons")

_JOB_UPDATABLE = ("status", "error", "supplier_code", "invoice_number",
                  "doc_kind", "container_no", "delivery_terms")


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
        doc_kind TEXT DEFAULT 'invoice',
        source_file TEXT DEFAULT '',
        page_from INTEGER DEFAULT 0,
        page_to INTEGER DEFAULT 0,
        container_no TEXT DEFAULT '',
        delivery_terms TEXT DEFAULT '',
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
        descr TEXT DEFAULT '',
        master_ref TEXT DEFAULT '',
        name_pl TEXT DEFAULT '',
        tariff_cn TEXT DEFAULT '',
        sent INTEGER DEFAULT 0,
        uom_factor TEXT DEFAULT '',
        match_status TEXT DEFAULT 'unmatched',
        skipped INTEGER DEFAULT 0,
        cartons TEXT DEFAULT ''
    )""")
    db.commit()


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


def get_job(db, job_id) -> dict | None:
    row = db.execute("SELECT * FROM invoice_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(db, batch_id) -> list:
    rows = db.execute(
        "SELECT * FROM invoice_jobs WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
    return [dict(r) for r in rows]


def list_invoice_jobs(db, batch_id) -> list:
    """Joby faktur batcha — bez packing-list (te służą tylko jako źródło wag)."""
    rows = db.execute(
        "SELECT * FROM invoice_jobs WHERE batch_id=? AND status<>? "
        "AND COALESCE(doc_kind,?)<>? ORDER BY id",
        (batch_id, InvoiceJobStatus.PACKING_LIST.value, DocKind.INVOICE.value,
         DocKind.PACKING_LIST.value)).fetchall()
    return [dict(r) for r in rows]


def update_job(db, job_id, **fields) -> None:
    cols = [c for c in fields if c in _JOB_UPDATABLE]
    if not cols:
        return
    sets = ", ".join(f"{c}=?" for c in cols) + ", updated_at=datetime('now')"
    # bandit: kolumny przefiltrowane przez stałą _JOB_UPDATABLE; wartości przez ?
    db.execute(f"UPDATE invoice_jobs SET {sets} WHERE id=?",  # nosec B608
               tuple(_plain(fields[c]) for c in cols) + (job_id,))
    db.commit()


_INT_COLS = ("skipped", "sent")


def save_items(db, job_id, items) -> None:
    db.execute("DELETE FROM invoice_items WHERE job_id=?", (job_id,))
    for it in items:
        vals = [int(it.get(c) or 0) if c in _INT_COLS else _plain(it.get(c)) for c in _ITEM_COLS]
        ph = ", ".join(["?"] * (len(_ITEM_COLS) + 1))
        db.execute(
            # bandit: kolumny ze stałej krotki _ITEM_COLS; wartości przez ?
            f"INSERT INTO invoice_items (job_id, {', '.join(_ITEM_COLS)}) VALUES ({ph})",  # nosec B608
            tuple([job_id] + vals))
    db.commit()


def list_packing_lists(db, batch_id) -> list:
    rows = db.execute(
        "SELECT * FROM invoice_jobs WHERE batch_id=? AND "
        "(COALESCE(doc_kind,'')=? OR status=?) ORDER BY id",
        (batch_id, DocKind.PACKING_LIST.value, InvoiceJobStatus.PACKING_LIST.value)
        ).fetchall()
    return [dict(r) for r in rows]


def get_items(db, job_id) -> list:
    rows = db.execute(
        "SELECT * FROM invoice_items WHERE job_id=? ORDER BY line_no, id", (job_id,)).fetchall()
    return [dict(r) for r in rows]
