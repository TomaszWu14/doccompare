import io
import os
import re
import json
import shutil
import logging
import secrets
import threading
import time
import zipfile
from functools import wraps
from datetime import timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, g, send_file, flash, Response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from comparator import extract_pdf_text, compare_documents
from po_parsing import clean_po_ref as _clean_po_ref
from db import get_db, IntegrityError, escape_like
from core.security import (
    role_level, can_see_all, can_delete, is_admin,
    login_required, require_role, csrf_protect,
    check_session_active as _check_session_active,
    get_csrf_token as _get_csrf_token,
    verify_csrf as _verify_csrf,
)
from core.audit import log_audit as _log_audit
from core.timeutil import ts_ago as _ts_ago
from email_helpers import send_reset_email
from constants import ROLE_LEVEL, VALID_ROLES, EXTERNAL_ROLES, API_RATE_MAX, API_RATE_WINDOW, KPI_CACHE_TTL, MAX_UPLOAD_BYTES, MAX_UPLOAD_MB
from constants import FORWARDER_ALLOWED_PREFIXES as _FWD_PREFIXES, CUSTOMS_AGENT_ALLOWED_PREFIXES as _AGENCJA_PREFIXES, EXTERNAL_ROLE_HOME as _EXTERNAL_ROLE_HOME
import delivery_workflow as _dw
import customs_checklist as _customs
from delivery_workflow import (
    DOC_SLOTS as _DELIVERY_DOC_SLOTS,
    COMP_TYPE_TO_SLOTS as _COMP_TYPE_TO_SLOTS,
    PAIRS as _DELIVERY_PAIRS,
    NONADJACENT_COMPARE as _DELIVERY_NONADJ,
    PHASES as _DELIVERY_PHASES,
    STEPS as _DELIVERY_STEPS,
    step_index as _delivery_step_index,
    STATUS_LABELS as _DELIVERY_STATUS_LABELS,
    STATUS_DESCRIPTIONS as _DELIVERY_STATUS_DESCRIPTIONS,
    NEXT_STATUS as _DELIVERY_NEXT_STATUS,
    NEXT_LABELS as _DELIVERY_NEXT_LABELS,
    APPROVAL_STEPS as _DELIVERY_APPROVAL_STEPS,
    phase_index as _delivery_phase_index,
    prev_status as _delivery_prev_status,
    auto_advance_target as _delivery_auto_advance_target,
    missing_required_docs as _delivery_missing_required_docs,
    missing_slots as _delivery_missing_slots,
    status_from_comparison as _delivery_status_from_comparison,
    proforma_loaded_target as _delivery_proforma_loaded_target,
    status_from_artwork_comparison as _delivery_status_from_artwork,
    artwork_awaiting_target as _delivery_artwork_awaiting_target,
    PROFORMA_LOADED_STATUS as _DELIVERY_PROFORMA_LOADED,
    VERIFY_RESULT_STATUSES as _DELIVERY_VERIFY_STATUSES,
    ARTWORK_RESULT_STATUSES as _DELIVERY_ARTWORK_STATUSES,
    sla_overdue as _delivery_sla_overdue,
)
# Statusy „negatywne" twardo blokujące advance (dalej tylko ręcznym „Zmień status"
# z uzasadnieniem): niezgodność PO/PI oraz krytyczna różnica artworku w kodach/tekście
# (EAN/LOT/data/pola krytyczne = artwork_bledne). „artwork_niezgodne" to różnica
# graficzna/kolory → „do weryfikacji" (miękka, advance z potwierdzeniem) — decyzja #7.
_DELIVERY_BLOCKED_ADVANCE = {"sprawdzone_niezgodne", "artwork_bledne"}
from translations import t as _t_func, TRANSLATIONS
from translation_en import translate_html as _translate_html_en

# Ładuj .env (lokalne dev) — ignoruj brak pliku
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Logging ───────────────────────────────────────────────────────────────────
from logging.handlers import RotatingFileHandler
_log_dir = os.environ.get("LOG_DIR", "logs")
_log_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_log_fmt)
_handlers = [_stream_handler]
try:
    os.makedirs(_log_dir, exist_ok=True)
    _file_handler = RotatingFileHandler(
        os.path.join(_log_dir, "app.log"),
        maxBytes=10 * 1024 * 1024,  # 10 MB per file
        backupCount=5,
        encoding="utf-8",
    )
    _file_handler.setFormatter(_log_fmt)
    _handlers.append(_file_handler)
except OSError:
    pass  # log dir not writable — fall back to stdout only
logging.basicConfig(level=logging.INFO, handlers=_handlers)
logger = logging.getLogger("doccompare")

app = Flask(__name__)

# Trust one reverse-proxy hop (Render, Heroku, nginx) so request.remote_addr
# reflects the real client IP rather than the load-balancer address.
try:
    _proxy_count = int(os.environ.get("PROXY_COUNT", "1"))
    if _proxy_count < 0 or _proxy_count > 10:
        raise ValueError("out of range")
except (ValueError, TypeError):
    logger.warning("PROXY_COUNT invalid — defaulting to 1")
    _proxy_count = 1
if _proxy_count > 0:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_proxy_count, x_proto=_proxy_count, x_host=_proxy_count)

def _load_or_create_secret_key():
    """Return a stable random session key persisted to instance/.secret_key.

    Used only when SECRET_KEY env is absent. Persisting (rather than a random
    per-process key) keeps sessions valid across both gunicorn workers and
    across restarts, while removing the predictable hard-coded key. Returns None
    if the filesystem is not writable (caller falls back). O_EXCL avoids a race
    when workers start concurrently.
    """
    import secrets as _secrets
    path = os.path.join("instance", ".secret_key")
    try:
        os.makedirs("instance", exist_ok=True)
        if os.path.exists(path):
            with open(path) as _f:
                k = _f.read().strip()
                if k:
                    return k
        key = _secrets.token_hex(32)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as _f:
                _f.write(key)
            return key
        except FileExistsError:                       # another worker won the race
            with open(path) as _f:
                return _f.read().strip() or key
    except Exception:
        return None


_secret_key = os.environ.get("SECRET_KEY", "")
if not _secret_key:
    _persisted = _load_or_create_secret_key()
    if _persisted:
        logger.critical(
            "SECRET_KEY env not set — using a persisted random key (instance/.secret_key). "
            "Set SECRET_KEY in the environment for stable, secure sessions."
        )
        _secret_key = _persisted
    elif os.environ.get("DATABASE_URL"):
        # Production (PostgreSQL) — refuse to boot with a publicly-known key.
        raise SystemExit(
            "FATAL: SECRET_KEY env not set and key file unwritable in a production "
            "deployment (DATABASE_URL is set). Set SECRET_KEY in the environment."
        )
    else:
        logger.critical(
            "SECRET_KEY env not set and key file unwritable — falling back to an INSECURE "
            "built-in key. Set SECRET_KEY in the environment immediately."
        )
        _secret_key = "doccompare-local-secret-2024"
app.secret_key = _secret_key

# Katalog uploadów: preferuj trwały wolumen, by pliki (np. bajty masterów do
# podglądu/porównania) NIE ginęły przy każdym redeployu kontenera.
#  1) jawny UPLOAD_FOLDER z env (np. ustawiony w Coolify/Render), albo
#  2) /data/uploads, jeśli istnieje trwały mount /data, albo
#  3) fallback 'uploads' (efemeryczny — tylko gdy nie ma trwałego wolumenu).
_upload_folder = os.environ.get("UPLOAD_FOLDER") or (
    "/data/uploads" if os.path.isdir("/data") else "uploads")
try:
    os.makedirs(_upload_folder, exist_ok=True)
except OSError:
    _upload_folder = "uploads"
    try:
        os.makedirs(_upload_folder, exist_ok=True)
    except OSError:
        pass
app.config["UPLOAD_FOLDER"] = _upload_folder
app.config["LIBRARY_FOLDER"] = os.environ.get("LIBRARY_FOLDER", "library")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
# Secure session cookie + HSTS. Domyślnie WŁĄCZONE w produkcji (DATABASE_URL
# ustawione — Coolify/Render serwują po HTTPS), żeby zapomnienie FORCE_HTTPS nie
# wysyłało ciasteczka sesji bez flagi Secure ani nie pomijało HSTS. Jawny
# FORCE_HTTPS nadpisuje auto-detekcję w obie strony (0/false wymusza wyłączenie,
# np. do lokalnego testu prod-configu po HTTP).
_force_https_env = os.environ.get("FORCE_HTTPS", "").strip().lower()
if _force_https_env in ("true", "1", "yes", "on"):
    _is_https = True
elif _force_https_env in ("false", "0", "no", "off"):
    _is_https = False
else:
    _is_https = bool(os.environ.get("DATABASE_URL"))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
app.config["SESSION_COOKIE_SECURE"] = _is_https

# ── Blueprinty (trasy wydzielone z app.py) ────────────────────────────────────
from blueprints.auth import bp as _auth_bp
from blueprints.incoterms import bp as _incoterms_bp, ensure_incoterms_table as _ensure_incoterms_table
from blueprints.translation_dict import bp as _translation_dict_bp
from blueprints.transit_countries import bp as _transit_bp, list_countries as _list_transit_countries
from blueprints.palviz import bp as _palviz_bp
from blueprints.suppliers import bp as _suppliers_bp
from blueprints.warehouse import bp as _warehouse_bp
from blueprints.kolejka import bp as _kolejka_bp
from blueprints.api_invoices import bp as _api_invoices_bp
from invoice_routes import invoice_bp
app.register_blueprint(_auth_bp)
app.register_blueprint(_incoterms_bp)
app.register_blueprint(_translation_dict_bp)
app.register_blueprint(_transit_bp)
app.register_blueprint(_palviz_bp)
app.register_blueprint(_suppliers_bp)
app.register_blueprint(_warehouse_bp)
app.register_blueprint(_kolejka_bp)
app.register_blueprint(invoice_bp)
app.register_blueprint(_api_invoices_bp)


@app.teardown_appcontext
def _auto_close_dbs(exc):
    """Close any DB connections that routes opened but didn't close (e.g. on exception)."""
    for _db in g.pop("_open_dbs", []):
        try:
            _db.close()
        except Exception:
            pass


@app.after_request
def _set_security_headers(response):
    response.headers.pop("Server", None)
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-XSS-Protection", "1; mode=block")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://unpkg.com https://cdn.tailwindcss.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.tailwindcss.com; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "img-src 'self' data: blob: https://*.tile.openstreetmap.org; "
        "connect-src 'self'; "
        "frame-ancestors 'self';"
    )
    response.headers.setdefault(
        "Permissions-Policy",
        "geolocation=(), camera=(), microphone=(), payment=()"
    )
    # Instruct caches to vary on Accept-Language and Cookie so localised pages
    # aren't served to the wrong user/language.
    if response.content_type and "text/html" in response.content_type:
        existing_vary = response.headers.get("Vary", "")
        extras = [v for v in ("Accept-Language", "Cookie") if v.lower() not in existing_vary.lower()]
        if extras:
            new_vary = ", ".join(filter(None, [existing_vary] + extras))
            response.headers["Vary"] = new_vary
        if "user_id" in session:
            response.headers.setdefault("Cache-Control", "no-cache, no-store, must-revalidate")
            response.headers.setdefault("Pragma", "no-cache")
    if _is_https:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload")
    return response


# ── Sentry ────────────────────────────────────────────────────────────────────
_sentry_dsn = os.environ.get("SENTRY_DSN", "")
if _sentry_dsn:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        sentry_sdk.init(
            dsn=_sentry_dsn,
            integrations=[FlaskIntegration()],
            traces_sample_rate=0.2,
            environment=os.environ.get("RENDER_SERVICE_NAME", "local"),
        )
        logger.info("Sentry initialized")
    except ImportError:
        logger.warning("sentry-sdk not installed — Sentry disabled")

# ── Shipment document storage path ───────────────────────────────────────────
# Domyślnie na TRWAŁY dysk (/data) jeśli jest zamontowany (Render persistent disk),
# inaczej lokalnie w instance/. Inaczej pliki dostaw znikają przy każdym redeployu
# (rekordy w bazie zostają → kafelki widać, ale pliku na dysku brak).
_DATA_DIR_DEFAULT = os.environ.get("DATA_DIR", "/data")
_SHIPMENT_DOCS_PATH = os.path.abspath(os.environ.get(
    "SHIPMENT_DOCS_PATH",
    os.path.join(_DATA_DIR_DEFAULT, "shipment_docs")
    if os.path.isdir(_DATA_DIR_DEFAULT)
    else os.path.join(os.path.dirname(__file__), "instance", "shipment_docs")
))


# _ts_ago moved to core/timeutil.py (Phase 3, REFACTOR-01) — imported below as _ts_ago


def _validate_startup_config() -> None:
    """Validate critical configuration at startup and log errors/warnings."""
    _db_url = os.environ.get("DATABASE_URL", "")
    errors: list[str] = []
    warnings: list[str] = []

    _known_insecure = {"", "dev-secret", "doccompare-local-secret-2024"}
    if not os.environ.get("SECRET_KEY") or os.environ.get("SECRET_KEY") in _known_insecure:
        if _db_url:
            errors.append("SECRET_KEY must be set explicitly in production (DATABASE_URL is set)")
        else:
            warnings.append("SECRET_KEY not set — sessions are forgeable; set SECRET_KEY env var")

    upload_folder = app.config.get("UPLOAD_FOLDER", "uploads")
    if _db_url and not str(upload_folder).startswith("/data"):
        warnings.append(
            f"UPLOAD_FOLDER={upload_folder!r} may be ephemeral in container deployments — consider /data/uploads"
        )

    for _e in errors:
        logger.error("STARTUP CONFIG ERROR: %s", _e)
    for _w in warnings:
        logger.warning("STARTUP CONFIG WARNING: %s", _w)

    # In production (DATABASE_URL set) a misconfigured SECRET_KEY is fatal — refuse
    # to boot rather than serve forgeable sessions.
    if _db_url and errors:
        raise SystemExit("FATAL startup config: " + "; ".join(errors))


_validate_startup_config()

# Login + forgot-password rate-limiter state moved to blueprints/auth.py (Phase 3, REFACTOR-01).

# Per-user rate limiting for expensive AI/comparison endpoints
# { user_id: [timestamp, ...] }
_api_attempts: dict = {}
_api_attempts_lock = threading.Lock()

# Async job store — backed by the DB so job state is shared across gunicorn
# workers (a client polling worker B sees a job created by worker A) and
# survives worker recycling (max_requests).
# Row shape: {id, user_id, status, step, step_label, pct, result(JSON), error,
#             created_at, done_at}
# Used by /api/compare-async, /api/compare-async/<job_id>, /api/compare-async/<job_id>/events
_ASYNC_JOB_TTL = 3600        # seconds before a completed job is evicted
_ASYNC_JOB_STALE_TTL = 7200  # 2 h — in-progress jobs older than this are treated as crashed
_async_jobs_table_ready = False

def _ensure_async_jobs_table(db) -> None:
    global _async_jobs_table_ready
    if _async_jobs_table_ready:
        return
    db.execute("""CREATE TABLE IF NOT EXISTS async_jobs (
        id TEXT PRIMARY KEY,
        user_id INTEGER,
        status TEXT NOT NULL DEFAULT 'pending',
        step INTEGER DEFAULT 0,
        step_label TEXT DEFAULT '',
        pct INTEGER DEFAULT 0,
        result TEXT,
        error TEXT DEFAULT '',
        created_at DOUBLE PRECISION,
        done_at DOUBLE PRECISION
    )""")
    db.commit()
    _async_jobs_table_ready = True

def _async_job_create(user_id: int) -> str:
    import uuid
    jid = uuid.uuid4().hex[:16]
    db = get_db()
    try:
        _ensure_async_jobs_table(db)
        db.execute(
            "INSERT INTO async_jobs(id,user_id,status,step,step_label,pct,result,error,created_at) "
            "VALUES(?,?,'pending',0,'Oczekiwanie…',0,NULL,'',?)",
            (jid, user_id, time.time()),
        )
        db.commit()
    finally:
        db.close()
    return jid

def _async_job_set(jid: str, status: str, result=None, error: str = "",
                   step: int = 0, step_label: str = "", pct: int = 0):
    done_at = time.time() if status in ("done", "error") else None
    if status == "done":
        pct = 100
    db = get_db()
    try:
        _ensure_async_jobs_table(db)
        if result is not None:
            db.execute(
                "UPDATE async_jobs SET status=?, error=?, step=?, step_label=?, pct=?, "
                "result=?, done_at=COALESCE(?, done_at) WHERE id=?",
                (status, error, step, step_label or status, pct,
                 json.dumps(result, ensure_ascii=False, default=str), done_at, jid),
            )
        else:
            db.execute(
                "UPDATE async_jobs SET status=?, error=?, step=?, step_label=?, pct=?, "
                "done_at=COALESCE(?, done_at) WHERE id=?",
                (status, error, step, step_label or status, pct, done_at, jid),
            )
        db.commit()
    finally:
        db.close()

def _async_job_get(jid: str) -> dict | None:
    db = get_db()
    try:
        _ensure_async_jobs_table(db)
        row = db.execute("SELECT * FROM async_jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            return None
        entry = dict(row)
        now = time.time()
        # Evict completed/errored jobs past their TTL
        if entry.get("done_at") and (now - entry["done_at"]) > _ASYNC_JOB_TTL:
            db.execute("DELETE FROM async_jobs WHERE id=?", (jid,)); db.commit()
            return None
        # Evict stale in-progress jobs (worker thread likely crashed)
        if entry.get("status") not in ("done", "error") and \
                (now - (entry.get("created_at") or now)) > _ASYNC_JOB_STALE_TTL:
            db.execute("DELETE FROM async_jobs WHERE id=?", (jid,)); db.commit()
            return None
        # Deserialize result JSON back to a dict (matches prior in-memory shape)
        if entry.get("result"):
            try:
                entry["result"] = json.loads(entry["result"])
            except (ValueError, TypeError):
                entry["result"] = None
        else:
            entry["result"] = None
        return entry
    finally:
        db.close()

# KPI data cache — keyed by (uid_key, period, user_filter), TTL 5 minutes
_kpi_cache: dict = {}
_kpi_cache_lock = threading.Lock()

def _kpi_cache_get(key: tuple):
    with _kpi_cache_lock:
        entry = _kpi_cache.get(key)
        if entry and (time.time() - entry[0]) < KPI_CACHE_TTL:
            return entry[1]
    return None

def _kpi_cache_set(key: tuple, data):
    with _kpi_cache_lock:
        _kpi_cache[key] = (time.time(), data)

def _kpi_cache_invalidate():
    """Call after any comparison is written to keep KPI fresh."""
    with _kpi_cache_lock:
        _kpi_cache.clear()

_api_rate_table_ready = False

def _ensure_api_rate_table(db) -> None:
    global _api_rate_table_ready
    if _api_rate_table_ready:
        return
    db.execute("""CREATE TABLE IF NOT EXISTS api_rate_events (
        user_id INTEGER NOT NULL,
        created_at DOUBLE PRECISION NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_api_rate_user_ts ON api_rate_events(user_id, created_at)")
    db.commit()
    _api_rate_table_ready = True

def _check_and_record_api_rate(user_id: int) -> bool:
    """Check the per-user API rate limit and record the call. Returns True if limited.

    DB-backed (table api_rate_events) so the limit is shared across gunicorn
    workers instead of being counted per-process (which let the effective limit
    scale with WEB_CONCURRENCY). Falls back to the in-memory dict on DB errors."""
    now = time.time()
    cutoff = now - API_RATE_WINDOW
    try:
        db = get_db()
        try:
            _ensure_api_rate_table(db)
            db.execute("DELETE FROM api_rate_events WHERE user_id=? AND created_at < ?",
                       (user_id, cutoff))
            count = db.execute(
                "SELECT COUNT(*) FROM api_rate_events WHERE user_id=? AND created_at >= ?",
                (user_id, cutoff)).fetchone()[0]
            if count >= API_RATE_MAX:
                db.commit()
                return True
            db.execute("INSERT INTO api_rate_events(user_id, created_at) VALUES(?,?)",
                       (user_id, now))
            db.commit()
            return False
        finally:
            db.close()
    except Exception:
        with _api_attempts_lock:
            ts = [t for t in _api_attempts.get(user_id, []) if now - t < API_RATE_WINDOW]
            if len(ts) >= API_RATE_MAX:
                _api_attempts[user_id] = ts
                return True
            ts.append(now)
            _api_attempts[user_id] = ts[-(API_RATE_MAX + 5):]
            return False

# ── Upload cleanup thread ──────────────────────────────────────────────────────
def _cleanup_uploads():
    """Delete upload files older than 24 h. Runs every 30 min in background."""
    upload_dir = app.config["UPLOAD_FOLDER"]
    cutoff = 24 * 3600
    while True:
        time.sleep(1800)
        try:
            now = time.time()
            for fname in os.listdir(upload_dir):
                fpath = os.path.join(upload_dir, fname)
                try:
                    if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > cutoff:
                        try:
                            os.remove(fpath)
                        except FileNotFoundError:
                            pass
                except OSError:
                    pass
        except Exception as _exc:
            logger.warning("Upload cleanup error: %s", _exc)

_cleanup_thread = threading.Thread(target=_cleanup_uploads, daemon=True)
_cleanup_thread.start()


# ── Automatic SQLite backup (daily) ───────────────────────────────────────────

def _backup_sqlite_once(backup_dir: str) -> str | None:
    """Copy the SQLite DB to backup_dir/doccompare_YYYYMMDD_HHMMSS.db using the
    SQLite online-backup API (safe while app is running).  Returns backup path
    on success or None on failure / non-SQLite environments."""
    from db import DATABASE_URL, SQLITE_PATH
    if DATABASE_URL:
        return None  # PostgreSQL — backup is the responsibility of the managed DB service
    import sqlite3 as _sq
    try:
        ts = time.strftime("%Y%m%d_%H%M%S")
        os.makedirs(backup_dir, exist_ok=True)
        dest = os.path.join(backup_dir, f"doccompare_{ts}.db")
        src = _sq.connect(SQLITE_PATH, timeout=5)
        dst = _sq.connect(dest)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        # Keep only the 7 most recent backups
        backups = sorted(
            f for f in os.listdir(backup_dir) if f.startswith("doccompare_") and f.endswith(".db")
        )
        for old in backups[:-7]:
            try: os.remove(os.path.join(backup_dir, old))
            except OSError: pass
        logger.info("SQLite backup created: %s", dest)
        return dest
    except Exception as exc:
        logger.warning("SQLite backup failed: %s", exc)
        return None


def _auto_backup_loop():
    """Daemon thread: run a daily SQLite backup."""
    backup_dir = os.environ.get("BACKUP_DIR", os.path.join(os.path.dirname(__file__) or ".", "backups"))
    interval = int(os.environ.get("BACKUP_INTERVAL_HOURS", "24")) * 3600
    # Run one backup immediately at startup (avoids 24h gap on first deploy)
    time.sleep(30)  # small delay so DB is fully initialized
    _backup_sqlite_once(backup_dir)
    while True:
        time.sleep(interval)
        _backup_sqlite_once(backup_dir)


_backup_thread = threading.Thread(target=_auto_backup_loop, daemon=True)
_backup_thread.start()

from pdf_validation import (
    ALLOWED_PDF_MIMES as _ALLOWED_PDF_MIMES,
    validate_pdf_upload as _validate_pdf_upload,
)

# JSON body validation ─────────────────────────────────────────────────────────

def _validate_json_body(data: dict, schema: dict) -> str | None:
    """Lightweight JSON body validator.

    schema example:
        {"username": (str, True), "role": (str, False), "is_active": (bool, False)}
    Returns an error string, or None if valid.
    """
    if data is None:
        return "Wymagane dane JSON w treści żądania"
    for field, (ftype, required) in schema.items():
        if field not in data:
            if required:
                return f"Wymagane pole: '{field}'"
        else:
            val = data[field]
            if val is not None and not isinstance(val, ftype):
                # Accept int where float expected
                if ftype is float and isinstance(val, int):
                    continue
                type_name = ftype.__name__ if hasattr(ftype, "__name__") else str(ftype)
                return f"Pole '{field}' musi być typu {type_name}"
    return None


def _decrypt_pdf_inplace(path: str) -> None:
    """Jeśli PDF jest zaszyfrowany PUSTYM hasłem użytkownika (tylko ograniczenia
    właściciela), zapisz w to samo miejsce odszyfrowaną kopię — by wszystkie
    czytniki (pdfplumber/pdfminer/camelot) działały. Pliki niezaszyfrowane lub
    wymagające realnego hasła pozostają nietknięte."""
    try:
        import fitz
        doc = fitz.open(path)
        try:
            if not getattr(doc, "is_encrypted", False) or doc.needs_pass:
                return  # niezaszyfrowany albo wymaga realnego hasła → nie ruszamy
            tmp = path + ".dec.pdf"
            doc.save(tmp, encryption=fitz.PDF_ENCRYPT_NONE)
        finally:
            doc.close()
        try:
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try: os.remove(tmp)
                except OSError: pass
    except Exception as _e:
        logger.debug("PDF decrypt skip for %s: %s", path, _e)

# role_level / can_see_all / can_delete / is_admin → core.security (import na górze)


# ─────────────────────────────────────────────────────────────────────────────
# BAZA DANYCH
# ─────────────────────────────────────────────────────────────────────────────


def _db_run_migrations(db):
    # Migracje
    for migration in [
        "ALTER TABLE users ADD COLUMN last_login TEXT",
        "ALTER TABLE users ADD COLUMN email TEXT",
        "ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1",
        """CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )""",
        "ALTER TABLE suppliers ADD COLUMN column_mapping_json TEXT DEFAULT '{}'",
        "ALTER TABLE suppliers ADD COLUMN detect_keywords_json TEXT DEFAULT '[]'",
        """CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            UNIQUE(category, key)
        )""",
        """CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            country TEXT DEFAULT 'XX',
            currency TEXT DEFAULT 'USD',
            language TEXT DEFAULT 'EN',
            price_rounding INTEGER DEFAULT 4,
            po_rounding INTEGER DEFAULT 2,
            payment_terms_json TEXT DEFAULT '{}',
            date_format TEXT DEFAULT '%Y/%m/%d',
            known_issues_json TEXT DEFAULT '[]',
            price_tolerance_pct REAL DEFAULT 0.5,
            qty_tolerance_pct REAL DEFAULT 0.0,
            detect_keywords_json TEXT DEFAULT '[]',
            notes TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL,
            username TEXT,
            detail TEXT DEFAULT '',
            ip TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )""",
        "ALTER TABLE audit_log ADD COLUMN duration_ms INTEGER DEFAULT NULL",
        "ALTER TABLE audit_log ADD COLUMN extra TEXT DEFAULT NULL",
        "CREATE INDEX IF NOT EXISTS idx_audit_ip_event ON audit_log(ip, event, created_at)",
        # Lokalna tabela referencyjna taryfy CN/TARIC (import nomenklatury UE)
        """CREATE TABLE IF NOT EXISTS cn_reference (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            description TEXT DEFAULT '',
            duty_rate TEXT DEFAULT '',
            restrictions TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            updated_at TEXT DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_cn_reference_code ON cn_reference(code)",
        # comparisons — dodatkowe kolumny (migrate_db.py sync)
        "ALTER TABLE comparisons ADD COLUMN po_number TEXT DEFAULT ''",
        "ALTER TABLE comparisons ADD COLUMN supplier_code TEXT DEFAULT ''",
        "ALTER TABLE comparisons ADD COLUMN total_a TEXT DEFAULT ''",
        "ALTER TABLE comparisons ADD COLUMN total_b TEXT DEFAULT ''",
        "ALTER TABLE comparisons ADD COLUMN comment TEXT DEFAULT ''",
        "ALTER TABLE comparisons ADD COLUMN approval_status TEXT DEFAULT 'pending'",
        "ALTER TABLE comparisons ADD COLUMN approved_by INTEGER",
        "ALTER TABLE comparisons ADD COLUMN approved_at TEXT",
        "ALTER TABLE comparisons ADD COLUMN is_public INTEGER DEFAULT 0",
        # users — dodatkowe kolumny
        "ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1",
        # suppliers — dodatkowe kolumny
        "ALTER TABLE suppliers ADD COLUMN synonyms_json TEXT DEFAULT '[]'",
        "ALTER TABLE suppliers ADD COLUMN pi_column_mapping_json TEXT DEFAULT '{}'",
        "ALTER TABLE suppliers ADD COLUMN po_column_mapping_json TEXT DEFAULT '{}'",
        "ALTER TABLE suppliers ADD COLUMN ref_format_pi TEXT DEFAULT 'standard'",
        "ALTER TABLE suppliers ADD COLUMN ref_format_po TEXT DEFAULT 'standard'",
        "ALTER TABLE suppliers ADD COLUMN wizard_completed INTEGER DEFAULT 0",
        "ALTER TABLE suppliers ADD COLUMN profile_updated_at TEXT",
        "ALTER TABLE suppliers ADD COLUMN profile_updated_by TEXT",
        "ALTER TABLE suppliers ADD COLUMN profile_version INTEGER DEFAULT 1",
        "ALTER TABLE suppliers ADD COLUMN header_patterns_json TEXT DEFAULT '{}'",
        "ALTER TABLE suppliers ADD COLUMN ignore_fields_json TEXT DEFAULT '[]'",
        "ALTER TABLE suppliers ADD COLUMN custom_rules_json TEXT DEFAULT '[]'",
        """CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'inne',
            status TEXT NOT NULL DEFAULT 'nowe',
            admin_response TEXT DEFAULT '',
            resolved_by TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )""",
        """CREATE TABLE IF NOT EXISTS artwork_batch_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            status TEXT DEFAULT 'pending' CHECK(status IN ('pending','running','done','failed','cancelled')),
            total INTEGER DEFAULT 0 CHECK(total >= 0),
            done INTEGER DEFAULT 0 CHECK(done >= 0),
            failed INTEGER DEFAULT 0 CHECK(failed >= 0),
            pairs_json TEXT DEFAULT '[]',
            results_json TEXT DEFAULT '[]',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS api_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT (datetime('now')),
            user_id INTEGER,
            model TEXT,
            call_type TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0
        )""",
        "CREATE INDEX IF NOT EXISTS idx_comparisons_user_created ON comparisons(user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_comparisons_status ON comparisons(status)",
        "ALTER TABLE comparisons ADD COLUMN file_hash_a TEXT",
        "ALTER TABLE comparisons ADD COLUMN file_hash_b TEXT",
        "CREATE INDEX IF NOT EXISTS idx_comparisons_hashes ON comparisons(file_hash_a, file_hash_b)",
        """CREATE TABLE IF NOT EXISTS comparison_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            page_type TEXT NOT NULL DEFAULT 'compare',
            is_public INTEGER DEFAULT 0,
            supplier_code TEXT DEFAULT '',
            doc_type_a TEXT DEFAULT '',
            doc_type_b TEXT DEFAULT '',
            price_tolerance_pct REAL DEFAULT 0.5,
            qty_tolerance_pct REAL DEFAULT 0.0,
            ai_model TEXT DEFAULT 'claude-sonnet-4-6',
            use_ai INTEGER DEFAULT 1,
            column_mapping_json TEXT DEFAULT '{}',
            ignore_fields_json TEXT DEFAULT '[]',
            use_count INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(created_by) REFERENCES users(id)
        )""",
        "ALTER TABLE suppliers ADD COLUMN sad_column_mapping_json TEXT DEFAULT '{}'",
        """CREATE TABLE IF NOT EXISTS library_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rel_path TEXT NOT NULL UNIQUE,
            filename TEXT NOT NULL,
            size_bytes INTEGER DEFAULT 0,
            modified_at TEXT,
            synced_at TEXT DEFAULT (datetime('now')),
            checksum TEXT,
            lvl1 TEXT DEFAULT '',
            lvl2 TEXT DEFAULT '',
            lvl3 TEXT DEFAULT '',
            lvl4 TEXT DEFAULT '',
            lvl5 TEXT DEFAULT '',
            file_path TEXT DEFAULT '',
            thumb_data BLOB,
            z_path TEXT DEFAULT ''
        )""",
        "CREATE INDEX IF NOT EXISTS idx_library_rel_path ON library_files(rel_path)",
        "CREATE INDEX IF NOT EXISTS idx_library_lvl1 ON library_files(lvl1)",
        "ALTER TABLE library_files ADD COLUMN thumb_data BLOB",
        "ALTER TABLE library_files ADD COLUMN z_path TEXT DEFAULT ''",
        """CREATE TABLE IF NOT EXISTS ref_database (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ref_code TEXT NOT NULL,
            product_name TEXT NOT NULL,
            uploaded_by INTEGER,
            uploaded_at TEXT DEFAULT (datetime('now')),
            UNIQUE(ref_code)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_ref_database_code ON ref_database(ref_code)",
        # Soft-delete support for comparisons
        "ALTER TABLE comparisons ADD COLUMN is_deleted INTEGER DEFAULT 0",
        "ALTER TABLE comparisons ADD COLUMN deleted_at TEXT",
        "ALTER TABLE comparisons ADD COLUMN deleted_by INTEGER",
        # Approval workflow
        "ALTER TABLE comparisons ADD COLUMN approval_status TEXT DEFAULT NULL",
        "ALTER TABLE comparisons ADD COLUMN approved_by INTEGER",
        "ALTER TABLE comparisons ADD COLUMN approved_at TEXT",
        "ALTER TABLE comparisons ADD COLUMN approval_note TEXT DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS idx_comparisons_not_deleted ON comparisons(is_deleted, created_at DESC)",
        # Webhooks table
        """CREATE TABLE IF NOT EXISTS webhooks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL CHECK(url LIKE 'http://%' OR url LIKE 'https://%'),
            secret TEXT DEFAULT '',
            events TEXT DEFAULT 'comparison.completed',
            is_active INTEGER DEFAULT 1 CHECK(is_active IN (0,1)),
            created_by INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        )""",
        # Artwork profile version history
        """CREATE TABLE IF NOT EXISTS artwork_profile_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            action TEXT NOT NULL DEFAULT 'update',
            changed_by INTEGER,
            changed_by_name TEXT DEFAULT '',
            snapshot_json TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(profile_id) REFERENCES artwork_profiles(id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_artwork_profile_history_pid ON artwork_profile_history(profile_id, created_at DESC)",
        # Artwork profile indexes
        "CREATE INDEX IF NOT EXISTS idx_artwork_profiles_ean ON artwork_profiles(ean)",
        "CREATE INDEX IF NOT EXISTS idx_artwork_profiles_ref ON artwork_profiles(ref_code)",
        "CREATE INDEX IF NOT EXISTS idx_artwork_profiles_active ON artwork_profiles(is_active)",
        "CREATE INDEX IF NOT EXISTS idx_artwork_profile_fields_pid ON artwork_profile_fields(profile_id)",
        # API usage index
        "CREATE INDEX IF NOT EXISTS idx_api_usage_user_created ON api_usage(user_id, created_at DESC)",
        # Artwork field comments — per-field annotations on comparison reports
        """CREATE TABLE IF NOT EXISTS artwork_field_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comparison_id INTEGER NOT NULL,
            field_name TEXT NOT NULL,
            comment TEXT NOT NULL DEFAULT '',
            created_by INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(comparison_id, field_name),
            FOREIGN KEY(comparison_id) REFERENCES comparisons(id)
        )""",
        """CREATE TABLE IF NOT EXISTS translation_dictionary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            term_original TEXT NOT NULL,
            lang_from TEXT NOT NULL DEFAULT 'en',
            term_translated TEXT NOT NULL,
            lang_to TEXT NOT NULL DEFAULT 'pl',
            context TEXT NOT NULL DEFAULT 'all',
            notes TEXT DEFAULT '',
            created_by INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(term_original, lang_from, lang_to, context)
        )""",
        """CREATE TABLE IF NOT EXISTS sad_field_mapping (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sad_field_code TEXT NOT NULL,
            sad_field_name_pl TEXT NOT NULL,
            sad_field_name_en TEXT DEFAULT '',
            mapped_to_field TEXT DEFAULT '',
            mapped_to_doctype TEXT DEFAULT 'CI',
            data_type TEXT DEFAULT 'text',
            description TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(sad_field_code)
        )""",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('1', 'Deklaracja / typ', 'Declaration / type', 'doc_type', 'CI', 'text', 'Pole 1 SAD: typ deklaracji')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('2', 'Nadawca / eksporter', 'Sender / exporter', 'seller_name', 'CI', 'text', 'Pole 2 SAD: nazwa i adres eksportera')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('3', 'Formularze', 'Forms', '', '', 'text', 'Pole 3 SAD: liczba formularzy')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('4', 'Wykazy', 'Lists', '', '', 'text', 'Pole 4 SAD: wykazy załadunkowe')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('5', 'Pozycje', 'Items', '', '', 'number', 'Pole 5 SAD: łączna liczba pozycji')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('6', 'Opakowania', 'Packages', 'total_packages', 'PL', 'number', 'Pole 6 SAD: łączna liczba opakowań')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('7', 'Nr referencyjny', 'Reference number', '', '', 'text', 'Pole 7 SAD: numer referencyjny')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('8', 'Odbiorca', 'Consignee', 'buyer_name', 'CI', 'text', 'Pole 8 SAD: nazwa i adres odbiorcy')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('11', 'Kraj handlujący', 'Trading country', 'origin_country', 'CI', 'text', 'Pole 11 SAD: kraj handlujący')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('12', 'Wartość celna', 'Customs value', 'total_net', 'CI', 'number', 'Pole 12 SAD: wartość celna')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('14', 'Zgłaszający/przedstawiciel', 'Declarant', '', '', 'text', 'Pole 14 SAD: zgłaszający')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('15', 'Kraj wysyłki', 'Country of dispatch', '', '', 'text', 'Pole 15 SAD: kraj wysyłki')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('17', 'Kraj przeznaczenia', 'Country of destination', '', '', 'text', 'Pole 17 SAD: kraj przeznaczenia')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('18', 'Środek transportu', 'Transport means', '', '', 'text', 'Pole 18 SAD: środek transportu przy wyjściu')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('20', 'Warunki dostawy', 'Delivery terms', 'incoterms', 'CI', 'text', 'Pole 20 SAD: warunki dostawy (Incoterms)')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('21', 'Aktywny środek transportu', 'Active transport', '', '', 'text', 'Pole 21 SAD: aktywny środek transportu')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('22', 'Waluta i wartość', 'Currency and value', 'currency', 'CI', 'text', 'Pole 22 SAD: waluta i wartość faktury')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('23', 'Kurs wymiany', 'Exchange rate', '', '', 'number', 'Pole 23 SAD: kurs wymiany waluty')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('24', 'Rodzaj transakcji', 'Transaction type', '', '', 'text', 'Pole 24 SAD: rodzaj transakcji')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('25', 'Rodzaj transportu', 'Transport mode', '', '', 'text', 'Pole 25 SAD: rodzaj transportu na granicy')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('26', 'Wewnętrzny rodzaj transportu', 'Inland transport mode', '', '', 'text', 'Pole 26 SAD: wewnętrzny rodzaj transportu')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('28', 'Rachunek finansowy', 'Financial account', '', '', 'text', 'Pole 28 SAD: rachunek finansowy')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('29', 'Urząd wyjścia', 'Exit office', '', '', 'text', 'Pole 29 SAD: urząd celny wyjścia')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('30', 'Lokalizacja towaru', 'Location of goods', '', '', 'text', 'Pole 30 SAD: lokalizacja towarów')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('31', 'Opakowania i opis', 'Packages and description', 'description', 'CI', 'text', 'Pole 31 SAD: opakowania i opis towaru')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('32', 'Nr pozycji', 'Item number', 'ref', 'CI', 'text', 'Pole 32 SAD: numer pozycji')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('33', 'Kod towaru', 'Commodity code', 'tariff_code', 'CI', 'code', 'Pole 33 SAD: kod taryfy celnej CN')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('34', 'Kod kraju', 'Country code', 'origin_country', 'CI', 'text', 'Pole 34 SAD: kod kraju pochodzenia')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('35', 'Masa brutto', 'Gross mass', 'gross_weight', 'PL', 'number', 'Pole 35 SAD: masa brutto (kg)')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('36', 'Preferencja', 'Preference', '', '', 'text', 'Pole 36 SAD: preferencja celna')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('37', 'Procedura', 'Procedure', '', '', 'text', 'Pole 37 SAD: procedura celna')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('38', 'Masa netto', 'Net mass', 'net_weight', 'PL', 'number', 'Pole 38 SAD: masa netto (kg)')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('39', 'Kontyngent', 'Quota', '', '', 'text', 'Pole 39 SAD: kontyngent')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('40', 'Deklaracja skrócona', 'Summary declaration', 'po_number', 'PO', 'text', 'Pole 40 SAD: poprzedni dokument / PO')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('41', 'Uzupełniające jednostki', 'Supplementary units', 'qty', 'CI', 'number', 'Pole 41 SAD: uzupełniające jednostki miary')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('42', 'Cena pozycji', 'Item price', 'price', 'CI', 'number', 'Pole 42 SAD: cena pozycji')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('43', 'Metoda wartości', 'Valuation method', '', '', 'text', 'Pole 43 SAD: metoda wartości celnej')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('44', 'Dodatkowe informacje', 'Additional info', 'notes', 'CI', 'text', 'Pole 44 SAD: dodatkowe informacje / dokumenty')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('45', 'Korekta', 'Adjustment', '', '', 'number', 'Pole 45 SAD: korekta')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('46', 'Wartość statystyczna', 'Statistical value', '', '', 'number', 'Pole 46 SAD: wartość statystyczna')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('47', 'Obliczanie opłat', 'Calculation of taxes', '', '', 'text', 'Pole 47 SAD: obliczanie opłat')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('48', 'Odroczenie płatności', 'Deferred payment', '', '', 'text', 'Pole 48 SAD: odroczenie płatności')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('49', 'Magazyn celny', 'Warehouse', '', '', 'text', 'Pole 49 SAD: magazyn celny')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('50', 'Zleceniodawca', 'Principal', '', '', 'text', 'Pole 50 SAD: zleceniodawca')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('51', 'Planowane urzędy tranzytu', 'Intended offices of transit', '', '', 'text', 'Pole 51 SAD: planowane urzędy tranzytu')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('52', 'Gwarancja', 'Guarantee', '', '', 'text', 'Pole 52 SAD: gwarancja')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('53', 'Urząd przeznaczenia', 'Office of destination', '', '', 'text', 'Pole 53 SAD: urząd celny przeznaczenia')",
        "INSERT OR IGNORE INTO sad_field_mapping(sad_field_code, sad_field_name_pl, sad_field_name_en, mapped_to_field, mapped_to_doctype, data_type, description) VALUES ('54', 'Miejsce i data', 'Place and date', '', '', 'text', 'Pole 54 SAD: miejsce i data, podpis')",
        # ── Dane referencyjne: kursy walut ──────────────────────────────────────
        """CREATE TABLE IF NOT EXISTS currency_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    currency_code TEXT NOT NULL,
    currency_name TEXT NOT NULL,
    rate REAL NOT NULL,
    rate_date TEXT NOT NULL,
    source TEXT DEFAULT 'NBP',
    fetched_at TEXT DEFAULT (datetime('now')),
    UNIQUE(currency_code, rate_date)
)""",
        "CREATE INDEX IF NOT EXISTS idx_currency_rates_date ON currency_rates(currency_code, rate_date DESC)",
        # ── Dane referencyjne: baza produktów ───────────────────────────────────
        """CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_code TEXT NOT NULL UNIQUE,
    product_name TEXT NOT NULL,
    ean TEXT DEFAULT '',
    unit TEXT DEFAULT 'szt',
    tariff_cn TEXT DEFAULT '',
    description TEXT DEFAULT '',
    supplier_codes TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
)""",
        "CREATE INDEX IF NOT EXISTS idx_products_ref ON products(ref_code)",
        "CREATE INDEX IF NOT EXISTS idx_products_ean ON products(ean)",
        # ── Dane referencyjne: śledzenie kontenerów ──────────────────────────────
        """CREATE TABLE IF NOT EXISTS container_tracking (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container_number TEXT NOT NULL,
    carrier TEXT DEFAULT '',
    status TEXT DEFAULT 'unknown',
    origin_port TEXT DEFAULT '',
    dest_port TEXT DEFAULT '',
    etd TEXT DEFAULT '',
    eta TEXT DEFAULT '',
    last_event TEXT DEFAULT '',
    last_event_date TEXT DEFAULT '',
    events_json TEXT DEFAULT '[]',
    raw_json TEXT DEFAULT '{}',
    last_checked TEXT DEFAULT (datetime('now')),
    created_by INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(container_number)
)""",
        "CREATE INDEX IF NOT EXISTS idx_container_tracking_number ON container_tracking(container_number)",
        # container_tracking — ryzyko postojowe (demurrage/detention): ręczne
        # nadpisania kamieni milowych + ostatni zgłoszony poziom (dedup alertów).
        # MUSZĄ być po CREATE container_tracking, inaczej na świeżej bazie ALTER
        # poleci "no such table" i kolumny nie powstaną.
        "ALTER TABLE container_tracking ADD COLUMN discharge_date TEXT DEFAULT ''",
        "ALTER TABLE container_tracking ADD COLUMN gate_out_date TEXT DEFAULT ''",
        "ALTER TABLE container_tracking ADD COLUMN empty_return_date TEXT DEFAULT ''",
        "ALTER TABLE container_tracking ADD COLUMN demurrage_notified_level TEXT DEFAULT ''",
        # ── Transport: kolejka kontenerów ────────────────────────────────────────
        """CREATE TABLE IF NOT EXISTS transport_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container_number TEXT NOT NULL,
    po_numbers TEXT DEFAULT '',
    etd TEXT DEFAULT '',
    eta TEXT DEFAULT '',
    origin_port TEXT DEFAULT '',
    dest_port TEXT DEFAULT '',
    unload_location TEXT DEFAULT '',
    carrier TEXT DEFAULT '',
    customs_status TEXT DEFAULT 'oczekuje' CHECK(customs_status IN ('oczekuje','w_odprawie','zatwierdzone','odrzucone','wydane')),
    urgency TEXT DEFAULT 'normalne' CHECK(urgency IN ('pilne','normalne','spokojnie')),
    notes TEXT DEFAULT '',
    created_by INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
)""",
        "CREATE INDEX IF NOT EXISTS idx_transport_queue_eta ON transport_queue(eta)",
        # ── Transport: spedytorzy ─────────────────────────────────────────────────
        """CREATE TABLE IF NOT EXISTS freight_forwarders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    company TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    email TEXT DEFAULT '',
    nip TEXT DEFAULT '',
    address TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
)""",
        # ── Transport: kierowcy ───────────────────────────────────────────────────
        """CREATE TABLE IF NOT EXISTS drivers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    truck_plate TEXT DEFAULT '',
    forwarder_id INTEGER,
    notes TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(forwarder_id) REFERENCES freight_forwarders(id)
)""",
        # ── Transport: powiadomienia kierowców ────────────────────────────────────
        """CREATE TABLE IF NOT EXISTS driver_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id INTEGER NOT NULL,
    transport_queue_id INTEGER,
    container_number TEXT DEFAULT '',
    po_numbers TEXT DEFAULT '',
    load_location TEXT DEFAULT '',
    scheduled_time TEXT DEFAULT '',
    token TEXT UNIQUE NOT NULL,
    status TEXT DEFAULT 'sent' CHECK(status IN ('sent','confirmed','declined','expired')),
    sms_sent_at TEXT,
    confirmed_at TEXT,
    confirmation_note TEXT DEFAULT '',
    created_by INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(driver_id) REFERENCES drivers(id)
)""",
        "CREATE INDEX IF NOT EXISTS idx_driver_notifications_token ON driver_notifications(token)",
        "ALTER TABLE driver_notifications ADD COLUMN gate_number TEXT DEFAULT ''",
        "ALTER TABLE driver_notifications ADD COLUMN pickup_location TEXT DEFAULT ''",
        "ALTER TABLE transport_queue ADD COLUMN tracking_number TEXT DEFAULT ''",
        # ── Spedycja: zlecenie spedycyjne (Etap 2) ───────────────────────────────
        "ALTER TABLE transport_queue ADD COLUMN forwarder_id INTEGER",
        "ALTER TABLE transport_queue ADD COLUMN customs_agent TEXT DEFAULT ''",
        "ALTER TABLE transport_queue ADD COLUMN sent_to_forwarder_at TEXT DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS idx_transport_queue_forwarder ON transport_queue(forwarder_id)",
        # ── Agencja celna: portal Agencji (rola customs_agent) ──
        """CREATE TABLE IF NOT EXISTS customs_agencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    company TEXT DEFAULT '',
    email TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
)""",
        "ALTER TABLE transport_queue ADD COLUMN customs_agency_id INTEGER",
        "ALTER TABLE transport_queue ADD COLUMN sent_to_agency_at TEXT DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS idx_transport_queue_agency ON transport_queue(customs_agency_id)",
        # ── Magazyn: rejestr przyjęć kontenerów ──────────────────────────────────
        """CREATE TABLE IF NOT EXISTS warehouse_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container_number TEXT NOT NULL,
    po_numbers TEXT DEFAULT '',
    received_date TEXT DEFAULT '',
    carrier TEXT DEFAULT '',
    unload_location TEXT DEFAULT '',
    status TEXT DEFAULT 'przybyle' CHECK(status IN ('przybyle','sprawdzone','do_sap','wprowadzone_sap')),
    condition_notes TEXT DEFAULT '',
    checked_by INTEGER,
    checked_at TEXT DEFAULT '',
    sap_document TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_by INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
)""",
        "CREATE INDEX IF NOT EXISTS idx_warehouse_receipts_status ON warehouse_receipts(status, created_at DESC)",
        # ── Magazyn: rozładunek (kto, czas, narzucona norma) ──
        "ALTER TABLE warehouse_receipts ADD COLUMN unloaded_by TEXT DEFAULT ''",
        "ALTER TABLE warehouse_receipts ADD COLUMN unload_start TEXT DEFAULT ''",
        "ALTER TABLE warehouse_receipts ADD COLUMN unload_end TEXT DEFAULT ''",
        "ALTER TABLE warehouse_receipts ADD COLUMN unload_norm_min INTEGER DEFAULT 120",
        "ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'pl'",
        "ALTER TABLE users ADD COLUMN department TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN allowed_modules TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN phone TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN avatar_initials TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN forwarder_id INTEGER",
        "ALTER TABLE users ADD COLUMN customs_agency_id INTEGER",
        "ALTER TABLE kolejka_zlecenia ADD COLUMN department TEXT DEFAULT ''",
        """CREATE TABLE IF NOT EXISTS supplier_checklists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER,
    supplier_name TEXT NOT NULL DEFAULT '',
    doc_type TEXT NOT NULL,
    required INTEGER DEFAULT 1,
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
)""",
        """CREATE TABLE IF NOT EXISTS delivery_checklists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    po_number TEXT NOT NULL,
    supplier_name TEXT DEFAULT '',
    container_number TEXT DEFAULT '',
    doc_type TEXT NOT NULL,
    status TEXT DEFAULT 'brak' CHECK(status IN ('brak','oczekuje','wgrano','zatwierdzono')),
    file_path TEXT DEFAULT '',
    comparison_id INTEGER,
    notes TEXT DEFAULT '',
    due_date TEXT DEFAULT '',
    created_by INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
)""",
        "ALTER TABLE tickets ADD COLUMN comparison_id INTEGER DEFAULT NULL",
        "ALTER TABLE tickets ADD COLUMN priority TEXT DEFAULT 'normal' CHECK(priority IN ('low','normal','high','critical'))",
        """CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL DEFAULT 'system',
            title TEXT NOT NULL,
            message TEXT DEFAULT '',
            link TEXT DEFAULT '',
            is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read, created_at DESC)",
        """CREATE TABLE IF NOT EXISTS shipments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shipment_ref TEXT UNIQUE NOT NULL,
            po_number TEXT NOT NULL,
            shipment_date TEXT NOT NULL,
            supplier_name TEXT DEFAULT '',
            status TEXT DEFAULT 'nowe' CHECK(status IN
                ('nowe','potwierdzono','w_transporcie','odprawa','w_magazynie','zakonczone')),
            comparison_id INTEGER,
            transport_queue_id INTEGER,
            warehouse_receipt_id INTEGER,
            notes TEXT DEFAULT '',
            created_by INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_shipments_po ON shipments(po_number)",
        "CREATE INDEX IF NOT EXISTS idx_shipments_ref ON shipments(shipment_ref)",
        """CREATE TABLE IF NOT EXISTS shipment_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            po_number TEXT NOT NULL,
            shipment_ref TEXT DEFAULT '',
            filename TEXT NOT NULL,
            original_name TEXT NOT NULL,
            doc_type TEXT DEFAULT '',
            file_size INTEGER DEFAULT 0,
            source TEXT DEFAULT 'manual',
            uploaded_by INTEGER,
            uploaded_at TEXT DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_shipdocs_po ON shipment_documents(po_number)",
        """CREATE TABLE IF NOT EXISTS job_progress (
    job_id TEXT PRIMARY KEY,
    user_id INTEGER,
    status TEXT DEFAULT 'pending',
    step TEXT DEFAULT '',
    progress INTEGER DEFAULT 0,
    current_page INTEGER DEFAULT 0,
    total_pages INTEGER DEFAULT 0,
    items_found INTEGER DEFAULT 0,
    message TEXT DEFAULT '',
    result_json TEXT,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
)""",
        """CREATE TABLE IF NOT EXISTS comparison_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comparison_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    comment TEXT NOT NULL,
    comment_type TEXT DEFAULT 'note',
    created_at TEXT DEFAULT (datetime('now'))
)""",
        "CREATE INDEX IF NOT EXISTS idx_comments_cid ON comparison_comments(comparison_id)",
        """CREATE TABLE IF NOT EXISTS intake_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    sender TEXT DEFAULT '',
    subject TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    processed_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
)""",
    ]:
        try:
            db.execute(migration)
            db.commit()
        except Exception as _mig_err:
            db.rollback()
            _mig_msg = str(_mig_err).lower()
            if "already exists" in _mig_msg or "duplicate column" in _mig_msg:
                pass  # idempotent — column/table already present
            else:
                logger.warning("Migration failed (schema may be incomplete): %s", _mig_err)


def _db_track_schema_version(db):
    # ── Schema version tracking (#48) ──────────────────────────────────────────
    # Record the current schema version as a SHA-1 digest of all migration SQL.
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT DEFAULT (datetime('now')),
            description TEXT DEFAULT ''
        )""")
        db.commit()
        import hashlib as _hl48
        # Use a digest of the init script + migration count as version string
        _payload = "doccompare-v6-migrations-rev20260530"
        _ver = _hl48.sha1(_payload.encode(), usedforsecurity=False).hexdigest()[:12]
        db.execute(
            "INSERT OR IGNORE INTO schema_migrations(version) VALUES(?)", (_ver,)
        )
        db.commit()
    except Exception:
        pass


def _db_seed_defaults(db):
    # Domyślne typy dokumentów
    defaults = [
        ('auto',  '❓', 'Wykryj automatycznie',          'Silnik sam rozpozna typ dokumentu', 1, 0),
        ('PO',    '📋', 'Purchase Order (PO)',             'Zamówienie zakupu — SAP, zewnętrzne', 1, 1),
        ('PI',    '📄', 'Proforma Invoice (PI)',           'Faktura proforma od dostawcy', 1, 2),
        ('CI',    '🧾', 'Commercial Invoice (CI)',         'Faktura handlowa eksportowa', 1, 3),
        ('PL',    '📦', 'Packing List',                   'Lista pakowania', 1, 4),
        ('SAD',   '🛃', 'SAD / ZC415',                    'Zgłoszenie celne importowe (WinSAD)', 1, 5),
        ('BL',    '🚢', 'Bill of Lading',                 'Konosament morski', 1, 6),
        ('WZ',    '📑', 'WZ (wydanie zewnętrzne)',         'Dokument magazynowy WZ', 1, 7),
        ('FV',    '🔖', 'Faktura VAT (polska)',            'Polska faktura VAT', 1, 8),
        ('MULTI', '🔀', 'Zestaw faktur (multi-CI)',        'Wiele CI w jednym PDF', 1, 9),
        ('CMR',   '🚛', 'CMR (list przewozowy)',           'Międzynarodowy list przewozowy', 1, 10),
        ('SWIFT', '💳', 'Potwierdzenie SWIFT',             'Potwierdzenie przelewu bankowego', 0, 11),
    ]
    for code, icon, label, desc, active, order in defaults:
        db.execute(
            'INSERT INTO doc_types(code,icon,label,description,active,sort_order) VALUES(?,?,?,?,?,?) ON CONFLICT(code) DO NOTHING',
            (code, icon, label, desc, active, order)
        )

    if not db.execute("SELECT id FROM users WHERE username='admin'").fetchone():
        import secrets as _sec
        _rand_pw = _sec.token_urlsafe(16)
        for uname, pwd, urole in [
            ("admin",        _rand_pw, "admin"),
            ("superuser",    _sec.token_urlsafe(16), "superuser"),
            ("jan.kowalski", _sec.token_urlsafe(16), "user"),
            ("anna.nowak",   _sec.token_urlsafe(16), "manager"),
        ]:
            db.execute(
                "INSERT INTO users(username, password_hash, role) VALUES (?, ?, ?)",
                (uname, generate_password_hash(pwd), urole)
            )
        # Save generated admin password to a local file (not just stdout)
        try:
            _pw_file = os.path.join(os.path.dirname(__file__) or ".", "INITIAL_ADMIN_PASSWORD.txt")
            with open(_pw_file, "w") as _pf:
                _pf.write(f"admin: {_rand_pw}\n")
            os.chmod(_pw_file, 0o600)
            logger.warning("First-run: admin password saved to %s — delete after first login!", _pw_file)
        except Exception:
            pass
        print(f"\n{'='*60}")
        print(f"  NOWA INSTALACJA — hasło administratora: {_rand_pw}")
        print(f"  Zaloguj się jako 'admin' i natychmiast zmień hasło!")
        print(f"{'='*60}\n")
        logger.warning("First-run: default users created with random passwords.")
    db.commit()


def _db_ensure_extra_columns(db):
    # Explicit column-presence migrations — safe to re-run on every startup
    _missing_cols = [
        ("library_files", "thumb_data", "BYTEA"),
        ("library_files", "z_path",     "TEXT DEFAULT ''"),
        ("library_files", "file_path",  "TEXT DEFAULT ''"),
    ]
    # Defence-in-depth: although _missing_cols is a hardcoded controlled list,
    # validate identifiers before interpolating them into PRAGMA/ALTER (which
    # cannot be parameterised), so a future refactor can't introduce injection.
    _IDENT_RE  = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    _COLDEF_RE = re.compile(r"^[A-Za-z0-9_'()., ]+$")
    for _tbl, _col, _def in _missing_cols:
        if not (_IDENT_RE.match(_tbl) and _IDENT_RE.match(_col) and _COLDEF_RE.match(_def)):
            logger.warning("Pomijam niebezpieczną migrację kolumny: %r %r %r", _tbl, _col, _def)
            continue
        try:
            if os.environ.get("DATABASE_URL"):  # PostgreSQL
                _exists = db.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name=? AND column_name=?", (_tbl, _col)
                ).fetchone()
            else:  # SQLite
                _exists = None
                for _r in db.execute(f"PRAGMA table_info({_tbl})").fetchall():
                    if _r[1] == _col:
                        _exists = _r
                        break
            if not _exists:
                db.execute(f"ALTER TABLE {_tbl} ADD COLUMN {_col} {_def}")
                db.commit()
        except Exception:
            db.rollback()


def init_db():
    os.makedirs("instance", exist_ok=True)
    os.makedirs("uploads", exist_ok=True)
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS comparisons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            doc_type TEXT,
            file_a TEXT,
            file_b TEXT,
            result_json TEXT,
            status TEXT DEFAULT 'ok',
            diff_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL,
            username TEXT,
            detail TEXT DEFAULT '',
            ip TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS doc_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            label TEXT NOT NULL,
            icon TEXT NOT NULL DEFAULT '📄',
            description TEXT DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 99
        );
    """)
    _db_run_migrations(db)
    _db_track_schema_version(db)
    _db_seed_defaults(db)
    _db_ensure_extra_columns(db)
    db.close()


# Run on every startup (python app.py AND gunicorn) — safe with CREATE IF NOT EXISTS
try:
    init_db()
    try:
        from supplier_profiles import init_suppliers_table
        init_suppliers_table()
    except Exception:
        pass
except Exception as _e:
    logger.critical("init_db() failed at startup — DB schema may be incomplete: %s", _e)

# Załaduj ANTHROPIC_API_KEY z bazy danych jeśli nie ma w środowisku
if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
    try:
        _db = get_db()
        try:
            _row = _db.execute(
                "SELECT value FROM settings WHERE category='ai' AND key='anthropic_api_key'"
            ).fetchone()
        finally:
            _db.close()
        if _row and _row[0]:
            os.environ["ANTHROPIC_API_KEY"] = _row[0]
            logger.info("ANTHROPIC_API_KEY loaded from database settings")
    except Exception as _e:
        logger.warning(f"Could not load API key from DB: {_e}")


# ─────────────────────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────────────────────

# _check_session_active / login_required / require_role → core.security (import na górze)


# Dozwolone ścieżki per rola zewnętrzna (każdy portal odcięty od reszty aplikacji).
_EXTERNAL_PREFIXES = {
    "forwarder": _FWD_PREFIXES,
    "customs_agent": _AGENCJA_PREFIXES,
}


@app.before_request
def _restrict_external_forwarders():
    """Izolacja portali zewnętrznych: forwarder→/spedycja, agencja celna→/agencja.
    Każda rola ma dostęp wyłącznie do swojego portalu i kilku bezpiecznych ścieżek.
    Próba wejścia gdziekolwiek indziej → przekierowanie do właściwego portalu."""
    role = session.get("role")
    if role not in EXTERNAL_ROLES:
        return
    prefixes = _EXTERNAL_PREFIXES.get(role, ())
    p = request.path or "/"
    if p == "/" or any(p.startswith(pref) for pref in prefixes):
        return
    if request.path.startswith("/api/"):
        return jsonify({"error": "Brak uprawnień"}), 403
    return redirect(url_for(_EXTERNAL_ROLE_HOME.get(role, "spedycja_page")))


@app.before_request
def _restrict_delivery_by_department():
    """Podział per dział: user/manager mają dostęp tylko do dostaw swojego działu;
    superuser/admin (can_see_all) — bez ograniczeń. Centralna bramka dla WSZYSTKICH
    tras /dostawy/<nr> i /api/dostawy/<nr>, żeby żadna nie umknęła (anty-IDOR).
    Dostawy bez przypisanego działu (legacy) są widoczne dla wszystkich."""
    if not session.get("user_id"):
        return  # niezalogowany — obsłuży login_required
    role = session.get("role", "")
    if role in EXTERNAL_ROLES or can_see_all(role):
        return
    m = re.match(r"^/(?:api/)?dostawy/([^/]+)", request.path or "/")
    if not m:
        return  # listy/kolekcje bez konkretnego numeru — filtrowane w zapytaniu
    from urllib.parse import unquote as _unq
    nr = _unq(m.group(1))
    if nr in ("backfill-refs", "extract-po", "analytics", "analityka"):
        return  # endpointy zbiorcze / osobne ścieżki, nie konkretna dostawa
    db = get_db()
    try:
        row = db.execute(
            "SELECT department FROM kolejka_zlecenia WHERE nr_zamowienia=?", (nr,)
        ).fetchone()
    finally:
        db.close()
    if row is None:
        return  # nie istnieje — trasa zwróci 404 normalnie
    ddept = (row["department"] or "")
    if ddept and ddept != (session.get("department") or ""):
        if (request.path or "").startswith("/api/"):
            return jsonify({"error": "Brak dostępu do tej dostawy (inny dział)"}), 403
        return ("Brak dostępu — ta dostawa należy do innego działu.", 403)


# _check_rate_limit / _clear_login_attempts → blueprints/auth.py (Phase 3, REFACTOR-01)

# ── CSRF protection → core.security (_get_csrf_token / _verify_csrf / csrf_protect) ──

@app.context_processor
def _inject_csrf():
    """Make csrf_token() available in every Jinja2 template."""
    return {"csrf_token": _get_csrf_token}


@app.context_processor
def inject_i18n():
    lang = session.get("lang", "pl")
    def _t(key):
        return _t_func(key, lang)
    return {"_t": _t, "_lang": lang}


@app.after_request
def apply_en_translation(response):
    """Post-process HTML responses: replace Polish text with English when lang=en."""
    if (session.get("lang", "pl") == "en"
            and response.status_code == 200
            and "text/html" in response.content_type):
        try:
            html = response.get_data(as_text=True)
            html = _translate_html_en(html)
            response.set_data(html)
        except Exception:
            pass  # Never break the response on translation errors
    return response


# _check_forgot_limit / _record_forgot_attempt moved to blueprints/auth.py (Phase 3, REFACTOR-01)

# _log_audit → core.audit (import na górze)


# ─────────────────────────────────────────────────────────────────────────────
# BEFORE REQUEST — cross-cutting concerns
# ─────────────────────────────────────────────────────────────────────────────

@app.before_request
def _before_request():
    """Set Sentry user context for every authenticated request."""
    if _sentry_dsn and "user_id" in session:
        try:
            import sentry_sdk
            sentry_sdk.set_user({
                "id": session.get("user_id"),
                "username": session.get("username"),
                "ip_address": request.remote_addr,
            })
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — STRONY
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    # Public liveness probe — intentionally minimal to avoid leaking business
    # metrics or infrastructure details to unauthenticated callers.
    details: dict = {}
    overall = "ok"
    # DB reachability (no row counts — comparisons_total is business-sensitive)
    try:
        db = get_db()
        try:
            db.execute("SELECT 1")
            details["db"] = "ok"
        finally:
            db.close()
    except Exception as e:
        details["db"] = "error"
        overall = "degraded"
    # Disk space warning only (no exact free-space number)
    try:
        import shutil as _shutil
        _total, _used, _free = _shutil.disk_usage(".")
        if _free < 200 * 1024 * 1024:
            details["disk_warning"] = "Low disk space"
            overall = "degraded"
    except Exception:
        pass
    return jsonify({"status": overall, **details}), 200 if overall == "ok" else 503


@app.route("/api/admin/backup", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_admin_backup():
    """Trigger an immediate SQLite backup. No-op for PostgreSQL environments."""
    backup_dir = os.environ.get("BACKUP_DIR", os.path.join(os.path.dirname(__file__) or ".", "backups"))
    dest = _backup_sqlite_once(backup_dir)
    if dest is None:
        from db import DATABASE_URL as _dbu
        if _dbu:
            return jsonify({"ok": True, "message": "Backup zarządzany przez serwis PostgreSQL — nie wymagany ręczny backup"}), 200
        return jsonify({"error": "Backup nie powiódł się — sprawdź logi"}), 500
    return jsonify({"ok": True, "path": os.path.basename(dest)})


@app.route("/api/admin/schema-versions")
@require_role("admin")
def api_admin_schema_versions():
    """Return the list of recorded schema migration version hashes."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY applied_at DESC"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception:
        return jsonify([])
    finally:
        db.close()


@app.route("/")
def index():
    if "user_id" in session:
        _role = session.get("role")
        if _role in EXTERNAL_ROLES:
            return redirect(url_for(_EXTERNAL_ROLE_HOME.get(_role, "spedycja_page")))
        return redirect(url_for("home_page"))
    return redirect(url_for("auth.login"))


@app.route("/home")
@login_required
def home_page():
    db = get_db()
    try:
        uid = session["user_id"]
        role = session["role"]
        if is_admin(role):
            total_comparisons = (db.execute("SELECT COUNT(*) FROM comparisons").fetchone() or [0])[0]
            total_artworks = (db.execute("SELECT COUNT(*) FROM comparisons WHERE doc_type='artwork'").fetchone() or [0])[0]
            total_users = (db.execute("SELECT COUNT(*) FROM users").fetchone() or [0])[0]
            recent = db.execute(
                "SELECT c.id,c.file_a,c.file_b,c.status,c.diff_count,c.created_at,u.username "
                "FROM comparisons c JOIN users u ON c.user_id=u.id "
                "ORDER BY c.id DESC LIMIT 5"
            ).fetchall()
        else:
            total_comparisons = (db.execute("SELECT COUNT(*) FROM comparisons WHERE user_id=?", [uid]).fetchone() or [0])[0]
            total_artworks = (db.execute("SELECT COUNT(*) FROM comparisons WHERE doc_type='artwork' AND user_id=?", [uid]).fetchone() or [0])[0]
            total_users = None
            recent = db.execute(
                "SELECT c.id,c.file_a,c.file_b,c.status,c.diff_count,c.created_at,u.username "
                "FROM comparisons c JOIN users u ON c.user_id=u.id "
                "WHERE c.user_id=? ORDER BY c.id DESC LIMIT 5", [uid]
            ).fetchall()
        recent = [dict(r) for r in (recent or [])]
    except Exception:
        total_comparisons = total_artworks = total_users = 0
        recent = []
    finally:
        db.close()
    return render_template("home.html",
                           username=session["username"],
                           role=session["role"],
                           total_comparisons=total_comparisons,
                           total_artworks=total_artworks,
                           total_users=total_users,
                           recent=recent)


# login() / set_language() moved to blueprints/auth.py (Phase 3, REFACTOR-01)


# _send_reset_email moved to email_helpers.py (Phase 3, REFACTOR-01) — imported
# above as send_reset_email (shared by blueprints/auth.py and the admin
# reset-link endpoint below).


def _send_critical_alert(username: str, to_email: str, cid: int,
                          doc_type: str, file_a: str, file_b: str,
                          diff_count: int, po_number: str) -> None:
    """Send a non-blocking email alert when a comparison is rated critical."""
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key or not to_email:
        return
    base_url = os.environ.get("APP_BASE_URL", "").rstrip("/")
    report_url = f"{base_url}/history" if base_url else "/history"
    from markupsafe import escape as _esc
    u_safe  = str(_esc(username))
    fa_safe = str(_esc(file_a))
    fb_safe = str(_esc(file_b))
    dt_safe = str(_esc(doc_type))
    po_safe = str(_esc(po_number)) if po_number else "—"
    html_body = (
        f"<p>Cześć <strong>{u_safe}</strong>,</p>"
        f"<p>Porównanie dokumentów zakończyło się wynikiem <strong style='color:#ff5c5c'>KRYTYCZNYM</strong>.</p>"
        f"<table style='border-collapse:collapse;font-size:14px'>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#999'>Typ dokumentu</td><td><strong>{dt_safe}</strong></td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#999'>Plik A</td><td>{fa_safe}</td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#999'>Plik B</td><td>{fb_safe}</td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#999'>Nr PO</td><td>{po_safe}</td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#999'>Liczba różnic</td><td><strong>{diff_count}</strong></td></tr>"
        f"</table>"
        f"<p style='margin-top:16px'>"
        f'<a href="{report_url}" style="background:#ff5c5c;color:#fff;padding:10px 20px;'
        f'border-radius:6px;text-decoration:none;font-weight:600">Przejdź do raportu →</a></p>'
        "<p style='color:#999;font-size:12px;margin-top:16px'>DocCompare — ACME</p>"
    )
    email_from = os.environ.get("EMAIL_FROM", "DocCompare <noreply@doccompare.app>")
    def _do_send():
        try:
            import httpx
            httpx.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"from": email_from, "to": [to_email],
                      "subject": f"⚠ KRYTYCZNE porównanie #{cid} — DocCompare", "html": html_body},
                timeout=15,
            )
        except Exception as exc:
            logger.warning("Critical alert email failed: %s", exc)
    threading.Thread(target=_do_send, daemon=True).start()


# forgot_password() / reset_password() moved to blueprints/auth.py (Phase 3, REFACTOR-01)


# logout() moved to blueprints/auth.py (Phase 3, REFACTOR-01)


@app.route("/compare")
@login_required
def compare_page():
    return render_template("compare.html",
                           username=session["username"],
                           role=session["role"],
                           can_see_all=can_see_all(session["role"]))


@app.route("/table")
@login_required
def table_page():
    return render_template("table_compare.html",
                           username=session["username"],
                           role=session["role"],
                           can_see_all=can_see_all(session["role"]))


@app.route("/typo")
@login_required
def typo_page():
    return render_template("typo_compare.html",
                           username=session["username"],
                           role=session["role"],
                           can_see_all=can_see_all(session["role"]))


@app.route("/search")
@login_required
def global_search():
    """Globalna wyszukiwarka — z jednego pola przeszukuje porównania, PO/zlecenia,
    kontenery i dostawców. Read-only, parametryzowane zapytania przez get_db()."""
    role = session["role"]
    uid = session["user_id"]
    q = request.args.get("q", "")[:200].strip()
    results = {"comparisons": [], "zlecenia": [], "kontenery": [], "suppliers": []}
    if q:
        s_esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{s_esc}%"
        db = get_db()
        # ── Porównania dokumentów (respektuje widoczność per rola) ──
        try:
            conds = ["(c.is_deleted IS NULL OR c.is_deleted=0)",
                     "(c.file_a LIKE ? ESCAPE '\\' OR c.file_b LIKE ? ESCAPE '\\' "
                     "OR c.po_number LIKE ? ESCAPE '\\' OR c.supplier_code LIKE ? ESCAPE '\\')"]
            params = [like, like, like, like]
            if not can_see_all(role):
                conds.append("c.user_id = ?")
                params.append(uid)
            for r in db.execute(
                # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
                "SELECT c.id, c.doc_type, c.file_a, c.file_b, c.po_number, "  # nosec B608
                "c.supplier_code, c.status, c.created_at FROM comparisons c "
                "WHERE " + " AND ".join(conds) +
                " ORDER BY c.created_at DESC LIMIT 15", params).fetchall():
                results["comparisons"].append({
                    "id": r["id"], "doc_type": r["doc_type"],
                    "file_a": r["file_a"], "file_b": r["file_b"],
                    "po_number": r["po_number"], "supplier_code": r["supplier_code"],
                    "status": r["status"], "created_at": r["created_at"],
                })
        except Exception as e:
            app.logger.debug("search comparisons failed: %s", e)
        # ── PO / zlecenia (Kolejka) ──
        try:
            for r in db.execute(
                "SELECT id, nr_zamowienia, supplier_name, supplier_code, status, "
                "proforma_lot FROM kolejka_zlecenia "
                "WHERE nr_zamowienia LIKE ? ESCAPE '\\' OR supplier_name LIKE ? ESCAPE '\\' "
                "OR supplier_code LIKE ? ESCAPE '\\' OR proforma_lot LIKE ? ESCAPE '\\' "
                "ORDER BY id DESC LIMIT 15", [like, like, like, like]).fetchall():
                results["zlecenia"].append({
                    "nr": r["nr_zamowienia"],
                    "supplier": r["supplier_name"] or r["supplier_code"],
                    "status": r["status"], "lot": r["proforma_lot"],
                })
        except Exception as e:
            app.logger.debug("search zlecenia failed: %s", e)
        # ── Kontenery ──
        try:
            for r in db.execute(
                "SELECT id, numer_kontenera, fo_number, sealine, origin_port, "
                "dest_port, customs_status FROM kolejka_kontenery "
                "WHERE numer_kontenera LIKE ? ESCAPE '\\' OR fo_number LIKE ? ESCAPE '\\' "
                "OR sealine LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT 15",
                [like, like, like]).fetchall():
                results["kontenery"].append({
                    "numer": r["numer_kontenera"], "fo": r["fo_number"],
                    "sealine": r["sealine"], "customs": r["customs_status"],
                    "route": " → ".join([p for p in (r["origin_port"], r["dest_port"]) if p]),
                })
        except Exception as e:
            app.logger.debug("search kontenery failed: %s", e)
        # ── Dostawcy ──
        try:
            for r in db.execute(
                "SELECT id, code, name, country FROM suppliers "
                "WHERE code LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' "
                "OR country LIKE ? ESCAPE '\\' ORDER BY name LIMIT 15",
                [like, like, like]).fetchall():
                results["suppliers"].append({
                    "code": r["code"], "name": r["name"], "country": r["country"],
                })
        except Exception as e:
            app.logger.debug("search suppliers failed: %s", e)
        finally:
            db.close()   # zwolnij połączenie z puli od razu (nie czekaj na teardown)
    total = sum(len(v) for v in results.values())
    return render_template("search.html", q=q, results=results, total=total,
                           username=session["username"], role=role)


@app.route("/history")
@login_required
def history_page():
    role = session["role"]
    uid = session["user_id"]
    # Parametry paginacji i wyszukiwania — strict validation
    try:
        page = max(1, min(10000, int(request.args.get("page", 1))))
    except (ValueError, TypeError):
        page = 1
    per_page = 20
    search = request.args.get("q", "")[:200].strip()   # cap length
    status_filter = request.args.get("status", "").strip()
    # Whitelist allowed status values to prevent injection via conditions list
    if status_filter not in ("ok", "warning", "error", "critical", "pending", ""):
        status_filter = ""
    offset = (page - 1) * per_page

    # Buduj zapytanie z filtrami
    params = []
    conditions = ["c.doc_type != 'artwork'", "(c.is_deleted IS NULL OR c.is_deleted=0)"]
    if not can_see_all(role):
        conditions.append("c.user_id = ?")
        params.append(uid)
    if search:
        conditions.append(
            "(c.file_a LIKE ? ESCAPE '\\' OR c.file_b LIKE ? ESCAPE '\\' "
            "OR c.po_number LIKE ? ESCAPE '\\' OR c.supplier_code LIKE ? ESCAPE '\\' "
            "OR u.username LIKE ? ESCAPE '\\' OR c.result_json LIKE ? ESCAPE '\\')"
        )
        s_esc = escape_like(search)
        like = f"%{s_esc}%"
        params.extend([like, like, like, like, like, like])
    if status_filter:
        conditions.append("c.status = ?")
        params.append(status_filter)

    where = "WHERE " + " AND ".join(conditions)

    db = get_db()
    try:
        total_count = db.execute(
            # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
            f"SELECT COUNT(*) FROM comparisons c JOIN users u ON c.user_id = u.id {where}",  # nosec B608
            params
        ).fetchone()[0]

        rows = db.execute(
            # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
            f"""SELECT c.id, c.user_id, c.doc_type, c.file_a, c.file_b,
                       c.status, c.diff_count, c.created_at, c.po_number,
                       c.supplier_code, c.total_a, c.total_b, c.comment,
                       c.approval_status, c.approved_by, c.approved_at,
                       c.is_public, c.is_deleted, c.deleted_at, c.deleted_by,
                       u.username
                FROM comparisons c
                JOIN users u ON c.user_id = u.id
                {where}
                ORDER BY c.created_at DESC
                LIMIT ? OFFSET ?""",  # nosec B608
            params + [per_page, offset]
        ).fetchall()
    finally:
        db.close()

    total_pages = max(1, (total_count + per_page - 1) // per_page)
    return render_template("history.html",
                           comparisons=rows,
                           username=session["username"],
                           role=session["role"],
                           can_see_all=can_see_all(role),
                           can_delete=can_delete(role),
                           page=page,
                           total_pages=total_pages,
                           total_count=total_count,
                           per_page=per_page,
                           search=search,
                           status_filter=status_filter)


@app.route("/kpi")
@login_required
def kpi_page():
    role = session["role"]
    uid  = session["user_id"]
    db   = get_db()
    try:
        if can_see_all(role):
            # ── Dokumenty per dostawca ────────────────────────────────────────
            per_supplier = db.execute("""
                SELECT
                    COALESCE(NULLIF(supplier_code,''), 'Nieznany') as supplier,
                    COUNT(*) as total,
                    SUM(CASE WHEN status IN ('blad','error','critical') THEN 1 ELSE 0 END) as errors,
                    SUM(CASE WHEN status IN ('ostrzezenie','warning') THEN 1 ELSE 0 END) as warnings,
                    SUM(COALESCE(diff_count,0)) as total_diffs,
                    MAX(created_at) as last_at
                FROM comparisons
                GROUP BY COALESCE(NULLIF(supplier_code,''), 'Nieznany')
                ORDER BY total DESC
                LIMIT 20
            """).fetchall()

            # ── Dokumenty per użytkownik ──────────────────────────────────────
            per_user = db.execute("""
                SELECT
                    u.username, u.role,
                    COUNT(c.id) as total,
                    SUM(CASE WHEN c.status IN ('blad','error','critical') THEN 1 ELSE 0 END) as errors,
                    SUM(CASE WHEN c.status IN ('ostrzezenie','warning') THEN 1 ELSE 0 END) as warnings,
                    SUM(COALESCE(c.diff_count,0)) as total_diffs,
                    MAX(c.created_at) as last_at
                FROM users u
                LEFT JOIN comparisons c ON c.user_id = u.id
                GROUP BY u.id, u.username, u.role
                ORDER BY total DESC
            """).fetchall()

            # ── Łączna liczba wykrytych błędów ────────────────────────────────
            totals = db.execute("""
                SELECT
                    COUNT(*) as total_docs,
                    SUM(CASE WHEN status IN ('blad','error','critical') THEN 1 ELSE 0 END) as error_docs,
                    SUM(CASE WHEN status IN ('ostrzezenie','warning') THEN 1 ELSE 0 END) as warning_docs,
                    SUM(CASE WHEN status='ok' OR status='format' THEN 1 ELSE 0 END) as ok_docs,
                    SUM(COALESCE(diff_count,0)) as total_diffs,
                    COUNT(DISTINCT NULLIF(supplier_code,'')) as unique_suppliers,
                    COUNT(DISTINCT user_id) as active_users
                FROM comparisons
            """).fetchone()

        else:
            # User widzi tylko swoje statystyki
            per_supplier = db.execute("""
                SELECT
                    COALESCE(NULLIF(supplier_code,''), 'Nieznany') as supplier,
                    COUNT(*) as total,
                    SUM(CASE WHEN status IN ('blad','error','critical') THEN 1 ELSE 0 END) as errors,
                    SUM(CASE WHEN status IN ('ostrzezenie','warning') THEN 1 ELSE 0 END) as warnings,
                    SUM(COALESCE(diff_count,0)) as total_diffs,
                    MAX(created_at) as last_at
                FROM comparisons
                WHERE user_id = ?
                GROUP BY COALESCE(NULLIF(supplier_code,''), 'Nieznany')
                ORDER BY total DESC
                LIMIT 20
            """, (uid,)).fetchall()

            per_user = db.execute("""
                SELECT
                    u.username, u.role,
                    COUNT(c.id) as total,
                    SUM(CASE WHEN c.status IN ('blad','error','critical') THEN 1 ELSE 0 END) as errors,
                    SUM(CASE WHEN c.status IN ('ostrzezenie','warning') THEN 1 ELSE 0 END) as warnings,
                    SUM(COALESCE(c.diff_count,0)) as total_diffs,
                    MAX(c.created_at) as last_at
                FROM users u
                LEFT JOIN comparisons c ON c.user_id = u.id AND c.user_id = ?
                WHERE u.id = ?
                GROUP BY u.id
            """, (uid, uid)).fetchall()

            totals = db.execute("""
                SELECT
                    COUNT(*) as total_docs,
                    SUM(CASE WHEN status IN ('blad','error','critical') THEN 1 ELSE 0 END) as error_docs,
                    SUM(CASE WHEN status IN ('ostrzezenie','warning') THEN 1 ELSE 0 END) as warning_docs,
                    SUM(CASE WHEN status='ok' OR status='format' THEN 1 ELSE 0 END) as ok_docs,
                    SUM(COALESCE(diff_count,0)) as total_diffs,
                    COUNT(DISTINCT NULLIF(supplier_code,'')) as unique_suppliers,
                    1 as active_users
                FROM comparisons
                WHERE user_id = ?
            """, (uid,)).fetchone()
    finally:
        db.close()
    return render_template("kpi.html",
                           username=session["username"],
                           role=session["role"],
                           can_see_all=can_see_all(role),
                           per_supplier=[dict(r) for r in per_supplier],
                           per_user=[dict(r) for r in per_user],
                           totals=dict(totals) if totals else {})
@app.route("/tariff")
@require_role("manager")
def tariff_page():
    """Lokalna taryfa referencyjna CN/TARIC — import nomenklatury i przegląd.
    Zasila walidację istnienia kodów CN w audycie SAD (E8)."""
    db = get_db()
    try:
        total = (db.execute("SELECT COUNT(*) FROM cn_reference").fetchone() or [0])[0]
        restricted = (db.execute(
            "SELECT COUNT(*) FROM cn_reference WHERE TRIM(restrictions) <> ''").fetchone() or [0])[0]
        inactive = (db.execute(
            "SELECT COUNT(*) FROM cn_reference WHERE active = 0").fetchone() or [0])[0]
        last = (db.execute("SELECT MAX(updated_at) FROM cn_reference").fetchone() or [None])[0]
        sample = [dict(r) for r in db.execute(
            "SELECT code, description, duty_rate, restrictions, active "
            "FROM cn_reference ORDER BY updated_at DESC LIMIT 50").fetchall()]
    finally:
        db.close()
    return render_template("tariff.html",
                           username=session["username"], role=session["role"],
                           total=total, restricted=restricted, inactive=inactive,
                           last_update=str(last or "")[:16], sample=sample)


@app.route("/api/cn-reference/import", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_cn_reference_import():
    """Import CSV taryfy CN/TARIC. Kolumny: code, description, duty_rate,
    restrictions, active (akceptuje też polskie nagłówki). Upsert po kodzie."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Brak pliku"}), 400
    if not f.filename.lower().endswith(".csv"):
        return jsonify({"error": "Dozwolone tylko pliki .csv"}), 400
    import csv, io, re as _re
    try:
        content = f.read().decode("utf-8-sig")
    except Exception:
        return jsonify({"error": "Nie udało się odczytać pliku (oczekiwano UTF-8 CSV)"}), 400
    reader = csv.DictReader(io.StringIO(content))
    db = get_db()
    ok, skipped = 0, 0
    try:
        for row in reader:
            raw = (row.get("code") or row.get("Kod") or row.get("CN") or "").strip()
            code = _re.sub(r"\D", "", raw)
            if not code:
                skipped += 1
                continue
            active_raw = str(row.get("active") or row.get("Aktywny") or "1").strip().lower()
            active = 0 if active_raw in ("0", "nie", "no", "false", "n") else 1
            try:
                db.execute(
                    "INSERT INTO cn_reference(code, description, duty_rate, restrictions, active) "
                    "VALUES(?,?,?,?,?) "
                    "ON CONFLICT(code) DO UPDATE SET description=excluded.description, "
                    "duty_rate=excluded.duty_rate, restrictions=excluded.restrictions, "
                    "active=excluded.active, updated_at=datetime('now')",
                    (code[:20],
                     (row.get("description") or row.get("Opis") or "")[:500],
                     (row.get("duty_rate") or row.get("Stawka") or "")[:50],
                     (row.get("restrictions") or row.get("Ograniczenia") or "")[:300],
                     active))
                db.commit()   # per-wiersz: na PG nieudany INSERT psuje całą transakcję
                ok += 1
            except Exception:
                db.rollback()
                skipped += 1
    finally:
        db.close()
    return jsonify({"ok": ok, "imported": ok, "skipped": skipped}), 200


@app.route("/scorecard")
@login_required
def scorecard_page():
    """Scorecard jakości dostawców — dla każdego dostawcy liczy wynik 0–100
    (ważony wg severity raportów), ocenę literową A–F, rozkład statusów,
    wskaźnik akceptacji i trend (ostatnie 90 dni vs całość historii).

    Uprawnienia: superuser+ widzi wszystkich dostawców; zwykły użytkownik —
    scorecard liczony tylko z jego własnych porównań (spójnie z /kpi).
    Soft-usunięte raporty (is_deleted=1) są wykluczane.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    role    = session["role"]
    uid     = session["user_id"]
    see_all = can_see_all(role)
    since90 = (_dt.now(_tz.utc) - _td(days=90)).strftime("%Y-%m-%d")

    where = "WHERE COALESCE(c.is_deleted,0)=0"
    # 5 parametrów `since90` to 5 wyrażeń CASE okna 90-dniowego w SELECT (pozycyjne,
    # pojawiają się PRZED klauzulą WHERE), dopiero potem ewentualny filtr użytkownika.
    params: list = [since90, since90, since90, since90, since90]
    if not see_all:
        where += " AND c.user_id = ?"
        params.append(uid)

    db = get_db()
    try:
        # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
        rows = db.execute(f"""
            SELECT
                COALESCE(NULLIF(c.supplier_code,''),'Nieznany') AS supplier_code,
                MAX(s.name)    AS supplier_name,
                MAX(s.country) AS country,
                COUNT(*) AS total,
                SUM(CASE WHEN c.status IN ('ok','format') THEN 1 ELSE 0 END)            AS ok,
                SUM(CASE WHEN c.status IN ('ostrzezenie','warning') THEN 1 ELSE 0 END)  AS warnings,
                SUM(CASE WHEN c.status IN ('blad','error') THEN 1 ELSE 0 END)           AS errors,
                SUM(CASE WHEN c.status='critical' THEN 1 ELSE 0 END)                    AS critical,
                SUM(COALESCE(c.diff_count,0)) AS total_diffs,
                SUM(CASE WHEN c.approval_status='approved' THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN c.approval_status='rejected' THEN 1 ELSE 0 END) AS rejected,
                SUM(CASE WHEN c.created_at >= ? THEN 1 ELSE 0 END) AS recent_total,
                SUM(CASE WHEN c.created_at >= ? AND c.status IN ('ok','format') THEN 1 ELSE 0 END)           AS recent_ok,
                SUM(CASE WHEN c.created_at >= ? AND c.status IN ('ostrzezenie','warning') THEN 1 ELSE 0 END) AS recent_warn,
                SUM(CASE WHEN c.created_at >= ? AND c.status IN ('blad','error') THEN 1 ELSE 0 END)          AS recent_err,
                SUM(CASE WHEN c.created_at >= ? AND c.status='critical' THEN 1 ELSE 0 END)                   AS recent_crit,
                MAX(c.created_at) AS last_at
            FROM comparisons c
            LEFT JOIN suppliers s ON s.code = c.supplier_code
            {where}
            GROUP BY COALESCE(NULLIF(c.supplier_code,''),'Nieznany')
            ORDER BY total DESC
        """, params).fetchall()  # nosec B608
        rows = [dict(r) for r in rows]
    finally:
        db.close()

    # Wynik jakości: każdy raport waży wg severity (ok=1.0, ostrzeżenie=0.6,
    # błąd=0.25, krytyczny=0.0) → średnia ×100. Trend = wynik z 90 dni − wynik
    # ogólny (pokazywany tylko gdy w oknie jest ≥3 raporty, by uniknąć szumu).
    def _score(ok, warn, err, crit):
        tot = ok + warn + err + crit
        if tot <= 0:
            return None
        return round(100.0 * (1.0 * ok + 0.6 * warn + 0.25 * err) / tot)

    def _grade(sc):
        if sc is None:
            return "—"
        return ("A" if sc >= 90 else "B" if sc >= 75 else
                "C" if sc >= 60 else "D" if sc >= 40 else "F")

    cards = []
    for r in rows:
        ok, warn = int(r["ok"] or 0), int(r["warnings"] or 0)
        err, crit = int(r["errors"] or 0), int(r["critical"] or 0)
        total = int(r["total"] or 0)
        sc = _score(ok, warn, err, crit)
        rec_total = int(r["recent_total"] or 0)
        rec_sc = _score(int(r["recent_ok"] or 0), int(r["recent_warn"] or 0),
                        int(r["recent_err"] or 0), int(r["recent_crit"] or 0))
        trend = (rec_sc - sc) if (rec_sc is not None and sc is not None and rec_total >= 3) else None
        decided = int(r["approved"] or 0) + int(r["rejected"] or 0)
        cards.append({
            "code": r["supplier_code"],
            "name": r["supplier_name"] or "",
            "country": r["country"] or "",
            "total": total,
            "ok": ok, "warnings": warn, "errors": err, "critical": crit,
            "total_diffs": int(r["total_diffs"] or 0),
            "avg_diffs": round((int(r["total_diffs"] or 0) / total), 1) if total else 0,
            "approved": int(r["approved"] or 0),
            "rejected": int(r["rejected"] or 0),
            "approval_rate": round(100.0 * int(r["approved"] or 0) / decided) if decided else None,
            "score": sc,
            "grade": _grade(sc),
            "trend": trend,
            "last_at": r["last_at"] or "",
        })

    # Ranking: najlepszy wynik na górze; remis rozstrzyga liczba dokumentów.
    cards.sort(key=lambda c: (c["score"] if c["score"] is not None else -1, c["total"]),
               reverse=True)

    scored = [c for c in cards if c["score"] is not None]
    summary = {
        "suppliers": len(cards),
        "scored": len(scored),
        "total_docs": sum(c["total"] for c in cards),
        "avg_score": round(sum(c["score"] for c in scored) / len(scored)) if scored else None,
        "at_risk": sum(1 for c in scored if c["grade"] in ("D", "F")),
        "best": (max(scored, key=lambda c: (c["score"], c["total"]))["code"]
                 if scored else None),
    }

    return render_template("scorecard.html",
                           username=session["username"],
                           role=role,
                           can_see_all=see_all,
                           cards=cards,
                           summary=summary)


@app.route("/versions")
@login_required
def versions_page():
    """Śledzenie rewizji dokumentu — ten sam (dostawca, numer PO) zgłoszony
    wielokrotnie, ale EWOLUUJĄCY: między wersjami zmienił się status, liczba
    różnic lub kwota (np. PI v1 → PI podpisana → CI). Oś czasu pokazuje, czy
    kolejne wersje poprawiają (mniej różnic) czy pogarszają (więcej) zgodność.

    To odróżnia ten widok od /duplicates (identyczne ponowienia / ryzyko
    podwójnej płatności): tu pokazujemy WYŁĄCZNIE pary, które się zmieniły.
    Uprawnienia/zakres jak /scorecard. Soft-usunięte wykluczone.
    """
    role    = session["role"]
    uid     = session["user_id"]
    see_all = can_see_all(role)

    where = ("WHERE COALESCE(c.is_deleted,0)=0 "
             "AND TRIM(c.supplier_code) <> '' AND TRIM(c.po_number) <> ''")
    params: list = []
    if not see_all:
        where += " AND c.user_id = ?"
        params.append(uid)

    db = get_db()
    try:
        # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
        rows = db.execute(f"""
            SELECT c.id, c.supplier_code, c.po_number, c.total_a, c.total_b,
                   c.status, c.doc_type, c.diff_count, c.created_at, u.username
            FROM comparisons c JOIN users u ON u.id = c.user_id
            {where}
            ORDER BY c.created_at ASC
            LIMIT 20000
        """, params).fetchall()  # nosec B608
        rows = [dict(r) for r in rows]
    finally:
        db.close()

    from collections import OrderedDict as _OD
    groups: "dict[tuple, list]" = _OD()
    for r in rows:
        key = ((r["supplier_code"] or "").strip().upper(),
               (r["po_number"] or "").strip().upper())
        groups.setdefault(key, []).append(r)

    _ERR = ("blad", "error", "critical")
    _WARN = ("ostrzezenie", "warning")

    def _amount(m):
        return (str(m["total_a"] or "").strip() or str(m["total_b"] or "").strip())

    timelines = []
    improving = regressing = 0
    for (sup, po), members in groups.items():
        if len(members) < 2:
            continue
        # members są już w kolejności rosnącej po dacie (ORDER BY ASC)
        versions = []
        changed_any = False
        prev = None
        for m in members:
            dc = int(m["diff_count"] or 0)
            amt = _amount(m)
            delta = None
            if prev is not None:
                d_diffs = dc - int(prev["diff_count"] or 0)
                amount_changed = amt != _amount(prev)
                status_changed = (m["status"] or "") != (prev["status"] or "")
                if d_diffs != 0 or amount_changed or status_changed:
                    changed_any = True
                delta = {
                    "d_diffs": d_diffs,
                    "amount_changed": amount_changed,
                    "status_changed": status_changed,
                }
            versions.append({
                "id": m["id"],
                "date": (m["created_at"] or "")[:16],
                "user": m["username"] or "",
                "doc_type": m["doc_type"] or "",
                "status": m["status"] or "",
                "diff_count": dc,
                "amount": amt,
                "delta": delta,
            })
            prev = m
        if not changed_any:
            continue  # identyczne ponowienia → należą do /duplicates, nie tutaj

        first_dc = int(members[0]["diff_count"] or 0)
        last_dc  = int(members[-1]["diff_count"] or 0)
        trend = "flat"
        if last_dc < first_dc:
            trend = "improving"; improving += 1
        elif last_dc > first_dc:
            trend = "regressing"; regressing += 1
        timelines.append({
            "supplier": sup,
            "po": po,
            "count": len(versions),
            "trend": trend,
            "first_diffs": first_dc,
            "last_diffs": last_dc,
            "last_at": (members[-1]["created_at"] or "")[:16],
            "versions": versions,
        })

    # Najświeższe rewizje na górze.
    timelines.sort(key=lambda t: t["last_at"], reverse=True)

    summary = {
        "tracked": len(timelines),
        "improving": improving,
        "regressing": regressing,
    }

    return render_template("versions.html",
                           username=session["username"],
                           role=role,
                           can_see_all=see_all,
                           timelines=timelines,
                           summary=summary)


@app.route("/duplicates")
@login_required
def duplicates_page():
    """Wykrywanie zduplikowanych faktur/zamówień — ta sama para (dostawca, numer
    PO) przetworzona więcej niż raz, niezależnie od tego, czy plik PDF jest
    bajtowo identyczny (re-skan / ponowny eksport omija dedup po haszu pliku).
    Ryzyko podwójnej płatności/podwójnego fakturowania.

    Klaster z RÓŻNYMI kwotami jest oznaczany mocniej — to albo korekta, albo
    realny błąd. Uprawnienia jak /scorecard (superuser+ widzi wszystkich;
    użytkownik — swoje). Soft-usunięte wykluczone.
    """
    role    = session["role"]
    uid     = session["user_id"]
    see_all = can_see_all(role)

    where = ("WHERE COALESCE(c.is_deleted,0)=0 "
             "AND TRIM(c.supplier_code) <> '' AND TRIM(c.po_number) <> ''")
    params: list = []
    if not see_all:
        where += " AND c.user_id = ?"
        params.append(uid)

    db = get_db()
    try:
        # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
        rows = db.execute(f"""
            SELECT c.id, c.supplier_code, c.po_number, c.total_a, c.total_b,
                   c.status, c.doc_type, c.created_at, c.file_a, c.file_b,
                   u.username
            FROM comparisons c JOIN users u ON u.id = c.user_id
            {where}
            ORDER BY c.created_at DESC
            LIMIT 20000
        """, params).fetchall()  # nosec B608
        rows = [dict(r) for r in rows]
    finally:
        db.close()

    # Grupowanie w Pythonie (unika dialektowych GROUP_CONCAT/string_agg).
    from collections import OrderedDict as _OD
    groups: "dict[tuple, list]" = _OD()
    for r in rows:
        key = ((r["supplier_code"] or "").strip().upper(),
               (r["po_number"] or "").strip().upper())
        groups.setdefault(key, []).append(r)

    clusters = []
    for (sup, po), members in groups.items():
        if len(members) < 2:
            continue
        # „Kwota" pozycji: total_a, a gdy brak — total_b.
        amounts = set()
        for m in members:
            amt = (str(m["total_a"] or "").strip() or str(m["total_b"] or "").strip())
            if amt:
                amounts.add(amt)
        amount_mismatch = len(amounts) > 1
        clusters.append({
            "supplier": sup,
            "po": po,
            "count": len(members),
            "amount_mismatch": amount_mismatch,
            "amounts": sorted(amounts),
            "members": [{
                "id": m["id"],
                "date": (m["created_at"] or "")[:16],
                "user": m["username"] or "",
                "doc_type": m["doc_type"] or "",
                "status": m["status"] or "",
                "total_a": str(m["total_a"] or "").strip(),
                "total_b": str(m["total_b"] or "").strip(),
                "file_a": m["file_a"] or "",
                "file_b": m["file_b"] or "",
            } for m in members],
        })

    # Najpierw klastry z rozbieżną kwotą (większe ryzyko), potem najliczniejsze.
    clusters.sort(key=lambda c: (c["amount_mismatch"], c["count"]), reverse=True)

    summary = {
        "clusters": len(clusters),
        "dup_docs": sum(c["count"] for c in clusters),
        "mismatch": sum(1 for c in clusters if c["amount_mismatch"]),
    }

    return render_template("duplicates.html",
                           username=session["username"],
                           role=role,
                           can_see_all=see_all,
                           clusters=clusters,
                           summary=summary)


@app.route("/admin")
@login_required
def admin_page():
    if not is_admin(session["role"]):
        return redirect(url_for("analyze_page"))
    db = get_db()
    try:
        users = [dict(r) for r in db.execute(
            "SELECT id, username, email, role, created_at, last_login, "
            "department, allowed_modules FROM users ORDER BY role DESC, username"
        ).fetchall()]
        stats = db.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN status IN ('blad','error','critical') THEN 1 ELSE 0 END) as errors,
                   SUM(CASE WHEN status IN ('ostrzezenie','warning') THEN 1 ELSE 0 END) as warnings
            FROM comparisons
        """).fetchone()
        # Filter in Python to avoid TEXT vs TIMESTAMP comparison issue on PostgreSQL
        try:
            all_tokens = db.execute(
                """SELECT pr.token, pr.expires_at, u.username, u.email
                   FROM password_reset_tokens pr JOIN users u ON u.id=pr.user_id
                   WHERE pr.used=0
                   ORDER BY pr.created_at DESC LIMIT 20"""
            ).fetchall()
            from datetime import datetime as _dt, timezone as _tz
            _now = _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")
            reset_tokens = [t for t in all_tokens if t["expires_at"] and str(t["expires_at"]) > _now]
        except Exception:
            reset_tokens = []

        # API usage stats
        api_usage_stats = {"total_cost": 0, "total_calls": 0, "by_model": [], "by_type": [], "recent": []}
        try:
            row = db.execute(
                "SELECT COUNT(*) as calls, SUM(input_tokens) as inp, SUM(output_tokens) as out, SUM(cost_usd) as cost FROM api_usage"
            ).fetchone()
            if row:
                api_usage_stats["total_calls"] = row["calls"] or 0
                api_usage_stats["total_input"]  = row["inp"] or 0
                api_usage_stats["total_output"] = row["out"] or 0
                api_usage_stats["total_cost"]   = round(row["cost"] or 0, 4)
            api_usage_stats["by_model"] = [dict(r) for r in db.execute(
                """SELECT model, COUNT(*) as calls,
                          SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens,
                          SUM(cost_usd) as cost_usd
                   FROM api_usage GROUP BY model ORDER BY cost_usd DESC"""
            ).fetchall()]
            api_usage_stats["by_type"] = [dict(r) for r in db.execute(
                """SELECT call_type, COUNT(*) as calls, SUM(cost_usd) as cost_usd
                   FROM api_usage GROUP BY call_type ORDER BY cost_usd DESC"""
            ).fetchall()]
            api_usage_stats["by_day"] = [dict(r) for r in db.execute(
                """SELECT substr(created_at,1,10) as day, COUNT(*) as calls,
                          SUM(cost_usd) as cost_usd
                   FROM api_usage GROUP BY day ORDER BY day DESC LIMIT 30"""
            ).fetchall()]
            api_usage_stats["recent"] = [dict(r) for r in db.execute(
                """SELECT a.created_at, a.model, a.call_type, a.input_tokens,
                          a.output_tokens, a.cost_usd, u.username
                   FROM api_usage a LEFT JOIN users u ON a.user_id=u.id
                   ORDER BY a.created_at DESC LIMIT 50"""
            ).fetchall()]
        except Exception:
            pass
    finally:
        db.close()
    return render_template("admin.html", users=users, stats=stats,
                           reset_tokens=reset_tokens,
                           api_usage=api_usage_stats,
                           username=session["username"], role=session["role"],
                           can_see_all=True, ROLE_LEVEL=ROLE_LEVEL)


def _pretty_bytes(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "0 B"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return (f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}")
        n /= 1024


@app.route("/api/admin/db-usage")
@require_role("admin")
def api_admin_db_usage():
    """Zużycie bazy: rozmiar bazy, największe tabele, miejsce zajęte przez mastery
    artworków; wolne miejsce liczone, gdy ustawiono DB_SIZE_LIMIT_GB."""
    import db as _dbmod
    is_pg = bool(getattr(_dbmod, "DATABASE_URL", ""))
    out = {"engine": "postgresql" if is_pg else "sqlite"}
    db = get_db()
    try:
        try:
            if is_pg:
                row = db.execute("SELECT pg_database_size(current_database()) AS sz").fetchone()
                out["db_bytes"] = int(row["sz"] or 0)
                out["tables"] = [{"name": r["name"], "pretty": _pretty_bytes(r["bytes"])}
                                 for r in db.execute(
                    "SELECT relname AS name, pg_total_relation_size(relid) AS bytes "
                    "FROM pg_catalog.pg_statio_user_tables "
                    "ORDER BY pg_total_relation_size(relid) DESC LIMIT 10").fetchall()]
            else:
                row = db.execute(
                    "SELECT (SELECT page_count FROM pragma_page_count())*"
                    "(SELECT page_size FROM pragma_page_size()) AS sz").fetchone()
                out["db_bytes"] = int(row["sz"] or 0)
                out["tables"] = []
            out["db_pretty"] = _pretty_bytes(out["db_bytes"])
        except Exception as e:
            out["error"] = f"Nie udało się odczytać rozmiaru bazy: {e}"
        try:
            import artwork_index as _ai
            _ai.ensure_master_file_table(db)
            r = db.execute("SELECT COUNT(*) AS c, COALESCE(SUM(LENGTH(data)),0) AS b "
                           "FROM artwork_master_file WHERE data IS NOT NULL").fetchone()
            out["masters_count"] = int(r["c"] or 0)
            out["masters_bytes"] = int(r["b"] or 0)
            out["masters_pretty"] = _pretty_bytes(out["masters_bytes"])
        except Exception:
            pass
        try:
            _lr = db.execute("SELECT value FROM settings WHERE category='admin' "
                             "AND key='db_size_limit_gb'").fetchone()
            _lim_setting = _lr["value"] if _lr else None
        except Exception:
            _lim_setting = None
        try:
            _ar = db.execute("SELECT value FROM settings WHERE category='admin' "
                             "AND key='artwork_budget_gb'").fetchone()
            _art_setting = _ar["value"] if _ar else None
        except Exception:
            _art_setting = None
    finally:
        db.close()
    lim = _lim_setting or os.environ.get("DB_SIZE_LIMIT_GB")
    if lim:
        try:
            lb = float(lim) * 1024 ** 3
            out["limit_gb"] = float(lim)
            out["limit_bytes"] = lb
            out["limit_pretty"] = _pretty_bytes(lb)
            out["free_bytes"] = max(0, lb - out.get("db_bytes", 0))
            out["free_pretty"] = _pretty_bytes(out["free_bytes"])
        except ValueError:
            pass
    if _art_setting:
        try:
            ab = float(_art_setting) * 1024 ** 3
            used = out.get("masters_bytes", 0)
            out["artwork_budget_gb"] = float(_art_setting)
            out["artwork_budget_pretty"] = _pretty_bytes(ab)
            out["artwork_free_pretty"] = _pretty_bytes(max(0, ab - used))
            out["artwork_pct"] = round(used * 100.0 / ab, 1) if ab > 0 else 0
        except ValueError:
            pass
    return jsonify(out)


@app.route("/api/admin/db-limit", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_admin_db_limit():
    """Zapisuje limit dysku (GB) i/lub budżet na artworki (GB) — do pasków w /admin."""
    data = request.get_json(silent=True) or {}
    saved = {}
    for field, key in (("limit_gb", "db_size_limit_gb"), ("artwork_gb", "artwork_budget_gb")):
        if field not in data:
            continue
        raw = str(data.get(field) or "").replace(",", ".").strip()
        try:
            val = float(raw)
            if val < 0:
                raise ValueError
        except ValueError:
            return jsonify({"error": f"Podaj liczbę GB w polu {field} (np. 160)"}), 400
        saved[key] = val
    if not saved:
        return jsonify({"error": "Brak wartości do zapisu"}), 400
    db = get_db()
    try:
        for key, val in saved.items():
            db.execute(
                "INSERT INTO settings(category,key,value) VALUES('admin',?,?) "
                "ON CONFLICT(category,key) DO UPDATE SET value=EXCLUDED.value",
                (key, str(val)))
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, **saved})


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — API COMPARE
# ─────────────────────────────────────────────────────────────────────────────

_B64_KEYS = frozenset({
    "img_a_b64", "img_b_b64", "img_diff_b64", "img_a_annot_b64", "img_b_annot_b64",
    "img_a_hires_b64", "img_b_hires_b64", "diff_overlay_b64", "page_a_b64", "page_b_b64",
    "thumb_b64", "thumb_data",
})

def _strip_images_from_result(d: dict) -> dict:
    """Recursively remove base64 image keys so result_json stays compact in DB."""
    if not isinstance(d, dict):
        return d
    out = {}
    for k, v in d.items():
        if k in _B64_KEYS:
            continue
        if isinstance(v, dict):
            out[k] = _strip_images_from_result(v)
        elif isinstance(v, list):
            out[k] = [_strip_images_from_result(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    return out


_VALID_COMPARISON_STATUSES = {"ok", "warning", "error", "critical", "blad", "ostrzezenie"}


def _save_comparison(uid, doc_type, file_a_name, file_b_name, result_dict,
                     hash_a=None, hash_b=None, path_a=None, path_b=None):
    """Helper: zapisuje porównanie do bazy."""
    # Strip large base64 image payloads before persisting (artwork images stay in session cache)
    result_dict = _strip_images_from_result(result_dict)
    # Apply length caps on caller-provided names
    file_a_name = str(file_a_name or "")[:500]
    file_b_name = str(file_b_name or "")[:500]
    # Wyciągnij dodatkowe metadane
    po_number = ''
    supplier_code = str(result_dict.get('supplier_detected') or '')[:50]
    total_a = ''
    total_b = ''

    # Szukaj numeru PO w modułach
    mods = result_dict.get('modules', {})
    tbl = mods.get('table', {})
    enh = mods.get('enhanced', {})
    # Total
    total_a = str(tbl.get('total_a') or enh.get('total_a') or '')
    total_b = str(tbl.get('total_b') or enh.get('total_b') or '')
    # Numer PO z nagłówków
    for mod in [tbl, enh]:
        for h in (mod.get('headers') or []):
            if h.get('key', '').lower() in ('numer po', 'po number', 'numer zamówienia / po'):
                v = h.get('val_a') or h.get('val_b') or ''
                if v and not po_number:
                    po_number = str(v)[:30]

    db = get_db()
    try:
        _rl = result_dict.get("risk_level", result_dict.get("status", "ok"))
        if _rl not in _VALID_COMPARISON_STATUSES:
            _rl = "ok"
        cursor = db.execute(
            """INSERT INTO comparisons
               (user_id,doc_type,file_a,file_b,result_json,status,diff_count,
                po_number,supplier_code,total_a,total_b,file_hash_a,file_hash_b)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (uid, doc_type[:50], file_a_name, file_b_name,
             json.dumps(result_dict, ensure_ascii=False),
             _rl,
             int(result_dict.get("total_errors", result_dict.get("diff_count",
                 result_dict.get("error_count", 0))) or 0),
             po_number, supplier_code, str(total_a)[:50], str(total_b)[:50], hash_a, hash_b)
        )
        cid = cursor.lastrowid
        db.commit()
        _kpi_cache_invalidate()
        status = result_dict.get("risk_level", result_dict.get("status", "ok"))
        # Fire webhook for external integrations (non-blocking)
        try:
            _fire_webhooks_bg("comparison.completed", {
                "comparison_id": cid,
                "doc_type": doc_type,
                "status": status,
                "diff_count": result_dict.get("diff_count", 0),
                "supplier": supplier_code,
                "po_number": po_number,
            })
        except Exception:
            pass
        # Email alert for critical results
        if status == "critical":
            try:
                urow = db.execute("SELECT username, email FROM users WHERE id=?", (uid,)).fetchone()
                if urow and urow["email"]:
                    _send_critical_alert(
                        username=urow["username"],
                        to_email=urow["email"],
                        cid=cid,
                        doc_type=doc_type,
                        file_a=file_a_name,
                        file_b=file_b_name,
                        diff_count=result_dict.get("diff_count", result_dict.get("total_errors", 0)),
                        po_number=po_number,
                    )
            except Exception:
                pass
        # Auto-create shipment record when PO is detected
        if po_number:
            try:
                _ensure_shipment(po_number, supplier_code, cid, uid)
            except Exception:
                pass
            # Auto-save uploaded files to shipment document folder
            if path_a and os.path.exists(path_a):
                _save_doc_to_shipment(po_number, path_a, file_a_name, doc_type, uid)
            if path_b and os.path.exists(path_b):
                _save_doc_to_shipment(po_number, path_b, file_b_name, doc_type, uid)
        # Notifications
        try:
            _fa = (file_a_name or "")[:200]
            _fb = (file_b_name or "")[:200]
            if doc_type == "SAD":
                _notify_role("manager", "sad_pending",
                             f"🛃 Nowe SAD do zatwierdzenia — {(po_number or _fa)[:200]}",
                             f"Plik: {_fa} vs {_fb}",
                             f"/sad")
            elif status in ("error", "critical"):
                diff = result_dict.get("diff_count", result_dict.get("total_errors", 0))
                _create_notification(uid, "compare_error",
                                     f"❌ Rozbieżność w porównaniu — {diff} błędów",
                                     f"{doc_type}: {_fa} vs {_fb}",
                                     f"/history")
        except Exception:
            pass
        return cid
    finally:
        db.close()


# Zadania kolejkowalne (Faza 3): ciężkie silniki porównań wołane synchronicznie.
# Wyodrębnione do funkcji modułowych, by `jobs.run(...)` mogło policzyć je w workerze
# (gdy REDIS_URL) i zwrócić wynik bez zmiany kontraktu HTTP. Argumenty lekkie
# (ścieżki/typy/teksty), zwrot to dict — serializowalne przez RQ.
def _compare_documents_job(text_a, text_b, doc_type):
    return compare_documents(text_a, text_b, doc_type).to_dict()


def _compare_enhanced_job(path_a, path_b, doc_type, supplier):
    from enhanced_comparator import compare_enhanced
    return compare_enhanced(path_a, path_b, doc_type, doc_type, supplier).to_dict()


def _compare_tables_job(path_a, path_b):
    from table_extractor import compare_tables
    return compare_tables(path_a, path_b).to_dict()


@app.route("/api/compare", methods=["POST"])
@login_required
@csrf_protect
def api_compare():
    uid = session["user_id"]
    if _check_and_record_api_rate(uid):
        return jsonify({"error": f"Zbyt wiele żądań. Odczekaj minutę i spróbuj ponownie."}), 429
    file_a = request.files.get("file_a")
    file_b = request.files.get("file_b")
    doc_type = request.form.get("doc_type", "auto")
    _VALID_DOC_TYPES = {"auto", "PO", "PI", "CI", "PL", "SAD", "BL", "WZ", "FV", "MULTI", "CMR", "artwork"}
    if doc_type not in _VALID_DOC_TYPES:
        return jsonify({"error": f"Nieprawidłowy typ dokumentu: {doc_type}"}), 400
    use_ai = request.form.get("use_ai", "0") == "1"
    # Wybór silnika OCR (test A/B): 'cascade' (domyślny) lub wymuszony pojedynczy.
    import table_extractor as _te
    ocr_engine = (request.form.get("ocr_engine") or "cascade").strip().lower()
    if ocr_engine not in _te.OCR_ENGINES:
        ocr_engine = "cascade"
    if ocr_engine == "glm" and not _te._glm_ocr_configured():
        return jsonify({"error": "GLM-OCR nie jest skonfigurowany — ustaw zmienną "
                        "GLM_OCR_URL (endpoint self-host/API)."}), 400
    for f in (file_a, file_b):
        err = _validate_pdf_upload(f)
        if err:
            return jsonify({"error": err}), 400

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    _sname_a = secure_filename(file_a.filename) or "document_a.pdf"
    _sname_b = secure_filename(file_b.filename) or "document_b.pdf"
    import uuid as _uuid_cmp
    _cmp_uniq = _uuid_cmp.uuid4().hex[:8]
    path_a = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_a_{_cmp_uniq}_{_sname_a}")
    path_b = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_b_{_cmp_uniq}_{_sname_b}")
    file_a.save(path_a)
    file_b.save(path_b)
    _decrypt_pdf_inplace(path_a)
    _decrypt_pdf_inplace(path_b)

    try:
        import hashlib as _hl
        def _file_sha256(p):
            h = _hl.sha256()
            with open(p, 'rb') as _f:
                for _chunk in iter(lambda: _f.read(65536), b''):
                    h.update(_chunk)
            return h.hexdigest()
        _hash_a = _file_sha256(path_a)
        _hash_b = _file_sha256(path_b)
        _db = get_db()
        try:
            _existing = _db.execute(
                "SELECT id FROM comparisons WHERE file_hash_a=? AND file_hash_b=? AND user_id=?",
                (_hash_a, _hash_b, uid)
            ).fetchone()
        finally:
            _db.close()
        if _existing:
            return jsonify({"duplicate": True, "existing_id": _existing["id"]}), 200

        try:
            text_a = extract_pdf_text(path_a)
            text_b = extract_pdf_text(path_b)
        except ValueError as e:
            return jsonify({"error": str(e)[:300]}), 400
        except Exception as e:
            logger.exception("PDF read error: %s", e)
            return jsonify({"error": "Błąd odczytu pliku PDF — sprawdź, czy plik nie jest uszkodzony."}), 500

        # Pre-detect doc type with the richer 9-type detector so both comparators
        # receive the correctly identified type instead of running with "auto".
        effective_doc_type = doc_type
        if doc_type == "auto":
            try:
                from pdf_extractor import detect_doc_type as _pex_detect
                _dt_a, _conf_a = _pex_detect(text_a)
                _dt_b, _conf_b = _pex_detect(text_b)
                _best = _dt_a if _conf_a >= _conf_b else _dt_b
                if _best and _best != "auto":
                    effective_doc_type = _best
            except Exception:
                pass

        _te.set_ocr_engine(ocr_engine)
        with _HEAVY_SEM:
            try:
                # Faza 3: silnik porównań liczy worker (RQ) gdy REDIS_URL, inaczej inline.
                import jobs as _jobs
                result_dict = _jobs.run(_compare_documents_job, text_a, text_b, effective_doc_type)
                if effective_doc_type != "auto":
                    result_dict.setdefault("doc_type_detected", effective_doc_type)
            except Exception as e:
                logger.exception("Compare analysis error: %s", e)
                return jsonify({"error": "Błąd analizy dokumentów — spróbuj ponownie."}), 500

            # Uruchom enhanced comparator jako uzupełnienie dla lepszej ekstrakcji tabel.
            # Jeśli stary silnik nie wyciągnął pozycji (line_items=[]), użyj wyników
            # enhanced (który ma Vision fallback i poprawiony fitz/pdfplumber).
            try:
                from enhanced_comparator import compare_enhanced
                from supplier_profiles import detect_supplier
                _sp = detect_supplier(text_a + ' ' + text_b)
                _ed = _jobs.run(_compare_enhanced_job, path_a, path_b, effective_doc_type, _sp)
                # Uzupełnij line_items gdy stary silnik nic nie znalazł
                if not result_dict.get("line_items") and _ed.get("items"):
                    result_dict["line_items"] = [
                        {
                            "lp": i + 1,
                            "name": (it.get("ref") or "") + (
                                f" — {it['desc_a']}" if it.get("desc_a") else ""),
                            "qty_a": it.get("qty_a", ""), "qty_b": it.get("qty_b", ""),
                            "price_a": it.get("price_a", ""), "price_b": it.get("price_b", ""),
                            "value_a": it.get("net_a", ""), "value_b": it.get("net_b", ""),
                            "vat_a": "", "vat_b": "",
                            "status": it.get("status", "ok"),
                            "comment": "; ".join(str(_x) for _x in (it.get("issues") or []) if _x),
                        }
                        for i, it in enumerate(_ed["items"])
                    ]
                # Dodaj ostrzeżenia z enhanced (metoda ekstrakcji, Vision, itd.)
                if _ed.get("warnings"):
                    result_dict.setdefault("warnings", [])
                    result_dict["warnings"].extend(_ed["warnings"])
                # Jeśli doc type auto i enhanced wykrył konkretny — użyj go
                if result_dict.get("doc_type_detected") in ("auto", "", None):
                    result_dict["doc_type_detected"] = _ed.get("doc_type_a", "auto")
                # Build ME22N panel from extracted header fields
                _me22n = {}
                for _hdr in (_ed.get("headers") or []):
                    k = _hdr.get("key", "")
                    if k in ("ETD", "ETA", "Data dostawy", "Port załadunku", "Port rozładunku"):
                        _me22n[k] = {
                            "val_a": _hdr.get("val_a", ""),
                            "val_b": _hdr.get("val_b", ""),
                            "status": _hdr.get("status", "ok"),
                        }
                if _me22n:
                    result_dict["me22n_panel"] = _me22n
            except Exception as _enh_err:
                logger.warning("enhanced_comparator failed (non-blocking): %s", _enh_err)

            # Wymuszony silnik OCR (test A/B): pozycje liczy compare_tables, które
            # respektuje override (kaskada/GLM/pojedyncza biblioteka). Inline — działa
            # też przy workerze RQ (override jest contextvarem bieżącego wątku żądania).
            if ocr_engine != "cascade":
                try:
                    from table_extractor import compare_tables as _ct
                    _tt = _ct(path_a, path_b).to_dict()
                    _titems = _tt.get("items") or []
                    result_dict["line_items"] = [
                        {
                            "lp": i + 1,
                            "name": (it.get("ref") or "") + (
                                f" — {it['desc_a']}" if it.get("desc_a") else ""),
                            "qty_a": it.get("qty_a", ""), "qty_b": it.get("qty_b", ""),
                            "price_a": it.get("price_a", ""), "price_b": it.get("price_b", ""),
                            "value_a": it.get("net_a", ""), "value_b": it.get("net_b", ""),
                            "vat_a": "", "vat_b": "",
                            "status": it.get("status", "ok"),
                            "comment": "; ".join(str(_x) for _x in (it.get("issues") or []) if _x),
                        }
                        for i, it in enumerate(_titems)
                    ]
                    result_dict.setdefault("warnings", []).append(
                        f"🔬 Silnik OCR: {ocr_engine} — wyodrębniono {len(_titems)} pozycji "
                        f"(tryb testowy A/B).")
                    result_dict["ocr_engine"] = ocr_engine
                except Exception as _oe:
                    logger.warning("OCR-engine override (%s) failed: %s", ocr_engine, _oe)
                    result_dict.setdefault("warnings", []).append(
                        f"⚠️ Silnik OCR {ocr_engine}: ekstrakcja nie powiodła się ({str(_oe)[:120]}).")

            if use_ai:
                try:
                    from ai_validator import validate_comparison_result
                    result_dict = validate_comparison_result(result_dict, mode="regex")
                except Exception as e:
                    logger.error("AI validation unavailable: %s", e)
                    result_dict["ai_validation"] = {"error": str(e)[:200]}

        # PO file validation (filename + content check)
        try:
            _po_for_val = result_dict.get("po_number") or ""
            if not _po_for_val:
                # Try to extract from per-module headers
                _mods_fb = result_dict.get("modules", {})
                for _mod in list(_mods_fb.values()):
                    for _hdr in (_mod.get("headers") or []) if isinstance(_mod, dict) else []:
                        if str(_hdr.get("key") or "").lower() in ("numer po","po number"):
                            _po_for_val = str(_hdr.get("val_a") or _hdr.get("val_b") or "")[:30]
                            if _po_for_val:
                                break
                    if _po_for_val:
                        break
            if _po_for_val:
                with open(path_a, "rb") as _fa, open(path_b, "rb") as _fb:
                    _val_a = _validate_file_po(file_a.filename, _fa.read(), _po_for_val)
                    _val_b = _validate_file_po(file_b.filename, _fb.read(), _po_for_val)
                result_dict["file_validation"] = {
                    "po_number": _po_for_val,
                    "file_a": _val_a,
                    "file_b": _val_b,
                    "both_ok": _val_a["ok"] and _val_b["ok"],
                }
        except Exception as _val_err:
            pass

        try:
            result_dict["comparison_id"] = _save_comparison(
                uid, result_dict.get("doc_type_detected", "auto"),
                file_a.filename, file_b.filename, result_dict,
                hash_a=_hash_a, hash_b=_hash_b,
                path_a=path_a, path_b=path_b)
        except Exception as _save_err:
            logger.error("_save_comparison failed: %s", _save_err)
            result_dict["save_error"] = str(_save_err)[:200]
        return jsonify(result_dict)
    finally:
        for _p in (path_a, path_b):
            try: os.remove(_p)
            except OSError: pass


@app.route("/api/table_compare", methods=["POST"])
@login_required
@csrf_protect
def api_table_compare():
    _uid = session["user_id"]
    if _check_and_record_api_rate(_uid):
        return jsonify({"error": "Zbyt wiele żądań. Odczekaj minutę i spróbuj ponownie."}), 429
    import table_extractor as _te
    file_a = request.files.get("file_a")
    file_b = request.files.get("file_b")
    use_ai = request.form.get("use_ai", "0") == "1"
    if not file_a or not file_b:
        return jsonify({"error": "Wymagane dwa pliki PDF"}), 400
    for f in (file_a, file_b):
        err = _validate_pdf_upload(f)
        if err:
            return jsonify({"error": err}), 400

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    uid = session["user_id"]
    import uuid as _uuid_tc
    _tc_uniq = _uuid_tc.uuid4().hex[:8]
    path_a = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_ta_{_tc_uniq}_{secure_filename(file_a.filename) or 'doc_a.pdf'}")
    path_b = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_tb_{_tc_uniq}_{secure_filename(file_b.filename) or 'doc_b.pdf'}")

    try:
        file_a.save(path_a)
        file_b.save(path_b)
        try:
            # Faza 3: ekstrakcja/porównanie tabel liczy worker (RQ) gdy REDIS_URL,
            # inaczej inline pod semaforem (ochrona przed OOM bez kolejki).
            import jobs as _jobs
            result_dict = _jobs.run(_compare_tables_job, path_a, path_b, inline_sem=_HEAVY_SEM)
        except Exception as e:
            logger.exception("Table compare error: %s", e)
            return jsonify({"error": "Błąd porównania tabeli — spróbuj ponownie.", "table_found": False}), 500

        if use_ai:
            try:
                from ai_validator import validate_comparison_result
                result_dict = validate_comparison_result(result_dict, mode="table")
            except Exception as e:
                result_dict["ai_validation"] = {"error": str(e)[:200]}

        label = f"{result_dict.get('doc_type_a', 'PDF-A')} vs {result_dict.get('doc_type_b', 'PDF-B')}"
        result_dict["comparison_id"] = _save_comparison(uid, label, file_a.filename, file_b.filename, result_dict)
        return jsonify(result_dict)
    finally:
        _te.set_ocr_engine("cascade")  # nie pozwól, by override wyciekł na kolejne żądanie
        for _p in (path_a, path_b):
            try: os.remove(_p)
            except OSError: pass


@app.route("/api/typo_compare", methods=["POST"])
@login_required
@csrf_protect
def api_typo_compare():
    _uid_tc = session["user_id"]
    if _check_and_record_api_rate(_uid_tc):
        return jsonify({"error": "Zbyt wiele żądań. Odczekaj minutę i spróbuj ponownie."}), 429
    file_a = request.files.get("file_a")
    file_b = request.files.get("file_b")
    use_ai = request.form.get("use_ai", "0") == "1"
    if not file_a or not file_b:
        return jsonify({"error": "Wymagane dwa pliki PDF"}), 400
    for f in (file_a, file_b):
        err = _validate_pdf_upload(f)
        if err:
            return jsonify({"error": err}), 400

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    uid = session["user_id"]
    # BUGFIX: unikalny suffix — równoległe żądania tego samego usera nie nadpisują plików
    _ty_uniq = __import__("uuid").uuid4().hex[:8]
    path_a = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_ty_a_{_ty_uniq}_{secure_filename(file_a.filename) or 'doc_a.pdf'}")
    path_b = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_ty_b_{_ty_uniq}_{secure_filename(file_b.filename) or 'doc_b.pdf'}")

    try:
        file_a.save(path_a)
        file_b.save(path_b)
        try:
            text_a = extract_pdf_text(path_a)
            text_b = extract_pdf_text(path_b)
        except Exception as e:
            logger.exception("PDF read error: %s", e)
            return jsonify({"error": "Błąd odczytu pliku PDF — sprawdź, czy plik nie jest uszkodzony."}), 500

        try:
            from typo_detector import analyze_full_texts
            report = analyze_full_texts(text_a, text_b, file_a.filename, file_b.filename)
            result_dict = report.to_dict()
        except Exception as e:
            logger.exception("Typo analysis error: %s", e)
            return jsonify({"error": "Błąd analizy — spróbuj ponownie."}), 500

        status = "blad" if result_dict["error_count"] > 0 else \
                 "ostrzezenie" if result_dict["warning_count"] > 0 else "ok"
        result_dict["risk_level"] = status

        if use_ai:
            try:
                from ai_validator import validate_comparison_result
                result_dict = validate_comparison_result(result_dict, mode="typo")
            except Exception as e:
                result_dict["ai_validation"] = {"error": str(e)[:200]}

        result_dict["comparison_id"] = _save_comparison(
            uid, "Analiza literówek",
            file_a.filename, file_b.filename, result_dict)
        return jsonify(result_dict)
    finally:
        for _p in (path_a, path_b):
            try: os.remove(_p)
            except OSError: pass


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — ASYNC COMPARE (#12) — fire-and-forget using background thread
# ─────────────────────────────────────────────────────────────────────────────

# Zadanie kolejkowalne (Faza 2): asynchroniczne porownanie dokumentow.
# Wyodrebnione z domkniecia route'a /api/compare-async do funkcji modulowej,
# by RQ moglo je zserializowac (postep w tabeli async_jobs — miedzyprocesowo).
def _compare_async_job(jid, path_a, path_b, doc_type, use_ai, name_a, name_b, uid):
    try:
        _async_job_set(jid, "running", step=1, step_label="Odczyt PDF…", pct=10)
        text_a = extract_pdf_text(path_a)
        text_b = extract_pdf_text(path_b)
        _async_job_set(jid, "running", step=2, step_label="Wykrywanie typu dokumentu…", pct=25)
        eff_dt = doc_type
        if doc_type == "auto":
            try:
                from pdf_extractor import detect_doc_type as _pex_d
                _da, _ca = _pex_d(text_a); _db, _cb = _pex_d(text_b)
                eff_dt = _da if _ca >= _cb else _db
            except Exception:
                pass
        _async_job_set(jid, "running", step=3, step_label="Porównywanie dokumentów…", pct=50)
        with _HEAVY_SEM:
            result = compare_documents(text_a, text_b, eff_dt)
            rd = result.to_dict()
        if use_ai:
            _async_job_set(jid, "running", step=4, step_label="Walidacja AI…", pct=80)
            try:
                from ai_validator import validate_comparison_result
                rd = validate_comparison_result(rd, mode="regex")
            except Exception:
                pass
        _async_job_set(jid, "running", step=5, step_label="Zapis wyników…", pct=90)
        cid = _save_comparison(uid, eff_dt, name_a, name_b, rd)
        rd["comparison_id"] = cid
        _async_job_set(jid, "done", result=rd, pct=100)
    except Exception as exc:
        _async_job_set(jid, "error", error=str(exc)[:200])
    finally:
        for _p in (path_a, path_b):
            try: os.remove(_p)
            except OSError: pass


@app.route("/api/compare-async", methods=["POST"])
@login_required
@csrf_protect
def api_compare_async_start():
    """Start an asynchronous comparison.  Returns immediately with a job_id.
    Poll GET /api/compare-async/<job_id> for status and result.
    Accepts same form-data as POST /api/compare.
    """
    uid = session["user_id"]
    if _check_and_record_api_rate(uid):
        return jsonify({"error": "Zbyt wiele żądań. Odczekaj minutę i spróbuj ponownie."}), 429
    file_a = request.files.get("file_a")
    file_b = request.files.get("file_b")
    doc_type = request.form.get("doc_type", "auto")
    _VALID_DOC_TYPES = {"auto", "PO", "PI", "CI", "PL", "SAD", "BL", "WZ", "FV", "MULTI", "CMR", "artwork"}
    if doc_type not in _VALID_DOC_TYPES:
        return jsonify({"error": f"Nieprawidłowy typ dokumentu: {doc_type}"}), 400
    use_ai = request.form.get("use_ai", "0") == "1"
    for f in (file_a, file_b):
        err = _validate_pdf_upload(f)
        if err:
            return jsonify({"error": err}), 400

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    _aa_uniq = __import__("uuid").uuid4().hex[:8]
    path_a = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_aa_{_aa_uniq}_{secure_filename(file_a.filename) or 'art_a.pdf'}")
    path_b = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_ab_{_aa_uniq}_{secure_filename(file_b.filename) or 'art_b.pdf'}")
    file_a.save(path_a)
    file_b.save(path_b)
    name_a, name_b = file_a.filename, file_b.filename

    jid = _async_job_create(uid)

    # Faza 2: licz w workerze (RQ) gdy REDIS_URL, inaczej w watku.
    import jobs as _jobs
    _jobs.enqueue(_compare_async_job, jid, path_a, path_b, doc_type, use_ai, name_a, name_b, uid)
    return jsonify({"job_id": jid, "status": "pending"}), 202


@app.route("/api/compare-async/<job_id>")
@login_required
def api_compare_async_status(job_id):
    """Poll job status for an async comparison started via POST /api/compare-async."""
    entry = _async_job_get(job_id)
    if entry is None:
        return jsonify({"error": "Nieznane zadanie lub wygasło"}), 404
    if entry.get("user_id") != session["user_id"]:
        return jsonify({"error": "Brak dostępu"}), 403
    resp = {"job_id": job_id, "status": entry["status"],
            "step": entry.get("step", 0), "step_label": entry.get("step_label", ""),
            "pct": entry.get("pct", 0)}
    if entry["status"] == "done":
        resp["result"] = entry.get("result")
    elif entry["status"] == "error":
        resp["error"] = entry.get("error", "Nieznany błąd")
    return jsonify(resp)


@app.route("/api/compare-async/<job_id>/events")
@login_required
def api_compare_async_events(job_id):
    """Server-Sent Events stream for async comparison progress.

    Emits JSON data events until the job reaches 'done' or 'error'.
    Example client: new EventSource('/api/compare-async/<id>/events')
    """
    _owner_id = session["user_id"]

    def _generate():
        import json as _j
        last_pct = -1
        for _ in range(600):  # max 5 min at 0.5s poll
            entry = _async_job_get(job_id)
            if entry is None:
                yield f"data: {_j.dumps({'error': 'Zadanie wygasło lub nie istnieje'})}\n\n"
                break
            if entry.get("user_id") != _owner_id:
                yield f"data: {_j.dumps({'error': 'Brak dostępu'})}\n\n"
                break
            payload = {
                "job_id": job_id,
                "status": entry["status"],
                "step": entry.get("step", 0),
                "step_label": entry.get("step_label", ""),
                "pct": entry.get("pct", 0),
            }
            if entry["status"] in ("done", "error"):
                if entry["status"] == "error":
                    payload["error"] = entry.get("error", "")
                yield f"data: {_j.dumps(payload)}\n\n"
                break
            if payload["pct"] != last_pct:
                last_pct = payload["pct"]
                yield f"data: {_j.dumps(payload)}\n\n"
            time.sleep(0.5)
    from flask import Response
    return Response(_generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ─────────────────────────────────────────────────────────────────────────────
# ROUTE — BATCH ZIP COMPARE (#36)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/compare-zip", methods=["POST"])
@login_required
@csrf_protect
def api_compare_zip():
    """Accept a ZIP archive containing PDFs and compare them in sorted pairs.

    Pairs up PDFs alphabetically: (0,1), (2,3), …
    Returns an array of comparison results, one per pair.
    Limits: max 20 PDFs per ZIP.
    """
    import zipfile as _zf
    uid  = session["user_id"]
    if _check_and_record_api_rate(uid):
        return jsonify({"error": "Zbyt wiele żądań. Odczekaj minutę i spróbuj ponownie."}), 429

    zf = request.files.get("file")
    if not zf or not zf.filename:
        return jsonify({"error": "Brak pliku ZIP"}), 400
    if not zf.filename.lower().endswith(".zip"):
        return jsonify({"error": "Wymagany plik ZIP"}), 400

    doc_type = request.form.get("doc_type", "auto")
    upload_dir = app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_dir, exist_ok=True)
    zip_path = os.path.join(upload_dir, f"{uid}_batch_{secure_filename(zf.filename) or 'batch.zip'}")
    zf.save(zip_path)

    extracted = []
    try:
        with _zf.ZipFile(zip_path, "r") as zarch:
            pdf_names = sorted(
                n for n in zarch.namelist()
                if n.lower().endswith(".pdf") and not n.startswith("__MACOSX")
            )
            if not pdf_names:
                return jsonify({"error": "ZIP nie zawiera plików PDF"}), 400
            if len(pdf_names) > 20:
                return jsonify({"error": f"Zbyt wiele plików PDF w ZIP (max 20, znaleziono {len(pdf_names)})"}), 400
            _MAX_PDF_BYTES = 100 * 1024 * 1024  # 100 MB per file
            for _bz_idx, name in enumerate(pdf_names):
                if zarch.getinfo(name).file_size > _MAX_PDF_BYTES:
                    return jsonify({"error": f"Plik {os.path.basename(name)} w ZIP jest zbyt duży (max 100 MB)."}), 400
                _sname = secure_filename(os.path.basename(name)) or f"file_{_bz_idx}.pdf"
                safe = os.path.join(upload_dir, f"{uid}_bz_{_bz_idx}_{_sname}")
                with zarch.open(name) as src, open(safe, "wb") as dst:
                    _written = 0
                    for _chunk in iter(lambda: src.read(65536), b""):
                        _written += len(_chunk)
                        if _written > _MAX_PDF_BYTES:
                            dst.close()
                            try: os.remove(safe)
                            except OSError: pass
                            return jsonify({"error": f"Plik {os.path.basename(name)} przekracza 100 MB po rozpakowaniu."}), 400
                        dst.write(_chunk)
                extracted.append((os.path.basename(name), safe))
    except _zf.BadZipFile:
        return jsonify({"error": "Plik ZIP jest uszkodzony lub nieprawidłowy"}), 400
    finally:
        try: os.remove(zip_path)
        except OSError: pass

    if len(extracted) % 2 != 0:
        for _, p in extracted:
            try: os.remove(p)
            except OSError: pass
        return jsonify({
            "error": f"Liczba plików PDF musi być parzysta (znaleziono {len(extracted)}). "
                     "Kolejność par: plik A, plik B, plik A, plik B …"
        }), 400

    results = []
    try:
        with _HEAVY_SEM:
            for i in range(0, len(extracted), 2):
                name_a, path_a = extracted[i]
                name_b, path_b = extracted[i + 1]
                pair_result = {"pair": i // 2 + 1, "file_a": name_a, "file_b": name_b}
                try:
                    text_a = extract_pdf_text(path_a)
                    text_b = extract_pdf_text(path_b)
                    # Pre-detect doc type
                    eff_dt = doc_type
                    if doc_type == "auto":
                        try:
                            from pdf_extractor import detect_doc_type as _pex_dt
                            _da, _ca = _pex_dt(text_a)
                            _db, _cb = _pex_dt(text_b)
                            eff_dt = _da if _ca >= _cb else _db
                        except Exception:
                            pass
                    result = compare_documents(text_a, text_b, eff_dt)
                    rd = result.to_dict()
                    rd["doc_type_detected"] = eff_dt
                    cid = _save_comparison(uid, eff_dt, name_a, name_b, rd)
                    rd["comparison_id"] = cid
                    pair_result.update(rd)
                except Exception as exc:
                    pair_result["error"] = str(exc)[:200]
                results.append(pair_result)
    finally:
        for _, p in extracted:
            try: os.remove(p)
            except OSError: pass

    return jsonify({"pairs": results, "total": len(results)})


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — API DATA
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/comparison/<int:cid>")
@login_required
def get_comparison(cid):
    uid = session["user_id"]
    role = session["role"]
    db = get_db()
    try:
        row = db.execute("""
            SELECT c.*, u.username
            FROM comparisons c
            JOIN users u ON c.user_id = u.id
            WHERE c.id = ?
            AND (c.is_deleted = 0 OR c.is_deleted IS NULL)
        """, (cid,)).fetchone()
    finally:
        db.close()
    if not row:
        return jsonify({"error": "Nie znaleziono"}), 404
    if not can_see_all(role) and row["user_id"] != uid:
        return jsonify({"error": "Brak dostępu"}), 403
    try:
        data = json.loads(row["result_json"] or "{}")
    except (ValueError, TypeError):
        return jsonify({"error": "Dane porównania uszkodzone"}), 500
    # Dodaj metadane z bazy (mogą być nowsze/pełniejsze niż w result_json)
    data["comparison_id"]  = cid
    data["username"]       = row["username"]
    data["created_at"]     = row["created_at"]
    data["db_status"]      = row["status"]
    data["db_diff_count"]  = row["diff_count"]
    return jsonify(data)


@app.route("/api/comparison/<int:cid>", methods=["DELETE"])
@login_required
@csrf_protect
def delete_comparison(cid):
    uid = session["user_id"]
    role = session["role"]
    if not can_delete(role):
        return jsonify({"error": "Brak uprawnień do usuwania"}), 403

    db = get_db()
    try:
        row = db.execute("SELECT id, user_id FROM comparisons WHERE id = ?", (cid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        # IDOR fix: superuser may delete only own records unless they can_see_all
        if row["user_id"] != uid and not can_see_all(role):
            return jsonify({"error": "Brak dostępu do tego zasobu"}), 403
        # Soft delete — set is_deleted flag instead of removing the row
        db.execute(
            "UPDATE comparisons SET is_deleted=1, deleted_at=datetime('now'), deleted_by=? WHERE id=?",
            (uid, cid)
        )
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, "deleted_id": cid})


@app.route("/api/comparisons/bulk_delete", methods=["POST"])
@login_required
@csrf_protect
def bulk_delete_comparisons():
    if not can_delete(session["role"]):
        return jsonify({"error": "Brak uprawnień"}), 403
    data = request.get_json(silent=True) or {}
    raw_ids = data.get("ids", [])
    # Strictly validate: only accept positive integers — prevents SQL injection
    try:
        ids = [int(x) for x in raw_ids if str(x).strip().lstrip("-").isdigit() and int(x) > 0]
    except (ValueError, TypeError):
        return jsonify({"error": "Nieprawidłowe ID"}), 400
    if not ids:
        return jsonify({"error": "Brak ID do usunięcia"}), 400
    if len(ids) > 200:
        return jsonify({"error": "Zbyt wiele ID naraz (max 200)"}), 400

    uid = session["user_id"]
    role = session["role"]
    db = get_db()
    try:
        placeholders = ",".join("?" * len(ids))
        # Soft-delete: set is_deleted flag instead of removing rows
        if can_see_all(role):
            cursor = db.execute(
                # bandit: lista placeholderów ? budowana z len(); wartości parametryzowane
                f"UPDATE comparisons SET is_deleted=1, deleted_at=datetime('now'), deleted_by=?"  # nosec B608
                f" WHERE id IN ({placeholders}) AND is_deleted=0",
                [uid] + ids
            )
        else:
            cursor = db.execute(
                # bandit: lista placeholderów ? budowana z len(); wartości parametryzowane
                f"UPDATE comparisons SET is_deleted=1, deleted_at=datetime('now'), deleted_by=?"  # nosec B608
                f" WHERE id IN ({placeholders}) AND user_id=? AND is_deleted=0",
                [uid] + ids + [uid]
            )
        deleted = cursor.rowcount
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/comparison/<int:cid>/restore", methods=["POST"])
@login_required
@csrf_protect
def restore_comparison(cid):
    """Przywraca miękko usuniętą pozycję z historii."""
    uid = session["user_id"]
    role = session["role"]
    db = get_db()
    try:
        row = db.execute("SELECT id, user_id, is_deleted FROM comparisons WHERE id=?", (cid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        if not row["is_deleted"]:
            return jsonify({"error": "Pozycja nie jest usunięta"}), 400
        if row["user_id"] != uid and not can_see_all(role):
            return jsonify({"error": "Brak dostępu"}), 403
        db.execute(
            "UPDATE comparisons SET is_deleted=0, deleted_at=NULL, deleted_by=NULL WHERE id=?",
            (cid,)
        )
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, "restored_id": cid})


@app.route("/api/comparison/<int:cid>/approve", methods=["POST"])
@require_role("manager")
@csrf_protect
def approve_comparison(cid):
    """Zatwierdza lub odrzuca porównanie (workflow compliance)."""
    data = request.get_json(silent=True) or {}
    verdict = data.get("status", "approved")
    if verdict not in ("approved", "rejected", "pending"):
        return jsonify({"error": "Nieprawidłowy status. Użyj: approved, rejected, pending"}), 400
    note = str(data.get("note") or "")[:500]
    uid = session["user_id"]
    db = get_db()
    try:
        row = db.execute("SELECT id FROM comparisons WHERE id=? AND (is_deleted IS NULL OR is_deleted=0)", (cid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        comp = db.execute(
            "SELECT doc_type, po_number FROM comparisons WHERE id=?", (cid,)
        ).fetchone()
        db.execute(
            "UPDATE comparisons SET approval_status=?, approved_by=?, approved_at=datetime('now'), approval_note=? WHERE id=?",
            (verdict, uid, note, cid)
        )
        db.commit()
        # SAD approved → advance matching shipment to 'odprawa'
        if comp and comp["doc_type"] == "SAD" and verdict == "approved":
            try:
                po = comp["po_number"] or ""
                if po:
                    db.execute(
                        "UPDATE shipments SET status='odprawa', updated_at=datetime('now') "
                        "WHERE po_number=? AND status NOT IN ('w_magazynie','zakonczone')",
                        (po,)
                    )
                    db.commit()
            except Exception:
                pass
        _log_audit("comparison_approval", session["username"],
                   f"cid={cid} verdict={verdict} note={note[:80]}")
    finally:
        db.close()
    return jsonify({"ok": True, "status": verdict, "cid": cid})


@app.route("/api/kpi/data")
@login_required
def api_kpi_data():
    """JSON endpoint dla KPI — do dynamicznych wykresów."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from collections import defaultdict as _dd
    role = session["role"]
    uid = session["user_id"]

    period = request.args.get("period", "30")
    user_filter = request.args.get("user_id", "all").strip()[:20]

    # Oblicz datę graniczną w Pythonie (kompatybilne z SQLite i PostgreSQL)
    try:
        period_days = max(1, min(int(period), 3650))
    except (ValueError, TypeError):
        period_days = 30

    # Normalize user_filter to "all" or a valid integer string
    if user_filter != "all":
        try:
            user_filter = str(int(user_filter))
        except (ValueError, TypeError):
            user_filter = "all"

    # Cache key: effective uid (or "all" for admins), period, user filter
    uid_key = "all" if can_see_all(role) else uid
    cache_key = (uid_key, period_days, user_filter)
    cached = _kpi_cache_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    db = get_db()

    since = (_dt.now(_tz.utc) - _td(days=period_days)).strftime("%Y-%m-%d")

    base_where = "WHERE c.created_at >= ?"
    base_params = [since]

    if not can_see_all(role):
        base_where += " AND c.user_id = ?"
        base_params.append(uid)
    elif user_filter != "all":
        try:
            fuid = int(user_filter)
            base_where += " AND c.user_id = ?"
            base_params.append(fuid)
        except (ValueError, TypeError):
            pass

    try:
        # Pobierz surowe wiersze i agreguj w Pythonie (unika strftime/date SQLite-specific)
        # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
        raw_rows = db.execute(f"""
            SELECT c.created_at, c.status, c.doc_type, c.supplier_code, c.approval_status
            FROM comparisons c {base_where}
            LIMIT 100000
        """, base_params).fetchall()  # nosec B608
        raw_rows = [dict(r) for r in raw_rows]

        users_list = []
        if can_see_all(role):
            users_list = db.execute("SELECT id, username FROM users ORDER BY username").fetchall()

        # Ostatnie z błędami (z username)
        # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
        recent_errors = db.execute(f"""
            SELECT c.*, u.username
            FROM comparisons c JOIN users u ON c.user_id=u.id
            {base_where} AND c.status IN ('blad','error','critical')
            ORDER BY c.created_at DESC LIMIT 15
        """, base_params).fetchall()  # nosec B608
    finally:
        db.close()

    # Hourly
    hourly_map = _dd(int)
    for r in raw_rows:
        ca = (r.get("created_at") or "")
        try:
            h = int(ca[11:13]) if len(ca) >= 13 else 0
        except (ValueError, IndexError):
            h = 0
        hourly_map[h] += 1
    hourly = [{"hour": h, "cnt": c} for h, c in sorted(hourly_map.items())]

    # Daily
    daily_map = _dd(lambda: {"cnt": 0, "errors": 0})
    for r in raw_rows:
        day = (r.get("created_at") or "")[:10]
        daily_map[day]["cnt"] += 1
        if r.get("status") in ("blad", "error", "critical"):
            daily_map[day]["errors"] += 1
    daily = [{"day": d, "cnt": v["cnt"], "errors": v["errors"]}
             for d, v in sorted(daily_map.items())]

    # By type
    type_map = _dd(int)
    for r in raw_rows:
        dt = r.get("doc_type") or ""
        if dt:
            type_map[dt] += 1
    by_type = [{"doc_type": k, "cnt": v} for k, v in sorted(type_map.items(), key=lambda x: -x[1])]

    # By status
    status_map = _dd(int)
    for r in raw_rows:
        status_map[r.get("status") or "ok"] += 1
    by_status = [{"status": k, "cnt": v} for k, v in status_map.items()]

    # Top dostawcy z błędami
    sup_map = _dd(int)
    for r in raw_rows:
        if r.get("status") in ("blad", "error", "critical") and r.get("supplier_code"):
            sup_map[r["supplier_code"]] += 1
    top_suppliers = [{"name": k, "error_count": v}
                     for k, v in sorted(sup_map.items(), key=lambda x: -x[1])[:8]]

    # Approval stats
    approval_map = _dd(int)
    for r in raw_rows:
        approval_map[r.get("approval_status") or "pending"] += 1
    approval_stats = dict(approval_map)

    pending_approval = approval_map.get("pending", 0)
    total = len(raw_rows)

    # Scope the follow-up aggregates to the SAME user as the main query, so a
    # regular user never sees other users' supplier/monthly/doc-type stats.
    if not can_see_all(role):
        _scope_sql, _scope_params = " AND user_id = ?", [uid]
    elif user_filter != "all":
        try:
            _scope_sql, _scope_params = " AND user_id = ?", [int(user_filter)]
        except (ValueError, TypeError):
            _scope_sql, _scope_params = "", []
    else:
        _scope_sql, _scope_params = "", []

    # Supplier discrepancies (full DB query — not limited to period window)
    supplier_discrepancies = []
    try:
        _db2 = get_db()
        try:
            # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
            supplier_discrepancies = [dict(r) for r in _db2.execute(f"""
                SELECT supplier_code,
                       COUNT(*) as total,
                       SUM(CASE WHEN status IN ('error','critical','blad') THEN 1 ELSE 0 END) as errors,
                       AVG(diff_count) as avg_diffs,
                       MAX(created_at) as last_comparison
                FROM comparisons
                WHERE supplier_code IS NOT NULL AND supplier_code != ''
                AND (is_deleted IS NULL OR is_deleted=0){_scope_sql}
                GROUP BY supplier_code
                ORDER BY errors DESC, total DESC
                LIMIT 15
            """, _scope_params).fetchall()]  # nosec B608
        finally:
            _db2.close()
    except Exception:
        pass

    # Monthly trend (last 12 months)
    monthly_trend = []
    try:
        _db3 = get_db()
        try:
            # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
            monthly_trend = [dict(r) for r in _db3.execute(f"""
                SELECT substr(created_at,1,7) as month,
                       COUNT(*) as total,
                       SUM(CASE WHEN status IN ('error','critical','blad') THEN 1 ELSE 0 END) as errors,
                       SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) as ok_count
                FROM comparisons
                WHERE (is_deleted IS NULL OR is_deleted=0)
                AND created_at IS NOT NULL{_scope_sql}
                GROUP BY month ORDER BY month DESC LIMIT 12
            """, _scope_params).fetchall()]  # nosec B608
            monthly_trend = list(reversed(monthly_trend))
        finally:
            _db3.close()
    except Exception:
        pass

    # Doc type breakdown
    doc_type_breakdown = []
    try:
        _db4 = get_db()
        try:
            # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
            doc_type_breakdown = [dict(r) for r in _db4.execute(f"""
                SELECT doc_type, COUNT(*) as total,
                       SUM(CASE WHEN status IN ('error','critical','blad') THEN 1 ELSE 0 END) as errors
                FROM comparisons WHERE (is_deleted IS NULL OR is_deleted=0){_scope_sql}
                GROUP BY doc_type ORDER BY total DESC LIMIT 10
            """, _scope_params).fetchall()]  # nosec B608
        finally:
            _db4.close()
    except Exception:
        pass

    result = {
        "hourly": hourly,
        "daily": daily,
        "by_type": by_type,
        "by_status": by_status,
        "users": [dict(r) for r in users_list],
        "top_suppliers": top_suppliers,
        "approval_stats": approval_stats,
        "pending_approval": pending_approval,
        "total": total,
        "recent_errors": [dict(r) for r in recent_errors],
        "supplier_discrepancies": supplier_discrepancies,
        "monthly_trend": monthly_trend,
        "doc_type_breakdown": doc_type_breakdown,
    }
    _kpi_cache_set(cache_key, result)
    return jsonify(result)


@app.route("/api/users", methods=["POST"])
@require_role("admin")
@csrf_protect
def add_user():
    data = request.get_json(silent=True)
    err = _validate_json_body(data, {"username": (str, True), "password": (str, True)})
    if err:
        return jsonify({"error": err}), 400
    uname = data["username"].strip()
    if len(uname) < 3 or len(uname) > 80:
        return jsonify({"error": "Login musi mieć od 3 do 80 znaków"}), 400
    pwd = data["password"]
    if len(pwd) < 8 or len(pwd) > 1000:
        return jsonify({"error": "Hasło musi mieć od 8 do 1000 znaków"}), 400
    if not any(c.isdigit() for c in pwd):
        return jsonify({"error": "Hasło musi zawierać co najmniej jedną cyfrę"}), 400
    new_role = data.get("role", "user")
    if new_role not in VALID_ROLES:
        return jsonify({"error": f"Nieznana rola '{new_role}'"}), 400
    # Role zewnętrzne muszą być powiązane ze swoją organizacją (izolacja danych):
    #   forwarder → firma spedycyjna; customs_agent → agencja celna.
    forwarder_id = None
    customs_agency_id = None
    if new_role == "forwarder":
        try:
            forwarder_id = int(data.get("forwarder_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "Spedytor wymaga przypisania firmy spedycyjnej"}), 400
    elif new_role == "customs_agent":
        try:
            customs_agency_id = int(data.get("customs_agency_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "Agent celny wymaga przypisania agencji celnej"}), 400
    email = str(data.get("email") or "").strip().lower() or None
    if email and len(email) > 254:
        return jsonify({"error": "Adres email jest za długi (max 254 znaki)"}), 400
    db = get_db()
    try:
        db.execute(
            "INSERT INTO users(username, email, password_hash, role, forwarder_id, customs_agency_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uname, email, generate_password_hash(data["password"]), new_role,
             forwarder_id, customs_agency_id)
        )
        db.commit()
    except IntegrityError:
        return jsonify({"error": f"Użytkownik lub email już istnieje"}), 400
    finally:
        db.close()
    _log_audit("user_created", session.get("username"), f"username={uname} role={new_role}")
    return jsonify({"ok": True})


@app.route("/api/users/<int:uid>/reset-link", methods=["POST"])
@require_role("admin")
@csrf_protect
def generate_reset_link(uid):
    db = get_db()
    try:
        user = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not user:
            return jsonify({"error": "Użytkownik nie istnieje"}), 404
        token = secrets.token_urlsafe(32)
        db.execute(
            "INSERT INTO password_reset_tokens(user_id,token,expires_at) "
            "VALUES(?,?,datetime('now','+24 hours'))",
            (uid, token)
        )
        db.commit()
    finally:
        db.close()
    _log_audit("reset_link_generated", user["username"], "Przez admina")
    base_url = os.environ.get("APP_BASE_URL", request.host_url.rstrip("/"))
    reset_url = f"{base_url}/reset-password?token={token}"
    email_sent = False
    if user["email"]:
        email_sent = send_reset_email(user["email"], user["username"], reset_url)
    return jsonify({"ok": True, "email_sent": email_sent, "has_email": bool(user["email"]),
                    "reset_url": reset_url if not email_sent else None})


@app.route("/api/users/<int:uid>", methods=["DELETE"])
@require_role("admin")
@csrf_protect
def delete_user(uid):
    if uid == session["user_id"]:
        return jsonify({"error": "Nie możesz usunąć swojego konta"}), 400
    db = get_db()
    try:
        target = db.execute("SELECT username, role FROM users WHERE id=?", (uid,)).fetchone()
        if not target:
            return jsonify({"error": "Użytkownik nie istnieje"}), 404
        if target["role"] == "admin":
            _others = db.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND id<>? "
                "AND COALESCE(is_active,1)=1", (uid,)
            ).fetchone()[0]
            if _others == 0:
                return jsonify({"error": "Nie można usunąć ostatniego administratora"}), 400
        db.execute("DELETE FROM users WHERE id = ?", (uid,))
        db.commit()
    finally:
        db.close()
    _log_audit("user_deleted", session.get("username"), f"deleted_uid={uid} deleted_username={target['username']}")
    return jsonify({"ok": True})


@app.route("/api/users/<int:uid>/role", methods=["PATCH"])
@require_role("admin")
@csrf_protect
def change_user_role(uid):
    data = request.get_json(silent=True) or {}
    new_role = data.get("role", "user")
    if new_role not in VALID_ROLES:
        return jsonify({"error": f"Nieznana rola"}), 400
    if uid == session["user_id"]:
        return jsonify({"error": "Nie możesz zmienić własnej roli"}), 400
    db = get_db()
    try:
        target = db.execute("SELECT username, role FROM users WHERE id=?", (uid,)).fetchone()
        if not target:
            return jsonify({"error": "Użytkownik nie istnieje"}), 404
        old_role = target["role"]
        if old_role == "admin" and new_role != "admin":
            _others = db.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND id<>? "
                "AND COALESCE(is_active,1)=1", (uid,)
            ).fetchone()[0]
            if _others == 0:
                return jsonify({"error": "Nie można odebrać roli ostatniemu administratorowi"}), 400
        db.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, uid))
        db.commit()
    finally:
        db.close()
    _log_audit("role_changed", session.get("username"), f"uid={uid} username={target['username']} {old_role}->{new_role}")
    return jsonify({"ok": True})


@app.route("/api/users/<int:uid>", methods=["PATCH"])
@require_role("admin")
@csrf_protect
def update_user(uid):
    data = request.get_json(silent=True) or {}
    new_email = (str(data.get("email") or "").strip().lower() or None)
    if new_email and len(new_email) > 254:
        return jsonify({"error": "Adres email jest za długi (max 254 znaków)"}), 400
    new_username = str(data.get("username") or "").strip()[:80] or None
    if not new_email and not new_username:
        return jsonify({"error": "Podaj email lub login do zaktualizowania"}), 400
    if new_username and len(new_username) < 3:
        return jsonify({"error": "Login musi mieć minimum 3 znaki"}), 400
    db = get_db()
    try:
        user = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not user:
            return jsonify({"error": "Użytkownik nie istnieje"}), 404
        if new_email:
            db.execute("UPDATE users SET email=? WHERE id=?", (new_email, uid))
        if new_username:
            if uid == session["user_id"] and user["username"] == "admin":
                return jsonify({"error": "Nie można zmieniać loginu konta admin"}), 400
            db.execute("UPDATE users SET username=? WHERE id=?", (new_username, uid))
        db.commit()
    except IntegrityError:
        return jsonify({"error": "Email lub login jest już zajęty"}), 400
    finally:
        db.close()
    _log_audit("user_updated", user["username"], f"email={new_email}, username={new_username}")
    return jsonify({"ok": True})


@app.route("/api/users/<int:uid>/modules", methods=["PATCH"])
@require_role("admin")
@csrf_protect
def update_user_modules(uid):
    data = request.get_json(silent=True) or {}
    modules = str(data.get("allowed_modules", "") or "")[:500]
    dept = str(data.get("department", "") or "")[:100] if data.get("department") is not None else None
    db = get_db()
    try:
        user = db.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        if not user:
            return jsonify({"error": "Użytkownik nie istnieje"}), 404
        fields, vals = [], []
        fields.append("allowed_modules=?"); vals.append(modules)
        if dept is not None:
            fields.append("department=?"); vals.append(dept)
        vals.append(uid)
        # bandit: nazwy kolumn z białej listy w kodzie (nie z kluczy żądania); wartości przez ?
        db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", vals)  # nosec B608
        db.commit()
    finally:
        db.close()
    _log_audit("user_modules_updated", user["username"], f"modules={modules}, dept={dept}")
    return jsonify({"ok": True})


@app.route("/api/admin/role-modules", methods=["GET"])
@require_role("admin")
def api_role_modules_get():
    """Return saved role→module defaults from settings."""
    db = get_db()
    try:
        row = db.execute("SELECT value FROM settings WHERE category='admin' AND key='role_module_defaults'").fetchone()
    finally:
        db.close()
    if row and row[0]:
        try:
            return jsonify({"ok": True, "config": json.loads(row[0])})
        except Exception:
            pass
    return jsonify({"ok": True, "config": {}})


@app.route("/api/admin/role-modules", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_role_modules_save():
    """Save role→module defaults to settings."""
    data = request.get_json(silent=True) or {}
    config = data.get("config")
    if not isinstance(config, dict):
        return jsonify({"error": "Nieprawidłowy format config"}), 400
    _VALID_MODULES = {"zakupy", "transport", "spedycja", "magazyn", "artwork", "dane_ref", "admin"}
    _VALID_ROLE_KEYS = {"user", "manager", "superuser", "admin", "forwarder", "customs_agent"}
    sanitized = {}
    for role, modules in config.items():
        if role not in _VALID_ROLE_KEYS:
            continue
        if not isinstance(modules, list):
            continue
        sanitized[role] = [m for m in modules if m in _VALID_MODULES]
    db = get_db()
    try:
        db.execute(
            "INSERT INTO settings(key, value, category) VALUES(?, ?, 'admin') "
            "ON CONFLICT(category,key) DO UPDATE SET value=excluded.value",
            ("role_module_defaults", json.dumps(sanitized))
        )
        db.commit()
    finally:
        db.close()
    _log_audit("role_modules_saved", session.get("username"), f"config={sanitized}")
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# PROFILE PAGE
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/profile")
@login_required
def profile_page():
    uid = session["user_id"]
    db = get_db()
    try:
        user = db.execute(
            "SELECT id, username, email, role, department, phone, language, "
            "allowed_modules, created_at, last_login FROM users WHERE id=?", (uid,)
        ).fetchone()
        activity = db.execute(
            "SELECT event, detail, ip, created_at FROM audit_log "
            "WHERE username=? ORDER BY created_at DESC LIMIT 50",
            (session["username"],)
        ).fetchall()
        comp_count = db.execute(
            "SELECT COUNT(*) FROM comparisons WHERE user_id=?", (uid,)
        ).fetchone()[0]
    finally:
        db.close()
    return render_template("profile.html",
                           user=dict(user) if user else {},
                           activity=[dict(r) for r in activity],
                           comp_count=comp_count,
                           username=session["username"],
                           role=session["role"])


@app.route("/api/profile/update", methods=["POST"])
@login_required
@csrf_protect
def api_profile_update():
    uid = session["user_id"]
    data = request.get_json(silent=True) or {}
    _profile_max_lens = {"email": 254, "phone": 40, "department": 100}
    allowed = ["email", "phone", "department"]
    fields, vals = [], []
    for k in allowed:
        if k in data:
            v = str(data[k] or "").strip()
            max_len = _profile_max_lens.get(k, 200)
            if len(v) > max_len:
                return jsonify({"error": f"Pole '{k}' jest za długie (max {max_len})"}), 400
            fields.append(f"{k}=?")
            vals.append(v or None)
    if not fields:
        return jsonify({"error": "Brak pól"}), 400
    vals.append(uid)
    db = get_db()
    try:
        # bandit: nazwy kolumn z białej listy w kodzie (nie z kluczy żądania); wartości przez ?
        db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", vals)  # nosec B608
        db.commit()
    except IntegrityError:
        return jsonify({"error": "Email jest już zajęty"}), 400
    finally:
        db.close()
    _log_audit("profile_updated", session["username"], f"fields={','.join(f.split('=')[0] for f in fields)}")
    return jsonify({"ok": True})


@app.route("/api/profile/change-password", methods=["POST"])
@login_required
@csrf_protect
def api_profile_change_password():
    uid = session["user_id"]
    data = request.get_json(silent=True) or {}
    current = data.get("current_password", "")
    new_pwd = data.get("new_password", "")
    if not current or not new_pwd:
        return jsonify({"error": "Podaj obecne i nowe hasło"}), 400
    if len(current) > 1000 or len(new_pwd) > 1000:
        return jsonify({"error": "Hasło jest za długie"}), 400
    if len(new_pwd) < 8:
        return jsonify({"error": "Nowe hasło musi mieć co najmniej 8 znaków"}), 400
    if not any(c.isdigit() for c in new_pwd):
        return jsonify({"error": "Nowe hasło musi zawierać cyfrę"}), 400
    db = get_db()
    try:
        user = db.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], current):
            return jsonify({"error": "Obecne hasło jest nieprawidłowe"}), 400
        db.execute("UPDATE users SET password_hash=? WHERE id=?",
                   (generate_password_hash(new_pwd), uid))
        db.commit()
    finally:
        db.close()
    _log_audit("password_changed", session["username"], "Własna zmiana hasła")
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# ACTIVITY LOG PAGE
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/activity")
@login_required
def activity_page():
    if session.get("role", "user") not in ("manager", "superuser", "admin"):
        return redirect(url_for("home_page"))
    db = get_db()
    try:
        users_list = db.execute(
            "SELECT DISTINCT username FROM audit_log ORDER BY username"
        ).fetchall()
        event_types = db.execute(
            "SELECT DISTINCT event FROM audit_log ORDER BY event"
        ).fetchall()
        # Stats
        total_today = db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE date(created_at)=date('now')"
        ).fetchone()[0]
        total_week = db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE created_at >= ?", (_ts_ago(days=7),)
        ).fetchone()[0]
        active_users = db.execute(
            "SELECT COUNT(DISTINCT username) FROM audit_log WHERE created_at >= ?",
            (_ts_ago(days=7),)
        ).fetchone()[0]
        top_events = db.execute(
            "SELECT event, COUNT(*) as cnt FROM audit_log "
            "GROUP BY event ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        recent = db.execute(
            "SELECT event, username, detail, ip, created_at FROM audit_log "
            "ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
    finally:
        db.close()
    return render_template("activity.html",
                           users_list=[r["username"] for r in users_list],
                           event_types=[r["event"] for r in event_types],
                           total_today=total_today,
                           total_week=total_week,
                           active_users=active_users,
                           top_events=[dict(r) for r in top_events],
                           recent=[dict(r) for r in recent],
                           username=session["username"],
                           role=session["role"])



# ─────────────────────────────────────────────────────────────────────────────
# ROUTE — UNIFIED ANALYZE (wszystkie 3 konteksty naraz)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/analyze")
@login_required
def analyze_page():
    return render_template("analyze.html",
                           username=session["username"],
                           role=session["role"],
                           can_see_all=can_see_all(session["role"]))


# ─────────────────────────────────────────────────────────────────────────────
# NOTIFICATIONS SYSTEM
# ─────────────────────────────────────────────────────────────────────────────

def _create_notification(user_id: int, ntype: str, title: str, message: str = "", link: str = "") -> None:
    """Create one in-app notification for a user (silent, never raises)."""
    try:
        db = get_db()
        try:
            db.execute(
                "INSERT INTO notifications(user_id, type, title, message, link) VALUES(?,?,?,?,?)",
                (user_id, ntype, title, message, link)
            )
            db.commit()
        finally:
            db.close()
    except Exception as _e:
        logger.debug("_create_notification failed (non-fatal): %s", _e)


def _notify_role(role: str, ntype: str, title: str, message: str = "", link: str = "") -> None:
    """Broadcast a notification to all users with the given role or above."""
    try:
        db = get_db()
        try:
            roles = [r for r, lvl in ROLE_LEVEL.items() if lvl >= ROLE_LEVEL.get(role, 0)]
            users = db.execute(
                # bandit: lista placeholderów ? budowana z len(); wartości parametryzowane
                f"SELECT id FROM users WHERE role IN ({','.join('?'*len(roles))}) AND (is_active IS NULL OR is_active=1)",  # nosec B608
                roles
            ).fetchall()
            if users:
                db.executemany(
                    "INSERT INTO notifications(user_id, type, title, message, link) VALUES(?,?,?,?,?)",
                    [(u["id"], ntype, title, message, link) for u in users]
                )
            db.commit()
        finally:
            db.close()
    except Exception as _e:
        logger.debug("_notify_role failed (non-fatal): %s", _e)


def _notify_managers(title: str, message: str = "", link: str = "", email: bool = True) -> None:
    """Powiadom managerów+ in-app oraz (best-effort, w tle) e-mailem. Nigdy nie rzuca."""
    try:
        _notify_role("manager", "delivery", title, message, link)
    except Exception:
        pass
    if not email:
        return
    def _send():
        try:
            db = get_db()
            try:
                roles = [r for r, lvl in ROLE_LEVEL.items() if lvl >= ROLE_LEVEL.get("manager", 0)]
                rows = db.execute(
                    # bandit: lista placeholderów ? budowana z len(); wartości parametryzowane
                    f"SELECT email FROM users WHERE role IN ({','.join('?'*len(roles))}) "  # nosec B608
                    f"AND email IS NOT NULL AND email<>'' AND (is_active IS NULL OR is_active=1)",
                    roles,
                ).fetchall()
            finally:
                db.close()
            _base = os.environ.get("APP_BASE_URL", "").rstrip("/")
            _body = message + (f"\n\n{_base}{link}" if (link and _base) else "")
            for r in rows:
                try:
                    _send_email(r["email"], title, _body)
                except Exception:
                    pass
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()


def _auto_notifications() -> None:
    """Check DB state and generate notifications for upcoming/overdue items. Called lazily."""
    try:
        db = get_db()
        try:
            # 1. Containers arriving in ≤3 days — notify managers once per container per day
            try:
                urgent = db.execute(
                    "SELECT id, container_number, eta, po_numbers FROM transport_queue "
                    "WHERE (urgency='pilne' OR (eta!='' AND eta IS NOT NULL AND date(eta)<=date('now','+3 days'))) "
                    "AND (customs_status IS NULL OR customs_status NOT IN ('wydane','zatwierdzone'))"
                ).fetchall()
                for row in urgent:
                    _container_link = f"/transport/queue/{row['id']}"
                    existing = db.execute(
                        "SELECT id FROM notifications WHERE type='container_urgent' "
                        "AND ((link IS NULL AND ? IS NULL) OR link=?) "
                        "AND created_at >= ? LIMIT 1",
                        (_container_link, _container_link, _ts_ago(days=1))
                    ).fetchone()
                    if not existing:
                        _notify_role("manager", "container_urgent",
                                     f"🚢 Kontener {row['container_number']} — ETA {row['eta']}",
                                     f"PO: {row['po_numbers'] or '—'}",
                                     _container_link)
            except Exception:
                pass

            # 2. SAD comparisons pending > 24h — notify admins
            try:
                pending_sad = db.execute(
                    "SELECT COUNT(*) FROM comparisons WHERE doc_type='SAD' "
                    "AND (approval_status IS NULL OR approval_status='pending') "
                    "AND created_at <= ? "
                    "AND (is_deleted IS NULL OR is_deleted=0)", (_ts_ago(days=1),)
                ).fetchone()[0]
                if pending_sad > 0:
                    existing = db.execute(
                        "SELECT id FROM notifications WHERE type='sad_pending' "
                        "AND created_at >= ? LIMIT 1", (_ts_ago(hours=4),)
                    ).fetchone()
                    if not existing:
                        _notify_role("manager", "sad_pending",
                                     f"🛃 {pending_sad} SAD czeka na zatwierdzenie",
                                     "Zgłoszenia celne oczekują na weryfikację od ponad 24 godzin.",
                                     "/sad")
            except Exception:
                pass

            # 3. Driver SMS not confirmed after 24h
            try:
                unconfirmed = db.execute(
                    "SELECT dn.id, d.name, dn.pickup_location FROM driver_notifications dn "
                    "LEFT JOIN drivers d ON dn.driver_id=d.id "
                    "WHERE dn.status='sent' AND dn.sms_sent_at <= ?", (_ts_ago(days=1),)
                ).fetchall()
                for row in unconfirmed:
                    # Marker [#id] z ogranicznikami — '%5%' łapało też 15/50/itd.
                    existing = db.execute(
                        "SELECT id FROM notifications WHERE type='driver_pending' "
                        "AND link='/transport/dispatch' AND message LIKE ? "
                        "AND created_at >= ? LIMIT 1",
                        (f"%[#{row['id']}]%", _ts_ago(hours=12))
                    ).fetchone()
                    if not existing:
                        _notify_role("manager", "driver_pending",
                                     f"🚛 Kierowca {row['name'] or '—'} nie potwierdził",
                                     f"Lokalizacja: {row['pickup_location'] or '—'} · ID powiadomienia: [#{row['id']}]",
                                     "/transport/dispatch")
            except Exception:
                pass

            # 4. Warehouse receipts in 'przybyle' for > 48h
            try:
                stale = db.execute(
                    "SELECT COUNT(*) FROM warehouse_receipts WHERE status='przybyle' "
                    "AND created_at <= ?", (_ts_ago(days=2),)
                ).fetchone()[0]
                if stale > 0:
                    existing = db.execute(
                        "SELECT id FROM notifications WHERE type='warehouse_pending' "
                        "AND created_at >= ? LIMIT 1", (_ts_ago(hours=6),)
                    ).fetchone()
                    if not existing:
                        _notify_role("user", "warehouse_pending",
                                     f"🏭 {stale} przyjęć w magazynie nie sprawdzono",
                                     "Kontenery w statusie 'Przybyłe' od ponad 48 godzin.",
                                     "/warehouse")
            except Exception:
                pass
        finally:
            db.close()
    except Exception:
        pass


@app.route("/api/notifications")
@login_required
def api_notifications():
    uid = session["user_id"]
    _auto_notifications()
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, type, title, message, link, is_read, created_at "
            "FROM notifications WHERE user_id=? "
            "ORDER BY is_read ASC, created_at DESC LIMIT 100",
            (uid,)
        ).fetchall()
        unread = db.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0", (uid,)
        ).fetchone()[0]
        return jsonify({
            "notifications": [dict(r) for r in rows],
            "unread": unread
        })
    finally:
        db.close()


@app.route("/api/notifications/<int:nid>/read", methods=["POST"])
@login_required
@csrf_protect
def api_notification_read(nid):
    uid = session["user_id"]
    db = get_db()
    try:
        db.execute("UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?", (nid, uid))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/notifications/read-all", methods=["POST"])
@login_required
@csrf_protect
def api_notifications_read_all():
    uid = session["user_id"]
    db = get_db()
    try:
        db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (uid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/notifications/<int:nid>", methods=["DELETE"])
@login_required
@csrf_protect
def api_notification_delete(nid):
    uid = session["user_id"]
    db = get_db()
    try:
        db.execute("DELETE FROM notifications WHERE id=? AND user_id=?", (nid, uid))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# SHIPMENTS — tracking from PO to SAP MIGO
# ─────────────────────────────────────────────────────────────────────────────

def _make_shipment_ref(po_number: str, date_str: str = None) -> str:
    """Format shipment reference: DD-MM-YYYY/PO_NUMBER."""
    from datetime import date as _d
    if not date_str:
        today = _d.today()
        date_str = today.strftime("%d-%m-%Y")
    return f"{date_str}/{po_number}"


def _ensure_shipment_folder(po_number: str) -> str:
    """Create and return the local folder path for all documents of a given PO."""
    if not po_number:
        return ""
    safe_po = re.sub(r'[^\w\-]', '_', po_number)[:80]  # cap length, strip path separators
    folder = os.path.realpath(os.path.join(_SHIPMENT_DOCS_PATH, safe_po))
    if not folder.startswith(os.path.realpath(_SHIPMENT_DOCS_PATH) + os.sep):
        return ""  # path traversal guard
    os.makedirs(folder, exist_ok=True)
    return folder


def _save_doc_to_shipment(po_number: str, src_path: str, original_name: str,
                          doc_type: str, user_id: int, source: str = "auto") -> bool:
    """Copy a PDF to the shipment folder and record it in shipment_documents.

    Returns True on success, False on any error.
    """
    try:
        folder = _ensure_shipment_folder(po_number)
        if not folder or not os.path.exists(src_path):
            return False
        safe_orig = re.sub(r'[^\w\-.]', '_', original_name)[:200]
        # Prefix disk filename with doc_type so same-named files from different
        # document types don't silently overwrite each other.
        safe_type = re.sub(r'[^\w]', '', str(doc_type or "OTHER").upper())[:20] or "OTHER"
        safe_name = f"{safe_type}__{safe_orig}"
        dest = os.path.join(folder, safe_name)
        if not os.path.exists(dest):
            shutil.copy2(src_path, dest)
        file_size = os.path.getsize(dest)
        db = get_db()
        try:
            row = db.execute(
                "SELECT shipment_ref FROM shipments WHERE po_number=?", (po_number,)
            ).fetchone()
            shipment_ref = row["shipment_ref"] if row else ""
            # Avoid duplicate records for same file+type
            exists = db.execute(
                "SELECT id FROM shipment_documents WHERE po_number=? AND filename=?",
                (po_number, safe_name)
            ).fetchone()
            if not exists:
                db.execute(
                    "INSERT INTO shipment_documents"
                    "(po_number, shipment_ref, filename, original_name, doc_type,"
                    " file_size, source, uploaded_by) VALUES(?,?,?,?,?,?,?,?)",
                    (po_number, shipment_ref, safe_name, original_name[:255],
                     doc_type, file_size, source, user_id)
                )
                db.commit()
        finally:
            db.close()
        return True
    except Exception as _e:
        logger.warning("_save_doc_to_shipment failed for PO %s: %s", po_number, _e)
        return False


def _extract_po_data_from_pdf(pdf_path: str, filename: str = None) -> dict:
    """Extract structured data from a Purchase Order PDF.

    Returns dict with: sap_number, supplier_name, supplier_code, po_date,
    po_author, delivery_date_expected, line_items (list of dicts), currency.
    All fields are best-effort; missing fields returned as empty string/list.
    """
    result = {
        "sap_number": "", "delivery_number": "", "supplier_name": "", "supplier_code": "",
        "po_date": "", "po_author": "", "delivery_date_expected": "",
        "currency": "EUR", "line_items": [],
    }
    try:
        import pdfplumber as _pp
        text = ""
        tables = []
        with _pp.open(pdf_path) as pdf:
            for page in pdf.pages[:4]:
                t = page.extract_text() or ""
                text += t + "\n"
                for tbl in (page.extract_tables() or []):
                    if tbl:
                        tables.append(tbl)

        # ── SAP number ────────────────────────────────────────────
        sap_m = re.search(r'\b([45]\d{8,10})\b', text)
        if sap_m:
            result["sap_number"] = sap_m.group(1)

        # ── Numer dostawy (SAP) — prefiks "45", min. 10 cyfr ──────
        # Najpierw z nazwy pliku (np. "4500000204 Inspection Report.pdf"),
        # potem z treści dokumentu, jeśli w nazwie nie ma.
        from sap_extract import extract_delivery_number
        name_src = filename or os.path.basename(pdf_path)
        dn = extract_delivery_number(name_src, text)   # nazwa pliku ma pierwszeństwo
        if dn:
            result["delivery_number"] = dn
            # Pole 'nr' formularza dostaw konsumuje sap_number — uzupełnij je,
            # gdy z treści dokumentu nic nie wyłuskano.
            if not result["sap_number"]:
                result["sap_number"] = dn

        # ── Supplier name ─────────────────────────────────────────
        supplier_patterns = [
            r"(?:Vendor|Supplier|Sold[\s-]?[Tt]o|Dostawca|From|To)[:\s]+([A-Z][A-Za-z0-9\s,&.\-]{3,60})",
            r"(?:Company|Firma)[:\s]+([A-Z][A-Za-z0-9\s,&.\-]{3,60})",
        ]
        for pat in supplier_patterns:
            m = re.search(pat, text)
            if m:
                candidate = m.group(1).strip().rstrip(",").strip()
                if len(candidate) >= 3:
                    result["supplier_name"] = candidate[:80]
                    break

        # ── Supplier code (short code in parentheses or after colon) ──
        code_m = re.search(r'(?:Vendor|Supplier)\s+(?:Code|No\.?|Number|ID)[:\s]+([A-Z0-9]{2,15})', text, re.I)
        if code_m:
            result["supplier_code"] = code_m.group(1).strip()

        # ── PO date ───────────────────────────────────────────────
        date_patterns = [
            r"(?:Date|Datum|Data)[:\s]+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
            r"(?:Order\s+Date|Data\s+zam[oó]wienia)[:\s]+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
            r"(\d{2}\.\d{2}\.\d{4})",
        ]
        for pat in date_patterns:
            m = re.search(pat, text, re.I)
            if m:
                result["po_date"] = m.group(1).strip()
                break

        # ── Delivery date ─────────────────────────────────────────
        del_patterns = [
            r"(?:Delivery\s+Date|Data\s+dostawy|Ship\s+[Bb]y|Required\s+Date)[:\s]+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
            r"(?:ETD|ETA)[:\s]+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
        ]
        for pat in del_patterns:
            m = re.search(pat, text, re.I)
            if m:
                result["delivery_date_expected"] = m.group(1).strip()
                break

        # ── Author / created by ───────────────────────────────────
        author_patterns = [
            r"(?:Created\s+[Bb]y|Prepared\s+[Bb]y|Wystawił|Osoba\s+kontaktowa|Contact)[:\s]+([A-Za-z][A-Za-z\s]{2,40})",
            r"(?:Buyer|Nabywca)[:\s]+([A-Za-z][A-Za-z\s]{2,40})",
        ]
        for pat in author_patterns:
            m = re.search(pat, text, re.I)
            if m:
                candidate = m.group(1).strip()
                if 3 <= len(candidate) <= 60:
                    result["po_author"] = candidate
                    break

        # ── Currency ──────────────────────────────────────────────
        cur_m = re.search(r'\b(EUR|USD|GBP|PLN|CNY|CHF)\b', text)
        if cur_m:
            result["currency"] = cur_m.group(1)

        # ── Line items from tables ────────────────────────────────
        items = []
        for tbl in tables:
            if not tbl or len(tbl) < 2:
                continue
            header = [str(c or "").lower().strip() for c in tbl[0]]
            # detect columns by keyword
            def _col(kws):
                for kw in kws:
                    for i, h in enumerate(header):
                        if kw in h:
                            return i
                return -1
            ci_ref   = _col(["ref", "article", "material", "item", "nr", "kod", "pozycja"])
            ci_desc  = _col(["desc", "name", "opis", "bezeichnung", "product"])
            ci_qty   = _col(["qty", "quant", "ilo", "menge"])
            ci_unit  = _col(["unit", "jedn", "uom"])
            ci_price = _col(["price", "cena", "preis", "unit price"])
            ci_val   = _col(["value", "amount", "total", "warto", "betrag"])
            if ci_ref < 0 and ci_qty < 0:
                continue
            for pos, row in enumerate(tbl[1:], 1):
                if not row:
                    continue
                def _cell(i):
                    if i < 0 or i >= len(row):
                        return ""
                    return str(row[i] or "").strip()
                ref_val = _cell(ci_ref)
                if not ref_val or ref_val.lower() in ("", "nan", "-"):
                    continue
                qty_raw = _cell(ci_qty).replace(",", ".").replace(" ", "")
                price_raw = _cell(ci_price).replace(",", ".").replace(" ", "")
                val_raw  = _cell(ci_val).replace(",", ".").replace(" ", "")
                try:
                    qty = float(qty_raw) if qty_raw else 0.0
                except ValueError:
                    qty = 0.0
                try:
                    price = float(re.sub(r"[^\d.]", "", price_raw)) if price_raw else 0.0
                except ValueError:
                    price = 0.0
                try:
                    val = float(re.sub(r"[^\d.]", "", val_raw)) if val_raw else round(qty * price, 4)
                except ValueError:
                    val = round(qty * price, 4)
                items.append({
                    "pozycja": pos,
                    "ref_code": ref_val[:50],
                    "opis": _cell(ci_desc)[:120],
                    "ilosc": qty,
                    "jednostka": _cell(ci_unit)[:10],
                    "cena_jednostkowa": price,
                    "wartosc": val,
                    "waluta": result["currency"],
                })
                if len(items) >= 200:
                    break
            if items:
                break
        result["line_items"] = items
    except Exception as _e:
        logger.debug("_extract_po_data_from_pdf error: %s", _e)
    return result


def _try_extract_supplier_from_pdf(po_number: str, pdf_path: str) -> None:
    """Best-effort: extract supplier name from a PO PDF and save if not already set."""
    try:
        db = get_db()
        try:
            row = db.execute(
                "SELECT supplier_name FROM kolejka_zlecenia WHERE nr_zamowienia=?", (po_number,)
            ).fetchone()
            if not row or (row["supplier_name"] or "").strip():
                return  # already has supplier
        finally:
            db.close()

        import pdfplumber
        text = ""
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages[:2]:
                    t = page.extract_text() or ""
                    text += t + "\n"
                    if len(text) > 3000:
                        break
        except Exception:
            return

        # Look for "Vendor" / "Supplier" / "Sold to" / "Dostawca" patterns
        import re as _re
        supplier = ""
        patterns = [
            r"(?:Vendor|Supplier|Sold[\s-]?[Tt]o|Dostawca|From)[:\s]+([A-Z][A-Za-z0-9\s,&.\-]{3,60})",
            r"(?:Company|Firma)[:\s]+([A-Z][A-Za-z0-9\s,&.\-]{3,60})",
        ]
        for pat in patterns:
            m = _re.search(pat, text)
            if m:
                candidate = m.group(1).strip().rstrip(",").strip()
                if len(candidate) >= 3:
                    supplier = candidate[:80]
                    break

        if supplier:
            db2 = get_db()
            try:
                db2.execute(
                    "UPDATE kolejka_zlecenia SET supplier_name=?, updated_at=datetime('now') "
                    "WHERE nr_zamowienia=? AND (supplier_name IS NULL OR supplier_name='')",
                    (supplier, po_number)
                )
                db2.commit()
            finally:
                db2.close()
    except Exception:
        pass


def _try_enrich_po_from_pdf(po_number: str, pdf_path: str) -> None:
    """Extract and persist full PO data (supplier, dates, author, line items) from PDF."""
    try:
        data = _extract_po_data_from_pdf(pdf_path)
        db = get_db()
        try:
            row = db.execute(
                "SELECT supplier_name, supplier_code, po_date, po_author, "
                "po_delivery_date_expected FROM kolejka_zlecenia "
                "WHERE nr_zamowienia=?", (po_number,)
            ).fetchone()
            if not row:
                return
            updates = {}
            if data.get("supplier_name") and not (row["supplier_name"] or "").strip():
                updates["supplier_name"] = str(data["supplier_name"])[:200]
            if data.get("supplier_code") and not (row["supplier_code"] or "").strip():
                updates["supplier_code"] = str(data["supplier_code"])[:50]
            if data.get("po_date") and not (row["po_date"] or "").strip():
                updates["po_date"] = str(data["po_date"])[:20]
            if data.get("po_author") and not (row["po_author"] or "").strip():
                updates["po_author"] = str(data["po_author"])[:200]
            if data.get("delivery_date_expected") and not (row["po_delivery_date_expected"] or "").strip():
                updates["po_delivery_date_expected"] = str(data["delivery_date_expected"])[:20]
            if updates:
                set_clause = ", ".join(f"{k}=?" for k in updates)
                db.execute(
                    # bandit: nazwy kolumn z białej listy w kodzie (nie z kluczy żądania); wartości przez ?
                    f"UPDATE kolejka_zlecenia SET {set_clause}, updated_at=datetime('now') "  # nosec B608
                    f"WHERE nr_zamowienia=?",
                    list(updates.values()) + [po_number]
                )
            # Save line items
            if data.get("line_items"):
                db.execute("DELETE FROM po_line_items WHERE po_number=?", (po_number,))
                for item in data["line_items"]:
                    _rc, _op = _clean_po_ref(db, item["ref_code"], item.get("opis", ""))
                    item["ref_code"], item["opis"] = _rc, _op
                    db.execute(
                        "INSERT INTO po_line_items (po_number, pozycja, ref_code, opis, ilosc, "
                        "jednostka, cena_jednostkowa, wartosc, waluta) VALUES (?,?,?,?,?,?,?,?,?)",
                        (po_number, item["pozycja"], item["ref_code"], item["opis"],
                         item["ilosc"], item["jednostka"], item["cena_jednostkowa"],
                         item["wartosc"], item["waluta"])
                    )
            db.commit()
        finally:
            db.close()
    except Exception as _e:
        logger.debug("_try_enrich_po_from_pdf error: %s", _e)


def _perform_auto_compare_pair(po_number: str, type_a: str, type_b: str,
                                result_doc_type: str, user_id: int) -> None:
    """Rdzeń auto-porównania dwóch dokumentów dostawy (bez request/session/g —
    bezpieczne w wątku-daemonie albo w workerze RQ). Re-derywuje ścieżki z bazy
    zamiast ufać wartościom przechwyconym przed enqueue (mogły się zdezaktualizować).
    Wynik zapisywany pod result_doc_type, który musi mapować się w COMP_TYPE_TO_SLOTS."""
    try:
        folder = _ensure_shipment_folder(po_number)
        if not folder:
            return
        db = get_db()
        try:
            a = db.execute(
                "SELECT filename FROM shipment_documents WHERE po_number=? AND doc_type=? "
                "ORDER BY uploaded_at DESC LIMIT 1", (po_number, type_a)).fetchone()
            b = db.execute(
                "SELECT filename FROM shipment_documents WHERE po_number=? AND doc_type=? "
                "ORDER BY uploaded_at DESC LIMIT 1", (po_number, type_b)).fetchone()
        finally:
            db.close()
        if not a or not b:
            return
        pa = os.path.join(folder, a["filename"])
        pb = os.path.join(folder, b["filename"])
        if not os.path.exists(pa) or not os.path.exists(pb):
            return

        from enhanced_comparator import compare_enhanced
        result = compare_enhanced(pa, pb, type_a, type_b).to_dict()
        import json as _json
        _db = get_db()
        try:
            _db.execute(
                "INSERT INTO comparisons (user_id, doc_type, file_a, file_b, result_json, "
                "status, diff_count, po_number, created_at) VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
                (user_id, result_doc_type, a["filename"], b["filename"],
                 _json.dumps(result), result.get("risk_level", "ok"),
                 result.get("diff_count", 0), po_number))
            _db.commit()
            logger.info("Auto-comparison %s for %s: %s diffs",
                        result_doc_type, po_number, result.get("diff_count", 0))
        finally:
            _db.close()
    except Exception as _e:
        logger.warning("Auto-compare %s failed for %s: %s", result_doc_type, po_number, _e)


def _try_auto_compare_pair(po_number: str, type_a: str, type_b: str,
                           result_doc_type: str, user_id: int) -> None:
    """Trigger: sprawdza czy oba dokumenty (type_a i type_b) już są wgrane, po czym
    wrzuca ciężką pracę do jobs.enqueue (worker RQ gdy REDIS_URL, wątek-daemon w
    przeciwnym razie — Faza 2 KOLEJKOWANIE.md). _perform_auto_compare_pair jest
    funkcją modułową (serializowalną przez RQ) i sama zapisuje wynik do bazy."""
    try:
        db = get_db()
        try:
            has_a = db.execute(
                "SELECT 1 FROM shipment_documents WHERE po_number=? AND doc_type=? LIMIT 1",
                (po_number, type_a)).fetchone()
            has_b = db.execute(
                "SELECT 1 FROM shipment_documents WHERE po_number=? AND doc_type=? LIMIT 1",
                (po_number, type_b)).fetchone()
        finally:
            db.close()
        if not has_a or not has_b:
            return
        import jobs as _jobs
        _jobs.enqueue(_perform_auto_compare_pair, po_number, type_a, type_b, result_doc_type, user_id)
    except Exception as _e:
        logger.debug("_try_auto_compare_pair error: %s", _e)


@app.route("/api/dostawy/extract-po", methods=["POST"])
@login_required
@csrf_protect
def api_extract_po_preview():
    """Parse a PO PDF and return extracted fields for form pre-fill (no DB write)."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Brak pliku"}), 400
    err = _validate_pdf_upload(f)
    if err:
        return jsonify({"error": err}), 400
    f.seek(0)
    tmp = os.path.join(app.config["UPLOAD_FOLDER"],
                       f"po_preview_{session['user_id']}_{secure_filename(f.filename) or 'preview.pdf'}")
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    f.save(tmp)
    try:
        data = _extract_po_data_from_pdf(tmp, filename=f.filename)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return jsonify(data)


def _validate_file_po(filename: str, file_bytes: bytes, po_number: str) -> dict:
    """Validate that a PDF file belongs to the given PO number.

    Checks:
    1. Filename contains the PO number (normalized, case-insensitive).
    2. PDF text content contains the PO number.
    """
    po_norm = re.sub(r'[\s\-_/]', '', po_number).lower()

    # 1. Filename check
    name_base = filename.rsplit('.', 1)[0] if '.' in filename else filename
    name_norm = re.sub(r'[\s\-_/]', '', name_base).lower()
    filename_ok = po_norm in name_norm

    # 2. Content check — extract text from PDF
    content_ok = False
    found_in_content = []
    try:
        import pdfplumber as _pp
        with _pp.open(io.BytesIO(file_bytes)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages[:5])
        text_norm = re.sub(r'[\s\-_/]', '', text).lower()
        content_ok = po_norm in text_norm
        for match in re.finditer(re.escape(po_number[:4]), text, re.IGNORECASE):
            start = max(0, match.start() - 10)
            end = min(len(text), match.end() + 20)
            snippet = text[start:end].replace('\n', ' ').strip()
            if po_norm in re.sub(r'[\s\-_/]', '', snippet).lower():
                found_in_content.append(snippet)
                if len(found_in_content) >= 3:
                    break
    except Exception:
        pass

    ok = filename_ok and content_ok
    if ok:
        warning = ""
    elif filename_ok and not content_ok:
        warning = f"Nazwa pliku ✅ — numer {po_number} NIE znaleziono w treści dokumentu ❌"
    elif not filename_ok and content_ok:
        warning = f"Treść dokumentu ✅ — numer {po_number} NIE znaleziono w nazwie pliku ❌"
    else:
        warning = f"Numer {po_number} NIE znaleziono ani w nazwie pliku ani w treści ❌"

    return {
        "ok": ok,
        "filename_ok": filename_ok,
        "content_ok": content_ok,
        "found_in_content": found_in_content,
        "warning": warning,
        "filename": filename,
        "po_number": po_number,
    }


def _ensure_shipment(po_number: str, supplier_name: str = "", comparison_id: int = None,
                     created_by: int = None) -> str:
    """Create or update a shipment record for the given PO number. Returns shipment_ref."""
    if not po_number:
        return ""
    try:
        from datetime import date as _d
        ref = _make_shipment_ref(po_number)
        db = get_db()
        try:
            existing = db.execute(
                "SELECT id FROM shipments WHERE po_number=?", (po_number,)
            ).fetchone()
            if existing:
                # Update comparison_id if not yet set
                if comparison_id:
                    db.execute(
                        "UPDATE shipments SET comparison_id=?, supplier_name=COALESCE(NULLIF(supplier_name,''),?), "
                        "updated_at=datetime('now') WHERE po_number=? AND comparison_id IS NULL",
                        (comparison_id, supplier_name, po_number)
                    )
                    db.commit()
            else:
                today = _d.today().strftime("%Y-%m-%d")
                db.execute(
                    "INSERT INTO shipments(shipment_ref, po_number, shipment_date, supplier_name, "
                    "comparison_id, created_by) VALUES(?,?,?,?,?,?)",
                    (ref, po_number, today, supplier_name, comparison_id, created_by)
                )
                db.commit()
        finally:
            db.close()
        _ensure_shipment_folder(po_number)
        return ref
    except Exception:
        return ""


@app.route("/shipments")
@login_required
def shipments_page():
    # Consolidated into the canonical Polish "Dostawy" system (/dostawy), which
    # tracks the same inbound POs with the full workflow. The /shipments page was a
    # redundant parallel UI; the shipments table/_ensure_shipment plumbing stays in
    # place for the comparison→PO→document linkage. Old bookmarks land on /dostawy.
    return redirect("/dostawy")


@app.route("/api/shipments", methods=["POST"])
@login_required
@csrf_protect
def api_shipment_create():
    data = request.get_json(silent=True) or {}
    po = str(data.get("po_number") or "").strip()[:100]
    if not po:
        return jsonify({"error": "Numer PO jest wymagany"}), 400
    from datetime import date as _d
    date_str = data.get("date") or _d.today().strftime("%d-%m-%Y")
    ref = _make_shipment_ref(po, date_str)
    db = get_db()
    try:
        existing = db.execute("SELECT id, shipment_ref FROM shipments WHERE po_number=?", (po,)).fetchone()
        if existing:
            return jsonify({"ok": True, "shipment_ref": existing["shipment_ref"], "existed": True})
        db_date = str(data.get("date") or "").strip() or _d.today().strftime("%Y-%m-%d")
        db.execute(
            "INSERT INTO shipments(shipment_ref, po_number, shipment_date, supplier_name, "
            "notes, created_by) VALUES(?,?,?,?,?,?)",
            (ref, po, db_date, str(data.get("supplier_name") or "")[:200],
             str(data.get("notes") or "")[:500], session["user_id"])
        )
        db.commit()
    except Exception:
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 400
    finally:
        db.close()
    _log_audit("shipment_create", session["username"], f"ref={ref}")
    _ensure_shipment_folder(po)
    return jsonify({"ok": True, "shipment_ref": ref})


@app.route("/api/shipments/<path:ref>", methods=["PATCH"])
@require_role("manager")
@csrf_protect
def api_shipment_update(ref):
    data = request.get_json(silent=True) or {}
    _VALID_SHIP_STATUSES = {"nowe", "potwierdzono", "w_transporcie", "odprawa",
                            "w_magazynie", "zakonczone"}
    allowed = ["status", "supplier_name", "notes",
               "transport_queue_id", "warehouse_receipt_id"]
    _ship_max_lens = {"supplier_name": 200, "notes": 500}
    fields, vals = [], []
    _int_fields = {"transport_queue_id", "warehouse_receipt_id"}
    for k in allowed:
        if k in data:
            v = data[k]
            if k == "status":
                v = str(v or "").strip()
                if v not in _VALID_SHIP_STATUSES:
                    continue
            elif k in _int_fields:
                try:
                    v = int(v) if v not in (None, "", False) else None
                except (TypeError, ValueError):
                    v = None
            elif k in _ship_max_lens:
                v = str(v or "")[:_ship_max_lens[k]]
            fields.append(f"{k}=?")
            vals.append(v)
    if not fields:
        return jsonify({"error": "Brak pól"}), 400
    fields.append("updated_at=datetime('now')")
    vals.append(ref)
    db = get_db()
    try:
        # bandit: nazwy kolumn z białej listy w kodzie (nie z kluczy żądania); wartości przez ?
        db.execute(f"UPDATE shipments SET {', '.join(fields)} WHERE shipment_ref=?", vals)  # nosec B608
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


# ── Shipment Documents ────────────────────────────────────────────────────────

def _get_po_for_ref(ref: str):
    """Return po_number for a shipment_ref, or None if not found."""
    db = get_db()
    try:
        ship = db.execute(
            "SELECT po_number FROM shipments WHERE shipment_ref=?", (ref,)
        ).fetchone()
        return ship["po_number"] if ship else None
    finally:
        db.close()


@app.route("/api/shipments/<path:ref>/documents", methods=["GET"])
@require_role("manager")
def api_shipment_docs_list(ref):
    """List all documents attached to a shipment."""
    po = _get_po_for_ref(ref)
    if po is None:
        return jsonify({"error": "Dostawa nie znaleziona"}), 404
    db = get_db()
    try:
        docs = db.execute(
            "SELECT id, filename, original_name, doc_type, file_size, source, uploaded_at "
            "FROM shipment_documents WHERE po_number=? ORDER BY uploaded_at DESC",
            (po,)
        ).fetchall()
    finally:
        db.close()
    folder = _ensure_shipment_folder(po)
    result = []
    for d in docs:
        path = os.path.join(folder, d["filename"])
        result.append({
            "id": d["id"],
            "filename": d["filename"],
            "original_name": d["original_name"],
            "doc_type": d["doc_type"],
            "file_size": d["file_size"],
            "source": d["source"],
            "uploaded_at": d["uploaded_at"],
            "exists": os.path.isfile(path),
        })
    return jsonify({"documents": result, "po_number": po})


@app.route("/api/shipments/<path:ref>/documents", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_shipment_docs_upload(ref):
    """Manually upload a document to a shipment folder."""
    po = _get_po_for_ref(ref)
    if po is None:
        return jsonify({"error": "Dostawa nie znaleziona"}), 404

    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Brak pliku"}), 400
    err = _validate_pdf_upload(f)
    if err:
        return jsonify({"error": err}), 400

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    import uuid as _uuid_sdoc
    tmp_path = os.path.join(app.config["UPLOAD_FOLDER"],
                            f"sdoc_{session['user_id']}_{_uuid_sdoc.uuid4().hex[:8]}_{secure_filename(f.filename) or 'document.pdf'}")
    f.seek(0)
    f.save(tmp_path)
    doc_type = re.sub(r'[^\w]', '', (request.form.get("doc_type") or "").strip().upper())[:20]
    _VALID_SHIP_DOC_TYPES = {
        "PO", "PI", "CI", "PL", "SAD", "BL", "CMR", "COO", "COA", "INV",
        "PACK", "AWB", "WZ", "DHL", "CUSTOMS", "OTHER", "DOC"
    }
    if doc_type not in _VALID_SHIP_DOC_TYPES:
        doc_type = "OTHER"
    ok = _save_doc_to_shipment(po, tmp_path, f.filename, doc_type,
                               session["user_id"], source="manual")
    try:
        os.remove(tmp_path)
    except OSError:
        pass
    if not ok:
        return jsonify({"error": "Nie udało się zapisać pliku"}), 500
    _log_audit("shipment_doc_upload", session["username"], f"po={po} file={f.filename}")
    return jsonify({"ok": True})


@app.route("/api/shipments/<path:ref>/documents/<int:doc_id>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_shipment_docs_delete(ref, doc_id):
    """Remove a document from shipment folder and DB record."""
    po = _get_po_for_ref(ref)
    if po is None:
        return jsonify({"error": "Dostawa nie znaleziona"}), 404
    db = get_db()
    try:
        doc = db.execute(
            "SELECT filename FROM shipment_documents WHERE id=? AND po_number=?", (doc_id, po)
        ).fetchone()
        if not doc:
            return jsonify({"error": "Dokument nie znaleziony"}), 404
        folder = _ensure_shipment_folder(po)
        file_path = os.path.join(folder, doc["filename"])
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except OSError:
            pass
        db.execute("DELETE FROM shipment_documents WHERE id=? AND po_number=?", (doc_id, po))
        db.commit()
        _log_audit("shipment_doc_delete", session["username"], f"po={po} doc_id={doc_id}")
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/shipments/<path:ref>/documents/zip", methods=["GET"])
@require_role("manager")
def api_shipment_docs_zip(ref):
    """Download all documents for a shipment as a ZIP archive."""
    po = _get_po_for_ref(ref)
    if po is None:
        return jsonify({"error": "Dostawa nie znaleziona"}), 404
    _ZIP_MAX_FILES = 200
    _ZIP_MAX_BYTES = 500 * 1024 * 1024  # 500 MB total
    db = get_db()
    try:
        doc_rows = db.execute(
            "SELECT filename, file_size FROM shipment_documents WHERE po_number=?", (po,)
        ).fetchall()
    finally:
        db.close()

    total_size = sum(r["file_size"] or 0 for r in doc_rows)
    if len(doc_rows) > _ZIP_MAX_FILES:
        return jsonify({"error": f"Zbyt wiele plików ({len(doc_rows)}). Limit: {_ZIP_MAX_FILES}."}), 400
    if total_size > _ZIP_MAX_BYTES:
        return jsonify({"error": f"Łączny rozmiar za duży ({total_size // 1024 // 1024} MB). Limit: 500 MB."}), 400

    folder = _ensure_shipment_folder(po)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for row in doc_rows:
            fpath = os.path.join(folder, row["filename"])
            if os.path.isfile(fpath) and not os.path.islink(fpath):
                zf.write(fpath, os.path.basename(row["filename"]))
    buf.seek(0)
    safe_po = po.replace("/", "_").replace("\\", "_")
    return send_file(buf, mimetype="application/zip",
                     as_attachment=True,
                     download_name=f"dokumenty_{safe_po}.zip")


@app.route("/api/shipments/<path:ref>/documents/<filename>", methods=["GET"])
@login_required  # spójnie z /api/dostawy/.../documents — odczyt dokumentów dozwolony
                 # dla każdego zalogowanego usera (forwarderzy nadal odcięci przez
                 # _restrict_external_forwarders); ścieżka i przynależność pliku
                 # zweryfikowane niżej.
def api_shipment_docs_download(ref, filename):
    """Download a single document from a shipment folder."""
    po = _get_po_for_ref(ref)
    if po is None:
        return jsonify({"error": "Dostawa nie znaleziona"}), 404
    # Verify the filename actually belongs to this shipment (no cross-shipment access)
    safe_fn = re.sub(r'[^\w\-.]', '_', filename)
    db = get_db()
    try:
        doc = db.execute(
            "SELECT id FROM shipment_documents WHERE po_number=? AND filename=?",
            (po, safe_fn)
        ).fetchone()
        if not doc:
            return jsonify({"error": "Plik nie znaleziony"}), 404
    finally:
        db.close()

    folder = _ensure_shipment_folder(po)
    fpath = os.path.join(folder, safe_fn)
    if not os.path.isfile(fpath) or os.path.islink(fpath):
        return jsonify({"error": "Plik nie znaleziony"}), 404
    _log_audit("shipment_doc_download", session["username"], f"po={po} file={safe_fn}")
    return send_file(fpath, as_attachment=True, download_name=safe_fn)


@app.route("/api/shipments/validate-file", methods=["POST"])
@login_required
@csrf_protect
def api_validate_shipment_file():
    """Validate that an uploaded PDF matches a given PO number (filename + content)."""
    po = (request.form.get("po_number") or "").strip()
    if not po:
        return jsonify({"error": "Podaj numer PO"}), 400
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Brak pliku"}), 400
    result = _validate_file_po(f.filename, f.read(), po)
    return jsonify(result)


@app.route("/api/analyze", methods=["POST"])
@login_required
@csrf_protect
def api_analyze():
    """
    Unified endpoint: przyjmuje 2 PDF, uruchamia wszystkie 3 silniki,
    zwraca jeden skonsolidowany raport.
    """
    file_a = request.files.get("file_a")
    file_b = request.files.get("file_b")
    use_ai = request.form.get("use_ai", "0") == "1"
    # Przyjmuj zarówno type_a jak i legacy doc_type_a
    type_a = request.form.get("type_a") or request.form.get("doc_type_a", "auto")
    type_b = request.form.get("type_b") or request.form.get("doc_type_b", "auto")

    uid = session["user_id"]
    if _check_and_record_api_rate(uid):
        return jsonify({"error": "Przekroczono limit zapytań. Poczekaj chwilę."}), 429

    for f in (file_a, file_b):
        err = _validate_pdf_upload(f)
        if err:
            return jsonify({"error": err}), 400

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    import uuid as _uuid
    _uniq = _uuid.uuid4().hex[:8]
    fa_name = secure_filename(file_a.filename) or "doc_a.pdf"
    fb_name = secure_filename(file_b.filename) or "doc_b.pdf"
    path_a = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_u_a_{_uniq}_{fa_name}")
    path_b = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_u_b_{_uniq}_{fb_name}")
    file_a.save(path_a)
    file_b.save(path_b)

    report = {
        "file_a": file_a.filename,
        "file_b": file_b.filename,
        "doc_type_a": type_a,
        "doc_type_b": type_b,
        "modules": {},
        "risk_level": "ok",
        "total_errors": 0,
        "total_warnings": 0,
        "summary": "",
        "ai_validation": None,
    }

    # ── ENHANCED: uruchom ulepszony silnik jako główny ────────────────────────
    text_a = ""
    text_b = ""
    try:
        from enhanced_comparator import compare_enhanced
        from supplier_profiles import detect_supplier
        supplier_profile = None
        try:
            from pdf_extractor import extract_text
            ta = extract_text(path_a)
            tb = extract_text(path_b)
            text_a = ta.get('text', '')
            text_b = tb.get('text', '')
            supplier_profile = detect_supplier(text_a + ' ' + text_b)
        except Exception:
            pass

        enh = compare_enhanced(path_a, path_b, type_a, type_b, supplier_profile)
        ed = enh.to_dict()

        # Moduł "enhanced" zastępuje/uzupełnia tabele i literówki
        report["modules"]["enhanced"] = {
            "ok": True,
            "doc_type_a": ed["doc_type_a"],
            "doc_type_b": ed["doc_type_b"],
            "diff_count": ed["diff_count"],
            "warn_count": ed["warn_count"],
            "format_count": ed["format_count"],
            "ok_count": ed["ok_count"],
            "risk_level": ed["risk_level"],
            "summary": ed["summary"],
            "items": ed["items"],
            "headers": ed["headers"],
            "findings": ed["findings"],
            "checksum_a": ed.get("checksum_a"),
            "checksum_b": ed.get("checksum_b"),
            "extraction_method_a": ed.get("extraction_method_a", ""),
            "extraction_method_b": ed.get("extraction_method_b", ""),
            "is_scan_a": ed.get("is_scan_a", False),
            "is_scan_b": ed.get("is_scan_b", False),
            "warnings": ed.get("warnings", []),
            "supplier_code": supplier_profile.get("code") if supplier_profile else None,
            "supplier_name": supplier_profile.get("name") if supplier_profile else None,
        }
        report["total_errors"]   += ed["diff_count"]
        report["total_warnings"] += ed["warn_count"]
        if supplier_profile:
            report["supplier_detected"] = supplier_profile.get("code", "")
            report["supplier_name"]     = supplier_profile.get("name", "")
            report["supplier_unknown"]  = False
        else:
            # Dostawca nie wykryty — spróbuj wyciągnąć nazwę z tekstu PDF
            report["supplier_detected"] = ""
            report["supplier_name"]     = ""
            report["supplier_unknown"]  = True
            # Szukaj nazwy dostawcy w tekście (pierwsze linii)
            supplier_hint = ""
            for line in (text_a + "\n" + text_b).split("\n")[:20]:
                line = line.strip()
                # Szukaj linii z "Co.,Ltd", "Ltd.", "GmbH", "S.A.", "Sp.", "Inc."
                if any(kw in line for kw in ["Co.,Ltd","Co., Ltd","Ltd.","GmbH","S.A.","Inc.","Sp. z o.o."]):
                    if "ACME" not in line and len(line) > 5 and len(line) < 80:
                        supplier_hint = line[:60]
                        break
            if not supplier_hint:
                # Fallback: pierwsza niepusta linia tekstu B (dokument dostawcy)
                for line in text_b.split("\n")[:5]:
                    line = line.strip()
                    if len(line) > 8 and not line.startswith("http") and "ACME" not in line:
                        supplier_hint = line[:60]
                        break
            report["supplier_hint"] = supplier_hint
            # Zaloguj użycie profilu — skip when no supplier detected
            pass
    except Exception as e:
        report["modules"]["enhanced"] = {"ok": False, "error": str(e)[:200]}

    # ── 1. Regex (pola tekstowe, faktury PL) ─────────────────────────────────
    try:
        # Użyj tekstu już wyekstrahowanego przez enhanced (lub fallback)
        if not text_a or not text_b:
            text_a = extract_pdf_text(path_a)
            text_b = extract_pdf_text(path_b)
        regex_doc_type = "auto"
        if type_a in ("FV",) or type_b in ("FV",):
            regex_doc_type = "faktura_vat"
        elif type_a in ("WZ",) or type_b in ("WZ",):
            regex_doc_type = "wz"
        result_regex = compare_documents(text_a, text_b, regex_doc_type)
        rd = result_regex.to_dict()
        report["modules"]["regex"] = {
            "ok": True,
            "doc_type": rd.get("doc_type_detected", ""),
            "diff_count": rd.get("diff_count", 0),
            "warn_count": rd.get("warn_count", 0),
            "risk_level": rd.get("risk_level", "ok"),
            "summary": rd.get("summary", ""),
            "fields": rd.get("fields", []),
            "line_items": rd.get("line_items", []),
            "user_type_a": type_a,
            "user_type_b": type_b,
        }
        report["total_errors"]   += rd.get("diff_count", 0)
        report["total_warnings"] += rd.get("warn_count", 0)
    except Exception as e:
        report["modules"]["regex"] = {"ok": False, "error": str(e)[:200]}

    # ── 2. Tabelaryczny legacy (PO/PI/CI/PL/SAD) ─────────────────────────────
    try:
        from table_extractor import compare_tables
        result_tbl = compare_tables(path_a, path_b)
        td = result_tbl.to_dict()
        report["modules"]["table"] = {
            "ok": True,
            "doc_type_a": type_a if type_a != "auto" else td.get("doc_type_a", ""),
            "doc_type_b": type_b if type_b != "auto" else td.get("doc_type_b", ""),
            "diff_count": td.get("diff_count", 0),
            "warn_count": td.get("warn_count", 0),
            "format_count": td.get("format_count", 0),
            "ok_count": td.get("ok_count", 0),
            "risk_level": td.get("risk_level", "ok"),
            "summary": td.get("summary", ""),
            "headers": td.get("headers", []),
            "items": td.get("items", []),
            "total_a": td.get("total_a"),
            "total_b": td.get("total_b"),
            "total_qty_a": td.get("total_qty_a"),
            "total_qty_b": td.get("total_qty_b"),
        }
        report["total_errors"]   += td.get("diff_count", 0)
        report["total_warnings"] += td.get("warn_count", 0)
    except Exception as e:
        report["modules"]["table"] = {"ok": False, "error": str(e)[:200]}

    # ── 3. Literówki / I↔1 ───────────────────────────────────────────────────
    try:
        from typo_detector import analyze_full_texts
        result_typo = analyze_full_texts(text_a, text_b, file_a.filename, file_b.filename)
        yd = result_typo.to_dict()
        # Uzupełnij o I↔1 znaleziska z enhanced (unikaj duplikatów)
        enh_findings = report["modules"].get("enhanced", {}).get("findings", [])
        enh_i1 = [f for f in enh_findings if f.get("category") == "I_vs_1"]
        existing_i1_vals = {(f.get("val_a"), f.get("val_b")) for f in enh_i1}
        all_findings = list(yd.get("findings", []))
        for f in enh_i1:
            if (f.get("val_a"), f.get("val_b")) not in {(x.get("val_a"), x.get("val_b")) for x in all_findings}:
                all_findings.append(f)
        report["modules"]["typo"] = {
            "ok": True,
            "error_count":   yd.get("error_count", 0),
            "warning_count": yd.get("warning_count", 0),
            "info_count":    yd.get("info_count", 0),
            "risk_level":    "blad" if yd.get("error_count", 0) > 0 else
                             "ostrzezenie" if yd.get("warning_count", 0) > 0 else "ok",
            "summary": yd.get("summary", ""),
            "findings": all_findings,
        }
        report["total_errors"]   += yd.get("error_count", 0)
        report["total_warnings"] += yd.get("warning_count", 0)
    except Exception as e:
        report["modules"]["typo"] = {"ok": False, "error": str(e)[:200]}

    # ── Ogólny risk level ─────────────────────────────────────────────────────
    if report["total_errors"] > 0:
        report["risk_level"] = "blad"
    elif report["total_warnings"] > 0:
        report["risk_level"] = "ostrzezenie"
    else:
        report["risk_level"] = "ok"

    report["summary"] = (
        f"Analiza ukończona. Łącznie {report['total_errors']} rozbieżności "
        f"i {report['total_warnings']} ostrzeżeń we wszystkich trybach."
    )
    # Policz łączne "ok" ze wszystkich modułów dla wskaźnika zgodności
    _ok = 0
    for _mod in report["modules"].values():
        if isinstance(_mod, dict) and _mod.get("ok"):
            _ok += _mod.get("ok_count", 0)
    report["total_ok"] = _ok

    # ── AI walidacja (opcjonalna) ─────────────────────────────────────────────
    if use_ai:
        try:
            # `generate_text_report` zostało usunięte — nigdzie nie jest wywoływane
            from ai_validator import validate_comparison_result
            # AI dostaje wyniki najlepszego silnika (tabelaryczny ma priorytetu)
            best_result = (td if "table" in report["modules"] and report["modules"]["table"]["ok"]
                          else rd if "regex" in report["modules"] and report["modules"]["regex"]["ok"]
                          else {})
            best_result = dict(best_result)   # kopia — nie mutuj wyniku modułu zapisanego w report["modules"]
            best_result["summary"] = report["summary"]
            ai_out = validate_comparison_result(best_result, mode="auto")
            report["ai_validation"] = ai_out.get("ai_validation", {})
        except Exception as e:
            report["ai_validation"] = {"error": str(e)[:200], "report_pl": "AI niedostępne."}

    # ── Zapisz do historii ────────────────────────────────────────────────────
    cid = _save_comparison(
        uid,
        f"Unified: {report['modules'].get('regex',{}).get('doc_type','') or report['modules'].get('table',{}).get('doc_type_a','')}",
        file_a.filename,
        file_b.filename,
        report,
        path_a=path_a,
        path_b=path_b
    )
    report["comparison_id"] = cid

    # ── Powiadomienie email przy błędach ──────────────────────────────────────
    try:
        _notify_errors(cid, report)
    except Exception:
        pass

    # ── Dołącz ścieżki plików do podglądu ────────────────────────────────────
    report["preview_a"] = f"/preview/{uid}_u_a_{_uniq}_{fa_name}"
    report["preview_b"] = f"/preview/{uid}_u_b_{_uniq}_{fb_name}"

    return jsonify(report)


@app.route("/preview/<path:filename>")
@login_required
def preview_file(filename):
    """Serwuje pliki PDF (oryginalne i adnotowane) do podglądu."""
    from flask import send_from_directory, abort
    uid = session["user_id"]
    # Bezpieczenstwo: user moze ogladac tylko swoje pliki
    safe = secure_filename(filename)
    if not safe.startswith(f"{uid}_"):
        if not can_see_all(session["role"]):
            abort(403)
    upload_dir = os.path.abspath(app.config["UPLOAD_FOLDER"])
    return send_from_directory(upload_dir, safe, mimetype="application/pdf")


@app.route("/api/annotate", methods=["POST"])
@login_required
@csrf_protect
def api_annotate():
    """
    Generuje adnotowane obrazy PNG stron PDF z zaznaczonymi błędami.
    Zwraca base64 PNG dla każdej strony obu dokumentów.
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Brak danych"}), 400

    report    = data.get("report", {})
    file_a    = data.get("file_a", "")
    file_b    = data.get("file_b", "")
    uid       = session["user_id"]
    upload_dir = app.config["UPLOAD_FOLDER"]

    def find_file(fname):
        sname = secure_filename(fname)
        for prefix in [f"{uid}_u_a_", f"{uid}_u_b_",
                       f"{uid}_a_", f"{uid}_b_",
                       f"{uid}_ta_", f"{uid}_tb_"]:
            p = os.path.join(upload_dir, prefix + sname)
            if os.path.exists(p):
                return p
        return None

    path_a = find_file(file_a)
    path_b = find_file(file_b)

    if not path_a:
        return jsonify({"error": f"Plik A '{file_a}' niedostępny — uruchom analizę ponownie"}), 404
    if not path_b:
        return jsonify({"error": f"Plik B '{file_b}' niedostępny — uruchom analizę ponownie"}), 404

    try:
        from pdf_annotator import annotate_pdf, results_to_annotations, LEGEND

        anns_a = results_to_annotations(report, side="a")
        anns_b = results_to_annotations(report, side="b")

        pages_a = annotate_pdf(path_a, anns_a, resolution=130, max_pages=4)
        pages_b = annotate_pdf(path_b, anns_b, resolution=130, max_pages=4)

        return jsonify({
            "ok": True,
            "doc_a": pages_a,
            "doc_b": pages_b,
            "legend": LEGEND,
            "ann_count_a": sum(p["found"] for p in pages_a),
            "ann_count_b": sum(p["found"] for p in pages_b),
        })
    except Exception as e:
        import traceback as _tb
        app.logger.error("Annotation error: %s", _tb.format_exc())
        return jsonify({"error": "Błąd adnotacji — spróbuj ponownie."}), 500

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — TYPY DOKUMENTÓW
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/doc-types")
@login_required
def doc_types_page():
    if not is_admin(session["role"]):
        return redirect(url_for("analyze_page"))
    db = get_db()
    try:
        types = db.execute("SELECT * FROM doc_types ORDER BY sort_order, id").fetchall()
    finally:
        db.close()
    return render_template("doc_types.html",
                           doc_types=[dict(t) for t in types],
                           username=session["username"],
                           role=session["role"])


@app.route("/api/doc-types")
@login_required
def api_doc_types_list():
    """Zwraca aktywne typy dokumentów — używane przez analyze.html."""
    db = get_db()
    try:
        types = db.execute(
            "SELECT code, label, icon, description FROM doc_types WHERE active=1 ORDER BY sort_order"
        ).fetchall()
    finally:
        db.close()
    return jsonify([dict(t) for t in types])


@app.route("/api/doc-types/all")
@login_required
def api_doc_types_all():
    """Wszystkie typy (łącznie z nieaktywnymi) — dla panelu admin."""
    db = get_db()
    try:
        types = db.execute("SELECT * FROM doc_types ORDER BY sort_order").fetchall()
    finally:
        db.close()
    return jsonify([dict(t) for t in types])


@app.route("/api/doc-types", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_doc_types_add():
    """Dodaje nowy typ dokumentu."""
    data = request.get_json(silent=True)
    if not data or not data.get("code") or not data.get("label"):
        return jsonify({"error": "Wymagane: code, label"}), 400
    code = str(data["code"]).upper().strip()
    if not code or len(code) > 12:
        return jsonify({"error": "Kod dokumentu może mieć maksymalnie 12 znaków"}), 400
    db = get_db()
    try:
        # Pobierz max sort_order
        max_order = db.execute("SELECT COALESCE(MAX(sort_order),0) FROM doc_types").fetchone()[0]
        try:
            db.execute(
                "INSERT INTO doc_types(code, label, icon, description, active, sort_order) VALUES(?,?,?,?,?,?)",
                (code, str(data["label"])[:100], str(data.get("icon") or "📄")[:10], str(data.get("description") or "")[:500], 1, max_order+1)
            )
            db.commit()
            row = db.execute("SELECT * FROM doc_types WHERE code=?", (code,)).fetchone()
            return jsonify({"ok": True, "type": dict(row)})
        except Exception as e:
            logger.exception("doc-types error: %s", e)
            return jsonify({"error": "Błąd zapisu — spróbuj ponownie."}), 400
    finally:
        db.close()


@app.route("/api/doc-types/<int:tid>", methods=["PATCH"])
@require_role("admin")
@csrf_protect
def api_doc_types_update(tid):
    """Aktualizuje typ dokumentu (label, icon, description, active, sort_order)."""
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        row = db.execute("SELECT * FROM doc_types WHERE id=?", (tid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404

        allowed = ["label", "icon", "description", "active", "sort_order"]
        _dt_max_lens = {"label": 100, "icon": 10, "description": 500}
        updates, params = [], []
        for field in allowed:
            if field in data:
                v = data[field]
                if field == "active":
                    v = 1 if v else 0
                elif field == "sort_order":
                    try:
                        v = int(v)
                    except (TypeError, ValueError):
                        continue
                elif field in _dt_max_lens:
                    v = str(v or "")[:_dt_max_lens[field]]
                updates.append(f"{field}=?")
                params.append(v)
        if not updates:
            return jsonify({"error": "Brak pól do aktualizacji"}), 400

        params.append(tid)
        # bandit: nazwy kolumn z białej listy w kodzie (nie z kluczy żądania); wartości przez ?
        db.execute(f"UPDATE doc_types SET {', '.join(updates)} WHERE id=?", params)  # nosec B608
        db.commit()
        updated = db.execute("SELECT * FROM doc_types WHERE id=?", (tid,)).fetchone()
        return jsonify({"ok": True, "type": dict(updated)})
    finally:
        db.close()


@app.route("/api/doc-types/<int:tid>", methods=["DELETE"])
@require_role("admin")
@csrf_protect
def api_doc_types_delete(tid):
    """Usuwa typ dokumentu (tylko jeśli nie jest wbudowany)."""
    db = get_db()
    try:
        row = db.execute("SELECT code FROM doc_types WHERE id=?", (tid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        # Chroń typy wbudowane
        builtin = {"auto","PO","PI","CI","PL","SAD","BL","WZ","FV","MULTI","CMR","SWIFT"}
        if row["code"] in builtin:
            return jsonify({"error": "Nie można usunąć wbudowanego typu — wyłącz go zamiast usuwać"}), 400
        db.execute("DELETE FROM doc_types WHERE id=?", (tid,))
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/doc-types/reorder", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_doc_types_reorder():
    """Ustawia kolejność — przyjmuje listę {id, sort_order}."""
    data = request.get_json(silent=True) or {}
    items = data.get("items", [])
    db = get_db()
    try:
        for item in items:
            sid = item.get("id")
            sorder = item.get("sort_order")
            if sid is None or sorder is None:
                continue
            try:
                _sid = int(sid)
                _sorder = int(sorder)
            except (TypeError, ValueError):
                continue
            db.execute("UPDATE doc_types SET sort_order=? WHERE id=?",
                       (_sorder, _sid))
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — EKSPORT (PDF / EXCEL)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/export/pdf/<int:cid>")
@login_required
def export_pdf_route(cid):
    uid = session["user_id"]
    role = session["role"]
    db = get_db()
    try:
        row = db.execute("SELECT * FROM comparisons WHERE id=?", (cid,)).fetchone()
    finally:
        db.close()
    if not row:
        return jsonify({"error": "Nie znaleziono"}), 404
    if not can_see_all(role) and row["user_id"] != uid:
        return jsonify({"error": "Brak dostępu"}), 403
    try:
        from export_engine import export_pdf
        report = json.loads(row["result_json"] or "{}")
        pdf_bytes = export_pdf(report, cid)
        from flask import Response
        fname = ("raport_" + str(cid) + "_" + secure_filename(row['file_a'] or '')[:20] + ".pdf").replace(" ", "_")
        return Response(pdf_bytes, mimetype="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename={fname}"})
    except Exception as e:
        logger.exception("PDF export error cid=%s", cid)
        return jsonify({"error": "Błąd generowania PDF"}), 500


@app.route("/api/export/excel/<int:cid>")
@login_required
def export_excel_route(cid):
    uid = session["user_id"]
    role = session["role"]
    db = get_db()
    try:
        row = db.execute("SELECT * FROM comparisons WHERE id=?", (cid,)).fetchone()
    finally:
        db.close()
    if not row:
        return jsonify({"error": "Nie znaleziono"}), 404
    if not can_see_all(role) and row["user_id"] != uid:
        return jsonify({"error": "Brak dostępu"}), 403
    try:
        from export_engine import export_excel
        report = json.loads(row["result_json"] or "{}")
        xlsx_bytes = export_excel(report, cid)
        from flask import Response
        fname = ("raport_" + str(cid) + "_" + secure_filename(row['file_a'] or '')[:20] + ".xlsx").replace(" ", "_")
        return Response(xlsx_bytes,
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename={fname}"})
    except Exception as e:
        logger.exception("Excel export error cid=%s", cid)
        return jsonify({"error": "Błąd generowania Excel"}), 500


@app.route("/api/export/current", methods=["POST"])
@login_required
@csrf_protect
def export_current():
    """Eksportuje aktualny raport (z analyze.html) bez zapisywania do historii."""
    import traceback
    data = request.get_json(force=True, silent=True) or {}
    if not data:
        return jsonify({"error": "Brak danych — upewnij się że request ma Content-Type: application/json"}), 400
    fmt    = data.get("format", "pdf")
    report = data.get("report") or {}
    cid    = data.get("comparison_id")
    if not report:
        return jsonify({"error": "Brak raportu do eksportu"}), 400
    cid = int(cid) if cid is not None and str(cid).lstrip('-').isdigit() else None
    _safe_cid = str(cid) if cid is not None else 'x'
    try:
        if fmt == "excel":
            from export_engine import export_excel
            from flask import Response
            xlsx_bytes = export_excel(report, cid)
            fname = f"raport_{_safe_cid}.xlsx"
            return Response(xlsx_bytes,
                            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": f"attachment; filename={fname}"})
        else:
            from export_engine import export_pdf
            from flask import Response
            pdf_bytes = export_pdf(report, cid)
            fname = f"raport_{_safe_cid}.pdf"
            return Response(pdf_bytes, mimetype="application/pdf",
                            headers={"Content-Disposition": f"attachment; filename={fname}"})
    except Exception as e:
        tb = traceback.format_exc()
        app.logger.error("Export error: %s", tb)
        _role = session.get("role", "")
        resp = {"error": "Błąd generowania eksportu"}
        if is_admin(_role):
            resp["detail"] = tb[-500:]
        return jsonify(resp), 500


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — BATCH PROCESSING
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/batch/pair", methods=["POST"])
@login_required
@csrf_protect
def api_batch_pair():
    """Wczytuje wiele plików i sugeruje pary."""
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "Brak plików"}), 400

    import hashlib
    _MAX_FILE_BYTES = 50 * 1024 * 1024
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    uid = session["user_id"]
    saved = []
    seen_hashes: set = set()
    for f in files:
        if not f or not f.filename:
            continue
        if not f.filename.lower().endswith(".pdf"):
            continue
        data = f.read(_MAX_FILE_BYTES + 1)
        if len(data) > _MAX_FILE_BYTES:
            continue
        # Walidacja zawartości (jak _validate_pdf_upload): magic %PDF + brak hasła —
        # samo rozszerzenie .pdf nie wystarcza (można podrzucić nie-PDF do uploads/).
        if not data.startswith(b"%PDF") or b"/Encrypt" in data[-4096:]:
            continue
        md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
        if md5 in seen_hashes:
            continue
        seen_hashes.add(md5)
        fname = secure_filename(f.filename) or "batch.pdf"
        path = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_b_a_{fname}")
        with open(path, "wb") as fh:
            fh.write(data)
        saved.append(fname)

    from batch_processor import auto_pair_files
    pairs, unpaired = auto_pair_files(saved)
    return jsonify({"pairs": pairs, "unpaired": unpaired, "total_files": len(saved)})


@app.route("/api/batch/run", methods=["POST"])
@login_required
@csrf_protect
def api_batch_run():
    """Uruchamia analizę batch dla podanych par. Zwraca wyniki jako JSON."""
    data = request.get_json(silent=True) or {}
    pairs = data.get("pairs", [])
    if not pairs:
        return jsonify({"error": "Brak par do analizy"}), 400
    if not isinstance(pairs, list):
        return jsonify({"error": "Pole 'pairs' musi być tablicą"}), 400
    if len(pairs) > 200:
        return jsonify({"error": "Zbyt wiele par — limit wynosi 200"}), 400

    uid = session["user_id"]
    upload_dir = app.config["UPLOAD_FOLDER"]
    use_ai = bool(data.get("use_ai", False))
    # Per-request global supplier_code override (can be overridden per-pair)
    global_supplier_code = str(data.get("supplier_code") or "")[:50] or None

    # Apply per-pair supplier_code from global override only when not explicitly set
    for pair in pairs:
        if "supplier_code" not in pair and global_supplier_code:
            pair["supplier_code"] = global_supplier_code

    from batch_processor import run_batch
    results = list(run_batch(pairs, upload_dir, uid, use_ai=use_ai))

    # Zapisz do historii
    db = get_db()
    try:
        for res in results:
            if res.get("report"):
                try:
                    db.execute("SAVEPOINT sp_batch")
                    sc = res.get("supplier_code") or global_supplier_code or ""
                    _rl = res.get("risk_level", "ok")
                    if _rl not in ("ok", "warning", "error", "critical", "blad", "ostrzezenie"):
                        _rl = "ok"
                    db.execute(
                        "INSERT INTO comparisons(user_id,doc_type,file_a,file_b,result_json,status,diff_count,supplier_code)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (uid,
                         f"{res.get('type_a','?')} vs {res.get('type_b','?')}"[:200],
                         str(res.get("file_a") or "")[:500],
                         str(res.get("file_b") or "")[:500],
                         json.dumps(res["report"], ensure_ascii=False),
                         _rl,
                         int(res.get("total_errors") or 0),
                         sc[:50])
                    )
                    db.execute("RELEASE SAVEPOINT sp_batch")
                except Exception:
                    try:
                        db.execute("ROLLBACK TO SAVEPOINT sp_batch")
                    except Exception:
                        pass
        db.commit()
        _kpi_cache_invalidate()
    finally:
        db.close()

    return jsonify({"results": results, "total": len(results)})


@app.route("/api/batch/export_excel", methods=["POST"])
@login_required
@csrf_protect
def api_batch_export_excel():
    """Eksportuje zbiorczy Excel z wynikami batch."""
    data = request.get_json(silent=True) or {}
    results = data.get("results", [])
    if not results:
        return jsonify({"error": "Brak wyników"}), 400
    try:
        from batch_processor import build_batch_excel
        from flask import Response
        xlsx_bytes = build_batch_excel(results)
        return Response(xlsx_bytes,
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": "attachment; filename=batch_raport.xlsx"})
    except Exception as e:
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500


# ── Checklists ──────────────────────────────────────────────────────────────

@app.route("/checklists")
@login_required
def checklists_page():
    db = get_db()
    try:
        # All delivery checklists grouped by PO
        deliveries = db.execute("""
            SELECT po_number, supplier_name, container_number,
                   COUNT(*) as total_docs,
                   SUM(CASE WHEN status='zatwierdzono' THEN 1 ELSE 0 END) as approved,
                   SUM(CASE WHEN status='brak' THEN 1 ELSE 0 END) as missing,
                   MAX(updated_at) as last_update
            FROM delivery_checklists
            GROUP BY po_number, supplier_name, container_number
            ORDER BY last_update DESC LIMIT 100
        """).fetchall()
        deliveries = [dict(r) for r in deliveries]

        # Supplier checklist templates
        templates_raw = db.execute("""
            SELECT * FROM supplier_checklists ORDER BY supplier_name, doc_type
        """).fetchall()
        templates = [dict(r) for r in templates_raw]

        # Recent comparisons for quick import
        recent_pos = db.execute("""
            SELECT DISTINCT po_number, supplier_code, created_at
            FROM comparisons
            WHERE po_number != '' AND po_number IS NOT NULL
            AND (is_deleted IS NULL OR is_deleted=0)
            ORDER BY created_at DESC LIMIT 30
        """).fetchall()
        recent_pos = [dict(r) for r in recent_pos]

        try:
            suppliers = db.execute("SELECT id, name FROM suppliers ORDER BY name").fetchall()
            suppliers = [dict(s) for s in suppliers]
        except Exception:
            suppliers = []
    finally:
        db.close()
    return render_template("checklists.html",
                           deliveries=deliveries,
                           templates=templates,
                           recent_pos=recent_pos,
                           suppliers=suppliers,
                           username=session["username"],
                           role=session["role"])


@app.route("/api/checklists/delivery", methods=["POST"])
@login_required
@csrf_protect
def api_checklist_create():
    data = request.get_json(silent=True) or {}
    po = str(data.get("po_number") or "").strip()[:100]
    if not po:
        return jsonify({"error": "Numer PO jest wymagany"}), 400
    docs = data.get("docs", [])
    if not docs:
        # Default doc types
        docs = ["PO", "PI", "CI", "PL", "SAD", "BL"]
    _VALID_DOC_TYPES_CL = {"PO", "PI", "CI", "PL", "SAD", "BL", "CMR", "COO", "COA", "INV", "PACK", "BL", "AWB"}
    uid = session["user_id"]
    db = get_db()
    try:
        for doc in docs:
            doc = str(doc).strip().upper()[:20]
            if doc not in _VALID_DOC_TYPES_CL:
                continue
            db.execute(
                "INSERT INTO delivery_checklists(po_number, supplier_name, container_number, doc_type, status, created_by) "
                "VALUES(?,?,?,?,?,?)",
                (po, str(data.get("supplier_name") or "")[:200], str(data.get("container_number") or "")[:100], doc, "brak", uid)
            )
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/checklists/delivery/<po>")
@login_required
def api_checklist_get(po):
    db = get_db()
    try:
        items = db.execute(
            "SELECT * FROM delivery_checklists WHERE po_number=? ORDER BY doc_type",
            (po,)
        ).fetchall()
        return jsonify({"items": [dict(r) for r in items]})
    finally:
        db.close()


@app.route("/api/checklists/delivery/<int:did>", methods=["PATCH"])
@require_role("manager")
@csrf_protect
def api_checklist_update(did):
    data = request.get_json(silent=True) or {}
    _VALID_CL_STATUSES = {"brak", "oczekuje", "wgrano", "zatwierdzono"}
    allowed = ["status", "notes", "file_path", "comparison_id", "due_date"]
    _cl_max_lens = {"status": 50, "notes": 500, "file_path": 500, "due_date": 20}
    fields, vals = [], []
    for k in allowed:
        if k in data:
            v = data[k]
            if k == "status":
                v = str(v or "").strip()
                if v not in _VALID_CL_STATUSES:
                    continue
            elif k == "comparison_id":
                # Wymuś int/None — inaczej dict/list z JSON trafiłby surowy do SQL (500).
                try:
                    v = int(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    continue
            elif k in _cl_max_lens:
                v = str(v or "")[:_cl_max_lens[k]]
            fields.append(f"{k}=?")
            vals.append(v)
    if not fields:
        return jsonify({"error": "Brak pól"}), 400
    fields.append("updated_at=datetime('now')")
    vals.append(did)
    db = get_db()
    try:
        # bandit: nazwy kolumn z białej listy w kodzie (nie z kluczy żądania); wartości przez ?
        db.execute(f"UPDATE delivery_checklists SET {', '.join(fields)} WHERE id=?", vals)  # nosec B608
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/checklists/templates", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_checklist_template_create():
    data = request.get_json(silent=True) or {}
    supplier = str(data.get("supplier_name") or "").strip()
    doc_type = str(data.get("doc_type") or "").strip().upper()
    if not supplier or not doc_type:
        return jsonify({"error": "Dostawca i typ dokumentu są wymagane"}), 400
    try:
        required = int(data.get("required", 1))
    except (ValueError, TypeError):
        required = 1
    required = 1 if required else 0
    db = get_db()
    try:
        db.execute(
            "INSERT INTO supplier_checklists(supplier_name, doc_type, required, notes) VALUES(?,?,?,?)",
            (supplier[:200], doc_type[:12], required, str(data.get("notes") or "")[:500])
        )
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/checklists/templates/<int:tid>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_checklist_template_delete(tid):
    db = get_db()
    try:
        db.execute("DELETE FROM supplier_checklists WHERE id=?", (tid,))
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# AKTUALIZACJA api_analyze — dodaj eksport + alert + profil dostawcy
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/analyze/export", methods=["POST"])
@login_required
@csrf_protect
def api_analyze_export():
    """Eksport aktualnego raportu (po analizie, przed zapisem do historii)."""
    import traceback
    data = request.get_json(force=True, silent=True) or {}
    fmt = data.get("format", "pdf")
    report = data.get("report", {})
    cid = data.get("comparison_id")
    safe_cid = secure_filename(str(cid)) if cid is not None else "x"
    if not report:
        return jsonify({"error": "Brak danych raportu"}), 400
    try:
        if fmt == "excel":
            from export_engine import export_excel
            from flask import Response
            b = export_excel(report, cid)
            fname = f"raport_{safe_cid}.xlsx"
            return Response(b,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f"attachment; filename={fname}"})
        else:
            from export_engine import export_pdf
            from flask import Response
            b = export_pdf(report, cid)
            fname = f"raport_{safe_cid}.pdf"
            return Response(b, mimetype="application/pdf",
                headers={"Content-Disposition": f"attachment; filename={fname}"})
    except Exception as e:
        import traceback as _tb
        app.logger.error("Export error: %s", _tb.format_exc())
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500


# ─────────────────────────────────────────────────────────────────────────────
# START
# ─────────────────────────────────────────────────────────────────────────────


# ════════════════════════════════════════════════════════════
# ARTWORK COMPARATOR + AI ENDPOINTS (auto-injected)
# ════════════════════════════════════════════════════════════

_artwork_cache: dict = {}        # {cache_id: (result, inserted_at, owner_id)}
_cache_lock = threading.RLock()  # protects _artwork_cache from concurrent access
_rpt_cache_lock = threading.Lock()  # protects app._artwork_report_cache
_CACHE_TTL = 7200               # 2 hours in seconds
_CACHE_MAX = 50

def _cache_put(cache_id: str, result, owner_id=None) -> None:
    with _cache_lock:
        _artwork_cache[cache_id] = (result, time.time(), owner_id)
        if len(_artwork_cache) > _CACHE_MAX:
            oldest = min(_artwork_cache, key=lambda k: _artwork_cache[k][1])
            _artwork_cache.pop(oldest, None)

def _cache_get(cache_id: str):
    with _cache_lock:
        entry = _artwork_cache.get(cache_id)
        if entry and (time.time() - entry[1]) < _CACHE_TTL:
            return entry[0]
        if entry:
            del _artwork_cache[cache_id]
    return None

def _cache_get_owner(cache_id: str):
    """Return the owner user_id stored for a cache entry, or None if absent/expired."""
    with _cache_lock:
        entry = _artwork_cache.get(cache_id)
        if entry and (time.time() - entry[1]) < _CACHE_TTL:
            return entry[2] if len(entry) > 2 else None
    return None

def _cache_evict(cache_id: str) -> None:
    """Remove a specific entry (e.g. on corrupt build)."""
    with _cache_lock:
        _artwork_cache.pop(cache_id, None)

def _cache_cleanup():
    while True:
        time.sleep(1800)
        now = time.time()
        with _cache_lock:
            # Wpis to 3-krotka (result, inserted_at, owner_id) — rozpakowanie na
            # 2-krotkę rzucało ValueError i po cichu zabijało ten wątek daemon na
            # pierwszym niepustym przebiegu (eksmisja w tle była martwa; TTL i tak
            # egzekwowane leniwie w _cache_get). Bierzemy timestamp po indeksie.
            expired = [k for k, v in list(_artwork_cache.items()) if (now - v[1]) >= _CACHE_TTL]
            for k in expired:
                _artwork_cache.pop(k, None)

threading.Thread(target=_cache_cleanup, daemon=True).start()

# Optional background warm-up of the heavy artwork models (DINOv2 + LightGlue) so
# the first real comparison doesn't pay their cold-start. Off by default (boot
# stays fast / no extra memory unless wanted); enable with ARTWORK_WARMUP=1 on
# artwork-heavy deployments. Runs in a daemon thread — never blocks startup.
if os.environ.get("ARTWORK_WARMUP", "0").strip().lower() in ("1", "true", "yes", "on"):
    def _warmup_artwork_models():
        try:
            from artwork_comparator import warmup_models
            warmup_models()
        except Exception as _e:
            app.logger.warning("artwork warmup failed: %s", _e)
    threading.Thread(target=_warmup_artwork_models, daemon=True).start()

_HEAVY_SEM = threading.Semaphore(3)  # max 3 concurrent heavy comparisons


# ═══════════════════════════════════════════════════════════════════════════════
# MODUŁ 3: SKANY DOSTAW — EKSTRAKCJA DANYCH AI
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/scan")
@login_required
def scan_page():
    return render_template("scan.html",
                           username=session["username"],
                           role=session["role"])


@app.route("/api/scan/extract", methods=["POST"])
@login_required
@csrf_protect
def api_scan_extract():
    """Przyjmuje jeden plik, ekstraktuje dane dostawy przez Claude Vision."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Brak pliku"}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    allowed = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp"}
    if ext not in allowed:
        return jsonify({"error": f"Nieobsługiwany format: {ext}"}), 400

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    uid    = session["user_id"]
    safe   = secure_filename(f.filename) or "scan.bin"
    path   = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_scan_{safe}")
    f.save(path)

    try:
        from scan_extractor import extract_from_file
        rows = extract_from_file(path, f.filename)
        return jsonify({"rows": rows, "count": len(rows)})
    except Exception as e:
        logger.exception("scan_extract error")
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


@app.route("/api/scan/export", methods=["POST"])
@login_required
@csrf_protect
def api_scan_export():
    """Generuje plik Excel z przekazanych wierszy."""
    data = request.get_json(silent=True) or {}
    rows = data.get("rows", [])
    if not rows:
        return jsonify({"error": "Brak danych"}), 400
    if not isinstance(rows, list) or len(rows) > 100_000:
        return jsonify({"error": "Nieprawidłowe dane wejściowe"}), 400
    try:
        from scan_extractor import export_to_excel
        xlsx = export_to_excel(rows)
        from flask import send_file
        buf = __import__("io").BytesIO(xlsx)
        buf.seek(0)
        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=f"dane_dostaw_{__import__('datetime').date.today()}.xlsx",
        )
    except Exception as e:
        logger.exception("scan_export error")
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500


@app.route("/artwork")
@login_required
def artwork_page():
    return render_template("artwork.html",
                           username=session["username"],
                           role=session["role"],
                           can_see_all=can_see_all(session["role"]))


@app.route("/artwork/history")
@login_required
def artwork_history_page():
    uid  = session["user_id"]
    role = session["role"]
    per_page = 50
    try:
        page = max(1, min(10000, int(request.args.get("page", 1))))
    except (ValueError, TypeError):
        page = 1
    offset = (page - 1) * per_page
    search = request.args.get("q", "")[:200].strip()
    status_filter = request.args.get("status", "").strip()
    if status_filter not in ("ok", "warning", "error", "critical", ""):
        status_filter = ""

    conditions = ["c.doc_type = 'artwork'", "(c.is_deleted IS NULL OR c.is_deleted=0)"]
    params: list = []
    if not can_see_all(role):
        conditions.append("(c.user_id = ? OR c.is_public = 1)")
        params.append(uid)
    if search:
        conditions.append("(c.file_a LIKE ? ESCAPE '\\' OR c.file_b LIKE ? ESCAPE '\\' OR u.username LIKE ? ESCAPE '\\')")
        esc = search.replace("\\","\\\\").replace("%","\\%").replace("_","\\_")
        like = f"%{esc}%"
        params.extend([like, like, like])
    if status_filter:
        conditions.append("c.status = ?")
        params.append(status_filter)

    where = "WHERE " + " AND ".join(conditions)

    db = get_db()
    try:
        # Single aggregate query for stats — avoids N+1
        stats_row = db.execute(
            # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
            f"""SELECT COUNT(*) as total,
                       SUM(CASE WHEN c.status IN ('critical','error') THEN 1 ELSE 0 END) as critical,
                       SUM(CASE WHEN c.status='ok' THEN 1 ELSE 0 END) as ok_cnt,
                       COUNT(DISTINCT c.user_id) as users
                FROM comparisons c JOIN users u ON c.user_id = u.id {where}""",  # nosec B608
            params
        ).fetchone()

        total_count = stats_row["total"] or 0
        stats = {
            "total": total_count,
            "critical": stats_row["critical"] or 0,
            "ok": stats_row["ok_cnt"] or 0,
            "users": stats_row["users"] or 0,
        }

        rows = db.execute(
            # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
            f"""SELECT c.id, c.file_a, c.file_b, c.status, c.diff_count,
                       c.created_at, c.user_id, c.is_public, u.username
                FROM comparisons c JOIN users u ON c.user_id = u.id
                {where}
                ORDER BY c.created_at DESC LIMIT ? OFFSET ?""",  # nosec B608
            params + [per_page, offset]
        ).fetchall()

        rows = [dict(r) | {"db_id": r["id"]} for r in rows]
        all_users = list(dict.fromkeys(r["username"] for r in rows))
        total_pages = max(1, (total_count + per_page - 1) // per_page)

        return render_template("artwork_history.html",
                               username=session["username"],
                               role=role,
                               rows=rows,
                               stats=stats,
                               all_users=all_users,
                               can_see_all=can_see_all(role),
                               current_uid=uid,
                               page=page,
                               total_pages=total_pages,
                               total_count=total_count,
                               per_page=per_page,
                               search=search,
                               status_filter=status_filter)
    finally:
        db.close()


@app.route("/artwork/kpi")
@login_required
def artwork_kpi_page():
    from datetime import datetime as _dt, timedelta, timezone as _tz_dt
    uid = session["user_id"]
    role = session.get("role", "user")
    db = get_db()
    try:
        if can_see_all(role):
            all_rows = db.execute("""
                SELECT c.status, c.diff_count, c.created_at, u.username, u.role
                FROM comparisons c JOIN users u ON c.user_id = u.id
                WHERE c.doc_type = 'artwork'
                ORDER BY c.created_at DESC
            """).fetchall()
        else:
            all_rows = db.execute("""
                SELECT c.status, c.diff_count, c.created_at, u.username, u.role
                FROM comparisons c JOIN users u ON c.user_id = u.id
                WHERE c.doc_type = 'artwork' AND c.user_id = ?
                ORDER BY c.created_at DESC
            """, (uid,)).fetchall()
        all_rows = [dict(r) for r in all_rows]

        total = len(all_rows)
        now = _dt.now(_tz_dt.utc)
        month_str = now.strftime("%Y-%m")
        this_month = sum(1 for r in all_rows if str(r.get("created_at") or "").startswith(month_str))
        total_errors = sum((r.get("diff_count") or 0) for r in all_rows)
        ok_count = sum(1 for r in all_rows if str(r.get("status") or "").lower() == "ok")
        ok_rate = round(ok_count / total * 100) if total else 0

        # Per-user stats
        from collections import defaultdict
        user_map = defaultdict(lambda: {"count": 0, "last_activity": "", "role": "user"})
        for r in all_rows:
            u = r["username"]
            user_map[u]["count"] += 1
            user_map[u]["role"] = r.get("role", "user")
            dt = r.get("created_at") or ""
            if dt > user_map[u]["last_activity"]:
                user_map[u]["last_activity"] = dt
        max_c = max((v["count"] for v in user_map.values()), default=1)
        user_stats = sorted([
            {"username": u, "count": v["count"], "role": v["role"],
             "last_activity": v["last_activity"],
             "pct": round(v["count"] / max_c * 100)}
            for u, v in user_map.items()
        ], key=lambda x: -x["count"])

        # Status breakdown
        from collections import Counter
        status_c = Counter(str(r.get("status") or "ok").lower() for r in all_rows)
        status_breakdown = [
            {"status": s, "count": c, "pct": round(c / max(total, 1) * 100)}
            for s, c in status_c.most_common()
        ]

        # Weekly (last 7 days)
        weekly = []
        for i in range(6, -1, -1):
            day = now - timedelta(days=i)
            day_str = day.strftime("%Y-%m-%d")
            cnt = sum(1 for r in all_rows if str(r.get("created_at") or "").startswith(day_str))
            weekly.append({"label": day.strftime("%d.%m"), "count": cnt})

        kpi = {
            "total": total, "this_month": this_month,
            "total_errors": total_errors, "ok_rate": ok_rate,
            "month_name": now.strftime("%B %Y"),
            "user_stats": user_stats,
            "status_breakdown": status_breakdown,
            "weekly": weekly,
        }
        return render_template("artwork_kpi.html",
                               username=session["username"],
                               role=session["role"],
                               kpi=kpi)
    finally:
        db.close()


@app.route("/artwork/design-preview")
@login_required
def artwork_design_preview():
    from flask import send_from_directory as _sfd
    import os as _os
    design_dir = _os.path.join(app.root_path, "static", "design")
    return _sfd(design_dir, "index.html")


@app.route("/artwork/design-preview/<path:filename>")
@login_required
def artwork_design_preview_assets(filename):
    from flask import send_file as _sf, abort as _abort
    from werkzeug.utils import safe_join as _safe_join
    import os as _os
    design_dir = _os.path.join(app.root_path, "static", "design")
    safe_path = _safe_join(design_dir, filename)
    if not safe_path or not _os.path.isfile(safe_path):
        _abort(404)
    return _sf(safe_path)


@app.route("/artwork/admin")
@login_required
def artwork_admin_page():
    if not can_see_all(session["role"]):
        return redirect(url_for("artwork_page"))
    db = get_db()
    try:
        users = db.execute("""
            SELECT u.id, u.username, u.email, u.role, u.created_at,
                   COUNT(c.id) as cmp_count
            FROM users u
            LEFT JOIN comparisons c ON c.user_id = u.id AND c.doc_type = 'artwork'
            GROUP BY u.id ORDER BY cmp_count DESC
        """).fetchall()
        users = [dict(r) for r in users]

        recent = db.execute("""
            SELECT c.file_a, c.file_b, c.status, c.diff_count, c.created_at, u.username
            FROM comparisons c JOIN users u ON c.user_id = u.id
            WHERE c.doc_type = 'artwork'
            ORDER BY c.created_at DESC LIMIT 15
        """).fetchall()
        recent = [dict(r) for r in recent]

        audit = db.execute("""
            SELECT event, username, detail, created_at
            FROM audit_log ORDER BY created_at DESC LIMIT 20
        """).fetchall()
        audit = [dict(r) for r in audit]

        from datetime import datetime as _dt, timezone as _tz
        today = _dt.now(_tz.utc).strftime("%Y-%m-%d")
        active_today = db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM comparisons WHERE doc_type='artwork' AND created_at LIKE ?",
            (today + "%",)
        ).fetchone()[0]

        admin = {
            "total_users": len(users),
            "total_comparisons": db.execute("SELECT COUNT(*) FROM comparisons WHERE doc_type='artwork'").fetchone()[0],
            "active_today": active_today,
            "users": users,
            "recent": recent,
            "audit_log": audit,
        }
        return render_template("artwork_admin.html",
                               username=session["username"],
                               role=session["role"],
                               admin=admin)
    finally:
        db.close()


@app.route("/api/artwork/preview-crops", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_preview_crops():
    """Extract field crops for preview/validation before running the actual comparison.

    Accepts the same FormData as split-compare (supplier_pdf, masters[],
    master_configs_json, pairs_json) but only renders and crops — no diff analysis.

    Returns:
      {pairs: [{pair_idx, master_name, profile_id, fields: [...], page_b_b64, page_b_w, page_b_h}]}
    """
    import traceback as _tb
    supplier_path = None
    master_paths = []
    comp_paths = []
    try:
        supplier_file = request.files.get("supplier_pdf")
        masters_files = request.files.getlist("masters[]")
        pairs_json_str  = request.form.get("pairs_json", "")
        master_configs_str = request.form.get("master_configs_json", "")

        if not supplier_file:
            return jsonify({"error": "Wymagany plik PDF dostawcy"}), 400

        uid = session["user_id"]
        upload_dir = app.config["UPLOAD_FOLDER"]
        os.makedirs(upload_dir, exist_ok=True)

        supplier_path = os.path.join(upload_dir,
                                     f"{uid}_pv_sup_{secure_filename(supplier_file.filename) or 'supplier.pdf'}")
        supplier_file.save(supplier_path)

        # Build master_paths (same logic as split-compare)
        if master_configs_str:
            try:
                master_configs = json.loads(master_configs_str)
            except Exception:
                return jsonify({"error": "Błąd parsowania master_configs_json"}), 400
            db_mc = get_db()
            _ensure_artwork_profile_tables(db_mc)
            try:
                for cfg in master_configs:
                    if cfg.get("type") == "profile":
                        pid = cfg.get("profile_id")
                        if not pid:
                            return jsonify({"error": "Brak profile_id w konfiguracji mastera"}), 400
                        row = db_mc.execute(
                            "SELECT name, master_pdf_path FROM artwork_profiles WHERE id=?", (pid,)
                        ).fetchone()
                        if not row or not row["master_pdf_path"]:
                            return jsonify({"error": f"Master z biblioteki (id={pid}) nie ma pliku PDF"}), 404
                        resolved = _resolve_master_path(row["master_pdf_path"], profile_id=pid, db=db_mc)
                        if not os.path.exists(resolved):
                            return jsonify({"error": f"Plik mastera nie istnieje: {resolved}"}), 404
                        master_paths.append({"name": row["name"], "path": resolved, "profile_id": pid, "is_temp": False})
                    else:
                        fi = cfg.get("file_idx")
                        if fi is None or not isinstance(fi, int) or fi < 0 or fi >= len(masters_files):
                            return jsonify({"error": "Brakujący plik mastera"}), 400
                        mf = masters_files[fi]
                        mp = os.path.join(upload_dir, f"{uid}_pv_mst_{len(master_paths)}_{secure_filename(mf.filename or '') or 'master.pdf'}")
                        mf.save(mp)
                        master_paths.append({"name": mf.filename, "path": mp, "profile_id": None, "is_temp": True})
            finally:
                db_mc.close()
        else:
            for i, mf in enumerate(masters_files):
                mp = os.path.join(upload_dir, f"{uid}_pv_mst_{i}_{secure_filename(mf.filename or '') or 'master.pdf'}")
                mf.save(mp)
                master_paths.append({"name": mf.filename, "path": mp, "profile_id": None, "is_temp": True})

        if pairs_json_str:
            try:
                pairs = json.loads(pairs_json_str)
            except Exception:
                return jsonify({"error": "Błąd parsowania pairs_json"}), 400
        else:
            pairs = [{"master_idx": i, "page": i} for i in range(len(master_paths))]

        from artwork_comparator import extract_field_crops_for_preview, _load_artwork_profile_by_id
        import fitz as _fitz_pv
        from pypdf import PdfReader, PdfWriter

        doc = _fitz_pv.open(supplier_path)
        reader = None
        try:
            reader = PdfReader(supplier_path)
        except Exception:
            pass

        result_pairs = []
        try:
            for i, pair in enumerate(pairs):
                try:
                    page_idx = int(pair.get("page", 0))
                    master_idx = int(pair.get("master_idx", 0))
                except (TypeError, ValueError):
                    continue
                if page_idx < 0 or master_idx < 0:
                    return jsonify({"error": "page_idx / master_idx musí být >= 0"}), 400
                if master_idx >= len(master_paths):
                    continue
                pid = master_paths[master_idx].get("profile_id")
                if not pid:
                    # No profile → no field crops to preview
                    result_pairs.append({
                        "pair_idx": i,
                        "master_name": master_paths[master_idx]["name"],
                        "profile_id": None,
                        "fields": [],
                        "page_b_b64": None,
                    })
                    continue

                profile = _load_artwork_profile_by_id(pid)
                if not profile or not profile.get("fields"):
                    result_pairs.append({
                        "pair_idx": i,
                        "master_name": master_paths[master_idx]["name"],
                        "profile_id": pid,
                        "fields": [],
                        "page_b_b64": None,
                    })
                    continue

                # Extract the supplier page as a separate file for rendering
                comp_path = None
                try:
                    if page_idx < len(doc):
                        if reader and page_idx < len(reader.pages):
                            writer = PdfWriter()
                            writer.add_page(reader.pages[page_idx])
                            comp_path = os.path.join(upload_dir, f"{uid}_pv_pg{page_idx}_{i}.pdf")
                            with open(comp_path, "wb") as fp:
                                writer.write(fp)
                        else:
                            pix = doc[page_idx].get_pixmap(matrix=_fitz_pv.Matrix(3.0, 3.0), alpha=False)
                            comp_path = os.path.join(upload_dir, f"{uid}_pv_pg{page_idx}_{i}.png")
                            pix.save(comp_path)
                        comp_paths.append(comp_path)
                    else:
                        comp_path = supplier_path

                    with _HEAVY_SEM:
                        preview_data = extract_field_crops_for_preview(
                            master_paths[master_idx]["path"],
                            comp_path,
                            profile,
                            page_a=0,
                            page_b=0,
                        )
                    result_pairs.append({
                        "pair_idx":    i,
                        "master_name": master_paths[master_idx]["name"],
                        "profile_id":  pid,
                        **preview_data,
                    })
                except Exception as _pe:
                    logger.warning("preview-crops pair %d error: %s", i, _pe)
                    result_pairs.append({
                        "pair_idx": i,
                        "master_name": master_paths[master_idx]["name"],
                        "profile_id": pid,
                        "fields": [],
                        "page_b_b64": None,
                        "error": str(_pe)[:200],
                    })
        finally:
            doc.close()

        return jsonify({"pairs": result_pairs})

    except Exception as e:
        logger.exception("preview-crops error")
        return jsonify({"error": "Błąd generowania podglądu"}), 500
    finally:
        temp_master_paths = [m["path"] for m in master_paths if m.get("is_temp", True)]
        for p in ([supplier_path] + temp_master_paths + comp_paths):
            try:
                if p:
                    os.remove(p)
            except Exception:
                pass


@app.route("/api/artwork/split-compare", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_split_compare():
    """
    Wielostronicowy PDF dostawcy → porównuje z masterami wg jawnych par.

    Form fields:
    - supplier_pdf: PDF file
    - masters[]: N master files (indexed 0..N-1)
    - pairs_json: JSON array [{master_idx, page, crop, label}, ...]
      crop format: {x0,y0,x1,y1} as fractions 0-1, or null for full page
    - use_ai: "0" | "1"

    Fallback (no pairs_json): auto-pair by index order (old behaviour).
    """
    import traceback as _tb
    supplier_path = None
    master_paths = []
    comp_paths = []
    try:
        supplier_file = request.files.get("supplier_pdf")
        masters_files = request.files.getlist("masters[]")
        use_ai = request.form.get("use_ai", "0") == "1"
        pairs_json_str = request.form.get("pairs_json", "")
        master_configs_str = request.form.get("master_configs_json", "")
        field_overrides_str = request.form.get("field_overrides_json", "")

        if not supplier_file or not supplier_file.filename:
            return jsonify({"error": "Wymagany plik PDF dostawcy"}), 400
        if not masters_files and not master_configs_str:
            return jsonify({"error": "Wymagane pliki masterów"}), 400
        if not supplier_file.filename.lower().endswith(".pdf"):
            return jsonify({"error": "Plik dostawcy musi być PDF"}), 400

        uid = session["user_id"]
        upload_dir = app.config["UPLOAD_FOLDER"]
        os.makedirs(upload_dir, exist_ok=True)

        supplier_path = os.path.join(upload_dir,
                                     f"{uid}_spl_sup_{secure_filename(supplier_file.filename) or 'supplier.pdf'}")
        supplier_file.save(supplier_path)

        # Build master_paths from either uploaded files or library profiles
        if master_configs_str:
            try:
                master_configs = json.loads(master_configs_str)
            except Exception:
                return jsonify({"error": "Błąd parsowania master_configs_json"}), 400
            db_mc = get_db()
            _ensure_artwork_profile_tables(db_mc)
            _mc_error = None
            try:
                for cfg in master_configs:
                    if cfg.get("type") == "profile":
                        pid = cfg.get("profile_id")
                        if not pid:
                            _mc_error = ({"error": "Brakujący profile_id w konfiguracji mastera"}, 400)
                            break
                        row = db_mc.execute(
                            "SELECT name, master_pdf_path FROM artwork_profiles WHERE id=?",
                            (pid,)
                        ).fetchone()
                        if not row or not row["master_pdf_path"]:
                            _mc_error = ({"error": f"Master z biblioteki (id={pid}) nie ma pliku PDF — dodaj plik w edytorze szablonów"}, 404)
                            break
                        resolved = _resolve_master_path(row["master_pdf_path"], profile_id=pid, db=db_mc)
                        if not os.path.exists(resolved):
                            _mc_error = ({"error": f"Plik mastera nie istnieje na dysku: {resolved}"}, 404)
                            break
                        master_paths.append({"name": row["name"], "path": resolved, "profile_id": pid, "is_temp": False})
                    else:
                        fi = cfg.get("file_idx")
                        if fi is None or not isinstance(fi, int) or fi < 0 or fi >= len(masters_files):
                            _mc_error = ({"error": "Brakujący plik mastera — odśwież stronę"}, 400)
                            break
                        mf = masters_files[fi]
                        mp = os.path.join(upload_dir, f"{uid}_spl_mst_{len(master_paths)}_{secure_filename(mf.filename or '') or 'master.pdf'}")
                        mf.save(mp)
                        master_paths.append({"name": mf.filename, "path": mp, "profile_id": None, "is_temp": True})
            finally:
                db_mc.close()
            if _mc_error:
                return jsonify(_mc_error[0]), _mc_error[1]
        else:
            # Legacy: all uploaded files
            for i, mf in enumerate(masters_files):
                mp = os.path.join(upload_dir, f"{uid}_spl_mst_{i}_{secure_filename(mf.filename or '') or 'master.pdf'}")
                mf.save(mp)
                master_paths.append({"name": mf.filename, "path": mp, "profile_id": None, "is_temp": True})

        # Parse or generate pairs
        if pairs_json_str:
            try:
                pairs = json.loads(pairs_json_str)
            except Exception:
                return jsonify({"error": "Błąd parsowania pairs_json"}), 400
        else:
            # Fallback: auto-pair by index order
            try:
                from pypdf import PdfReader
                page_count = len(PdfReader(supplier_path).pages)
            except Exception:
                page_count = len(master_paths)
            if page_count != len(master_paths):
                return jsonify({
                    "error": (f"PDF dostawcy ma {page_count} stron, "
                              f"ale wgrano {len(master_paths)} masterów.")
                }), 400
            pairs = [{"master_idx": i, "page": i, "crop": None,
                      "label": f"Strona {i+1}"} for i in range(page_count)]

        # Validate
        for pair in pairs:
            mi = pair.get("master_idx")
            if not isinstance(mi, int) or mi < 0 or mi >= len(master_paths):
                return jsonify({"error": f"Nieprawidłowy master_idx: {mi}"}), 400

        import fitz
        from artwork_comparator import set_progress as _set_progress
        from artwork_engine_client import compare_artworks
        import hashlib as _hl, time as _t
        _progress_id = (request.form.get("progress_id") or "").strip() or None
        # field_overrides_json: {pair_idx: {field_name: {x1_pct,y1_pct,x2_pct,y2_pct}|null}}
        try:
            _field_overrides_all = json.loads(field_overrides_str) if field_overrides_str else {}
        except Exception:
            _field_overrides_all = {}

        try:
            from pypdf import PdfReader, PdfWriter
            reader = PdfReader(supplier_path)
        except Exception:
            reader = None

        doc = fitz.open(supplier_path)
        try:
            results = []
            for i, pair in enumerate(pairs):
                try:
                    page_idx = int(pair.get("page", 0))
                    master_idx = int(pair.get("master_idx", 0))
                except (TypeError, ValueError):
                    continue
                if page_idx < 0 or master_idx < 0:
                    return jsonify({"error": "page_idx / master_idx musí być >= 0"}), 400
                if master_idx >= len(master_paths):
                    return jsonify({"error": "master_idx poza zakresem"}), 400
                crop = pair.get("crop")   # None or {x0,y0,x1,y1} in 0-1 coords
                unit_label = pair.get("label") or f"Strona {page_idx + 1}"
                master_name = master_paths[master_idx]["name"]
                master_path_val = master_paths[master_idx]["path"]

                if page_idx >= len(doc):
                    results.append({
                        "pair_index": i + 1, "master_name": master_name,
                        "supplier_page": page_idx + 1, "unit_label": unit_label,
                        "risk_level": "error",
                        "error": f"Strona {page_idx + 1} nie istnieje w PDF.",
                    })
                    continue

                comp_path = None
                try:
                    page = doc[page_idx]
                    r = page.rect

                    if crop and isinstance(crop, dict) and all(k in crop for k in ("x0", "y0", "x1", "y1")):
                        try:
                            cx0, cy0, cx1, cy1 = (float(crop[k]) for k in ("x0", "y0", "x1", "y1"))
                        except (TypeError, ValueError):
                            return jsonify({"error": "Wartości crop muszą być liczbami"}), 400
                        if not all(0.0 <= v <= 1.0 for v in (cx0, cy0, cx1, cy1)):
                            return jsonify({"error": "Wartości crop muszą mieścić się w zakresie [0.0, 1.0]"}), 400
                        # Render cropped region as PNG at high resolution
                        clip = fitz.Rect(
                            cx0 * r.width,  cy0 * r.height,
                            cx1 * r.width,  cy1 * r.height,
                        )
                        pix = page.get_pixmap(matrix=fitz.Matrix(3.0, 3.0),
                                              clip=clip, alpha=False)
                        comp_path = os.path.join(upload_dir, f"{uid}_spl_crop_{i}.png")
                        pix.save(comp_path)
                    elif reader:
                        writer = PdfWriter()
                        writer.add_page(reader.pages[page_idx])
                        comp_path = os.path.join(upload_dir,
                                                 f"{uid}_spl_pg{page_idx}_{i}.pdf")
                        with open(comp_path, "wb") as fp:
                            writer.write(fp)
                    else:
                        pix = page.get_pixmap(matrix=fitz.Matrix(3.0, 3.0), alpha=False)
                        comp_path = os.path.join(upload_dir,
                                                 f"{uid}_spl_pg{page_idx}_{i}.png")
                        pix.save(comp_path)

                    comp_paths.append(comp_path)

                    _fob = _field_overrides_all.get(str(i)) or _field_overrides_all.get(i)
                    if _progress_id and len(pairs) > 1:
                        _set_progress(_progress_id, 8, f"Para {i+1}/{len(pairs)} — start…")
                    with _HEAVY_SEM:
                        result = compare_artworks(master_path_val, comp_path,
                                                  use_ai=use_ai,
                                                  profile_id=master_paths[master_idx].get("profile_id"),
                                                  field_overrides_b=_fob,
                                                  progress_id=_progress_id)

                    cache_id = _hl.sha256(f"{uid}_{_t.time()}_{i}".encode()).hexdigest()[:12]
                    _cache_put(cache_id, result, owner_id=uid)

                    rd = result.to_dict(include_images=False)
                    rd.update({
                        "cache_id": cache_id,
                        "pair_index": i + 1,
                        "master_name": master_name,
                        "supplier_page": page_idx + 1,
                        "unit_label": unit_label,
                    })

                    try:
                        rdsave = dict(rd)
                        rdsave["status"] = result.risk_level
                        rdsave["diff_count"] = result.critical_count + result.important_count
                        _save_comparison(uid, "artwork", master_name,
                                         f"{supplier_file.filename} — {unit_label}", rdsave)
                    except Exception:
                        pass

                    results.append(rd)

                except Exception as e:
                    results.append({
                        "pair_index": i + 1,
                        "master_name": master_name,
                        "supplier_page": page_idx + 1,
                        "unit_label": unit_label,
                        "risk_level": "error",
                        "error": str(e)[:200],
                    })
                    if comp_path and comp_path not in comp_paths:
                        comp_paths.append(comp_path)

            _set_progress(_progress_id, 99, "Generowanie raportu…")
            return jsonify({"results": results, "total": len(pairs),
                            "supplier_name": supplier_file.filename})
        finally:
            doc.close()

    except Exception as e:
        logger.exception("artwork split-compare error")
        return jsonify({"error": "Błąd porównania artworków"}), 500
    finally:
        temp_master_paths = [m["path"] for m in master_paths if m.get("is_temp", True)]
        for p in ([supplier_path] + temp_master_paths + comp_paths):
            if p:
                try:
                    os.remove(p)
                except Exception:
                    pass



@app.route("/api/artwork/compare-progress/<pid>", methods=["GET"])
@login_required
def api_artwork_compare_progress(pid):
    """Bieżący etap porównania (do paska postępu). Frontend odpytuje co ~0.7 s."""
    try:
        from artwork_comparator import get_progress
        p = get_progress(pid) or {}
    except Exception:
        p = {}
    return jsonify({"pct": p.get("pct", 0), "label": p.get("label", "")})


@app.route("/api/artwork/compare", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_compare():
    _uid = session["user_id"]
    if _check_and_record_api_rate(_uid):
        return jsonify({"error": "Zbyt wiele żądań. Odczekaj minutę i spróbuj ponownie."}), 429
    import traceback as _tb
    path_a = path_b = None
    uploaded_path_a = False  # track if we saved file_a ourselves (so we can delete it)
    try:
        file_a = request.files.get("file_a")
        file_b = request.files.get("file_b")
        use_ai = request.form.get("use_ai", "0") == "1"
        master_profile_id = request.form.get("master_profile_id", type=int)
        master_name = None

        ALLOWED = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp"}
        uid = session["user_id"]
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

        # --- File A: either uploaded or from master library ---
        if master_profile_id:
            db = get_db()
            try:
                _ensure_artwork_profile_tables(db)
                row = db.execute(
                    "SELECT name, master_pdf_path FROM artwork_profiles WHERE id=?",
                    (master_profile_id,)
                ).fetchone()
            finally:
                db.close()
            if not row or not row["master_pdf_path"]:
                return jsonify({"error": "Master nie znaleziony w bibliotece lub brak pliku PDF"}), 404
            resolved_a = _resolve_master_path(row["master_pdf_path"], profile_id=master_profile_id)
            if not os.path.exists(resolved_a):
                return jsonify({"error": f"Plik mastera nie istnieje na dysku: {resolved_a}"}), 404
            path_a = resolved_a
            master_name = row["name"]
        elif file_a and file_a.filename:
            ext_a = os.path.splitext(file_a.filename.lower())[1]
            if ext_a not in ALLOWED:
                return jsonify({"error": "Dozwolone: PDF, JPG, PNG, TIFF"}), 400
            path_a = os.path.join(app.config["UPLOAD_FOLDER"],
                                  f"{uid}_aw_a_{secure_filename(file_a.filename) or 'art_a'}")
            file_a.save(path_a)
            uploaded_path_a = True
        else:
            return jsonify({"error": "Wymagany plik A lub wybór mastera z biblioteki"}), 400

        # --- File B: always uploaded ---
        if not file_b or not file_b.filename:
            return jsonify({"error": "Wymagany plik B (wersja fabryczna)"}), 400
        ext_b = os.path.splitext(file_b.filename.lower())[1]
        if ext_b not in ALLOWED:
            return jsonify({"error": "Dozwolone: PDF, JPG, PNG, TIFF"}), 400
        path_b = os.path.join(app.config["UPLOAD_FOLDER"],
                              f"{uid}_aw_b_{secure_filename(file_b.filename) or 'art_b'}")
        file_b.save(path_b)

        # Reject unreasonably large files early
        for p, label in [(path_a, "A"), (path_b, "B")]:
            size_mb = os.path.getsize(p) / 1024 / 1024
            if size_mb > 150:
                return jsonify({"error": f"Plik {label} jest zbyt duży ({size_mb:.0f} MB). Maksimum 150 MB."}), 400
        from artwork_comparator import set_ai_model, AVAILABLE_MODELS
        from artwork_engine_client import compare_artworks
        page_a = request.form.get("page_a", type=int)
        page_b = request.form.get("page_b", type=int)
        ai_model = request.form.get("ai_model", "claude-sonnet-4-6")
        valid_ids = {m[0] for m in AVAILABLE_MODELS}
        if ai_model not in valid_ids:
            return jsonify({"error": f"Nieznany model AI: {ai_model}. Dostępne: {sorted(valid_ids)}"}), 400
        set_ai_model(ai_model)
        # On-demand performance profiling — checkbox in UI or ?perf=1; ARTWORK_PROFILE
        # env forces it on globally. Adds a detailed "Wydajność" section to the report.
        _profile_perf = str(request.form.get("profile_perf")
                            or request.args.get("perf") or "").strip().lower() in ("1", "true", "on", "yes")
        _progress_id = (request.form.get("progress_id") or "").strip() or None
        import time as _time
        _t0 = _time.monotonic()
        with _HEAVY_SEM:
            result = compare_artworks(path_a, path_b, use_ai=use_ai,
                                      use_ai_sections=use_ai, max_pages=6,
                                      page_a=page_a, page_b=page_b,
                                      profile_id=master_profile_id,
                                      profile_perf=_profile_perf,
                                      progress_id=_progress_id)
        _dur = int((_time.monotonic() - _t0) * 1000)
        import hashlib as _hl
        cache_id = _hl.sha256(f"{uid}_{_time.time()}".encode()).hexdigest()[:12]
        _cache_put(cache_id, result, owner_id=uid)
        data = result.to_dict(include_images=False)
        data["cache_id"] = cache_id
        name_a = master_name or (file_a.filename if file_a else os.path.basename(path_a))
        name_b = file_b.filename if file_b else ""
        try:
            full_data = result.to_dict(include_images=False)
            full_data["cache_id"] = cache_id
            full_data["status"] = result.risk_level
            full_data["diff_count"] = result.critical_count + result.important_count
            _save_comparison(uid, "artwork", name_a, name_b, full_data)
            if master_profile_id:
                _db = get_db()
                try:
                    _db.execute(
                        "UPDATE artwork_profiles SET use_count=COALESCE(use_count,0)+1 WHERE id=?",
                        (master_profile_id,)
                    )
                    _db.commit()
                finally:
                    _db.close()
        except Exception:
            pass
        _log_audit("artwork_compare", detail=f"{name_a} vs {name_b}",
                   duration_ms=_dur,
                   extra={"file_a": name_a, "file_b": name_b,
                          "profile_id": master_profile_id,
                          "risk_level": result.risk_level,
                          "critical": result.critical_count,
                          "important": result.important_count,
                          "use_ai": use_ai, "duration_ms": _dur})
        return jsonify(data)
    except Exception as e:
        logger.exception("artwork compare error")
        _log_audit("artwork_compare_error", detail=str(e)[:200])
        return jsonify({"error": "Błąd porównania artwork"}), 500
    finally:
        if uploaded_path_a and path_a:
            try: os.remove(path_a)
            except Exception: pass
        if path_b:
            try: os.remove(path_b)
            except Exception: pass


@app.route("/api/artwork/pdf-page-count", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_pdf_page_count():
    """Return page count for an uploaded PDF without storing it."""
    f = request.files.get("file")
    if not f or not f.filename or not f.filename.lower().endswith(".pdf"):
        return jsonify({"pages": 1})
    uid = session["user_id"]
    tmp_path = os.path.join(app.config["UPLOAD_FOLDER"],
                            f"{uid}_tmp_pgcount_{secure_filename(f.filename) or 'count.pdf'}")
    try:
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
        f.save(tmp_path)
        from artwork_comparator import count_pdf_pages
        n = count_pdf_pages(tmp_path)
        return jsonify({"pages": n or 1})
    except Exception as e:
        return jsonify({"pages": 1, "error": str(e)[:200]})
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


@app.route("/artwork/3d")
@login_required
def artwork_3d_page():
    """Strona generatora modeli 3D opakowań z artworków (dieline PDF)."""
    return render_template("artwork_3d.html",
                           username=session["username"],
                           role=session["role"],
                           max_upload_mb=MAX_UPLOAD_MB)


@app.route("/api/artwork/3d", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_3d():
    """Generuje model GLB opakowania z wgranego artworku PDF (dieline).

    Form-data: file (PDF), opcjonalnie w_mm/h_mm/d_mm (ręczne wymiary,
    nadpisują odczytane z tabelki artworku).
    """
    import uuid as _uuid_3d

    f = request.files.get("file")
    if not f or not f.filename or not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Wgraj plik PDF z dieline opakowania."}), 400
    if f.mimetype and f.mimetype not in _ALLOWED_PDF_MIMES:
        return jsonify({"error": f"Niedozwolony typ pliku: {f.mimetype}"}), 400

    dims_override = None
    raw_dims = [request.form.get(k, "").strip() for k in ("w_mm", "h_mm", "d_mm")]
    if any(raw_dims):
        try:
            vals = [float(v.replace(",", ".")) for v in raw_dims]
            if not all(5.0 <= v <= 2000.0 for v in vals):
                raise ValueError
            dims_override = tuple(vals)
        except ValueError:
            return jsonify({"error": "Nieprawidłowe wymiary — podaj trzy wartości 5–2000 mm."}), 400

    uid = session["user_id"]
    out_dir = os.path.join(app.config["UPLOAD_FOLDER"], "artwork3d")
    os.makedirs(out_dir, exist_ok=True)
    token = _uuid_3d.uuid4().hex[:12]
    src_name = secure_filename(f.filename) or "artwork.pdf"
    tmp_pdf = os.path.join(out_dir, f"{uid}_src_{token}_{src_name}")
    glb_name = f"{uid}_{token}.glb"
    glb_path = os.path.join(out_dir, glb_name)
    try:
        f.save(tmp_pdf)
        from artwork_3d import generate_artwork_3d
        result = generate_artwork_3d(tmp_pdf, glb_path, dims_override=dims_override)
        _log_audit("artwork_3d", session.get("username"),
                   f"{src_name} → {glb_name}, dims={result['dims_mm']}")
        return jsonify({
            "glb_url": url_for("artwork_3d_model", fname=glb_name),
            "dims_mm": result["dims_mm"],
            "package_type": result["package_type"],
            "ean": result["ean"],
            "panels_found": result["panels_found"],
            "warnings": result["warnings"],
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        logger.exception("artwork 3d generation failed")
        return jsonify({"error": f"Generowanie modelu nie powiodło się: {str(e)[:200]}"}), 500
    finally:
        try:
            os.remove(tmp_pdf)
        except OSError:
            pass


@app.route("/artwork/3d/model/<path:fname>")
@login_required
def artwork_3d_model(fname):
    """Serwuje wygenerowany model GLB. User widzi tylko swoje modele."""
    from flask import send_from_directory, abort
    uid = session["user_id"]
    safe = secure_filename(fname)
    if not safe.endswith(".glb"):
        abort(404)
    if not safe.startswith(f"{uid}_") and not can_see_all(session["role"]):
        abort(403)
    out_dir = os.path.abspath(os.path.join(app.config["UPLOAD_FOLDER"], "artwork3d"))
    return send_from_directory(out_dir, safe, mimetype="model/gltf-binary")


@app.route("/api/artwork/report/<int:cid>")
@login_required
def api_artwork_report_by_id(cid):
    uid  = session["user_id"]
    role = session["role"]
    db   = get_db()
    try:
        row = db.execute(
            "SELECT c.*, u.username FROM comparisons c JOIN users u ON c.user_id=u.id "
            "WHERE c.id=? AND c.doc_type='artwork' AND (c.is_deleted IS NULL OR c.is_deleted=0)",
            (cid,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono raportu"}), 404
        if row["user_id"] != uid and not row["is_public"] and not can_see_all(role):
            return jsonify({"error": "Brak dostępu"}), 403
        try:
            d = json.loads(row["result_json"] or "{}")
        except Exception:
            d = {}
        d["db_id"]      = cid
        d["file_a"]     = row["file_a"]
        d["file_b"]     = row["file_b"]
        d["username"]   = row["username"]
        d["created_at"] = row["created_at"]
        d["is_public"]  = row["is_public"]
        # Attach per-field comments
        comments_rows = db.execute(
            "SELECT field_name, comment, updated_at FROM artwork_field_comments WHERE comparison_id=?",
            (cid,)
        ).fetchall()
        d["field_comments"] = {r["field_name"]: {"comment": r["comment"], "updated_at": r["updated_at"]}
                               for r in comments_rows}
        return jsonify(d)
    finally:
        db.close()


@app.route("/api/artwork/report/<int:cid>/field-comment", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_field_comment_save(cid):
    """Upsert a comment on a single artwork field row."""
    uid  = session["user_id"]
    role = session["role"]
    db   = get_db()
    try:
        row = db.execute(
            "SELECT user_id, is_public FROM comparisons WHERE id=? AND doc_type='artwork'", (cid,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono raportu"}), 404
        if row["user_id"] != uid and not can_see_all(role):
            return jsonify({"error": "Brak dostępu"}), 403
        data = request.get_json(silent=True) or {}
        field_name = str(data.get("field_name") or "").strip()[:200]
        comment    = str(data.get("comment") or "").strip()[:2000]
        if not field_name:
            return jsonify({"error": "Wymagane: field_name"}), 400
        if not comment:
            db.execute(
                "DELETE FROM artwork_field_comments WHERE comparison_id=? AND field_name=?",
                (cid, field_name)
            )
        else:
            db.execute(
                """INSERT INTO artwork_field_comments(comparison_id, field_name, comment, created_by, updated_at)
                   VALUES(?,?,?,?,datetime('now'))
                   ON CONFLICT(comparison_id, field_name)
                   DO UPDATE SET comment=excluded.comment, updated_at=excluded.updated_at""",
                (cid, field_name, comment, uid)
            )
        db.commit()
        return jsonify({"ok": True, "field_name": field_name, "comment": comment})
    finally:
        db.close()


@app.route("/api/artwork/report/<int:cid>/visibility", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_report_visibility(cid):
    uid = session["user_id"]
    db  = get_db()
    try:
        row = db.execute("SELECT user_id, is_public FROM comparisons WHERE id=? AND doc_type='artwork'", (cid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        if row["user_id"] != uid and not can_see_all(session["role"]):
            return jsonify({"error": "Brak dostępu"}), 403
        new_val = 0 if row["is_public"] else 1
        db.execute("UPDATE comparisons SET is_public=? WHERE id=?", (new_val, cid))
        db.commit()
        return jsonify({"ok": True, "is_public": new_val})
    finally:
        db.close()


@app.route("/api/artwork/page/<cache_id>/<int:page_idx>")
@login_required
def api_artwork_page_images(cache_id, page_idx):
    result = _cache_get(cache_id)
    if not result:
        return jsonify({"error": "Sesja wygasła — wgraj pliki ponownie"}), 404
    owner_id = _cache_get_owner(cache_id)
    if owner_id is not None and owner_id != session["user_id"] and not can_see_all(session["role"]):
        return jsonify({"error": "Brak dostępu"}), 403
    return jsonify(result.page_images(page_idx))


@app.route("/api/artwork/pdf-pages", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_pdf_pages():
    """Zwraca liczbę stron i miniatury dla wgranego PDF.
    Jeśli page_idx podany → zwraca tylko jedną stronę (hires dla crop modal).
    """
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Brak pliku"}), 400
    uid = session["user_id"]
    fname = secure_filename(f.filename) or "pages.pdf"
    path = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_pages_{fname}")
    try:
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
        f.save(path)
        try:
            import fitz, base64
            try:
                scale_param = float(request.form.get("scale", 0.45))
            except (ValueError, TypeError):
                scale_param = 0.45
            scale = max(0.1, min(4.0, scale_param))
            page_idx_param = request.form.get("page_idx")
            doc = fitz.open(path)
            try:
                pages = []
                mat = fitz.Matrix(scale, scale)
                indices = range(len(doc))
                if page_idx_param is not None:
                    try:
                        idx = int(page_idx_param)
                    except (ValueError, TypeError):
                        idx = 0
                    indices = [idx] if 0 <= idx < len(doc) else []
                quality = 90 if scale >= 1.0 else 75
                for i in indices:
                    page = doc[i]
                    pix = page.get_pixmap(matrix=mat, alpha=False)
                    thumb = base64.b64encode(pix.tobytes("jpeg", jpg_quality=quality)).decode()
                    w_mm = round(page.rect.width / 72 * 25.4)
                    h_mm = round(page.rect.height / 72 * 25.4)
                    pages.append({
                        "index": i, "thumb": thumb,
                        "size_mm": f"{w_mm}×{h_mm}",
                        "width_px": pix.width,
                        "height_px": pix.height,
                    })
            finally:
                doc.close()
            return jsonify({"count": len(pages), "pages": pages})
        except Exception as e:
            return jsonify({"count": 1, "pages": [], "error": str(e)[:200]})
    finally:
        try:
            os.remove(path)
        except Exception:
            pass



@app.route("/api/artwork/history")
@login_required
def api_artwork_history():
    uid = session["user_id"]
    role = session["role"]
    db = get_db()
    try:
        if can_see_all(role):
            rows = db.execute("""
                SELECT c.id, c.file_a, c.file_b, c.status, c.diff_count,
                       c.created_at, u.username
                FROM comparisons c
                JOIN users u ON c.user_id = u.id
                WHERE c.doc_type = 'artwork' AND (c.is_deleted IS NULL OR c.is_deleted=0)
                ORDER BY c.created_at DESC LIMIT 50
            """).fetchall()
        else:
            rows = db.execute("""
                SELECT c.id, c.file_a, c.file_b, c.status, c.diff_count,
                       c.created_at, u.username
                FROM comparisons c
                JOIN users u ON c.user_id = u.id
                WHERE c.user_id = ? AND c.doc_type = 'artwork'
                  AND (c.is_deleted IS NULL OR c.is_deleted=0)
                ORDER BY c.created_at DESC LIMIT 20
            """, (uid,)).fetchall()
    finally:
        db.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/artwork/compare-multi", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_compare_multi():
    """Porownuje N artworkow — buduje macierz rozbieznosci pol."""
    files = request.files.getlist("files[]")
    if len(files) < 2:
        return jsonify({"error": "Wymagane co najmniej 2 pliki"}), 400
    if len(files) > 6:
        return jsonify({"error": "Maksymalnie 6 plikow naraz"}), 400

    ALLOWED = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif"}
    paths = []
    try:
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
        uid = session["user_id"]
        for i, f in enumerate(files):
            if not f or not f.filename:
                return jsonify({"error": "Pusty plik w żądaniu"}), 400
            ext = os.path.splitext(f.filename.lower())[1]
            if ext not in ALLOWED:
                return jsonify({"error": f"Plik '{f.filename}': nieobslugiwany format '{ext}'"}), 400
            fname = secure_filename(f.filename) or f"multi_{i}.pdf"
            path = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_multi_{i}_{fname}")
            f.save(path)
            paths.append(path)

        from artwork_comparator import compare_multi_artworks
        with _HEAVY_SEM:
            result = compare_multi_artworks(paths)
        return jsonify(result)

    except Exception as e:
        import traceback as _tb
        app.logger.error("Multi-artwork compare error: %s", _tb.format_exc())
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500
    finally:
        for p in paths:
            try:
                os.remove(p)
            except Exception:
                pass


def _build_report_from_cache_or_db(cache_id, comp_id=None, user_id=None, role=None):
    """Próbuje zbudować raport z cache (obiekt) lub DB (result_json).

    Accepts either a cache_id (in-session string) or a comp_id (DB row integer).
    When comp_id is given, user_id and role are used for ownership enforcement.
    """
    from artwork_report_engine import build_report_from_comparison, build_demo_report
    # Batch pre-built report cache
    with _rpt_cache_lock:
        _rpt_cache = getattr(app, "_artwork_report_cache", {})
        if cache_id and cache_id in _rpt_cache:
            rpt = _rpt_cache[cache_id]
            # Enforce ownership on the pre-built report cache (mirrors artwork_report_page).
            if user_id is not None:
                if isinstance(rpt, dict):
                    _owner = rpt.get("_owner_id")
                    _public = rpt.get("_is_public")
                else:
                    _owner = getattr(rpt, "_owner_id", None)
                    _public = getattr(rpt, "_is_public", None)
                if _owner is not None and _owner != user_id and not _public and not can_see_all(role):
                    return None
            return rpt
    _cached_result = _cache_get(cache_id) if cache_id else None
    if _cached_result:
        # Enforce ownership on the in-memory result cache (IDOR guard for exports).
        if cache_id and user_id is not None:
            _owner = _cache_get_owner(cache_id)
            if _owner is not None and _owner != user_id and not can_see_all(role):
                return None
        try:
            return build_report_from_comparison(_cached_result)
        except Exception:
            _cache_evict(cache_id)
    # Direct DB lookup by comparison ID
    if comp_id:
        try:
            db = get_db()
            try:
                row = db.execute(
                    "SELECT file_a, file_b, result_json, user_id AS owner_id, is_public"
                    " FROM comparisons WHERE id=? AND doc_type='artwork' AND (is_deleted IS NULL OR is_deleted=0)",
                    (comp_id,)
                ).fetchone()
            finally:
                db.close()
            if row:
                if user_id is not None and not can_see_all(role) and not row["is_public"] and row["owner_id"] != user_id:
                    return None
                d = json.loads(row["result_json"] or "{}")
                d["file_a"] = row["file_a"]
                d["file_b"] = row["file_b"]
                return build_report_from_comparison(d)
        except Exception:
            pass
    if cache_id:
        # Szukaj w DB po cache_id zapisanym w result_json
        try:
            db = get_db()
            try:
                row = db.execute(
                    "SELECT file_a, file_b, result_json, user_id, is_public FROM comparisons "
                    "WHERE doc_type='artwork' AND (is_deleted IS NULL OR is_deleted=0) "
                    "ORDER BY created_at DESC LIMIT 100"
                ).fetchall()
            finally:
                db.close()
            for r in row:
                try:
                    d = json.loads(r["result_json"] or "{}")
                    if d.get("cache_id") == cache_id:
                        d["file_a"] = r["file_a"]
                        d["file_b"] = r["file_b"]
                        d["_owner_id"] = r["user_id"]
                        d["_is_public"] = r["is_public"]
                        return build_report_from_comparison(d)
                except Exception:
                    pass
        except Exception:
            pass
    return None


@app.route("/artwork/report")
@login_required
def artwork_report_page():
    """Renderuje strukturalny raport porownania artworkow (z cache, DB lub demo)."""
    cache_id = request.args.get("cache_id")
    comp_id  = request.args.get("id", type=int)
    uid  = session["user_id"]
    role = session["role"]
    report = _build_report_from_cache_or_db(cache_id)
    # Enforce ownership on cache_id path.
    # report may be a dict (built from DB JSON) or an ArtworkReport dataclass
    # (retrieved directly from _artwork_report_cache) — use getattr fallback.
    if report and cache_id:
        if isinstance(report, dict):
            owner_id = report.get("_owner_id")
            is_public = report.get("_is_public")
        else:
            owner_id = getattr(report, "_owner_id", None)
            is_public = getattr(report, "_is_public", None)
        if owner_id is not None and owner_id != uid and not is_public and not can_see_all(role):
            report = None

    # Fallback: load from DB by comparison id
    if not report and comp_id:
        db   = get_db()
        try:
            row = db.execute(
                "SELECT * FROM comparisons WHERE id=? AND doc_type='artwork'", (comp_id,)
            ).fetchone()
            if row and (row["user_id"] == uid or row["is_public"] or can_see_all(role)):
                try:
                    d = json.loads(row["result_json"] or "{}")
                    d["file_a"] = row["file_a"]
                    d["file_b"] = row["file_b"]
                    from artwork_report_engine import build_report_from_comparison
                    report = build_report_from_comparison(d)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            db.close()

    if report is None:
        if cache_id or comp_id:
            return render_template(
                "artwork_report.html",
                report=None,
                cache_id=cache_id or "",
                comp_id=comp_id or 0,
                username=session["username"],
                role=session["role"],
                expired=True,
            )
        from artwork_report_engine import build_demo_report
        report = build_demo_report()
    return render_template(
        "artwork_report.html",
        report=report,
        cache_id=cache_id or "",
        comp_id=comp_id or 0,
        username=session["username"],
        role=session["role"],
        expired=False,
    )


@app.route("/artwork/report/export-html")
@login_required
def artwork_report_export_html():
    """Pobiera raport jako plik HTML."""
    from artwork_report_engine import build_demo_report, render_report_html
    cache_id = request.args.get("cache_id")
    comp_id  = request.args.get("id", type=int)
    report = _build_report_from_cache_or_db(cache_id, comp_id, user_id=session["user_id"], role=session["role"])
    if report is None:
        report = build_demo_report()
    html = render_report_html(report)
    safe_code = secure_filename(report.product_code or "artwork")
    filename = f"raport_{safe_code}_{report.analysis_date.replace('.', '')}.html"
    return (
        html,
        200,
        {
            "Content-Type": "text/html; charset=utf-8",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@app.route("/artwork/report/export-docx")
@login_required
def artwork_report_export_docx():
    """Pobiera raport jako plik Word (.docx)."""
    from artwork_report_engine import build_demo_report, export_to_docx
    cache_id = request.args.get("cache_id")
    comp_id  = request.args.get("id", type=int)
    report = _build_report_from_cache_or_db(cache_id, comp_id, user_id=session["user_id"], role=session["role"])
    if report is None:
        report = build_demo_report()
    docx_bytes = export_to_docx(report)
    safe_code = secure_filename(report.product_code or "artwork")
    filename = f"raport_{safe_code}_{report.analysis_date.replace('.', '')}.docx"
    return (
        docx_bytes,
        200,
        {
            "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@app.route("/artwork/report/export-pdf")
@login_required
def artwork_report_export_pdf():
    """Pobiera raport artworku jako PDF (ReportLab) z wycinkami i kodami."""
    from artwork_report_engine import build_demo_report, export_to_pdf
    cache_id = request.args.get("cache_id")
    comp_id  = request.args.get("id", type=int)
    report = _build_report_from_cache_or_db(cache_id, comp_id, user_id=session["user_id"], role=session["role"])
    if report is None:
        report = build_demo_report()
    pdf_bytes = export_to_pdf(report)
    safe_code = secure_filename(report.product_code or "artwork")
    filename = f"raport_{safe_code}_{report.analysis_date.replace('.', '')}.pdf"
    return (
        pdf_bytes,
        200,
        {
            "Content-Type": "application/pdf",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )



# ── Artwork: Batch ────────────────────────────────────────────────────────────

@app.route("/artwork/batch")
@login_required
def artwork_batch_page():
    return render_template("artwork_batch.html",
                           username=session["username"], role=session["role"])


@app.route("/api/artwork/batch/pair", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_batch_pair():
    """Wgrywa pliki i zwraca wykryte pary."""
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "Brak plików"}), 400

    import hashlib
    _MAX_FILE_BYTES = 50 * 1024 * 1024
    uid = session.get("user_id", 0)
    ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp"}
    saved = []
    seen_hashes: set = set()
    for f in files[:100]:
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename.lower())[1]
        if ext not in ALLOWED_EXT:
            continue
        data = f.read(_MAX_FILE_BYTES + 1)
        if len(data) > _MAX_FILE_BYTES:
            continue
        # Walidacja zawartości: dla PDF wymagamy magic %PDF + brak hasła —
        # samo rozszerzenie .pdf nie wystarcza (można podrzucić nie-PDF do uploads/).
        # Obrazy (jpg/png/tiff/...) są dozwolone i nie mają nagłówka %PDF.
        if ext == ".pdf" and (not data.startswith(b"%PDF") or b"/Encrypt" in data[-4096:]):
            continue
        md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
        if md5 in seen_hashes:
            continue
        seen_hashes.add(md5)
        safe_name = secure_filename(f.filename) or "file"
        dest_a = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_aw_a_{safe_name}")
        dest_b = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_aw_b_{safe_name}")
        with open(dest_a, "wb") as fh:
            fh.write(data)
        import shutil; shutil.copy2(dest_a, dest_b)
        saved.append(f.filename)

    from artwork_batch_processor import auto_pair_artwork_files
    pairs = auto_pair_artwork_files(saved)
    return jsonify({"pairs": pairs, "total_files": len(saved)})


def _artwork_batch_worker(job_id, pairs, upload_dir, uid, use_ai, ai_model="claude-sonnet-4-6"):
    """Background thread: processes artwork batch and writes results to DB."""
    import uuid as _uuid
    from artwork_batch_processor import run_artwork_batch
    from artwork_comparator import set_ai_model
    set_ai_model(ai_model)
    results = []
    try:
        _db_start = get_db()
        try:
            _db_start.execute(
                "UPDATE artwork_batch_jobs SET status='running', updated_at=datetime('now') WHERE id=?",
                (job_id,)
            )
            _db_start.commit()
        finally:
            _db_start.close()

        for result in run_artwork_batch(pairs, upload_dir, uid, use_ai=use_ai, heavy_sem=_HEAVY_SEM):
            # Check for cancellation between pairs
            _db_chk = get_db()
            try:
                _job_status = _db_chk.execute(
                    "SELECT status FROM artwork_batch_jobs WHERE id=?", (job_id,)
                ).fetchone()
            finally:
                _db_chk.close()
            if _job_status and _job_status["status"] == "cancelled":
                return

            report_obj = result.pop("report_obj", None)
            result.pop("docx_bytes", None)
            cmp_data   = result.pop("cmp_data", None)

            # Cache report object for fast in-session access
            if report_obj:
                # Oznacz właściciela — bez tego raport z cache (owner_id None) omijał
                # kontrolę własności w artwork_report_page (IDOR po cache_id).
                try:
                    if isinstance(report_obj, dict):
                        report_obj["_owner_id"] = uid
                    else:
                        setattr(report_obj, "_owner_id", uid)
                except Exception:
                    pass
                cache_id = _uuid.uuid4().hex[:16]
                with _rpt_cache_lock:
                    _rpt_cache = getattr(app, "_artwork_report_cache", {})
                    _rpt_cache[cache_id] = report_obj
                    if len(_rpt_cache) > 200:
                        del _rpt_cache[next(iter(_rpt_cache))]
                    app._artwork_report_cache = _rpt_cache
                result["cache_id"] = cache_id

            # Persist to comparisons table so report survives session expiry
            if result.get("status") == "done" and cmp_data:
                try:
                    cmp_data["cache_id"] = result.get("cache_id")
                    cmp_data["status"]   = result.get("risk", "ok")
                    cmp_data["diff_count"] = result.get("critical", 0) + result.get("warnings", 0)
                    cid = _save_comparison(
                        uid, "artwork",
                        result["file_a"], result["file_b"],
                        cmp_data
                    )
                    result["db_id"] = cid
                    result["db_save_failed"] = False
                except Exception as _se:
                    logger.warning(f"Could not save batch result to DB: {_se}")
                    result["db_save_failed"] = True

            results.append(result)
            failed = sum(1 for r in results if r.get("status") == "error")
            import gzip as _gz, base64 as _b64
            _raw = json.dumps(results, ensure_ascii=False, default=str).encode()
            _compressed = "gz:" + _b64.b64encode(_gz.compress(_raw, compresslevel=6)).decode()
            _db_upd = get_db()
            try:
                _db_upd.execute(
                    """UPDATE artwork_batch_jobs
                       SET done=?, failed=?, results_json=?, updated_at=datetime('now')
                       WHERE id=?""",
                    (len(results), failed, _compressed, job_id)
                )
                _db_upd.commit()
            finally:
                _db_upd.close()

        _db_done = get_db()
        try:
            _db_done.execute(
                "UPDATE artwork_batch_jobs SET status='done', updated_at=datetime('now') WHERE id=?",
                (job_id,)
            )
            _db_done.commit()
        finally:
            _db_done.close()
    except Exception as e:
        logger.error(f"_artwork_batch_worker job {job_id} failed: {e}")
        try:
            _db_err = get_db()
            try:
                _db_err.execute(
                    "UPDATE artwork_batch_jobs SET status='failed', updated_at=datetime('now') WHERE id=?",
                    (job_id,)
                )
                _db_err.commit()
            finally:
                _db_err.close()
        except Exception:
            pass


@app.route("/api/artwork/batch/start", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_batch_start():
    """Tworzy asynchroniczne zadanie batch i uruchamia je w tle."""
    import threading
    pairs_raw = request.form.get("pairs", "[]")
    use_ai = request.form.get("use_ai", "1") == "1"
    from artwork_comparator import AVAILABLE_MODELS
    ai_model = request.form.get("ai_model", "claude-sonnet-4-6")
    _valid_batch_models = {m[0] for m in AVAILABLE_MODELS}
    if ai_model not in _valid_batch_models:
        return jsonify({"error": f"Nieznany model AI: {ai_model}"}), 400
    if len(pairs_raw) > 500_000:
        return jsonify({"error": "Za duże dane wejściowe"}), 400
    try:
        pairs = json.loads(pairs_raw)
    except Exception:
        return jsonify({"error": "Błąd parsowania par"}), 400
    if not pairs:
        return jsonify({"error": "Brak par do porównania"}), 400
    if len(pairs) > 500:
        return jsonify({"error": "Zbyt wiele par — limit wynosi 500"}), 400

    uid = session.get("user_id", 0)
    upload_dir = app.config["UPLOAD_FOLDER"]

    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO artwork_batch_jobs(user_id, status, total, pairs_json) VALUES(?,?,?,?)",
            (uid, "pending", len(pairs), json.dumps(pairs))
        )
        db.commit()
        job_id = cur.lastrowid
    finally:
        db.close()

    # Poziom 1 kolejkowania: jeśli REDIS_URL jest ustawiony, zadanie liczy osobny
    # worker (web wolny). Bez Redis — fallback na wątek (dotychczasowe zachowanie).
    # Kontrakt postępu (artwork_batch_jobs) bez zmian. Patrz docs/KOLEJKOWANIE.md.
    import jobs as _jobs
    _jobs.enqueue(_artwork_batch_worker, job_id, pairs, upload_dir, uid, use_ai, ai_model)

    return jsonify({"job_id": job_id, "total": len(pairs)})


@app.route("/api/artwork/batch/status/<int:job_id>", methods=["GET"])
@login_required
def api_artwork_batch_status(job_id):
    """Zwraca aktualny stan zadania batch."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT status, total, done, failed, results_json FROM artwork_batch_jobs WHERE id=? AND user_id=?",
            (job_id, session["user_id"])
        ).fetchone()
    finally:
        db.close()
    if not row:
        return jsonify({"error": "Nie znaleziono zadania"}), 404
    _rj = row["results_json"] or "[]"
    try:
        if _rj.startswith("gz:"):
            import gzip as _gz, base64 as _b64
            _rj = _gz.decompress(_b64.b64decode(_rj[3:])).decode()
        results = json.loads(_rj)
    except Exception:
        results = []
    import hashlib as _hl2
    checksum = _hl2.sha256(_rj.encode()).hexdigest()[:16] if row["status"] in ("done", "failed") else None
    return jsonify({
        "status": row["status"],
        "total": row["total"],
        "done": row["done"],
        "failed": row["failed"],
        "last_result": results[-1] if results else None,
        "results": results if row["status"] in ("done", "failed") else [],
        "checksum": checksum,
    })


@app.route("/api/artwork/batch/cancel/<int:job_id>", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_batch_cancel(job_id):
    """Anuluje zadanie batch — worker zatrzymuje się po bieżącej parze."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT status FROM artwork_batch_jobs WHERE id=? AND user_id=?",
            (job_id, session["user_id"])
        ).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono zadania"}), 404
        if row["status"] not in ("pending", "running"):
            return jsonify({"error": "Zadanie już zakończone"}), 400
        db.execute(
            "UPDATE artwork_batch_jobs SET status='cancelled', updated_at=datetime('now') WHERE id=?",
            (job_id,)
        )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/batch/run", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_batch_run():
    """Uruchamia batch porównanie artworków. Streamuje NDJSON."""
    import json as _json

    pairs_raw = request.form.get("pairs", "[]")
    use_ai    = request.form.get("use_ai", "1") == "1"
    if len(pairs_raw) > 500_000:
        return jsonify({"error": "Za duże dane wejściowe"}), 400
    try:
        pairs = _json.loads(pairs_raw)
    except Exception:
        return jsonify({"error": "Błąd parsowania par"}), 400
    if len(pairs) > 500:
        return jsonify({"error": "Zbyt wiele par — limit wynosi 500"}), 400

    uid = session.get("user_id", 0)
    upload_dir = app.config["UPLOAD_FOLDER"]

    from artwork_batch_processor import run_artwork_batch

    def generate():
        for result in run_artwork_batch(pairs, upload_dir, uid, use_ai=use_ai, heavy_sem=_HEAVY_SEM):
            # Cache the pre-built report for individual report view + docx export
            report_obj = result.pop("report_obj", None)
            result.pop("docx_bytes", None)
            if report_obj:
                import uuid
                cache_id = str(uuid.uuid4()).replace("-", "")[:16]
                with _rpt_cache_lock:
                    _rpt_cache = getattr(app, "_artwork_report_cache", {})
                    _rpt_cache[cache_id] = report_obj
                    if len(_rpt_cache) > 200:
                        del _rpt_cache[next(iter(_rpt_cache))]
                    app._artwork_report_cache = _rpt_cache
                result["cache_id"] = cache_id
            yield _json.dumps(result, ensure_ascii=False, default=str) + "\n"

    return app.response_class(generate(), mimetype="application/x-ndjson")


@app.route("/api/artwork/batch/zip", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_batch_zip():
    """Pobiera ZIP ze wszystkimi raportami Word batch."""
    import json as _json
    results_raw = request.form.get("results", "[]")
    if len(results_raw) > 500_000:
        return jsonify({"error": "Za duże dane wejściowe"}), 400
    try:
        results_meta = _json.loads(results_raw)
    except Exception:
        return jsonify({"error": "Błąd parsowania"}), 400
    if len(results_meta) > 500:
        return jsonify({"error": "Zbyt wiele wyników — limit wynosi 500"}), 400

    with _rpt_cache_lock:
        report_cache = dict(getattr(app, "_artwork_report_cache", {}))
    results_full = []
    for meta in results_meta:
        cid = meta.get("cache_id")
        report = report_cache.get(cid) if cid else None
        results_full.append({**meta, "report_obj": report})

    from artwork_batch_processor import build_artwork_batch_zip
    from artwork_report_engine import export_to_docx

    # Generate docx for each
    for r in results_full:
        if r.get("report_obj") and not r.get("docx_bytes"):
            try:
                r["docx_bytes"] = export_to_docx(r["report_obj"])
            except Exception:
                pass

    zip_bytes = build_artwork_batch_zip(results_full)
    return (
        zip_bytes, 200,
        {
            "Content-Type": "application/zip",
            "Content-Disposition": "attachment; filename=raporty_artworkow.zip",
        },
    )


# ── Artwork: manualne zaznaczanie stref ───────────────────────────────────────

@app.route("/artwork/zone-select")
@login_required
def artwork_zone_select():
    """Interaktywny selektor stref do manualnego porównania regionów artworku."""
    cache_id = request.args.get("cache_id", "")
    result   = _cache_get(cache_id)
    if not result:
        flash("Brak danych porównania — najpierw wykonaj porównanie artworków.", "error")
        return redirect(url_for("artwork_compare_page"))

    page0    = result.page_diffs[0] if result.page_diffs else None
    img_a_b64 = page0.img_a_b64 if page0 else ""
    img_b_b64 = page0.img_b_b64 if page0 else ""

    from artwork_zone_comparator import list_zone_templates
    templates = list_zone_templates()

    return render_template(
        "artwork_zone_select.html",
        cache_id  = cache_id,
        file_a    = result.file_a,
        file_b    = result.file_b,
        img_a_b64 = img_a_b64 or "",
        img_b_b64 = img_b_b64 or "",
        templates = templates,
    )


@app.route("/api/artwork/zone-compare", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_zone_compare():
    """Porównuje ręcznie zaznaczone pary stref na artworkach."""
    data      = request.get_json(silent=True) or {}
    cache_id  = data.get("cache_id", "")
    zones     = data.get("zones", [])
    save_tpl  = data.get("save_template", False)
    tpl_name  = str(data.get("template_name") or "").strip()[:200]

    result = _cache_get(cache_id)
    if not result:
        return jsonify({"error": "Brak danych porównania w cache"}), 404
    if not zones:
        return jsonify({"error": "Brak zdefiniowanych stref"}), 400

    page0     = result.page_diffs[0] if result.page_diffs else None
    img_a_b64 = page0.img_a_b64 if page0 else ""
    img_b_b64 = page0.img_b_b64 if page0 else ""

    from artwork_zone_comparator import compare_zone_pairs, save_zone_template
    results = compare_zone_pairs(img_a_b64, img_b_b64, zones)

    if save_tpl and tpl_name:
        try:
            save_zone_template(tpl_name, zones, result.file_a, result.file_b)
        except Exception:
            pass

    return jsonify({"ok": True, "results": results})


@app.route("/api/artwork/field-queue/init", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_field_queue_init():
    """
    Initialise a field comparison queue for one artwork pair.
    Accepts same FormData as split-compare (supplier_pdf + master_configs_json
    + pairs_json), but only processes the FIRST pair.
    Returns {queue_id, fields: [{field_name, display_name, crop_a_b64, ...}]}
    """
    import traceback as _tb
    supplier_path = None
    comp_path = None
    master_path_val = None
    is_temp_master = True

    def _cleanup_temps():
        for _p in [supplier_path, comp_path,
                   master_path_val if is_temp_master else None]:
            try:
                if _p and os.path.exists(_p):
                    os.remove(_p)
            except Exception:
                pass

    try:
        supplier_file = request.files.get("supplier_pdf")
        master_configs_str = request.form.get("master_configs_json", "")
        pairs_json_str = request.form.get("pairs_json", "")
        masters_files = request.files.getlist("masters[]")

        if not supplier_file:
            return jsonify({"error": "Brak pliku dostawcy"}), 400

        uid = session["user_id"]
        upload_dir = app.config["UPLOAD_FOLDER"]
        os.makedirs(upload_dir, exist_ok=True)

        supplier_path = os.path.join(upload_dir,
                                     f"{uid}_fq_sup_{secure_filename(supplier_file.filename) or 'supplier.pdf'}")
        supplier_file.save(supplier_path)

        # Resolve master
        profile_id = None
        if master_configs_str:
            try:
                configs = json.loads(master_configs_str)
            except Exception:
                return jsonify({"error": "Błąd parsowania master_configs_json"}), 400
            cfg = configs[0] if configs else {}
            if cfg.get("type") == "profile":
                profile_id = cfg.get("profile_id")
                if not profile_id:
                    _cleanup_temps()
                    return jsonify({"error": "Brakujący profile_id w konfiguracji mastera"}), 400
                db2 = get_db()
                try:
                    row = db2.execute(
                        "SELECT name, master_pdf_path FROM artwork_profiles WHERE id=?",
                        (profile_id,)).fetchone()
                    if not row or not row["master_pdf_path"]:
                        _cleanup_temps()
                        return jsonify({"error": "Master z biblioteki nie ma pliku PDF"}), 404
                    master_path_val = _resolve_master_path(row["master_pdf_path"],
                                                           profile_id=profile_id, db=db2)
                    is_temp_master = False
                finally:
                    db2.close()
            else:
                fi = cfg.get("file_idx", 0)
                try:
                    fi = int(fi)
                except (TypeError, ValueError):
                    fi = 0
                if 0 <= fi < len(masters_files):
                    mf = masters_files[fi]
                    master_path_val = os.path.join(upload_dir,
                                                   f"{uid}_fq_mst_{secure_filename(mf.filename or '') or 'master.pdf'}")
                    mf.save(master_path_val)
        elif masters_files:
            mf = masters_files[0]
            master_path_val = os.path.join(upload_dir,
                                           f"{uid}_fq_mst_{secure_filename(mf.filename or '') or 'master.pdf'}")
            mf.save(master_path_val)

        if not master_path_val or not os.path.exists(master_path_val):
            _cleanup_temps()
            return jsonify({"error": "Nie znaleziono pliku mastera"}), 404

        # Resolve supplier page
        page_a = 0
        page_b = 0
        if pairs_json_str:
            try:
                pairs = json.loads(pairs_json_str)
            except Exception:
                pairs = []
            if pairs:
                try:
                    page_b = max(0, int(pairs[0].get("page", 0)))
                except (TypeError, ValueError):
                    page_b = 0

        # Extract supplier page as separate PDF
        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(supplier_path)
        num_pages = len(reader.pages)
        if page_b >= num_pages:
            _cleanup_temps()
            return jsonify({"error": f"Strona {page_b} nie istnieje w pliku dostawcy ({num_pages} stron)"}), 400
        writer = PdfWriter()
        writer.add_page(reader.pages[page_b])
        comp_path = os.path.join(upload_dir, f"{uid}_fq_pg{page_b}.pdf")
        with open(comp_path, "wb") as fp:
            writer.write(fp)

        # Load profile
        from artwork_comparator import (field_queue_init, _load_artwork_profile_by_id)
        profile = _load_artwork_profile_by_id(profile_id) if profile_id else None
        if not profile or not profile.get("fields"):
            _cleanup_temps()
            return jsonify({"error": "Profil nie ma zdefiniowanych pól"}), 400

        # Pull effective per-field supplier coords for pair 0 (auto-located + manual)
        field_overrides_str = request.form.get("field_overrides_json", "")
        try:
            field_overrides_all = json.loads(field_overrides_str) if field_overrides_str else {}
        except Exception:
            field_overrides_all = {}
        field_overrides = (field_overrides_all.get("0")
                           or field_overrides_all.get(0)
                           or {})

        _temp_files = [supplier_path, comp_path,
                       master_path_val if is_temp_master else None]
        import time as _time
        _t0 = _time.monotonic()
        _fq_db = get_db()
        try:
            _ensure_artwork_profile_tables(_fq_db)
            _fq_stats = _load_artwork_field_stats(_fq_db)
        finally:
            _fq_db.close()
        result = field_queue_init(master_path_val, comp_path, profile,
                                  page_a=page_a, page_b=0,
                                  field_overrides=field_overrides,
                                  temp_files=_temp_files,
                                  field_stats=_fq_stats)
        _dur = int((_time.monotonic() - _t0) * 1000)
        if "error" in result:
            for p in _temp_files:
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            return jsonify(result), 400

        _log_audit("field_queue_init",
                   detail=f"profil={profile.get('name','')} pól={len(result.get('fields',[]))}",
                   duration_ms=_dur,
                   extra={"profile_id": profile_id,
                          "profile_name": profile.get("name"),
                          "field_count": len(result.get("fields", [])),
                          "queue_id": result.get("queue_id"),
                          "duration_ms": _dur})
        result["profile_name"] = profile.get("name", "")
        return jsonify(result)

    except Exception as exc:
        logger.error("field_queue_init error: %s\n%s", exc, _tb.format_exc())
        _cleanup_temps()
        return jsonify({"error": "Błąd inicjalizacji kolejki pól — spróbuj ponownie."}), 500


@app.route("/api/artwork/field-queue/compare", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_field_queue_compare():
    """
    Compare a single field from an initialised queue.
    Body: {queue_id, field_name, override_b: {x1_pct,y1_pct,x2_pct,y2_pct}|null}
    Returns the comparison row dict.
    """
    field_name = None
    try:
        data = request.get_json(force=True) or {}
        queue_id = data.get("queue_id")
        field_name = data.get("field_name")
        override_b = data.get("override_b")  # coords or null

        if not queue_id or not field_name:
            return jsonify({"error": "queue_id i field_name są wymagane"}), 400

        import time as _time
        _t0 = _time.monotonic()
        from artwork_comparator import field_queue_compare_one
        result = field_queue_compare_one(queue_id, field_name, override_b)
        _dur = int((_time.monotonic() - _t0) * 1000)
        if isinstance(result, dict) and "error" in result:
            return jsonify(result), 400
        _log_audit("field_compare",
                   detail=f"{field_name}: {'RÓŻNICA' if result.get('changed') else 'OK'}",
                   duration_ms=_dur,
                   extra={"field": field_name,
                          "changed": result.get("changed"),
                          "severity": result.get("severity"),
                          "val_a": str(result.get("val_a",""))[:80],
                          "val_b": str(result.get("val_b",""))[:80],
                          "duration_ms": _dur})
        return jsonify(result)
    except Exception as exc:
        logger.error("field_queue_compare error: %s", exc)
        _log_audit("field_compare_error", detail=f"{field_name}: {str(exc)[:150]}")
        return jsonify({"error": "Błąd porównania pola — spróbuj ponownie."}), 500


@app.route("/api/artwork/field-queue/<queue_id>", methods=["DELETE"])
@login_required
@csrf_protect
def api_artwork_field_queue_destroy(queue_id):
    """Release queue cache when operator is done."""
    from artwork_comparator import field_queue_destroy
    field_queue_destroy(queue_id)
    return jsonify({"ok": True})


@app.route("/api/artwork/field-queue/finalize", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_field_queue_finalize():
    """Build + save the full report from the queue's ALREADY-computed field rows,
    without re-running the comparison. Body: {queue_id, fields:[row…], file_a, file_b}.
    Returns {cache_id} → frontend opens /artwork/report?cache_id=…"""
    uid = session["user_id"]
    data = request.get_json(force=True) or {}
    queue_id = data.get("queue_id")
    field_rows = data.get("fields") or []
    file_a = (data.get("file_a") or "").strip()
    file_b = (data.get("file_b") or "").strip()
    if not queue_id:
        return jsonify({"error": "queue_id wymagany"}), 400
    try:
        from artwork_comparator import field_queue_finalize
        result = field_queue_finalize(queue_id, field_rows, file_a=file_a, file_b=file_b)
    except Exception:
        logger.exception("field_queue_finalize error queue=%s", queue_id)
        return jsonify({"error": "Błąd budowy raportu z kolejki."}), 500
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 400
    import hashlib as _hl, time as _t
    cache_id = _hl.sha256(f"{uid}_{_t.time()}_fqf".encode()).hexdigest()[:12]
    _cache_put(cache_id, result, owner_id=uid)
    try:
        full = result.to_dict(include_images=False)
        full["cache_id"] = cache_id
        full["status"] = result.risk_level
        full["diff_count"] = result.critical_count + result.important_count
        _save_comparison(uid, "artwork", file_a or "wzorzec", file_b or "dostawca", full)
    except Exception:
        logger.warning("finalize save_comparison failed queue=%s", queue_id, exc_info=True)
    _log_audit("artwork_field_queue_finalize", session.get("username"),
               f"queue={queue_id} crit={result.critical_count} imp={result.important_count}")
    return jsonify({"cache_id": cache_id, "risk_level": result.risk_level,
                    "critical_count": result.critical_count,
                    "important_count": result.important_count,
                    "ok_count": result.ok_count})


@app.route("/api/artwork/zone-templates", methods=["GET", "DELETE"])
@login_required
@csrf_protect
def api_artwork_zone_templates():
    """Zarządza zapisanymi szablonami stref."""
    from artwork_zone_comparator import list_zone_templates, delete_zone_template
    if request.method == "DELETE":
        if role_level(session.get("role", "user")) < role_level("manager"):
            return jsonify({"error": "Brak uprawnień"}), 403
        tid = request.args.get("id", "")
        ok  = delete_zone_template(tid)
        return jsonify({"ok": ok})
    return jsonify({"templates": list_zone_templates()})



@app.route("/api/ai/status")
@login_required
def ai_status():
    try:
        from ai_validator import is_api_key_set
        return jsonify(is_api_key_set())
    except Exception as e:
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"ok": False, "error": "Błąd serwera"}), 500


@app.route("/api/ai/config", methods=["GET", "POST"])
@require_role("admin")
@csrf_protect
def ai_config():
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        api_key = str(data.get("api_key") or "").strip()
        if not api_key or not api_key.startswith("sk-ant-") or len(api_key) > 500:
            return jsonify({"error": "Nieprawidłowy klucz"}), 400
        db = get_db()
        try:
            db.execute(
                "INSERT INTO settings(category,key,value) VALUES(?,?,?) "
                "ON CONFLICT(category,key) DO UPDATE SET value=EXCLUDED.value",
                ("ai", "anthropic_api_key", api_key))
            db.commit()
        finally:
            db.close()
        os.environ["ANTHROPIC_API_KEY"] = api_key
        return jsonify({"ok": True})
    key_env = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    return jsonify({"api_key_set": key_env, "source": "env" if key_env else "none",
                    "model": "claude-sonnet-4-20250514"})


@app.route("/api/ai/budget", methods=["GET", "POST"])
@login_required
@csrf_protect
def api_ai_budget():
    """Miesięczny budżet AI: GET zwraca status (wydane/limit/%/flagi),
    POST (admin) ustawia limit USD (0 = brak limitu)."""
    from api_usage_tracker import get_budget_status, set_budget_limit
    if request.method == "POST":
        if session.get("role") != "admin":
            return jsonify({"error": "Tylko administrator może zmienić limit"}), 403
        data = request.get_json(silent=True) or {}
        try:
            limit = max(0.0, float(data.get("limit_usd", 0)))
        except (TypeError, ValueError):
            return jsonify({"error": "Nieprawidłowa kwota"}), 400
        if limit > 100000:
            return jsonify({"error": "Kwota zbyt duża"}), 400
        set_budget_limit(limit)
        _log_audit("ai_budget_set", session.get("username"), f"limit={limit:.2f} USD/mc")
        return jsonify({"ok": True, **get_budget_status()})
    return jsonify(get_budget_status())


@app.route("/api/ai/learn-document", methods=["POST"])
@login_required
@csrf_protect
def ai_learn_document():
    f = request.files.get("file")
    _pdf_err = _validate_pdf_upload(f)
    if _pdf_err:
        return jsonify({"error": _pdf_err}), 400
    uid = session["user_id"]
    if _check_and_record_api_rate(uid):
        return jsonify({"error": "Przekroczono limit zapytań. Poczekaj chwilę."}), 429
    _sname = secure_filename(f.filename) or "document.pdf"
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    path = os.path.join(app.config["UPLOAD_FOLDER"],
                        f"{uid}_learn_{_sname}")
    f.save(path)
    try:
        from ai_learning import learn_from_document
        _learn_doc_type = request.form.get("doc_type", "auto")
        if _learn_doc_type not in ("auto", "PO", "PI", "CI", "PL", "SAD", "BL", "WZ", "FV"):
            _learn_doc_type = "auto"
        return jsonify(learn_from_document(path, _learn_doc_type))
    except Exception as e:
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500
    finally:
        try: os.remove(path)
        except Exception: pass


@app.route("/api/ai/apply-profile", methods=["POST"])
@require_role("manager")
@csrf_protect
def ai_apply_profile():
    data = request.get_json(force=True, silent=True) or {}
    code = str(data.get("supplier_code") or "").upper()
    proposal = data.get("proposal")
    if not code or not proposal:
        return jsonify({"error": "Wymagane: supplier_code, proposal"}), 400
    try:
        from ai_learning import apply_learned_profile
        return jsonify(apply_learned_profile(code, proposal,
                       approved_by=session.get("username", "unknown")))
    except Exception as e:
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500


@app.route("/api/ai/learn-comparison/<int:cid>", methods=["POST"])
@login_required
@csrf_protect
def ai_learn_comparison(cid):
    uid = session["user_id"]
    role = session.get("role", "user")
    db = get_db()
    try:
        row = db.execute("SELECT * FROM comparisons WHERE id=?", (cid,)).fetchone()
    finally:
        db.close()
    if not row:
        return jsonify({"error": "Nie znaleziono porównania"}), 404
    if row["user_id"] != uid and not can_see_all(role):
        return jsonify({"error": "Brak dostępu"}), 403
    try:
        cr = json.loads(row["result_json"] or "{}")
    except (ValueError, TypeError):
        return jsonify({"error": "Dane porównania uszkodzone"}), 500
    sc = row["supplier_code"] or cr.get("supplier_detected", "")
    if not sc:
        return jsonify({"status": "no_supplier", "message": "Brak kodu dostawcy"}), 200
    try:
        from ai_learning import learn_from_comparison
        return jsonify(learn_from_comparison(cr, sc))
    except Exception as e:
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500


@app.route("/api/ai/apply-suggestions", methods=["POST"])
@require_role("manager")
@csrf_protect
def ai_apply_suggestions():
    data = request.get_json(force=True, silent=True) or {}
    sc = str(data.get("supplier_code") or "").upper()
    suggestions = data.get("suggestions", {})
    approved = data.get("approved_fields", None)
    if not sc or not suggestions:
        return jsonify({"error": "Wymagane: supplier_code, suggestions"}), 400
    try:
        from ai_learning import apply_comparison_suggestions
        return jsonify(apply_comparison_suggestions(sc, suggestions,
                       approved_fields=approved,
                       approved_by=session.get("username", "unknown")))
    except Exception as e:
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500


@app.route("/api/ai/pending-updates")
@require_role("manager")
def ai_pending_updates():
    try:
        from ai_learning import get_pending_updates
        return jsonify(get_pending_updates())
    except Exception as e:
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500


@app.route("/api/ai/pending-updates/<pending_id>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def ai_dismiss_pending(pending_id):
    try:
        from ai_learning import dismiss_pending
        return jsonify({"ok": dismiss_pending(pending_id)})
    except Exception as e:
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500


@app.route("/api/ai/learning-stats")
@login_required
def ai_learning_stats():
    try:
        from ai_learning import get_learning_stats
        return jsonify(get_learning_stats())
    except Exception as e:
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500


def _trigger_background_learning(result_dict: dict):
    supplier = result_dict.get("supplier_detected", "")
    if not supplier or result_dict.get("total_errors", 0) == 0:
        return
    try:
        import threading
        from ai_learning import learn_from_comparison
        threading.Thread(target=lambda: learn_from_comparison(result_dict, supplier),
                         daemon=True).start()
    except Exception:
        pass



# ─────────────────────────────────────────────────────────────────────────────
# KOMENTARZE I AKCEPTACJA RAPORTÓW
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/comparison/<int:cid>/comment", methods=["POST"])
@login_required
@csrf_protect
def add_comment(cid):
    uid = session["user_id"]
    role = session.get("role", "user")
    data = request.get_json(silent=True) or {}
    comment = str(data.get("comment") or "").strip()[:2000]
    ctype = (data.get("type") or "note")
    if ctype not in ("note", "issue", "resolved"):
        ctype = "note"
    if not comment:
        return jsonify({"error": "Brak treści komentarza"}), 400
    db = get_db()
    try:
        row = db.execute("SELECT id, user_id FROM comparisons WHERE id=?", (cid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        if not can_see_all(role) and row["user_id"] != uid:
            return jsonify({"error": "Brak dostępu"}), 403
        _cur = db.execute(
            "INSERT INTO comparison_comments(comparison_id,user_id,comment,comment_type) VALUES(?,?,?,?)",
            (cid, uid, comment, ctype)
        )
        db.commit()
        # Zwróć komentarz z username
        new_id = _cur.lastrowid
        row2 = db.execute("""
            SELECT cc.*, u.username
            FROM comparison_comments cc JOIN users u ON cc.user_id=u.id
            WHERE cc.id=?
        """, (new_id,)).fetchone()
    finally:
        db.close()
    return jsonify({"ok": True, "comment": dict(row2) if row2 else {}})


@app.route("/api/comparison/<int:cid>/comments")
@login_required
def get_comments(cid):
    uid = session["user_id"]
    role = session["role"]
    db = get_db()
    try:
        row = db.execute("SELECT user_id FROM comparisons WHERE id=?", (cid,)).fetchone()
        if not row:
            return jsonify([])
        if not can_see_all(role) and row["user_id"] != uid:
            return jsonify({"error": "Brak dostępu"}), 403
        rows = db.execute("""
            SELECT cc.*, u.username
            FROM comparison_comments cc JOIN users u ON cc.user_id=u.id
            WHERE cc.comparison_id=?
            ORDER BY cc.created_at ASC
        """, (cid,)).fetchall()
    finally:
        db.close()
    return jsonify([dict(r) for r in rows])


# ─────────────────────────────────────────────────────────────────────────────
# WYSZUKIWANIE PO NUMERZE PO
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/search")
@login_required
def api_search():
    q = request.args.get("q", "").strip()[:200]
    uid = session["user_id"]
    role = session["role"]
    if not q or len(q) < 3:
        return jsonify([])
    db = get_db()
    q_esc = escape_like(q)
    pattern = f"%{q_esc}%"
    try:
        if can_see_all(role):
            rows = db.execute("""
                SELECT c.*, u.username
                FROM comparisons c JOIN users u ON c.user_id=u.id
                WHERE c.po_number LIKE ? ESCAPE '\\'
                   OR c.file_a LIKE ? ESCAPE '\\' OR c.file_b LIKE ? ESCAPE '\\'
                   OR c.supplier_code LIKE ? ESCAPE '\\'
                ORDER BY c.created_at DESC LIMIT 30
            """, (pattern, pattern, pattern, pattern)).fetchall()
        else:
            rows = db.execute("""
                SELECT c.*, u.username
                FROM comparisons c JOIN users u ON c.user_id=u.id
                WHERE c.user_id=? AND (
                    c.po_number LIKE ? ESCAPE '\\'
                    OR c.file_a LIKE ? ESCAPE '\\' OR c.file_b LIKE ? ESCAPE '\\'
                    OR c.supplier_code LIKE ? ESCAPE '\\')
                ORDER BY c.created_at DESC LIMIT 30
            """, (uid, pattern, pattern, pattern, pattern)).fetchall()
    finally:
        db.close()
    result = []
    for r in rows:
        d = dict(r)
        d.pop("result_json", None)  # nie zwracaj pełnego raportu w wyszukiwaniu
        result.append(d)
    return jsonify(result)


# ─────────────────────────────────────────────────────────────────────────────
# WALIDACJA SUM KONTROLNYCH PER POZYCJA
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/validate_line_sums", methods=["POST"])
@login_required
@csrf_protect
def validate_line_sums():
    """
    Sprawdza czy qty × price = net dla każdej pozycji.
    Przyjmuje items z raportu.
    """
    data = request.get_json(silent=True) or {}
    items = data.get("items", [])
    results = []
    for it in items:
        ref = it.get("ref", "?")
        for side in ("a", "b"):
            qty_s = str(it.get(f"qty_{side}", "") or "").replace(",", "")
            price_s = str(it.get(f"price_{side}", "") or "").lstrip("$").replace(",", "")
            net_s = str(it.get(f"net_{side}", "") or "").lstrip("$").replace(",", "")
            try:
                qty = float(qty_s)
                price = float(price_s)
                net = float(net_s)
                if qty <= 0 or price <= 0 or net <= 0:
                    continue
                calculated = round(qty * price, 2)
                declared = round(net, 2)
                diff = abs(calculated - declared)
                diff_pct = diff / declared * 100 if declared else 0
                if diff_pct > 0.5:
                    results.append({
                        "ref": ref,
                        "side": side.upper(),
                        "qty": qty,
                        "price": price,
                        "net_declared": declared,
                        "net_calculated": calculated,
                        "diff": round(diff, 4),
                        "diff_pct": round(diff_pct, 2),
                        "severity": "error" if diff_pct > 2 else "warning",
                    })
            except (ValueError, TypeError):
                continue
    return jsonify({"issues": results, "ok": len(results) == 0})


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL — konfiguracja i wysyłka powiadomień
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/email/config", methods=["GET", "POST"])
@require_role("admin")
@csrf_protect
def email_config():
    """Konfiguracja SMTP dla powiadomień email."""
    db = get_db()
    try:
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            _email_max_lens = {"smtp_host": 255, "smtp_port": 10, "smtp_user": 254,
                               "smtp_pass": 500, "smtp_from": 254, "notify_on_error": 10,
                               "notify_recipients": 2000}
            for key, val in data.items():
                if key in _email_max_lens:
                    db.execute(
                        "INSERT INTO settings(category,key,value) VALUES('email',?,?) "
                        "ON CONFLICT(category,key) DO UPDATE SET value=EXCLUDED.value",
                        (key, str(val)[:_email_max_lens[key]])
                    )
            db.commit()
            return jsonify({"ok": True})
        # GET
        rows = db.execute("SELECT key,value FROM settings WHERE category='email'").fetchall()
    finally:
        db.close()
    cfg = {r["key"]: r["value"] for r in rows}
    cfg.pop("smtp_pass", None)  # nie zwracaj hasła
    return jsonify(cfg)


@app.route("/api/email/test", methods=["POST"])
@require_role("admin")
@csrf_protect
def email_test():
    """Wyślij testowy email."""
    db = get_db()
    try:
        rows = db.execute("SELECT key,value FROM settings WHERE category='email'").fetchall()
    finally:
        db.close()
    cfg = {r["key"]: r["value"] for r in rows}
    recipient = str((request.get_json(silent=True) or {}).get("recipient") or cfg.get("smtp_from") or "").strip()[:254]
    if not recipient:
        return jsonify({"error": "Brak adresu odbiorcy"}), 400
    try:
        _send_email(
            to=recipient,
            subject="DocCompare — test powiadomień",
            body="Jeśli widzisz tę wiadomość, konfiguracja SMTP działa poprawnie.",
            cfg=cfg
        )
        return jsonify({"ok": True, "sent_to": recipient})
    except Exception as e:
        logger.exception("Email send error: %s", e)
        return jsonify({"error": "Błąd wysyłki e-mail — sprawdź konfigurację SMTP."}), 400


@app.route("/settings/email")
@require_role("admin")
def page_email_settings():
    """Strona konfiguracji SMTP (nadawca powiadomień i wysyłki do dostawców)."""
    return render_template("email_settings.html",
                           username=session.get("username"), role=session.get("role"))


def _send_email(to: str, subject: str, body: str, cfg: dict = None):
    """Wysyła email przez SMTP."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    if not cfg:
        db = get_db()
        try:
            rows = db.execute("SELECT key,value FROM settings WHERE category='email'").fetchall()
        finally:
            db.close()
        cfg = {r["key"]: r["value"] for r in rows}

    host = cfg.get("smtp_host", "")
    if not host:
        raise ValueError("Brak konfiguracji SMTP. Skonfiguruj w Ustawieniach → Email.")

    try:
        port = int(cfg.get("smtp_port", 587))
    except (ValueError, TypeError):
        port = 587
    user = cfg.get("smtp_user", "")
    pwd  = cfg.get("smtp_pass", "")
    frm  = cfg.get("smtp_from", user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = frm
    msg["To"]      = to
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=10) as srv:
            srv.ehlo()
            if user and pwd:
                srv.login(user, pwd)
            srv.sendmail(frm, [to], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=10) as srv:
            srv.ehlo()
            if port != 25:
                srv.starttls()
            if user and pwd:
                srv.login(user, pwd)
            srv.sendmail(frm, [to], msg.as_string())


def _notify_errors(comparison_id: int, report: dict):
    """Wysyła powiadomienie email gdy wykryto błędy krytyczne."""
    db = get_db()
    try:
        rows = db.execute("SELECT key,value FROM settings WHERE category='email'").fetchall()
    finally:
        db.close()
    cfg = {r["key"]: r["value"] for r in rows}

    if str(cfg.get("notify_on_error") or "0") != "1":
        return
    recipients = str(cfg.get("notify_recipients") or "").strip()
    if not recipients:
        return

    total_errors = report.get("total_errors", 0)
    if total_errors == 0:
        return

    file_a = report.get("file_a", "?")
    file_b = report.get("file_b", "?")
    po = report.get("po_number", "")
    sup = report.get("supplier_name", "") or report.get("supplier_detected", "")

    # Zbierz krytyczne problemy
    crits = []
    mods = report.get("modules", {})
    for mod_name in ("enhanced", "table"):
        mod = mods.get(mod_name, {})
        for it in (mod.get("items") or []):
            if it.get("status") == "roznica":
                issues = "; ".join(it.get("issues") or [])
                crits.append(f"  • {it.get('ref','?')}: {issues}")
    crits_text = "\n".join(crits[:10]) or "  Brak szczegółów"

    body = f"""DocCompare wykrył {total_errors} błędów krytycznych.

Dokumenty:
  A: {file_a}
  B: {file_b}
  {'Dostawca: '+str(sup) if sup else ''}
  {'Numer PO: '+str(po) if po else ''}

Problemy:
{crits_text}

Otwórz raport: {os.environ.get('APP_BASE_URL', '').rstrip('/')}/history
Numer porównania: #{comparison_id}
"""

    for recipient in [r.strip() for r in recipients.split(",") if r.strip()]:
        try:
            _send_email(
                to=recipient,
                subject=f"DocCompare ⚠ {total_errors} błędów — {str(file_a)[:40]}",
                body=body,
                cfg=cfg
            )
        except Exception as e:
            app.logger.warning(f"Email notify failed for {recipient}: {e}")



# ═══════════════════════════════════════════════════════════════
# FUNKCJA 4 — PROGRESS BAR (async analiza)
# ═══════════════════════════════════════════════════════════════

_PROGRESS_ALLOWED_COLS = frozenset({
    'status', 'progress', 'message', 'result_id', 'error',
    'total_pages', 'current_page', 'items_found', 'step',
})

def _update_progress(job_id, **kwargs):
    """Aktualizuje stan zadania w DB."""
    from datetime import datetime, timezone as _dt_tz
    invalid = set(kwargs) - _PROGRESS_ALLOWED_COLS
    if invalid:
        raise ValueError(f"_update_progress: niedozwolone kolumny {invalid}")
    db = get_db()
    try:
        sets = ', '.join(f'{k}=?' for k in kwargs)
        vals = list(kwargs.values()) + [datetime.now(_dt_tz.utc).isoformat(), job_id]
        # bandit: kolumny sprawdzone wyżej względem _PROGRESS_ALLOWED_COLS; wartości przez ?
        db.execute(f'UPDATE job_progress SET {sets}, updated_at=? WHERE job_id=?', vals)  # nosec B608
        db.commit()
    finally:
        db.close()


# Zadanie kolejkowalne (Poziom 1/Faza 2): pełna analiza dokumentów w tle.
# Wyodrębnione z domknięcia route'a api_analyze_async do funkcji modułowej,
# by RQ mogło ją zserializować i policzyć w osobnym workerze. Postęp/wynik
# trafiają do tabeli job_progress (kontrakt pollowania bez zmian).
def _async_compare_job(job_id, path_a, path_b, type_a, type_b, fa_name, fb_name, uid):
    try:
        _update_progress(job_id, step="extract", message="Ekstrakcja tekstu z PDF…")
        
        from pdf_extractor import extract_text
        ta = extract_text(path_a)
        tb = extract_text(path_b)
        pages_a = ta.get("page_count", 1)
        pages_b = tb.get("page_count", 1)
        total_pages = pages_a + pages_b
        
        _update_progress(job_id, step="detect", 
                       message=f"Wykrywanie dostawcy… ({pages_a + pages_b} stron)",
                       total_pages=total_pages)
        
        from supplier_profiles import detect_supplier
        supplier = detect_supplier(ta.get("text","") + " " + tb.get("text",""))
        
        _update_progress(job_id, step="enhanced",
                       message="Analiza tabel i pozycji produktów…",
                       current_page=1)
        
        from enhanced_comparator import compare_enhanced
        enh = compare_enhanced(path_a, path_b, type_a, type_b, supplier)
        ed = enh.to_dict()
        items_count = len(ed.get("items", []))
        
        _update_progress(job_id, step="table",
                       message=f"Dopasowanie {items_count} pozycji…",
                       items_found=items_count,
                       current_page=2)
        
        from table_extractor import compare_tables
        tbl = compare_tables(path_a, path_b).to_dict()
        
        _update_progress(job_id, step="regex",
                       message="Analiza pól tekstowych…",
                       current_page=3)
        

        text_a = extract_pdf_text(path_a)
        text_b = extract_pdf_text(path_b)
        rx = compare_documents(text_a, text_b, "auto").to_dict()
        
        _update_progress(job_id, step="typo",
                       message="Wykrywanie literówek i zamian I↔1…",
                       current_page=total_pages)
        
        from typo_detector import analyze_full_texts
        ty = analyze_full_texts(text_a, text_b, fa_name, fb_name).to_dict()
        
        _update_progress(job_id, step="finalize", message="Generowanie raportu…")
        
        report = {
            "file_a": fa_name, "file_b": fb_name,
            "doc_type_a": type_a, "doc_type_b": type_b,
            "total_errors": ed["diff_count"] + tbl.get("diff_count",0),
            "total_warnings": ed["warn_count"] + tbl.get("warn_count",0),
            "risk_level": ed["risk_level"],
            "supplier_detected": supplier.get("code","") if supplier else "",
            "supplier_name": supplier.get("name","") if supplier else "",
            "modules": {
                "enhanced": {**ed, "ok": True},
                "table":    {**tbl, "ok": True},
                "regex":    {**rx,  "ok": True},
                "typo":     {**ty,  "ok": True},
            }
        }
        # Zapisujemy realny typ dokumentu (nie etykietę „Async: …"), żeby
        # statystyki KPI po doc_type nie miały sztucznego kubełka i nie gubiły
        # tych porównań z agregatów. Bierzemy rozwiązane typy z wyniku enhanced.
        _dta = ed.get("doc_type_a") or type_a
        _dtb = ed.get("doc_type_b") or type_b
        _eff_dt = _dta if _dta == _dtb else f"{_dta}_vs_{_dtb}"
        cid = _save_comparison(uid, _eff_dt, fa_name, fb_name, report)
        report["comparison_id"] = cid
        
        try: _notify_errors(cid, report)
        except Exception as _e: logger.debug("notify_errors failed for cid=%s: %s", cid, _e)
        
        db2 = get_db()
        try:
            db2.execute(
                "UPDATE job_progress SET status=?,step=?,message=?,result_json=?,updated_at=datetime('now') WHERE job_id=?",
                ("done", "done", f"Gotowe — {items_count} pozycji, {report['total_errors']} błędów",
                 json.dumps(report), job_id)
            )
            db2.commit()
        finally:
            db2.close()

    except Exception as e:
        import traceback as _tb
        app.logger.error("Async job %s error: %s", job_id, _tb.format_exc())
        db3 = get_db()
        try:
            db3.execute(
                "UPDATE job_progress SET status=?,error=?,step=? WHERE job_id=?",
                ("error", str(e)[:500], "error", job_id)
            )
            db3.commit()
        finally:
            db3.close()
    finally:
        for _tmp in (path_a, path_b):
            try:
                if os.path.exists(_tmp):
                    os.remove(_tmp)
            except Exception:
                pass


@app.route("/api/analyze_async", methods=["POST"])
@login_required
@csrf_protect
def api_analyze_async():
    """Uruchamia analizę w tle, zwraca job_id do pollowania."""
    import uuid, threading
    
    file_a = request.files.get("file_a")
    file_b = request.files.get("file_b")
    if not file_a or not file_b:
        return jsonify({"error": "Wymagane dwa pliki PDF"}), 400

    uid = session["user_id"]
    if _check_and_record_api_rate(uid):
        return jsonify({"error": "Przekroczono limit zapytań. Poczekaj chwilę."}), 429
    for _f in (file_a, file_b):
        _err = _validate_pdf_upload(_f)
        if _err:
            return jsonify({"error": _err}), 400

    job_id = str(uuid.uuid4())
    
    # Zapisz pliki tymczasowo
    import os
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    fa_name = secure_filename(file_a.filename) or "doc_a.pdf"
    fb_name = secure_filename(file_b.filename) or "doc_b.pdf"
    _jid_prefix = job_id[:8]
    path_a = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_async_{_jid_prefix}_a_{fa_name}")
    path_b = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_async_{_jid_prefix}_b_{fb_name}")
    file_a.save(path_a)
    file_b.save(path_b)
    
    type_a = request.form.get("type_a", "auto")
    type_b = request.form.get("type_b", "auto")
    
    # Inicjalizuj rekord
    db = get_db()
    try:
        db.execute(
            "INSERT INTO job_progress(job_id,user_id,status,step,message) VALUES(?,?,?,?,?)",
            (job_id, uid, "running", "start", "Inicjalizacja analizy…")
        )
        db.commit()
    finally:
        db.close()
    
    # Faza 2 kolejkowania: licz w workerze (RQ) gdy REDIS_URL, inaczej w wątku.
    import jobs as _jobs
    _jobs.enqueue(_async_compare_job, job_id, path_a, path_b, type_a, type_b, fa_name, fb_name, uid)
    return jsonify({"job_id": job_id, "status": "running"})


@app.route("/api/progress/<job_id>")
@login_required
def api_progress(job_id):
    """Zwraca aktualny stan zadania analizy."""
    uid = session["user_id"]
    role = session.get("role", "user")
    db = get_db()
    try:
        row = db.execute("SELECT * FROM job_progress WHERE job_id=?", (job_id,)).fetchone()
    finally:
        db.close()
    if not row:
        return jsonify({"error": "Nie znaleziono zadania"}), 404
    row_uid = row["user_id"] if "user_id" in (row.keys() if hasattr(row, "keys") else {}) else None
    if row_uid != uid and not can_see_all(role):
        return jsonify({"error": "Brak dostępu"}), 403
    d = dict(row)
    # Nie zwracaj pełnego result_json w polling — tylko status
    has_result = bool(d.get("result_json"))
    result = None
    if d["status"] == "done" and has_result:
        try:
            result = json.loads(d["result_json"])
        except (ValueError, TypeError):
            result = None
            has_result = False
    d.pop("result_json", None)
    d["has_result"] = has_result
    if result:
        d["result"] = result
    return jsonify(d)


# ═══════════════════════════════════════════════════════════════
# FUNKCJA 7 — SZABLONY KOMENTARZY
# ═══════════════════════════════════════════════════════════════

@app.route("/api/comparison/<int:cid>/suggest_comment", methods=["POST"])
@login_required
@csrf_protect
def suggest_comment(cid):
    """Generuje sugerowane komentarze na podstawie błędów w raporcie."""
    data = request.get_json(silent=True) or {}
    items    = data.get("items", [])
    findings = data.get("findings", [])
    headers  = data.get("headers", [])
    currency = data.get("currency", "USD")
    
    suggestions = []
    diff_value = 0.0
    
    # Z pozycji towarowych
    for it in items:
        ref = it.get("ref", "?")
        status = it.get("status", "")
        issues = it.get("issues", [])
        
        if status in ("brak_w_b", "tylko_a"):
            suggestions.append({
                "type": "missing",
                "severity": "error",
                "text_pl": f"Pozycja {ref} z PO nie figuruje w PI — proszę o uzupełnienie.",
                "text_en": f"Item {ref} from PO is missing in PI — please add it.",
                "ref": ref
            })
        elif status in ("brak_w_a", "tylko_b"):
            suggestions.append({
                "type": "extra",
                "severity": "warning",
                "text_pl": f"Pozycja {ref} w PI nie jest na PO — proszę o wyjaśnienie.",
                "text_en": f"Item {ref} in PI is not on PO — please clarify.",
                "ref": ref
            })
        elif status == "roznica":
            for issue in issues:
                if "Ilość" in issue or "qty" in issue.lower():
                    qa = it.get("qty_a","?"); qb = it.get("qty_b","?")
                    suggestions.append({
                        "type": "qty",
                        "severity": "error",
                        "text_pl": f"Proszę o korektę ilości pozycji {ref}: PO={qa} szt., PI={qb} szt.",
                        "text_en": f"Please correct quantity for {ref}: PO={qa} pcs, PI={qb} pcs.",
                        "ref": ref
                    })
                if "Cena" in issue or "price" in issue.lower():
                    pa = it.get("price_a","?"); pb = it.get("price_b","?")
                    suggestions.append({
                        "type": "price",
                        "severity": "error",
                        "text_pl": f"Proszę potwierdzić cenę pozycji {ref}: PO={pa} {currency}, PI={pb} {currency}.",
                        "text_en": f"Please confirm price for {ref}: PO={pa} {currency}, PI={pb} {currency}.",
                        "ref": ref
                    })
                if "Net" in issue:
                    try:
                        from normalizer import normalize_number as _nn
                        na = float(_nn(str(it.get("net_a","0"))) or 0)
                        nb = float(_nn(str(it.get("net_b","0"))) or 0)
                        diff_value += abs(na - nb)
                    except Exception:
                        pass
    
    # Z nagłówków
    for h in headers:
        if h.get("status") in ("roznica","brak_w_a","brak_w_b"):
            key = h.get("key","?")
            va = h.get("val_a","—"); vb = h.get("val_b","—")
            if "płatno" in key.lower() or "payment" in key.lower():
                suggestions.append({
                    "type": "payment",
                    "severity": "warning",
                    "text_pl": f"Warunki płatności niezgodne: PO='{va}', PI='{vb}' — proszę o potwierdzenie.",
                    "text_en": f"Payment terms mismatch: PO='{va}', PI='{vb}' — please confirm.",
                })
    
    # Podsumowanie
    n = len([s for s in suggestions if s["severity"] == "error"])
    if n > 0:
        suggestions.append({
            "type": "summary",
            "severity": "info",
            "text_pl": f"Łącznie {n} pozycji wymaga wyjaśnienia. "
                       f"{'Wartość różnic: '+str(round(diff_value,2))+' '+currency+'.' if diff_value else ''}",
            "text_en": f"Total {n} items require clarification. "
                       f"{'Value of differences: '+str(round(diff_value,2))+' '+currency+'.' if diff_value else ''}",
        })
    
    # Pełny email EN do dostawcy
    if suggestions:
        lines = [f"Dear Supplier,",
                 f"",
                 f"We have reviewed your Proforma Invoice against our Purchase Order and found the following discrepancies:",
                 f""]
        for s in suggestions:
            if s.get("type") != "summary":
                lines.append(f"• {s.get('text_en','')}")
        lines += ["", "Please provide corrected PI addressing the above points at your earliest convenience.",
                  "", "Best regards,", "ACME — Purchasing Department"]
        email_text = "\n".join(lines)
    else:
        email_text = ""
    
    return jsonify({"suggestions": suggestions, "email_draft": email_text, "diff_value": round(diff_value,2)})


# ═══════════════════════════════════════════════════════════════
# ZGŁOSZENIA (TICKETS)
# ═══════════════════════════════════════════════════════════════

@app.route("/tickets")
@login_required
def tickets_page():
    db = get_db()
    uid = session["user_id"]
    role = session.get("role", "user")
    tickets = []
    try:
        if role_level(role) >= role_level("manager"):
            rows = db.execute(
                """SELECT t.*, u.username,
                          c.doc_type as comp_doc_type,
                          c.po_number as comp_po_number,
                          c.file_a as comp_file_a
                   FROM tickets t
                   JOIN users u ON t.user_id=u.id
                   LEFT JOIN comparisons c ON t.comparison_id=c.id
                   ORDER BY t.created_at DESC""").fetchall()
        else:
            rows = db.execute(
                """SELECT t.*, u.username,
                          c.doc_type as comp_doc_type,
                          c.po_number as comp_po_number,
                          c.file_a as comp_file_a
                   FROM tickets t
                   JOIN users u ON t.user_id=u.id
                   LEFT JOIN comparisons c ON t.comparison_id=c.id
                   WHERE t.user_id=? ORDER BY t.created_at DESC""", (uid,)).fetchall()
        tickets = [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"tickets_page DB error: {e}")
    finally:
        db.close()
    return render_template("tickets.html", tickets=tickets,
                           role=role, username=session.get("username", ""))


@app.route("/api/tickets", methods=["POST"])
@login_required
@csrf_protect
def create_ticket():
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()[:200]
    description = str(data.get("description") or "").strip()[:2000]
    category = data.get("category", "inne")
    if category not in ("niezgodność", "sugestia", "błąd", "inne", "rozbieznosc"):
        category = "inne"
    _cmp_id_raw = data.get("comparison_id")
    try:
        comparison_id = int(_cmp_id_raw) if _cmp_id_raw not in (None, "", False) else None
    except (TypeError, ValueError):
        comparison_id = None
    priority = data.get("priority", "normal")
    if priority not in ("low", "normal", "high", "critical"):
        priority = "normal"
    if not title or not description:
        return jsonify({"error": "Tytuł i opis są wymagane"}), 400
    db = get_db()
    try:
        db.execute(
            "INSERT INTO tickets(user_id,title,description,category,status,comparison_id,priority) VALUES(?,?,?,?,?,?,?)",
            (session["user_id"], title, description, category, "nowe", comparison_id, priority)
        )
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/tickets/<int:tid>", methods=["PATCH"])
@require_role("manager")
@csrf_protect
def update_ticket(tid):
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    admin_response = str(data.get("admin_response") or "").strip()[:2000]
    valid_statuses = ("nowe", "w_toku", "rozwiązane", "zamknięte")
    if status and status not in valid_statuses:
        return jsonify({"error": "Nieprawidłowy status"}), 400
    db = get_db()
    try:
        row = db.execute("SELECT id FROM tickets WHERE id=?", (tid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono zgłoszenia"}), 404
        fields = []
        params = []
        if status:
            fields.append("status=?")
            params.append(status)
        if admin_response:
            fields.append("admin_response=?")
            params.append(admin_response)
            fields.append("resolved_by=?")
            params.append(session.get("username", ""))
        fields.append("updated_at=datetime('now')")
        params.append(tid)
        # bandit: nazwy kolumn z białej listy w kodzie (nie z kluczy żądania); wartości przez ?
        db.execute(f"UPDATE tickets SET {', '.join(fields)} WHERE id=?", params)  # nosec B608
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/tickets/count")
@login_required
def tickets_count():
    try:
        db = get_db()
        try:
            role = session.get("role", "user")
            if role_level(role) >= role_level("manager"):
                row = db.execute(
                    "SELECT COUNT(*) FROM tickets WHERE status='nowe'"
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT COUNT(*) FROM tickets WHERE user_id=? AND status NOT IN ('rozwiązane','zamknięte')",
                    (session["user_id"],)
                ).fetchone()
        finally:
            db.close()
        return jsonify({"count": row[0] if row else 0})
    except Exception:
        return jsonify({"count": 0})


@app.route("/api/tickets/from-comparison/<int:cid>", methods=["POST"])
@login_required
@csrf_protect
def api_ticket_from_comparison(cid):
    db = get_db()
    try:
        comp = db.execute("SELECT * FROM comparisons WHERE id=?", (cid,)).fetchone()
        if not comp:
            return jsonify({"error": "Porównanie nie istnieje"}), 404
        comp = dict(comp)
        uid = session["user_id"]
        role = session.get("role", "user")
        if comp.get("user_id") != uid and not can_see_all(role):
            return jsonify({"error": "Brak dostępu"}), 403
        title = f"Rozbieżność: {comp.get('doc_type','?')} — {comp.get('po_number') or comp.get('file_a') or 'ID ' + str(cid)}"
        desc = (
            f"Automatycznie wygenerowane z porównania ID {cid}.\n"
            f"Typ: {comp.get('doc_type','?')}\n"
            f"PO: {comp.get('po_number') or '—'}\n"
            f"Liczba różnic: {comp.get('diff_count') or 0}"
        )
        cur = db.execute(
            "INSERT INTO tickets(user_id, title, description, category, status, comparison_id, priority) "
            "VALUES(?,?,?,?,?,?,?)",
            (uid, title, desc, "rozbieznosc", "nowe", cid, "high")
        )
        db.commit()
        return jsonify({"ok": True, "ticket_id": cur.lastrowid})
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# KOSZTY API CLAUDE
# ═══════════════════════════════════════════════════════════════

@app.route("/api-costs")
@require_role("manager")
def api_costs_page():
    """Strona kosztów Claude API — manager widzi swoje, admin widzi wszystkich."""
    role = session["role"]
    uid  = session["user_id"]
    is_admin_role = role in ("admin", "superuser")
    db = get_db()
    totals = {"calls": 0, "inp": 0, "out": 0, "cost": 0.0}
    current_month = {"calls": 0, "cost": 0.0}
    by_model = []; by_type = []; monthly = []; by_user = []
    try:
        wh   = "" if is_admin_role else "WHERE a.user_id=?"
        p    = [] if is_admin_role else [uid]
        # PG nie ma strftime — porównujemy z miesiącem policzonym w Pythonie (param).
        wh_m = (wh + (" AND" if wh else "WHERE") + " substr(a.created_at,1,7)=?")
        _cur_month = _ts_ago(seconds=0)[:7]   # 'YYYY-MM'

        totals = dict(db.execute(
            # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
            f"SELECT COUNT(*) as calls, COALESCE(SUM(input_tokens),0) as inp,"  # nosec B608
            f" COALESCE(SUM(output_tokens),0) as out, COALESCE(SUM(cost_usd),0.0) as cost"
            f" FROM api_usage a {wh}", p).fetchone() or {}) or totals

        current_month = dict(db.execute(
            # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
            f"SELECT COUNT(*) as calls, COALESCE(SUM(cost_usd),0.0) as cost"  # nosec B608
            f" FROM api_usage a {wh_m}", p + [_cur_month]).fetchone() or {}) or current_month

        by_model = [dict(r) for r in db.execute(
            # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
            f"SELECT model, COUNT(*) as calls,"  # nosec B608
            f" COALESCE(SUM(input_tokens),0) as inp,"
            f" COALESCE(SUM(output_tokens),0) as out,"
            f" COALESCE(SUM(cost_usd),0.0) as cost"
            f" FROM api_usage a {wh} GROUP BY model ORDER BY cost DESC", p).fetchall()]

        by_type = [dict(r) for r in db.execute(
            # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
            f"SELECT call_type, COUNT(*) as calls, COALESCE(SUM(cost_usd),0.0) as cost"  # nosec B608
            f" FROM api_usage a {wh} GROUP BY call_type ORDER BY cost DESC", p).fetchall()]

        monthly = [dict(r) for r in db.execute(
            # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
            f"SELECT substr(a.created_at,1,7) as month, COUNT(*) as calls,"  # nosec B608
            f" COALESCE(SUM(cost_usd),0.0) as cost"
            f" FROM api_usage a {wh} GROUP BY month ORDER BY month DESC LIMIT 12", p).fetchall()]

        if is_admin_role:
            by_user = [dict(r) for r in db.execute(
                "SELECT u.username, COUNT(*) as calls,"
                " COALESCE(SUM(a.cost_usd),0.0) as cost"
                " FROM api_usage a JOIN users u ON a.user_id=u.id"
                " GROUP BY a.user_id ORDER BY cost DESC").fetchall()]
            total_cost = totals.get("cost") or 0
            for row in by_user:
                row["pct"] = round(row["cost"] / total_cost * 100, 1) if total_cost else 0
    except Exception as _e:
        logger.warning(f"api_costs_page DB error: {_e}")
    finally:
        db.close()

    avg_cost = round(totals.get("cost", 0) / totals["calls"], 6) if totals.get("calls") else 0
    USD_TO_PLN = 4.0  # przybliżony kurs — aktualizuj ręcznie w razie potrzeby
    return render_template("api_costs.html",
        username=session["username"], role=role,
        totals=totals, current_month=current_month,
        by_model=by_model, by_type=by_type,
        monthly=list(reversed(monthly)),
        by_user=by_user, is_admin=is_admin_role,
        avg_cost=avg_cost, usd_to_pln=USD_TO_PLN)


# ═══════════════════════════════════════════════════════════════
# SZABLONY PORÓWNAŃ
# ═══════════════════════════════════════════════════════════════

def _ensure_json_str(value, default: str = "{}") -> str:
    """Return *value* as a JSON string. If it's already a string, validate it;
    if it's a dict/list, serialize it; otherwise return *default*."""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value)
        except Exception:
            return default
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except Exception:
            return default
    return default

@app.route("/templates")
@require_role("admin")
def templates_page():
    """Strona zarządzania szablonami porównań (tylko admin)."""
    db = get_db()
    try:
        rows = db.execute("SELECT * FROM comparison_templates ORDER BY page_type, name").fetchall()
        rows = [dict(r) for r in rows]
    except Exception as _e:
        logger.warning(f"templates_page DB error: {_e}")
        rows = []
    finally:
        db.close()
    return render_template("templates.html",
        username=session["username"], role=session["role"],
        templates=rows)


@app.route("/api/templates")
@login_required
def api_templates_list():
    """Zwraca listę szablonów publicznych (lub wszystkich dla admina)."""
    page_type = request.args.get("page_type", "")
    uid  = session["user_id"]
    role = session["role"]
    db   = get_db()
    try:
        if is_admin(role):
            rows = db.execute(
                "SELECT * FROM comparison_templates ORDER BY name").fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM comparison_templates WHERE is_public=1 ORDER BY name",
            ).fetchall()
    finally:
        db.close()
    result = [dict(r) for r in rows]
    if page_type:
        result = [r for r in result if r.get("page_type") == page_type]
    return jsonify(result)


@app.route("/api/templates", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_templates_create():
    data = request.get_json(force=True) or {}
    name = str(data.get("name") or "").strip()[:200]
    if not name:
        return jsonify({"error": "Pole 'name' jest wymagane"}), 400
    page_type = data.get("page_type", "compare")
    if page_type not in ("compare", "typo", "table"):
        return jsonify({"error": "Nieprawidłowy page_type — dozwolone: compare, typo, table"}), 400
    try:
        price_tol = float(data.get("price_tolerance_pct") or 0.5)
        qty_tol   = float(data.get("qty_tolerance_pct") or 0.0)
    except (TypeError, ValueError):
        return jsonify({"error": "price_tolerance_pct i qty_tolerance_pct muszą być liczbami"}), 400
    if not (0.0 <= price_tol <= 100.0):
        price_tol = 0.5
    if not (0.0 <= qty_tol <= 100.0):
        qty_tol = 0.0
    db = get_db()
    try:
        cursor = db.execute(
            """INSERT INTO comparison_templates
               (name, description, page_type, is_public, supplier_code, doc_type_a, doc_type_b,
                price_tolerance_pct, qty_tolerance_pct, ai_model, use_ai,
                column_mapping_json, ignore_fields_json, created_by)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, str(data.get("description") or "")[:2000], page_type,
             int(bool(data.get("is_public"))),
             str(data.get("supplier_code") or "")[:50],
             str(data.get("doc_type_a") or "")[:50],
             str(data.get("doc_type_b") or "")[:50],
             price_tol, qty_tol,
             str(data.get("ai_model") or "claude-sonnet-4-6")[:100],
             int(bool(data.get("use_ai", True))),
             json.dumps(data.get("column_mapping") or {}),
             json.dumps(data.get("ignore_fields") or []),
             session["user_id"])
        )
        db.commit()
        return jsonify({"id": cursor.lastrowid}), 201
    except Exception as e:
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500
    finally:
        db.close()


@app.route("/api/templates/<int:tid>", methods=["PATCH"])
@require_role("admin")
@csrf_protect
def api_templates_update(tid):
    data = request.get_json(force=True) or {}
    db = get_db()
    try:
        if not db.execute("SELECT id FROM comparison_templates WHERE id=?", (tid,)).fetchone():
            return jsonify({"error": "Nie znaleziono szablonu"}), 404
        ALLOWED = {"name", "description", "page_type", "is_public", "supplier_code",
                   "doc_type_a", "doc_type_b", "price_tolerance_pct", "qty_tolerance_pct",
                   "ai_model", "use_ai", "column_mapping_json", "ignore_fields_json"}
        _tmpl_max_lens = {"name": 200, "description": 2000, "supplier_code": 50,
                          "doc_type_a": 50, "doc_type_b": 50, "ai_model": 100,
                          "column_mapping_json": 50000, "ignore_fields_json": 10000}
        _tmpl_enums = {"page_type": {"compare", "typo", "table"}}
        sets, vals = [], []
        for k, v in data.items():
            if k in ALLOWED:
                if k in _tmpl_enums:
                    if v not in _tmpl_enums[k]:
                        continue
                elif k in ("is_public", "use_ai"):
                    v = 1 if v else 0
                elif k in ("price_tolerance_pct", "qty_tolerance_pct"):
                    try:
                        v = float(v or 0)
                    except (TypeError, ValueError):
                        continue
                    if not (0.0 <= v <= 100.0):
                        continue
                elif k in _tmpl_max_lens:
                    v = str(v or "")[:_tmpl_max_lens[k]]
                sets.append(f"{k}=?")
                vals.append(v)
        if sets:
            sets.append("updated_at=datetime('now')")
            vals.append(tid)
            # bandit: nazwy kolumn z białej listy w kodzie (nie z kluczy żądania); wartości przez ?
            db.execute(f"UPDATE comparison_templates SET {','.join(sets)} WHERE id=?", vals)  # nosec B608
            db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/templates/<int:tid>", methods=["DELETE"])
@require_role("admin")
@csrf_protect
def api_templates_delete(tid):
    db = get_db()
    try:
        db.execute("DELETE FROM comparison_templates WHERE id=?", (tid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/templates/<int:tid>/use", methods=["POST"])
@login_required
@csrf_protect
def api_templates_use(tid):
    db = get_db()
    try:
        db.execute("UPDATE comparison_templates SET use_count=use_count+1 WHERE id=?", (tid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/templates/export")
@require_role("admin")
def api_templates_export():
    db = get_db()
    try:
        rows = db.execute("SELECT * FROM comparison_templates ORDER BY name").fetchall()
    finally:
        db.close()
    payload = json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2)
    return (payload, 200, {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Disposition": "attachment; filename=comparison_templates.json",
    })


@app.route("/api/templates/import", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_templates_import():
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, list):
        return jsonify({"error": "Oczekiwano tablicy JSON szablonów"}), 400
    db = get_db()
    imported = 0
    try:
        for t in data:
            name      = (t.get("name") or "").strip()
            page_type = t.get("page_type", "compare")
            if not name or page_type not in ("compare", "typo", "table"):
                continue
            try:
                _ptol = float(t.get("price_tolerance_pct") or 0.5)
                _qtol = float(t.get("qty_tolerance_pct") or 0.0)
                if not (0.0 <= _ptol <= 100.0): _ptol = 0.5
                if not (0.0 <= _qtol <= 100.0): _qtol = 0.0
                _imp_name = name[:200]
                _imp_desc = str(t.get("description") or "")[:2000]
                _imp_sc   = str(t.get("supplier_code") or "")[:50]
                _imp_dta  = str(t.get("doc_type_a") or "")[:50]
                _imp_dtb  = str(t.get("doc_type_b") or "")[:50]
                _imp_am   = str(t.get("ai_model") or "claude-sonnet-4-6")[:100]
                _imp_cmj  = _ensure_json_str(t.get("column_mapping_json"), "{}")[:50000]
                _imp_ifj  = _ensure_json_str(t.get("ignore_fields_json"), "[]")[:10000]
                db.execute(
                    """INSERT INTO comparison_templates
                       (name, description, page_type, is_public, supplier_code,
                        doc_type_a, doc_type_b, price_tolerance_pct, qty_tolerance_pct,
                        ai_model, use_ai, column_mapping_json, ignore_fields_json, created_by)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (_imp_name, _imp_desc, page_type,
                     int(bool(t.get("is_public"))), _imp_sc,
                     _imp_dta, _imp_dtb,
                     _ptol, _qtol,
                     _imp_am,
                     int(bool(t.get("use_ai", True))),
                     _imp_cmj, _imp_ifj,
                     session["user_id"])
                )
                imported += 1
            except Exception:
                pass
        db.commit()
        return jsonify({"imported": imported})
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# POMOC / INSTRUKCJA
# ═══════════════════════════════════════════════════════════════

@app.route("/help")
@login_required
def help_page():
    return render_template("help.html",
                           role=session.get("role", "user"),
                           username=session.get("username", ""))


# ═══════════════════════════════════════════════════════════════
# BIBLIOTEKA ARTWORKÓW (dysk sieciowy)
# ═══════════════════════════════════════════════════════════════

def _library_token() -> str:
    """Zwraca (lub generuje) token synchronizacji biblioteki."""
    import secrets as _sec
    db = get_db()
    try:
        row = db.execute(
            "SELECT value FROM settings WHERE category='library' AND key='sync_token'"
        ).fetchone()
        if row:
            return row[0]
        token = _sec.token_hex(32)
        # ON CONFLICT — dwa równoległe pierwsze wywołania nie mogą wstawić dwóch tokenów
        # (UNIQUE(category,key)); po wstawieniu odczytujemy wartość obowiązującą.
        db.execute(
            "INSERT INTO settings(category,key,value) VALUES('library','sync_token',?) "
            "ON CONFLICT(category,key) DO NOTHING",
            (token,)
        )
        db.commit()
        row = db.execute(
            "SELECT value FROM settings WHERE category='library' AND key='sync_token'"
        ).fetchone()
        return row[0] if row else token
    finally:
        db.close()


def _library_auth(req) -> bool:
    """Sprawdza Bearer token w nagłówku Authorization."""
    import hmac as _hmac_lib
    auth = req.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return _hmac_lib.compare_digest(auth[7:], _library_token())


def _path_levels(rel_path: str) -> list:
    """Rozbija ścieżkę na maks. 5 poziomów folderów."""
    parts = rel_path.replace("\\", "/").split("/")
    # ostatni element to plik, reszta to foldery
    parts = parts[:-1]
    return (parts + ["", "", "", "", ""])[:5]


@app.route("/library/download-agent")
@require_role("manager")
def library_download_agent():
    """Serwuje skrypt library_sync.py do pobrania."""
    from flask import send_file as _sf
    agent_path = os.path.join(os.path.dirname(__file__), "library_sync.py")
    if not os.path.exists(agent_path):
        return "Plik agenta nie znaleziony", 404
    return _sf(agent_path, as_attachment=True, download_name="library_sync.py",
               mimetype="text/x-python")


@app.route("/library")
@require_role("manager")
def library_page():
    """Strona zarządzania biblioteką artworków."""
    db = get_db()
    try:
        total = (db.execute("SELECT COUNT(*) FROM library_files").fetchone() or [0])[0]
        last_sync = (db.execute(
            "SELECT MAX(synced_at) FROM library_files"
        ).fetchone() or [None])[0]
        brands = [r[0] for r in db.execute(
            "SELECT DISTINCT lvl1 FROM library_files WHERE lvl1!='' ORDER BY lvl1"
        ).fetchall()]
    except Exception:
        total = 0; last_sync = None; brands = []
    finally:
        db.close()
    token = _library_token()
    return render_template("library.html",
        username=session["username"], role=session["role"],
        total=total, last_sync=last_sync, brands=brands,
        sync_token=token)


def _ensure_master_profile(db, file_path: str, filename: str, rel_path: str,
                           thumb_bytes=None) -> bool:
    """Zarejestruj wgrany plik mastera jako profil mapowania (artwork_profiles), o ile
    jeszcze go nie ma — żeby pojawił się w zakładce „Mapowanie" jako pozycja DO
    ZMAPOWANIA (0 pól). Nie nadpisuje istniejącego profilu (nie kasuje mapowań).
    Zwraca True, gdy utworzono nowy profil."""
    try:
        _ensure_artwork_profile_tables(db)
    except Exception:
        pass
    ex = db.execute(
        "SELECT id FROM artwork_profiles WHERE master_pdf_path=? LIMIT 1", (file_path,)).fetchone()
    if ex:
        return False
    parsed = {}
    try:
        from artwork_naming import parse_master_filename as _pmf
        parsed = _pmf(filename) or {}
    except Exception:
        parsed = {}
    ref_code = str(parsed.get("ref") or "").strip()
    pkg_type = str(parsed.get("packaging_type") or "").strip()
    rev_str  = str(parsed.get("revision") or "").strip()
    try:
        rev_rank = int(parsed.get("revision_rank") or 0)
    except (TypeError, ValueError):
        rev_rank = 0
    ean_val = str(parsed.get("ean") or "").strip()
    name = ref_code or os.path.splitext(filename)[0]
    folder = "/".join(rel_path.replace("\\", "/").split("/")[:-1])
    thumb_b64 = ""
    if thumb_bytes:
        try:
            import base64 as _b64
            thumb_b64 = _b64.b64encode(thumb_bytes).decode()   # surowy base64 (jak w profilach)
        except Exception:
            thumb_b64 = ""
    db.execute(
        "INSERT INTO artwork_profiles "
        "(name, ean, ref_code, master_pdf_path, thumb_b64, folder_path, "
        "packaging_type, revision, revision_rank, source_path, is_active, created_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,1,NULL)",
        (name, ean_val, ref_code, file_path, thumb_b64, folder,
         pkg_type, rev_str, rev_rank, rel_path))
    db.commit()
    logger.info("master profile created (do zmapowania) ref=%s plik=%s", ref_code or name, filename)
    return True


@app.route("/api/library/sync", methods=["POST"])
def api_library_sync():
    """
    Endpoint dla agenta synchronizacji (library_sync.py).
    Przyjmuje multipart/form-data z polem 'file' (PDF) i 'meta' (JSON).
    Gdy meta.is_master=true i przesłano pełny plik — rejestruje master jako profil
    mapowania (żeby był widoczny w zakładce „Mapowanie" jako do zmapowania).
    """
    if not _library_auth(request):
        return jsonify({"error": "Brak autoryzacji"}), 401

    meta_raw = request.form.get("meta") or request.get_json(silent=True) or {}
    if isinstance(meta_raw, str):
        try:
            import json as _j; meta_raw = _j.loads(meta_raw)
        except Exception:
            meta_raw = {}

    rel_path   = str(meta_raw.get("rel_path") or "").replace("\\", "/").strip("/")[:1000]
    filename   = str(meta_raw.get("filename") or rel_path.split("/")[-1] or "")[:500]
    checksum   = str(meta_raw.get("checksum", "") or "")[:128]
    modified   = str(meta_raw.get("modified_at", "") or "")[:50]
    z_path_raw = str(meta_raw.get("z_path", "") or "")[:1000]
    try:
        size_bytes = int(meta_raw.get("size_bytes") or 0)
    except (TypeError, ValueError):
        size_bytes = 0

    if not rel_path or not filename:
        return jsonify({"error": "rel_path i filename są wymagane"}), 400

    levels = _path_levels(rel_path)

    # Get thumbnail (new mode: thumb only, no full file stored)
    thumb_data = None
    thumb_file = request.files.get("thumb")
    if thumb_file:
        thumb_data = thumb_file.read()
    elif meta_raw.get("thumb_b64"):
        import base64 as _b64
        try:
            thumb_data = _b64.b64decode(meta_raw["thumb_b64"])
        except Exception:
            thumb_data = None

    z_path = z_path_raw

    # Only save full file if explicitly sent (backward compat)
    lib_folder = os.path.realpath(app.config["LIBRARY_FOLDER"])
    file_path_on_disk = ""
    f = request.files.get("file")
    if f:
        dest_path = os.path.join(lib_folder, rel_path)
        dest_real = os.path.realpath(dest_path)
        if not dest_real.startswith(lib_folder + os.sep):
            return jsonify({"error": "Nieprawidłowa ścieżka pliku"}), 400
        os.makedirs(os.path.dirname(dest_real), exist_ok=True)
        f.save(dest_real)
        dest_path = dest_real
        size_bytes = os.path.getsize(dest_path)
        file_path_on_disk = dest_path

    db = get_db()
    try:
        existing = db.execute(
            "SELECT id, checksum FROM library_files WHERE rel_path=?", (rel_path,)
        ).fetchone()

        if existing:
            if existing["checksum"] == checksum and not file_path_on_disk and not thumb_data:
                return jsonify({"status": "skipped", "id": existing["id"]})
            update_fields = (
                "filename=?, size_bytes=?, modified_at=?, synced_at=datetime('now'), "
                "checksum=?, lvl1=?, lvl2=?, lvl3=?, lvl4=?, lvl5=?, z_path=?"
            )
            params = [filename, size_bytes, modified, checksum, *levels, z_path]
            if file_path_on_disk:
                update_fields += ", file_path=?"
                params.append(file_path_on_disk)
            if thumb_data is not None:
                update_fields += ", thumb_data=?"
                params.append(thumb_data)
            params.append(rel_path)
            # bandit: nazwy kolumn z białej listy w kodzie (nie z kluczy żądania); wartości przez ?
            db.execute(f"UPDATE library_files SET {update_fields} WHERE rel_path=?", params)  # nosec B608
            db.commit()
            if meta_raw.get("is_master") and file_path_on_disk:
                try:
                    _ensure_master_profile(db, file_path_on_disk, filename, rel_path, thumb_data)
                except Exception as _pe:
                    logger.warning("ensure master profile failed rel=%s: %s", rel_path, _pe)
            return jsonify({"status": "updated", "id": existing["id"]})
        else:
            cur = db.execute(
                """INSERT INTO library_files
                   (rel_path, filename, size_bytes, modified_at, checksum,
                    lvl1, lvl2, lvl3, lvl4, lvl5, file_path, thumb_data, z_path)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rel_path, filename, size_bytes, modified, checksum,
                 *levels, file_path_on_disk, thumb_data, z_path)
            )
            db.commit()
            if meta_raw.get("is_master") and file_path_on_disk:
                try:
                    _ensure_master_profile(db, file_path_on_disk, filename, rel_path, thumb_data)
                except Exception as _pe:
                    logger.warning("ensure master profile failed rel=%s: %s", rel_path, _pe)
            return jsonify({"status": "created", "id": cur.lastrowid})
    except Exception as e:
        logger.error(f"library sync error: {e}")
        logger.exception("Unexpected error %s", request.path)
        return jsonify({"error": "Błąd serwera"}), 500
    finally:
        db.close()


@app.route("/api/library/needed-masters", methods=["GET"])
def api_library_needed_masters():
    """Lista POTWIERDZONYCH masterów, które NIE mają jeszcze pliku PDF na serwerze.

    Agent `library_sync.py --upload-masters` pobiera tę listę i wysyła TYLKO te
    pełne pliki — dzięki temu auto-podpinanie masterów do dostaw działa, bez
    przesyłania całej biblioteki (potwierdzone = potrzebne i aktualne). Auth
    tokenem synchronizacji (jak /api/library/sync)."""
    if not _library_auth(request):
        return jsonify({"error": "Brak autoryzacji"}), 401
    import artwork_index as _ai
    out = []
    db = get_db()
    try:
        try:
            _ai.ensure_confirmed_table(db)
        except Exception:
            pass
        rows = db.execute(
            "SELECT ref_code, rel_path, filename FROM artwork_confirmed "
            "WHERE rel_path<>'' ORDER BY ref_code").fetchall()
        for r in rows:
            rel = (r["rel_path"] or "").strip()
            if not rel:
                continue
            lf = db.execute(
                "SELECT file_path, z_path FROM library_files WHERE rel_path=?", (rel,)).fetchone()
            # Już ma plik PDF na serwerze (i istnieje na dysku) → pomiń.
            if lf and (lf["file_path"] or "").strip() and os.path.exists(lf["file_path"]):
                continue
            out.append({
                "rel_path": rel,
                "filename": (r["filename"] or rel.split("/")[-1]),
                "z_path": ((lf["z_path"] if lf else "") or ""),
            })
    finally:
        db.close()
    return jsonify({"files": out, "count": len(out)})


@app.route("/api/library/browse")
@login_required
def api_library_browse():
    """Przeglądanie biblioteki po ścieżce."""
    path = request.args.get("path", "").strip("/")
    # Guard against path traversal and invalid characters
    if ".." in path or any(c in path for c in ("\x00", "\r", "\n")):
        return jsonify({"error": "Nieprawidłowa ścieżka"}), 400
    _MAX_LIBRARY_DEPTH = 5
    items: list = []
    total_files: int | None = None
    _page: int | None = None
    _per: int | None = None
    db = get_db()
    try:
        if not path:
            # Poziom 0 — lista marek (lvl1)
            rows = db.execute(
                "SELECT lvl1, COUNT(*) as cnt FROM library_files "
                "WHERE lvl1!='' GROUP BY lvl1 ORDER BY lvl1"
            ).fetchall()
            items = [{"name": r["lvl1"], "type": "folder",
                      "path": r["lvl1"], "count": r["cnt"]} for r in rows]
        else:
            parts = path.split("/")
            depth = min(len(parts), _MAX_LIBRARY_DEPTH - 1)
            parts = parts[:depth]
            col = f"lvl{depth + 1}"
            # Warunek WHERE dla wszystkich dotychczasowych poziomów
            wheres = [f"lvl{i+1}=?" for i in range(depth)]
            where_sql = " AND ".join(wheres) if wheres else "1=1"

            # Sprawdź czy są podfoldery
            try:
                sub = db.execute(
                    # bandit: kolumny lvlN wyliczane z liczby (depth), nie z wejścia; wartości przez ?
                    f"SELECT {col}, COUNT(*) as cnt FROM library_files "  # nosec B608
                    f"WHERE {where_sql} AND {col}!='' "
                    f"GROUP BY {col} ORDER BY {col}",
                    parts
                ).fetchall()
            except Exception:
                sub = []

            if sub:
                items = [{"name": r[col], "type": "folder",
                          "path": path + "/" + r[col], "count": r["cnt"]}
                         for r in sub]
            else:
                # Liście — pliki z paginacją; where_sql uses bare column names (lvl1=? …)
                # so we prefix them with lf. for the JOIN query
                lf_where = re.sub(r'\blvl(\d)\b', r'lf.lvl\1', where_sql)
                try:
                    _page = max(1, min(10000, int(request.args.get("page", 1))))
                    _per  = max(1, min(int(request.args.get("per_page", 100)), 500))
                except (ValueError, TypeError):
                    _page, _per = 1, 100
                _offset = (_page - 1) * _per

                total_files = (db.execute(
                    # bandit: kolumny lvlN wyliczane z liczby (depth), nie z wejścia; wartości przez ?
                    f"SELECT COUNT(*) FROM library_files lf WHERE {lf_where}",  # nosec B608
                    parts
                ).fetchone() or [0])[0]

                files = db.execute(
                    # bandit: kolumny lvlN wyliczane z liczby (depth), nie z wejścia; wartości przez ?
                    f"""SELECT lf.id, lf.filename, lf.size_bytes, lf.modified_at,
                               COUNT(apf.id) AS field_count
                        FROM library_files lf
                        LEFT JOIN artwork_profiles ap
                               ON ap.master_pdf_path = lf.file_path
                              AND lf.file_path != '' AND lf.file_path IS NOT NULL
                        LEFT JOIN artwork_profile_fields apf ON apf.profile_id = ap.id
                        WHERE {lf_where}
                        GROUP BY lf.id
                        ORDER BY lf.filename
                        LIMIT ? OFFSET ?""",  # nosec B608
                    parts + [_per, _offset]
                ).fetchall()
                items = [{"id": r["id"], "name": r["filename"],
                          "type": "file", "size": r["size_bytes"],
                          "modified": r["modified_at"],
                          "path": path + "/" + r["filename"],
                          "field_count": r["field_count"] or 0}
                         for r in files]
    except Exception as e:
        logger.warning(f"library browse error: {e}")
        items = []
        total_files = 0
        _page = _per = 1
    finally:
        db.close()
    resp: dict = {"path": path, "items": items}
    # Include pagination metadata when browsing files (leaf level)
    if isinstance(total_files, int):
        resp["total"] = total_files
        resp["page"] = _page
        resp["per_page"] = _per
        resp["total_pages"] = max(1, (total_files + _per - 1) // _per)
    return jsonify(resp)


@app.route("/api/library/search")
def api_library_search():
    """Wyszukiwanie plików w bibliotece."""
    if "user_id" not in session and not _library_auth(request):
        return jsonify({"error": "Brak autoryzacji"}), 401
    q = request.args.get("q", "").strip()
    try:
        limit = max(1, min(int(request.args.get("limit", 30)), 100))
    except (ValueError, TypeError):
        limit = 30
    if not q or len(q) < 2:
        return jsonify({"results": []})
    db = get_db()
    try:
        q_esc = escape_like(q)
        like = f"%{q_esc}%"
        rows = db.execute(
            """SELECT lf.id, lf.filename, lf.rel_path, lf.size_bytes, lf.modified_at,
                      lf.lvl1, lf.lvl2, lf.lvl3, lf.lvl4, lf.lvl5,
                      COUNT(apf.id) AS field_count
               FROM library_files lf
               LEFT JOIN artwork_profiles ap
                      ON ap.master_pdf_path = lf.file_path
                     AND lf.file_path != '' AND lf.file_path IS NOT NULL
               LEFT JOIN artwork_profile_fields apf ON apf.profile_id = ap.id
               WHERE lf.filename LIKE ? ESCAPE '\\' OR lf.rel_path LIKE ? ESCAPE '\\'
               GROUP BY lf.id
               ORDER BY
                 CASE WHEN lf.filename LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END,
                 lf.lvl1, lf.lvl2, lf.lvl3, lf.lvl4, lf.filename
               LIMIT ?""",
            (like, like, like, limit)
        ).fetchall()
        results = []
        for r in rows:
            results.append({
                "id": r["id"],
                "filename": r["filename"],
                "rel_path": r["rel_path"],
                "size": r["size_bytes"],
                "modified": r["modified_at"],
                "field_count": r["field_count"] or 0,
                "breadcrumb": " / ".join(
                    p for p in [r["lvl1"], r["lvl2"], r["lvl3"], r["lvl4"], r["lvl5"]] if p
                ),
            })
    except Exception as e:
        logger.warning(f"library search error: {e}")
        results = []
    finally:
        db.close()
    return jsonify({"results": results, "query": q})


@app.route("/api/library/file/<int:fid>")
@login_required
def api_library_file(fid):
    """Serwuje plik PDF z biblioteki (z dysku)."""
    from flask import send_file as _send_file
    db = get_db()
    try:
        row = db.execute(
            "SELECT filename, file_path FROM library_files WHERE id=?", (fid,)
        ).fetchone()
    finally:
        db.close()
    if not row or not row["file_path"] or not os.path.isfile(row["file_path"]):
        return jsonify({"error": "Plik niedostępny — brak na dysku serwera"}), 404
    real = os.path.realpath(row["file_path"])
    lib_folder = os.path.realpath(app.config.get("LIBRARY_FOLDER", "library"))
    if not real.startswith(lib_folder + os.sep):
        from flask import abort as _abort
        _abort(403)
    return _send_file(
        real,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=row["filename"],
    )


@app.route("/api/library/file/<int:fid>/thumb")
@login_required
def api_library_file_thumb(fid):
    """Serwuje miniaturę (JPEG) pliku z biblioteki."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT thumb_data, filename FROM library_files WHERE id=?", (fid,)
        ).fetchone()
        if not row or not row["thumb_data"]:
            svg = '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="220"><rect width="160" height="220" fill="#f3f4f6"/><text x="80" y="120" text-anchor="middle" font-size="40">&#128196;</text></svg>'
            return Response(svg, mimetype="image/svg+xml")
        return Response(
            row["thumb_data"],
            mimetype="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    finally:
        db.close()


@app.route("/api/library/file/<int:fid>/use", methods=["POST"])
@login_required
@csrf_protect
def api_library_file_use(fid):
    """
    Kopiuje plik z biblioteki (z dysku) do folderu uploads/ i zwraca ścieżkę
    do użycia w porównaniu (zamiast uploadu).
    Jeśli plik nie jest na dysku serwera, zwraca z_path — ścieżkę sieciową.
    """
    import shutil
    db = get_db()
    try:
        row = db.execute(
            "SELECT filename, file_path, z_path FROM library_files WHERE id=?", (fid,)
        ).fetchone()
    finally:
        db.close()

    if not row:
        return jsonify({"error": "Plik nie istnieje w bibliotece"}), 404

    z_path = row["z_path"] or ""
    has_file = bool(row["file_path"] and os.path.isfile(row["file_path"]))

    if not has_file:
        # Thumbnail-only mode — return z_path for the user to open manually
        return jsonify({
            "ok": True,
            "has_file": False,
            "filename": row["filename"],
            "z_path": z_path,
        })

    uid = session["user_id"]
    safe = secure_filename(row["filename"]) or f"lib_{fid}.pdf"
    dest = os.path.join(app.config["UPLOAD_FOLDER"], f"{uid}_lib_{fid}_{safe}")
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    shutil.copy2(row["file_path"], dest)
    return jsonify({
        "ok": True,
        "has_file": True,
        "path": dest,
        "filename": row["filename"],
        "z_path": z_path,
    })


@app.route("/api/library/token-reset", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_library_token_reset():
    """Regeneruje token synchronizacji biblioteki."""
    import secrets as _sec
    token = _sec.token_hex(32)
    db = get_db()
    try:
        db.execute(
            "INSERT INTO settings(category,key,value) "
            "VALUES('library','sync_token',?) "
            "ON CONFLICT(category,key) DO UPDATE SET value=excluded.value", (token,)
        )
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, "token": token})


@app.route("/api/library/stats")
@require_role("manager")
def api_library_stats():
    """Statystyki biblioteki."""
    db = get_db()
    try:
        total = (db.execute("SELECT COUNT(*) FROM library_files").fetchone() or [0])[0]
        with_file = (db.execute(
            "SELECT COUNT(*) FROM library_files WHERE file_path!='' AND file_path IS NOT NULL"
        ).fetchone() or [0])[0]
        last_sync = (db.execute(
            "SELECT MAX(synced_at) FROM library_files"
        ).fetchone() or [None])[0]
        brands = [r[0] for r in db.execute(
            "SELECT DISTINCT lvl1 FROM library_files WHERE lvl1!='' ORDER BY lvl1"
        ).fetchall()]
        try:
            mapped_count = (db.execute(
                """SELECT COUNT(DISTINCT lf.id) FROM library_files lf
                   JOIN artwork_profiles ap ON ap.master_pdf_path = lf.file_path
                   JOIN artwork_profile_fields apf ON apf.profile_id = ap.id
                   WHERE lf.file_path != '' AND lf.file_path IS NOT NULL"""
            ).fetchone() or [0])[0]
        except Exception:
            mapped_count = 0
    except Exception:
        total = with_file = mapped_count = 0; last_sync = None; brands = []
    finally:
        db.close()
    return jsonify({
        "total_files": total, "files_on_disk": with_file,
        "mapped_count": mapped_count,
        "last_sync": last_sync, "brands": brands
    })


# ═══════════════════════════════════════════════════════════════
# REF DATABASE — baza kodów REF i nazw produktów
# ═══════════════════════════════════════════════════════════════

@app.route("/api/ref-database/upload", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_ref_database_upload():
    """Importuje plik Excel (.xlsx/.xls) z kolumnami REF i NAZWA do bazy ref_database."""
    if "file" not in request.files:
        return jsonify({"error": "Brak pliku"}), 400
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"error": "Brak pliku"}), 400
    fname = f.filename.lower()
    if not (fname.endswith(".xlsx") or fname.endswith(".xls")):
        return jsonify({"error": "Wymagany plik .xlsx lub .xls"}), 400

    try:
        import openpyxl
        import io
        data = f.read()
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
    except Exception as e:
        logger.exception("Excel read error: %s", e)
        return jsonify({"error": "Błąd odczytu pliku Excel — sprawdź format i zawartość pliku."}), 400

    if not rows:
        return jsonify({"error": "Plik jest pusty"}), 400

    # Detect header row vs data-only file
    ref_col = 0
    name_col = 1
    start_row = 0
    first = [str(v).strip().upper() if v is not None else "" for v in rows[0]]
    ref_headers = {"REF", "KOD", "KOD REF", "REFCODE"}
    name_headers = {"NAZWA", "NAME", "PRODUCT", "PRODUKT", "OPIS"}
    # Try to find header columns
    for ci, val in enumerate(first):
        if val in ref_headers:
            ref_col = ci
        if val in name_headers:
            name_col = ci
    # If first row looks like a header (contains known keyword), skip it
    if any(v in ref_headers or v in name_headers for v in first):
        start_row = 1

    db = get_db()
    imported = 0
    skipped = 0
    user_id = session.get("user_id")
    try:
        for row in rows[start_row:]:
            if len(row) <= max(ref_col, name_col):
                skipped += 1
                continue
            ref_val = row[ref_col]
            name_val = row[name_col]
            if ref_val is None or name_val is None:
                skipped += 1
                continue
            ref_str = str(ref_val).strip()
            name_str = str(name_val).strip()
            if not ref_str or not name_str:
                skipped += 1
                continue
            db.execute(
                "INSERT INTO ref_database(ref_code, product_name, uploaded_by, uploaded_at) "
                "VALUES(?, ?, ?, datetime('now')) "
                "ON CONFLICT(ref_code) DO UPDATE SET product_name=excluded.product_name, "
                "uploaded_by=excluded.uploaded_by, uploaded_at=excluded.uploaded_at",
                (ref_str, name_str, user_id)
            )
            imported += 1
        db.commit()
    except Exception as e:
        db.rollback()
        logger.exception("DB write error: %s", e)
        return jsonify({"error": "Błąd zapisu — spróbuj ponownie."}), 500
    finally:
        db.close()

    _log_audit("ref_db_upload", detail=f"imported={imported} skipped={skipped}")
    return jsonify({"ok": True, "imported": imported, "skipped": skipped})


@app.route("/api/ref-database/lookup")
def api_ref_database_lookup():
    """Wyszukuje nazwę produktu po kodzie REF (exact + prefix fuzzy match)."""
    if "user_id" not in session:
        return jsonify({"error": "Wymagane logowanie"}), 401
    ref = request.args.get("ref", "").strip()
    if not ref:
        return jsonify({"ref": ref, "name": None})
    db = get_db()
    try:
        # Try exact match first
        row = db.execute(
            "SELECT product_name FROM ref_database WHERE ref_code=?", (ref,)
        ).fetchone()
        if row:
            return jsonify({"ref": ref, "name": row[0]})
        # Prefix match fallback
        ref_esc = escape_like(ref)
        row = db.execute(
            "SELECT product_name FROM ref_database WHERE ref_code LIKE ? ESCAPE '\\' ORDER BY ref_code LIMIT 1",
            (ref_esc + "%",)
        ).fetchone()
        return jsonify({"ref": ref, "name": row[0] if row else None})
    finally:
        db.close()


@app.route("/api/ref-database/stats")
def api_ref_database_stats():
    """Zwraca statystyki bazy REF."""
    if "user_id" not in session:
        return jsonify({"error": "Wymagane logowanie"}), 401
    db = get_db()
    try:
        count_row = db.execute("SELECT COUNT(*) FROM ref_database").fetchone()
        count = count_row[0] if count_row else 0
        last_row = db.execute(
            "SELECT MAX(uploaded_at) FROM ref_database"
        ).fetchone()
        last_uploaded = last_row[0] if last_row else None
    finally:
        db.close()
    return jsonify({"count": count, "last_uploaded": last_uploaded})


@app.route("/api/ref-database", methods=["DELETE"])
@require_role("admin")
@csrf_protect
def api_ref_database_clear():
    """Usuwa wszystkie wpisy z bazy REF (tylko admin)."""
    db = get_db()
    try:
        db.execute("DELETE FROM ref_database")
        db.commit()
    finally:
        db.close()
    _log_audit("ref_db_clear", detail="Wyczyszczono całą bazę REF")
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════
# ARTWORK PROFILES (szablony pól artworka)
# ═══════════════════════════════════════════════════════════════

@app.route("/artwork/profiles")
@require_role("manager")
def artwork_profiles_page():
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        profiles = db.execute(
            "SELECT p.id, p.name, p.description, p.ean, p.ref_code, p.ref_list_json, "
            "p.master_pdf_path, p.page_width_mm, p.page_height_mm, p.is_active, "
            "p.use_count, p.created_at, p.folder_path, u.username as created_by_name, "
            "(p.thumb_b64 IS NOT NULL AND p.thumb_b64 != '') as has_thumb, "
            "(SELECT COUNT(*) FROM artwork_profile_fields f WHERE f.profile_id=p.id AND (f.skip_analysis IS NULL OR f.skip_analysis=0)) as field_count "
            "FROM artwork_profiles p "
            "LEFT JOIN users u ON p.created_by=u.id "
            "ORDER BY p.folder_path, p.name"
        ).fetchall()
        profiles = [dict(r) for r in profiles]
        return render_template("artwork_profiles.html",
                               username=session["username"],
                               role=session["role"],
                               profiles=profiles)
    finally:
        db.close()


# Persist master PDFs on the Render.com /data disk when available.
# On local dev falls back to uploads/masters/ inside the repo.
_DATA_DIR = os.environ.get("DATA_DIR", "/data")
if os.path.isdir(_DATA_DIR):
    MASTERS_DIR = os.path.join(_DATA_DIR, "masters")
else:
    MASTERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads", "masters")
os.makedirs(MASTERS_DIR, exist_ok=True)


def _resolve_master_path(stored_path: str, profile_id: int = None, db=None) -> str:
    """Return a usable path for a stored master PDF.

    Absolute paths break when the deployment root changes between environments.
    Tries multiple candidate directories in order:
      1. stored_path as-is
      2. Current MASTERS_DIR (e.g. /data/masters/ on Render, uploads/masters/ locally)
      3. <app_root>/uploads/masters/  (old default before /data migration)
      4. /app/uploads/masters/        (Render container root before DATA_DIR was set)
    When found at a new path and profile_id given, self-heals the DB record.

    If the caller already holds an open connection it should pass it as ``db`` so
    the self-heal UPDATE reuses it instead of opening a second connection (avoids
    SQLite write-lock contention / potential deadlock).
    """
    if not stored_path:
        return stored_path
    if os.path.exists(stored_path):
        return stored_path
    basename = os.path.basename(stored_path)
    if not basename:
        return stored_path
    _app_root = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(MASTERS_DIR, basename),
        os.path.join(_app_root, "uploads", "masters", basename),
        os.path.join("/app", "uploads", "masters", basename),
        os.path.join("/data", "masters", basename),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            if profile_id:
                if db is not None:
                    # Reuse the caller's open connection — no second connection,
                    # no commit (the caller owns the transaction lifecycle).
                    try:
                        db.execute("UPDATE artwork_profiles SET master_pdf_path=? WHERE id=?",
                                   (candidate, profile_id))
                    except Exception:
                        pass
                else:
                    _db = None
                    try:
                        _db = get_db()
                        _db.execute("UPDATE artwork_profiles SET master_pdf_path=? WHERE id=?",
                                    (candidate, profile_id))
                        _db.commit()
                    except Exception:
                        pass
                    finally:
                        try:
                            if _db:
                                _db.close()
                        except Exception:
                            pass
            return candidate
    return stored_path


_artwork_tables_initialized = False


def _ensure_artwork_profile_tables(db):
    """Tworzy tabele artwork_profiles i artwork_profile_fields jeśli nie istnieją."""
    global _artwork_tables_initialized
    if _artwork_tables_initialized:
        return
    db.execute("""
        CREATE TABLE IF NOT EXISTS artwork_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            ean TEXT DEFAULT '',
            ref_code TEXT DEFAULT '',
            ref_list_json TEXT DEFAULT '[]',
            master_pdf_path TEXT DEFAULT '',
            thumb_b64 TEXT DEFAULT '',
            page_width_mm REAL DEFAULT 0,
            page_height_mm REAL DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_by INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS artwork_profile_fields (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            field_name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            x1_pct REAL NOT NULL,
            y1_pct REAL NOT NULL,
            x2_pct REAL NOT NULL,
            y2_pct REAL NOT NULL,
            severity TEXT DEFAULT 'critical',
            notes TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0
        )
    """)
    _ALLOWED_COL_TYPES = {"TEXT", "INTEGER", "REAL", "BLOB", "NUMERIC"}
    def _safe_add_col(table, col, dtype, dflt):
        if not re.match(r'^[a-z_][a-z0-9_]*$', col) or dtype.upper() not in _ALLOWED_COL_TYPES:
            return
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {dtype} DEFAULT {dflt}")
            # UTRWAL od razu. Bez commitu na PostgreSQL kolejny nieudany ALTER
            # (np. kolumna już istnieje) wywoła rollback CAŁEJ transakcji i cofnie
            # też te kolumny, które właśnie dodaliśmy → „column ... does not exist".
            db.commit()
        except Exception:
            # On PostgreSQL a failed DDL aborts the transaction; rollback to recover.
            try:
                db.rollback()
            except Exception:
                pass
    for col, dflt in [
        ("ref_list_json", "'[]'"),
        ("master_pdf_path", "''"),
        ("thumb_b64", "''"),
        ("use_count", "0"),
        ("folder_path", "''"),
        ("packaging_type", "''"),
        ("revision", "''"),
        ("revision_rank", "0"),
        ("source_path", "''"),
        ("is_current_revision", "1"),
    ]:
        dtype = "INTEGER" if dflt == "0" else "TEXT"
        _safe_add_col("artwork_profiles", col, dtype, dflt)
    # artwork_profile_fields extra columns
    for col, dflt in [("size_label", "''"), ("skip_analysis", "0"), ("display_layout", "'side_by_side'")]:
        dtype = "INTEGER" if dflt == "0" else "TEXT"
        _safe_add_col("artwork_profile_fields", col, dtype, dflt)
    db.execute("""
        CREATE TABLE IF NOT EXISTS artwork_field_dict (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            display_name TEXT NOT NULL UNIQUE,
            default_severity TEXT DEFAULT 'critical',
            display_layout TEXT DEFAULT 'side_by_side',
            default_rotation INTEGER DEFAULT 0,
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    # artwork_field_dict extra columns for existing DBs
    _safe_add_col("artwork_field_dict", "display_layout", "TEXT", "'side_by_side'")
    _safe_add_col("artwork_field_dict", "default_rotation", "INTEGER", "0")
    _safe_add_col("artwork_field_dict", "comparison_mode", "TEXT", "'text'")
    _safe_add_col("artwork_field_dict", "numeric_tolerance", "REAL", "0.0")
    # Źródło zmiany statusu: 'auto' (porównanie/upload) vs 'manual' (ręczne wymuszenie).
    _safe_add_col("kolejka_status_log", "source", "TEXT", "'auto'")
    # Proces/przeznaczenie dostawy: 'magazyn' (Magazyn Centralny Radom) | 'tranzyt' | 'inne'
    # + kraj tranzytu (gdy 'tranzyt') z listy master „Państwa tranzytowe".
    _safe_add_col("kolejka_zlecenia", "destination_type", "TEXT", "'magazyn'")
    _safe_add_col("kolejka_zlecenia", "transit_country", "TEXT", "''")
    # ── Material master tables ────────────────────────────────────────────
    db.execute("""
        CREATE TABLE IF NOT EXISTS artwork_pkg_level_type (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            label TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            color TEXT DEFAULT '#6b7280',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS artwork_material (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ref_code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            product_family TEXT DEFAULT '',
            ean TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS artwork_material_level (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            material_id INTEGER NOT NULL REFERENCES artwork_material(id) ON DELETE CASCADE,
            level_type_id INTEGER NOT NULL REFERENCES artwork_pkg_level_type(id),
            profile_id INTEGER REFERENCES artwork_profiles(id),
            mapping_type TEXT DEFAULT 'none',
            source_material_id INTEGER REFERENCES artwork_material(id),
            notes TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(material_id, level_type_id)
        )
    """)
    # Seed default level types if table is empty
    count = db.execute("SELECT COUNT(*) as n FROM artwork_pkg_level_type").fetchone()["n"]
    if count == 0:
        for code, label, sort_order, color in [
            ("sztuka", "Sztuka (JED)", 1, "#10b981"),
            ("op",     "Opakowanie (OP)", 2, "#3b82f6"),
            ("opz",    "OPZ",         3, "#8b5cf6"),
            ("karton", "Karton (KAR)", 4, "#f59e0b"),
        ]:
            db.execute(
                "INSERT OR IGNORE INTO artwork_pkg_level_type (code, label, sort_order, color) VALUES (?, ?, ?, ?)",
                (code, label, sort_order, color)
            )
    # ── Operator feedback / learning tables ──────────────────────────────
    db.execute("""
        CREATE TABLE IF NOT EXISTS artwork_field_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            field_name TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            profile_name TEXT DEFAULT '',
            queue_id TEXT DEFAULT '',
            comparison_mode TEXT DEFAULT 'text',
            val_a TEXT DEFAULT '',
            val_b TEXT DEFAULT '',
            was_changed INTEGER DEFAULT 0,
            operator_verdict TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    db.commit()
    _artwork_tables_initialized = True


def _master_thumb(img, max_w=220) -> str:
    """Tworzy miniaturę mastera jako base64 JPEG."""
    import io as _io, base64 as _b64
    w, h = img.size
    if w > max_w:
        img = img.resize((max_w, int(h * max_w / w)), img.LANCZOS if hasattr(img, 'LANCZOS') else 1)
    buf = _io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return _b64.b64encode(buf.getvalue()).decode()


def _ai_detect_fields_from_b64(img_b64: str, mime: str = "image/jpeg") -> list:
    """Wywołuje Claude Vision na obrazie b64 i zwraca listę pól."""
    import json as _json
    from artwork_comparator import _get_api_key_artwork
    import anthropic
    api_key = _get_api_key_artwork()
    if not api_key:
        return []
    client = anthropic.Anthropic(api_key=api_key, timeout=120.0)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": img_b64}},
            {"type": "text", "text": (
                "Analyze this medical packaging artwork. Return ONLY a JSON array (no markdown).\n"
                'Each object: {"field_name":"snake_case","display_name":"Polish label",'
                '"x1_pct":5.2,"y1_pct":3.1,"x2_pct":45.8,"y2_pct":12.4,'
                '"severity":"critical|important|info","notes":"desc"}\n\n'
                "Coordinates = % of image (0,0=top-left). Crop tightly.\n"
                "critical: EAN barcode number, REF/catalog number, product name, CE/MDR+notified body, "
                "EACH size table row separately (XS row, S row, M row, L row, XL row), quantity, sterility\n"
                "important: each icon/pictogram, translation blocks by language group, AQL, storage\n"
                "info: manufacturer address, importer, website\n"
                "Return 15-45 fields. Be precise."
            )}
        ]}]
    )
    raw = (response.content[0].text if response.content and hasattr(response.content[0], "text") else "").strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
    raw = raw.strip()
    try:
        fields = _json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(fields, list):
        return []
    clean = []
    for f in fields:
        if not all(k in f for k in ("x1_pct","y1_pct","x2_pct","y2_pct")):
            continue
        try:
            _x1 = float(f["x1_pct"]); _y1 = float(f["y1_pct"])
            _x2 = float(f["x2_pct"]); _y2 = float(f["y2_pct"])
        except (TypeError, ValueError):
            continue   # pojedyncze złe pole z AI nie może wywalić całej partii
        sev = f.get("severity","important")
        clean.append({
            "field_name":   str(f.get("field_name","field")),
            "display_name": str(f.get("display_name", f.get("field_name","Pole"))),
            "x1_pct": max(0.0, min(99.0, _x1)),
            "y1_pct": max(0.0, min(99.0, _y1)),
            "x2_pct": max(1.0, min(100.0, _x2)),
            "y2_pct": max(1.0, min(100.0, _y2)),
            "severity": sev if sev in ("critical","important","info") else "important",
            "notes": str(f.get("notes","")),
        })
    return clean


_VALID_FIELD_SEVERITIES = {"critical", "important", "info", "warning"}

def _save_profile_fields(db, pid, fields):
    if not fields:
        return
    db.execute("DELETE FROM artwork_profile_fields WHERE profile_id=?", (pid,))
    for i, f in enumerate(fields):
        if not all(k in f for k in ("x1_pct", "y1_pct", "x2_pct", "y2_pct")):
            continue
        try:
            x1 = float(f["x1_pct"]); y1 = float(f["y1_pct"])
            x2 = float(f["x2_pct"]); y2 = float(f["y2_pct"])
        except (TypeError, ValueError):
            continue
        # Clamp to valid percentage range
        x1 = max(0.0, min(99.0, x1)); y1 = max(0.0, min(99.0, y1))
        x2 = max(1.0, min(100.0, x2)); y2 = max(1.0, min(100.0, y2))
        # Ensure x1 < x2 and y1 < y2 (non-zero-area region)
        if x1 >= x2: x2 = min(100.0, x1 + 1.0)
        if y1 >= y2: y2 = min(100.0, y1 + 1.0)
        sev = f.get("severity", "critical")
        if sev not in _VALID_FIELD_SEVERITIES:
            sev = "critical"
        db.execute(
            "INSERT INTO artwork_profile_fields "
            "(profile_id, field_name, display_name, x1_pct, y1_pct, x2_pct, y2_pct, "
            "severity, notes, sort_order, size_label, skip_analysis) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, str(f.get("field_name") or f.get("display_name") or "field")[:200],
             str(f.get("display_name", "") or "")[:200], x1, y1, x2, y2,
             sev, str(f.get("notes", "") or "")[:500], i,
             str(f.get("size_label", "") or "")[:50], 1 if f.get("skip_analysis") else 0)
        )


@app.route("/api/artwork/masters/upload", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_masters_bulk_upload():
    """
    Bulk upload masterów artworków.
    Dla każdego pliku PDF:
      1. Zapisuje do uploads/masters/
      2. Renderuje 300 DPI, tworzy miniaturę
      3. Ekstraktuje EAN/REF przez OCR
      4. Wywołuje Claude AI → wykrywa pola z bboxami
      5. Zapisuje profil z przypiętym masterem i polami
    Zwraca listę wyników per plik.
    """
    import base64 as _b64, io as _io, hashlib as _hs, json as _json, re as _re
    from artwork_comparator import _load_pages, _PROFILE_RENDER_DPI, _extract_text_ocr, _extract_ean
    from artwork_naming import parse_master_filename

    files = request.files.getlist("files[]")
    if not files:
        return jsonify({"error": "Brak plików"}), 400
    # Optional per-file original paths (np. Z:\...) aligned with files[] — used by
    # the local bulk uploader to preserve the source location for later reference.
    source_paths = request.form.getlist("source_paths[]")

    raw_folder = (request.form.get("folder_path") or "").strip().strip("/")
    # Filter out ".." and "." to prevent path-traversal strings being stored
    # in artwork_profiles.folder_path and later used in filesystem operations.
    upload_folder = "/".join(
        p.strip() for p in raw_folder.replace("\\", "/").split("/")
        if p.strip() and p.strip() not in ("..", ".")
    )

    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
    except Exception:
        pass
    results = []

    try:
        for _fi, file in enumerate(files):
            fname = file.filename or "unknown.pdf"
            result = {"filename": fname, "status": "error", "error": "", "pid": None, "field_count": 0}
            try:
                # Size guard before reading into RAM
                file.seek(0, 2)
                fsize_mb = file.tell() / 1024 / 1024
                file.seek(0)
                if fsize_mb > 150:
                    raise ValueError(f"Plik zbyt duży ({fsize_mb:.0f} MB). Maksimum 150 MB.")
                # Save master PDF to persistent directory
                raw = file.read()
                h = _hs.md5(raw, usedforsecurity=False).hexdigest()[:12]
                safe = _re.sub(r'[^\w\-.]', '_', os.path.splitext(fname)[0])[:40]
                pdf_path = os.path.join(MASTERS_DIR, f"{safe}_{h}.pdf")
                with open(pdf_path, "wb") as fout:
                    fout.write(raw)

                # Render at 300 DPI
                pages = _load_pages(pdf_path, _PROFILE_RENDER_DPI, page_idx=None)
                if not pages:
                    raise ValueError("Nie można wyrenderować PDF")
                img = pages[0]

                # Thumbnail
                thumb = _master_thumb(img)

                # Extract EAN/REF via OCR
                ocr_text = _extract_text_ocr(img)
                ean_val = _extract_ean(ocr_text) or ""

                # Parse REF + packaging type + revision from the ACME filename
                parsed = parse_master_filename(fname)
                ref_from_name = parsed["ref"] or safe
                pkg_type = parsed["packaging_type"]
                rev_str = parsed["revision"]
                rev_rank = parsed["revision_rank"]
                if not ean_val and parsed["ean"]:
                    ean_val = parsed["ean"]
                src_path = (source_paths[_fi] if _fi < len(source_paths) else "")[:500]

                # No AI on upload — user maps fields manually in the editor
                fields = []

                # Check if profile with same master PDF already exists → update
                existing = db.execute(
                    "SELECT id FROM artwork_profiles WHERE master_pdf_path=?", (pdf_path,)
                ).fetchone()

                profile_name = os.path.splitext(fname)[0]
                ref_list = [ref_from_name] if ref_from_name else []

                if existing:
                    pid = existing["id"]
                    db.execute(
                        "UPDATE artwork_profiles SET name=?, ean=?, ref_code=?, ref_list_json=?, "
                        "thumb_b64=?, folder_path=?, packaging_type=?, revision=?, revision_rank=?, "
                        "source_path=?, updated_at=datetime('now') WHERE id=?",
                        (profile_name, ean_val, ref_from_name,
                         _json.dumps(ref_list, ensure_ascii=False), thumb, upload_folder,
                         pkg_type, rev_str, rev_rank, src_path, pid)
                    )
                    db.commit()
                    # Do NOT call _save_profile_fields here — fields=[] always on upload,
                    # and _save_profile_fields DELETEs all existing fields before inserting.
                    # Calling it would silently wipe every manually-mapped bbox field.
                else:
                    cur = db.execute(
                        "INSERT INTO artwork_profiles "
                        "(name, ean, ref_code, ref_list_json, master_pdf_path, thumb_b64, "
                        "folder_path, packaging_type, revision, revision_rank, source_path, "
                        "is_active, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)",
                        (profile_name, ean_val, ref_from_name,
                         _json.dumps(ref_list, ensure_ascii=False),
                         pdf_path, thumb, upload_folder,
                         pkg_type, rev_str, rev_rank, src_path, session["user_id"])
                    )
                    pid = cur.lastrowid
                    # Single commit AFTER _save_profile_fields so both writes are atomic.
                    # (Previously db.commit() was called here, before _save_profile_fields,
                    # which orphaned the artwork_profiles row when _save_profile_fields raised.)
                    if fields:
                        _save_profile_fields(db, pid, fields)
                    db.commit()

                result.update({
                    "status": "ok", "pid": pid,
                    "field_count": len(fields),
                    "ean": ean_val, "ref": ref_from_name,
                    "packaging_type": pkg_type, "revision": rev_str,
                    "thumb_b64": thumb,
                })
            except Exception as e:
                # Rollback on EVERY per-file error so PostgreSQL doesn't leave
                # the shared db connection in an aborted-transaction state,
                # which would cause every subsequent file in the loop to fail
                # with "InFailedSqlTransaction" even if the file itself is valid.
                try:
                    db.rollback()
                except Exception:
                    pass
                result["error"] = str(e)[:200]
            results.append(result)
    finally:
        db.close()
    ok = sum(1 for r in results if r["status"] == "ok")
    return jsonify({"ok": True, "total": len(results), "success": ok, "results": results})


@app.route("/api/artwork/profiles", methods=["GET"])
@login_required
def api_artwork_profiles_list():
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        rows = db.execute(
            "SELECT p.id, p.name, p.description, p.ean, p.ref_code, p.ref_list_json, "
            "p.master_pdf_path, p.page_width_mm, p.page_height_mm, p.is_active, "
            "p.use_count, p.created_at, p.folder_path, "
            "(p.thumb_b64 IS NOT NULL AND p.thumb_b64 != '') as has_thumb, "
            "(SELECT COUNT(*) FROM artwork_profile_fields f WHERE f.profile_id=p.id AND (f.skip_analysis IS NULL OR f.skip_analysis=0)) as field_count "
            "FROM artwork_profiles p ORDER BY p.folder_path, p.name"
        ).fetchall()
        field_rows = db.execute(
            "SELECT profile_id, display_name FROM artwork_profile_fields "
            "WHERE (skip_analysis IS NULL OR skip_analysis=0) ORDER BY profile_id, sort_order"
        ).fetchall()
        fields_by_pid = {}
        for fr in field_rows:
            fields_by_pid.setdefault(fr["profile_id"], []).append(fr["display_name"])
        result = []
        for r in rows:
            d = dict(r)
            d["field_names"] = fields_by_pid.get(r["id"], [])
            result.append(d)
        return jsonify(result)
    finally:
        db.close()


@app.route("/api/artwork/profiles/<int:pid>/canvas")
@require_role("manager")
def api_artwork_profile_canvas(pid):
    """Renderuje zapisany PDF mastera w wysokiej jakości do edytora.
    Gdy plik PDF nie istnieje na dysku (np. po rebuildzie kontenera),
    zwraca zapisaną miniaturę z bazy danych jako fallback.
    Gdy miniatura jest pusta ale PDF istnieje, auto-zapisuje miniaturę.
    """
    import io as _io, base64 as _b64c
    from artwork_comparator import _load_pages, _PROFILE_RENDER_DPI
    from flask import Response

    db = get_db()
    try:
        row = db.execute(
            "SELECT master_pdf_path, thumb_b64 FROM artwork_profiles WHERE id=?", (pid,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono profilu"}), 404

        # Try rendering from PDF file
        pdf_path = row["master_pdf_path"] or ""
        if pdf_path:
            resolved = _resolve_master_path(pdf_path, profile_id=pid, db=db)
            if os.path.isfile(resolved):
                # Adaptive DPI: ensure minimum 2000px on the longer side for sharp display
                render_dpi = _PROFILE_RENDER_DPI
                try:
                    import fitz as _fitz_canvas
                    _cdoc = _fitz_canvas.open(resolved)
                    _cp = _cdoc[0]
                    _max_pts = max(_cp.rect.width, _cp.rect.height, 1)
                    _cdoc.close()
                    render_dpi = min(900, max(_PROFILE_RENDER_DPI, int(2000 * 72 / _max_pts)))
                except Exception:
                    pass
                pages = _load_pages(resolved, render_dpi, page_idx=None)
                if pages:
                    buf = _io.BytesIO()
                    pages[0].save(buf, "JPEG", quality=92)
                    img_bytes = buf.getvalue()
                    # Auto-save thumbnail when missing so future fallbacks work
                    if not (row["thumb_b64"] or ""):
                        try:
                            thumb = _master_thumb(pages[0])
                            db.execute(
                                "UPDATE artwork_profiles SET thumb_b64=? WHERE id=?",
                                (thumb, pid)
                            )
                            db.commit()
                        except Exception:
                            pass
                    return Response(img_bytes, mimetype="image/jpeg",
                                    headers={"Cache-Control": "private, max-age=300"})

        # PDF missing or unrenderable — fall back to stored thumbnail
        thumb_b64 = row["thumb_b64"] or ""
        if thumb_b64:
            try:
                img_bytes = _b64c.b64decode(thumb_b64)
            except Exception:
                return jsonify({"error": "Miniatura uszkodzona. Załaduj plik PDF ponownie."}), 404
            return Response(img_bytes, mimetype="image/jpeg",
                            headers={"Cache-Control": "public, max-age=3600",
                                     "X-Canvas-Fallback": "thumb"})

        return jsonify({"error": "Brak pliku PDF i miniatury. Załaduj plik PDF."}), 404
    finally:
        db.close()


@app.route("/api/artwork/masters/download/<int:pid>")
@require_role("manager")
def api_artwork_master_download(pid):
    """Pobierz plik PDF mastera."""
    from flask import send_file
    db = get_db()
    try:
        row = db.execute("SELECT master_pdf_path, name FROM artwork_profiles WHERE id=?", (pid,)).fetchone()
        if not row or not row["master_pdf_path"]:
            return jsonify({"error": "Brak pliku"}), 404
        path = _resolve_master_path(row["master_pdf_path"])
        if not os.path.isfile(path):
            return jsonify({"error": "Plik nie istnieje na dysku"}), 404
        real_path = os.path.realpath(path)
        real_masters = os.path.realpath(MASTERS_DIR)
        if not real_path.startswith(real_masters + os.sep):
            return jsonify({"error": "Niedozwolona ścieżka pliku"}), 403
        safe_name = (row["name"] or "master").replace("/", "_")[:60] + ".pdf"
        return send_file(path, mimetype="application/pdf",
                         as_attachment=True, download_name=safe_name)
    finally:
        db.close()


@app.route("/api/artwork/masters/storage-info")
@require_role("manager")
def api_artwork_masters_storage_info():
    """Zwraca informacje o zużyciu dysku przez mastery."""
    import shutil
    info = {"dir": MASTERS_DIR, "files": [], "total_bytes": 0, "total_mb": 0, "disk_free_gb": None}
    try:
        if os.path.isdir(MASTERS_DIR):
            for fname in os.listdir(MASTERS_DIR):
                fpath = os.path.join(MASTERS_DIR, fname)
                if os.path.isfile(fpath):
                    sz = os.path.getsize(fpath)
                    info["files"].append({"name": fname, "size_bytes": sz, "size_mb": round(sz/1024/1024, 2)})
                    info["total_bytes"] += sz
        info["total_mb"] = round(info["total_bytes"] / 1024 / 1024, 1)
        try:
            usage = shutil.disk_usage(MASTERS_DIR)
            info["disk_free_gb"] = round(usage.free / 1024**3, 2)
            info["disk_total_gb"] = round(usage.total / 1024**3, 2)
            info["disk_used_gb"] = round(usage.used / 1024**3, 2)
        except Exception:
            pass
    except Exception as e:
        info["error"] = str(e)[:200]
    return jsonify(info)


@app.route("/api/artwork/profiles/<int:pid>/thumb")
@login_required
def api_artwork_profile_thumb(pid):
    db = get_db()
    try:
        row = db.execute("SELECT thumb_b64 FROM artwork_profiles WHERE id=?", (pid,)).fetchone()
        if not row or not row["thumb_b64"]:
            return "", 404
        import base64 as _b64
        try:
            img_bytes = _b64.b64decode(row["thumb_b64"])
        except Exception:
            return "", 404
        from flask import Response
        return Response(img_bytes, mimetype="image/jpeg",
                        headers={"Cache-Control": "public, max-age=3600"})
    finally:
        db.close()


@app.route("/api/artwork/profiles/<int:pid>/move", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_profile_move(pid):
    """Przenosi master do innego folderu."""
    data = request.get_json(silent=True) or {}
    folder = str(data.get("folder_path") or "").strip().strip("/")[:500]
    folder = "/".join(p.strip() for p in folder.replace("\\", "/").split("/") if p.strip())
    db = get_db()
    try:
        db.execute("UPDATE artwork_profiles SET folder_path=?, updated_at=datetime('now') WHERE id=?",
                   (folder, pid))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/profiles/<int:pid>", methods=["GET"])
@login_required
def api_artwork_profile_get(pid):
    db = get_db()
    try:
        row = db.execute("SELECT * FROM artwork_profiles WHERE id=?", (pid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        profile = dict(row)
        fields = db.execute(
            "SELECT * FROM artwork_profile_fields WHERE profile_id=? ORDER BY sort_order",
            (pid,)
        ).fetchall()
        profile["fields"] = [dict(f) for f in fields]
        return jsonify(profile)
    finally:
        db.close()


@app.route("/api/artwork/profiles", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_profile_create():
    data = request.get_json(force=True, silent=True)
    err = _validate_json_body(data, {"name": (str, True)})
    if err:
        return jsonify({"error": err}), 400
    data = data or {}
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Nazwa profilu jest wymagana"}), 400
    import json as _json
    ref_list = data.get("ref_list", [])
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        try:
            _pw = float(data.get("page_width_mm") or 0)
            _ph = float(data.get("page_height_mm") or 0)
        except (TypeError, ValueError):
            _pw = _ph = 0.0
        _pw = max(0.0, min(5000.0, _pw))
        _ph = max(0.0, min(5000.0, _ph))
        _cur = db.execute(
            "INSERT INTO artwork_profiles (name, description, ean, ref_code, ref_list_json, "
            "page_width_mm, page_height_mm, is_active, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (name[:200], str(data.get("description") or "")[:500], str(data.get("ean") or "")[:20],
             str(data.get("ref_code") or "")[:100], _json.dumps(ref_list, ensure_ascii=False),
             _pw, _ph, session["user_id"])
        )
        pid = _cur.lastrowid
        _save_profile_fields(db, pid, data.get("fields", []))
        db.commit()
        return jsonify({"ok": True, "id": pid})
    finally:
        db.close()


@app.route("/api/artwork/profiles/import-csv", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_profiles_import_csv():
    """Import profili artworków z pliku CSV.

    Wymagane kolumny CSV: name, ean, ref_code
    Opcjonalne: description, folder_path

    Zwraca liczbę zaimportowanych i pominiętych (duplikatów) profili.
    """
    import csv, io as _io
    file = request.files.get("file")
    if not file or not (file.filename or "").lower().endswith(".csv"):
        return jsonify({"error": "Wymagany plik CSV (.csv)"}), 400
    try:
        content = file.read().decode("utf-8-sig")  # handle BOM
    except UnicodeDecodeError:
        try:
            file.stream.seek(0)
            content = file.read().decode("cp1250")
        except Exception:
            return jsonify({"error": "Nie można odczytać pliku — sprawdź kodowanie (UTF-8 lub CP1250)"}), 400

    reader = csv.DictReader(_io.StringIO(content))
    if "name" not in (reader.fieldnames or []):
        return jsonify({"error": "Brak wymaganej kolumny 'name' w CSV"}), 400

    db = get_db()
    created, skipped = 0, 0
    errors = []
    try:
        _ensure_artwork_profile_tables(db)
        for i, row in enumerate(reader, 2):
            name = (row.get("name") or "").strip()
            if not name:
                skipped += 1
                continue
            ean = (row.get("ean") or "").strip()
            ref_code = (row.get("ref_code") or "").strip()
            description = (row.get("description") or "").strip()
            folder_path = (row.get("folder_path") or "").strip()
            existing = db.execute(
                "SELECT id FROM artwork_profiles WHERE name=?", (name,)
            ).fetchone()
            if existing:
                skipped += 1
                continue
            try:
                db.execute(
                    "INSERT INTO artwork_profiles (name, description, ean, ref_code, "
                    "is_active, created_by, folder_path) VALUES (?,?,?,?,1,?,?)",
                    (name[:200], description[:500], ean[:20], ref_code[:100],
                     session["user_id"], folder_path[:500])
                )
                created += 1
            except Exception as _e:
                errors.append(f"Wiersz {i}: {str(_e)[:200]}")
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, "created": created, "skipped": skipped,
                    "errors": errors[:10]})


@app.route("/api/artwork/profiles/<int:pid>", methods=["PUT"])
@require_role("manager")
@csrf_protect
def api_artwork_profile_update(pid):
    data = request.get_json(force=True, silent=True) or {}
    import json as _json
    ref_list = data.get("ref_list", [])
    uid  = session["user_id"]
    uname = session["username"]
    db = get_db()
    try:
        row = db.execute("SELECT * FROM artwork_profiles WHERE id=?", (pid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        name = str(data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Nazwa profilu jest wymagana"}), 400
        folder = str(data.get("folder_path") or "").strip().strip("/")
        folder = "/".join(p.strip() for p in folder.replace("\\", "/").split("/") if p.strip())
        # Save history snapshot before overwriting
        try:
            fields_rows = db.execute(
                "SELECT * FROM artwork_profile_fields WHERE profile_id=? ORDER BY id", (pid,)
            ).fetchall()
            snapshot = {
                "profile": dict(row),
                "fields": [dict(f) for f in fields_rows],
            }
            db.execute(
                "INSERT INTO artwork_profile_history(profile_id, action, changed_by, changed_by_name, snapshot_json)"
                " VALUES(?,?,?,?,?)",
                (pid, "update", uid, uname, _json.dumps(snapshot, ensure_ascii=False, default=str))
            )
        except Exception:
            pass
        try:
            _pw2 = float(data.get("page_width_mm") or 0)
            _ph2 = float(data.get("page_height_mm") or 0)
        except (TypeError, ValueError):
            _pw2 = _ph2 = 0.0
        _pw2 = max(0.0, min(5000.0, _pw2))
        _ph2 = max(0.0, min(5000.0, _ph2))
        db.execute(
            "UPDATE artwork_profiles SET name=?, description=?, ean=?, ref_code=?, "
            "ref_list_json=?, page_width_mm=?, page_height_mm=?, is_active=?, "
            "folder_path=?, updated_at=datetime('now') WHERE id=?",
            (name[:200], str(data.get("description") or "")[:500], str(data.get("ean") or "")[:20],
             str(data.get("ref_code") or "")[:100], _json.dumps(ref_list, ensure_ascii=False),
             _pw2, _ph2,
             (1 if data.get("is_active", 1) else 0), folder[:500], pid)
        )
        if "fields" in data:
            _save_profile_fields(db, pid, data["fields"])
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/profiles/<int:pid>/history")
@require_role("manager")
def api_artwork_profile_history(pid):
    """Return audit trail for an artwork profile (last 50 revisions)."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, action, changed_by_name, created_at FROM artwork_profile_history"
            " WHERE profile_id=? ORDER BY created_at DESC LIMIT 50",
            (pid,)
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@app.route("/api/artwork/profiles/<int:pid>/history/<int:hid>")
@require_role("manager")
def api_artwork_profile_history_snapshot(pid, hid):
    """Return the full snapshot JSON for a specific history entry."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT snapshot_json, created_at, changed_by_name, action"
            " FROM artwork_profile_history WHERE id=? AND profile_id=?",
            (hid, pid)
        ).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        try:
            snapshot = json.loads(row["snapshot_json"] or "{}")
        except Exception:
            snapshot = {}
        return jsonify({
            "snapshot": snapshot,
            "created_at": row["created_at"],
            "changed_by_name": row["changed_by_name"],
            "action": row["action"],
        })
    finally:
        db.close()


@app.route("/api/artwork/profiles/ai-detect", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_profile_ai_detect():
    """Wysyła artworka do Claude Vision i zwraca AI-sugerowane pola z bboxami."""
    import base64 as _b64, io as _io, json as _json
    from artwork_comparator import _load_pages, _PROFILE_RENDER_DPI, _get_api_key_artwork

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "Brak pliku PDF"}), 400

    api_key = _get_api_key_artwork()
    if not api_key:
        return jsonify({"error": "Brak klucza API Claude"}), 400

    uid = session["user_id"]
    import uuid as _uuid
    tmp = os.path.join(app.config["UPLOAD_FOLDER"], f"_ai_detect_{uid}_{_uuid.uuid4().hex}.pdf")
    try:
        file.save(tmp)
        pages = _load_pages(tmp, _PROFILE_RENDER_DPI, page_idx=None)
        if not pages:
            return jsonify({"error": "Nie można wyrenderować PDF"}), 400

        buf = _io.BytesIO()
        pages[0].save(buf, "JPEG", quality=85)
        img_b64 = _b64.b64encode(buf.getvalue()).decode()

        import anthropic
        client = anthropic.Anthropic(api_key=api_key, timeout=120.0)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg",
                                   "data": img_b64},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Analyze this medical packaging artwork and identify ALL fields requiring quality control verification.\n\n"
                            "Return ONLY a valid JSON array (no markdown, no code blocks). Each object:\n"
                            "{\n"
                            '  "field_name": "snake_case_id",\n'
                            '  "display_name": "Polish label",\n'
                            '  "x1_pct": 5.2,\n'
                            '  "y1_pct": 3.1,\n'
                            '  "x2_pct": 45.8,\n'
                            '  "y2_pct": 12.4,\n'
                            '  "severity": "critical|important|info",\n'
                            '  "notes": "brief description"\n'
                            "}\n\n"
                            "Coordinates are % of full image (0,0=top-left, 100,100=bottom-right). Crop tightly.\n\n"
                            "Severity: critical=EAN/REF/CE/rozmiary/sterylność/ilość, important=tłumaczenia/ikony/specs, info=adres/www\n\n"
                            "Identify individually:\n"
                            "- EAN-13 barcode number area\n"
                            "- REF/catalog number\n"
                            "- Product name\n"
                            "- CE/MDR marking with notified body number\n"
                            "- EACH row of size table (XS/S/M/L/XL with values) — separate field per row\n"
                            "- Quantity/count\n"
                            "- Sterility/single-use symbols\n"
                            "- Each icon/pictogram separately\n"
                            "- Translation blocks by language group\n"
                            "- Manufacturer block\n"
                            "- Importer block\n\n"
                            "Be precise. Return 15-40 fields typical for medical glove packaging."
                        )
                    }
                ]
            }]
        )

        raw = (response.content[0].text if response.content and hasattr(response.content[0], "text") else "").strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        fields = _json.loads(raw)
        if not isinstance(fields, list):
            raise ValueError("Not a list")

        # Validate and clamp all coordinates
        clean = []
        for f in fields:
            if not all(k in f for k in ("x1_pct", "y1_pct", "x2_pct", "y2_pct")):
                continue
            try:
                _x1 = max(0.0, min(99.0, float(f["x1_pct"])))
                _y1 = max(0.0, min(99.0, float(f["y1_pct"])))
                _x2 = max(1.0, min(100.0, float(f["x2_pct"])))
                _y2 = max(1.0, min(100.0, float(f["y2_pct"])))
            except (TypeError, ValueError):
                continue
            if _x1 >= _x2: _x2 = min(100.0, _x1 + 1.0)
            if _y1 >= _y2: _y2 = min(100.0, _y1 + 1.0)
            clean.append({
                "field_name":   str(f.get("field_name", "field")),
                "display_name": str(f.get("display_name", f.get("field_name", "Pole"))),
                "x1_pct": _x1, "y1_pct": _y1, "x2_pct": _x2, "y2_pct": _y2,
                "severity": f.get("severity", "important") if f.get("severity") in ("critical","important","info") else "important",
                "notes":    str(f.get("notes", "")),
            })
        return jsonify({"ok": True, "fields": clean, "count": len(clean)})

    except Exception as e:
        import traceback as _tb
        app.logger.error("AI detect (PDF) error: %s", _tb.format_exc())
        return jsonify({"error": "Błąd modułu AI — spróbuj ponownie."}), 500
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


@app.route("/api/artwork/profiles/ai-detect-image", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_profile_ai_detect_image():
    """Przyjmuje obraz JPEG (z canvas) i zwraca AI-sugerowane pola."""
    import base64 as _b64, json as _json
    from artwork_comparator import _get_api_key_artwork

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "Brak pliku"}), 400

    api_key = _get_api_key_artwork()
    if not api_key:
        return jsonify({"error": "Brak klucza API Claude"}), 400

    try:
        img_bytes = file.read()
        img_b64 = _b64.b64encode(img_bytes).decode()
        # Detect mime type
        mime = "image/jpeg" if img_bytes[:2] == b'\xff\xd8' else "image/png"

        import anthropic
        client = anthropic.Anthropic(api_key=api_key, timeout=120.0)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": img_b64}},
                    {"type": "text", "text": (
                        "Analyze this medical packaging artwork and identify ALL fields requiring quality control verification.\n\n"
                        "Return ONLY a valid JSON array (no markdown). Each object:\n"
                        '{"field_name":"snake_case","display_name":"Polish label",'
                        '"x1_pct":5.2,"y1_pct":3.1,"x2_pct":45.8,"y2_pct":12.4,'
                        '"severity":"critical|important|info","notes":"brief desc"}\n\n'
                        "Coordinates: % of image (0,0=top-left, 100,100=bottom-right). Crop tightly.\n"
                        "critical=EAN/REF/CE mark/rozmiary/sterylność/ilość\n"
                        "important=tłumaczenia/ikony/specs\ninfo=adres/www\n\n"
                        "Identify individually:\n"
                        "- EAN-13 barcode digits area\n- REF/catalog number\n- Product name\n"
                        "- CE/MDR marking + notified body number\n"
                        "- EACH size table row separately (XS row, S row, M row, L row, XL row)\n"
                        "- Quantity/count (e.g. 100 szt)\n- Sterility symbol\n- Single-use symbol\n"
                        "- Latex-free / AQL icon areas\n- Storage conditions icon\n"
                        "- Translation blocks (group by language family: EN/DE/FR, PL/CZ/SK, etc.)\n"
                        "- Manufacturer block\n- Importer block\n\n"
                        "Return 15-45 fields. Be precise with coordinates."
                    )}
                ]
            }]
        )

        raw = (response.content[0].text if response.content and hasattr(response.content[0], "text") else "").strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        raw = raw.strip().strip("` \n")

        fields = _json.loads(raw)
        if not isinstance(fields, list):
            raise ValueError("Response is not a list")

        clean = []
        for f in fields:
            if not all(k in f for k in ("x1_pct","y1_pct","x2_pct","y2_pct")):
                continue
            try:
                _x1 = max(0.0, min(99.0, float(f["x1_pct"])))
                _y1 = max(0.0, min(99.0, float(f["y1_pct"])))
                _x2 = max(1.0, min(100.0, float(f["x2_pct"])))
                _y2 = max(1.0, min(100.0, float(f["y2_pct"])))
            except (TypeError, ValueError):
                continue   # pojedyncze złe pole z AI nie może wywalić całej partii
            sev = f.get("severity","important")
            clean.append({
                "field_name":   str(f.get("field_name","field")),
                "display_name": str(f.get("display_name", f.get("field_name","Pole"))),
                "x1_pct": _x1,
                "y1_pct": _y1,
                "x2_pct": _x2,
                "y2_pct": _y2,
                "severity": sev if sev in ("critical","important","info") else "important",
                "notes": str(f.get("notes","")),
            })
        return jsonify({"ok": True, "fields": clean, "count": len(clean)})

    except Exception as e:
        import traceback as _tb
        app.logger.error("AI detect (image) error: %s", _tb.format_exc())
        return jsonify({"error": "Błąd modułu AI — spróbuj ponownie."}), 500


@app.route("/api/artwork/profiles/<int:pid>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_artwork_profile_delete(pid):
    db = get_db()
    try:
        db.execute("DELETE FROM artwork_profile_fields WHERE profile_id=?", (pid,))
        db.execute("DELETE FROM artwork_profiles WHERE id=?", (pid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/profiles/<int:pid>/duplicate", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_profile_duplicate(pid):
    """Duplikuje profil artworku wraz z polami — nowy profil otrzymuje suffix '(kopia)'."""
    import json as _json
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        src = db.execute("SELECT * FROM artwork_profiles WHERE id=?", (pid,)).fetchone()
        if not src:
            return jsonify({"error": "Profil nie istnieje"}), 404
        fields = db.execute(
            "SELECT * FROM artwork_profile_fields WHERE profile_id=? ORDER BY sort_order", (pid,)
        ).fetchall()
        new_name = (src["name"] or "") + " (kopia)"
        _dup_cur = db.execute(
            "INSERT INTO artwork_profiles (name, description, ean, ref_code, ref_list_json, "
            "master_pdf_path, thumb_b64, page_width_mm, page_height_mm, is_active, "
            "created_by, folder_path) VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
            (new_name, src["description"], src["ean"], src["ref_code"],
             src["ref_list_json"], src["master_pdf_path"], src["thumb_b64"],
             src["page_width_mm"], src["page_height_mm"],
             session["user_id"], src["folder_path"] or "")
        )
        new_pid = _dup_cur.lastrowid
        for f in fields:
            db.execute(
                "INSERT INTO artwork_profile_fields "
                "(profile_id, field_name, display_name, x1_pct, y1_pct, x2_pct, y2_pct, "
                "severity, notes, sort_order, display_layout, skip_analysis) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_pid, f["field_name"], f["display_name"],
                 f["x1_pct"], f["y1_pct"], f["x2_pct"], f["y2_pct"],
                 f["severity"], f["notes"] or "", f["sort_order"],
                 f["display_layout"] if "display_layout" in f.keys() else "side_by_side",
                 f["skip_analysis"] if "skip_analysis" in f.keys() else 0)
            )
        db.commit()
        return jsonify({"ok": True, "new_id": new_pid, "name": new_name})
    finally:
        db.close()


@app.route("/api/artwork/profiles/<int:pid>/render", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_profile_render(pid):
    """Renderuje artworka z nałożonymi polami szablonu — podgląd dla edytora."""
    import base64 as _b64, io as _io
    from artwork_comparator import _load_pages, _PROFILE_RENDER_DPI

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "Brak pliku PDF"}), 400

    db = get_db()
    try:
        profile_row = db.execute("SELECT * FROM artwork_profiles WHERE id=?", (pid,)).fetchone()
        fields = db.execute(
            "SELECT * FROM artwork_profile_fields WHERE profile_id=? ORDER BY sort_order", (pid,)
        ).fetchall()
        if not profile_row:
            return jsonify({"error": "Profil nie istnieje"}), 404
    finally:
        db.close()

    uid = session["user_id"]
    import uuid as _uuid
    upload_dir = app.config["UPLOAD_FOLDER"]
    tmp_path = os.path.join(upload_dir, f"_profile_render_{uid}_{pid}_{_uuid.uuid4().hex}.pdf")
    try:
        file.save(tmp_path)
        pages = _load_pages(tmp_path, _PROFILE_RENDER_DPI, page_idx=None)
        if not pages:
            return jsonify({"error": "Nie można wyrenderować PDF"}), 400

        from PIL import ImageDraw
        img = pages[0].copy()
        draw = ImageDraw.Draw(img)
        w, h = img.size
        colors = {"critical": (220, 38, 38, 100), "important": (234, 179, 8, 100),
                  "info": (59, 130, 246, 100)}

        for f in fields:
            x1 = int(f["x1_pct"] * w / 100)
            y1 = int(f["y1_pct"] * h / 100)
            x2 = int(f["x2_pct"] * w / 100)
            y2 = int(f["y2_pct"] * h / 100)
            color = colors.get(f["severity"], colors["info"])
            draw.rectangle([x1, y1, x2, y2], outline=color[:3], width=3)
            draw.rectangle([x1, y1, x2, min(y1+22, y2)], fill=color[:3])
            draw.text((x1+4, y1+3), f["display_name"][:25], fill=(255, 255, 255))

        buf = _io.BytesIO()
        img.save(buf, "JPEG", quality=75)
        b64 = _b64.b64encode(buf.getvalue()).decode()
        return jsonify({"image_b64": b64, "width": w, "height": h})
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


@app.route("/api/artwork/profiles/render-blank", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_profile_render_blank():
    """Renderuje artworka bez nałożonych pól — do definiowania nowych regionów."""
    import base64 as _b64, io as _io
    from artwork_comparator import _load_pages, _PROFILE_RENDER_DPI

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "Brak pliku PDF"}), 400

    uid = session["user_id"]
    import uuid as _uuid
    upload_dir = app.config["UPLOAD_FOLDER"]
    tmp_path = os.path.join(upload_dir, f"_profile_blank_{uid}_{_uuid.uuid4().hex}.pdf")
    try:
        file.save(tmp_path)
        pages = _load_pages(tmp_path, _PROFILE_RENDER_DPI, page_idx=None)
        if not pages:
            return jsonify({"error": "Nie można wyrenderować PDF"}), 400

        import io as _io2
        buf = _io2.BytesIO()
        pages[0].save(buf, "JPEG", quality=90)
        b64 = _b64.b64encode(buf.getvalue()).decode()
        w, h = pages[0].size
        return jsonify({"image_b64": b64, "width": w, "height": h})
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


@app.route("/api/artwork/profiles/<int:pid>/master", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_profile_attach_master(pid):
    """Save an uploaded PDF as the master for profile <pid>.

    Writes the file to MASTERS_DIR (persistent), updates master_pdf_path in the DB,
    and returns a rendered canvas image so the editor can display it immediately.
    Mapped fields are preserved — only the file path changes.
    """
    import hashlib as _hs, base64 as _b64, io as _io, re as _re
    from artwork_comparator import _load_pages, _PROFILE_RENDER_DPI

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "Brak pliku PDF"}), 400

    db = get_db()
    try:
        row = db.execute("SELECT id, name FROM artwork_profiles WHERE id=?", (pid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono profilu"}), 404

        raw = file.read()
        h = _hs.md5(raw, usedforsecurity=False).hexdigest()[:12]
        safe = _re.sub(r"[^\w\-.]", "_", os.path.splitext(file.filename or row["name"])[0])[:40]
        pdf_path = os.path.join(MASTERS_DIR, f"{safe}_{h}.pdf")
        with open(pdf_path, "wb") as fout:
            fout.write(raw)

        # Render first page — used both for canvas response and thumbnail
        pages = _load_pages(pdf_path, _PROFILE_RENDER_DPI, page_idx=None)
        if not pages:
            try:
                os.remove(pdf_path)
            except OSError:
                pass
            return jsonify({"error": "Nie można wyrenderować PDF. Sprawdź czy plik nie jest uszkodzony."}), 400

        # Save thumbnail to DB so canvas can fall back when file is gone (e.g. container rebuild)
        thumb = _master_thumb(pages[0])
        db.execute(
            "UPDATE artwork_profiles SET master_pdf_path=?, thumb_b64=?, updated_at=datetime('now') WHERE id=?",
            (pdf_path, thumb, pid)
        )
        db.commit()

        buf = _io.BytesIO()
        pages[0].save(buf, "JPEG", quality=90)
        b64 = _b64.b64encode(buf.getvalue()).decode()
        w, h_px = pages[0].size
        return jsonify({"ok": True, "image_b64": b64, "width": w, "height": h_px, "path": pdf_path})
    finally:
        db.close()


# ─── ARTWORK FIELD DICT ──────────────────────────────────────────────────────

@app.route("/api/artwork/field-dict", methods=["GET"])
@require_role("manager")
def api_artwork_field_dict_list():
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        rows = db.execute(
            "SELECT id, display_name, default_severity, display_layout, default_rotation,"
            " comparison_mode, numeric_tolerance, notes FROM artwork_field_dict ORDER BY display_name"
        ).fetchall()
        # Also collect names from existing profile fields not yet in dict
        used = db.execute(
            "SELECT DISTINCT display_name FROM artwork_profile_fields ORDER BY display_name"
        ).fetchall()
        dict_names = {r["display_name"] for r in rows}
        extra = [{"id": None, "display_name": u["display_name"], "default_severity": "critical",
                  "display_layout": "side_by_side", "default_rotation": 0,
                  "comparison_mode": "text", "numeric_tolerance": 0.0,
                  "notes": "", "in_dict": False}
                 for u in used if u["display_name"] not in dict_names]
        result = [{"id": r["id"], "display_name": r["display_name"],
                   "default_severity": r["default_severity"],
                   "display_layout": r["display_layout"] or "side_by_side",
                   "default_rotation": int(r["default_rotation"] or 0),
                   "comparison_mode": r["comparison_mode"] or "text",
                   "numeric_tolerance": float(r["numeric_tolerance"] or 0.0),
                   "notes": r["notes"] or "", "in_dict": True} for r in rows]
        return jsonify({"items": result + extra})
    finally:
        db.close()


_VALID_DISPLAY_LAYOUTS = {"side_by_side", "stacked"}
_VALID_COMPARISON_MODES = {"text", "numeric", "graphic", "table", "translation"}


@app.route("/api/artwork/field-dict", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_field_dict_create():
    data = request.get_json(silent=True) or {}
    name = str(data.get("display_name") or "").strip()[:200]
    sev = data.get("default_severity", "critical")
    if sev not in _VALID_FIELD_SEVERITIES:
        sev = "critical"
    if not name:
        return jsonify({"error": "Brak nazwy"}), 400
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        layout = data.get("display_layout", "side_by_side")
        if layout not in _VALID_DISPLAY_LAYOUTS:
            layout = "side_by_side"
        try:
            rotation = int(data.get("default_rotation", 0) or 0) % 360
        except (TypeError, ValueError):
            rotation = 0
        cmode = data.get("comparison_mode", "text")
        if cmode not in _VALID_COMPARISON_MODES:
            cmode = "text"
        try:
            ntol = float(data.get("numeric_tolerance", 0.0) or 0.0)
        except (TypeError, ValueError):
            ntol = 0.0
        if not (0.0 <= ntol <= 1000.0):
            ntol = 0.0
        db.execute(
            "INSERT INTO artwork_field_dict (display_name, default_severity, display_layout,"
            " default_rotation, comparison_mode, numeric_tolerance) VALUES (?, ?, ?, ?, ?, ?)",
            (name, sev, layout, rotation, cmode, ntol)
        )
        db.commit()
        row = db.execute("SELECT * FROM artwork_field_dict WHERE display_name=?", (name,)).fetchone()
        return jsonify({"id": row["id"], "display_name": row["display_name"],
                        "default_severity": row["default_severity"],
                        "display_layout": row["display_layout"] or "side_by_side",
                        "default_rotation": int(row["default_rotation"] or 0),
                        "comparison_mode": row["comparison_mode"] or "text",
                        "numeric_tolerance": float(row["numeric_tolerance"] or 0.0)})
    except Exception as e:
        logger.exception("artwork_field_dict create error: %s", e)
        return jsonify({"error": "Błąd zapisu — sprawdź czy nazwa jest unikalna."}), 400
    finally:
        db.close()


@app.route("/api/artwork/field-dict/<int:fid>", methods=["PUT"])
@require_role("manager")
@csrf_protect
def api_artwork_field_dict_update(fid):
    data = request.get_json(silent=True) or {}
    name = str(data.get("display_name") or "").strip()[:200]
    sev = data.get("default_severity", "critical")
    if sev not in _VALID_FIELD_SEVERITIES:
        sev = "critical"
    if not name:
        return jsonify({"error": "Brak nazwy"}), 400
    db = get_db()
    try:
        old = db.execute("SELECT display_name FROM artwork_field_dict WHERE id=?", (fid,)).fetchone()
        if not old:
            return jsonify({"error": "Nie znaleziono"}), 404
        layout = data.get("display_layout", "side_by_side")
        if layout not in _VALID_DISPLAY_LAYOUTS:
            layout = "side_by_side"
        try:
            rotation = int(data.get("default_rotation", 0) or 0) % 360
        except (ValueError, TypeError):
            rotation = 0
        cmode = data.get("comparison_mode", "text")
        if cmode not in _VALID_COMPARISON_MODES:
            cmode = "text"
        try:
            ntol = float(data.get("numeric_tolerance", 0.0) or 0.0)
        except (ValueError, TypeError):
            ntol = 0.0
        if not (0.0 <= ntol <= 1000.0):
            ntol = 0.0
        db.execute(
            "UPDATE artwork_field_dict SET display_name=?, default_severity=?, display_layout=?,"
            " default_rotation=?, comparison_mode=?, numeric_tolerance=? WHERE id=?",
            (name, sev, layout, rotation, cmode, ntol, fid)
        )
        if old["display_name"] != name:
            db.execute(
                "UPDATE artwork_profile_fields SET display_name=? WHERE display_name=?",
                (name, old["display_name"])
            )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/field-dict/<int:fid>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_artwork_field_dict_delete(fid):
    db = get_db()
    try:
        db.execute("DELETE FROM artwork_field_dict WHERE id=?", (fid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/field-dict/set-rotation", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_field_dict_set_rotation():
    """Upsert default_rotation for a field's display_name. Lets users 'teach'
    the system the correct orientation once during mapping — next time the
    same field type is loaded, the rotation is applied automatically."""
    data = request.get_json(silent=True) or {}
    name = str(data.get("display_name") or "").strip()
    if not name:
        return jsonify({"error": "display_name wymagane"}), 400
    try:
        rot = int(data.get("rotation", 0)) % 360
    except (TypeError, ValueError):
        return jsonify({"error": "Nieprawidłowa rotacja"}), 400
    if rot not in (0, 90, 180, 270):
        return jsonify({"error": "Rotacja musi być 0/90/180/270"}), 400
    db = get_db()
    try:
        existing = db.execute(
            "SELECT id FROM artwork_field_dict WHERE display_name=?", (name,)
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE artwork_field_dict SET default_rotation=? WHERE id=?",
                (rot, existing["id"]),
            )
        else:
            db.execute(
                "INSERT INTO artwork_field_dict (display_name, default_severity, display_layout, default_rotation, comparison_mode, numeric_tolerance) VALUES (?, ?, ?, ?, ?, ?)",
                (name, "critical", "side_by_side", rot, "text", 0.0),
            )
        db.commit()
        return jsonify({"ok": True, "display_name": name, "rotation": rot})
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════════
# ARTWORK FIELD FEEDBACK / LEARNING
# ═══════════════════════════════════════════════════════════════════════════

def _load_artwork_field_stats(db) -> dict:
    """Return {field_name: {fpr, reviews, fp_count}} from feedback table.
    Only includes fields with ≥5 operator verdicts (minimum for reliable stats).
    fpr = false_positive_rate = verdicts where was_changed=1 but operator said OK.
    """
    rows = db.execute("""
        SELECT field_name,
               COUNT(*) as total,
               SUM(CASE WHEN was_changed=1 THEN 1 ELSE 0 END) as flagged,
               SUM(CASE WHEN was_changed=1 AND operator_verdict='false_positive' THEN 1 ELSE 0 END) as fp
        FROM artwork_field_feedback
        WHERE operator_verdict IS NOT NULL
        GROUP BY field_name
        HAVING COUNT(*) >= 5
    """).fetchall()
    stats = {}
    for r in rows:
        flagged = int(r["flagged"] or 0)
        fp = int(r["fp"] or 0)
        fpr = (fp / flagged) if flagged > 0 else 0.0
        stats[r["field_name"]] = {
            "fpr": round(fpr, 3),
            "reviews": int(r["total"]),
            "fp_count": fp,
            "flagged": flagged,
        }
    return stats


@app.route("/api/artwork/field-feedback", methods=["POST"])
@require_role("user")
@csrf_protect
def api_artwork_field_feedback_create():
    data = request.get_json(silent=True) or {}
    field_name = str(data.get("field_name") or "").strip()
    verdict = data.get("operator_verdict")
    if not field_name or verdict not in ("false_positive", "confirmed_error"):
        return jsonify({"error": "Wymagane: field_name, operator_verdict in (false_positive, confirmed_error)"}), 400
    _cmode = str(data.get("comparison_mode") or "text")
    if _cmode not in ("text", "numeric", "graphic", "table", "translation"):
        _cmode = "text"
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        db.execute(
            "INSERT INTO artwork_field_feedback "
            "(field_name, display_name, profile_name, queue_id, comparison_mode, val_a, val_b, was_changed, operator_verdict) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (field_name[:200],
             str(data.get("display_name", field_name))[:200],
             str(data.get("profile_name", ""))[:200],
             str(data.get("queue_id", ""))[:100],
             _cmode,
             str(data.get("val_a", ""))[:200],
             str(data.get("val_b", ""))[:200],
             1 if data.get("was_changed") is True else 0,
             verdict)
        )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/field-stats", methods=["GET"])
@require_role("manager")
def api_artwork_field_stats():
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        stats = _load_artwork_field_stats(db)
        # Also include all-feedback counts (before min-5 filter)
        all_rows = db.execute("""
            SELECT field_name, display_name,
                   COUNT(*) as total,
                   SUM(CASE WHEN operator_verdict='false_positive' THEN 1 ELSE 0 END) as fp,
                   SUM(CASE WHEN operator_verdict='confirmed_error' THEN 1 ELSE 0 END) as tp,
                   MAX(created_at) as last_at
            FROM artwork_field_feedback
            WHERE operator_verdict IS NOT NULL
            GROUP BY field_name
            ORDER BY total DESC
        """).fetchall()
        result = []
        for r in all_rows:
            fname = r["field_name"]
            s = stats.get(fname, {})
            result.append({
                "field_name": fname,
                "display_name": r["display_name"],
                "total": r["total"],
                "fp_count": r["fp"],
                "tp_count": r["tp"],
                "fpr": s.get("fpr"),
                "last_at": r["last_at"],
                "adjustment": (
                    "pix_threshold_96 + sev-2" if s.get("fpr", 0) >= 0.90 else
                    "pix_threshold_93 + sev-1" if s.get("fpr", 0) >= 0.80 else
                    "ostrzeżenie" if s.get("fpr", 0) >= 0.70 else
                    "brak"
                ),
            })
        return jsonify({"stats": result})
    finally:
        db.close()


@app.route("/api/artwork/field-stats/reset/<field_name>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_artwork_field_stats_reset(field_name):
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        db.execute("DELETE FROM artwork_field_feedback WHERE field_name=?", (field_name,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════════
# ARTWORK MATERIAL MASTER DATA
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/artwork/materials")
@require_role("manager")
def page_artwork_materials():
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
    finally:
        db.close()
    return render_template("artwork_materials.html")


@app.route("/api/artwork/pkg-level-types", methods=["GET"])
@require_role("manager")
def api_pkg_level_types_list():
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        rows = db.execute(
            "SELECT id, code, label, sort_order, color FROM artwork_pkg_level_type ORDER BY sort_order"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@app.route("/api/artwork/pkg-level-types", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_pkg_level_types_create():
    data = request.get_json(silent=True) or {}
    code = str(data.get("code") or "").strip().lower().replace(" ", "_")[:50]
    label = str(data.get("label") or "").strip()[:100]
    if not code or not label:
        return jsonify({"error": "Wymagane: code i label"}), 400
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        max_order = db.execute("SELECT MAX(sort_order) as m FROM artwork_pkg_level_type").fetchone()["m"] or 0
        db.execute(
            "INSERT INTO artwork_pkg_level_type (code, label, sort_order, color) VALUES (?, ?, ?, ?)",
            (code, label, max_order + 1, str(data.get("color") or "#6b7280")[:50])
        )
        db.commit()
        row = db.execute("SELECT * FROM artwork_pkg_level_type WHERE code=?", (code,)).fetchone()
        return jsonify(dict(row)), 201
    except Exception as e:
        logger.exception("pkg_level_type create error: %s", e)
        return jsonify({"error": "Błąd zapisu — kod musi być unikalny."}), 400
    finally:
        db.close()


@app.route("/api/artwork/pkg-level-types/<int:ltid>", methods=["PUT"])
@require_role("manager")
@csrf_protect
def api_pkg_level_types_update(ltid):
    data = request.get_json(silent=True) or {}
    try:
        sort_order = int(data.get("sort_order", 0))
    except (ValueError, TypeError):
        sort_order = 0
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        db.execute(
            "UPDATE artwork_pkg_level_type SET label=?, color=?, sort_order=? WHERE id=?",
            (str(data.get("label") or "")[:100], str(data.get("color") or "#6b7280")[:50], sort_order, ltid)
        )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/pkg-level-types/<int:ltid>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_pkg_level_types_delete(ltid):
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        db.execute("DELETE FROM artwork_material_level WHERE level_type_id=?", (ltid,))
        db.execute("DELETE FROM artwork_pkg_level_type WHERE id=?", (ltid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/materials", methods=["GET"])
@login_required
def api_materials_list():
    """Tabela materiałów master z filtrami (q, rodzina, producent) + paginacja."""
    import material_master as _mm
    q = request.args.get("q", "").strip()
    rodzina = request.args.get("rodzina", "").strip()
    producent = request.args.get("producent", "").strip()
    before = request.args.get("before", "").strip()   # migracja starsza niż (YYYY-MM-DD)
    after = request.args.get("after", "").strip()      # migracja od (YYYY-MM-DD)
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    # limit<=0 → wszystko (z bezpiecznym sufitem, by filtry działały na całym zakresie)
    if limit <= 0 or limit > 20000:
        limit = 20000
    _esc = lambda s: s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    db = get_db()
    try:
        _mm.ensure_table(db)
        where = ["active=1"]
        params = []
        if q:
            like = f"%{_esc(q)}%"
            where.append("(ref_code LIKE ? ESCAPE '\\' OR opis_pl LIKE ? ESCAPE '\\' "
                         "OR opis_en LIKE ? ESCAPE '\\' OR ean LIKE ? ESCAPE '\\')")
            params += [like, like, like, like]
        if rodzina:
            where.append("rodzina = ?")
            params.append(rodzina)
        if producent:
            where.append("producer_code LIKE ? ESCAPE '\\'")
            params.append(f"%{_esc(producent)}%")
        if before:
            where.append("updated_at < ?")
            params.append(before)
        if after:
            where.append("updated_at >= ?")
            params.append(after)
        wsql = " AND ".join(where)
        rows = db.execute(
            # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
            "SELECT ref_code, opis_pl, opis_en, ean, rodzina, base_uom, producer_code, "  # nosec B608
            f"tariff_cn, customs_code, vat_rate, sent, supplier_codes, txt_short_pl, levels_json, updated_at "
            f"FROM material_master WHERE {wsql} "
            "ORDER BY ref_code LIMIT ? OFFSET ?", params + [limit, offset]
        ).fetchall()
        # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
        total = db.execute(f"SELECT COUNT(*) FROM material_master WHERE {wsql}", params).fetchone()[0]  # nosec B608
        families = [r[0] for r in db.execute(
            "SELECT DISTINCT rodzina FROM material_master WHERE active=1 AND rodzina<>'' "
            "ORDER BY rodzina LIMIT 400").fetchall()]
    finally:
        db.close()
    return jsonify({"items": [dict(r) for r in rows], "total": total,
                    "offset": offset, "families": families})


@app.route("/api/materials/<path:ref>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_materials_delete(ref):
    """Usuwa pojedynczy rekord materiału (po REF)."""
    import material_master as _mm
    db = get_db()
    try:
        _mm.ensure_table(db)
        cur = db.execute("DELETE FROM material_master WHERE ref_code=?", (str(ref),))
        db.commit()
        _log_audit("material_delete", session.get("username"), str(ref))
        return jsonify({"ok": True, "deleted": cur.rowcount})
    finally:
        db.close()


@app.route("/api/materials/<path:ref>", methods=["PATCH"])
@require_role("manager")
@csrf_protect
def api_materials_update(ref):
    """Edycja inline pól materiału master: stawka VAT i flaga SENT."""
    import material_master as _mm
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        _mm.ensure_table(db)
        sets, params = [], []
        if "vat_rate" in data:
            sets.append("vat_rate=?"); params.append(_mm.norm_vat(data.get("vat_rate")))
        if "sent" in data:
            sets.append("sent=?"); params.append(_mm.sent_truthy(data.get("sent")))
        if not sets:
            return jsonify({"error": "Brak pól do zapisu (vat_rate / sent)"}), 400
        sets.append("updated_at=datetime('now')")
        set_sql = ", ".join(sets)
        # bandit: nazwy kolumn z białej listy w kodzie (nie z kluczy żądania); wartości przez ?
        cur = db.execute(f"UPDATE material_master SET {set_sql} WHERE ref_code=?",  # nosec B608
                         params + [str(ref)])
        db.commit()
        if cur.rowcount == 0:
            # bandit: nazwy kolumn z białej listy w kodzie (nie z kluczy żądania); wartości przez ?
            db.execute(f"UPDATE material_master SET {set_sql} WHERE ref_norm=?",  # nosec B608
                       params + [_mm.normalize_ref(ref)])
            db.commit()
        _log_audit("material_update", session.get("username"),
                   f"{ref}: " + ", ".join(f"{k}={data[k]}" for k in ("vat_rate", "sent") if k in data))
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/materials/delete-old", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_materials_delete_old():
    """Usuwa hurtowo rekordy starsze niż podana data migracji (updated_at < before).
    Służy do czyszczenia po wgraniu nowej bazy (stare, niezaktualizowane rekordy)."""
    import material_master as _mm
    data = request.get_json(silent=True) or {}
    before = str(data.get("before") or "").strip()
    if not before:
        return jsonify({"error": "Podaj datę graniczną (before)"}), 400
    db = get_db()
    try:
        _mm.ensure_table(db)
        cur = db.execute("DELETE FROM material_master WHERE updated_at < ?", (before,))
        db.commit()
        _log_audit("material_delete_old", session.get("username"), f"before={before} n={cur.rowcount}")
        return jsonify({"ok": True, "deleted": cur.rowcount})
    finally:
        db.close()


@app.route("/api/materials/<path:ref>", methods=["GET"])
@login_required
def api_material_detail(ref):
    """Szczegóły materiału: REF, opis, producent, baza JM, poziomy + przeliczniki UOM."""
    import material_master as _mm
    db = get_db()
    try:
        mat = _mm.get_material(db, ref)
        if not mat:
            return jsonify({"error": "Nie znaleziono materiału"}), 404
        try:
            levels = json.loads(mat.get("levels_json") or "{}")
        except (ValueError, TypeError):
            levels = {}
        conv = []
        try:
            import uom as _uom
            _uom.ensure_table(db)
            rows = db.execute(
                "SELECT unit_from, unit_to, factor FROM uom_conversion WHERE ref_norm=? "
                "ORDER BY factor", (mat.get("ref_norm") or "",)
            ).fetchall()
            conv = [dict(r) for r in rows]
        except Exception:
            conv = []
    finally:
        db.close()
    return jsonify({
        "ref_code": mat.get("ref_code"), "opis_pl": mat.get("opis_pl"),
        "opis_en": mat.get("opis_en"), "ean": mat.get("ean"),
        "rodzina": mat.get("rodzina"), "base_uom": mat.get("base_uom"),
        "producer_code": mat.get("producer_code"),
        "tariff_cn": mat.get("tariff_cn"), "customs_code": mat.get("customs_code"),
        "supplier_codes": mat.get("supplier_codes"),
        "txt_short_pl": mat.get("txt_short_pl"),
        "levels": levels, "conversions": conv,
    })


@app.route("/api/materials/migrate-from-products", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_materials_migrate_products():
    """Wchłania tabelę products do material_master (scalenie rejestrów)."""
    import material_master as _mm
    db = get_db()
    try:
        res = _mm.migrate_from_products(db)
    finally:
        db.close()
    _log_audit("materials_migrate_products", session.get("username"),
               f"migrated={res.get('migrated', 0)}")
    return jsonify({"ok": "error" not in res, **res})


@app.route("/materials")
@login_required
def page_materials():
    return render_template("materials.html",
                           username=session.get("username"), role=session.get("role"))


@app.route("/api/materials/import", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_materials_import():
    """Import master daty materiałów z CSV/XLSX. Format tolerancyjny: nagłówek
    1- lub 2-wierszowy (banner poziomów + nazwy pól), przeliczniki PAZ/PPA i
    podstawowa JM trafiają też do uom_conversion."""
    import material_master as _mm
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Brak pliku"}), 400
    fname = f.filename.lower()
    raw_rows = []   # lista wierszy jako listy komórek (surowo)
    try:
        if fname.endswith(".xlsx"):
            from openpyxl import load_workbook
            wb = load_workbook(f, read_only=True, data_only=True)
            ws = wb.active
            for r in ws.iter_rows(values_only=True):
                raw_rows.append(list(r))
                if len(raw_rows) > 60000:
                    break
        elif fname.endswith(".csv"):
            import csv as _csv, io as _io
            raw = f.read().decode("utf-8-sig", errors="replace")
            # Wykryj separator (polski Excel zapisuje CSV ze średnikiem).
            _sample = raw[:8192]
            _delim = max((",", ";", "\t"), key=lambda d: _sample.count(d))
            raw_rows = [r for r in _csv.reader(_io.StringIO(raw), delimiter=_delim)]
        else:
            return jsonify({"error": "Obsługiwane formaty: .xlsx, .csv"}), 400
    except Exception as _e:
        logger.warning("materials import parse error: %s", _e)
        return jsonify({"error": "Nie udało się odczytać pliku"}), 400
    if len(raw_rows) > 55000:
        return jsonify({"error": "Za dużo wierszy (max ~55000)"}), 400
    db = get_db()
    try:
        res = _mm.import_workbook(db, raw_rows)
    finally:
        db.close()
    _log_audit("materials_import", session.get("username"),
               f"imported={res['imported']} conversions={res.get('conversions', 0)}")
    return jsonify({"ok": True, **res})


@app.route("/api/artwork/index/sync", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_index_sync():
    """Odbiera paczkę wpisów indeksu masterów z agenta ACME (skan Z:\\). #5"""
    import artwork_index as _ai
    data = request.get_json(silent=True) or {}
    entries = data.get("entries") or []
    if not isinstance(entries, list):
        return jsonify({"error": "entries musi być listą"}), 400
    if len(entries) > 5000:
        return jsonify({"error": "Za duża paczka (max 5000)"}), 400
    db = get_db()
    try:
        res = _ai.sync_entries(db, entries)
    finally:
        db.close()
    return jsonify({"ok": True, **res})


@app.route("/api/artwork/index/status", methods=["GET"])
@login_required
def api_artwork_index_status():
    """Stan indeksu masterów: ile plików, ile z REF, ostatni skan."""
    import artwork_index as _ai
    db = get_db()
    try:
        return jsonify(_ai.index_stats(db))
    finally:
        db.close()


@app.route("/api/artwork/master-files/wanted", methods=["GET"])
@require_role("manager")
def api_artwork_master_files_wanted():
    """rel_path masterów potrzebnych do podglądu (potwierdzone/grupowe), których chmura
    jeszcze nie ma. Agent skanujący Z:\\ dosyła te pliki (POST .../upload)."""
    import artwork_index as _ai
    db = get_db()
    try:
        return jsonify({"wanted": _ai.wanted_master_paths(db)})
    finally:
        db.close()


@app.route("/api/artwork/master-files/upload", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_master_file_upload():
    """Agent wgrywa bajty mastera (multipart: rel_path + file)."""
    import artwork_index as _ai, hashlib as _hl
    rel_path = (request.form.get("rel_path") or "").strip()
    f = request.files.get("file")
    if not rel_path or not f or not f.filename:
        return jsonify({"error": "Wymagane: rel_path + file"}), 400
    folder = os.path.join(app.config["UPLOAD_FOLDER"], "artwork_masters")
    os.makedirs(folder, exist_ok=True)
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"):
        ext = ".pdf"
    stored = _hl.sha1(rel_path.encode("utf-8"), usedforsecurity=False).hexdigest() + ext
    dest = os.path.join(folder, stored)
    # Enforce a size cap before reading the whole file into memory / the DB.
    _clen = request.content_length
    if _clen and _clen > MAX_UPLOAD_BYTES:
        return jsonify({"error": f"Plik jest zbyt duży. Maksymalny rozmiar to {MAX_UPLOAD_MB} MB."}), 413
    blob = f.read(MAX_UPLOAD_BYTES + 1)   # bajty trafiają DO BAZY (trwałe)
    if not blob:
        return jsonify({"error": "Pusty plik"}), 400
    if len(blob) > MAX_UPLOAD_BYTES:
        return jsonify({"error": f"Plik jest zbyt duży. Maksymalny rozmiar to {MAX_UPLOAD_MB} MB."}), 413
    try:
        with open(dest, "wb") as _fp:     # + cache na dysku (przyspiesza serwowanie)
            _fp.write(blob)
    except OSError:
        pass
    size = len(blob)
    ctype = f.mimetype or ("application/pdf" if ext == ".pdf" else "image/" + ext.lstrip("."))
    db = get_db()
    try:
        _ai.record_master_file(db, rel_path, stored, ctype, size, data=blob)
        # Zaciągnij też do widoku mapowania (Profile artwork) jako „do zmapowania".
        try:
            _ensure_master_profile(db, dest, f.filename or os.path.basename(rel_path), rel_path)
        except Exception as _pe:
            logger.debug("ensure master profile (upload) rel=%s: %s", rel_path, _pe)
    finally:
        db.close()
    return jsonify({"ok": True, "size": size})


@app.route("/api/artwork/master-file/view", methods=["GET"])
@login_required
def api_artwork_master_file_view():
    """Podgląd mastera (inline) — serwuje bajty wgrane przez agenta."""
    import artwork_index as _ai
    from flask import abort as _abort
    rel_path = (request.args.get("rel_path") or "").strip()
    if not rel_path:
        _abort(404)
    db = get_db()
    try:
        rec = _ai.master_file_for(db, rel_path)
        if not rec:
            _abort(404)
        path = os.path.join(app.config["UPLOAD_FOLDER"], "artwork_masters", rec["stored_name"])
        if os.path.isfile(path):
            return send_file(path, mimetype=rec.get("content_type") or "application/pdf",
                             as_attachment=False, download_name=os.path.basename(rel_path))
        # Cache na dysku zniknął (np. po redeployu) → odtwórz z BAZY i zapisz cache.
        blob = _ai.master_file_blob(db, rel_path)
        if not blob:
            _abort(404)
        ctype, data = blob
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as _fp:
                _fp.write(data)
        except OSError:
            pass
    finally:
        db.close()
    import io as _io
    return send_file(_io.BytesIO(data), mimetype=ctype or "application/pdf",
                     as_attachment=False, download_name=os.path.basename(rel_path))


@app.route("/api/artwork/master-files/from-path", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_master_file_from_path():
    """Jednym kliknięciem pobiera plik mastera ze znanej ścieżki PO STRONIE SERWERA
    (biblioteka/indeks zsynchr. przez agenta) i zapisuje do składnicy podglądu — bez
    ręcznego wgrywania. Działa, gdy plik jest już dostępny na serwerze; inaczej 409."""
    import artwork_index as _ai, hashlib as _hl
    data = request.get_json(silent=True) or {}
    rel_path = (data.get("rel_path") or "").strip()
    filename = (data.get("filename") or "").strip()
    if not rel_path:
        return jsonify({"error": "Brak ścieżki"}), 400
    db = get_db()
    try:
        src = _resolve_artwork_master_path(db, rel_path, filename)
        if not src or not os.path.exists(src):
            return jsonify({"error": "Plik nie jest jeszcze dostępny na serwerze — użyj "
                            "„Wgraj ręcznie” albo poczekaj, aż agent skanujący Z:\\ go prześle.",
                            "available": False}), 409
        folder = os.path.join(app.config["UPLOAD_FOLDER"], "artwork_masters")
        os.makedirs(folder, exist_ok=True)
        ext = os.path.splitext(src)[1].lower()
        if ext not in (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"):
            ext = ".pdf"
        stored = _hl.sha1(rel_path.encode("utf-8"), usedforsecurity=False).hexdigest() + ext
        dest = os.path.join(folder, stored)
        with open(src, "rb") as _fp:      # bajty DO BAZY (trwałe)
            blob = _fp.read()
        if not blob:
            return jsonify({"error": "Plik na serwerze jest pusty", "available": False}), 409
        if os.path.abspath(src) != os.path.abspath(dest):
            try:
                with open(dest, "wb") as _out:
                    _out.write(blob)
            except OSError:
                pass
        ctype = "application/pdf" if ext == ".pdf" else "image/" + ext.lstrip(".")
        _ai.record_master_file(db, rel_path, stored, ctype, len(blob), data=blob)
        try:
            _ensure_master_profile(db, dest, filename or os.path.basename(rel_path), rel_path)
        except Exception as _pe:
            logger.debug("ensure master profile (from-path) rel=%s: %s", rel_path, _pe)
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/artwork/profiles/to-map", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_profile_to_map():
    """Tworzy w Profilach artworków wpis „do zmapowania" (0 pól) dla niezmapowanego
    REF — żeby od razu pojawił się w /artwork/profiles do uzupełnienia mapowania."""
    data = request.get_json(silent=True) or {}
    ref = (data.get("ref") or "").strip()
    ean = (data.get("ean") or "").strip()
    if not ref:
        return jsonify({"error": "Brak REF"}), 400
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        ex = db.execute(
            "SELECT id FROM artwork_profiles WHERE ref_code=? LIMIT 1", (ref,)).fetchone()
        if ex:
            return jsonify({"ok": True, "existed": True})
        db.execute(
            "INSERT INTO artwork_profiles (name, ean, ref_code, master_pdf_path, thumb_b64, "
            "folder_path, packaging_type, revision, revision_rank, source_path, is_active, created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
            (ref, ean, ref, "", "", "", "", "", 0, "", session.get("user_id")))
        db.commit()
        _log_audit("artwork_profile_to_map", session.get("username"), f"ref={ref}")
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/artwork/index/import", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_index_import():
    """Zasila indeks z LISTY ŚCIEŻEK (np. `dir /s /b "Z:\\...\\*.pdf" > lista.txt`)
    albo z kolumny ścieżek w CSV/XLSX. Wysyłamy tylko metadane (nazwa+ścieżka)."""
    import artwork_index as _ai
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Brak pliku"}), 400
    fname = f.filename.lower()
    rows = []   # wiersze tabeli (listy komórek) — kolumnowy CSV/XLSX albo 1 ścieżka/linię
    try:
        if fname.endswith(".xlsx"):
            from openpyxl import load_workbook
            wb = load_workbook(f, read_only=True, data_only=True)
            for r in wb.active.iter_rows(values_only=True):
                rows.append(list(r))
                if len(rows) > 200000:
                    break
        else:  # .txt / .csv / dowolny tekst
            raw_bytes = f.read()
            # Wykryj kodowanie — Windows `dir > plik.txt` daje UTF-16 (PowerShell)
            # albo polską stronę kodową (cmd: cp1250/cp852); naiwny UTF-8 gubił „.pdf".
            if raw_bytes[:2] in (b"\xff\xfe", b"\xfe\xff"):
                raw = raw_bytes.decode("utf-16", errors="replace")
            elif raw_bytes[:3] == b"\xef\xbb\xbf":
                raw = raw_bytes.decode("utf-8-sig", errors="replace")
            else:
                raw = None
                for _enc in ("utf-8", "cp1250", "cp852", "latin-1"):
                    try:
                        raw = raw_bytes.decode(_enc)
                        break
                    except UnicodeDecodeError:
                        continue
                if raw is None:
                    raw = raw_bytes.decode("utf-8", errors="replace")
            import csv as _csv, io as _io
            if fname.endswith(".csv"):
                # Wykryj separator (polski/PowerShell eksport używa ';').
                _sample = raw[:8192]
                _delim = max((";", ",", "\t"), key=lambda d: _sample.count(d))
                rows = [r for r in _csv.reader(_io.StringIO(raw), delimiter=_delim)]
            else:
                rows = [[ln] for ln in raw.splitlines()]
    except Exception as _e:
        logger.warning("artwork index import parse error: %s", _e)
        return jsonify({"error": "Nie udało się odczytać pliku"}), 400
    if len(rows) > 200000:
        return jsonify({"error": "Za dużo wierszy (max ~200000)"}), 400
    db = get_db()
    try:
        res = _ai.import_listing(db, rows)
    finally:
        db.close()
    _log_audit("artwork_index_import", session.get("username"),
               f"indexed={res.get('indexed')} candidates={res.get('candidates')}")
    return jsonify({"ok": True, **res})


@app.route("/artwork/index")
@login_required
def artwork_index_page():
    return render_template("artwork_index.html",
                           username=session["username"], role=session["role"])


@app.route("/api/artwork/index/lookup", methods=["GET"])
@login_required
def api_artwork_index_lookup():
    """Najnowsza rewizja mastera dla REF + wszystkie rewizje (do ręcznej korekty)."""
    import artwork_index as _ai
    ref = request.args.get("ref", "").strip()
    if not ref:
        return jsonify({"error": "Podaj ref"}), 400
    db = get_db()
    try:
        best = _ai.lookup_best(db, ref)
        revs = _ai.all_revisions(db, ref)
    finally:
        db.close()
    return jsonify({"ref": ref, "best": best, "revisions": revs})


@app.route("/api/artwork/index/suggest", methods=["POST"])
@login_required
@csrf_protect
def api_artwork_index_suggest():
    """Auto-dobór masterów dla listy REF-ów (pozycje zamówienia). #6
    Body: {refs:[...]}. Zwraca mapę REF→{found, master}."""
    import artwork_index as _ai
    data = request.get_json(silent=True) or {}
    refs = data.get("refs") or []
    if not isinstance(refs, list) or len(refs) > 1000:
        return jsonify({"error": "refs musi być listą (max 1000)"}), 400
    db = get_db()
    try:
        suggestions = _ai.suggest_for_refs(db, [str(x) for x in refs])
    finally:
        db.close()
    _found = sum(1 for v in suggestions.values() if v.get("found"))
    return jsonify({"suggestions": suggestions, "found": _found, "total": len(suggestions)})


@app.route("/api/artwork/index/stats", methods=["GET"])
@login_required
def api_artwork_index_stats():
    """Statystyki indeksu masterów (ile plików / unikalnych REF / ostatni skan)."""
    import artwork_index as _ai
    db = get_db()
    try:
        _ai.ensure_table(db)
        total = db.execute("SELECT COUNT(*) FROM artwork_index").fetchone()[0]
        refs = db.execute("SELECT COUNT(DISTINCT ref_norm) FROM artwork_index WHERE ref_norm<>''").fetchone()[0]
        last = db.execute("SELECT MAX(scanned_at) FROM artwork_index").fetchone()[0]
    finally:
        db.close()
    return jsonify({"files": total, "unique_refs": refs, "last_scan": last})


@app.route("/artwork/aliases")
@login_required
def page_artwork_aliases():
    return render_template("artwork_aliases.html",
                           username=session.get("username"), role=session.get("role"))


@app.route("/artwork/revisions")
@login_required
def page_artwork_revisions():
    return render_template("artwork_revisions.html",
                           username=session.get("username"), role=session.get("role"))


@app.route("/api/artwork/alias", methods=["GET", "POST"])
@login_required
@csrf_protect
def api_artwork_alias():
    """Aliasy nazwa-pliku→REF (gdy nazwa artworku nie zawiera REF). GET: lista;
    POST (manager): {ref_code, pattern} — dodaje i backfilluje indeks."""
    import artwork_index as _ai
    db = get_db()
    try:
        if request.method == "POST":
            if session.get("role") not in ("manager", "superuser", "admin"):
                return jsonify({"error": "Brak uprawnień"}), 403
            data = request.get_json(silent=True) or {}
            res = _ai.add_alias(db, str(data.get("ref_code") or ""), str(data.get("pattern") or ""),
                                replace=bool(data.get("replace")))
            if "error" in res:
                return jsonify(res), 400
            _log_audit("artwork_alias_add", session.get("username"),
                       f"{data.get('ref_code')} <- {data.get('pattern')} ({res.get('matched')})"
                       + (" [replace]" if data.get("replace") else ""))
            return jsonify(res)
        return jsonify({"items": _ai.list_aliases(db)})
    finally:
        db.close()


@app.route("/api/artwork/alias/<int:aid>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_artwork_alias_delete(aid):
    import artwork_index as _ai
    db = get_db()
    try:
        res = _ai.delete_alias(db, aid)
    finally:
        db.close()
    return jsonify(res)


@app.route("/api/artwork/alias/suggest", methods=["GET"])
@login_required
def api_artwork_alias_suggest():
    """Podpowiada pliki artworków pasujące do REF (po opisie z master daty) lub
    do podanego tekstu — fuzzy po tokenach nazwy pliku."""
    import artwork_index as _ai
    ref = request.args.get("ref", "").strip()
    text = request.args.get("text", "").strip()
    db = get_db()
    try:
        opis = ""
        if ref:
            try:
                import material_master as _mm
                m = _mm.get_material(db, ref)
                if m:
                    # TXT_SHORT_PL (część nazwy artworku) ma priorytet — najlepiej
                    # pasuje do nazw plików; w razie braku użyj opisów.
                    opis = (m.get("txt_short_pl") or "").strip() or \
                        ((m.get("opis_pl") or "") + " " + (m.get("opis_en") or "")).strip()
            except Exception:
                opis = ""
        combined = (text + " " + opis).strip() or ref
        cands = _ai.suggest_by_text(db, combined, limit=12)
    finally:
        db.close()
    return jsonify({"ref": ref, "opis": opis, "candidates": cands})


@app.route("/artwork/mapping-groups")
@login_required
def page_artwork_mapping_groups():
    return render_template("artwork_mapping_groups.html",
                           username=session.get("username"), role=session.get("role"))


@app.route("/api/artwork/mapping-group", methods=["GET", "POST"])
@login_required
@csrf_protect
def api_artwork_mapping_group():
    """Grupy mapowań artworków (grupa → lista REF + jeden master). GET: lista;
    POST (manager): {id?, group_name, master_rel_path, refs[]} — tworzy/aktualizuje.
    REF w grupie dziedziczy master grupy, gdy nie ma własnego wzorca."""
    import artwork_index as _ai
    db = get_db()
    try:
        if request.method == "POST":
            if session.get("role") not in ("manager", "superuser", "admin"):
                return jsonify({"error": "Brak uprawnień"}), 403
            data = request.get_json(silent=True) or {}
            try:
                gid = _ai.save_group(
                    db,
                    str(data.get("group_name") or ""),
                    str(data.get("master_rel_path") or ""),
                    data.get("refs") or [],
                    created_by=session.get("user_id"),
                    gid=data.get("id") or None,
                )
            except ValueError as ve:
                return jsonify({"error": str(ve)}), 400
            _log_audit("artwork_group_save", session.get("username"),
                       f"{data.get('group_name')} ({len(data.get('refs') or [])} REF)")
            return jsonify({"ok": True, "id": gid})
        return jsonify({"items": _ai.list_groups(db)})
    finally:
        db.close()


@app.route("/api/artwork/mapping-group/<int:gid>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_artwork_mapping_group_delete(gid):
    import artwork_index as _ai
    db = get_db()
    try:
        _ai.delete_group(db, gid)
        _log_audit("artwork_group_delete", session.get("username"), f"group #{gid}")
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/artwork/confirm", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_confirm():
    """Zapamiętuje potwierdzony artwork (REF→plik) — przy pierwszym porównaniu."""
    import artwork_index as _ai
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        res = _ai.confirm_artwork(db, str(data.get("ref") or ""),
                                  str(data.get("rel_path") or ""),
                                  session.get("username", ""),
                                  level=str(data.get("level") or ""))
    finally:
        db.close()
    if "error" in res:
        return jsonify(res), 400
    _log_audit("artwork_confirm", session.get("username"),
               f"{data.get('ref')} [{data.get('level') or '—'}] = {data.get('rel_path')}")
    return jsonify(res)


@app.route("/api/artwork/unconfirm", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_unconfirm():
    """Cofa potwierdzenie artworku dla (REF, poziom) — pozwala poprawić błędny wybór."""
    import artwork_index as _ai
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        res = _ai.unconfirm_artwork(db, str(data.get("ref") or ""),
                                    level=str(data.get("level") or ""))
    finally:
        db.close()
    _log_audit("artwork_unconfirm", session.get("username"),
               f"{data.get('ref')} [{data.get('level') or '—'}]")
    return jsonify(res)


@app.route("/api/artwork/confirm-status", methods=["GET"])
@login_required
def api_artwork_confirm_status():
    """Status dla flow porównania: czy trzeba zapytać o potwierdzenie i czy
    pojawiła się nowsza rewizja niż potwierdzona."""
    import artwork_index as _ai
    db = get_db()
    try:
        res = _ai.lookup_for_comparison(db, request.args.get("ref", "").strip())
    finally:
        db.close()
    return jsonify(res)


@app.route("/api/artwork/confirmations/stale", methods=["GET"])
@login_required
def api_artwork_stale():
    """REF-y, dla których pojawiła się nowsza rewizja niż potwierdzona artwork."""
    import artwork_index as _ai
    db = get_db()
    try:
        items = _ai.stale_confirmations(db)
    finally:
        db.close()
    return jsonify({"items": items, "count": len(items)})


@app.route("/api/uom", methods=["GET", "POST", "DELETE"])
@login_required
@csrf_protect
def api_uom():
    """Przeliczniki jednostek (#4). GET: lista; POST (manager): dodaj/edytuj
    {ref_norm?, unit_from, unit_to, factor}; DELETE (manager): {id}."""
    import uom as _uom
    db = get_db()
    try:
        _uom.ensure_table(db)
        if request.method == "GET":
            rows = db.execute(
                "SELECT id, ref_norm, unit_from, unit_to, factor FROM uom_conversion "
                "ORDER BY ref_norm, unit_from LIMIT 1000").fetchall()
            return jsonify({"items": [dict(r) for r in rows]})
        if session.get("role") not in ("manager", "superuser", "admin"):
            return jsonify({"error": "Brak uprawnień"}), 403
        data = request.get_json(silent=True) or {}
        if request.method == "DELETE":
            try:
                _id = int(data.get("id"))
            except (TypeError, ValueError):
                return jsonify({"error": "Brak id"}), 400
            db.execute("DELETE FROM uom_conversion WHERE id=?", (_id,))
            db.commit()
            return jsonify({"ok": True})
        # POST
        uf = _uom.canonical_unit(data.get("unit_from"))
        ut = _uom.canonical_unit(data.get("unit_to"))
        ref_norm = _uom.normalize_ref(data.get("ref_norm")) or "*"
        try:
            factor = float(data.get("factor"))
        except (TypeError, ValueError):
            return jsonify({"error": "Nieprawidłowy przelicznik"}), 400
        if not uf or not ut or factor <= 0:
            return jsonify({"error": "Podaj jednostki i dodatni przelicznik"}), 400
        db.execute(
            "INSERT INTO uom_conversion(ref_norm, unit_from, unit_to, factor) "
            "VALUES(?,?,?,?) ON CONFLICT(ref_norm, unit_from, unit_to) "
            "DO UPDATE SET factor=excluded.factor", (ref_norm, uf, ut, factor))
        db.commit()
        _log_audit("uom_set", session.get("username"),
                   f"{ref_norm} {uf}->{ut} x{factor}")
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/materials", methods=["GET"])
@require_role("manager")
def api_artwork_materials_list():
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        family_filter = request.args.get("family", "").strip()
        status_filter = request.args.get("status", "").strip()  # complete|partial|none
        search = request.args.get("q", "").strip()

        q = "SELECT * FROM artwork_material"
        params = []
        conds = []
        if family_filter:
            conds.append("product_family=?"); params.append(family_filter)
        if search:
            safe_search = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conds.append("(ref_code LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\')"); params += [f"%{safe_search}%", f"%{safe_search}%"]
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY product_family, ref_code"
        mats = db.execute(q, params).fetchall()

        level_types = db.execute(
            "SELECT id, code, label, sort_order, color FROM artwork_pkg_level_type ORDER BY sort_order"
        ).fetchall()
        lt_ids = [r["id"] for r in level_types]

        # Batch-fetch all levels in one query to avoid N+1
        mat_ids = [m["id"] for m in mats]
        all_levels: dict = {}
        if mat_ids:
            placeholders = ",".join(["?"] * len(mat_ids))
            levels_all = db.execute(
                # bandit: lista placeholderów ? budowana z len(); wartości parametryzowane
                "SELECT aml.*, ap.name as profile_name FROM artwork_material_level aml "  # nosec B608
                f"LEFT JOIN artwork_profiles ap ON ap.id = aml.profile_id "
                f"WHERE aml.material_id IN ({placeholders})", mat_ids
            ).fetchall()
            for r in levels_all:
                all_levels.setdefault(r["material_id"], {})[r["level_type_id"]] = dict(r)

        result = []
        for m in mats:
            mid = m["id"]
            levels_map = all_levels.get(mid, {})
            assigned = sum(1 for lt in lt_ids if levels_map.get(lt, {}).get("profile_id"))
            total = len(lt_ids)
            if status_filter == "complete" and assigned < total:
                continue
            if status_filter == "partial" and (assigned == 0 or assigned == total):
                continue
            if status_filter == "none" and assigned > 0:
                continue
            result.append({
                "id": mid,
                "ref_code": m["ref_code"],
                "name": m["name"],
                "product_family": m["product_family"] or "",
                "ean": m["ean"] or "",
                "notes": m["notes"] or "",
                "created_at": m["created_at"],
                "updated_at": m["updated_at"],
                "levels": levels_map,
                "assigned": assigned,
                "total": total,
            })
        return jsonify({
            "items": result,
            "level_types": [dict(r) for r in level_types],
            "families": [r["product_family"] for r in db.execute(
                "SELECT DISTINCT product_family FROM artwork_material WHERE product_family!='' ORDER BY product_family"
            ).fetchall()],
        })
    finally:
        db.close()


@app.route("/api/artwork/materials", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_materials_create():
    data = request.get_json(silent=True) or {}
    ref = str(data.get("ref_code") or "").strip()
    if not ref:
        return jsonify({"error": "Brak ref_code"}), 400
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        db.execute(
            "INSERT INTO artwork_material (ref_code, name, product_family, ean, notes) VALUES (?, ?, ?, ?, ?)",
            (ref[:100], str(data.get("name") or "")[:200], str(data.get("product_family") or "")[:100],
             str(data.get("ean") or "")[:20], str(data.get("notes") or "")[:500])
        )
        db.commit()
        row = db.execute("SELECT * FROM artwork_material WHERE ref_code=?", (ref,)).fetchone()
        return jsonify(dict(row)), 201
    except Exception as e:
        logger.exception("artwork_material create error: %s", e)
        return jsonify({"error": "Błąd zapisu — ref_code musi być unikalny."}), 400
    finally:
        db.close()


@app.route("/api/artwork/materials/<int:mid>", methods=["PUT"])
@require_role("manager")
@csrf_protect
def api_artwork_materials_update(mid):
    data = request.get_json(silent=True) or {}
    ref = str(data.get("ref_code") or "").strip()
    if not ref:
        return jsonify({"error": "Brak ref_code"}), 400
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        db.execute(
            "UPDATE artwork_material SET ref_code=?, name=?, product_family=?, ean=?, notes=?, updated_at=datetime('now') WHERE id=?",
            (ref[:100], str(data.get("name") or "")[:200], str(data.get("product_family") or "")[:100],
             str(data.get("ean") or "")[:20], str(data.get("notes") or "")[:500], mid)
        )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/materials/<int:mid>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_artwork_materials_delete(mid):
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        db.execute("DELETE FROM artwork_material_level WHERE material_id=?", (mid,))
        db.execute("DELETE FROM artwork_material WHERE id=?", (mid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/materials/<int:mid>/levels/<int:ltid>", methods=["PUT"])
@require_role("manager")
@csrf_protect
def api_artwork_material_level_set(mid, ltid):
    data = request.get_json(silent=True) or {}
    _pid_raw = data.get("profile_id")
    try:
        profile_id = int(_pid_raw) if _pid_raw not in (None, "", False) else None
    except (TypeError, ValueError):
        profile_id = None
    _mt = str(data.get("mapping_type") or "direct").strip()
    mapping_type = _mt if _mt in ("direct", "none", "inherited") else ("direct" if profile_id else "none")
    if not profile_id:
        mapping_type = "none"
    _smid_raw = data.get("source_material_id")
    try:
        source_mid = int(_smid_raw) if _smid_raw not in (None, "", False) else None
    except (TypeError, ValueError):
        source_mid = None
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        existing = db.execute(
            "SELECT id FROM artwork_material_level WHERE material_id=? AND level_type_id=?",
            (mid, ltid)
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE artwork_material_level SET profile_id=?, mapping_type=?, source_material_id=?, notes=?, updated_at=datetime('now') WHERE id=?",
                (profile_id, mapping_type, source_mid, str(data.get("notes") or "")[:500], existing["id"])
            )
        else:
            db.execute(
                "INSERT INTO artwork_material_level (material_id, level_type_id, profile_id, mapping_type, source_material_id, notes) VALUES (?, ?, ?, ?, ?, ?)",
                (mid, ltid, profile_id, mapping_type, source_mid, str(data.get("notes") or "")[:500])
            )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/artwork/materials/import-csv", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_artwork_materials_import_csv():
    """Import materials from CSV. Expected columns: ref_code, name, product_family, ean, notes
    Optionally: sztuka_profile, op_profile, opz_profile, karton_profile (profile names to auto-assign)"""
    import csv, io
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Brak pliku"}), 400
    try:
        content = f.read().decode("utf-8-sig")  # utf-8-sig strips BOM from Excel exports
        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
    except Exception as e:
        logger.exception("CSV parse error: %s", e)
        return jsonify({"error": "Błąd parsowania CSV — sprawdź format i kodowanie pliku."}), 400

    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        level_types = {r["code"]: r["id"] for r in
                       db.execute("SELECT code, id FROM artwork_pkg_level_type").fetchall()}
        profiles = {r["name"]: r["id"] for r in
                    db.execute("SELECT name, id FROM artwork_profiles").fetchall()}

        imported = 0; updated = 0; skipped = 0; errors = []
        for row in rows:
            ref = (row.get("ref_code") or row.get("REF") or "").strip()
            if not ref:
                skipped += 1; continue
            name = (row.get("name") or row.get("nazwa") or "").strip()
            family = (row.get("product_family") or row.get("rodzina") or "").strip()
            ean = (row.get("ean") or "").strip()
            notes = (row.get("notes") or row.get("uwagi") or "").strip()
            try:
                existing = db.execute("SELECT id FROM artwork_material WHERE ref_code=?", (ref,)).fetchone()
                if existing:
                    mid = existing["id"]
                    db.execute(
                        "UPDATE artwork_material SET name=?, product_family=?, ean=?, notes=?, updated_at=datetime('now') WHERE id=?",
                        (name[:200], family[:100], ean[:20], notes[:500], mid)
                    )
                    updated += 1
                else:
                    db.execute(
                        "INSERT INTO artwork_material (ref_code, name, product_family, ean, notes) VALUES (?, ?, ?, ?, ?)",
                        (ref[:100], name[:200], family[:100], ean[:20], notes[:500])
                    )
                    # BUGFIX: szukaj po ref[:100] (tak zapisano), inaczej dla ref>100 znaków
                    # fetchone() = None → crash na ["id"]. Brak wiersza → pomiń przypisania.
                    _mrow = db.execute("SELECT id FROM artwork_material WHERE ref_code=?", (ref[:100],)).fetchone()
                    if not _mrow:
                        continue
                    mid = _mrow["id"]
                    imported += 1
                # Auto-assign profiles if column exists
                for col_suffix, lt_code in [("sztuka", "sztuka"), ("op", "op"), ("opz", "opz"), ("karton", "karton")]:
                    pname = (row.get(f"{col_suffix}_profile") or row.get(f"profil_{col_suffix}") or "").strip()
                    if pname and pname in profiles and col_suffix in level_types:
                        ltid = level_types[col_suffix]
                        pid = profiles[pname]
                        ex = db.execute(
                            "SELECT id FROM artwork_material_level WHERE material_id=? AND level_type_id=?",
                            (mid, ltid)
                        ).fetchone()
                        if ex:
                            db.execute("UPDATE artwork_material_level SET profile_id=?, mapping_type='direct', updated_at=datetime('now') WHERE id=?", (pid, ex["id"]))
                        else:
                            db.execute(
                                "INSERT INTO artwork_material_level (material_id, level_type_id, profile_id, mapping_type) VALUES (?, ?, ?, 'direct')",
                                (mid, ltid, pid)
                            )
                db.commit()   # per-wiersz: na PG błąd jednego wiersza psuje transakcję
            except Exception as e:
                db.rollback()  # odblokuj transakcję dla kolejnych wierszy
                errors.append(f"{ref}: {str(e)[:200]}")
        return jsonify({"imported": imported, "updated": updated, "skipped": skipped, "errors": errors})
    finally:
        db.close()


@app.route("/api/artwork/materials/export-csv", methods=["GET"])
@require_role("manager")
def api_artwork_materials_export_csv():
    """Export material master as CSV."""
    import csv, io
    db = get_db()
    try:
        _ensure_artwork_profile_tables(db)
        level_types = db.execute(
            "SELECT id, code FROM artwork_pkg_level_type ORDER BY sort_order"
        ).fetchall()
        mats = db.execute("SELECT * FROM artwork_material ORDER BY product_family, ref_code").fetchall()
        # Bulk-load all profile assignments in one query to avoid N+1
        _profile_map: dict = {}
        for _lv_row in db.execute(
            "SELECT aml.material_id, aml.level_type_id, ap.name "
            "FROM artwork_material_level aml JOIN artwork_profiles ap ON ap.id=aml.profile_id"
        ).fetchall():
            _profile_map[(_lv_row["material_id"], _lv_row["level_type_id"])] = _lv_row["name"]
        buf = io.StringIO()
        cols = ["ref_code", "name", "product_family", "ean", "notes"] + [f"{r['code']}_profile" for r in level_types]
        writer = csv.DictWriter(buf, fieldnames=cols)
        writer.writeheader()
        for m in mats:
            row_d = {"ref_code": m["ref_code"], "name": m["name"],
                     "product_family": m["product_family"] or "",
                     "ean": m["ean"] or "", "notes": m["notes"] or ""}
            for lt in level_types:
                row_d[f"{lt['code']}_profile"] = _profile_map.get((m["id"], lt["id"]), "")
            writer.writerow(row_d)
        csv_bytes = buf.getvalue().encode("utf-8-sig")
        return csv_bytes, 200, {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": "attachment; filename=material_master.csv"
        }
    finally:
        db.close()


@app.route("/api/admin/system-status")
@require_role("admin")
def api_admin_system_status():
    """Return status of all system and Python dependencies."""
    import importlib, shutil, subprocess, sys, platform
    checks = []

    def _check(name, label, test_fn, detail_fn=None):
        try:
            ok = test_fn()
            detail = detail_fn() if (ok and detail_fn) else ""
        except Exception as e:
            ok = False
            detail = str(e)[:200]
        checks.append({"name": name, "label": label, "ok": ok, "detail": detail})

    # ── system binaries ──────────────────────────────────────────
    _check("tesseract", "Tesseract OCR",
           lambda: bool(shutil.which("tesseract")),
           lambda: subprocess.check_output(["tesseract", "--version"], stderr=subprocess.STDOUT).decode().splitlines()[0])

    def _tess_langs():
        out = subprocess.check_output(["tesseract", "--list-langs"], stderr=subprocess.STDOUT).decode()
        langs = [l.strip() for l in out.splitlines() if l.strip() and "List" not in l]
        return "języki: " + ", ".join(langs)
    _check("tesseract_langs", "Tesseract — języki (pol/eng)",
           lambda: bool(shutil.which("tesseract")) and "pol" in subprocess.check_output(
               ["tesseract", "--list-langs"], stderr=subprocess.STDOUT).decode(),
           _tess_langs)

    _check("ghostscript", "Ghostscript (camelot tabele)",
           lambda: bool(shutil.which("gs") or shutil.which("gswin64c")),
           lambda: subprocess.check_output(["gs", "--version"], stderr=subprocess.STDOUT).decode().strip())

    _check("poppler", "Poppler (pdfinfo/pdftoppm)",
           lambda: bool(shutil.which("pdftoppm") or shutil.which("pdfinfo")),
           lambda: subprocess.check_output(["pdftoppm", "-v"], stderr=subprocess.STDOUT).decode().splitlines()[0])

    # ── Python packages ──────────────────────────────────────────
    def _pyver(pkg):
        m = importlib.import_module(pkg.replace("-", "_"))
        return getattr(m, "__version__", "?")

    for pkg, lbl in [
        ("flask", "Flask"),
        ("pymupdf", "PyMuPDF (fitz)"),
        ("pdfplumber", "pdfplumber"),
        ("camelot", "camelot"),
        ("pytesseract", "pytesseract"),
        ("PIL", "Pillow"),
        ("cv2", "OpenCV"),
        ("numpy", "numpy"),
        ("scipy", "scipy"),
        ("sklearn", "scikit-learn"),
        ("rapidfuzz", "rapidfuzz"),
        ("anthropic", "anthropic SDK"),
        ("reportlab", "ReportLab"),
        ("openpyxl", "openpyxl"),
        ("docx", "python-docx"),
    ]:
        mod = pkg.replace("-", "_")
        _check(f"py_{pkg}", lbl,
               lambda m=mod: bool(importlib.import_module(m)),
               lambda m=mod: _pyver(m))

    # ── barcode libs (optional) ───────────────────────────────────
    _check("py_zxingcpp", "zxingcpp (barcodes, preferred)",
           lambda: bool(importlib.import_module("zxingcpp")),
           lambda: _pyver("zxingcpp"))
    _check("py_pyzbar", "pyzbar (barcodes, fallback)",
           lambda: bool(importlib.import_module("pyzbar")),
           lambda: _pyver("pyzbar"))

    # ── runtime info ─────────────────────────────────────────────
    from db import SQLITE_PATH as _SQLITE_PATH
    runtime = {
        "python": sys.version,
        "platform": platform.platform(),
        "masters_dir": MASTERS_DIR,
        "masters_dir_writable": os.access(MASTERS_DIR, os.W_OK),
        "masters_dir_exists": os.path.isdir(MASTERS_DIR),
        "data_dir_env": os.environ.get("DATA_DIR", "(not set)"),
        "data_dir_exists": os.path.isdir(os.environ.get("DATA_DIR", "/data")),
        "sqlite_path": _SQLITE_PATH,
        "sqlite_exists": os.path.isfile(_SQLITE_PATH),
        "database_url_set": bool(os.environ.get("DATABASE_URL")),
        "anthropic_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
    }

    ok_count = sum(1 for c in checks if c["ok"])
    return jsonify({"ok": True, "checks": checks, "runtime": runtime,
                    "summary": {"total": len(checks), "ok": ok_count, "fail": len(checks) - ok_count}})


@app.route("/api/admin/activity-log")
@require_role("manager")
def api_admin_activity_log():
    """
    Export audit_log as JSONL (one JSON object per line).
    Query params:
      limit  — max rows (default 2000, max 10000)
      event  — filter by event name prefix
      user   — filter by username
      since  — ISO date string, e.g. '2026-05-01'
    Requires manager role or above.
    """
    import json as _json
    if session.get("role", "user") not in ("manager", "superuser", "admin"):
        return jsonify({"error": "Brak dostępu"}), 403

    try:
        limit = max(1, min(int(request.args.get("limit", 2000)), 10000))
    except (TypeError, ValueError):
        limit = 2000
    event_filter = request.args.get("event", "").strip()
    user_filter  = request.args.get("user", "").strip()
    since_filter = request.args.get("since", "").strip()

    db2 = get_db()
    try:
        where_parts = []
        params = []
        if event_filter:
            _esc_ev = event_filter.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where_parts.append("event LIKE ? ESCAPE '\\'")
            params.append(_esc_ev + "%")
        if user_filter:
            where_parts.append("username = ?")
            params.append(user_filter)
        if since_filter:
            where_parts.append("created_at >= ?")
            params.append(since_filter)
        where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        rows = db2.execute(
            # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
            f"SELECT id, event, username, detail, ip, duration_ms, extra, created_at "  # nosec B608
            f"FROM audit_log {where_sql} ORDER BY id DESC LIMIT ?",
            params + [limit]
        ).fetchall()
    finally:
        db2.close()

    lines = []
    for r in rows:
        extra_val = None
        if r["extra"]:
            try:
                extra_val = _json.loads(r["extra"])
            except Exception:
                extra_val = r["extra"]
        lines.append(_json.dumps({
            "id":          r["id"],
            "ts":          r["created_at"],
            "event":       r["event"],
            "user":        r["username"],
            "detail":      r["detail"],
            "ip":          r["ip"],
            "duration_ms": r["duration_ms"],
            "extra":       extra_val,
        }, ensure_ascii=False))

    from flask import Response
    return Response(
        "\n".join(lines) + "\n",
        mimetype="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=activity_log.jsonl"}
    )


# ═══════════════════════════════════════════════════════════════
# ERROR HANDLERS
# ═══════════════════════════════════════════════════════════════

@app.errorhandler(404)
def page_not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Nie znaleziono"}), 404
    return render_template("404.html",
                           username=session.get("username"),
                           role=session.get("role", "user")), 404

@app.errorhandler(413)
def request_entity_too_large(e):
    msg = f"Plik jest zbyt duży. Maksymalny rozmiar to {MAX_UPLOAD_MB} MB."
    if request.path.startswith("/api/"):
        return jsonify({"error": msg}), 413
    return render_template("500.html",
                           error_code=413,
                           error_title="Plik zbyt duży",
                           error_message=msg,
                           username=session.get("username"),
                           role=session.get("role", "user")), 413

@app.errorhandler(429)
def too_many_requests(e):
    msg = "Zbyt wiele żądań. Poczekaj chwilę i spróbuj ponownie."
    if request.path.startswith("/api/"):
        return jsonify({"error": msg}), 429
    return render_template("500.html",
                           error_code=429,
                           error_title="Zbyt wiele żądań",
                           error_message=msg,
                           username=session.get("username"),
                           role=session.get("role", "user")), 429

@app.errorhandler(500)
def internal_error(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Błąd serwera"}), 500
    return render_template("500.html",
                           username=session.get("username"),
                           role=session.get("role", "user")), 500


@app.route("/robots.txt")
def robots_txt():
    return app.response_class(
        "User-agent: *\nDisallow: /\n",
        mimetype="text/plain"
    )


# ─────────────────────────────────────────────────────────────────────────────
# ROUTE — POWER AUTOMATE INTAKE
# Przyjmuje PDF z Power Automate (lub dowolnego HTTP POST z tokenem).
# Autoryzacja: nagłówek  Authorization: Bearer <INTAKE_TOKEN>
# Env var INTAKE_TOKEN musi być ustawiony w Coolify — bez niego endpoint
# jest wyłączony (zwraca 503).
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/intake/artwork", methods=["POST"])
def intake_artwork():
    token = os.environ.get("INTAKE_TOKEN", "")
    if not token:
        return jsonify({"error": "Intake wyłączony — brak INTAKE_TOKEN"}), 503
    import hmac as _hmac_intake
    auth = request.headers.get("Authorization", "")
    if not _hmac_intake.compare_digest(auth, f"Bearer {token}"):
        return jsonify({"error": "Brak autoryzacji"}), 401

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "Brak pliku (pole: file)"}), 400
    fname = secure_filename(file.filename or "artwork.pdf")
    if not fname.lower().endswith(".pdf"):
        return jsonify({"error": "Tylko pliki PDF"}), 400

    # magic bytes check
    header = file.stream.read(4)
    file.stream.seek(0)
    if header != b"%PDF":
        return jsonify({"error": "Plik nie jest prawidłowym dokumentem PDF"}), 400

    sender  = (request.form.get("sender")  or request.headers.get("X-Sender",  "")).strip()[:200]
    subject = (request.form.get("subject") or request.headers.get("X-Subject", "")).strip()[:200]

    intake_dir = os.path.join(_DATA_DIR if os.path.isdir(_DATA_DIR) else
                              os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads"),
                              "intake")
    os.makedirs(intake_dir, exist_ok=True)

    import hashlib as _hs, datetime as _dt2
    raw = file.read()
    h = _hs.md5(raw, usedforsecurity=False).hexdigest()[:10]
    date_str = _dt2.date.today().isoformat()
    dest_name = f"{date_str}_{h}_{fname}"
    dest_path = os.path.join(intake_dir, dest_name)
    with open(dest_path, "wb") as fh:
        fh.write(raw)

    db = get_db()
    try:
        db.execute(
            "INSERT INTO intake_queue(filename, path, sender, subject, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (dest_name, dest_path, sender, subject)
        )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

    logger.info("Intake artwork: %s from %s", dest_name, sender or "unknown")
    return jsonify({"ok": True, "filename": dest_name}), 201


# ═══════════════════════════════════════════════════════════════
# WEBHOOKI — integracja z systemami zewnętrznymi (ERP/WMS)
# ═══════════════════════════════════════════════════════════════

def _is_ssrf_blocked_url(url: str) -> bool:
    """Return True if the URL resolves to a private/loopback address (SSRF guard)."""
    import socket as _sock, ipaddress as _ip
    try:
        from urllib.parse import urlparse as _up
        if _up(url).scheme not in ("http", "https"):
            return True
        host = _up(url).hostname or ""
        if not host:
            return True
        resolved = _sock.getaddrinfo(host, None)
        for _fam, _type, _proto, _canon, _addr in resolved:
            addr = _ip.ip_address(_addr[0])
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                return True
    except Exception:
        return True
    return False


def _fire_webhooks(event: str, payload: dict):
    """Wysyła payload JSON do wszystkich aktywnych webhooków pasujących do eventu.

    Uruchamiane w tle (daemon thread) — nie blokuje odpowiedzi HTTP.
    """
    import urllib.request as _ur, hashlib as _hl, hmac as _hmac, json as _json
    try:
        db = get_db()
        try:
            rows = db.execute(
                "SELECT url, secret, events FROM webhooks WHERE is_active=1"
            ).fetchall()
        finally:
            db.close()
    except Exception as _e:
        logger.warning("webhook DB read failed: %s", _e)
        return

    body = _json.dumps({"event": event, **payload}, ensure_ascii=False).encode()
    for row in rows:
        subscribed = [e.strip() for e in (row["events"] or "").split(",")]
        if event not in subscribed and "*" not in subscribed:
            continue
        if _is_ssrf_blocked_url(row["url"]):
            logger.warning("webhook SSRF blocked: %s", row["url"])
            continue
        try:
            headers = {"Content-Type": "application/json", "X-DocCompare-Event": event}
            if row["secret"]:
                sig = _hmac.new(row["secret"].encode(), body, _hl.sha256).hexdigest()
                headers["X-DocCompare-Signature"] = f"sha256={sig}"
            req = _ur.Request(row["url"], data=body, headers=headers, method="POST")
            # bandit: URL webhooka: http(s) wymuszone przy zapisie + _is_ssrf_blocked_url (schemat/prywatne IP)
            with _ur.urlopen(req, timeout=10) as _resp:  # nosec B310
                pass
        except Exception as _e:
            logger.warning("webhook delivery failed to %s: %s", row["url"], _e)


def _fire_webhooks_bg(event: str, payload: dict):
    """Non-blocking wrapper — fires webhook in a daemon thread."""
    t = threading.Thread(target=_fire_webhooks, args=(event, payload), daemon=True)
    t.start()


@app.route("/api/webhooks", methods=["GET"])
@require_role("admin")
def api_webhooks_list():
    db = get_db()
    try:
        raw = db.execute("SELECT id, name, url, secret, events, is_active, created_at FROM webhooks ORDER BY id").fetchall()
        rows = []
        for r in raw:
            row = dict(r)
            row["secret"] = "***" if row.get("secret") else ""
            rows.append(row)
    finally:
        db.close()
    return jsonify(rows)


@app.route("/api/webhooks", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_webhooks_create():
    data = request.get_json(silent=True)
    err = _validate_json_body(data, {"name": (str, True), "url": (str, True)})
    if err:
        return jsonify({"error": err}), 400
    data = data or {}
    url = str(data.get("url") or "").strip()[:2000]
    name = str(data.get("name") or "").strip()[:200]
    if not url or not name:
        return jsonify({"error": "Wymagane: name i url"}), 400
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "URL musi zaczynać się od http:// lub https://"}), 400
    db = get_db()
    try:
        _wh_cur = db.execute(
            "INSERT INTO webhooks (name, url, secret, events, is_active, created_by) VALUES (?,?,?,?,1,?)",
            (name, url, str(data.get("secret") or "")[:500],
             str(data.get("events") or "comparison.completed")[:500], session["user_id"])
        )
        db.commit()
        wid = _wh_cur.lastrowid
    finally:
        db.close()
    return jsonify({"ok": True, "id": wid})


@app.route("/api/webhooks/<int:wid>", methods=["DELETE"])
@require_role("admin")
@csrf_protect
def api_webhooks_delete(wid):
    db = get_db()
    try:
        db.execute("DELETE FROM webhooks WHERE id=?", (wid,))
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/webhooks/<int:wid>/test", methods=["POST"])
@require_role("admin")
@csrf_protect
def api_webhooks_test(wid):
    """Wysyła testowy event do webhooka."""
    db = get_db()
    try:
        row = db.execute("SELECT * FROM webhooks WHERE id=?", (wid,)).fetchone()
    finally:
        db.close()
    if not row:
        return jsonify({"error": "Nie znaleziono"}), 404
    _fire_webhooks_bg("test.ping", {"message": "DocCompare webhook test", "webhook_id": wid})
    return jsonify({"ok": True, "message": "Test event wysłany"})


# ─────────────────────────────────────────────────────────────────────────────
# SŁOWNIK TŁUMACZEŃ
# ─────────────────────────────────────────────────────────────────────────────

# ── Słownik Incoterms ─────────────────────────────────────────────────────────
# _INCOTERMS_SEED / _ensure_incoterms_table / trasy /incoterms → blueprints.incoterms


def _load_incoterms_dict() -> dict:
    """{KOD: opis_pl} ze słownika incoterms — do wzbogacania/walidacji porównań."""
    db = get_db()
    try:
        _ensure_incoterms_table(db)
        return {(r["code"] or "").upper(): (r["description_pl"] or "")
                for r in db.execute(
                    "SELECT code, description_pl FROM incoterms_dictionary").fetchall()}
    except Exception:
        return {}
    finally:
        db.close()


# Trasy /incoterms i /api/incoterms* → blueprints.incoterms (zarejestrowane wyżej)


# Trasy /translation-dictionary* → blueprints.translation_dict (zarejestrowane wyżej)


# ─────────────────────────────────────────────────────────────────────────────
# MAPOWANIE SAD
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/sad-mapping", methods=["GET"])
@login_required
def api_sad_mapping_list():
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, sad_field_code, sad_field_name_pl, sad_field_name_en, "
            "mapped_to_field, mapped_to_doctype, data_type, description, active "
            "FROM sad_field_mapping ORDER BY CAST(sad_field_code AS INTEGER), sad_field_code"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()

@app.route("/api/sad-mapping/<int:mid>", methods=["PUT"])
@require_role("manager")
@csrf_protect
def api_sad_mapping_update(mid):
    data = request.get_json(silent=True) or {}
    _sad_max_lens = {"mapped_to_field": 100, "mapped_to_doctype": 50, "data_type": 50,
                     "description": 500, "sad_field_name_pl": 200, "sad_field_name_en": 200}
    fields, vals = [], []
    for k in ("mapped_to_field", "mapped_to_doctype", "data_type", "description", "active",
              "sad_field_name_pl", "sad_field_name_en"):
        if k in data:
            v = data[k]
            if k == "active":
                v = 1 if v else 0
            elif k in _sad_max_lens:
                v = str(v or "")[:_sad_max_lens[k]]
            fields.append(f"{k}=?")
            vals.append(v)
    if not fields:
        return jsonify({"error": "Brak pól"}), 400
    vals.append(mid)
    db = get_db()
    try:
        # bandit: nazwy kolumn z białej listy w kodzie (nie z kluczy żądania); wartości przez ?
        db.execute(f"UPDATE sad_field_mapping SET {', '.join(fields)} WHERE id=?", vals)  # nosec B608
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/sad")
@login_required
def sad_approvals_page():
    role = session["role"]
    uid = session["user_id"]
    db = get_db()
    try:
        if can_see_all(role):
            rows = db.execute(
                "SELECT c.id, c.file_a, c.file_b, c.created_at, c.approval_status, "
                "c.approval_note, c.approved_at, u.username as submitted_by, "
                "u2.username as approved_by_name "
                "FROM comparisons c "
                "LEFT JOIN users u ON c.user_id=u.id "
                "LEFT JOIN users u2 ON c.approved_by=u2.id "
                "WHERE c.doc_type='SAD' AND (c.is_deleted IS NULL OR c.is_deleted=0) "
                "ORDER BY c.created_at DESC LIMIT 200"
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT c.id, c.file_a, c.file_b, c.created_at, c.approval_status, "
                "c.approval_note, c.approved_at, u.username as submitted_by, "
                "u2.username as approved_by_name "
                "FROM comparisons c "
                "LEFT JOIN users u ON c.user_id=u.id "
                "LEFT JOIN users u2 ON c.approved_by=u2.id "
                "WHERE c.doc_type='SAD' AND c.user_id=? AND (c.is_deleted IS NULL OR c.is_deleted=0) "
                "ORDER BY c.created_at DESC LIMIT 200",
                (uid,)
            ).fetchall()
        items = [dict(r) for r in rows]
    finally:
        db.close()
    return render_template("sad_approvals.html",
                           items=items,
                           username=session.get("username"),
                           role=role)


@app.route("/bundle")
@login_required
def bundle_compare_page():
    return render_template("bundle_compare.html",
                           username=session.get("username"),
                           role=session.get("role"))


@app.route("/api/bundle/compare", methods=["POST"])
@login_required
@csrf_protect
def api_bundle_compare():
    uid = session["user_id"]
    if _check_and_record_api_rate(uid):
        return jsonify({"error": "Zbyt wiele żądań. Odczekaj minutę i spróbuj ponownie."}), 429

    # Accept 2-4 files: invoice (CI), bol, transport_invoice, sad
    slots = ["invoice", "bol", "transport_invoice", "sad"]
    files = {}
    paths = {}
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    for slot in slots:
        f = request.files.get(slot)
        if f and f.filename:
            err = _validate_pdf_upload(f)
            if err:
                return jsonify({"error": f"{slot}: {err}"}), 400
            p = os.path.join(app.config["UPLOAD_FOLDER"],
                             f"{uid}_{slot}_{secure_filename(f.filename) or 'upload.pdf'}")
            f.save(p)
            files[slot] = f.filename
            paths[slot] = p

    if len(paths) < 2:
        return jsonify({"error": "Wgraj co najmniej 2 dokumenty."}), 400

    try:
        from comparator import extract_pdf_text
        from enhanced_comparator import extract_header_fields

        texts = {}
        for slot, p in paths.items():
            try:
                texts[slot] = extract_pdf_text(p)
            except Exception as e:
                texts[slot] = ""

        # Extract header fields from each doc
        headers = {}
        for slot, text in texts.items():
            doc_type_map = {"invoice": "CI", "bol": "BL", "transport_invoice": "CI", "sad": "SAD"}
            headers[slot] = extract_header_fields(text, doc_type_map.get(slot, "auto"))

        # Cross-compare key values
        checks = []
        LABEL = {
            "invoice": "Faktura handlowa (CI)",
            "bol": "Bill of Lading (BOL)",
            "transport_invoice": "Faktura transportowa",
            "sad": "SAD",
        }

        def _cross(field_name, slots_to_check):
            vals = {s: headers.get(s, {}).get(field_name, "") for s in slots_to_check if s in headers}
            vals = {k: v for k, v in vals.items() if v}
            if len(vals) < 2:
                return
            unique = set(vals.values())
            status = "ok" if len(unique) == 1 else "roznica"
            checks.append({
                "field": field_name,
                "values": {LABEL.get(k, k): v for k, v in vals.items()},
                "status": status,
                "comment": "" if status == "ok" else "Różne wartości między dokumentami",
            })

        # Fields to cross-compare
        for field in ["Numer PO", "Waluta", "Warunki dostawy", "Port załadunku", "Port rozładunku", "ETD", "ETA"]:
            _cross(field, list(paths.keys()))

        # Total value cross-check (invoice vs SAD customs value)
        # This is approximate — just report what was found
        numeric_fields = {}
        for slot in ("invoice", "sad"):
            if slot in texts:
                import re as _re
                # Look for total value patterns
                m = _re.search(r"Total\s+(?:Value|Amount|CIF|FOB)\s*[:\s]+([\d,. ]+)", texts[slot], _re.IGNORECASE)
                if m:
                    numeric_fields[slot] = m.group(1).strip()

        errors = sum(1 for c in checks if c["status"] == "roznica")
        ok_count = sum(1 for c in checks if c["status"] == "ok")

        result = {
            "checks": checks,
            "files": {LABEL.get(k, k): v for k, v in files.items()},
            "error_count": errors,
            "ok_count": ok_count,
            "risk_level": "blad" if errors > 0 else "ok",
        }
        return jsonify(result)

    finally:
        for p in paths.values():
            try:
                os.remove(p)
            except OSError:
                pass


# ── PROFORMA VERIFICATION AGENT ──────────────────────────────────────────────

@app.route("/agent/proforma")
@login_required
def agent_proforma_page():
    # Zakładka wycofana — auto-porównanie PO↔PI dzieje się w szczegółach dostawy
    # (wrzuć proformę, rozpozna się po numerze zlecenia w nazwie). Przekierowanie
    # zachowuje stare linki/zakładki przeglądarki.
    return redirect(url_for("dostawy_list"))


@app.route("/api/agent/proforma-check", methods=["POST"])
@login_required
@csrf_protect
def api_agent_proforma_check():
    uid = session["user_id"]
    if _check_and_record_api_rate(uid):
        return jsonify({"error": "Zbyt wiele żądań. Odczekaj minutę."}), 429

    f = request.files.get("proforma")
    if not f or not f.filename:
        return jsonify({"error": "Brak pliku pro-formy."}), 400
    err = _validate_pdf_upload(f)
    if err:
        return jsonify({"error": err}), 400

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    path_pi = os.path.join(app.config["UPLOAD_FOLDER"],
                           f"{uid}_pi_{secure_filename(f.filename) or 'proforma.pdf'}")
    f.save(path_pi)

    try:
        from comparator import extract_pdf_text
        from enhanced_comparator import extract_header_fields, compare_enhanced
        from supplier_profiles import detect_supplier

        text_pi = extract_pdf_text(path_pi)
        hdr = extract_header_fields(text_pi, "PI")

        po_number = str(hdr.get("Numer PO") or "").strip()
        if not po_number:
            return jsonify({"error": "Nie udało się wykryć numeru PO w pro-formie. Upewnij się, że dokument zawiera numer zamówienia."}), 422

        # Search library for matching PO file
        _po_esc = po_number.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        _db = get_db()
        try:
            row = _db.execute(
                "SELECT id, filename, file_path, rel_path FROM library_files "
                "WHERE (filename LIKE ? ESCAPE '\\' OR rel_path LIKE ? ESCAPE '\\') AND file_path != '' "
                "ORDER BY modified_at DESC LIMIT 1",
                (f"%{_po_esc}%", f"%{_po_esc}%")
            ).fetchone()
        finally:
            _db.close()

        # Fallback: search in shipment_documents (delivery module uploads)
        if not row or not row["file_path"] or not os.path.exists(row["file_path"]):
            _db2 = get_db()
            try:
                ship_row = _db2.execute(
                    "SELECT sd.po_number, sd.filename "
                    "FROM shipment_documents sd "
                    "WHERE sd.doc_type='PO' AND sd.po_number LIKE ? ESCAPE '\\' "
                    "ORDER BY sd.uploaded_at DESC LIMIT 1",
                    (f"%{_po_esc}%",),
                ).fetchone()
            finally:
                _db2.close()
            if ship_row:
                _ship_folder = _ensure_shipment_folder(ship_row["po_number"])
                _ship_path = os.path.join(_ship_folder, ship_row["filename"])
                if os.path.isfile(_ship_path):
                    row = {"file_path": _ship_path, "filename": ship_row["filename"]}

        if not row or not row["file_path"] or not os.path.exists(row["file_path"]):
            return jsonify({
                "error": f"Nie znaleziono PO {po_number} w bibliotece ani w module dostaw. Wgraj plik PO do dostawy lub biblioteki.",
                "po_number": po_number,
            }), 404

        path_po = row["file_path"]
        supplier_code = detect_supplier(text_pi)

        with _HEAVY_SEM:
            enh = compare_enhanced(path_po, path_pi, "PO", "PI", supplier_code)
        ed = enh.to_dict()

        errors   = ed.get("diff_count", 0)
        warnings = ed.get("warn_count", 0)
        status   = "ok" if errors == 0 else "error"

        # Collect email recipients
        _db2 = get_db()
        try:
            submitter = _db2.execute(
                "SELECT email, username FROM users WHERE id=?", (uid,)
            ).fetchone()
            managers = _db2.execute(
                "SELECT email, username FROM users "
                "WHERE role IN ('manager','admin','superuser') AND email IS NOT NULL AND email != '' AND is_active=1"
            ).fetchall()
        finally:
            _db2.close()

        recipients = []
        if submitter and submitter["email"]:
            recipients.append({"email": submitter["email"], "username": submitter["username"]})
        for m in (managers or []):
            if m["email"] and not any(r["email"] == m["email"] for r in recipients):
                recipients.append({"email": m["email"], "username": m["username"]})

        # Build email body
        if status == "ok":
            subject = f"✅ Pro-forma OK — PO {po_number} [{f.filename}]"
            body_lines = [
                f"Weryfikacja pro-formy zakończona pomyślnie.",
                f"",
                f"Numer PO:    {po_number}",
                f"Pro-forma:   {f.filename}",
                f"PO w bibl.:  {row['filename']}",
                f"Wynik:       ZGODNOŚĆ — {ed.get('ok_count', 0)} pozycji zgodnych",
                f"",
                f"Zamówienie można realizować.",
                f"",
                f"— DocCompare Agent",
            ]
        else:
            discrepancies = []
            for finding in (ed.get("findings") or []):
                if finding.get("severity") in ("error", "warning"):
                    discrepancies.append(
                        f"  • {finding.get('field_name','?')}: {finding.get('description','')}"
                    )
            for item in (ed.get("items") or []):
                if item.get("status") == "roznica":
                    issues = "; ".join(item.get("issues") or [])
                    discrepancies.append(f"  • {item.get('ref','?')}: {issues}")

            subject = f"⚠️ Pro-forma NIEZGODNA — PO {po_number} [{f.filename}]"
            body_lines = [
                f"Weryfikacja pro-formy wykryła ROZBIEŻNOŚCI.",
                f"",
                f"Numer PO:    {po_number}",
                f"Pro-forma:   {f.filename}",
                f"PO w bibl.:  {row['filename']}",
                f"Błędy:       {errors}",
                f"Ostrzeżenia: {warnings}",
                f"",
                f"Rozbieżności:",
            ] + (discrepancies[:20] or ["  Brak szczegółów"]) + [
                f"",
                f"Zaloguj się do DocCompare aby zobaczyć pełny raport.",
                f"",
                f"— DocCompare Agent",
            ]

        body = "\n".join(body_lines)
        emails_sent = []
        email_errors = []
        for r in recipients:
            try:
                _send_email(r["email"], subject, body)
                emails_sent.append(r["email"])
            except Exception as _e:
                logger.warning("agent_proforma: email to %s failed: %s", r["email"], _e)
                email_errors.append(r["email"])

        # Save comparison
        result_for_db = dict(ed)
        result_for_db["doc_type_detected"] = "PI"
        result_for_db["agent_triggered"] = True
        result_for_db["po_number"] = po_number
        try:
            import hashlib as _hl
            def _fsha(p):
                h = _hl.sha256()
                with open(p, "rb") as _f2:
                    for _c in iter(lambda: _f2.read(65536), b""):
                        h.update(_c)
                return h.hexdigest()
            cid = _save_comparison(uid, "PI", row["filename"], f.filename,
                                   result_for_db, hash_a=_fsha(path_po), hash_b=_fsha(path_pi))
            if po_number:
                _ensure_shipment(po_number, "", cid, uid)
        except Exception as _se:
            logger.warning("agent_proforma: _save_comparison failed: %s", _se)
            cid = None

        _log_audit("agent_proforma_check", session["username"],
                   f"po={po_number} status={status} errors={errors} recipients={len(emails_sent)}")

        return jsonify({
            "status": status,
            "po_number": po_number,
            "proforma_file": f.filename,
            "po_file": row["filename"],
            "diff_count": errors,
            "warn_count": warnings,
            "ok_count": ed.get("ok_count", 0),
            "findings": [x for x in (ed.get("findings") or [])
                         if x.get("severity") in ("error", "warning")][:20],
            "items_with_issues": [i for i in (ed.get("items") or [])
                                  if i.get("status") == "roznica"][:20],
            "emails_sent": emails_sent,
            "email_errors": email_errors,
            "comparison_id": cid,
            "summary": ed.get("summary", ""),
        })

    finally:
        try:
            os.remove(path_pi)
        except OSError:
            pass


# ── Dane referencyjne — hub ───────────────────────────────────────────────────

@app.route("/data")
@login_required
def data_hub_page():
    return render_template("data_hub.html",
                           username=session.get("username"),
                           role=session.get("role"))


# ── Baza produktów ────────────────────────────────────────────────────────────

@app.route("/products")
@login_required
def products_page():
    # Scalone z Master data materiałów — „Baza produktów" przekierowuje do /materials.
    return redirect("/materials")


@app.route("/api/products", methods=["GET"])
@login_required
def api_products_list():
    q = request.args.get("q", "").strip()[:200]
    try:
        page = max(1, min(10000, int(request.args.get("page", 1))))
    except (ValueError, TypeError):
        page = 1
    per_page = 50
    offset = (page - 1) * per_page
    db = get_db()
    try:
        if q:
            _esc_q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{_esc_q}%"
            rows = db.execute(
                "SELECT id, ref_code, product_name, ean, unit, tariff_cn, supplier_codes, active "
                "FROM products WHERE (ref_code LIKE ? ESCAPE '\\' OR product_name LIKE ? ESCAPE '\\' OR ean LIKE ? ESCAPE '\\') "
                "AND active=1 ORDER BY ref_code LIMIT ? OFFSET ?",
                (pattern, pattern, pattern, per_page, offset)
            ).fetchall()
            total = db.execute(
                "SELECT COUNT(*) FROM products WHERE (ref_code LIKE ? ESCAPE '\\' OR product_name LIKE ? ESCAPE '\\' OR ean LIKE ? ESCAPE '\\') AND active=1",
                (pattern, pattern, pattern)
            ).fetchone()[0]
        else:
            rows = db.execute(
                "SELECT id, ref_code, product_name, ean, unit, tariff_cn, supplier_codes, active "
                "FROM products WHERE active=1 ORDER BY ref_code LIMIT ? OFFSET ?",
                (per_page, offset)
            ).fetchall()
            total = db.execute("SELECT COUNT(*) FROM products WHERE active=1").fetchone()[0]
        return jsonify({"items": [dict(r) for r in rows], "total": total, "page": page})
    finally:
        db.close()


@app.route("/api/products", methods=["POST"])
@login_required
@require_role("manager")
@csrf_protect
def api_products_create():
    data = request.get_json(silent=True) or {}
    ref = str(data.get("ref_code") or "").strip().upper()[:100]
    name = str(data.get("product_name") or "").strip()[:500]
    if not ref or not name:
        return jsonify({"error": "ref_code i product_name są wymagane"}), 400
    db = get_db()
    try:
        try:
            cur = db.execute(
                "INSERT INTO products(ref_code, product_name, ean, unit, tariff_cn, description, supplier_codes) "
                "VALUES(?,?,?,?,?,?,?)",
                (ref, name, str(data.get("ean") or "")[:30], str(data.get("unit") or "szt")[:20],
                 str(data.get("tariff_cn") or "")[:20], str(data.get("description") or "")[:1000],
                 str(data.get("supplier_codes") or "")[:500])
            )
            db.commit()
            return jsonify({"ok": True, "id": cur.lastrowid})
        except Exception as e:
            if "UNIQUE" in str(e).upper():
                return jsonify({"error": f"Kod REF {ref} już istnieje"}), 409
            raise
    finally:
        db.close()


@app.route("/api/products/<int:pid>", methods=["PUT"])
@require_role("manager")
@csrf_protect
def api_products_update(pid):
    data = request.get_json(silent=True) or {}
    _prod_max_lens = {"product_name": 500, "ean": 30, "unit": 20, "tariff_cn": 20,
                      "description": 1000, "supplier_codes": 500}
    fields, vals = [], []
    for k in ("product_name", "ean", "unit", "tariff_cn", "description", "supplier_codes", "active"):
        if k in data:
            v = data[k]
            if k == "active":
                v = 1 if v else 0
            elif k in _prod_max_lens:
                v = str(v or "")[:_prod_max_lens[k]]
            fields.append(f"{k}=?")
            vals.append(v)
    if not fields:
        return jsonify({"error": "Brak pól"}), 400
    fields.append("updated_at=datetime('now')")
    vals.append(pid)
    db = get_db()
    try:
        # bandit: nazwy kolumn z białej listy w kodzie (nie z kluczy żądania); wartości przez ?
        db.execute(f"UPDATE products SET {', '.join(fields)} WHERE id=?", vals)  # nosec B608
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/products/<int:pid>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_products_delete(pid):
    db = get_db()
    try:
        db.execute("UPDATE products SET active=0 WHERE id=?", (pid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/products/import", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_products_import():
    """Import products from CSV. Expected columns: ref_code, product_name, ean, unit, tariff_cn."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Brak pliku"}), 400
    if not f.filename.lower().endswith(".csv"):
        return jsonify({"error": "Dozwolone tylko pliki .csv"}), 400
    import csv, io
    try:
        content = f.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        db = get_db()
        ok, skipped = 0, 0
        try:
            for row in reader:
                ref = (row.get("ref_code") or row.get("REF") or "").strip().upper()
                name = (row.get("product_name") or row.get("Nazwa") or "").strip()
                if not ref or not name:
                    skipped += 1
                    continue
                try:
                    db.execute(
                        "INSERT INTO products(ref_code, product_name, ean, unit, tariff_cn, description, supplier_codes) "
                        "VALUES(?,?,?,?,?,?,?) "
                        "ON CONFLICT(ref_code) DO UPDATE SET product_name=excluded.product_name, "
                        "ean=excluded.ean, unit=excluded.unit, tariff_cn=excluded.tariff_cn, "
                        "description=excluded.description, supplier_codes=excluded.supplier_codes, "
                        "updated_at=datetime('now')",
                        (ref[:100], name[:300], row.get("ean", "")[:20], row.get("unit", "szt")[:20],
                         row.get("tariff_cn", "")[:20], row.get("description", "")[:1000],
                         row.get("supplier_codes", "")[:500])
                    )
                    db.commit()   # per-wiersz: na PG nieudany INSERT psuje całą transakcję
                    ok += 1
                except Exception:
                    db.rollback()  # odblokuj transakcję, by kolejne wiersze przeszły
                    skipped += 1
        finally:
            db.close()
        return jsonify({"ok": True, "imported": ok, "skipped": skipped})
    except Exception as e:
        logger.exception("Transport import error: %s", e)
        return jsonify({"error": "Błąd importu danych — sprawdź format pliku."}), 500


# ── Śledzenie kontenerów ──────────────────────────────────────────────────────

def _17track_status(code: int) -> str:
    STATUS = {
        0: "Nie śledzony", 10: "W transporcie", 20: "W porcie",
        30: "W odprawie celnej", 35: "Odprawa zakończona",
        40: "W magazynie", 50: "W dostawie", 60: "Dostarczono",
        65: "Nieodebrano", 70: "Wyjątek", 80: "Wygasło",
    }
    return STATUS.get(code, f"Status {code}")


@app.route("/tracking")
@login_required
def tracking_page():
    db = get_db()
    try:
        containers = db.execute(
            "SELECT ct.id, ct.container_number, ct.carrier, ct.status, ct.origin_port, "
            "ct.dest_port, ct.eta, ct.last_event, ct.last_event_date, ct.last_checked, "
            "u.username as added_by "
            "FROM container_tracking ct "
            "LEFT JOIN users u ON ct.created_by=u.id "
            "ORDER BY ct.created_at DESC LIMIT 100"
        ).fetchall()
        containers = [dict(c) for c in containers]
    finally:
        db.close()
    return render_template("tracking.html",
                           containers=containers,
                           sealines=SAFECUBE_SEALINES,
                           has_safecube=bool(_get_tracking_key("safecube")),
                           has_17track=bool(_get_tracking_key("17track")),
                           has_shipsgo=bool(_get_tracking_key("shipsgo")),
                           username=session.get("username"),
                           role=session.get("role"))


# ── SafeCube / Sinay (agregator oceaniczny) ───────────────────────────────────
# Najczęściej używane sealine (kody SCAC-like) — armatorzy ACME na pierwszym miejscu.
SAFECUBE_SEALINES = [
    ("COSU", "COSCO"),
    ("MAEU", "Maersk"),
    ("MSCU", "MSC"),
    ("HLCU", "Hapag-Lloyd"),
    ("CMDU", "CMA CGM"),
    ("ONEY", "ONE"),
    ("EGLV", "Evergreen"),
    ("YMLU", "Yang Ming"),
    ("HDMU", "HMM"),
    ("OOLU", "OOCL"),
]


def _safecube_status_pl(status: str) -> str:
    """Mapuj status SafeCube (metadata.status) na polski."""
    S = {
        "PLANNED": "Zaplanowano",
        "IN_TRANSIT": "W transporcie",
        "DELIVERED": "Dostarczono",
        "UNKNOWN": "Nieznany",
        "REGISTERED": "Zarejestrowano",
    }
    return S.get(str(status or "").upper(), str(status) if status else "Nieznany")


def _safecube_parse(number: str, payload) -> dict:
    """Parsuj odpowiedź SafeCube/Sinay do znormalizowanego dict-a.

    Schemat jest defensywny — próbuje kilku wariantów nazw pól, bo publiczna
    dokumentacja bywa niedostępna. Pełna odpowiedź jest zapisywana w raw_json,
    co pozwala dokalibrować parser po pierwszym realnym zapytaniu.
    """
    # 1) Zlokalizuj obiekt przesyłki
    ship = None
    if isinstance(payload, list):
        ship = payload[0] if payload else {}
    elif isinstance(payload, dict):
        for key in ("shipments", "data", "results"):
            node = payload.get(key)
            if isinstance(node, list) and node:
                ship = node[0]
                break
        if ship is None:
            ship = payload
    ship = ship if isinstance(ship, dict) else {}

    meta = ship.get("metadata") or ship.get("meta") or {}
    route = ship.get("route") or {}
    locations = ship.get("locations") or []

    status_raw = (meta.get("shippingStatus") or meta.get("status")
                  or ship.get("shippingStatus") or ship.get("status") or "")
    carrier = (meta.get("sealineName") or meta.get("sealine")
               or ship.get("sealineName") or ship.get("carrier") or "")

    def _loc_name(node):
        # Indeks do tablicy locations (SafeCube często podaje location jako int)
        if isinstance(node, bool):
            return ""
        if isinstance(node, int) and 0 <= node < len(locations):
            l = locations[node]
            return l.get("name", "") if isinstance(l, dict) else ""
        if isinstance(node, dict):
            loc = node.get("location")
            if isinstance(loc, dict):
                return loc.get("name") or loc.get("locationName") or ""
            if isinstance(loc, int) and not isinstance(loc, bool) and 0 <= loc < len(locations):
                l = locations[loc]
                return l.get("name", "") if isinstance(l, dict) else ""
            return node.get("name") or node.get("locationName") or ""
        return ""

    def _node_date(node):
        if isinstance(node, dict):
            return node.get("date") or node.get("eta") or node.get("predictiveEta") or ""
        return ""

    pol_node = route.get("pol") or {}
    pod_node = route.get("pod") or {}
    origin_port = _loc_name(pol_node) or _loc_name(route.get("prepol"))
    dest_port = _loc_name(pod_node) or _loc_name(route.get("postpod"))
    etd = _node_date(pol_node)
    eta = (_node_date(pod_node) or ship.get("eta") or meta.get("eta")
           or meta.get("estimatedTimeOfArrival") or "")

    # 2) Zdarzenia z kontenerów
    events = []
    for c in (ship.get("containers") or []):
        for ev in (c.get("events") or []):
            desc = (ev.get("description") or ev.get("eventType")
                    or ev.get("status") or ev.get("eventCode") or "")
            locn = _loc_name(ev.get("location"))
            full = (f"{desc} — {locn}" if locn else desc).strip(" —")
            events.append({
                "date": ev.get("date") or ev.get("eventDate") or "",
                "desc": full,
            })
    events.sort(key=lambda e: e.get("date") or "", reverse=True)
    latest = events[0] if events else {}

    has_data = bool(ship.get("containers") or route or events)
    status_pl = _safecube_status_pl(status_raw) if has_data else "Zarejestrowano — odśwież za chwilę"

    return {
        "number": number,
        "carrier": carrier,
        "status": status_pl,
        "origin_port": origin_port,
        "dest_port": dest_port,
        "eta": eta,
        "etd": etd,
        "last_event": latest.get("desc", ""),
        "last_event_date": latest.get("date", ""),
        "events": events[:15],
        "raw": ship,
    }


# Browser-like User-Agent — część API trackingowych (Sinay/SafeCube, ShipsGo) stoi
# za Cloudflare i blokuje domyślny "Python-urllib/..." jako bota (Error 1010:
# Access denied — "blocked based on your browser's signature").
_TRACK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Sinay zmieniał ścieżki/wersje API (v1/v2, safecube vs container-tracking).
# Nie znając pewnego adresu, próbujemy kilku kandydatów i zapamiętujemy działający.
# Twarde nadpisanie: env SAFECUBE_API_URL (wtedy próbujemy tylko jego).
_SAFECUBE_BASE_OK = None


def _safecube_candidates() -> list:
    env = os.environ.get("SAFECUBE_API_URL", "").strip()
    if env.startswith(("http://", "https://")):
        return [env]
    if env:
        logger.warning("SAFECUBE_API_URL musi zaczynać się od http:// lub https:// — używam domyślnych")
    return [
        "https://api.sinay.ai/safecube/api/v1/public/shipment",
        "https://api.sinay.ai/container-tracking/api/v2/shipment",
        "https://api.sinay.ai/container-tracking/api/v1/shipment",
        "https://api.sinay.ai/safecube/api/v2/public/shipment",
        "https://api.sinay.ai/container-tracking/api/v1/shipments",
    ]


def _track_via_safecube(numbers, sealine, api_key):
    """Śledź kontenery via SafeCube/Sinay Container Tracking API.

    Oficjalny endpoint: GET /shipment (per numer), klucz w nagłówku API_KEY.
    Parametry: shipmentNumber (wymagany), shipmentType=CT, sealine (SCAC, opc.).
    Odpowiedź (metadata / locations / route / containers.events) parsuje
    _safecube_parse. Bazę URL można nadpisać env SAFECUBE_API_URL."""
    import urllib.request as _ur
    import urllib.parse as _up
    import urllib.error as _ue
    import json as _json

    global _SAFECUBE_BASE_OK
    results = []
    for n in numbers:
        _params = {"shipmentNumber": n, "shipmentType": "CT"}
        if sealine:
            _params["sealine"] = sealine
        qs = _up.urlencode(_params)
        # Najpierw zapamiętany działający endpoint, inaczej lista kandydatów.
        bases = [_SAFECUBE_BASE_OK] if _SAFECUBE_BASE_OK else _safecube_candidates()
        parsed = None
        last_code = None
        last_detail = ""
        saw_403 = False
        for base in bases:
            req = _ur.Request(f"{base}?{qs}", method="GET", headers={
                "Accept": "application/json",
                "API_KEY": api_key,
                "User-Agent": _TRACK_UA,
            })
            try:
                # bandit: bazy z _safecube_candidates(): stałe https:// lub env sprawdzony pod http(s)
                with _ur.urlopen(req, timeout=20) as resp:  # nosec B310
                    payload = _json.loads(resp.read().decode())
                parsed = _safecube_parse(n, payload)
                _SAFECUBE_BASE_OK = base   # zapamiętaj działający adres
                logger.info("SafeCube endpoint OK: %s", base)
                break
            except _ue.HTTPError as e:
                last_code = e.code
                try:
                    last_detail = e.read().decode()[:300]
                except Exception:
                    last_detail = ""
                logger.warning("SafeCube %s @ %s (%s): %s", e.code, base, n, last_detail[:120])
                if e.code == 401:
                    raise RuntimeError("nieprawidłowy lub wygasły klucz API SafeCube")
                if e.code == 403:
                    saw_403 = True
                # 403/404/5xx → spróbuj kolejnego kandydata
                continue
            except Exception as e:
                last_code = "conn"
                last_detail = str(e)[:200]
                logger.warning("SafeCube conn @ %s (%s): %s", base, n, last_detail)
                continue
        if parsed is not None:
            results.append(parsed)
        elif saw_403:
            raise RuntimeError(
                "SafeCube 403 — klucz rozpoznany, ale brak uprawnień do zasobu. "
                "Sprawdź aktywną subskrypcję 'Container Tracking' na koncie Sinay.")
        else:
            results.append({
                "number": n, "carrier": "", "status": f"Błąd API ({last_code})",
                "origin_port": "", "dest_port": "", "eta": "", "etd": "",
                "last_event": (last_detail or "Nie znaleziono działającego endpointu SafeCube "
                               "— ustaw SAFECUBE_API_URL na adres z dokumentacji."),
                "last_event_date": "", "events": [], "raw": {"error": last_detail, "code": last_code},
            })
    return results


def _track_via_17track(numbers, api_key):
    """Śledź kontenery via 17track.net. Zwraca listę znormalizowanych wyników."""
    import urllib.request as _ur
    import json as _json

    payload = _json.dumps([{"number": n} for n in numbers]).encode()
    req = _ur.Request(
        "https://api.17track.net/track/v2.2/gettrackinfo",
        data=payload,
        headers={"Content-Type": "application/json", "17token": api_key,
                 "User-Agent": _TRACK_UA}
    )
    # bandit: URL to stała https:// w kodzie
    with _ur.urlopen(req, timeout=15) as resp:  # nosec B310
        resp_data = _json.loads(resp.read().decode())

    results = []
    # Obrona przed nietypowym kształtem odpowiedzi API (null/typ inny niż lista/dict).
    _data = (resp_data.get("data") if isinstance(resp_data, dict) else None) or {}
    accepted = _data.get("accepted") or []
    if not isinstance(accepted, list):
        accepted = []
    for item in accepted:
        if not isinstance(item, dict):
            continue
        track = item.get("track") or {}
        if not isinstance(track, dict):
            track = {}
        events = track.get("z1") or []
        if not isinstance(events, list):
            events = []
        latest = events[0] if (events and isinstance(events[0], dict)) else {}
        results.append({
            "number": item.get("number"),
            "carrier": track.get("fn", ""),
            "status": _17track_status(track.get("e", 0)),
            "origin_port": track.get("op", ""),
            "dest_port": track.get("dp", ""),
            "eta": track.get("eta", ""),
            "etd": track.get("etd", ""),
            "last_event": latest.get("z", ""),
            "last_event_date": latest.get("a", ""),
            "events": events[:10],
            "raw": track,
        })
    return results


def _shipsgo_parse(number: str, item) -> dict:
    """Parsuj obiekt ShipsGo (GetContainerInfo, v1.2) do znormalizowanego dict-a.
    Defensywnie — ShipsGo zwraca PascalCase; pełny obiekt trzymamy w 'raw', by
    dokalibrować parser po pierwszym realnym zapytaniu (jak przy SafeCube)."""
    d = item if isinstance(item, dict) else {}

    def _g(*keys):
        for k in keys:
            v = d.get(k)
            if v not in (None, "", [], {}):
                return v
        return ""

    carrier = _g("ShippingLine", "Carrier", "Line", "SealineName", "shippingLine")
    status = _g("Status", "ContainerStatus", "status")
    origin = _g("Pol", "LoadPort", "PortOfLoading", "FromPort", "OriginPort", "POL")
    dest = _g("Pod", "DischargePort", "PortOfDischarge", "ToPort", "DestinationPort", "POD")
    eta = _g("FormatedETA", "ETA", "FirstETA", "Eta", "EstimatedArrival")
    etd = _g("FormatedETD", "ETD", "Etd", "EstimatedDeparture")

    moves = (d.get("Movements") or d.get("Events") or d.get("Timeline")
             or d.get("movements") or [])
    events = []
    if isinstance(moves, list):
        for ev in moves:
            if not isinstance(ev, dict):
                continue
            ed = (ev.get("Date") or ev.get("EventDate") or ev.get("Timestamp")
                  or ev.get("date") or "")
            loc = (ev.get("Location") or ev.get("Port") or ev.get("LocationName")
                   or ev.get("location") or "")
            desc = (ev.get("Event") or ev.get("Status") or ev.get("Description")
                    or ev.get("Movement") or ev.get("event") or "")
            full = (f"{desc} — {loc}" if loc else desc).strip(" —")
            if full or ed:
                events.append({"date": ed, "desc": full})

    def _key(e):
        # ShipsGo podaje dd/MM/yyyy — sortuj po znormalizowanej dacie.
        try:
            from normalizer import normalize_date as _nd
            _dd = _nd(str(e.get("date") or ""))
            return _dd.isoformat() if _dd else str(e.get("date") or "")
        except Exception:
            return str(e.get("date") or "")
    events.sort(key=_key, reverse=True)
    latest = events[0] if events else {}

    return {
        "number": number,
        "carrier": str(carrier or ""),
        "status": str(status or "") or "Zarejestrowano — odśwież za chwilę",
        "origin_port": str(origin or ""),
        "dest_port": str(dest or ""),
        "eta": str(eta or ""),
        "etd": str(etd or ""),
        "last_event": latest.get("desc", ""),
        "last_event_date": latest.get("date", ""),
        "events": events[:15],
        "raw": d,
    }


def _track_via_shipsgo(numbers, api_key, sealine=""):
    """Śledź kontenery via ShipsGo (API v1.2). Flow: PostContainerInfo (rejestruje
    /odświeża kontener → requestId; istniejący nie zużywa nowego kredytu) →
    GetContainerInfo (milestone'y). Pierwszy odczyt świeżo dodanego kontenera bywa
    pusty (ShipsGo dociąga dane od przewoźnika asynchronicznie) — wtedy status
    'Zarejestrowano…', ponowny track po chwili zwróci komplet."""
    import urllib.request as _ur
    import urllib.parse as _up
    import urllib.error as _ue
    import json as _json

    POST_URL = "https://shipsgo.com/api/v1.2/ContainerService/PostContainerInfo/"
    GET_URL = "https://shipsgo.com/api/v1.2/ContainerService/GetContainerInfo/"
    results = []
    for n in numbers:
        try:
            # ShipsGo sam wykrywa przewoźnika z numeru — 'OTHER' = auto. (Kody
            # armatorów ShipsGo różnią się od SCAC SafeCube, więc nie przekazujemy
            # `sealine` z dropdownu SafeCube, by nie psuć detekcji.)
            form = {"authCode": api_key, "containerNumber": n, "shippingLine": "OTHER"}
            req = _ur.Request(
                POST_URL, data=_up.urlencode(form).encode(), method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "Accept": "application/json", "User-Agent": _TRACK_UA})
            # bandit: URL to stała https:// w kodzie
            with _ur.urlopen(req, timeout=25) as resp:  # nosec B310
                rid = resp.read().decode().strip().strip('"')

            q = _up.urlencode({"authCode": api_key, "requestId": rid, "mapPoint": "false"})
            get_req = _ur.Request(f"{GET_URL}?{q}",
                                  headers={"Accept": "application/json", "User-Agent": _TRACK_UA})
            # bandit: URL to stała https:// w kodzie
            with _ur.urlopen(get_req, timeout=25) as resp:  # nosec B310
                payload = _json.loads(resp.read().decode() or "[]")
            if isinstance(payload, list):
                item = payload[0] if payload else {}
            elif isinstance(payload, dict):
                item = payload
            else:
                item = {}
            results.append(_shipsgo_parse(n, item))
        except _ue.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:300]
            except Exception:
                pass
            logger.error("ShipsGo HTTP %s for %s: %s", e.code, n, detail)
            if e.code in (401, 403):
                raise RuntimeError("nieprawidłowy lub wygasły klucz API ShipsGo (authCode)")
            results.append({
                "number": n, "carrier": "", "status": f"Błąd API ({e.code})",
                "origin_port": "", "dest_port": "", "eta": "", "etd": "",
                "last_event": detail or "Błąd zapytania ShipsGo",
                "last_event_date": "", "events": [], "raw": {"error": detail, "code": e.code},
            })
        except RuntimeError:
            raise
        except Exception as e:
            logger.error("ShipsGo error for %s: %s", n, e)
            results.append({
                "number": n, "carrier": "", "status": "Błąd połączenia",
                "origin_port": "", "dest_port": "", "eta": "", "etd": "",
                "last_event": str(e)[:200], "last_event_date": "", "events": [], "raw": {},
            })
    return results


def _propagate_container_eta(db, container_number: str, eta_raw, uid) -> list:
    """Propaguje ETA z trackingu na powiązaną dostawę (#18): aktualizuje
    kolejka_kontenery.eta oraz data_dostawy w kolejka_zlecenia spiętych z tym
    kontenerem. Zwraca listę (nr_zamowienia, stara_data, nowa_data) dla dostaw,
    którym zmieniła się data (do powiadomienia o opóźnieniu)."""
    changed = []
    try:
        eta_date = ""
        if eta_raw:
            from normalizer import normalize_date as _nd
            # normalize_date zwraca obiekt date — zamień na ISO string, bo niżej
            # porównujemy z `old` (string z DB). Bez tego `old == eta_date` było
            # zawsze False → aktualizacja+log re-odpalały się przy każdym pollu, a
            # powiadomienie o opóźnieniu (_new > _old: date>str) rzucało TypeError.
            _d = _nd(str(eta_raw))
            eta_date = _d.isoformat() if _d else str(eta_raw)[:10]
        if not eta_date:
            return changed
        # 1) kontener
        db.execute("UPDATE kolejka_kontenery SET eta=?, updated_at=datetime('now') "
                   "WHERE numer_kontenera=?", (eta_date, container_number))
        krow = db.execute("SELECT id FROM kolejka_kontenery WHERE numer_kontenera=?",
                          (container_number,)).fetchone()
        if not krow:
            return changed
        kid = krow["id"]
        # 2) dostawy spięte z kontenerem — aktualizuj datę dostawy gdy się różni
        drows = db.execute(
            "SELECT id, nr_zamowienia, data_dostawy FROM kolejka_zlecenia WHERE kontener_id=?",
            (kid,)).fetchall()
        for d in drows:
            old = (d["data_dostawy"] or "")[:10]
            if old == eta_date:
                continue
            db.execute("UPDATE kolejka_zlecenia SET data_dostawy=?, updated_at=datetime('now') "
                       "WHERE id=?", (eta_date, d["id"]))
            db.execute(
                "INSERT INTO kolejka_status_log (zlecenie_id, pole, stara_wartosc, nowa_wartosc, changed_by) "
                "VALUES (?, 'data_dostawy', ?, ?, ?)", (d["id"], old or "", eta_date, uid))
            changed.append((d["nr_zamowienia"], old, eta_date))
    except Exception as _e:
        logger.debug("ETA propagation failed for %s: %s", container_number, _e)
    return changed


class _TrackError(Exception):
    """Błąd providera trackingu z kodem HTTP do odpowiedzi API."""
    def __init__(self, msg, code=502):
        super().__init__(msg)
        self.msg = msg
        self.code = code


def _run_tracking_providers(numbers, provider="auto", sealine=""):
    """Woła wybranego providera trackingu i zwraca listę znormalizowanych wyników.
    Rzuca _TrackError(msg, code) przy braku klucza / błędzie API. Wspólne dla
    /api/tracking/track oraz przypisywania kontenera do dostawy."""
    provider = str(provider or "auto").strip().lower()
    sealine = str(sealine or "").strip().upper()
    safecube_key = _get_tracking_key("safecube")
    track17_key = _get_tracking_key("17track")
    # Auto: preferuj SafeCube (agregator oceaniczny), potem 17track
    if provider == "auto":
        provider = "safecube" if safecube_key else "17track"

    if provider == "safecube":
        if not safecube_key:
            raise _TrackError("Brak klucza API SafeCube — skonfiguruj w Ustawieniach", 400)
        try:
            return _track_via_safecube(numbers, sealine, safecube_key)
        except RuntimeError as e:
            raise _TrackError(str(e)[:200], 502)
        except Exception as e:
            logger.error("SafeCube API error: %s", e)
            raise _TrackError("Błąd API SafeCube — spróbuj ponownie później", 502)
    elif provider == "shipsgo":
        shipsgo_key = _get_tracking_key("shipsgo")
        if not shipsgo_key:
            raise _TrackError("Brak klucza API ShipsGo — skonfiguruj w Ustawieniach", 400)
        try:
            return _track_via_shipsgo(numbers, shipsgo_key, sealine)
        except RuntimeError as e:
            raise _TrackError(str(e)[:200], 502)
        except Exception as e:
            logger.error("ShipsGo API error: %s", e)
            raise _TrackError("Błąd API ShipsGo — spróbuj ponownie później", 502)
    elif track17_key:
        try:
            return _track_via_17track(numbers, track17_key)
        except Exception as e:
            logger.error("17track API error: %s", e)
            raise _TrackError("Błąd API 17track — spróbuj ponownie później", 502)
    else:
        return [{
            "number": n, "carrier": "", "status": "no_api",
            "origin_port": "", "dest_port": "", "eta": "", "etd": "",
            "last_event": "Brak klucza API — skonfiguruj SafeCube lub 17track w Ustawieniach",
            "last_event_date": "", "events": [],
        } for n in numbers]


def _store_tracking_results(results, uid):
    """Zapisz wyniki trackingu do container_tracking, propaguj ETA na dostawy i
    odpal alerty postojowe. Zwraca listę zmian ETA [(nr, stara, nowa)]."""
    import json as _json2
    _eta_changes = []
    db = get_db()
    try:
        for r in results:
            # BUGFIX: bez numeru kontenera (np. ułomna odpowiedź 17track) nie wstawiaj —
            # container_number to PK NOT NULL; pusty numer wysadziłby INSERT.
            if not (r.get("number") or "").strip():
                continue
            if r.get("status") == "no_api":
                db.execute(
                    "INSERT OR IGNORE INTO container_tracking(container_number, created_by) VALUES(?,?)",
                    (r["number"], uid)
                )
            else:
                if r.get("eta"):
                    _eta_changes += _propagate_container_eta(db, r["number"], r.get("eta"), uid)
                db.execute(
                    "INSERT INTO container_tracking"
                    "(container_number, carrier, status, origin_port, dest_port, eta, etd, "
                    " last_event, last_event_date, events_json, raw_json, last_checked, created_by) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now'),?) "
                    "ON CONFLICT(container_number) DO UPDATE SET carrier=excluded.carrier, "
                    "status=excluded.status, origin_port=excluded.origin_port, dest_port=excluded.dest_port, "
                    "eta=excluded.eta, etd=excluded.etd, last_event=excluded.last_event, "
                    "last_event_date=excluded.last_event_date, events_json=excluded.events_json, "
                    "raw_json=excluded.raw_json, last_checked=excluded.last_checked, created_by=excluded.created_by",
                    (r["number"], r.get("carrier", ""), r.get("status", "unknown"),
                     r.get("origin_port", ""), r.get("dest_port", ""),
                     r.get("eta", ""), r.get("etd", ""),
                     r.get("last_event", ""), r.get("last_event_date", ""),
                     _json2.dumps(r.get("events", [])),
                     _json2.dumps(r.get("raw", {})),
                     uid)
                )
        db.commit()
        # Licznik zużycia API trackingu (1 zapytanie = 1 kontener odpytany u dostawcy)
        try:
            _bump_tracking_usage(db, sum(1 for r in results if r.get("status") != "no_api"))
            db.commit()
        except Exception:
            pass
        # Ryzyko postojowe: oceń odświeżone kontenery i alarmuj przy 🟡/🔴
        try:
            _check_demurrage_alerts(
                db, [r["number"] for r in results
                     if r.get("status") != "no_api" and r.get("number")])
        except Exception:
            pass
    finally:
        db.close()
    return _eta_changes


# ── Licznik zużycia API trackingu (miesięcznie, w tabeli settings) ─────────────
def _tracking_usage_key(when=None):
    from datetime import datetime as _dt, timezone as _tz
    d = when or _dt.now(_tz.utc)
    return "api_calls_" + d.strftime("%Y-%m")


def _bump_tracking_usage(db, n):
    """Zwiększ licznik zapytań API trackingu w bieżącym miesiącu o n (read-modify-
    write — wystarczająco dokładne dla wskaźnika; nie commit-uje samodzielnie)."""
    if not n or n <= 0:
        return
    key = _tracking_usage_key()
    row = db.execute(
        "SELECT value FROM settings WHERE category='tracking' AND key=?", (key,)
    ).fetchone()
    cur = 0
    if row:
        try:
            cur = int((dict(row).get("value") or "0"))
        except (ValueError, TypeError):
            cur = 0
    db.execute(
        "INSERT INTO settings(category, key, value) VALUES('tracking',?,?) "
        "ON CONFLICT(category,key) DO UPDATE SET value=excluded.value",
        (key, str(cur + n)))


def _get_tracking_usage_month(db):
    """Liczba zapytań API trackingu wykonanych w bieżącym miesiącu (UTC)."""
    row = db.execute(
        "SELECT value FROM settings WHERE category='tracking' AND key=?",
        (_tracking_usage_key(),)).fetchone()
    if not row:
        return 0
    try:
        return int((dict(row).get("value") or "0"))
    except (ValueError, TypeError):
        return 0


# ── Auto-odświeżanie trackingu (leniwe, raz na dobę, wyzwalane wejściem na stronę) ──
_TRACK_AUTO_REFRESH_HOURS = int(os.environ.get("TRACKING_AUTO_REFRESH_HOURS", "18"))


def _auto_refresh_worker(numbers, uid):
    """Wątek w tle: odśwież listę kontenerów porcjami po 10 (limit jak w API)."""
    try:
        with app.app_context():
            for i in range(0, len(numbers), 10):
                chunk = numbers[i:i + 10]
                try:
                    results = _run_tracking_providers(chunk, "auto", "")
                    changes = _store_tracking_results(results, uid)
                    _notify_eta_changes(changes)
                except Exception as e:
                    logger.warning("Auto-refresh trackingu (porcja) nie powiódł się: %s", e)
    except Exception as e:
        logger.warning("Auto-refresh trackingu nie powiódł się: %s", e)


def _maybe_auto_refresh_tracking(uid):
    """Jeśli minęło >= _TRACK_AUTO_REFRESH_HOURS od ostatniego auto-odświeżenia,
    odpal w tle tracking wszystkich kontenerów spiętych z NIErozliczonymi dostawami.
    Znacznik czasu jest zapisywany PRZED startem wątku, by dwa requesty/workery nie
    odpaliły odświeżania równolegle. Wyłączane przez TRACKING_AUTO_REFRESH_HOURS=0."""
    if _TRACK_AUTO_REFRESH_HOURS <= 0:
        return
    # Bez klucza API nie ma sensu nic odpalać.
    if not (_get_tracking_key("safecube") or _get_tracking_key("17track")
            or _get_tracking_key("shipsgo")):
        return
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    now = _dt.now(_tz.utc)
    db = get_db()
    try:
        row = db.execute(
            "SELECT value FROM settings WHERE category='tracking' AND key='auto_refresh_last'"
        ).fetchone()
        last = None
        if row and dict(row).get("value"):
            try:
                last = _dt.fromisoformat(dict(row)["value"])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=_tz.utc)
            except (ValueError, TypeError):
                last = None
        if last and (now - last) < _td(hours=_TRACK_AUTO_REFRESH_HOURS):
            return
        # Zaklep znacznik czasu od razu (dedup równoległych wyzwoleń).
        db.execute(
            "INSERT INTO settings(category, key, value) VALUES('tracking','auto_refresh_last',?) "
            "ON CONFLICT(category,key) DO UPDATE SET value=excluded.value",
            (now.isoformat(),))
        db.commit()
        rows = db.execute(
            "SELECT DISTINCT kk.numer_kontenera AS cn FROM kolejka_kontenery kk "
            "JOIN kolejka_zlecenia z ON z.kontener_id = kk.id "
            "WHERE COALESCE(z.status,'') != 'rozliczone' "
            "AND COALESCE(kk.numer_kontenera,'') != '' LIMIT 100"
        ).fetchall()
        numbers = []
        for _r in rows:
            _cn = (dict(_r).get("cn") or "").strip().upper()
            if _cn:
                numbers.append(_cn)
    finally:
        db.close()
    if numbers:
        threading.Thread(target=_auto_refresh_worker, args=(numbers, uid),
                         daemon=True).start()


def _notify_eta_changes(eta_changes):
    """Powiadom managerów o zmianie daty dostawy (opóźnienie/przyspieszenie)."""
    for _nr, _old, _new in eta_changes:
        try:
            _delta = ""
            if _old and _new and _new > _old:
                _delta = " (opóźnienie)"
            elif _old and _new and _new < _old:
                _delta = " (przyspieszenie)"
            _notify_managers(
                f"ETA dostawy {_nr} zaktualizowane{_delta}",
                f"Nowa data dostawy z trackingu: {_new}" + (f" (było {_old})" if _old else ""),
                link=f"/dostawy/{_nr}", email=bool(_delta),
            )
        except Exception:
            pass


def _coord_num(v):
    """Czy v wygląda jak współrzędna (liczba lub liczbowy string, nie bool)?"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except (ValueError, AttributeError):
            return None
    return None


def _find_coords(obj, _depth=0):
    """Rekurencyjnie znajdź (lat, lng) w odpowiedzi trackingu. Preferuje węzły
    pozycji AIS statku (ais/position/vessel) przed portami trasy."""
    if _depth > 7 or obj is None:
        return None
    if isinstance(obj, dict):
        lat = _coord_num(obj.get("lat") if "lat" in obj else obj.get("latitude"))
        lng = _coord_num(obj.get("lng") if "lng" in obj else
                         (obj.get("lon") if "lon" in obj else obj.get("longitude")))
        if lat is not None and lng is not None and not (lat == 0 and lng == 0):
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                return (lat, lng)
        for key in ("ais", "position", "lastPosition", "vessel", "currentPosition", "coordinates"):
            if key in obj:
                r = _find_coords(obj[key], _depth + 1)
                if r:
                    return r
        for v in obj.values():
            r = _find_coords(v, _depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_coords(v, _depth + 1)
            if r:
                return r
    return None


def _deep_get_first(obj, key, _depth=0):
    """Pierwsza niepusta wartość pod kluczem `key` w zagnieżdżonej strukturze."""
    if _depth > 7 or obj is None:
        return None
    if isinstance(obj, dict):
        if key in obj and obj[key] not in (None, "", [], {}):
            return obj[key]
        for v in obj.values():
            r = _deep_get_first(v, key, _depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _deep_get_first(v, key, _depth + 1)
            if r is not None:
                return r
    return None


def _safecube_position(raw):
    """Najlepsze przybliżenie 'gdzie jest kontener' z odpowiedzi SafeCube:
    pozycja AIS statku → lokalizacja OSTATNIEGO zdarzenia → POD → POL.
    Zwraca (lat, lng) albo None. Wcześniej brano pierwsze lat/lng z odpowiedzi,
    co trafiało na port załadunku (POL) zamiast bieżącej pozycji statku."""
    if not isinstance(raw, dict):
        return None
    locations = raw.get("locations") if isinstance(raw.get("locations"), list) else []

    def _coords_of(node):
        # node: dict lokalizacji albo int (indeks do locations[])
        if isinstance(node, bool):
            return None
        if isinstance(node, int) and 0 <= node < len(locations):
            node = locations[node]
        if isinstance(node, dict):
            lat = _coord_num(node.get("lat") if "lat" in node else node.get("latitude"))
            lng = _coord_num(node.get("lng") if "lng" in node else
                             (node.get("lon") if "lon" in node else node.get("longitude")))
            if lat is not None and lng is not None and not (lat == 0 and lng == 0):
                if -90 <= lat <= 90 and -180 <= lng <= 180:
                    return (lat, lng)
            c = node.get("coordinates")
            if isinstance(c, dict):
                return _coords_of(c)
            if isinstance(c, list) and len(c) >= 2:
                lng2, lat2 = _coord_num(c[0]), _coord_num(c[1])
                if lat2 is not None and lng2 is not None and not (lat2 == 0 and lng2 == 0):
                    return (lat2, lng2)
        return None

    # 1) Pozycja AIS statku (jeśli SafeCube ją zwraca)
    for key in ("ais", "vesselPosition", "lastPosition", "currentPosition",
                "vessel", "position"):
        node = _deep_get_first(raw, key)
        if node is not None:
            c = _coords_of(node) or _find_coords(node, 0)
            if c:
                return c

    # 2) Lokalizacja najświeższego zdarzenia kontenera (ostatnia znana pozycja)
    last_loc, last_date = None, ""
    for cont in (raw.get("containers") or []):
        for ev in (cont.get("events") or []):
            d = str(ev.get("date") or ev.get("eventDate") or "")
            if ev.get("location") is not None and d >= last_date:
                last_date, last_loc = d, ev.get("location")
    if last_loc is not None:
        c = _coords_of(last_loc)
        if c:
            return c

    # 3) POD (port docelowy) → 4) POL (port załadunku) jako ostateczność
    route = raw.get("route") or {}
    for nodekey in ("pod", "postpod", "pol", "prepol"):
        nd = route.get(nodekey)
        if isinstance(nd, dict):
            c = _coords_of(nd.get("location"))
            if c:
                return c
    return None


def _safecube_vessel(raw):
    """Nazwa statku (bieżący/ostatni rejs) z odpowiedzi SafeCube. Preferuje statek
    z najświeższego zdarzenia kontenera, potem metadata/AIS. '' gdy brak."""
    if not isinstance(raw, dict):
        return ""

    def _vn(v):
        if isinstance(v, str):
            return v.strip()
        if isinstance(v, dict):
            return str(v.get("name") or v.get("vesselName")
                       or v.get("shipName") or "").strip()
        return ""

    best_date, best = "", ""
    for cont in (raw.get("containers") or []):
        for ev in (cont.get("events") or []):
            vn = _vn(ev.get("vessel") or ev.get("vesselName")
                     or ev.get("transport") or ev.get("transportMode"))
            if vn:
                d = str(ev.get("date") or ev.get("eventDate") or "")
                if d >= best_date:
                    best_date, best = d, vn
    if best:
        return best
    for key in ("vesselName", "vessel", "shipName", "currentVessel"):
        vn = _vn(_deep_get_first(raw, key))
        if vn:
            return vn
    return ""


def _container_departure(raw, etd_fallback=""):
    """Data wypłynięcia z portu załadunku (ISO 'YYYY-MM-DD' lub '').
    Kolejność: najwcześniejsze zdarzenie 'departure/sailed' → data POL z trasy → ETD."""
    from normalizer import normalize_date as _nd
    deps = []
    if isinstance(raw, dict):
        for cont in (raw.get("containers") or []):
            for ev in (cont.get("events") or []):
                desc = str(ev.get("description") or ev.get("eventType")
                           or ev.get("status") or ev.get("eventCode") or "").lower()
                if "depart" in desc or "sailed" in desc or "sailing" in desc:
                    _d = _nd(str(ev.get("date") or ev.get("eventDate") or ""))
                    if _d:
                        deps.append(_d.isoformat())
        if deps:
            return min(deps)   # najwcześniejsze wypłynięcie ≈ z portu załadunku
        pol = (raw.get("route") or {}).get("pol") or {}
        if isinstance(pol, dict) and pol.get("date"):
            _d = _nd(str(pol.get("date")))
            if _d:
                return _d.isoformat()
    if etd_fallback:
        _d = _nd(str(etd_fallback))
        if _d:
            return _d.isoformat()
    return ""


def _find_polyline(raw):
    """Najdłuższa lista par współrzędnych z odpowiedzi (geometria trasy morskiej).
    Zwraca [[lat, lng], ...]; zakłada zapis GeoJSON [lng, lat] i konwertuje."""
    best = []

    def walk(o, _d=0):
        nonlocal best
        if _d > 9 or o is None:
            return
        if isinstance(o, list):
            def _pair_dict(p):
                # Wierzchołek geometrii to GOŁE {lat,lng}; dict z nazwą/locode to
                # port/lokalizacja (np. locations[]) — NIE traktuj jako geometrii.
                if not isinstance(p, dict):
                    return None
                if any(k in p for k in ("name", "locationName", "locode", "unlocode")):
                    return None
                la = _coord_num(p.get("lat") if "lat" in p else p.get("latitude"))
                ln = _coord_num(p.get("lng") if "lng" in p else
                                (p.get("lon") if "lon" in p else p.get("longitude")))
                return (la, ln) if la is not None and ln is not None else None
            # Geometria trasy ma WIELE wierzchołków; krótkie listy to porty, nie trasa.
            _MIN_GEOM = 6
            if len(o) >= _MIN_GEOM and all(
                isinstance(p, (list, tuple)) and len(p) >= 2
                and _coord_num(p[0]) is not None and _coord_num(p[1]) is not None
                for p in o
            ):
                if len(o) > len(best):
                    conv = []
                    for p in o:
                        a, b = _coord_num(p[0]), _coord_num(p[1])
                        lng, lat = a, b           # GeoJSON [lng, lat]
                        if abs(b) > 90 and abs(a) <= 90:   # ewidentnie [lat, lng]
                            lat, lng = a, b
                        if -90 <= lat <= 90 and -180 <= lng <= 180:
                            conv.append([lat, lng])
                    if len(conv) > len(best):
                        best = conv
            elif len(o) >= _MIN_GEOM and all(_pair_dict(p) for p in o):
                conv = []
                for p in o:
                    la, ln = _pair_dict(p)
                    if -90 <= la <= 90 and -180 <= ln <= 180:
                        conv.append([la, ln])
                if len(conv) > len(best):
                    best = conv
            else:
                for v in o:
                    walk(v, _d + 1)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v, _d + 1)

    walk(raw)
    return best


def _coords_list(seg):
    """Lista par współrzędnych → [[lat, lng], ...]. Obsługuje [lng,lat] (GeoJSON),
    [lat,lng] oraz {lat,lng}. Pomija elementy bez sensownych współrzędnych."""
    if not isinstance(seg, list) or len(seg) < 2:
        return []
    out = []
    for p in seg:
        la = ln = None
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            a, b = _coord_num(p[0]), _coord_num(p[1])
            if a is None or b is None:
                continue
            ln, la = a, b                       # GeoJSON [lng, lat]
            if abs(b) > 90 and abs(a) <= 90:    # ewidentnie [lat, lng]
                la, ln = a, b
        elif isinstance(p, dict):
            la = _coord_num(p.get("lat") if "lat" in p else p.get("latitude"))
            ln = _coord_num(p.get("lng") if "lng" in p else
                            (p.get("lon") if "lon" in p else p.get("longitude")))
        if la is None or ln is None:
            continue
        if -90 <= la <= 90 and -180 <= ln <= 180:
            out.append([la, ln])
    return out


def _safecube_geometry(raw):
    """Pełna geometria trasy z SafeCube: skleja WSZYSTKIE segmenty współrzędnych z
    `routeData` (route[].path / geometry / coordinates / past+pending) w jedną
    linię [[lat, lng], ...]. Wielosegmentowa trasa (przeładunki) → ciągły szlak."""
    if not isinstance(raw, dict):
        return []
    rd = raw.get("routeData")
    if not isinstance(rd, (dict, list)):
        return []
    segments = []

    def _seg_from(v):
        # v może być listą par, albo dict-em z 'coordinates'/'path'
        if isinstance(v, dict):
            for k in ("coordinates", "path", "points", "line"):
                if isinstance(v.get(k), list):
                    return _coords_list(v[k])
            return []
        if isinstance(v, list):
            return _coords_list(v)
        return []

    # routeData jako lista segmentów albo dict z trasami
    containers = rd if isinstance(rd, list) else []
    if isinstance(rd, dict):
        for k in ("route", "pastRoute", "aisRoute", "pendingRoute",
                  "futureRoute", "segments", "paths"):
            v = rd.get(k)
            if isinstance(v, list):
                # lista segmentów lub bezpośrednia lista par
                if v and all(isinstance(x, (list, tuple)) and len(x) >= 2
                             and _coord_num(x[0]) is not None for x in v):
                    segments.append(_coords_list(v))   # bezpośrednia geometria
                else:
                    containers.append(v)
            elif isinstance(v, dict):
                seg = _seg_from(v)
                if seg:
                    segments.append(seg)
        # routeData.geometry/coordinates bezpośrednio
        for k in ("coordinates", "path", "geometry", "line"):
            v = rd.get(k)
            seg = _seg_from(v) if v is not None else []
            if seg:
                segments.append(seg)

    for grp in containers:
        if isinstance(grp, dict):
            # pojedynczy segment-dict (np. {path:[...]}) — gdy routeData to lista segmentów
            s = _seg_from(grp)
            if s:
                segments.append(s)
        elif isinstance(grp, list):
            direct = _coords_list(grp)   # lista par/coord-dictów = bezpośrednia geometria
            if len(direct) >= 2:
                segments.append(direct)
            else:                        # lista segmentów (list/dict) — rozłóż
                for seg in grp:
                    s = _seg_from(seg)
                    if s:
                        segments.append(s)

    # Sklej w kolejności, usuwając zduplikowane styki segmentów
    full = []
    for seg in segments:
        for c in seg:
            if not full or full[-1] != c:
                full.append(c)
    return full if len(full) >= 2 else []


def _safecube_route(raw):
    """Dane trasy do mapy: uporządkowane porty/przystanki, bieżąca pozycja i (gdy
    dostępna) geometria trasy morskiej. Defensywnie wobec schematu SafeCube."""
    out = {"points": [], "current": None, "path": []}
    if isinstance(raw, str):
        import json as _json
        try:
            raw = _json.loads(raw) if raw.strip() else {}
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        return out
    locations = raw.get("locations") if isinstance(raw.get("locations"), list) else []

    def _node(node):
        if isinstance(node, bool):
            return None
        if isinstance(node, int) and 0 <= node < len(locations):
            node = locations[node]
        if not isinstance(node, dict):
            return None
        name = (node.get("name") or node.get("locationName")
                or node.get("locode") or node.get("unlocode") or "")
        lat = _coord_num(node.get("lat") if "lat" in node else node.get("latitude"))
        lng = _coord_num(node.get("lng") if "lng" in node else
                         (node.get("lon") if "lon" in node else node.get("longitude")))
        if lat is None or lng is None:
            c = node.get("coordinates")
            if isinstance(c, dict):
                lat = _coord_num(c.get("lat") if "lat" in c else c.get("latitude"))
                lng = _coord_num(c.get("lng") if "lng" in c else
                                 (c.get("lon") if "lon" in c else c.get("longitude")))
            elif isinstance(c, list) and len(c) >= 2:
                lng, lat = _coord_num(c[0]), _coord_num(c[1])
        if lat is None or lng is None or (lat == 0 and lng == 0):
            return None
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return None
        return (str(name), lat, lng)

    route = raw.get("route") or {}
    ts = (route.get("ts") or route.get("transshipments")
          or route.get("tsPorts") or route.get("transhipments") or [])
    seq = []
    if route.get("prepol"):
        seq.append((route.get("prepol"), "Punkt początkowy", "stop"))
    if route.get("pol"):
        seq.append((route.get("pol"), "Port załadunku", "origin"))
    for t in (ts if isinstance(ts, list) else []):
        seq.append((t, "Przeładunek", "stop"))
    if route.get("pod"):
        seq.append((route.get("pod"), "Port docelowy", "dest"))
    if route.get("postpod"):
        seq.append((route.get("postpod"), "Punkt końcowy", "stop"))

    seen = set()
    for nd, label, typ in seq:
        node = nd.get("location") if isinstance(nd, dict) and "location" in nd else nd
        info = _node(node)
        if not info:
            continue
        nm, lat, lng = info
        key = (round(lat, 3), round(lng, 3))
        if key in seen:
            continue
        seen.add(key)
        date = nd.get("date") if isinstance(nd, dict) else ""
        out["points"].append({
            "name": nm or label, "lat": lat, "lng": lng,
            "type": typ, "label": label, "date": (str(date or ""))[:10],
        })

    # Przystanki z faktycznie zarejestrowanych zdarzeń (porty pośrednie po drodze),
    # jeśli nie pokryte przez route — dorzucamy jako 'stop'.
    for cont in (raw.get("containers") or []):
        for ev in (cont.get("events") or []):
            info = _node(ev.get("location"))
            if not info:
                continue
            nm, lat, lng = info
            key = (round(lat, 3), round(lng, 3))
            if key in seen:
                continue
            seen.add(key)
            out["points"].append({
                "name": nm, "lat": lat, "lng": lng, "type": "stop",
                "label": "Przystanek", "date": (str(ev.get("date") or ""))[:10],
            })

    cur = _safecube_position(raw)
    if cur:
        out["current"] = {"lat": cur[0], "lng": cur[1]}
    # Realna geometria: najpierw celowane sklejenie segmentów routeData (Sinay),
    # potem ogólne wyszukanie najdłuższej polilinii w odpowiedzi.
    out["path"] = _safecube_geometry(raw) or _find_polyline(raw)

    # Linia awaryjna (gdy brak realnej geometrii morskiej): prowadź ją przez
    # FAKTYCZNE punkty po kolei — port załadunku → odwiedzone przystanki wg daty
    # → bieżąca pozycja statku → port docelowy. Dzięki temu trasa zagina się tam,
    # gdzie statek był/jest (np. wokół Afryki), zamiast ciąć prosto przez ląd.
    origin_pt = next((p for p in out["points"] if p["type"] == "origin"), None)
    dest_pt = next((p for p in out["points"] if p["type"] == "dest"), None)
    mids = sorted((p for p in out["points"] if p["type"] == "stop"),
                  key=lambda p: p.get("date") or "")
    line = []
    if origin_pt:
        line.append([origin_pt["lat"], origin_pt["lng"]])
    for p in mids:
        line.append([p["lat"], p["lng"]])
    if out["current"]:
        line.append([out["current"]["lat"], out["current"]["lng"]])
    if dest_pt:
        line.append([dest_pt["lat"], dest_pt["lng"]])
    deduped = []
    for c in line:
        if not deduped or deduped[-1] != c:
            deduped.append(c)
    out["line"] = deduped
    return out


@app.route("/transport/map/<container_number>")
@login_required
def transport_map_page(container_number):
    """Mapa trasy kontenera: port załadunku → przystanki → pozycja → port docelowy
    (jak planowanie trasy), renderowana lokalnym Leafletem na kafelkach OSM."""
    from flask import abort
    cn = (container_number or "").strip().upper()
    if len(cn) > 20 or not cn.replace("-", "").isalnum():
        abort(400)
    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM container_tracking WHERE container_number=?", (cn,)
        ).fetchone()
    finally:
        db.close()
    if not row:
        abort(404)
    r = dict(row)
    import json as _json
    try:
        raw = _json.loads(r.get("raw_json") or "{}")
    except Exception:
        raw = {}
    route = _safecube_route(raw)
    departure = _container_departure(raw, r.get("etd", ""))
    transit_days = None
    if departure:
        from normalizer import normalize_date as _nd
        from datetime import datetime as _dt3, timezone as _tz3
        _dd = _nd(departure)
        if _dd:
            transit_days = max(0, (_dt3.now(_tz3.utc).date() - _dd).days)
    # Czy dane pozycji są nieświeże (>60 min)? Jeśli tak, mapa sama odświeży tracking.
    stale = True
    lc = r.get("last_checked", "")
    if lc:
        try:
            from datetime import datetime as _dt4, timezone as _tz4
            _lc = _dt4.strptime(str(lc)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_tz4.utc)
            stale = (_dt4.now(_tz4.utc) - _lc).total_seconds() > 3600
        except (ValueError, TypeError):
            stale = True
    return render_template(
        "transport_map.html", container=cn,
        carrier=r.get("carrier", ""), track_status=r.get("status", ""),
        origin_port=r.get("origin_port", ""), dest_port=r.get("dest_port", ""),
        eta=r.get("eta", ""), last_event=r.get("last_event", ""),
        last_checked=r.get("last_checked", ""), route_data=route,
        departure=departure, transit_days=transit_days,
        vessel=_safecube_vessel(raw), stale=stale,
        username=session.get("username"), role=session.get("role"))


def _transport_progress(etd, eta, today):
    """(pct, days_left) — pct = ile % trasy (czasowo ETD→ETA) za nami."""
    from normalizer import normalize_date as _nd
    de = _nd(str(etd)) if etd else None
    da = _nd(str(eta)) if eta else None
    days_left = (da - today).days if da else None
    pct = None
    if de and da and da > de:
        total = (da - de).days
        done = (today - de).days
        if total > 0:
            pct = max(0, min(100, round(done / total * 100)))
    elif da and days_left is not None and days_left <= 0:
        pct = 100
    return pct, days_left


def _radom_eta(discharge_date, eta, today):
    """Data dostawy do magazynu Radom = rozładunek (lub ETA) + ~3 dni transportu
    lądowego. Zwraca (iso_date, estimated) — estimated=True gdy liczone z ETA."""
    from normalizer import normalize_date as _nd
    from datetime import timedelta as _td
    base = _nd(str(discharge_date)) if discharge_date else None
    estimated = False
    if not base and eta:
        base = _nd(str(eta))
        estimated = True
    if not base:
        return "", False
    return (base + _td(days=3)).isoformat(), estimated


def _delivery_transport_view(d, today):
    """Pola pochodne dla wiersza kolejki transportu (postęp, mapa, data Radom,
    wypłynięcie i dni w trasie)."""
    import json as _json
    raw = {}
    _rj = d.get("raw_json")
    if _rj:
        try:
            raw = _json.loads(_rj) if isinstance(_rj, str) else (_rj or {})
        except Exception:
            raw = {}
    etd = d.get("track_etd") or d.get("etd_plan") or d.get("planowane_etd") or ""
    eta = d.get("track_eta") or d.get("kontener_eta") or d.get("data_dostawy") or ""
    pct, days_left = _transport_progress(etd, eta, today)
    radom, radom_est = _radom_eta(d.get("discharge_date"), eta, today)
    # Wypłynięcie z portu + ile dni już w trasie
    departure = _container_departure(raw, etd)
    transit_days = None
    if departure:
        from normalizer import normalize_date as _nd
        _dd = _nd(departure)
        if _dd:
            transit_days = max(0, (today - _dd).days)
    # Ile dni do dostawy w magazynie Radom (model analityczny — rozładunek +3 dni).
    radom_days = None
    if radom:
        try:
            from normalizer import normalize_date as _nd
            _zd = _nd(radom)
            if _zd:
                radom_days = (_zd - today).days
        except Exception:
            radom_days = None
    return {
        "_etd": etd[:10] if etd else "",
        "_eta": eta[:10] if eta else "",
        "_progress": pct,
        "_days_left": days_left,
        "_radom_date": radom,
        "_radom_estimated": radom_est,
        "_radom_days_left": radom_days,
        "_departure": departure,
        "_transit_days": transit_days,
        "_vessel": _safecube_vessel(raw),
        "_has_container": bool(d.get("container_number")),
    }


@app.route("/api/tracking/track", methods=["POST"])
@login_required
@csrf_protect
def api_tracking_track():
    """Śledź kontenery — provider SafeCube (preferowany) lub 17track.net."""
    data = request.get_json(silent=True) or {}
    numbers = data.get("numbers") or []
    if isinstance(numbers, str):
        numbers = [numbers]
    numbers = [n.strip().upper() for n in numbers if isinstance(n, str) and n.strip()]
    if not numbers:
        return jsonify({"error": "Podaj numer kontenera"}), 400
    if len(numbers) > 10:
        return jsonify({"error": "Maksymalnie 10 kontenerów naraz"}), 400

    provider = str(data.get("provider") or "auto").strip().lower()
    sealine = str(data.get("sealine") or "").strip().upper()

    try:
        results = _run_tracking_providers(numbers, provider, sealine)
    except _TrackError as e:
        return jsonify({"error": e.msg}), e.code

    uid = session["user_id"]
    _eta_changes = _store_tracking_results(results, uid)
    _notify_eta_changes(_eta_changes)
    return jsonify({"results": results, "eta_updated": len(_eta_changes)})


@app.route("/api/tracking/<container_number>/events")
@login_required
def api_tracking_events(container_number):
    import json as _json
    if len(container_number) > 20 or not container_number.replace("-", "").isalnum():
        return jsonify({"error": "Nieprawidłowy numer kontenera"}), 400
    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM container_tracking WHERE container_number=?",
            (container_number.upper(),)
        ).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        r = dict(row)
        try:
            r["events"] = _json.loads(r.get("events_json") or "[]")
        except Exception:
            r["events"] = []
        return jsonify(r)
    finally:
        db.close()


@app.route("/api/tracking/<container_number>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_tracking_delete(container_number):
    if len(container_number) > 20:
        return jsonify({"error": "Nieprawidłowy numer"}), 400
    db = get_db()
    try:
        db.execute("DELETE FROM container_tracking WHERE container_number=?", (container_number.upper(),))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


_TRACKING_PROVIDERS = {"17track", "maersk", "trackcargo", "safecube", "shipsgo"}
_TRACKING_ENV = {
    "17track": "TRACK17_API_KEY",
    "maersk": "MAERSK_CONSUMER_KEY",
    "trackcargo": "TRACKCARGO_API_KEY",
    "safecube": "SAFECUBE_API_KEY",
    "shipsgo": "SHIPSGO_API_KEY",
}


def _get_tracking_key(provider: str) -> str:
    """Read a tracking provider's API key: settings table first, env var fallback."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT value FROM settings WHERE category='tracking' AND key=?",
            (f"{provider}_api_key",)
        ).fetchone()
        key = (row["value"].strip() if row and row["value"] else "")
    finally:
        db.close()
    if not key:
        key = os.environ.get(_TRACKING_ENV.get(provider, ""), "").strip()
    return key


@app.route("/api/tracking/save-key", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_tracking_save_key():
    data = request.get_json(silent=True) or {}
    key = str(data.get("api_key") or "").strip()[:500]
    provider = str(data.get("provider") or "17track").strip().lower()
    if provider not in _TRACKING_PROVIDERS:
        return jsonify({"error": "Nieznany dostawca trackingu"}), 400
    db = get_db()
    try:
        db.execute(
            "INSERT INTO settings(category, key, value) VALUES('tracking',?,?) "
            "ON CONFLICT(category,key) DO UPDATE SET value=excluded.value",
            (f"{provider}_api_key", key)
        )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# TRANSPORT — KOLEJKA KONTENERÓW
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# RYZYKO POSTOJOWE — demurrage / detention (moduł transport)
# ─────────────────────────────────────────────────────────────────────────────

def _get_demurrage_settings(db):
    """Ustawienia ryzyka postojowego z settings (category='demurrage'), z domyślnymi."""
    import demurrage as _dm
    cfg = dict(_dm.DEFAULTS)
    try:
        for r in db.execute("SELECT key, value FROM settings WHERE category='demurrage'").fetchall():
            k = r["key"]
            v = r["value"]
            if k in cfg and v not in (None, ""):
                try:
                    cfg[k] = float(v) if "rate" in k else int(float(v))
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    return cfg


def _container_milestone_dates(track_row):
    """Daty kamieni milowych kontenera: ręczne nadpisanie > zdarzenia z API trackingu."""
    import json as _json
    import demurrage as _dm
    try:
        events = _json.loads(track_row.get("events_json") or "[]")
    except Exception:
        events = []
    auto = _dm.extract_milestone_dates(events)
    out = {}
    for key, col in (("discharge", "discharge_date"),
                     ("gate_out", "gate_out_date"),
                     ("empty_return", "empty_return_date")):
        out[key] = _dm.parse_date(track_row.get(col) or "") or auto.get(key)
    return out


def _assess_container_risk(track_row, cfg):
    """Ocena ryzyka postojowego dla wiersza container_tracking (dict)."""
    import demurrage as _dm
    from datetime import datetime as _dt2, timezone as _tz2
    return _dm.assess_container(_container_milestone_dates(track_row), settings=cfg,
                                today=_dt2.now(_tz2.utc).date())


def _check_demurrage_alerts(db, numbers):
    """Po odświeżeniu trackingu: alert managerom, gdy kontener wchodzi w 🟡/🔴.
    Dedup po demurrage_notified_level — alert tylko przy zmianie poziomu."""
    if not numbers:
        return
    cfg = _get_demurrage_settings(db)
    for nr in numbers:
        try:
            row = db.execute(
                "SELECT * FROM container_tracking WHERE container_number=?", (nr,)
            ).fetchone()
            if not row:
                continue
            d = dict(row)
            risk = _assess_container_risk(d, cfg)
            level = risk["overall_level"]
            prev = d.get("demurrage_notified_level") or ""
            if level in ("amber", "red") and level != prev:
                emoji = "🔴" if level == "red" else "🟡"
                parts = []
                dem, det = risk["demurrage"], risk["detention"]
                if dem["active"]:
                    parts.append(f"Demurrage {dem['days_used']}/{dem['free_days']} dni")
                if det["active"]:
                    parts.append(f"Detention {det['days_used']}/{det['free_days']} dni")
                if risk["total_cost"]:
                    parts.append(f"szac. koszt {risk['total_cost']:.2f}")
                _notify_managers(
                    f"{emoji} Ryzyko postojowe — kontener {nr}",
                    "; ".join(parts), link="/transport/queue",
                    email=(level == "red"),  # e-mail tylko przy 🔴 (po terminie)
                )
                db.execute(
                    "UPDATE container_tracking SET demurrage_notified_level=? WHERE container_number=?",
                    (level, nr))
            elif level not in ("amber", "red") and prev:
                # Ryzyko ustąpiło (kontener odebrany/zwrócony) — wyzeruj dedup
                db.execute(
                    "UPDATE container_tracking SET demurrage_notified_level='' WHERE container_number=?",
                    (nr,))
        except Exception:
            pass
    try:
        db.commit()
    except Exception:
        pass


def _upsert_demurrage_dates(db, container_number, data, uid):
    """Upsert ręcznych dat postojowych do container_tracking.
    Aktualizuje TYLKO klucze faktycznie obecne w 'data' — nie kasuje pozostałych
    (klient API może wysłać jedną datę bez wymazywania dwóch innych)."""
    import demurrage as _dm
    keys = ("discharge_date", "gate_out_date", "empty_return_date")
    present = [k for k in keys if k in data]
    cnum = str(container_number or "").strip().upper()[:50]
    if not present or not cnum:
        return
    vals = {}
    for k in present:
        raw = str(data.get(k) or "").strip()
        vals[k] = raw[:10] if (raw == "" or _dm.parse_date(raw)) else ""
    set_clause = ", ".join(f"{k}=excluded.{k}" for k in present)
    db.execute(
        # bandit: nazwy kolumn z białej listy w kodzie (nie z kluczy żądania); wartości przez ?
        "INSERT INTO container_tracking"  # nosec B608
        "(container_number, discharge_date, gate_out_date, empty_return_date, created_by) "
        "VALUES(?,?,?,?,?) "
        f"ON CONFLICT(container_number) DO UPDATE SET {set_clause}",
        (cnum, vals.get("discharge_date", ""), vals.get("gate_out_date", ""),
         vals.get("empty_return_date", ""), uid))


@app.route("/settings/demurrage", methods=["GET"])
@require_role("manager")
def demurrage_settings_page():
    db = get_db()
    try:
        cfg = _get_demurrage_settings(db)
    finally:
        db.close()
    return render_template("demurrage_settings.html",
                           cfg=cfg, username=session.get("username"),
                           role=session.get("role"))


@app.route("/api/settings/demurrage", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_demurrage_settings_save():
    import demurrage as _dm
    data = request.get_json(silent=True) or request.form
    db = get_db()
    saved = {}
    try:
        for k in _dm.DEFAULTS:
            if k in data and str(data.get(k)).strip() != "":
                try:
                    val = float(data.get(k)) if "rate" in k else int(float(data.get(k)))
                    if val < 0:
                        continue
                except (TypeError, ValueError):
                    continue
                db.execute(
                    "INSERT INTO settings(category, key, value) VALUES('demurrage', ?, ?) "
                    "ON CONFLICT(category, key) DO UPDATE SET value=excluded.value",
                    (k, str(val)))
                saved[k] = val
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, "saved": saved})


@app.route("/transport/queue")
@login_required
def transport_queue_page():
    """Kolejka transportu — dostawy z głównego panelu (kolejka_zlecenia). Do każdej
    dostawy przypisuje się kontener, który automatycznie wchodzi do trackingu API
    (statusy, mapa, pasek postępu, data dostawy do magazynu Radom = rozładunek +3 dni)."""
    # Leniwe auto-odświeżanie trackingu (raz na dobę, w tle) — wyzwalane wejściem.
    try:
        _maybe_auto_refresh_tracking(session.get("user_id"))
    except Exception:
        pass
    db = get_db()
    try:
        where = []
        params = []
        # Widoczność per dział — jak w /dostawy (superuser/admin widzą wszystko).
        if not can_see_all(session.get("role", "")):
            where.append("(COALESCE(z.department,'')='' OR z.department=?)")
            params.append(session.get("department") or "")
        sql = (
            "SELECT z.id, z.nr_zamowienia, z.supplier_name, z.supplier_code, "
            "       z.status, z.priorytet, z.planowane_etd, z.data_dostawy, z.kontener_id, "
            "       kk.numer_kontenera AS container_number, kk.sealine, "
            "       kk.etd_plan, kk.eta AS kontener_eta, "
            "       ct.status AS track_status, ct.carrier, ct.origin_port, ct.dest_port, "
            "       ct.etd AS track_etd, ct.eta AS track_eta, ct.last_event, ct.last_event_date, "
            "       ct.last_checked, ct.raw_json, ct.events_json, "
            "       ct.discharge_date, ct.gate_out_date, ct.empty_return_date "
            "FROM kolejka_zlecenia z "
            "LEFT JOIN kolejka_kontenery kk ON z.kontener_id = kk.id "
            "LEFT JOIN container_tracking ct ON ct.container_number = kk.numer_kontenera "
        )
        if where:
            sql += "WHERE " + " AND ".join(where) + " "
        # Z kontenerem i z najbliższą datą — na górze; rozliczone — na dół.
        sql += ("ORDER BY CASE WHEN z.status='rozliczone' THEN 1 ELSE 0 END ASC, "
                "(COALESCE(ct.eta, z.data_dostawy, '')='') ASC, "
                "COALESCE(ct.eta, z.data_dostawy) ASC, z.created_at DESC")
        rows = db.execute(sql, params).fetchall()

        from datetime import datetime as _dt, timezone as _tz
        today = _dt.now(_tz.utc).date()
        _dem_cfg = _get_demurrage_settings(db)
        _risk_rank = {"red": 3, "amber": 2, "green": 1, "none": 0}
        items = []
        for r in rows:
            d = dict(r)
            d.update(_delivery_transport_view(d, today))
            # Ryzyko postojowe (demurrage/detention) — gdy są daty rozładunku/odbioru.
            try:
                if d.get("container_number"):
                    _risk = _assess_container_risk(d, _dem_cfg)
                    d["_risk"] = _risk
                    d["_risk_rank"] = _risk_rank.get(_risk["overall_level"], 0)
                else:
                    d["_risk"] = None
                    d["_risk_rank"] = 0
            except Exception:
                d["_risk"] = None
                d["_risk_rank"] = 0
            items.append(d)

        _with_container = sum(1 for x in items if x.get("container_number"))
        _in_transit = sum(1 for x in items
                          if (x.get("_days_left") is not None and x["_days_left"] > 0
                              and x.get("container_number")))
        _urgent = sum(1 for x in items
                      if x.get("_days_left") is not None and 0 <= x["_days_left"] <= 3)
        _at_risk = sum(1 for x in items if x.get("_risk_rank", 0) >= 2)
        api_calls_month = _get_tracking_usage_month(db)
        return render_template("transport_queue.html",
                               items=items,
                               cnt_with_container=_with_container,
                               cnt_in_transit=_in_transit,
                               cnt_urgent=_urgent,
                               at_risk_count=_at_risk,
                               api_calls_month=api_calls_month,
                               username=session.get("username"),
                               role=session.get("role"))
    finally:
        db.close()


@app.route("/api/transport/queue/assign", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_transport_queue_assign():
    """Przypisz (lub odepnij) kontener do dostawy z kolejki. Po przypisaniu
    kontener jest od razu śledzony przez API (status, ETA, mapa) i ETA propaguje
    się na datę dostawy. Pusty numer = odpięcie kontenera od dostawy."""
    data = request.get_json(silent=True) or {}
    try:
        zid = int(data.get("zlecenie_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Brak/nieprawidłowy identyfikator dostawy"}), 400
    container = str(data.get("container_number") or "").strip().upper()[:50]
    sealine = str(data.get("sealine") or "").strip().upper()[:10]
    uid = session["user_id"]

    db = get_db()
    try:
        z = db.execute("SELECT id, nr_zamowienia FROM kolejka_zlecenia WHERE id=?",
                       (zid,)).fetchone()
        if not z:
            return jsonify({"error": "Nie znaleziono dostawy"}), 404
        # Odpięcie kontenera
        if not container:
            db.execute("UPDATE kolejka_zlecenia SET kontener_id=NULL, updated_at=datetime('now') "
                       "WHERE id=?", (zid,))
            db.commit()
            _log_audit("transport_detach_container", session.get("username"),
                       f"Dostawa {dict(z).get('nr_zamowienia')}: odpięto kontener")
            return jsonify({"ok": True, "detached": True})
        if not container.replace("-", "").isalnum():
            return jsonify({"error": "Nieprawidłowy numer kontenera"}), 400
        # Upsert kontenera w kolejka_kontenery i spięcie z dostawą
        krow = db.execute("SELECT id FROM kolejka_kontenery WHERE numer_kontenera=?",
                          (container,)).fetchone()
        if krow:
            kid = dict(krow)["id"]
            if sealine:
                db.execute("UPDATE kolejka_kontenery SET sealine=?, updated_at=datetime('now') "
                           "WHERE id=?", (sealine, kid))
        else:
            cur = db.execute(
                "INSERT INTO kolejka_kontenery (numer_kontenera, sealine, created_by) "
                "VALUES (?,?,?)", (container, sealine, uid))
            kid = cur.lastrowid
        db.execute("UPDATE kolejka_zlecenia SET kontener_id=?, updated_at=datetime('now') "
                   "WHERE id=?", (kid, zid))
        db.commit()
        _log_audit("transport_assign_container", session.get("username"),
                   f"Dostawa {dict(z).get('nr_zamowienia')}: przypisano kontener {container}")
    finally:
        db.close()

    # Tracking poza transakcją — przypisanie pozostaje nawet gdy API zawiedzie.
    track_result = None
    track_warning = None
    try:
        results = _run_tracking_providers([container], "auto", sealine)
        eta_changes = _store_tracking_results(results, uid)
        _notify_eta_changes(eta_changes)
        track_result = results[0] if results else None
    except _TrackError as e:
        track_warning = e.msg
    except Exception as e:
        logger.error("Tracking po przypisaniu kontenera %s nie powiódł się: %s", container, e)
        track_warning = "Kontener przypisany, ale tracking chwilowo niedostępny."

    return jsonify({"ok": True, "container_number": container,
                    "result": track_result, "track_warning": track_warning})


@app.route("/calendar")
@login_required
def calendar_page():
    db = get_db()
    try:
        try:
            containers = db.execute(
                "SELECT id, container_number, po_numbers, etd, eta, urgency, customs_status "
                "FROM transport_queue ORDER BY eta, etd"
            ).fetchall()
            containers = [dict(r) for r in containers]
        except Exception:
            containers = []
        sad_pending = db.execute(
            "SELECT id, po_number, created_at FROM comparisons "
            "WHERE doc_type='SAD' AND (approval_status IS NULL OR approval_status='pending') "
            "AND (is_deleted IS NULL OR is_deleted=0) ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        sad_list = [dict(r) for r in sad_pending]
    finally:
        db.close()
    return render_template("calendar.html",
                           containers=containers,
                           sad_list=sad_list,
                           username=session["username"],
                           role=session["role"])


@app.route("/api/transport/queue", methods=["GET"])
@login_required
def api_transport_queue_list():
    db = get_db()
    try:
        try:
            limit = max(1, min(int(request.args.get("limit", 500)), 2000))
        except (ValueError, TypeError):
            limit = 500
        rows = db.execute(
            "SELECT * FROM transport_queue ORDER BY eta ASC LIMIT ?", (limit,)
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@app.route("/api/transport/queue", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_transport_queue_create():
    data = request.get_json(silent=True) or {}
    container = str(data.get("container_number") or "").strip().upper()[:50]
    if not container:
        return jsonify({"error": "Numer kontenera jest wymagany"}), 400
    customs_status = data.get("customs_status", "oczekuje")
    if customs_status not in _CUSTOMS_STATUSES:
        customs_status = "oczekuje"
    urgency = data.get("urgency", "normalne")
    if urgency not in ("normalne", "pilne", "spokojnie"):
        urgency = "normalne"
    uid = session["user_id"]
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO transport_queue"
            "(container_number, po_numbers, etd, eta, origin_port, dest_port, "
            " unload_location, carrier, customs_status, urgency, notes, created_by) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (container, str(data.get("po_numbers") or "").strip()[:500],
             str(data.get("etd") or "").strip()[:20], str(data.get("eta") or "").strip()[:20],
             str(data.get("origin_port") or "").strip()[:100], str(data.get("dest_port") or "").strip()[:100],
             str(data.get("unload_location") or "").strip()[:200], str(data.get("carrier") or "").strip()[:100],
             customs_status, urgency,
             str(data.get("notes") or "").strip()[:1000], uid)
        )
        db.commit()
        qid = cur.lastrowid
        # Ręczne daty postojowe (jeśli podane przy dodawaniu) → container_tracking
        if any(str(data.get(k) or "").strip()
               for k in ("discharge_date", "gate_out_date", "empty_return_date")):
            _upsert_demurrage_dates(db, container, data, uid)
            db.commit()
        # Notify if urgency is pilne or ETA within 3 days
        try:
            eta = data.get("eta", "")
            urgency = data.get("urgency", "normalne")
            from datetime import date as _date
            is_urgent = urgency == "pilne"
            if not is_urgent and eta:
                try:
                    eta_d = _date.fromisoformat(eta)
                    is_urgent = (eta_d - _date.today()).days <= 3
                except Exception:
                    pass
            if is_urgent:
                _notify_role("manager", "container_urgent",
                             f"🚢 Pilny kontener: {container}",
                             f"ETA: {eta or '—'} · PO: {data.get('po_numbers','—')}",
                             "/transport/queue")
        except Exception:
            pass
        # Link matching shipments to this queue entry
        try:
            po_nums = [p.strip() for p in str(data.get("po_numbers") or "").split(",") if p.strip()]
            if po_nums:
                _db2 = get_db()
                try:
                    for _po in po_nums:
                        _db2.execute(
                            "UPDATE shipments SET transport_queue_id=?, updated_at=datetime('now') "
                            "WHERE po_number=? AND (transport_queue_id IS NULL OR transport_queue_id=0)",
                            (qid, _po)
                        )
                    _db2.commit()
                finally:
                    _db2.close()
        except Exception:
            pass
        return jsonify({"ok": True, "id": qid})
    finally:
        db.close()


@app.route("/api/transport/queue/<int:qid>", methods=["PUT"])
@require_role("manager")
@csrf_protect
def api_transport_queue_update(qid):
    data = request.get_json(silent=True) or {}
    _tq_max_lens = {
        "container_number": 50, "po_numbers": 500, "etd": 20, "eta": 20,
        "origin_port": 100, "dest_port": 100, "unload_location": 200,
        "carrier": 100, "notes": 1000, "customs_agent": 200,
    }
    allowed = ["container_number","po_numbers","etd","eta","origin_port","dest_port",
               "unload_location","carrier","customs_status","urgency","notes","customs_agent"]
    fields, vals = [], []
    for k in allowed:
        if k in data:
            v = data[k]
            if k == "customs_status" and v not in _CUSTOMS_STATUSES:
                continue
            if k == "urgency" and v not in ("normalne", "pilne", "spokojnie"):
                continue
            if k in _tq_max_lens:
                v = str(v or "")[:_tq_max_lens[k]]
            fields.append(f"{k}=?")
            vals.append(v)
    # Przypisanie firmy spedycyjnej = wysłanie zlecenia spedycyjnego do portalu Spedycja
    if "forwarder_id" in data:
        _fwd_raw = data.get("forwarder_id")
        try:
            _fwd_id = int(_fwd_raw) if _fwd_raw not in (None, "", False) else None
        except (TypeError, ValueError):
            _fwd_id = None
        fields.append("forwarder_id=?")
        vals.append(_fwd_id)
        if data.get("forwarder_id"):
            fields.append(
                "sent_to_forwarder_at=CASE WHEN sent_to_forwarder_at IS NULL OR sent_to_forwarder_at='' "
                "THEN datetime('now') ELSE sent_to_forwarder_at END"
            )
    # Przypisanie agencji celnej = skierowanie zlecenia do portalu Agencji
    if "customs_agency_id" in data:
        _ag_raw = data.get("customs_agency_id")
        try:
            _ag_id = int(_ag_raw) if _ag_raw not in (None, "", False) else None
        except (TypeError, ValueError):
            _ag_id = None
        fields.append("customs_agency_id=?")
        vals.append(_ag_id)
        if data.get("customs_agency_id"):
            fields.append(
                "sent_to_agency_at=CASE WHEN sent_to_agency_at IS NULL OR sent_to_agency_at='' "
                "THEN datetime('now') ELSE sent_to_agency_at END"
            )
    if not fields:
        return jsonify({"error": "Brak pól"}), 400
    fields.append("updated_at=datetime('now')")
    vals.append(qid)
    db = get_db()
    try:
        # bandit: nazwy kolumn z białej listy w kodzie (nie z kluczy żądania); wartości przez ?
        db.execute(f"UPDATE transport_queue SET {', '.join(fields)} WHERE id=?", vals)  # nosec B608
        # Ręczne nadpisanie dat postojowych (demurrage/detention) → container_tracking.
        # Pusty string = brak nadpisania (wraca do dat z API trackingu).
        cnum = str(data.get("container_number") or "").strip().upper()[:50]
        if not cnum and any(k in data for k in
                            ("discharge_date", "gate_out_date", "empty_return_date")):
            _r = db.execute(
                "SELECT container_number FROM transport_queue WHERE id=?", (qid,)).fetchone()
            cnum = (dict(_r).get("container_number") if _r else "") or ""
        _upsert_demurrage_dates(db, cnum, data, session.get("user_id"))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/transport/queue/<int:qid>", methods=["DELETE"])
@require_role("manager")
@csrf_protect
def api_transport_queue_delete(qid):
    db = get_db()
    try:
        db.execute("DELETE FROM transport_queue WHERE id=?", (qid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/transport/forwarders", methods=["GET"])
@login_required
def api_transport_forwarders_list():
    """Lista aktywnych firm spedycyjnych (do przypisywania zleceń)."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, name, company, email FROM freight_forwarders WHERE active=1 ORDER BY name"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# SPEDYCJA — portal zewnętrzny dla spedytorów (rola forwarder)
# ─────────────────────────────────────────────────────────────────────────────

# Statusy odprawy (zgodne z CHECK na transport_queue.customs_status)
_CUSTOMS_STATUSES = ["oczekuje", "w_odprawie", "zatwierdzone", "odrzucone", "wydane"]


@app.route("/agencja")
@login_required
def agencja_page():
    """Portal agencji celnej: dostawy skierowane do odprawy, przypisane do agencji.

    Admin/superuser bez przypisanej agencji widzą PODGLĄD wszystkich zleceń
    (tryb testowy/rozwojowy) — tylko do odczytu."""
    aid = session.get("customs_agency_id")
    role = session.get("role")
    orders, agency_name = [], ""
    admin_preview = False
    db = get_db()
    try:
        if aid:
            rows = db.execute(
                "SELECT id, container_number, po_numbers, etd, eta, origin_port, dest_port, "
                "carrier, customs_status, customs_agent, notes "
                "FROM transport_queue WHERE customs_agency_id=? AND sent_to_agency_at != '' "
                "ORDER BY (eta='' OR eta IS NULL) ASC, eta ASC", (aid,)
            ).fetchall()
            orders = [dict(r) for r in rows]
            ar = db.execute(
                "SELECT name, company FROM customs_agencies WHERE id=?", (aid,)).fetchone()
            if ar:
                agency_name = (ar["company"] or ar["name"] or "").strip()
        elif can_see_all(role):
            admin_preview = True
            agency_name = "Podgląd administratora — wszystkie agencje"
            rows = db.execute(
                "SELECT tq.id, tq.container_number, tq.po_numbers, tq.etd, tq.eta, "
                "tq.origin_port, tq.dest_port, tq.carrier, tq.customs_status, "
                "tq.customs_agent, tq.notes, "
                "COALESCE(ca.company, ca.name, '—') AS agency_company "
                "FROM transport_queue tq LEFT JOIN customs_agencies ca ON tq.customs_agency_id=ca.id "
                "ORDER BY (tq.eta='' OR tq.eta IS NULL) ASC, tq.eta ASC"
            ).fetchall()
            orders = [dict(r) for r in rows]
    finally:
        db.close()
    return render_template("agencja.html",
                           username=session.get("username"), role=role,
                           agency_name=agency_name, admin_preview=admin_preview,
                           statuses=_CUSTOMS_STATUSES, orders=orders)


@app.route("/api/agencja/queue/<int:qid>", methods=["POST"])
@login_required
@csrf_protect
def api_agencja_update(qid):
    """Agencja celna aktualizuje status odprawy — wyłącznie własne zlecenia."""
    if session.get("role") != "customs_agent":
        return jsonify({"error": "Brak uprawnień"}), 403
    aid = session.get("customs_agency_id")
    if not aid:
        return jsonify({"error": "Konto bez przypisanej agencji celnej"}), 403
    data = request.get_json(silent=True) or {}
    st = str(data.get("customs_status") or "").strip()
    if st not in _CUSTOMS_STATUSES:
        return jsonify({"error": "Nieprawidłowy status odprawy"}), 400
    db = get_db()
    try:
        cur = db.execute(
            "UPDATE transport_queue SET customs_status=?, updated_at=datetime('now') "
            "WHERE id=? AND customs_agency_id=? AND sent_to_agency_at != ''",
            (st, qid, aid))
        db.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "Nie znaleziono zlecenia"}), 404
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/customs/agencies", methods=["GET"])
@login_required
def api_customs_agencies_list():
    """Lista agencji celnych (do przypisania konta agenta w panelu admina)."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, name, company, email FROM customs_agencies WHERE active=1 ORDER BY name"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@app.route("/api/customs/agencies", methods=["POST"])
@login_required
@require_role("admin")
@csrf_protect
def api_customs_agencies_create():
    """Utworzenie agencji celnej (admin)."""
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()[:200]
    if not name:
        return jsonify({"error": "Podaj nazwę agencji"}), 400
    company = str(data.get("company") or "").strip()[:200]
    email = str(data.get("email") or "").strip().lower()[:254]
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO customs_agencies (name, company, email) VALUES (?,?,?)",
            (name, company, email))
        db.commit()
        return jsonify({"ok": True, "id": cur.lastrowid})
    finally:
        db.close()


@app.route("/spedycja")
@login_required
def spedycja_page():
    """Portal spedytora: lista zleceń spedycyjnych przypisanych do jego firmy.

    Admin/superuser bez przypisanej firmy widzą PODGLĄD wszystkich zleceń
    (tryb testowy/rozwojowy) — tylko do odczytu, edycja zostaje po stronie
    konta spedytora."""
    fid = session.get("forwarder_id")
    role = session.get("role")
    orders, forwarder_name = [], ""
    admin_preview = False
    db = get_db()
    try:
        if fid:
            rows = db.execute(
                "SELECT id, container_number, po_numbers, etd, eta, origin_port, dest_port, "
                "carrier, customs_status, customs_agent, sent_to_forwarder_at, notes "
                "FROM transport_queue "
                "WHERE forwarder_id=? AND sent_to_forwarder_at != '' "
                "ORDER BY (eta='' OR eta IS NULL) ASC, eta ASC",
                (fid,)
            ).fetchall()
            orders = [dict(r) for r in rows]
            fr = db.execute(
                "SELECT name, company FROM freight_forwarders WHERE id=?", (fid,)
            ).fetchone()
            if fr:
                forwarder_name = (fr["company"] or fr["name"] or "").strip()
        elif can_see_all(role):
            # Podgląd admina/superusera — wszystkie zlecenia, niezależnie od firmy.
            admin_preview = True
            forwarder_name = "Podgląd administratora — wszystkie firmy"
            rows = db.execute(
                "SELECT tq.id, tq.container_number, tq.po_numbers, tq.etd, tq.eta, "
                "tq.origin_port, tq.dest_port, tq.carrier, tq.customs_status, "
                "tq.customs_agent, tq.sent_to_forwarder_at, tq.notes, "
                "COALESCE(ff.company, ff.name, '—') AS forwarder_company "
                "FROM transport_queue tq "
                "LEFT JOIN freight_forwarders ff ON tq.forwarder_id=ff.id "
                "ORDER BY (tq.eta='' OR tq.eta IS NULL) ASC, tq.eta ASC"
            ).fetchall()
            orders = [dict(r) for r in rows]
    finally:
        db.close()
    return render_template("spedycja.html",
                           username=session.get("username"),
                           role=role,
                           forwarder_name=forwarder_name,
                           admin_preview=admin_preview,
                           statuses=_CUSTOMS_STATUSES,
                           orders=orders)


@app.route("/api/spedycja/queue/<int:qid>", methods=["POST"])
@login_required
@csrf_protect
def api_spedycja_update(qid):
    """Spedytor aktualizuje agenta celnego i status odprawy — tylko własne zlecenia."""
    if session.get("role") not in EXTERNAL_ROLES:
        return jsonify({"error": "Brak uprawnień"}), 403
    fid = session.get("forwarder_id")
    if not fid:
        return jsonify({"error": "Konto bez przypisanej firmy spedycyjnej"}), 403
    data = request.get_json(silent=True) or {}
    fields, vals = [], []
    if "customs_agent" in data:
        fields.append("customs_agent=?")
        vals.append(str(data.get("customs_agent") or "").strip()[:200])
    if "customs_status" in data:
        st = str(data.get("customs_status") or "").strip()
        if st not in _CUSTOMS_STATUSES:
            return jsonify({"error": "Nieprawidłowy status odprawy"}), 400
        fields.append("customs_status=?")
        vals.append(st)
    if not fields:
        return jsonify({"error": "Brak pól do zapisu"}), 400
    fields.append("updated_at=datetime('now')")
    db = get_db()
    try:
        cur = db.execute(
            # bandit: nazwy kolumn z białej listy w kodzie (nie z kluczy żądania); wartości przez ?
            f"UPDATE transport_queue SET {', '.join(fields)} "  # nosec B608
            "WHERE id=? AND forwarder_id=? AND sent_to_forwarder_at != ''",
            (*vals, qid, fid)
        )
        db.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "Zlecenie nie znalezione lub brak dostępu"}), 404
        return jsonify({"ok": True})
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# TRANSPORT — SPEDYCJA / KIEROWCY
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/transport/dispatch")
@login_required
def transport_dispatch_page():
    db = get_db()
    try:
        forwarders = db.execute(
            "SELECT * FROM freight_forwarders WHERE active=1 ORDER BY name"
        ).fetchall()
        drivers = db.execute(
            "SELECT d.*, ff.name as forwarder_name "
            "FROM drivers d "
            "LEFT JOIN freight_forwarders ff ON d.forwarder_id=ff.id "
            "WHERE d.active=1 ORDER BY d.name"
        ).fetchall()
        notifications = db.execute(
            "SELECT dn.*, d.name as driver_name, d.phone as driver_phone "
            "FROM driver_notifications dn "
            "LEFT JOIN drivers d ON dn.driver_id=d.id "
            "ORDER BY dn.created_at DESC LIMIT 50"
        ).fetchall()
        # Get SMS settings
        sms_cfg_row = db.execute(
            "SELECT value FROM settings WHERE category='sms' AND key='provider'"
        ).fetchone()
        sms_provider = sms_cfg_row["value"] if sms_cfg_row else ""
    finally:
        db.close()
    return render_template("transport_dispatch.html",
                           forwarders=[dict(f) for f in forwarders],
                           drivers=[dict(d) for d in drivers],
                           notifications=[dict(n) for n in notifications],
                           sms_provider=sms_provider,
                           username=session.get("username"),
                           role=session.get("role"))


# Forwarder CRUD
@app.route("/api/transport/forwarders", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_forwarder_create():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()[:200]
    if not name:
        return jsonify({"error": "Nazwa wymagana"}), 400
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO freight_forwarders(name, company, phone, email, nip, address, notes) "
            "VALUES(?,?,?,?,?,?,?)",
            (name, str(data.get("company") or "")[:200], str(data.get("phone") or "")[:40],
             str(data.get("email") or "")[:254], str(data.get("nip") or "")[:20],
             str(data.get("address") or "")[:500], str(data.get("notes") or "")[:1000])
        )
        db.commit()
        return jsonify({"ok": True, "id": cur.lastrowid})
    finally:
        db.close()


@app.route("/api/transport/forwarders/<int:fid>", methods=["PUT"])
@require_role("manager")
@csrf_protect
def api_forwarder_update(fid):
    data = request.get_json(silent=True) or {}
    _ff_max_lens = {"name": 200, "company": 200, "phone": 40, "email": 254, "nip": 20, "address": 500, "notes": 1000}
    fields, vals = [], []
    for k in ("name","company","phone","email","nip","address","notes","active"):
        if k in data:
            v = data[k]
            if k == "active":
                v = 1 if v else 0
            elif isinstance(v, str) and k in _ff_max_lens:
                v = v[:_ff_max_lens[k]]
            elif k in _ff_max_lens:
                v = str(v or "")[:_ff_max_lens[k]]
            fields.append(f"{k}=?")
            vals.append(v)
    if not fields:
        return jsonify({"error": "Brak pól"}), 400
    vals.append(fid)
    db = get_db()
    try:
        # bandit: nazwy kolumn z białej listy w kodzie (nie z kluczy żądania); wartości przez ?
        db.execute(f"UPDATE freight_forwarders SET {', '.join(fields)} WHERE id=?", vals)  # nosec B608
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/transport/forwarders/<int:fid>", methods=["DELETE"])
@login_required
@require_role("admin")
@csrf_protect
def api_forwarder_delete(fid):
    db = get_db()
    try:
        row = db.execute("SELECT id FROM freight_forwarders WHERE id=?", (fid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono spedytora"}), 404
        db.execute("DELETE FROM freight_forwarders WHERE id=?", (fid,))
        db.commit()
        _log_audit("forwarder_delete", session["username"], f"fid={fid}")
        return jsonify({"ok": True})
    finally:
        db.close()


# Driver CRUD
@app.route("/api/transport/drivers", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_driver_create():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()[:200]
    phone = str(data.get("phone") or "").strip()[:40]
    if not name or not phone:
        return jsonify({"error": "Imię i telefon są wymagane"}), 400
    db = get_db()
    try:
        _drv_fwd_raw = data.get("forwarder_id")
        try:
            _drv_fwd_id = int(_drv_fwd_raw) if _drv_fwd_raw not in (None, "", False) else None
        except (TypeError, ValueError):
            _drv_fwd_id = None
        cur = db.execute(
            "INSERT INTO drivers(name, phone, truck_plate, forwarder_id, notes) "
            "VALUES(?,?,?,?,?)",
            (name, phone, str(data.get("truck_plate") or "")[:30],
             _drv_fwd_id, str(data.get("notes") or "")[:500])
        )
        db.commit()
        return jsonify({"ok": True, "id": cur.lastrowid})
    finally:
        db.close()


@app.route("/api/transport/drivers/<int:did>", methods=["PUT"])
@require_role("manager")
@csrf_protect
def api_driver_update(did):
    data = request.get_json(silent=True) or {}
    _drv_max_lens = {"name": 200, "phone": 40, "truck_plate": 30, "notes": 500}
    fields, vals = [], []
    for k in ("name","phone","truck_plate","forwarder_id","notes","active"):
        if k in data:
            v = data[k]
            if k == "forwarder_id":
                try:
                    v = int(v) if v not in (None, "", False) else None
                except (TypeError, ValueError):
                    v = None
            elif k == "active":
                v = 1 if v else 0
            elif k in _drv_max_lens:
                v = str(v or "")[:_drv_max_lens[k]]
            fields.append(f"{k}=?")
            vals.append(v)
    if not fields:
        return jsonify({"error": "Brak pól"}), 400
    vals.append(did)
    db = get_db()
    try:
        # bandit: nazwy kolumn z białej listy w kodzie (nie z kluczy żądania); wartości przez ?
        db.execute(f"UPDATE drivers SET {', '.join(fields)} WHERE id=?", vals)  # nosec B608
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/transport/drivers/<int:did>", methods=["DELETE"])
@login_required
@require_role("admin")
@csrf_protect
def api_driver_delete(did):
    db = get_db()
    try:
        row = db.execute("SELECT id FROM drivers WHERE id=?", (did,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono kierowcy"}), 404
        db.execute("DELETE FROM drivers WHERE id=?", (did,))
        db.commit()
        _log_audit("driver_delete", session["username"], f"did={did}")
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/transport/drivers-list")
@login_required
def api_transport_drivers_list():
    db = get_db()
    try:
        rows = db.execute(
            "SELECT d.id, d.name, d.phone, d.truck_plate, ff.name as forwarder_name "
            "FROM drivers d LEFT JOIN freight_forwarders ff ON d.forwarder_id=ff.id "
            "WHERE d.active=1 ORDER BY d.name"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


# SMS notification
@app.route("/api/transport/notify-driver", methods=["POST"])
@login_required
@require_role("manager")
@csrf_protect
def api_transport_notify_driver():
    """Send SMS notification with confirmation link to driver."""
    data = request.get_json(silent=True) or {}
    _did_raw = data.get("driver_id")
    try:
        driver_id = int(_did_raw) if _did_raw not in (None, "", False) else None
    except (TypeError, ValueError):
        driver_id = None
    container_number = str(data.get("container_number") or "").strip()[:50]
    po_numbers = str(data.get("po_numbers") or "").strip()[:200]
    load_location = str(data.get("load_location") or "").strip()[:200]
    scheduled_time = str(data.get("scheduled_time") or "").strip()[:30]
    gate_number = str(data.get("gate_number") or "").strip()[:20]
    _qid_raw = data.get("transport_queue_id")
    try:
        queue_id = int(_qid_raw) if _qid_raw not in (None, "", False) else None
    except (TypeError, ValueError):
        queue_id = None

    if not driver_id:
        return jsonify({"error": "Wybierz kierowcę"}), 400
    if not load_location:
        return jsonify({"error": "Podaj miejsce załadunku"}), 400

    base_url = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
    if not base_url or not base_url.lower().startswith(("http://", "https://")):
        return jsonify({
            "error": "Brak konfiguracji APP_BASE_URL — link potwierdzenia w SMS byłby nieprawidłowy. "
                     "Ustaw zmienną środowiskową APP_BASE_URL."
        }), 503

    db = get_db()
    try:
        driver = db.execute(
            "SELECT * FROM drivers WHERE id=? AND active=1", (driver_id,)
        ).fetchone()
        if not driver:
            return jsonify({"error": "Nie znaleziono kierowcy"}), 404
        driver = dict(driver)

        token = secrets.token_urlsafe(24)

        cur = db.execute(
            "INSERT INTO driver_notifications"
            "(driver_id, transport_queue_id, container_number, po_numbers, "
            " load_location, pickup_location, gate_number, scheduled_time, token, status, sms_sent_at, created_by) "
            "VALUES(?,?,?,?,?,?,?,?,?,'sent',datetime('now'),?)",
            (driver_id, queue_id, container_number, po_numbers,
             load_location, (str(data.get("pickup_location") or "").strip() or load_location)[:200],
             gate_number, scheduled_time, token, session["user_id"])
        )
        notif_id = cur.lastrowid
        db.commit()
    finally:
        db.close()

    # Build SMS message
    confirm_url = f"{base_url}/transport/confirm/{token}"
    lines = ["DocCompare — zlecenie transportowe"]
    if container_number:
        lines.append(f"Kontener: {container_number}")
    if po_numbers:
        lines.append(f"PO: {po_numbers}")
    lines.append(f"Miejsce zaladunku: {load_location}")
    if gate_number:
        lines.append(f"BRAMA nr: {gate_number}")
    if scheduled_time:
        lines.append(f"Godzina: {scheduled_time}")
    lines.append(f"Potwierdz: {confirm_url}")
    sms_text = "\n".join(lines)

    # Send SMS
    sms_result = _send_sms(driver["phone"], sms_text)

    db2 = get_db()
    try:
        if sms_result.get("ok"):
            _log_audit("driver_sms_sent", session["username"],
                       f"driver={driver['name']} phone={driver['phone']} container={container_number}")
        else:
            db2.execute(
                "UPDATE driver_notifications SET status='expired' WHERE id=?",
                (notif_id,)
            )
        db2.commit()
    finally:
        db2.close()

    return jsonify({
        "ok": sms_result.get("ok", False),
        "notification_id": notif_id,
        "confirm_url": confirm_url,
        "sms_sent": sms_result.get("ok", False),
        "sms_error": sms_result.get("error"),
        "driver_name": driver["name"],
        "driver_phone": driver["phone"],
    })


@app.route("/transport/confirm/<token>")
def transport_driver_confirm(token):
    """Public page — driver confirms or declines via this link (no login required)."""
    db = get_db()
    try:
        notif = db.execute(
            "SELECT dn.*, d.name as driver_name, d.phone "
            "FROM driver_notifications dn "
            "LEFT JOIN drivers d ON dn.driver_id=d.id "
            "WHERE dn.token=?", (token,)
        ).fetchone()
        if not notif:
            return render_template("driver_confirm.html", error="Nieprawidlowy lub wygasly link.", notif=None)
        notif = dict(notif)
        if notif["status"] in ("confirmed","declined","expired"):
            return render_template("driver_confirm.html", notif=notif, already_done=True)
        return render_template("driver_confirm.html", notif=notif, already_done=False)
    finally:
        db.close()


@app.route("/api/transport/confirm/<token>", methods=["POST"])
def api_transport_driver_confirm(token):
    """Driver submits their confirmation — no login required."""
    data = request.get_json(silent=True) or {}
    verdict = data.get("verdict")  # "confirmed" or "declined"
    note = str(data.get("note") or "")[:300]
    if verdict not in ("confirmed", "declined"):
        return jsonify({"error": "Nieprawidlowy status"}), 400
    db = get_db()
    try:
        notif = db.execute(
            "SELECT * FROM driver_notifications WHERE token=?", (token,)
        ).fetchone()
        if not notif:
            return jsonify({"error": "Nie znaleziono"}), 404
        if notif["status"] in ("confirmed","declined","expired"):
            return jsonify({"error": "Juz odpowiedziales", "status": notif["status"]}), 409
        # Atomic update: only changes row if still sent (prevents TOCTOU race)
        rows_updated = db.execute(
            "UPDATE driver_notifications SET status=?, confirmed_at=datetime('now'), confirmation_note=? "
            "WHERE token=? AND status='sent'",
            (verdict, note, token)
        ).rowcount
        if rows_updated == 0:
            db.rollback()
            return jsonify({"error": "Juz odpowiedziales", "status": "confirmed"}), 409
        db.commit()
        # If confirmed → advance matching shipment to w_transporcie
        if verdict == "confirmed":
            try:
                po_nums = [p.strip() for p in (notif["po_numbers"] or "").split(",") if p.strip()]
                for _po in po_nums:
                    db.execute(
                        "UPDATE shipments SET status='w_transporcie', updated_at=datetime('now') "
                        "WHERE po_number=? AND status IN ('nowe','potwierdzono')",
                        (_po,)
                    )
                db.commit()
            except Exception as _e:
                logger.warning("Shipment status update failed after driver confirmation: %s", _e)
    finally:
        db.close()
    return jsonify({"ok": True, "status": verdict})


@app.route("/api/transport/sms-config", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_transport_sms_config():
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        _sms_max_lens = {"provider": 50, "api_key": 500, "sender": 50,
                         "twilio_sid": 100, "twilio_from": 50}
        _VALID_SMS_PROVIDERS = {"twilio", "smsapi", "textbelt", "infobip", "nexmo", "vonage", "test", ""}
        for k in ("provider", "api_key", "sender", "twilio_sid", "twilio_from"):
            if k in data:
                val = str(data[k] or "")[:_sms_max_lens.get(k, 500)]
                if k == "provider" and val.lower() not in _VALID_SMS_PROVIDERS:
                    continue
                db.execute(
                    "INSERT INTO settings(category, key, value) VALUES('sms',?,?) "
                    "ON CONFLICT(category,key) DO UPDATE SET value=excluded.value",
                    (k, val)
                )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


def _send_sms(phone: str, text: str) -> dict:
    """Send SMS using configured provider. Returns {'ok': bool, 'error': str|None}."""
    import urllib.request as _ur
    import urllib.parse as _up
    import json as _json

    db = get_db()
    try:
        rows = db.execute(
            "SELECT key, value FROM settings WHERE category='sms'"
        ).fetchall()
        cfg = {r["key"]: r["value"] for r in rows}
    finally:
        db.close()

    provider = cfg.get("provider", "")
    api_key  = cfg.get("api_key", "")
    sender   = cfg.get("sender", "DocCompare")

    if not provider or not api_key:
        logger.warning("SMS not configured — provider=%s", provider)
        return {"ok": False, "error": "SMS nie skonfigurowane. Dodaj klucz API w ustawieniach spedycji."}

    def _read_json_resp(resp):
        """Read a urllib response body and parse JSON, guarding against non-JSON
        gateway errors (e.g. HTML 502). Returns (data_or_None, raw_text)."""
        raw_bytes = resp.read()
        try:
            raw_text = raw_bytes.decode("utf-8", "replace")
        except Exception:
            raw_text = str(raw_bytes)
        try:
            return _json.loads(raw_text), raw_text
        except (ValueError, TypeError):
            return None, raw_text

    try:
        if provider == "textbelt":
            payload = _up.urlencode({
                "phone": phone, "message": text, "key": api_key
            }).encode()
            req = _ur.Request("https://textbelt.com/text",
                              data=payload,
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
            # bandit: URL to stała https:// w kodzie
            with _ur.urlopen(req, timeout=10) as resp:  # nosec B310
                result, _raw = _read_json_resp(resp)
            if result is None:
                return {"ok": False, "error": f"Nieprawidłowa odpowiedź bramki SMS: {_raw[:200]}"}
            if result.get("success"):
                return {"ok": True}
            return {"ok": False, "error": result.get("error", "Unknown error")}

        elif provider == "smsapi":
            params = _up.urlencode({
                "access_token": api_key, "to": phone,
                "message": text, "from": sender, "format": "json"
            })
            req = _ur.Request(f"https://api.smsapi.pl/sms.do?{params}")
            # bandit: URL to stała https:// w kodzie
            with _ur.urlopen(req, timeout=10) as resp:  # nosec B310
                result, _raw = _read_json_resp(resp)
            if result is None:
                return {"ok": False, "error": f"Nieprawidłowa odpowiedź bramki SMS: {_raw[:200]}"}
            if result.get("count", 0) > 0:
                return {"ok": True}
            return {"ok": False, "error": str(result.get("message", "Blad SMSAPI"))}

        elif provider == "twilio":
            account_sid = cfg.get("twilio_sid", "")
            auth_token  = api_key
            from_number = cfg.get("twilio_from", "")
            if not account_sid or not from_number:
                return {"ok": False, "error": "Brak konfiguracji Twilio (SID, From)"}
            import base64 as _b64
            payload = _up.urlencode({
                "To": phone, "From": from_number, "Body": text
            }).encode()
            url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
            creds = _b64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
            req = _ur.Request(url, data=payload,
                              headers={"Authorization": f"Basic {creds}",
                                       "Content-Type": "application/x-www-form-urlencoded"})
            # bandit: URL to stała https:// w kodzie
            with _ur.urlopen(req, timeout=10) as resp:  # nosec B310
                result, _raw = _read_json_resp(resp)
            if result is None:
                return {"ok": False, "error": f"Nieprawidłowa odpowiedź Twilio: {_raw[:200]}"}
            if result.get("sid"):
                return {"ok": True}
            return {"ok": False, "error": result.get("message", "Blad Twilio")}

        else:
            return {"ok": False, "error": f"Nieznany provider SMS: {provider}"}

    except Exception as e:
        logger.error("_send_sms error: %s", e)
        return {"ok": False, "error": "Błąd wysyłania SMS — sprawdź konfigurację"}


# ─────────────────────────────────────────────────────────────────────────────
# DOSTAWY — Unified delivery lifecycle panel
# Oparty na tabelach kolejka_zlecenia + kolejka_kontenery
# ─────────────────────────────────────────────────────────────────────────────

# _DELIVERY_DOC_SLOTS i _COMP_TYPE_TO_SLOTS przeniesione do delivery_workflow.py
# (importowane na górze pliku jako jedyne źródło prawdy).


def _build_doc_slots(po_number: str, db) -> list:
    """Build enriched document slot data for a given PO: uploads + comparison results."""
    # Uploaded files
    rows = db.execute(
        "SELECT id, filename, original_name, doc_type, file_size, uploaded_at "
        "FROM shipment_documents WHERE po_number=? ORDER BY uploaded_at ASC",
        (po_number,),
    ).fetchall()
    files_by_type: dict[str, list] = {}
    _shipment_folder = _ensure_shipment_folder(po_number)
    for r in rows:
        dt = (r["doc_type"] or "OTHER").upper()
        fd = dict(r)
        # Czy plik faktycznie jest na dysku? (rekord w bazie może zostać, a plik
        # zniknąć — np. usunięty albo redeploy bez trwałego wolumenu).
        fd["exists"] = bool(_shipment_folder) and os.path.isfile(
            os.path.join(_shipment_folder, r["filename"]))
        files_by_type.setdefault(dt, []).append(fd)

    # Comparison results
    comps = db.execute(
        "SELECT id, doc_type, status, diff_count, approval_status, created_at "
        "FROM comparisons "
        "WHERE po_number=? AND (is_deleted IS NULL OR is_deleted=0) "
        "ORDER BY created_at DESC",
        (po_number,),
    ).fetchall()

    # Group comparisons by affected slots
    comps_by_slot: dict[str, list] = {}
    for c in comps:
        dt = (c["doc_type"] or "").upper()
        pair = _COMP_TYPE_TO_SLOTS.get(dt)
        if pair:
            for slot in pair:
                if slot:
                    comps_by_slot.setdefault(slot, []).append(dict(c))
        else:
            comps_by_slot.setdefault(dt, []).append(dict(c))

    def _led(slot_type: str) -> str:
        """Return LED status: pending / uploaded / ok / warn / error / missing."""
        # Rekord(y) są, ale żaden plik nie istnieje na dysku → stan 'missing'.
        _f = files_by_type.get(slot_type)
        if _f and not any(x.get("exists") for x in _f):
            return "missing"
        slot_comps = comps_by_slot.get(slot_type, [])
        if slot_comps:
            worst = "ok"
            for c in slot_comps:
                s = c.get("status") or ""
                if s in ("critical", "error"):
                    return "error"
                if s == "warning" and worst == "ok":
                    worst = "warn"
            return worst
        if files_by_type.get(slot_type):
            return "uploaded"
        return "pending"

    # Pairs that need a comparison connector: (type_a, type_b) — from delivery_workflow
    _PAIRS = _DELIVERY_PAIRS

    def _conn_status(type_a: str, type_b: str) -> str:
        """Return connector status between a paired slot."""
        # Use comps from either slot (they share the same comparison results)
        pair_comps = comps_by_slot.get(type_a, []) + comps_by_slot.get(type_b, [])
        if pair_comps:
            for c in pair_comps:
                s = c.get("status") or ""
                if s in ("critical", "error"):
                    return "err"
            for c in pair_comps:
                if c.get("status") == "warning":
                    return "warn"
            return "ok"
        # Both have files but no comparison yet
        if files_by_type.get(type_a) and files_by_type.get(type_b):
            return "pend"
        return "none"

    slots = []
    for s in _DELIVERY_DOC_SLOTS:
        t = s["type"]
        # Determine if this slot is part of a compare pair (and which side)
        pair_target = None
        conn_status = None
        for (a, b) in _PAIRS:
            if t == a:
                pair_target = b
                conn_status = _conn_status(a, b)
                break
            if t == b:
                pair_target = a
                conn_status = _conn_status(a, b)
                break
        # Non-adjacent compare partner (np. SAD↔CI) — pokazywane jako badge, nie strzałka
        nonadj = _DELIVERY_NONADJ.get(t)
        nonadj_status = _conn_status(t, nonadj) if nonadj else None
        _slot_files = files_by_type.get(t, [])
        slots.append({
            **s,
            "files":         _slot_files,
            "missing_file":  bool(_slot_files) and not any(f.get("exists") for f in _slot_files),
            "comps":         comps_by_slot.get(t, []),
            "led":           _led(t),
            "pair_target":   pair_target,   # type string of the paired slot, or None
            "conn_status":   conn_status,    # 'ok'/'warn'/'err'/'pend'/'none'/None
            "nonadj_compare": nonadj,        # np. 'CI' dla slotu SAD, albo None
            "nonadj_status":  nonadj_status, # status porównania dla badge
        })
    return slots


# Stałe statusów/faz dostaw (_DELIVERY_PHASES, _STATUS_LABELS, _NEXT_STATUS,
# _NEXT_LABELS, _APPROVAL_STEPS) oraz _delivery_phase_index przeniesione do
# delivery_workflow.py i importowane na górze pliku (jedyne źródło prawdy).


def _present_doc_types(db, po_number: str) -> set:
    """Zbiór typów dokumentów (uppercase) wgranych dla danej dostawy."""
    rows = db.execute(
        "SELECT DISTINCT doc_type FROM shipment_documents WHERE po_number=?",
        (po_number,),
    ).fetchall()
    return {(r["doc_type"] or "").upper() for r in rows}


def _maybe_auto_advance_on_upload(po_number: str, doc_type: str, uid):
    """Po wgraniu dokumentu przesuń status do wczesnej fazy — tylko kroki BEZ
    zatwierdzenia (logika w delivery_workflow.auto_advance_target). Zwraca
    (new_status, label) albo (None, None)."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT id, status FROM kolejka_zlecenia WHERE nr_zamowienia=?",
            (po_number,),
        ).fetchone()
        if not row:
            return None, None
        current = row["status"] or "utworzone"
        target = _delivery_auto_advance_target(doc_type, current)
        if not target:
            return None, None
        db.execute(
            "UPDATE kolejka_zlecenia SET status=?, updated_at=datetime('now') WHERE nr_zamowienia=?",
            (target, po_number),
        )
        db.execute(
            "INSERT INTO kolejka_status_log (zlecenie_id, pole, stara_wartosc, nowa_wartosc, changed_by) "
            "VALUES (?, 'status', ?, ?, ?)",
            (row["id"], current, target, uid),
        )
        db.commit()
        return target, _DELIVERY_STATUS_LABELS.get(target, target)
    finally:
        db.close()


def _ensure_transport_queue_for_delivery(db, po_number: str, uid) -> None:
    """Auto-zaciągnięcie dostawy do kolejki transportowej na kroku ETD–SAP.
    Kolejka NIE jest uzupełniana ręcznie — wpis powstaje automatycznie wg statusu.
    Idempotentne: gdy wpis dla tego PO już istnieje, nic nie robi. Używa
    przekazanego połączenia (bez zagnieżdżania writerów)."""
    try:
        exists = db.execute(
            "SELECT 1 FROM transport_queue WHERE po_numbers LIKE ? LIMIT 1",
            (f"%{po_number}%",)
        ).fetchone()
        if exists:
            return
        zr = db.execute(
            "SELECT kontener_id, planowane_etd, data_dostawy FROM kolejka_zlecenia "
            "WHERE nr_zamowienia=?", (po_number,)
        ).fetchone()
        container, etd, eta = "", "", ""
        if zr:
            etd = zr["planowane_etd"] or ""
            eta = zr["data_dostawy"] or ""
            if zr["kontener_id"]:
                kr = db.execute(
                    "SELECT numer_kontenera, etd_plan, eta FROM kolejka_kontenery WHERE id=?",
                    (zr["kontener_id"],)
                ).fetchone()
                if kr:
                    container = kr["numer_kontenera"] or ""
                    etd = etd or (kr["etd_plan"] or "")
                    eta = eta or (kr["eta"] or "")
        db.execute(
            "INSERT INTO transport_queue (container_number, po_numbers, etd, eta, "
            "customs_status, urgency, notes, created_by) "
            "VALUES (?,?,?,?, 'oczekuje', 'normalne', 'Auto: ETD–SAP', ?)",
            (container, po_number, etd, eta, uid)
        )
        logger.info("auto transport_queue: dodano PO=%s na kroku ETD–SAP", po_number)
    except Exception as _e:
        logger.debug("auto transport_queue po=%s: %s", po_number, _e)


def _ensure_warehouse_receipt_for_delivery(db, po_number: str, uid) -> None:
    """Auto-wpis przyjęcia magazynowego, gdy dostawa wchodzi w status magazynowy.
    Idempotentne: gdy wpis dla tego PO już istnieje, nic nie robi. Operator
    uzupełnia w /warehouse: kto rozładował/sprawdził, czas vs norma."""
    try:
        # Idempotencja: dopasowanie po DOKŁADNYM tokenie PO (po_numbers bywa listą
        # rozdzieloną spacją/przecinkiem) — LIKE tylko zawęża kandydatów, a podciąg/
        # wildcard potwierdzamy rozbiciem na tokeny, by uniknąć fałszywych trafień.
        _esc = po_number.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        already = False
        for _r in db.execute(
            "SELECT po_numbers FROM warehouse_receipts WHERE po_numbers LIKE ? ESCAPE '\\'",
            (f"%{_esc}%",)
        ).fetchall():
            if po_number in re.split(r"[\s,;]+", (_r["po_numbers"] or "").strip()):
                already = True
                break
        if already:
            return
        zr = db.execute(
            "SELECT kontener_id, supplier_name FROM kolejka_zlecenia WHERE nr_zamowienia=?",
            (po_number,)
        ).fetchone()
        container = ""
        if zr and zr["kontener_id"]:
            kr = db.execute(
                "SELECT numer_kontenera FROM kolejka_kontenery WHERE id=?", (zr["kontener_id"],)
            ).fetchone()
            if kr:
                container = kr["numer_kontenera"] or ""
        db.execute(
            "INSERT INTO warehouse_receipts (container_number, po_numbers, status, notes, created_by) "
            "VALUES (?,?, 'przybyle', 'Auto: dostawa w statusie magazynowym', ?)",
            (container, po_number, uid)
        )
        logger.info("auto warehouse_receipt: PO=%s (status magazynowy)", po_number)
    except Exception as _e:
        logger.debug("auto warehouse_receipt po=%s: %s", po_number, _e)


def _ensure_delivery_checklist(db, po_number: str, supplier_name: str, uid) -> None:
    """Auto-tworzy domyślną checklistę dokumentów dla nowej dostawy (jeśli brak),
    żeby każda dostawa z /dostawy pojawiła się od razu w /checklists."""
    try:
        exists = db.execute(
            "SELECT 1 FROM delivery_checklists WHERE po_number=? LIMIT 1", (po_number,)
        ).fetchone()
        if exists:
            return
        for doc in ("PO", "PI", "CI", "PL", "SAD", "BL"):
            db.execute(
                "INSERT INTO delivery_checklists(po_number, supplier_name, container_number, "
                "doc_type, status, created_by) VALUES(?,?,?,?, 'brak', ?)",
                (po_number, (supplier_name or "")[:200], "", doc, uid)
            )
    except Exception as _e:
        logger.debug("auto delivery_checklist po=%s: %s", po_number, _e)


def _set_dostawa_status(po_number: str, new_status: str, uid, db=None) -> bool:
    """Ustawia status dostawy na new_status i zapisuje wpis w logu. Zwraca True
    gdy zmieniono. Może użyć przekazanego połączenia db (inaczej otwiera własne)."""
    own = db is None
    if own:
        db = get_db()
    try:
        row = db.execute(
            "SELECT id, status FROM kolejka_zlecenia WHERE nr_zamowienia=?", (po_number,)
        ).fetchone()
        if not row:
            return False
        current = row["status"] or "utworzone"
        if current == new_status:
            return False
        db.execute(
            "UPDATE kolejka_zlecenia SET status=?, updated_at=datetime('now') WHERE nr_zamowienia=?",
            (new_status, po_number),
        )
        db.execute(
            "INSERT INTO kolejka_status_log (zlecenie_id, pole, stara_wartosc, nowa_wartosc, changed_by) "
            "VALUES (?, 'status', ?, ?, ?)",
            (row["id"], current, new_status, uid),
        )
        # Krok ETD–SAP: dostawa auto-trafia do kolejki transportowej (wg statusu).
        if new_status in ("etd_sap", "zaokretowane"):
            _ensure_transport_queue_for_delivery(db, po_number, uid)
        # Status magazynowy: auto-wpis przyjęcia w /warehouse.
        if new_status in ("dostawa_magazyn", "w_magazynie", "dostarczone"):
            _ensure_warehouse_receipt_for_delivery(db, po_number, uid)
        if own:
            db.commit()
        return True
    finally:
        if own:
            db.close()


def _maybe_set_proforma_loaded(po_number: str, uid):
    """Po wgraniu PI ustaw status „Proforma załadowana — czeka na porównanie",
    jeśli dostawa jest jeszcze we wczesnej fazie. Zwraca (status, label)|(None,None)."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT status FROM kolejka_zlecenia WHERE nr_zamowienia=?", (po_number,)
        ).fetchone()
        if not row:
            return None, None
        target = _delivery_proforma_loaded_target(row["status"] or "utworzone")
        if not target:
            return None, None
    finally:
        db.close()
    if _set_dostawa_status(po_number, target, uid):
        return target, _DELIVERY_STATUS_LABELS.get(target, target)
    return None, None


def _maybe_set_artwork_awaiting(po_number: str, uid):
    """Po wgraniu artworku fabrycznego (ARTWORK_B) ustaw status
    „Oczekiwanie na artwork", jeśli dostawa jest najpóźniej w fazie Artwork."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT status FROM kolejka_zlecenia WHERE nr_zamowienia=?", (po_number,)
        ).fetchone()
        if not row:
            return None, None
        target = _delivery_artwork_awaiting_target(row["status"] or "utworzone")
        if not target:
            return None, None
    finally:
        db.close()
    if _set_dostawa_status(po_number, target, uid):
        return target, _DELIVERY_STATUS_LABELS.get(target, target)
    return None, None


@app.route("/dostawy")
@login_required
def dostawy_list():
    db = get_db()
    try:
        q = request.args.get("q", "").strip()
        status_filter = request.args.get("status", "").strip()
        phase_filter = request.args.get("phase", "").strip()

        sql = """
            SELECT z.id, z.nr_zamowienia, z.supplier_name, z.supplier_code,
                   z.status, z.priorytet, z.artwork_status,
                   z.planowane_etd, z.data_dostawy, z.dostawa_18, z.uwagi,
                   z.created_at, z.updated_at,
                   z.destination_type, z.transit_country,
                   k.numer_kontenera, k.eta, k.etd_plan,
                   u.username AS kupiec,
                   -- Autor OSTATNIej zmiany statusu, jeśli była ręczna (inaczej NULL).
                   (SELECT CASE WHEN sl.source='manual' THEN su.username END
                      FROM kolejka_status_log sl
                      LEFT JOIN users su ON su.id = sl.changed_by
                      WHERE sl.zlecenie_id = z.id AND sl.pole='status'
                      ORDER BY sl.id DESC LIMIT 1) AS status_manual_by
            FROM kolejka_zlecenia z
            LEFT JOIN kolejka_kontenery k ON z.kontener_id = k.id
            LEFT JOIN users u ON z.created_by = u.id
        """
        params = []
        where = []
        if q:
            _esc_q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{_esc_q}%"
            where.append("(z.nr_zamowienia LIKE ? ESCAPE '\\' OR z.supplier_name LIKE ? ESCAPE '\\' "
                         "OR z.supplier_code LIKE ? ESCAPE '\\' OR k.numer_kontenera LIKE ? ESCAPE '\\' "
                         "OR z.dostawa_18 LIKE ? ESCAPE '\\' OR u.username LIKE ? ESCAPE '\\')")
            params += [like, like, like, like, like, like]
        if status_filter:
            where.append("z.status = ?")
            params.append(status_filter)
        if phase_filter and phase_filter.isdigit():
            pi = int(phase_filter)
            if 0 <= pi < len(_DELIVERY_PHASES):
                ph_statuses = _DELIVERY_PHASES[pi]["statuses"]
                where.append("z.status IN ({})".format(",".join("?" * len(ph_statuses))))
                params += ph_statuses
        # Podział per dział: user/manager widzą tylko swój dział (+ nieprzypisane);
        # superuser/admin (can_see_all) — wszystko.
        if not can_see_all(session.get("role", "")):
            where.append("(COALESCE(z.department,'')='' OR z.department=?)")
            params.append(session.get("department") or "")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY z.updated_at DESC LIMIT 300"

        rows = db.execute(sql, params).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["_phase"] = _delivery_phase_index(d.get("status") or "")
            d["_step"] = _delivery_step_index(d.get("status") or "")
            d["_status_label"] = _DELIVERY_STATUS_LABELS.get(d.get("status") or "", "")
            d["_status_desc"] = _DELIVERY_STATUS_DESCRIPTIONS.get(d.get("status") or "", "")
            # Proste SLA: ile dni dostawa tkwi w bieżącym statusie (wg updated_at).
            _days = None
            _since = d.get("updated_at")
            if _since:
                try:
                    from datetime import datetime as _d2
                    _days = (_d2.utcnow() - _d2.strptime(str(_since)[:19], "%Y-%m-%d %H:%M:%S")).days
                except Exception:
                    _days = None
            _ov, _lim = _delivery_sla_overdue(d.get("status") or "", _days)
            d["_sla_overdue"] = _ov
            d["_sla_days"] = _days
            d["_sla_limit"] = _lim
            items.append(d)

        all_st = db.execute("SELECT status FROM kolejka_zlecenia").fetchall()
        stats = {"total": len(all_st), "active": 0, "by_phase": [0] * len(_DELIVERY_PHASES)}
        for r in all_st:
            st = r[0] or "utworzone"
            if st != "rozliczone":
                stats["active"] += 1
            stats["by_phase"][_delivery_phase_index(st)] += 1

        suppliers = []
        try:
            rows2 = db.execute(
                "SELECT code, name FROM suppliers WHERE active=1 ORDER BY name LIMIT 200"
            ).fetchall()
            suppliers = [dict(r) for r in rows2]
        except Exception:
            app.logger.warning("dostawy_list: failed to load suppliers")
        try:
            transit_countries = _list_transit_countries(db)
        except Exception:
            transit_countries = []
    finally:
        db.close()

    return render_template(
        "dostawy.html",
        items=items, stats=stats,
        phases=_DELIVERY_PHASES, steps=_DELIVERY_STEPS,
        status_labels=_DELIVERY_STATUS_LABELS,
        status_descriptions=_DELIVERY_STATUS_DESCRIPTIONS,
        suppliers=suppliers, transit_countries=transit_countries,
        q=q, status_filter=status_filter, phase_filter=phase_filter,
        username=session.get("username"), role=session.get("role"),
    )


@app.route("/dostawy/analityka")
@login_required
def dostawy_analityka():
    """Panel analityczny dostaw przychodzących — osobna ścieżka w nadsekcji Dostawy."""
    return render_template("dostawy_analityka.html",
                           username=session.get("username"), role=session.get("role"))


def _days_between(a, b):
    """Różnica w dniach (float) między dwiema datami (TEXT lub datetime). None gdy brak."""
    from datetime import datetime as _dt

    def _p(s):
        if isinstance(s, _dt):
            return s
        s = str(s or "").strip().replace("T", " ")
        if not s:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return _dt.strptime(s[:19] if len(s) >= 19 else s, fmt)
            except ValueError:
                continue
        return None
    da, db_ = _p(a), _p(b)
    if not da or not db_:
        return None
    return (db_ - da).total_seconds() / 86400.0


@app.route("/api/dostawy/analytics")
@login_required
def api_dostawy_analytics():
    """Panel analityczny: rozkład statusów, średni lead time (utworzenie→magazyn)
    i zamówiony asortyment z ilościami. Honoruje podział per dział."""
    db = get_db()
    try:
        dept_clause, dept_params = "", []
        if not can_see_all(session.get("role", "")):
            dept_clause = " AND (COALESCE(z.department,'')='' OR z.department=?)"
            dept_params = [session.get("department") or ""]

        # 1) Rozkład statusów → fazy.
        rows = db.execute(
            # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
            "SELECT z.status AS s, COUNT(*) AS c FROM kolejka_zlecenia z "  # nosec B608
            "WHERE 1=1" + dept_clause + " GROUP BY z.status", dept_params).fetchall()
        by_status = {r["s"]: r["c"] for r in rows}
        total = sum(by_status.values())
        phases = []
        for ph in _DELIVERY_PHASES:
            phases.append({
                "label": ph["label"],
                "count": sum(by_status.get(s, 0) for s in ph["statuses"]),
            })

        # 2) Lead time: utworzenie → pierwsze wejście w status magazynowy.
        wh = ("dostawa_magazyn", "w_magazynie", "dostarczone")
        lt_rows = db.execute(
            # bandit: lista placeholderów ? budowana z len(); wartości parametryzowane
            "SELECT z.created_at AS c0, MIN(l.changed_at) AS arr "  # nosec B608
            "FROM kolejka_zlecenia z JOIN kolejka_status_log l "
            "ON l.zlecenie_id=z.id AND l.pole='status' AND l.nowa_wartosc IN "
            "(" + ",".join("?" * len(wh)) + ") "
            "WHERE 1=1" + dept_clause + " GROUP BY z.id, z.created_at",
            list(wh) + dept_params).fetchall()
        days = []
        for r in lt_rows:
            d = _days_between(r["c0"], r["arr"])
            if d is not None and d >= 0:
                days.append(d)
        lead = None
        if days:
            days.sort()
            lead = {
                "count": len(days),
                "avg": round(sum(days) / len(days), 1),
                "median": round(days[len(days) // 2], 1),
                "min": round(days[0], 1),
                "max": round(days[-1], 1),
            }

        # 3) Zamówiony asortyment + model „na ile wystarczy".
        # Statusy „odebrane" = faza magazyn i dalsze (reszta = towar w drodze).
        _mag_idx = next((i for i, ph in enumerate(_DELIVERY_PHASES)
                         if "magaz" in ph["label"].lower()), len(_DELIVERY_PHASES))
        received = set()
        for i, ph in enumerate(_DELIVERY_PHASES):
            if i >= _mag_idx:
                received.update(ph["statuses"])

        raw = db.execute(
            # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
            "SELECT p.ref_code AS ref, p.opis AS opis, p.ilosc AS qty, p.jednostka AS jm, "  # nosec B608
            "p.wartosc AS val, p.waluta AS cur, p.po_number AS po, z.status AS st "
            "FROM po_line_items p JOIN kolejka_zlecenia z ON z.nr_zamowienia=p.po_number "
            "WHERE p.ref_code<>''" + dept_clause, dept_params).fetchall()
        import warehouse_stock as _ws
        stock_map = _ws.get_stock_map(db)
        has_stock = bool(stock_map)
        import material_master as _mm
        agg = {}
        for r in raw:
            ref = r["ref"]
            a = agg.get(ref)
            if a is None:
                a = agg[ref] = {"ref": ref, "opis": r["opis"], "qty": 0.0,
                                "incoming": 0.0, "jm": r["jm"], "value": 0.0,
                                "currency": r["cur"], "_pos": set()}
            q = r["qty"] or 0
            a["qty"] += q
            if (r["st"] or "") not in received:
                a["incoming"] += q
            a["value"] += (r["val"] or 0)
            a["_pos"].add(r["po"])
            if not a["opis"] and r["opis"]:
                a["opis"] = r["opis"]
        assortment = []
        for a in agg.values():
            stk = stock_map.get(_mm.normalize_ref(a["ref"]))
            on_hand = stk["on_hand"] if stk else None
            usage = stk["monthly_usage"] if stk else None
            out_dlv = stk["out_deliveries"] if stk else None
            out_conf = stk["out_confirmed"] if stk else None
            out_unconf = stk["out_unconfirmed"] if stk else None
            # Saldo dyspozycyjne = stan + w drodze (PO) − wych. w dostawach − wych. potwierdzone.
            balance = None
            if stk:
                balance = round((on_hand or 0) + a["incoming"]
                                - (out_dlv or 0) - (out_conf or 0), 2)
            days = None
            if usage and usage > 0:
                base_for_days = balance if balance is not None else ((on_hand or 0) + a["incoming"])
                days = round(base_for_days / (usage / 30.4), 1)
            assortment.append({
                "ref": a["ref"], "opis": a["opis"],
                "qty": round(a["qty"], 2), "incoming": round(a["incoming"], 2),
                "jm": a["jm"], "deliveries": len(a["_pos"]),
                "value": round(a["value"], 2), "currency": a["currency"],
                "on_hand": on_hand, "monthly_usage": usage,
                "out_deliveries": out_dlv, "out_confirmed": out_conf,
                "out_unconfirmed": out_unconf, "balance": balance,
                "days_cover": days,
            })
        # Sortuj: najpilniejsze (najmniej dni zapasu) na górze, brak danych na koniec.
        assortment.sort(key=lambda x: (x["days_cover"] is None,
                                       x["days_cover"] if x["days_cover"] is not None else 0,
                                       -x["qty"]))
        assortment = assortment[:80]

        return jsonify({
            "total": total,
            "by_status": by_status,
            "phases": phases,
            "lead_time": lead,
            "assortment": assortment,
            "warehouse_stock_available": has_stock,
        })
    finally:
        db.close()


_ART_AUTOLOAD_LEVELS = ["", "sztuka", "op", "opz", "karton"]
# Statusy WEJŚCIA w fazę artwork — tu (i tylko tu) auto-ładowanie próbuje raz
# podpiąć master. Po podpięciu/braku status zmienia się na jeden z _ART_MASTER_LOADED
# / artwork_brak_mastera, więc ciężki lookup nie powtarza się przy każdym otwarciu.
# 'artwork_brak_mastera' też jest tu, by zamówienie SAMO się naprawiło, gdy plik
# mastera pojawi się w bibliotece (po naprawie rozpoznawania pliku).
_ART_AUTOLOAD_ENTRY = ("artwork_oczekiwanie", "zatwierdzone_do_wyplyniecia", "artwork_brak_mastera")
# Statusy, w których master już wisi — sprawdzamy jedynie dostępność nowszej wersji.
_ART_MASTER_LOADED = ("artwork_master_zaladowany", "artwork_czeka_fabryczny")


def _resolve_artwork_master_path(db, rel_path: str, filename: str) -> str:
    """Zamień potwierdzony master (z indeksu artworków) na realną ścieżkę pliku NA
    SERWERZE. Pliki indeksu trzyma biblioteka (`library_files.file_path`, zsynchr.
    przez agenta) — NIE storage profili. Kolejność:
      1. library_files po rel_path (dokładnie),
      2. library_files po nazwie pliku (gdy ścieżka źródłowa się różni),
      3. fallback: _resolve_master_path (legacy storage profili).
    Zwraca istniejącą ścieżkę albo '' gdy pliku nie ma na serwerze (sama metadana)."""
    rel_path = (rel_path or "").strip()
    filename = (filename or "").strip()
    # Nowa składnica: bajty potwierdzonych/grupowych masterów wgrane przez agenta
    # (te same pliki zasilają podgląd ORAZ porównanie / auto-podpięcie do slotu).
    try:
        if rel_path:
            import artwork_index as _ai
            _mf = _ai.master_file_for(db, rel_path)
            if _mf:
                _p = os.path.join(app.config["UPLOAD_FOLDER"], "artwork_masters",
                                  _mf["stored_name"])
                if os.path.exists(_p):
                    return _p
                # Cache na dysku zniknął (redeploy) → odtwórz z BAZY (źródło prawdy).
                _blob = _ai.master_file_blob(db, rel_path)
                if _blob:
                    try:
                        os.makedirs(os.path.dirname(_p), exist_ok=True)
                        with open(_p, "wb") as _fp:
                            _fp.write(_blob[1])
                        return _p
                    except OSError:
                        pass
    except Exception as _e:
        logger.debug("master store resolve failed rel=%s: %s", rel_path, _e)
    try:
        if rel_path:
            r = db.execute(
                "SELECT file_path FROM library_files WHERE rel_path=?", (rel_path,)).fetchone()
            if r and (r["file_path"] or "").strip() and os.path.exists(r["file_path"]):
                return r["file_path"]
        if filename:
            r = db.execute(
                "SELECT file_path FROM library_files WHERE filename=? AND file_path<>'' "
                "ORDER BY modified_at DESC LIMIT 1", (filename,)).fetchone()
            if r and (r["file_path"] or "").strip() and os.path.exists(r["file_path"]):
                return r["file_path"]
    except Exception as _e:
        logger.debug("library master resolve failed rel=%s: %s", rel_path, _e)
    # Legacy: niektóre mastery mogą leżeć w storage profili.
    p = _resolve_master_path(rel_path or filename)
    if p and os.path.exists(p):
        return p
    return ""


def _confirmed_masters_for_po(db, nr: str) -> list:
    """Potwierdzone mastery dla wszystkich REF-ów zamówienia, po wszystkich poziomach.
    Zwraca [{"filename":.., "rel_path":..}] zdeduplikowane po nazwie pliku. Tylko
    odczyt — nie zamyka przekazanego połączenia."""
    try:
        import artwork_index as _ai
    except Exception:
        return []
    try:
        _ai.ensure_table(db)
    except Exception:
        pass
    refs_raw = [r[0] for r in db.execute(
        "SELECT DISTINCT ref_code FROM po_line_items WHERE po_number=? AND ref_code<>''",
        (nr,)).fetchall()]
    out, seen = [], set()
    for _rr in refs_raw:
        try:
            ref = _clean_po_ref(db, _rr)[0]
        except Exception:
            ref = _rr
        if not ref:
            continue
        for lv in _ART_AUTOLOAD_LEVELS:
            try:
                st = _ai.lookup_for_comparison(db, ref, lv)
            except Exception:
                continue
            conf = (st or {}).get("confirmed") or {}
            if not conf:
                # Fallback: master z grupy mapowań (REF bez własnego wzorca).
                conf = (st or {}).get("group_master") or {}
            if not conf:
                continue
            fname = (conf.get("filename") or "").strip() or os.path.basename(conf.get("rel_path") or "")
            if not fname or fname in seen:
                continue
            seen.add(fname)
            out.append({"filename": fname, "rel_path": conf.get("rel_path") or fname})
    return out


def _autoload_artwork_masters(nr: str, uid: int, attach_only: bool = False) -> dict:
    """Podepnij POTWIERDZONY master artworku dla REF-ów zamówienia jako dokument
    ARTWORK i przesuń status na 'artwork_master_zaladowany'. Następnie czekamy na
    artwork fabryczny; po wgraniu manager uruchamia normalną weryfikację.

    Zachowanie (wg ustaleń z operatorem):
      • WYZWALANIE: tylko raz, na WEJŚCIU w fazę artwork (statusy w
        ``_ART_AUTOLOAD_ENTRY``). Po podpięciu status→artwork_master_zaladowany,
        po braku→artwork_brak_mastera — żaden nie jest statusem wejściowym, więc
        ciężki lookup nie powtarza się przy każdym otwarciu szczegółów.
      • BRAK MASTERA: gdy są REF-y, ale nie ma potwierdzonego wzorca → ustaw
        status 'artwork_brak_mastera' i zaloguj alert (jednorazowo).
      • NOWSZA WERSJA: gdy master już wisi (auto), a w bazie jest potwierdzony
        master o innej nazwie → tylko sygnalizujemy (``newer``=True), NIE podmieniamy.

    Idempotentne: nie rusza ręcznie wgranych ARTWORK-ów. Każdy helper-zapis
    (`_save_doc_to_shipment`, `_set_dostawa_status`) otwiera własne połączenie, więc
    odczyt domykamy przed jakimkolwiek zapisem (brak zagnieżdżonych writerów SQLite).
    Zwraca {"attached":int, "outcome":str, "newer":bool}.
    """
    result = {"attached": 0, "outcome": "skip", "newer": False}
    candidates = []      # (filename, resolved_server_path)
    has_refs = False
    db = get_db()
    try:
        zr = db.execute("SELECT status FROM kolejka_zlecenia WHERE nr_zamowienia=?", (nr,)).fetchone()
        if not zr:
            return result
        status = (zr[0] or "")
        already = db.execute(
            "SELECT COUNT(*) FROM shipment_documents WHERE po_number=? AND doc_type='ARTWORK'",
            (nr,)).fetchone()[0]
        if already:
            result["outcome"] = "present"
            # Master już podpięty — sprawdź TYLKO czy pojawiła się nowsza POTWIERDZONA
            # wersja niż auto-podpięta (powiadom, nie podmieniaj).
            if status in _ART_MASTER_LOADED:
                attached_names = {(_r[0] or "") for _r in db.execute(
                    "SELECT original_name FROM shipment_documents "
                    "WHERE po_number=? AND doc_type='ARTWORK' AND source='auto'",
                    (nr,)).fetchall()}
                if attached_names:
                    conf_names = {c["filename"] for c in _confirmed_masters_for_po(db, nr)}
                    if conf_names and not conf_names.issubset(attached_names):
                        result["newer"] = True
            return result
        # Brak ARTWORK-a — auto-ładuj na wejściu w fazę artwork, ALBO od razu
        # przy wgraniu PO (attach_only) — wtedy bez zmiany statusu głównego.
        if status not in _ART_AUTOLOAD_ENTRY and not attach_only:
            return result
        has_refs = bool(db.execute(
            "SELECT 1 FROM po_line_items WHERE po_number=? AND ref_code<>'' LIMIT 1",
            (nr,)).fetchone())
        _confs = _confirmed_masters_for_po(db, nr)
        confirmed_found = bool(_confs)
        for c in _confs:
            path = _resolve_artwork_master_path(db, c["rel_path"], c["filename"])
            if path:
                candidates.append((c["filename"], path))
    finally:
        db.close()

    if candidates:
        attached = 0
        for fname, path in candidates:
            try:
                if _save_doc_to_shipment(nr, path, fname, "ARTWORK", uid, source="auto"):
                    attached += 1
            except Exception as _e:
                logger.warning("autoload master attach failed po=%s file=%s: %s", nr, fname, _e)
        if attached:
            try:
                if not attach_only:
                    _set_dostawa_status(nr, "artwork_master_zaladowany", uid)
                _log_audit("dostawa_autoload_master", session.get("username"),
                           f"po={nr} podpięto {attached} master(ów)"
                           + ("" if attach_only else " → artwork_master_zaladowany"))
            except Exception as _e:
                logger.warning("autoload master status set failed po=%s: %s", nr, _e)
        result.update(attached=attached, outcome="attached")
        return result

    # Master JEST potwierdzony w indeksie, ale jego pliku nie ma na dysku serwera
    # (biblioteka zsynchronizowała metadane bez pliku, albo plik tylko na dysku
    # źródłowym). To NIE jest „brak mastera" — nie alarmujemy fałszywie; zostawiamy
    # oczekiwanie. Plik dograj/zsynchronizuj do biblioteki, by podpiął się sam.
    if confirmed_found:
        logger.info("autoload po=%s: master potwierdzony, ale brak pliku na serwerze "
                    "— pomijam auto-podpięcie (bez alarmu 'brak mastera')", nr)
        result["outcome"] = "confirmed_no_file"
        # Gdyby zamówienie tkwiło w błędnym 'brak mastera' (poprzednia wersja) —
        # to NIE jest brak mastera (jest potwierdzony), więc wróć do oczekiwania.
        if status == "artwork_brak_mastera" and not attach_only:
            try:
                _set_dostawa_status(nr, "artwork_oczekiwanie", uid)
            except Exception as _e:
                logger.warning("autoload clear false brak-mastera failed po=%s: %s", nr, _e)
        return result

    # Są REF-y, ale w indeksie NIE MA potwierdzonego mastera → oznacz i zaalarmuj.
    # Tylko gdy zmiana statusu (bez re-flapowania, gdy już jest 'brak mastera').
    if has_refs and status != "artwork_brak_mastera" and not attach_only:
        try:
            _set_dostawa_status(nr, "artwork_brak_mastera", uid)
            _log_audit("dostawa_brak_mastera", session.get("username"),
                       f"po={nr} brak potwierdzonego mastera artworku dla REF-ów → artwork_brak_mastera")
        except Exception as _e:
            logger.warning("autoload brak-mastera status set failed po=%s: %s", nr, _e)
        result["outcome"] = "missing"
    elif has_refs:
        result["outcome"] = "missing"
    return result


@app.route("/dostawy/<path:nr>")
@login_required
def dostawa_detail(nr):
    # Auto-pull a confirmed master into the comparison slot + advance status.
    # Never let this break the page. Wynik niesie flagę o nowszej potwierdzonej
    # wersji mastera (baner „dostępna nowsza wersja").
    artwork_newer_master = False
    artwork_master_no_file = False
    try:
        _al = _autoload_artwork_masters(nr, session["user_id"]) or {}
        artwork_newer_master = bool(_al.get("newer"))
        # Master potwierdzony, ale plik nie jest zsynchronizowany do biblioteki
        # (tryb „tylko miniatura" — sam z_path) → nie da się podpiąć automatycznie.
        artwork_master_no_file = (_al.get("outcome") == "confirmed_no_file")
    except Exception as _e:
        logger.warning("autoload masters skipped po=%s: %s", nr, _e)
    db = get_db()
    try:
        row = db.execute(
            """SELECT z.*,
                      k.numer_kontenera, k.eta, k.etd_plan, k.etd_real,
                      k.origin_port, k.dest_port, k.sealine, k.fo_number,
                      k.customs_status AS kontener_customs_status,
                      k.notes AS kontener_notes
               FROM kolejka_zlecenia z
               LEFT JOIN kolejka_kontenery k ON z.kontener_id = k.id
               WHERE z.nr_zamowienia = ?""",
            (nr,),
        ).fetchone()
        if not row:
            return render_template("404.html",
                                   username=session.get("username"),
                                   role=session.get("role")), 404
        item = dict(row)

        doc_slots = _build_doc_slots(nr, db)

        # Ostatnie usunięcia plików tej dostawy (kto i kiedy) — z audytu.
        deleted_docs = []
        try:
            _drows = db.execute(
                "SELECT username, detail, created_at FROM audit_log "
                "WHERE event='dostawa_doc_delete' AND detail LIKE ? "
                "ORDER BY created_at DESC LIMIT 8",
                (f"po={nr} %",),
            ).fetchall()
            deleted_docs = [dict(r) for r in _drows]
        except Exception:
            pass

        # Raporty weryfikacji PO/PI i Artwork (osobna „druga linia" dokumentów)
        report_docs = [dict(r) for r in db.execute(
            "SELECT id, original_name, uploaded_at, doc_type FROM shipment_documents "
            "WHERE po_number=? AND doc_type IN ('RAPORT_PO_PI','RAPORT_ARTWORK') "
            "ORDER BY uploaded_at DESC", (nr,)).fetchall()]

        comps = db.execute(
            """SELECT id, doc_type, status, diff_count, created_at, approval_status,
                      file_a, file_b
               FROM comparisons
               WHERE po_number = ? AND (is_deleted IS NULL OR is_deleted = 0)
               ORDER BY created_at DESC LIMIT 30""",
            (nr,),
        ).fetchall()
        comparisons = [dict(c) for c in comps]

        log = db.execute(
            """SELECT sl.pole, sl.stara_wartosc, sl.nowa_wartosc,
                      sl.changed_at, u.username AS changed_by_name
               FROM kolejka_status_log sl
               LEFT JOIN users u ON sl.changed_by = u.id
               WHERE sl.zlecenie_id = ?
               ORDER BY sl.changed_at DESC LIMIT 50""",
            (item.get("id"),),
        ).fetchall()
        status_log = [dict(l) for l in log]

        transport_order = None
        try:
            to_row = db.execute(
                """SELECT zt.id, zt.numer, zt.status, zt.uwagi, zt.spedytor_name,
                          zt.forwarder_email, zt.agent_name, zt.agent_email, zt.agent_phone,
                          zt.agent_set_at, zt.email_sent_at
                   FROM zlecenia_transport_items zti
                   JOIN zlecenia_transportowe zt ON zt.id = zti.transport_id
                   WHERE zti.po_number = ?
                   ORDER BY zt.created_at DESC LIMIT 1""",
                (nr,)
            ).fetchone()
            if to_row:
                transport_order = dict(to_row)
        except Exception:
            pass

        # E-mail dostawcy (odbiorcy zamówienia) z master danych — po kodzie dostawcy.
        supplier_email = ""
        supplier_contact = ""
        _scode = (item.get("supplier_code") or "").strip()
        if _scode:
            try:
                _srow = db.execute(
                    "SELECT email, contact_person FROM suppliers WHERE code=?", (_scode,)
                ).fetchone()
                if _srow:
                    supplier_email = (_srow["email"] or "").strip()
                    supplier_contact = (_srow["contact_person"] or "").strip()
            except Exception:
                pass
    finally:
        db.close()

    cur_status = item.get("status") or ""
    phase_index = _delivery_phase_index(cur_status)
    cur_step_index = _delivery_step_index(cur_status)
    next_status = _DELIVERY_NEXT_STATUS.get(cur_status)
    next_label = _DELIVERY_NEXT_LABELS.get(cur_status, "")
    needs_approval = next_status in _DELIVERY_APPROVAL_STEPS

    # Kompletność dokumentów + miękka bramka dla następnego kroku
    present_types = [s["type"] for s in doc_slots if s.get("files")]
    _slot_label = {s["type"]: s["label"] for s in doc_slots}
    missing_slots = _delivery_missing_slots(present_types)
    missing_slot_labels = [_slot_label.get(t, t) for t in missing_slots]
    missing_for_next = _delivery_missing_required_docs(next_status, present_types) if next_status else []
    missing_for_next_labels = [_slot_label.get(t, t) for t in missing_for_next]

    # Cofanie statusu (manager/admin)
    prev_status = _delivery_prev_status(cur_status)
    prev_label = _DELIVERY_STATUS_LABELS.get(prev_status, "") if prev_status else ""
    can_revert = bool(prev_status) and session.get("role") in ("manager", "admin", "superuser")

    _is_mgr = session.get("role") in ("manager", "admin", "superuser")
    # Weryfikacja PO/PI — możliwa, gdy oba pliki są wgrane (rola manager+)
    can_verify = ("PO" in present_types and "PI" in present_types and _is_mgr)
    verified = cur_status in _DELIVERY_VERIFY_STATUSES
    # Weryfikacja artworku — gdy wzorzec i fabryczny są wgrane
    can_verify_artwork = ("ARTWORK" in present_types and "ARTWORK_B" in present_types and _is_mgr)
    verified_artwork = cur_status in _DELIVERY_ARTWORK_STATUSES

    return render_template(
        "dostawa_detail.html",
        item=item, comparisons=comparisons, status_log=status_log,
        doc_slots=doc_slots, report_docs=report_docs, deleted_docs=deleted_docs,
        phases=_DELIVERY_PHASES, phase_index=phase_index,
        steps=_DELIVERY_STEPS, cur_step_index=cur_step_index,
        next_status=next_status, next_label=next_label,
        needs_approval=needs_approval,
        status_labels=_DELIVERY_STATUS_LABELS,
        status_descriptions=_DELIVERY_STATUS_DESCRIPTIONS,
        missing_slots=missing_slots, missing_slot_labels=missing_slot_labels,
        missing_for_next=missing_for_next, missing_for_next_labels=missing_for_next_labels,
        prev_status=prev_status, prev_label=prev_label, can_revert=can_revert,
        can_verify=can_verify, verified=verified,
        can_verify_artwork=can_verify_artwork, verified_artwork=verified_artwork,
        artwork_newer_master=artwork_newer_master,
        artwork_master_no_file=artwork_master_no_file,
        transport_order=transport_order,
        supplier_email=supplier_email, supplier_contact=supplier_contact,
        username=session.get("username"), role=session.get("role"),
    )


@app.route("/api/dostawy/<path:nr>/documents", methods=["GET"])
@login_required
def api_dostawa_docs_list(nr):
    db = get_db()
    try:
        row = db.execute(
            "SELECT id FROM kolejka_zlecenia WHERE nr_zamowienia=?", (nr,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        docs = db.execute(
            "SELECT id, filename, original_name, doc_type, file_size, uploaded_at "
            "FROM shipment_documents WHERE po_number=? ORDER BY uploaded_at DESC",
            (nr,),
        ).fetchall()
        folder = _ensure_shipment_folder(nr)
        result = []
        for d in docs:
            path = os.path.join(folder, d["filename"])
            result.append({**dict(d), "exists": os.path.isfile(path)})
    finally:
        db.close()
    return jsonify({"documents": result})


def _po_numbers_in_text(text: str) -> set:
    """Numery zamówienia (PO) z tekstu dokumentu — przy słowach kluczowych oraz
    wzorzec ACME (45000xxxxx). Do walidacji 'dokument vs numer dostawy'."""
    import re as _re
    nums = set()
    for m in _re.finditer(
        r'(?:purchase\s*order|order\s*n[or]\.?|order\s*number|po\s*number|'
        r'numer\s*zam\w*|nr\s*zam\w*)[:\s#\-]*([0-9]{8,12})', text, _re.IGNORECASE):
        nums.add(m.group(1))
    for m in _re.finditer(r'\b(45\d{8})\b', text):
        nums.add(m.group(1))
    return nums


@app.route("/api/dostawy/<path:nr>/documents", methods=["POST"])
@login_required
@csrf_protect
def api_dostawa_docs_upload(nr):
    db = get_db()
    try:
        row = db.execute(
            "SELECT id FROM kolejka_zlecenia WHERE nr_zamowienia=?", (nr,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
    finally:
        db.close()

    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Brak pliku"}), 400
    doc_type = re.sub(r'[^\w]', '', (request.form.get("doc_type") or "OTHER").strip().upper())[:20] or "OTHER"

    # Accept PDFs and images
    ext = (f.filename or "").rsplit(".", 1)[-1].lower()
    if ext in ("pdf",):
        err = _validate_pdf_upload(f)
        if err:
            return jsonify({"error": err}), 400
        f.seek(0)
    elif ext in ("png", "jpg", "jpeg", "gif", "tiff", "bmp"):
        header = f.read(8)
        f.seek(0)
        _ok = (
            (ext == "png" and header[:4] == b'\x89PNG') or
            (ext in ("jpg", "jpeg") and header[:3] == b'\xff\xd8\xff') or
            (ext == "gif" and header[:6] in (b'GIF87a', b'GIF89a')) or
            (ext == "tiff" and header[:4] in (b'II\x2a\x00', b'MM\x00\x2a')) or
            (ext == "bmp" and header[:2] == b'BM')
        )
        if not _ok:
            return jsonify({"error": "Nieprawidłowy nagłówek pliku graficznego"}), 400
    else:
        return jsonify({"error": "Dozwolone formaty: PDF, PNG, JPG, TIFF"}), 400

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    _safe_fn = secure_filename(f.filename) or f"upload.{ext}"
    tmp_path = os.path.join(
        app.config["UPLOAD_FOLDER"],
        f"ddoc_{session['user_id']}_{_safe_fn}",
    )
    f.seek(0)
    f.save(tmp_path)
    if ext == "pdf":
        _decrypt_pdf_inplace(tmp_path)

    # Walidacja numeru: dokument nie może odnosić się do INNEJ dostawy (Q4/Q5 —
    # nr dostawy nadrzędny). Blokuj tylko gdy w pliku jest wyraźny numer PO różny
    # od numeru dostawy; brak numeru / błąd ekstrakcji = przepuść.
    if ext == "pdf" and doc_type in ("PO", "PI", "CI", "PL", "SAD"):
        try:
            _found = _po_numbers_in_text(extract_pdf_text(tmp_path) or "")
        except Exception as _e:
            _found = set()
            logger.debug("doc-number validation skipped po=%s: %s", nr, _e)
        if _found and nr not in _found:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return jsonify({"error":
                f"Numer w dokumencie ({', '.join(sorted(_found))}) nie pasuje do "
                f"dostawy {nr}. Wgraj właściwy plik albo utwórz dostawę o numerze z dokumentu."}), 409

    try:
        ok = _save_doc_to_shipment(
            nr, tmp_path, f.filename, doc_type, session["user_id"], source="manual"
        )
        if ok and ext == "pdf":
            if doc_type == "PO":
                # Full extraction: supplier, items, dates, author
                _try_enrich_po_from_pdf(nr, tmp_path)
                # Gałąź ARTWORK startuje od kroku 1: podepnij potwierdzony master
                # od razu po wgraniu PO — BEZ zmiany statusu głównego (attach_only).
                try:
                    _autoload_artwork_masters(nr, session["user_id"], attach_only=True)
                except Exception as _e:
                    logger.debug("auto-master (attach_only) po=%s: %s", nr, _e)
            elif doc_type in ("CI", "PL"):
                # Auto-porównanie CI↔PL (gdy oba są) — CI po lewej (A), PL po prawej (B)
                _try_auto_compare_pair(nr, "CI", "PL", "CI_PL", session["user_id"])
            # PI: NIE auto-porównujemy — status idzie na „czeka na porównanie",
            # a właściwe porównanie PO↔PI odpala przycisk „Weryfikuj PO/PI".
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    if not ok:
        return jsonify({"error": "Nie udało się zapisać pliku"}), 500
    _log_audit("dostawa_doc_upload", session["username"], f"po={nr} type={doc_type} file={f.filename}")
    resp = {"ok": True}
    try:
        new_status, new_label = None, None
        if doc_type == "PI":
            # PI wgrane: gdy PO już jest — automatyczne porównanie PO↔PI w tle
            # (status ustawi wynik). W przeciwnym razie „Proforma załadowana — czeka".
            if _try_auto_compare_po_pi(nr, session["user_id"], session["username"]):
                resp["po_pi_verifying"] = True
            else:
                new_status, new_label = _maybe_set_proforma_loaded(nr, session["user_id"])
        elif doc_type == "ARTWORK_B":
            # Artwork fabryczny wgrany. Jeśli wzorzec (ARTWORK) jest już podpięty —
            # odpal porównanie automatycznie (w tle); status ustawi wynik weryfikacji.
            # W przeciwnym razie status „Oczekiwanie na artwork".
            if _try_auto_verify_artwork(nr, session["user_id"], session["username"]):
                resp["artwork_verifying"] = True
            else:
                new_status, new_label = _maybe_set_artwork_awaiting(nr, session["user_id"])
        else:
            new_status, new_label = _maybe_auto_advance_on_upload(nr, doc_type, session["user_id"])
        if new_status:
            resp["auto_advanced"] = new_status
            resp["auto_label"] = new_label
            _log_audit("dostawa_auto_advance", session["username"],
                       f"po={nr}→{new_status} (upload {doc_type})")
    except Exception as e:
        logger.debug("status update after upload failed po=%s: %s", nr, e)
    return jsonify(resp)


@app.route("/api/dostawy/<path:nr>/status", methods=["GET"])
@login_required
def api_dostawa_status(nr):
    """Lekki status zamówienia — do odpytywania z UI (np. czekanie na wynik
    auto-weryfikacji artworku, która biegnie w tle)."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT status FROM kolejka_zlecenia WHERE nr_zamowienia=?", (nr,)).fetchone()
    finally:
        db.close()
    if not row:
        return jsonify({"error": "Nie znaleziono"}), 404
    st = row["status"] or ""
    return jsonify({"status": st, "label": _DELIVERY_STATUS_LABELS.get(st, st)})


@app.route("/dostawy/<path:nr>/raport/<int:cid>")
@login_required
def dostawa_report_view(nr, cid):
    """Widok raportu porównania (PO↔PI / CI↔PL) w HTML — do osadzenia w zakładce
    „Raporty" szczegółów dostawy oraz do otwarcia w przeglądarce. PDF nadal dostępny
    pod /api/export/pdf/<cid>. Dostęp: każdy zalogowany (dokumenty dostawy jawne dla
    userów); raport musi należeć do tej dostawy (cid + po_number)."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT id, doc_type, status, diff_count, result_json, file_a, file_b, created_at "
            "FROM comparisons WHERE id=? AND po_number=? AND (is_deleted IS NULL OR is_deleted=0)",
            (cid, nr)).fetchone()
    finally:
        db.close()
    if not row:
        return render_template("404.html", username=session.get("username"),
                               role=session.get("role")), 404
    try:
        result = json.loads(row["result_json"] or "{}")
    except Exception:
        result = {}
    return render_template("comparison_report.html",
                           nr=nr, cid=cid, comp=dict(row), result=result,
                           username=session.get("username"), role=session.get("role"))


@app.route("/api/dostawy/<path:nr>/documents/<int:doc_id>/view", methods=["GET"])
@login_required
def api_dostawa_doc_view(nr, doc_id):
    db = get_db()
    try:
        row = db.execute(
            "SELECT id FROM kolejka_zlecenia WHERE nr_zamowienia=?", (nr,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        doc = db.execute(
            "SELECT filename, original_name FROM shipment_documents "
            "WHERE id=? AND po_number=?",
            (doc_id, nr),
        ).fetchone()
    finally:
        db.close()
    if not doc:
        return jsonify({"error": "Dokument nie znaleziony"}), 404
    folder = _ensure_shipment_folder(nr)
    file_path = os.path.join(folder, doc["filename"])
    if not os.path.isfile(file_path) or os.path.islink(file_path):
        return jsonify({"error": "Plik nie istnieje na dysku"}), 404
    mime = "application/pdf"
    ext = doc["filename"].rsplit(".", 1)[-1].lower() if "." in doc["filename"] else ""
    if ext in ("png", "jpg", "jpeg"):
        mime = f"image/{ext if ext != 'jpg' else 'jpeg'}"
    elif ext in ("tiff", "bmp", "gif"):
        mime = f"image/{ext}"
    from flask import send_file as _sf
    # BUGFIX: usuń znaki kontrolne (CR/LF) z nazwy pliku — inaczej trafiają do
    # nagłówka Content-Disposition (CRLF / header injection).
    _dl_name = (doc["original_name"] or doc["filename"] or "dokument").replace("\r", "").replace("\n", "").strip() or "dokument"
    resp = _sf(file_path, mimetype=mime,
               download_name=_dl_name,
               as_attachment=False)
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resp


@app.route("/api/dostawy/<path:nr>/documents/<int:doc_id>", methods=["DELETE"])
@login_required
@require_role("manager")
@csrf_protect
def api_dostawa_doc_delete(nr, doc_id):
    db = get_db()
    try:
        row = db.execute(
            "SELECT id FROM kolejka_zlecenia WHERE nr_zamowienia=?", (nr,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        doc = db.execute(
            "SELECT filename FROM shipment_documents WHERE id=? AND po_number=?",
            (doc_id, nr),
        ).fetchone()
        if not doc:
            return jsonify({"error": "Dokument nie znaleziony"}), 404
        # Remove file from disk
        folder = _ensure_shipment_folder(nr)
        path = os.path.join(folder, doc["filename"])
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
        db.execute("DELETE FROM shipment_documents WHERE id=? AND po_number=?", (doc_id, nr))
        db.commit()
    finally:
        db.close()
    _log_audit("dostawa_doc_delete", session["username"], f"po={nr} doc_id={doc_id}")
    return jsonify({"ok": True})


@app.route("/api/dostawy", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_dostawa_create():
    data = request.get_json(silent=True) or {}
    nr = str(data.get("nr_zamowienia") or "").strip()
    if not nr:
        return jsonify({"error": "Numer zamówienia jest wymagany"}), 400
    if not re.match(r"^[45]\d{8,10}$", nr):
        return jsonify({"error": "Numer SAP musi zaczynać się od 4 lub 5 i mieć 9–11 cyfr"}), 400

    uid = session["user_id"]
    db = get_db()
    try:
        existing = db.execute(
            "SELECT id FROM kolejka_zlecenia WHERE nr_zamowienia=?", (nr,)
        ).fetchone()
        if existing:
            return jsonify({"error": f"Zamówienie {nr} już istnieje w systemie"}), 409

        _priorytet = (data.get("priorytet") or "normalny")
        if _priorytet not in ("normalny", "pilny", "spokojnie"):
            _priorytet = "normalny"
        # Proces/przeznaczenie: magazyn (Radom) | tranzyt (+kraj) | inne.
        _dest = (data.get("destination_type") or "magazyn")
        if _dest not in ("magazyn", "tranzyt", "inne"):
            _dest = "magazyn"
        _country = str(data.get("transit_country") or "").strip()[:120] if _dest == "tranzyt" else ""
        _dept = session.get("department") or ""
        cur = db.execute(
            """INSERT INTO kolejka_zlecenia
               (nr_zamowienia, supplier_name, supplier_code, status, priorytet,
                uwagi, created_by, destination_type, transit_country, department)
               VALUES (?, ?, ?, 'utworzone', ?, ?, ?, ?, ?, ?)""",
            (nr,
             str(data.get("supplier_name") or "").strip()[:200],
             str(data.get("supplier_code") or "").strip()[:50],
             _priorytet,
             str(data.get("uwagi") or "").strip()[:1000],
             uid, _dest, _country, _dept),
        )
        zlecenie_id = cur.lastrowid
        db.execute(
            """INSERT INTO kolejka_status_log
               (zlecenie_id, pole, stara_wartosc, nowa_wartosc, krok, changed_by)
               VALUES (?, 'status', '', 'utworzone', 1, ?)""",
            (zlecenie_id, uid),
        )
        # Auto-checklista dokumentów — dostawa od razu widoczna w /checklists.
        _ensure_delivery_checklist(db, nr, str(data.get("supplier_name") or ""), uid)
        db.commit()
        _log_audit("dostawa_create", session["username"], f"po={nr}")
        return jsonify({"ok": True, "nr": nr})
    except IntegrityError:
        return jsonify({"error": f"Zamówienie {nr} już istnieje w systemie"}), 409
    finally:
        db.close()


@app.route("/api/dostawy/<path:nr>", methods=["PATCH"])
@login_required
@require_role("manager")
@csrf_protect
def api_dostawa_update(nr):
    data = request.get_json(silent=True) or {}
    allowed = [
        "supplier_name", "supplier_code", "priorytet", "uwagi",
        "planowane_etd", "data_dostawy", "rodzaj_transportu",
        "proforma_lot", "proforma_data_produkcji",
        "proforma_data_wysylki", "proforma_data_waznosci",
        "artwork_status", "z_folder_link",
    ]
    _dostawa_max_lens = {
        "supplier_name": 200, "supplier_code": 50, "priorytet": 20, "uwagi": 1000,
        "planowane_etd": 20, "data_dostawy": 20, "rodzaj_transportu": 50,
        "proforma_lot": 100, "proforma_data_produkcji": 20,
        "proforma_data_wysylki": 20, "proforma_data_waznosci": 20,
        "artwork_status": 50, "z_folder_link": 500,
    }
    _int_fields = set()
    _VALID_PRIORITIES = {"normalny", "pilny", "spokojnie"}
    _VALID_ARTWORK_STATUSES = {"nie_dotyczy", "oczekuje", "w_trakcie", "zatwierdzone", "odrzucone"}
    fields, vals = [], []
    for k in allowed:
        if k in data:
            v = data[k]
            if k == "priorytet":
                v = str(v or "").strip()
                if v not in _VALID_PRIORITIES:
                    continue
            elif k == "artwork_status":
                v = str(v or "").strip()
                if v not in _VALID_ARTWORK_STATUSES:
                    continue
            elif k in _int_fields:
                try:
                    v = max(0, int(v))
                except (TypeError, ValueError):
                    continue
            elif k in _dostawa_max_lens:
                v = str(v or "").strip()[:_dostawa_max_lens[k]]
            fields.append(f"{k}=?")
            vals.append(v)
    if not fields:
        return jsonify({"error": "Brak pól do zapisu"}), 400
    fields.append("updated_at=datetime('now')")
    vals.append(nr)
    db = get_db()
    try:
        cur = db.execute(
            # bandit: nazwy kolumn z białej listy w kodzie (nie z kluczy żądania); wartości przez ?
            f"UPDATE kolejka_zlecenia SET {', '.join(fields)} WHERE nr_zamowienia=?",  # nosec B608
            vals,
        )
        db.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "Nie znaleziono zamówienia"}), 404
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/dostawy/<path:nr>/supplier-email", methods=["GET"])
@login_required
def api_dostawa_supplier_email(nr):
    """Zwraca odbiorcę zamówienia (dostawca + e-mail z master danych) dla potwierdzenia
    przed wysyłką. Pozwala UI pokazać DO KOGO leci mail, zanim go wyśle."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT supplier_name, supplier_code FROM kolejka_zlecenia WHERE nr_zamowienia=?",
            (nr,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono dostawy"}), 404
        code = (row["supplier_code"] or "").strip()
        email = contact = name = ""
        name = (row["supplier_name"] or "").strip()
        if code:
            srow = db.execute(
                "SELECT name, email, contact_person FROM suppliers WHERE code=?", (code,)
            ).fetchone()
            if srow:
                email = (srow["email"] or "").strip()
                contact = (srow["contact_person"] or "").strip()
                name = name or (srow["name"] or "").strip()
    finally:
        db.close()
    return jsonify({"supplier_name": name, "supplier_code": code,
                    "email": email, "contact_person": contact,
                    "has_email": bool(email)})


@app.route("/api/dostawy/<path:nr>/send-to-supplier", methods=["POST"])
@login_required
@require_role("manager")
@csrf_protect
def api_dostawa_send_to_supplier(nr):
    """Wysyła wiadomość do dostawcy (odbiorcy zamówienia) na e-mail z master danych.
    Odbiorca jest jawnie potwierdzany w UI. Po wysyłce ustawia status 'wyslane_do_dostawcy'."""
    uid = session["user_id"]
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        row = db.execute(
            "SELECT supplier_name, supplier_code FROM kolejka_zlecenia WHERE nr_zamowienia=?",
            (nr,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono dostawy"}), 404
        code = (row["supplier_code"] or "").strip()
        if not code:
            return jsonify({"error": "Brak przypisanego dostawcy — przypisz dostawcę na karcie dostawy."}), 400
        srow = db.execute(
            "SELECT name, email FROM suppliers WHERE code=?", (code,)).fetchone()
        email = (srow["email"].strip() if srow and srow["email"] else "")
        sup_name = (srow["name"] if srow else "") or (row["supplier_name"] or "")
        if not email:
            return jsonify({"error": f"Dostawca '{sup_name or code}' nie ma e-maila. "
                                     f"Uzupełnij e-mail w Master danych dostawców."}), 400
    finally:
        db.close()

    subject = str(data.get("subject") or f"Zamówienie {nr} — ACME").strip()[:200]
    body = str(data.get("body") or "").strip()
    if not body:
        body = (f"Dzień dobry,\n\nW załączeniu / poniżej przesyłamy zamówienie nr {nr}.\n"
                f"Prosimy o potwierdzenie przyjęcia oraz proformy.\n\n"
                f"Pozdrawiamy,\nACME — Dział Zakupów")
    try:
        _send_email(email, subject, body)
    except Exception as exc:
        logger.warning("send-to-supplier failed for %s → %s: %s", nr, email, exc)
        return jsonify({"error": f"Nie udało się wysłać e-maila: {str(exc)[:200]}"}), 502

    try:
        _set_dostawa_status(nr, "wyslane_do_dostawcy", uid)
    except Exception:
        pass
    _log_audit("dostawa_send_to_supplier", session.get("username"),
               f"{nr} → {sup_name} <{email}>")
    return jsonify({"ok": True, "sent_to": email, "supplier_name": sup_name})


@app.route("/api/dostawy/<path:nr>/advance", methods=["POST"])
@login_required
@require_role("manager")
@csrf_protect
def api_dostawa_advance(nr):
    uid = session["user_id"]
    role = session.get("role", "user")
    data = request.get_json(silent=True) or {}

    db = get_db()
    try:
        row = db.execute(
            "SELECT id, status FROM kolejka_zlecenia WHERE nr_zamowienia=?", (nr,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono zamówienia"}), 404

        current = row["status"] or "utworzone"
        raw_status = row["status"]          # surowa wartość (może być NULL) do CAS
        zlecenie_id = row["id"]

        # Twarda bramka wyniku weryfikacji: z „niezgodne/błędne" nie da się przejść
        # dalej liniowo — trzeba poprawić dokumenty i ponowić weryfikację, albo
        # świadomie nadpisać status („Zmień status" z uzasadnieniem).
        if current in _DELIVERY_BLOCKED_ADVANCE:
            return jsonify({
                "error": ("Wynik weryfikacji to „"
                          + _DELIVERY_STATUS_LABELS.get(current, current)
                          + "”. Popraw dokumenty i ponów weryfikację albo użyj "
                          + "„Zmień status” z uzasadnieniem, aby przejść dalej."),
                "blocked": True,
            }), 409

        next_st = _DELIVERY_NEXT_STATUS.get(current)
        if not next_st:
            return jsonify({"error": "Dostawa jest już zakończona"}), 400

        if next_st in _DELIVERY_APPROVAL_STEPS and role not in ("manager", "admin", "superuser"):
            return jsonify({"error": "Ten krok wymaga zatwierdzenia przez managera"}), 403

        # TWARDA bramka celna (KOLEJKA-03): wysłanie dokumentów do odprawy wymaga
        # zatwierdzonej checklisty celnej — bezwarunkowo, confirm=true NIE przepuszcza.
        if next_st == "dokumenty_wyslane_odprawa":
            _cl_rows = db.execute(
                "SELECT doc_type, status FROM delivery_checklists WHERE po_number=?",
                (nr,),
            ).fetchall()
            _cl_missing = _customs.missing_hard_required(_cl_rows)
            if _cl_missing:
                return jsonify({
                    "error": "Checklista celna niekompletna: " + ", ".join(_cl_missing),
                    "blocked": True,
                    "missing": _cl_missing,
                }), 409

        # Miękka bramka dokumentowa: jeśli brak wymaganych dokumentów, poproś
        # o potwierdzenie (nie blokujemy na twardo — confirm=true przepuszcza).
        if not bool(data.get("confirm")):
            missing = _delivery_missing_required_docs(next_st, _present_doc_types(db, nr))
            if missing:
                return jsonify({
                    "needs_confirm": True,
                    "missing": missing,
                    "message": "Brak wymaganych dokumentów: " + ", ".join(missing)
                               + ". Kontynuować mimo to?",
                })

        note = str(data.get("note") or "").strip()[:500]

        # Compare-and-swap: zapisz tylko jeśli status NIE zmienił się od odczytu
        # (chroni przed wyścigiem dwóch managerów / auto-advance vs ręczny advance).
        if raw_status is None:
            _upd = db.execute(
                "UPDATE kolejka_zlecenia SET status=?, updated_at=datetime('now') "
                "WHERE nr_zamowienia=? AND status IS NULL",
                (next_st, nr),
            )
        else:
            _upd = db.execute(
                "UPDATE kolejka_zlecenia SET status=?, updated_at=datetime('now') "
                "WHERE nr_zamowienia=? AND status=?",
                (next_st, nr, raw_status),
            )
        if _upd.rowcount == 0:
            db.rollback()
            _fresh = db.execute(
                "SELECT status FROM kolejka_zlecenia WHERE nr_zamowienia=?", (nr,)
            ).fetchone()
            _fresh_st = (_fresh["status"] if _fresh else "") or ""
            return jsonify({
                "error": "Status zmienił się w międzyczasie — odśwież stronę.",
                "current_status": _fresh_st,
                "label": _DELIVERY_STATUS_LABELS.get(_fresh_st, _fresh_st),
            }), 409
        db.execute(
            """INSERT INTO kolejka_status_log
               (zlecenie_id, pole, stara_wartosc, nowa_wartosc, changed_by)
               VALUES (?, 'status', ?, ?, ?)""",
            (zlecenie_id, current, next_st, uid),
        )
        if note:
            db.execute(
                "UPDATE kolejka_zlecenia SET uwagi=? WHERE nr_zamowienia=?",
                (note, nr),
            )
        db.commit()
        _log_audit("dostawa_advance", session["username"], f"po={nr} {current}→{next_st}")
        return jsonify({
            "ok": True,
            "status": next_st,
            "label": _DELIVERY_STATUS_LABELS.get(next_st, next_st),
        })
    finally:
        db.close()


@app.route("/api/dostawy/<path:nr>/revert", methods=["POST"])
@login_required
@require_role("manager")
@csrf_protect
def api_dostawa_revert(nr):
    """Cofa status dostawy o jeden etap wstecz (manager/admin), z zapisem w logu."""
    uid = session["user_id"]
    db = get_db()
    try:
        row = db.execute(
            "SELECT id, status FROM kolejka_zlecenia WHERE nr_zamowienia=?", (nr,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono zamówienia"}), 404

        current = row["status"] or "utworzone"
        raw_status = row["status"]
        prev_st = _delivery_prev_status(current)
        if not prev_st:
            return jsonify({"error": "To pierwszy etap — nie ma dokąd cofać"}), 400

        # Compare-and-swap: cofnij tylko jeśli status nie zmienił się od odczytu.
        if raw_status is None:
            _upd = db.execute(
                "UPDATE kolejka_zlecenia SET status=?, updated_at=datetime('now') "
                "WHERE nr_zamowienia=? AND status IS NULL",
                (prev_st, nr),
            )
        else:
            _upd = db.execute(
                "UPDATE kolejka_zlecenia SET status=?, updated_at=datetime('now') "
                "WHERE nr_zamowienia=? AND status=?",
                (prev_st, nr, raw_status),
            )
        if _upd.rowcount == 0:
            db.rollback()
            _fresh = db.execute(
                "SELECT status FROM kolejka_zlecenia WHERE nr_zamowienia=?", (nr,)
            ).fetchone()
            _fresh_st = (_fresh["status"] if _fresh else "") or ""
            return jsonify({
                "error": "Status zmienił się w międzyczasie — odśwież stronę.",
                "current_status": _fresh_st,
                "label": _DELIVERY_STATUS_LABELS.get(_fresh_st, _fresh_st),
            }), 409
        db.execute(
            """INSERT INTO kolejka_status_log
               (zlecenie_id, pole, stara_wartosc, nowa_wartosc, changed_by)
               VALUES (?, 'status', ?, ?, ?)""",
            (row["id"], current, prev_st, uid),
        )
        db.commit()
        _log_audit("dostawa_revert", session["username"], f"po={nr} {current}→{prev_st} (cofnięcie)")
        return jsonify({
            "ok": True,
            "status": prev_st,
            "label": _DELIVERY_STATUS_LABELS.get(prev_st, prev_st),
        })
    finally:
        db.close()


@app.route("/api/dostawy/<path:nr>/set-status", methods=["POST"])
@login_required
@require_role("manager")
@csrf_protect
def api_dostawa_set_status(nr):
    """Ręczne wymuszenie dowolnego statusu dostawy (manager/admin).

    Pozwala m.in. ręcznie ustawić „Wysłane do dostawcy" albo nadpisać wynik
    weryfikacji PO/PI na „Sprawdzone — zgodne", aby przejść dalej, gdy
    automatyczne porównanie dało inny wynik. Każda zmiana trafia do logu."""
    uid = session["user_id"]
    data = request.get_json(silent=True) or {}
    new_status = str(data.get("status") or "").strip()
    if new_status not in _DELIVERY_STATUS_LABELS:
        return jsonify({"error": "Nieznany status"}), 400
    note = str(data.get("note") or "").strip()[:500]
    db = get_db()
    try:
        row = db.execute(
            "SELECT id, status FROM kolejka_zlecenia WHERE nr_zamowienia=?", (nr,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono zamówienia"}), 404
        current = row["status"] or "utworzone"
        if new_status == current:
            return jsonify({"ok": True, "status": current,
                            "label": _DELIVERY_STATUS_LABELS.get(current, current)})
        # Nadpisanie negatywnego wyniku weryfikacji wymaga uzasadnienia (notatki).
        if current in _DELIVERY_BLOCKED_ADVANCE and not note:
            return jsonify({
                "error": ("Nadpisanie statusu „"
                          + _DELIVERY_STATUS_LABELS.get(current, current)
                          + "” wymaga uzasadnienia — wpisz notatkę."),
                "need_note": True,
            }), 400
        # Miękkie ostrzeżenie: ręczne wymuszenie omija bramki, ale jeśli docelowy
        # status wymaga dokumentów, których brak — poproś o świadome potwierdzenie.
        if not bool(data.get("confirm")):
            missing = _delivery_missing_required_docs(new_status, _present_doc_types(db, nr))
            if missing:
                return jsonify({
                    "needs_confirm": True,
                    "missing": missing,
                    "message": "Uwaga: dla statusu „"
                               + _DELIVERY_STATUS_LABELS.get(new_status, new_status)
                               + "” brakuje dokumentów: " + ", ".join(missing)
                               + ". Wymusić mimo to?",
                })
        db.execute(
            "UPDATE kolejka_zlecenia SET status=?, updated_at=datetime('now') "
            "WHERE nr_zamowienia=?",
            (new_status, nr),
        )
        db.execute(
            """INSERT INTO kolejka_status_log
               (zlecenie_id, pole, stara_wartosc, nowa_wartosc, changed_by, source)
               VALUES (?, 'status', ?, ?, ?, 'manual')""",
            (row["id"], current, new_status, uid),
        )
        if note:
            db.execute(
                "UPDATE kolejka_zlecenia SET uwagi=? WHERE nr_zamowienia=?",
                (note, nr),
            )
        db.commit()
        _log_audit("dostawa_set_status", session["username"],
                   f"po={nr} {current}→{new_status} (ręczne wymuszenie)")
        return jsonify({
            "ok": True,
            "status": new_status,
            "label": _DELIVERY_STATUS_LABELS.get(new_status, new_status),
        })
    finally:
        db.close()


def _extract_total_from_pdf(path: str):
    """Fallback sumy z tekstu PDF: liczba po słowie 'TOTAL'/'Total', preferując
    kwotę z 2 miejscami po przecinku (np. 26201.59) zamiast ilości (2337.500).
    Zwraca float albo None."""
    import re as _re
    from normalizer import normalize_number
    try:
        text = extract_pdf_text(path) or ""
    except Exception:
        return None
    fallback = None
    for m in _re.finditer(r'\bTOTAL\b[:\s]*([\s\S]{0,80})', text, _re.IGNORECASE):
        for num in _re.findall(r'\d[\d.,\s]{0,18}\d', m.group(1)):
            val = normalize_number(num.strip())
            if val is None:
                continue
            if _re.search(r'[.,]\d{2}(?!\d)', num):   # ma 2 miejsca → kwota
                return val
            if fallback is None:
                fallback = val
    return fallback


def _sum_item_field(items, key: str):
    """Σ wartości pola (np. net_a) ze wszystkich pozycji; None gdy brak danych."""
    from normalizer import normalize_number
    total, has = 0.0, False
    for it in (items or []):
        v = normalize_number(str(it.get(key) or ""))
        if v is not None:
            total += float(v)   # normalize_number zwraca Decimal — rzutuj, by nie mieszać z float
            has = True
    return total if has else None


def _ai_extract_header_fields(doc_text: str) -> dict:
    """AI fallback (Q15): wyciąga pola nagłówkowe z trudnego dokumentu (proforma
    o nietypowym layoucie). Zwraca {} gdy AI niedostępne/błąd."""
    if not doc_text or not doc_text.strip():
        return {}
    try:
        from ai_validator import _call_claude
    except Exception:
        return {}
    system = ("Jesteś precyzyjnym ekstraktorem danych z dokumentów handlu "
              "międzynarodowego. Odpowiadasz WYŁĄCZNIE poprawnym JSON-em, bez "
              "komentarzy. Brak danego pola = pusty string.")
    prompt = (
        "Wyciągnij pola nagłówkowe i zwróć dokładnie taki JSON:\n"
        '{"numer_pi":"","waluta":"kod ISO np USD","warunki_platnosci":"",'
        '"incoterms":"np FOB WUHAN","data_dokumentu":"YYYY-MM-DD",'
        '"data_dostawy":"YYYY-MM-DD","port_zaladunku":"","port_rozladunku":"",'
        '"buyer":"","seller":""}\n\nTEKST DOKUMENTU:\n' + doc_text[:6000]
    )
    res = _call_claude(prompt, system=system, max_tokens=500)
    if not isinstance(res, dict) or res.get("error"):
        return {}
    return {k: str(v).strip() for k, v in res.items()
            if isinstance(v, (str, int, float)) and str(v).strip()}


def _extract_pi_lots(pi_text: str, item_refs) -> dict:
    """Wyciąga numery LOT z proformy i paruje z REF-ami po kolejności (Q10 — LOT-y
    zachowane do późniejszego porównania przy CI/PL/celnej). Zwraca {ref: lot}
    tylko przy pewnym parowaniu (liczba LOT == liczba REF)."""
    import re as _re
    seen, ordered = set(), []
    for l in _re.findall(r'\b(\d{9}[A-Z])\b', pi_text or ""):
        if l not in seen:
            seen.add(l)
            ordered.append(l)
    refs = [r for r in (item_refs or []) if r]
    if ordered and refs and len(ordered) == len(refs):
        return dict(zip(refs, ordered))
    return {}


def _validate_pi_dates(pi_path: str) -> list:
    """Sanity dat z proformy (Q12): MFG ≤ DELIVERY ≤ EXP. Lista ostrzeżeń."""
    import re as _re
    from normalizer import normalize_date
    try:
        text = extract_pdf_text(pi_path) or ""
    except Exception:
        return []
    _D = r'(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4})'

    def _find(*labels):
        for lab in labels:
            m = _re.search(lab + r'[:\s]*' + _D, text, _re.IGNORECASE)
            if m:
                d = normalize_date(m.group(1))
                if d:
                    return d
        return None

    mfg = _find(r'MFG', r'manufactur\w*', r'production\s*date')
    exp = _find(r'EXP', r'expir\w*', r'valid\s*(?:until|through)')
    dlv = _find(r'DELIVERY\s*DATE', r'delivery\s*date')
    w = []
    if mfg and dlv and mfg > dlv:
        w.append(f"Niespójność dat PI: produkcja (MFG {mfg}) po dacie dostawy ({dlv})")
    if dlv and exp and dlv > exp:
        w.append(f"Niespójność dat PI: dostawa ({dlv}) po dacie ważności (EXP {exp})")
    if mfg and exp and mfg > exp:
        w.append(f"Niespójność dat PI: produkcja (MFG {mfg}) po EXP ({exp})")
    return w


@app.route("/api/dostawy/<path:nr>/verify-po-pi", methods=["POST"])
@login_required
@require_role("manager")
@csrf_protect
def api_dostawa_verify_po_pi(nr):
    """Ręczna weryfikacja PO↔PI (przycisk). Rdzeń: _perform_po_pi_verification.

    Obsługa ponownego porównania (mode):
      • 'check' (domyślnie) — gdy istnieje już raport PO/PI, zwróć {duplicate:True}
        i pozwól operatorowi zdecydować (nadpisz / zrób kolejny),
      • 'overwrite' — soft-usuń poprzednie raporty PO/PI, potem porównaj,
      • 'new' — po prostu zrób kolejne porównanie obok.
    """
    data = request.get_json(silent=True) or {}
    mode = (data.get("mode") or "check").strip()
    db = get_db()
    try:
        existing = db.execute(
            "SELECT COUNT(*) FROM comparisons WHERE po_number=? AND doc_type='PO_vs_PI' "
            "AND COALESCE(is_deleted,0)=0", (nr,)).fetchone()[0]
        if mode == "check" and existing:
            return jsonify({"duplicate": True, "count": existing})
        if mode == "overwrite" and existing:
            db.execute(
                "UPDATE comparisons SET is_deleted=1, deleted_at=datetime('now'), deleted_by=? "
                "WHERE po_number=? AND doc_type='PO_vs_PI' AND COALESCE(is_deleted,0)=0",
                (session["user_id"], nr))
            db.commit()
    finally:
        db.close()
    res = _perform_po_pi_verification(nr, session["user_id"], session["username"])
    if res.get("error") == "not_found":
        return jsonify({"error": "Nie znaleziono zamówienia"}), 404
    if res.get("error") in ("missing_files", "missing_files_disk"):
        return jsonify({"error": "Wymagane oba pliki: PO i PI"}), 400
    if res.get("error"):
        return jsonify({"error": "Błąd porównania PO/PI"}), 500
    return jsonify({
        "ok": True, "comparison_id": res["comparison_id"],
        "result_status": res["result_status"], "diff_count": res["diff_count"],
        "status": res["status"],
        "label": _DELIVERY_STATUS_LABELS.get(res["status"], res["status"]),
        "report_saved": res["report_saved"],
    })


@app.route("/api/dostawy/<path:nr>/comparisons/<int:cid>", methods=["DELETE"])
@login_required
@csrf_protect
def api_dostawa_comparison_delete(nr, cid):
    """Soft-usunięcie raportu porównania z dostawy (superuser/admin).
    Raport znika z listy 'Raporty porównań' (is_deleted=1)."""
    if not can_delete(session.get("role", "")):
        return jsonify({"error": "Brak uprawnień do usuwania raportów"}), 403
    db = get_db()
    try:
        row = db.execute(
            "SELECT id FROM comparisons WHERE id=? AND po_number=?", (cid, nr)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono raportu"}), 404
        db.execute(
            "UPDATE comparisons SET is_deleted=1, deleted_at=datetime('now'), deleted_by=? "
            "WHERE id=?", (session["user_id"], cid))
        db.commit()
    finally:
        db.close()
    _log_audit("dostawa_raport_delete", session.get("username"), f"po={nr} cid={cid}")
    return jsonify({"ok": True, "deleted_id": cid})


def _try_auto_compare_po_pi(nr: str, uid: int, username: str) -> bool:
    """Auto-porównanie PO↔PI w tle po wgraniu PI, gdy PO już jest. Wątek-daemon,
    żeby upload odpowiedział od razu — status ustawi wynik. Zwraca True gdy ruszono."""
    try:
        db = get_db()
        try:
            has_po = db.execute(
                "SELECT 1 FROM shipment_documents WHERE po_number=? AND doc_type='PO' LIMIT 1",
                (nr,)).fetchone()
            has_pi = db.execute(
                "SELECT 1 FROM shipment_documents WHERE po_number=? AND doc_type='PI' LIMIT 1",
                (nr,)).fetchone()
        finally:
            db.close()
        if not has_po or not has_pi:
            return False
        # Faza 2: licz w workerze (RQ) gdy REDIS_URL, inaczej w wątku-daemonie.
        # _perform_po_pi_verification jest funkcją modułową (serializowalną przez RQ)
        # i sama zapisuje status/wynik do bazy.
        import jobs as _jobs
        _jobs.enqueue(_perform_po_pi_verification, nr, uid, username)
        return True
    except Exception as _e:
        logger.debug("_try_auto_compare_po_pi error po=%s: %s", nr, _e)
        return False


def _perform_po_pi_verification(nr: str, uid: int, username: str) -> dict:
    """Rdzeń weryfikacji PO↔PI (bez request/session — można z wątku w tle): porównanie
    (PO=A/lewa, PI=B/prawa), zapis wyniku + raportu PDF jako dokument dostawy, status
    wg wyniku i powiadomienia. Zwraca dict z wynikiem albo {"error": <kod>}."""
    db = get_db()
    try:
        zrow = db.execute("SELECT id FROM kolejka_zlecenia WHERE nr_zamowienia=?", (nr,)).fetchone()
        if not zrow:
            return {"error": "not_found"}
        po_doc = db.execute(
            "SELECT filename FROM shipment_documents WHERE po_number=? AND doc_type='PO' "
            "ORDER BY uploaded_at DESC LIMIT 1", (nr,)).fetchone()
        pi_doc = db.execute(
            "SELECT filename FROM shipment_documents WHERE po_number=? AND doc_type='PI' "
            "ORDER BY uploaded_at DESC LIMIT 1", (nr,)).fetchone()
    finally:
        db.close()
    if not po_doc or not pi_doc:
        return {"error": "missing_files"}

    folder = _ensure_shipment_folder(nr)
    po_path = os.path.join(folder, po_doc["filename"])
    pi_path = os.path.join(folder, pi_doc["filename"])
    if not os.path.exists(po_path) or not os.path.exists(pi_path):
        return {"error": "missing_files_disk"}

    try:
        # Pełne porównanie tabelaryczne (jak /analyze) — pozycje, sumy i nagłówki,
        # które export_pdf renderuje kompletnie. Plus enhanced dla terminów/arytmetyki.
        from table_extractor import compare_tables
        from enhanced_comparator import compare_enhanced
        # Przeliczniki jednostek (#4) → profil przekazywany do komparatora.
        _uom_conv = []
        try:
            import uom as _uom
            _udb = get_db()
            try:
                _uom_conv = _uom.load_conversions(_udb)
            finally:
                _udb.close()
        except Exception:
            _uom_conv = []
        _profile = {"uom_conversions": _uom_conv} if _uom_conv else None
        with _HEAVY_SEM:
            td = compare_tables(po_path, pi_path).to_dict()
            ed = compare_enhanced(po_path, pi_path, "PO", "PI", supplier_profile=_profile).to_dict()
    except Exception:
        logger.exception("verify-po-pi compare error po=%s", nr)
        return {"error": "compare_failed"}

    from normalizer import numbers_equal as _numbers_equal, normalize_number as _norm_num

    # ── Suma PO (total_a): fallback z tekstu 'TOTAL …' gdy tabela jej nie poda ──
    items = td.get("items", []) or []
    _ta_raw = td.get("total_a")
    total_a_uncertain = False
    if not _ta_raw or str(_ta_raw).strip() in ("", "—", "None"):
        _ta_fb = _extract_total_from_pdf(po_path)
        if _ta_fb is None:                      # nadal brak → Σ pozycji jako ostatnia deska
            _ta_fb = _sum_item_field(items, "net_a")
            total_a_uncertain = _ta_fb is None
        if _ta_fb is not None:
            td["total_a"] = f"{_ta_fb:.2f}"

    # ── Porównanie sum PO vs PI (zero tolerancji) + walidacja arytmetyki ──
    extra_diffs = 0
    warnings = []
    ta_v = _norm_num(str(td.get("total_a") or ""))
    tb_v = _norm_num(str(td.get("total_b") or ""))
    if ta_v is not None and tb_v is not None:
        if _numbers_equal(str(ta_v), str(tb_v)) is False:
            extra_diffs += 1
            warnings.append(f"Suma PO {ta_v:.2f} ≠ suma PI {tb_v:.2f} (Δ {tb_v-ta_v:+.2f})")
    # Arytmetyka (checksum): Σ pozycji vs zadeklarowana suma. Niezgodność = twarda
    # różnica → status „niezgodne" (decyzja produktowa: checksum blokuje jak niezgodne).
    for side, tot in (("net_a", ta_v), ("net_b", tb_v)):
        s = _sum_item_field(items, side)
        if s is not None and tot is not None and _numbers_equal(str(s), str(tot)) is False:
            extra_diffs += 1
            warnings.append(f"Σ pozycji ({side}) {s:.2f} ≠ suma {tot:.2f} — checksum niezgodny")

    # Walidacja dat proformy (Q12): MFG < DELIVERY < EXP
    _date_warns = []
    try:
        _date_warns = _validate_pi_dates(pi_path)
        warnings.extend(_date_warns)
    except Exception as _e:
        logger.debug("PI date validation failed po=%s: %s", nr, _e)

    # Walidacja pozycji względem master daty materiałów (#16 — źródło prawdy):
    # nieznany REF / niezgodny EAN → ostrzeżenie (do weryfikacji).
    header_warns = 0
    try:
        import material_master as _mm
        _mdb = get_db()
        try:
            _mat_warns = _mm.validate_items(_mdb, items, ref_key="ref", ean_key="ean_a")
        finally:
            _mdb.close()
        warnings.extend(_mat_warns)
        header_warns += len(_mat_warns)
    except Exception as _e:
        logger.debug("material master validation failed po=%s: %s", nr, _e)

    # ── Nagłówki: reguły + AI fallback dla brakujących pól krytycznych (Q7/Q15) ──
    pi_lots = {}
    try:
        from enhanced_comparator import extract_header_fields, compare_header_fields
        _AI_MAP = {"numer_pi": "Nr PI dostawcy", "waluta": "Waluta",
                   "warunki_platnosci": "Warunki płatności", "incoterms": "Warunki dostawy",
                   "data_dokumentu": "Data dokumentu", "data_dostawy": "Data dostawy",
                   "port_zaladunku": "Port załadunku", "port_rozladunku": "Port rozładunku"}
        _CRIT = ["Waluta", "Warunki płatności", "Warunki dostawy"]
        _po_text = extract_pdf_text(po_path) or ""
        _pi_text = extract_pdf_text(pi_path) or ""
        fa = extract_header_fields(_po_text, "PO")
        fb = extract_header_fields(_pi_text, "PI")
        for _fields, _text in ((fa, _po_text), (fb, _pi_text)):
            if any(not _fields.get(k) for k in _CRIT):     # reguły zawiodły → AI
                _ai = _ai_extract_header_fields(_text)
                for _ak, _our in _AI_MAP.items():
                    if _ai.get(_ak) and not _fields.get(_our):
                        _fields[_our] = _ai[_ak]
        hdrs, hdr_findings = compare_header_fields(fa, fb, None)
        # Wzbogać incoterms o pełną nazwę ze słownika + flaguj kody spoza słownika
        _inc = _load_incoterms_dict()
        if hdrs and _inc:
            _IC_RE = re.compile(
                r'\b(FOB|CIF|EXW|DAP|CFR|CPT|DDP|FCA|FAS|CIP|DDU|DAT|DAF|DEQ|DES|FH|UN)\b')
            _unknown = set()
            for h in hdrs:
                if 'dostaw' in str(h.get("key", "")).lower() or 'incoterm' in str(h.get("key", "")).lower():
                    for _side in ("val_a", "val_b"):
                        _v = str(h.get(_side) or "")
                        m = _IC_RE.search(_v.upper())
                        if not m:
                            continue
                        _code = m.group(1)
                        _desc = _inc.get(_code)
                        if _desc and _desc not in _v:
                            h[_side] = f"{_v} — {_desc}"
                        elif not _desc and _code not in _unknown:
                            _unknown.add(_code)
                            warnings.append(f'Incoterm „{_code}" spoza słownika — uzupełnij w /incoterms')
                            header_warns += 1
        if hdrs:
            td["headers"] = hdrs                            # wzbogacone nagłówki do raportu
        # LOT-y z PI — zachowane do późniejszego porównania (CI/PL/celna)
        pi_lots = _extract_pi_lots(_pi_text, [it.get("ref") for it in items])
        for f in hdr_findings:
            if f.severity in ("error", "critical"):         # krytyczna różnica → niezgodne
                extra_diffs += 1
                warnings.append(f"{f.field}: {f.val_a} ≠ {f.val_b}")
            elif f.severity == "warning":
                header_warns += 1
    except Exception as _e:
        logger.debug("header AI/compare failed po=%s: %s", nr, _e)

    # ── Status: twarde różnice → niezgodne; niepewna ekstrakcja → do weryfikacji ──
    diff_count = (td.get("diff_count", 0) or 0) + (ed.get("diff_count", 0) or 0) + extra_diffs
    if diff_count > 0:
        comp_status = "blad"
    elif (total_a_uncertain or _date_warns or header_warns
          or td.get("warn_count", 0) or ed.get("warn_count", 0)):
        comp_status = "ostrzezenie"        # do weryfikacji (niepewność / miękkie)
    else:
        comp_status = "ok"
    result = {
        "risk_level": comp_status,
        "file_a": po_doc["filename"], "file_b": pi_doc["filename"],
        "doc_type_a": "PO", "doc_type_b": "PI",
        "diff_count": diff_count, "summary": td.get("summary", "") or ed.get("summary", ""),
        "warnings": warnings,
        "pi_lots": pi_lots,
        "modules": {
            "table": {
                "ok": True,
                "doc_type_a": "PO", "doc_type_b": "PI",
                "diff_count": td.get("diff_count", 0),
                "warn_count": td.get("warn_count", 0),
                "format_count": td.get("format_count", 0),
                "ok_count": td.get("ok_count", 0),
                "risk_level": td.get("risk_level", "ok"),
                "summary": td.get("summary", ""),
                "headers": td.get("headers", []),
                "items": td.get("items", []),
                "total_a": td.get("total_a"),
                "total_b": td.get("total_b"),
                "total_qty_a": td.get("total_qty_a"),
                "total_qty_b": td.get("total_qty_b"),
            },
            "enhanced": ed,
        },
    }

    # Zapis wyniku porównania
    cid = None
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO comparisons (user_id, doc_type, file_a, file_b, result_json, "
            "status, diff_count, po_number, created_at) VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
            (uid, "PO_vs_PI", po_doc["filename"], pi_doc["filename"],
             json.dumps(result), comp_status, diff_count, nr))
        cid = cur.lastrowid
        db.commit()
    finally:
        db.close()

    # Raport PDF → dokument dostawy (3. kafelek / druga linia)
    report_saved = False
    try:
        from export_engine import export_pdf
        pdf_bytes = export_pdf(result, cid)
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
        tmp_pdf = os.path.join(app.config["UPLOAD_FOLDER"], f"raport_po_pi_{nr}_{cid}.pdf")
        with open(tmp_pdf, "wb") as fp:
            fp.write(pdf_bytes)
        try:
            report_saved = _save_doc_to_shipment(
                nr, tmp_pdf, f"Raport PO-PI {nr}.pdf", "RAPORT_PO_PI", uid, source="auto")
        finally:
            try:
                os.remove(tmp_pdf)
            except OSError:
                pass
    except Exception as e:
        logger.warning("verify-po-pi report PDF failed po=%s: %s", nr, e)

    # Status wg wyniku
    new_status = _delivery_status_from_comparison(comp_status)
    _set_dostawa_status(nr, new_status, uid)
    _log_audit("dostawa_verify_po_pi", username,
               f"po={nr} {comp_status}→{new_status} diffs={diff_count} cid={cid}")
    if new_status == "sprawdzone_niezgodne":
        _notify_managers(
            f"Weryfikacja PO/PI — niezgodne: {nr}",
            f"Porównanie PO↔PI dla {nr} wykazało {diff_count} różnic(e). "
            f"Dostawa wymaga przeglądu przed dalszym procedowaniem.",
            link=f"/dostawy/{nr}",
        )

    return {
        "comparison_id": cid,
        "result_status": comp_status,
        "diff_count": diff_count,
        "status": new_status,
        "report_saved": report_saved,
    }


def _perform_artwork_verification(nr: str, uid: int, username: str) -> dict:
    """Rdzeń weryfikacji artworku: porównuje ARTWORK (wzorzec/master) ↔ ARTWORK_B
    (fabryczny), zapisuje porównanie i raport PDF jako dokument dostawy, ustawia
    status wg wyniku (artwork_zgodne / niezgodne / błędne) i powiadamia managerów.

    BEZ zależności od request/session — można wołać z wątku w tle (auto-weryfikacja
    po wgraniu artworku fabrycznego). Zwraca dict:
      {"comparison_id", "result_status", "diff_count", "status", "report_saved"}
    albo {"error": <kod>} gdy brak plików / błąd porównania.
    """
    db = get_db()
    try:
        a_doc = db.execute(
            "SELECT filename FROM shipment_documents WHERE po_number=? AND doc_type='ARTWORK' "
            "ORDER BY uploaded_at DESC LIMIT 1", (nr,)).fetchone()
        b_doc = db.execute(
            "SELECT filename FROM shipment_documents WHERE po_number=? AND doc_type='ARTWORK_B' "
            "ORDER BY uploaded_at DESC LIMIT 1", (nr,)).fetchone()
    finally:
        db.close()
    if not a_doc or not b_doc:
        return {"error": "missing_files"}

    folder = _ensure_shipment_folder(nr)
    a_path = os.path.join(folder, a_doc["filename"])
    b_path = os.path.join(folder, b_doc["filename"])
    if not os.path.exists(a_path) or not os.path.exists(b_path):
        return {"error": "missing_files_disk"}

    try:
        from artwork_engine_client import compare_artworks
        with _HEAVY_SEM:
            result = compare_artworks(a_path, b_path, use_ai=False, max_pages=6)
        rdict = result.to_dict(include_images=False)
        risk = result.risk_level
        diff_count = result.critical_count + result.important_count
    except Exception:
        logger.exception("verify-artwork compare error po=%s", nr)
        return {"error": "compare_failed"}

    cid = None
    db = get_db()
    try:
        rdict["status"] = risk
        rdict["diff_count"] = diff_count
        cur = db.execute(
            "INSERT INTO comparisons (user_id, doc_type, file_a, file_b, result_json, "
            "status, diff_count, po_number, created_at) VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
            (uid, "artwork", a_doc["filename"], b_doc["filename"],
             json.dumps(_strip_images_from_result(rdict)), risk, diff_count, nr))
        cid = cur.lastrowid
        db.commit()
    finally:
        db.close()

    report_saved = False
    try:
        from artwork_report_engine import build_report_from_comparison, export_to_pdf
        report = build_report_from_comparison(rdict)
        pdf_bytes = export_to_pdf(report)
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
        tmp_pdf = os.path.join(app.config["UPLOAD_FOLDER"], f"raport_artwork_{nr}_{cid}.pdf")
        with open(tmp_pdf, "wb") as fp:
            fp.write(pdf_bytes)
        try:
            report_saved = _save_doc_to_shipment(
                nr, tmp_pdf, f"Raport Artwork {nr}.pdf", "RAPORT_ARTWORK", uid, source="auto")
        finally:
            try:
                os.remove(tmp_pdf)
            except OSError:
                pass
    except Exception as e:
        logger.warning("verify-artwork report PDF failed po=%s: %s", nr, e)

    # Decyzja #7: różnice w KODACH/TEKŚCIE (EAN/LOT/data/REF/oznaczenia MDR =
    # severity 'critical') → artwork_bledne (twarda bramka); różnice GRAFICZNE/kolory/
    # wymiary (severity 'important') → artwork_niezgodne („do weryfikacji", miękka).
    if result.critical_count > 0:
        new_status = "artwork_bledne"
    elif result.important_count > 0:
        new_status = "artwork_niezgodne"
    else:
        new_status = "artwork_zgodne"
    _set_dostawa_status(nr, new_status, uid)
    _log_audit("dostawa_verify_artwork", username,
               f"po={nr} {risk}→{new_status} crit={result.critical_count} "
               f"imp={result.important_count} cid={cid}")
    if new_status == "artwork_bledne":
        _notify_managers(
            f"Weryfikacja artworku — błędne: {nr}",
            f"Porównanie artworku (wzorzec↔fabryczny) dla {nr} wykazało "
            f"{result.critical_count} krytyczn(ych) różnic w kodach/tekście "
            f"(EAN/LOT/data/oznaczenia). Wymagany przegląd.",
            link=f"/dostawy/{nr}",
        )

    return {
        "comparison_id": cid, "result_status": risk, "diff_count": diff_count,
        "status": new_status, "report_saved": report_saved,
    }


def _try_auto_verify_artwork(nr: str, uid: int, username: str) -> bool:
    """Auto-weryfikacja artworku w tle po wgraniu pliku fabrycznego (ARTWORK_B),
    o ile wzorzec (ARTWORK) jest już podpięty. Odpala porównanie w wątku-daemonie,
    żeby upload odpowiedział od razu — status zmieni się po zakończeniu. Zwraca
    True, gdy weryfikacja została uruchomiona."""
    try:
        db = get_db()
        try:
            has_a = db.execute(
                "SELECT 1 FROM shipment_documents WHERE po_number=? AND doc_type='ARTWORK' LIMIT 1",
                (nr,)).fetchone()
            has_b = db.execute(
                "SELECT 1 FROM shipment_documents WHERE po_number=? AND doc_type='ARTWORK_B' LIMIT 1",
                (nr,)).fetchone()
        finally:
            db.close()
        if not has_a or not has_b:
            return False

        # Faza 2: licz w workerze (RQ) gdy REDIS_URL, inaczej w wątku-daemonie.
        # _perform_artwork_verification jest funkcją modułową (serializowalną przez RQ)
        # i sama zapisuje status/wynik do bazy.
        import jobs as _jobs
        _jobs.enqueue(_perform_artwork_verification, nr, uid, username)
        return True
    except Exception as _e:
        logger.debug("_try_auto_verify_artwork error po=%s: %s", nr, _e)
        return False


@app.route("/api/dostawy/<path:nr>/verify-artwork", methods=["POST"])
@login_required
@require_role("manager")
@csrf_protect
def api_dostawa_verify_artwork(nr):
    """Weryfikacja artworku: porównuje ARTWORK (wzorzec/master) ↔ ARTWORK_B
    (fabryczny), zapisuje raport PDF jako dokument dostawy i ustawia status wg
    wyniku (artwork_zgodne / niezgodne / błędne)."""
    uid = session["user_id"]
    db = get_db()
    try:
        zrow = db.execute("SELECT id FROM kolejka_zlecenia WHERE nr_zamowienia=?", (nr,)).fetchone()
        if not zrow:
            return jsonify({"error": "Nie znaleziono zamówienia"}), 404
    finally:
        db.close()

    res = _perform_artwork_verification(nr, uid, session["username"])
    if res.get("error") in ("missing_files", "missing_files_disk"):
        return jsonify({"error": "Wymagane oba pliki: Artwork wzorzec i fabryczny"}), 400
    if res.get("error"):
        return jsonify({"error": "Błąd porównania artworku"}), 500

    return jsonify({
        "ok": True,
        "comparison_id": res["comparison_id"],
        "result_status": res["result_status"],
        "diff_count": res["diff_count"],
        "status": res["status"],
        "label": _DELIVERY_STATUS_LABELS.get(res["status"], res["status"]),
        "report_saved": res["report_saved"],
    })


@app.route("/api/dostawy/<path:nr>", methods=["DELETE"])
@login_required
@require_role("admin")
@csrf_protect
def api_dostawa_delete(nr):
    db = get_db()
    try:
        row = db.execute(
            "SELECT id, status FROM kolejka_zlecenia WHERE nr_zamowienia=?", (nr,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono"}), 404
        db.execute("DELETE FROM kolejka_status_log WHERE zlecenie_id=?", (row["id"],))
        db.execute("DELETE FROM kolejka_zlecenia WHERE nr_zamowienia=?", (nr,))
        db.commit()
        _log_audit("dostawa_delete", session["username"], f"po={nr}")
        return jsonify({"ok": True})
    finally:
        db.close()


# ── Transport orders ──────────────────────────────────────────────────────
@app.route("/api/transport-orders", methods=["GET"])
@login_required
def api_transport_orders_list():
    db = get_db()
    try:
        rows = db.execute("""
            SELECT zt.id, zt.numer, zt.status, zt.uwagi, zt.created_at,
                   COUNT(zti.id) as item_count
            FROM zlecenia_transportowe zt
            LEFT JOIN zlecenia_transport_items zti ON zti.transport_id = zt.id
            GROUP BY zt.id ORDER BY zt.created_at DESC
        """).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@app.route("/api/transport-orders", methods=["POST"])
@login_required
@require_role("manager")
@csrf_protect
def api_transport_orders_create():
    data = request.get_json(silent=True) or {}
    po_numbers = data.get("po_numbers") or []
    if isinstance(po_numbers, str):
        po_numbers = [po_numbers]
    uwagi = str(data.get("uwagi") or "").strip()[:1000]
    if not po_numbers:
        return jsonify({"error": "Podaj numery zamówień"}), 400
    db = get_db()
    try:
        from datetime import datetime as _dt_trans
        year = _dt_trans.utcnow().year
        # numer has a UNIQUE constraint. COUNT(*) is race-prone (gaps from deletes +
        # concurrent inserts collide), so derive next from MAX existing suffix and
        # retry on IntegrityError to recover from concurrent inserts cleanly.
        prefix = f"ZT-{year}-"
        numer = None
        tid = None
        last_err = None
        for _attempt in range(5):
            rows = db.execute(
                "SELECT numer FROM zlecenia_transportowe WHERE numer LIKE ?",
                (f"{prefix}%",)
            ).fetchall()
            max_seq = 0
            for r in rows:
                suffix = str(r["numer"])[len(prefix):]
                if suffix.isdigit():
                    max_seq = max(max_seq, int(suffix))
            numer = f"{prefix}{max_seq + 1 + _attempt:03d}"
            try:
                cur = db.execute(
                    "INSERT INTO zlecenia_transportowe (numer, uwagi, created_by) VALUES (?, ?, ?)",
                    (numer, uwagi, session["user_id"])
                )
                tid = cur.lastrowid
                break
            except IntegrityError as _ie:
                last_err = _ie
                db.rollback()
                continue
        if tid is None:
            logger.warning("transport order numer generation failed: %s", last_err)
            return jsonify({"error": "Nie udało się wygenerować numeru zlecenia — spróbuj ponownie."}), 409
        for po in po_numbers:
            po = str(po).strip()[:100]
            if not po:
                continue
            db.execute(
                "INSERT OR IGNORE INTO zlecenia_transport_items (transport_id, po_number) VALUES (?, ?)",
                (tid, po)
            )
        db.commit()
        _log_audit("transport_order_create", session["username"], f"numer={numer} pos={','.join(po_numbers)}")
        return jsonify({"ok": True, "numer": numer, "id": tid})
    finally:
        db.close()


@app.route("/api/transport-orders/<int:tid>/items", methods=["POST"])
@login_required
@require_role("manager")
@csrf_protect
def api_transport_order_add_item(tid):
    """Add a PO to an existing transport order (consolidation)."""
    data = request.get_json(silent=True) or {}
    po = str(data.get("po_number") or "").strip()
    if not po:
        return jsonify({"error": "Brak numeru zamówienia"}), 400
    db = get_db()
    try:
        row = db.execute("SELECT id FROM zlecenia_transportowe WHERE id=?", (tid,)).fetchone()
        if not row:
            return jsonify({"error": "Zlecenie nie znalezione"}), 404
        db.execute(
            "INSERT OR IGNORE INTO zlecenia_transport_items (transport_id, po_number) VALUES (?, ?)",
            (tid, po)
        )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/dostawy/<path:nr>/transport-order", methods=["GET"])
@login_required
def api_dostawa_transport_order(nr):
    """Get the transport order for a given PO, if any."""
    db = get_db()
    try:
        row = db.execute("""
            SELECT zt.id, zt.numer, zt.status, zt.uwagi, zt.created_at,
                   COUNT(zti2.id) as item_count
            FROM zlecenia_transport_items zti
            JOIN zlecenia_transportowe zt ON zt.id = zti.transport_id
            LEFT JOIN zlecenia_transport_items zti2 ON zti2.transport_id = zt.id
            WHERE zti.po_number = ?
            GROUP BY zt.id
        """, (nr,)).fetchone()
        if not row:
            return jsonify({"transport_order": None})
        return jsonify({"transport_order": dict(row)})
    finally:
        db.close()


# ── Krok 1: zwróć pozycje PO ───────────────────────────────────────────────
@app.route("/api/dostawy/backfill-refs", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_dostawy_backfill_refs():
    """Jednorazowo czyści ref_code we wszystkich pozycjach PO (usuwa doklejony
    opis), korzystając z master daty. Zwraca liczbę zaktualizowanych wierszy."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT po_number, pozycja, ref_code, opis FROM po_line_items WHERE ref_code<>''"
        ).fetchall()
        changed = 0
        for r in rows:
            cr, op = _clean_po_ref(db, r["ref_code"], r["opis"] or "")
            if cr and cr != r["ref_code"]:
                db.execute(
                    "UPDATE po_line_items SET ref_code=?, opis=? WHERE po_number=? AND pozycja=?",
                    (cr, op, r["po_number"], r["pozycja"]),
                )
                changed += 1
        db.commit()
    finally:
        db.close()
    _log_audit("po_refs_backfill", session.get("username"), f"updated={changed}")
    return jsonify({"ok": True, "updated": changed})


def _dims_to_m3(dim_str):
    """'520 x 380 x 300 mm' → objętość pojedynczego opakowania w m³.
    Obsługuje jednostki mm/cm/m (domyślnie mm). None, gdy nie da się odczytać."""
    if not dim_str:
        return None
    s = str(dim_str).lower().replace(",", ".")
    if "cm" in s:
        div = 1e6
    elif re.search(r"(?<![a-z])m(?![a-z])", s) and "mm" not in s:
        div = 1.0
    else:
        div = 1e9  # mm³ → m³
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    if len(nums) < 3:
        return None
    vol = (float(nums[0]) * float(nums[1]) * float(nums[2])) / div
    return vol if vol > 0 else None


def _logistics_for_line(db, ref_code, ilosc, jednostka):
    """Logistyka pozycji PO: {palety, miejsca_paletowe, m3} (None, gdy brak danych).

    • palety           = ilość_w_bazie / PAZ (ile bazowych JM mieści paleta),
    • miejsca_paletowe = ceil(palety)  — 1,5 palety = 2 miejsca paletowe,
    • m3               = liczba_kartonów × objętość_kartonu (wymiary KAR z masterdaty).
    Przeliczniki UOM (w tym PAZ) z uom_conversion; wymiary z levels_json materiału."""
    res = {"palety": None, "miejsca_paletowe": None, "m3": None}
    try:
        if not ref_code or not ilosc:
            return res
        qty = float(ilosc)
        if qty <= 0:
            return res
        import math, json as _json
        import uom as _uom, material_master as _mm
        ref_norm = _mm.normalize_ref(ref_code)
        if not ref_norm:
            return res
        rows = db.execute(
            "SELECT unit_from, unit_to, factor FROM uom_conversion WHERE ref_norm=?",
            (ref_norm,)
        ).fetchall()
        conv, bases = {}, set()
        for r in rows:
            try:
                f = float(r["factor"])
            except (TypeError, ValueError):
                continue
            if f <= 0:
                continue
            uf = _uom.canonical_unit(r["unit_from"])
            ut = _uom.canonical_unit(r["unit_to"])
            conv[(uf, ut)] = f
            bases.add(ut)
        base = next(iter(bases)) if len(bases) == 1 else None
        # Ilość w jednostce bazowej (pozycja może być w innej JM niż bazowa).
        base_qty = None
        if base:
            ju = _uom.canonical_unit(jednostka or "")
            if not ju or ju == base:
                base_qty = qty
            elif (ju, base) in conv:
                base_qty = qty * conv[(ju, base)]
        # Palety + miejsca paletowe.
        if base and base_qty is not None:
            paz_factor = conv.get((_uom.canonical_unit("PAZ"), base))
            if paz_factor:
                palety = round(base_qty / paz_factor, 2)
                res["palety"] = palety
                res["miejsca_paletowe"] = int(math.ceil(palety - 1e-9))
        # Objętość m³ z wymiarów kartonu × liczba kartonów.
        try:
            mat = _mm.get_material(db, ref_code)
            levels = _json.loads((mat or {}).get("levels_json") or "{}")
            kar = levels.get("karton") or {}
            vol_kar = _dims_to_m3(kar.get("wymiar"))
            kqb = kar.get("qty_base")
            if base and base_qty is not None and vol_kar and kqb:
                kqf = float(str(kqb).replace(",", "."))
                if kqf > 0:
                    res["m3"] = round((base_qty / kqf) * vol_kar, 3)
        except Exception:
            pass
        return res
    except Exception:
        return res


@app.route("/api/dostawy/<path:nr>/line-items", methods=["GET"])
@login_required
def api_dostawa_line_items(nr):
    db = get_db()
    try:
        items = db.execute(
            "SELECT pozycja, ref_code, opis, ilosc, jednostka, cena_jednostkowa, wartosc, waluta "
            "FROM po_line_items WHERE po_number=? ORDER BY pozycja",
            (nr,)
        ).fetchall()
        out, fixed = [], 0
        total_pallets, total_places, total_m3 = 0.0, 0, 0.0
        any_pallet, any_m3 = False, False
        for r in items:
            it = dict(r)
            cr, op = _clean_po_ref(db, it.get("ref_code") or "", it.get("opis") or "")
            # Rozdziel REF od opisu również dla danych starszych (sklejone w bazie)
            # i utrwal poprawkę, żeby raporty/auto-master też miały czysty REF.
            if cr and (cr != it.get("ref_code") or op != it.get("opis")):
                db.execute(
                    "UPDATE po_line_items SET ref_code=?, opis=? WHERE po_number=? AND pozycja=?",
                    (cr, op, nr, it["pozycja"]),
                )
                it["ref_code"], it["opis"] = cr, op
                fixed += 1
            # Logistyka: palety, miejsca paletowe i objętość m³ (z masterdaty).
            log = _logistics_for_line(db, it.get("ref_code"), it.get("ilosc"), it.get("jednostka"))
            it["palety"] = log["palety"]
            it["miejsca_paletowe"] = log["miejsca_paletowe"]
            it["m3"] = log["m3"]
            if log["palety"] is not None:
                total_pallets += log["palety"]
                any_pallet = True
            if log["miejsca_paletowe"] is not None:
                total_places += log["miejsca_paletowe"]
            if log["m3"] is not None:
                total_m3 += log["m3"]
                any_m3 = True
            out.append(it)
        if fixed:
            db.commit()
        return jsonify({
            "items": out,
            "total_pallets": round(total_pallets, 2) if any_pallet else None,
            "total_places": total_places if any_pallet else None,
            "total_m3": round(total_m3, 3) if any_m3 else None,
        })
    finally:
        db.close()


_ART_LEVELS = ["sztuka", "op", "opz", "karton"]
_ART_LEVEL_LBL = {"sztuka": "SZT", "op": "OP", "opz": "OPZ", "karton": "KAR"}


@app.route("/api/dostawy/<path:nr>/artwork-masters", methods=["GET"])
@login_required
def api_dostawa_artwork_masters(nr):
    """Auto-dobór masterów artworków dla zamówienia: dla każdego REF z PO i każdego
    poziomu opakowania zwraca NAJNOWSZĄ rewizję mastera + status potwierdzenia/dryfu."""
    import material_master as _mm
    import artwork_index as _ai
    db = get_db()
    try:
        _ai.ensure_table(db)
        try:
            _index_size = db.execute("SELECT COUNT(*) FROM artwork_index").fetchone()[0]
        except Exception:
            _index_size = 0
        # Pliki master dostępne w chmurze (agent dosyła bajty potwierdzonych/grupowych).
        try:
            _avail = _ai.available_master_paths(db)
        except Exception:
            _avail = set()
        from urllib.parse import quote as _q
        _raw = [r[0] for r in db.execute(
            "SELECT DISTINCT ref_code FROM po_line_items WHERE po_number=? AND ref_code<>''",
            (nr,)).fetchall()]
        refs = []
        for _rr in _raw:
            _cr = _clean_po_ref(db, _rr)[0]
            if _cr and _cr not in refs:
                refs.append(_cr)
        out = []
        for ref in refs:
            mat = _mm.get_material(db, ref)
            _mat_found = bool(mat)
            _has_desc = bool(mat and (str(mat.get("opis_pl") or "").strip()
                                      or str(mat.get("opis_en") or "").strip()
                                      or str(mat.get("txt_short_pl") or "").strip()))
            levels = {}
            if mat and mat.get("levels_json"):
                try:
                    levels = json.loads(mat["levels_json"])
                except (ValueError, TypeError):
                    levels = {}
            # cele: poziomy z master daty (z artwork_ref albo sam REF); brak → 1 wpis (REF)
            # (etykieta, surowy_klucz_poziomu, art_ref) — surowy klucz steruje
            # doborem per-poziom (carton dla KAR, pouch dla OP).
            targets = []
            for lv in _ART_LEVELS:
                d = levels.get(lv)
                if d is not None:
                    art_ref = (str(d.get("artwork_ref") or "").strip() or ref)
                    targets.append((_ART_LEVEL_LBL.get(lv, lv), lv, art_ref))
            if not targets:
                targets = [("—", "", ref)]
            # Tekst do doboru kandydatów po OPISIE materiału (nazwa pliku często nie
            # zawiera REF). Liczony raz; ranking kandydatów robimy PER POZIOM.
            _txt = ""
            if mat:
                _txt = " ".join(x for x in (
                    str(mat.get("opis_pl") or ""), str(mat.get("opis_en") or ""),
                    str(mat.get("txt_short_pl") or ""), str(ref)) if x).strip()
            for lvl, lv_raw, art_ref in targets:
                st = _ai.lookup_for_comparison(db, art_ref, lv_raw)
                newest = st.get("newest") or {}
                _conf = st.get("confirmed") or {}
                # Plik prezentowany = potwierdzony (wybrany master), chyba że pojawiła
                # się NOWSZA rewizja (wtedy pokaż ją, by skłonić do ponownego potwierdzenia).
                _disp = _conf if (_conf and not st.get("revision_changed")) else (newest or _conf)
                # Fallback: master z GRUPY mapowań, gdy REF nie ma własnego mastera.
                _grp = st.get("group_master") or {}
                _via_group = False
                if not _disp and _grp:
                    _disp = _grp
                    _via_group = True
                # Kandydaci specyficzni dla poziomu (carton wyżej dla KAR itd.).
                _row_cands = []
                if _txt:
                    try:
                        _row_cands = [{
                            "filename": c.get("filename", ""), "rel_path": c.get("rel_path", ""),
                            "score": c.get("score"), "shared": c.get("shared"),
                            "revision": c.get("revision", ""),
                            "source_mtime": c.get("source_mtime", ""),
                        } for c in (_ai.suggest_by_text(db, _txt, limit=5, level=lv_raw) or [])
                            if c.get("rel_path") != _disp.get("rel_path")]
                    except Exception:
                        _row_cands = []
                out.append({
                    "ref": ref, "level": lvl, "level_key": lv_raw, "art_ref": art_ref,
                    "found": bool(_disp),
                    "filename": _disp.get("filename", ""),
                    "rel_path": _disp.get("rel_path", ""),
                    "revision": _disp.get("revision", ""),
                    "source_mtime": _disp.get("source_mtime", "") if isinstance(_disp, dict) else "",
                    "confirmed": bool(st.get("confirmed")),
                    "confirmed_filename": _conf.get("filename", "") if isinstance(_conf, dict) else "",
                    "confirmed_revision": _conf.get("revision", "") if isinstance(_conf, dict) else "",
                    "needs_confirmation": st.get("needs_confirmation", True) and bool(newest),
                    "revision_changed": st.get("revision_changed", False),
                    "via_group": _via_group,
                    "group_name": _grp.get("group_name", "") if _via_group else "",
                    "needs_mapping": not bool(_disp),
                    "candidates": _row_cands,
                    "mat_found": _mat_found,
                    "has_desc": _has_desc,
                    "preview_available": (_disp.get("rel_path", "") in _avail) if _disp else False,
                    "preview_url": ("/api/artwork/master-file/view?rel_path="
                                    + _q(_disp.get("rel_path", ""))) if (_disp and _disp.get("rel_path", "") in _avail) else "",
                })
    finally:
        db.close()
    return jsonify({"items": out, "refs": len(refs), "index_size": _index_size})


@app.route("/api/dostawy/<path:nr>/suggest-supplier", methods=["GET"])
@login_required
def api_dostawa_suggest_supplier(nr):
    """Auto-podpowiedź dostawcy: z REF-ów PO → kody producentów (master data
    materiałów) → dostawca (master data dostawców) wg producer_code. Zwraca
    dominującego dostawcę + jego domyślne (waluta/incoterms/płatności/lead time)."""
    import material_master as _mm
    import supplier_master as _sm
    db = get_db()
    try:
        _raw = [r[0] for r in db.execute(
            "SELECT DISTINCT ref_code FROM po_line_items WHERE po_number=? AND ref_code<>''",
            (nr,)).fetchall()]
        refs = []
        for _rr in _raw:
            _cr = _clean_po_ref(db, _rr)[0]
            if _cr and _cr not in refs:
                refs.append(_cr)
        producers = {}
        for ref in refs:
            mat = _mm.get_material(db, ref)
            pc = (mat or {}).get("producer_code") if mat else ""
            pc = (pc or "").strip()
            if pc:
                producers[pc] = producers.get(pc, 0) + 1
        suppliers = {}   # code → {supplier, count}
        for pc, cnt in producers.items():
            s = _sm.supplier_for_producer(db, pc)
            if s:
                code = s["code"]
                if code not in suppliers:
                    suppliers[code] = {"supplier": s, "count": 0, "producers": []}
                suppliers[code]["count"] += cnt
                suppliers[code]["producers"].append(pc)
        ranked = sorted(suppliers.values(), key=lambda x: -x["count"])
        best = ranked[0]["supplier"] if ranked else None
    finally:
        db.close()

    def _slim(s):
        return {
            "code": s.get("code"), "name": s.get("name"),
            "currency": s.get("currency"), "incoterms": s.get("incoterms"),
            "payment_terms_default": s.get("payment_terms_default"),
            "lead_time_days": s.get("lead_time_days"),
            "country": s.get("country"),
        }
    return jsonify({
        "suggested": _slim(best) if best else None,
        "multiple": len(suppliers) > 1,
        "candidates": [{**_slim(v["supplier"]), "count": v["count"],
                        "producers": v["producers"]} for v in ranked],
        "producers": list(producers.keys()),
        "refs": len(refs),
    })


# Stara ręczna weryfikacja proformy (/api/dostawy/<nr>/verify) usunięta —
# weryfikacja PO/PI odbywa się wyłącznie automatycznie przez „Weryfikuj PO/PI"
# (verify-po-pi → statusy sprawdzone_*). Eliminuje to drugi, niespójny słownik
# werdyktów (potwierdzone/odrzucone).


# ── Krok 5a: aktualizuj zlecenie transportowe (spedytor, email) ───────────
@app.route("/api/transport-orders/<int:tid>", methods=["PATCH"])
@login_required
@require_role("manager")
@csrf_protect
def api_transport_order_update(tid):
    data = request.get_json(silent=True) or {}
    allowed = ("spedytor_name", "forwarder_email", "uwagi", "status")
    _to_max_lens = {"spedytor_name": 200, "forwarder_email": 254, "uwagi": 1000, "status": 50}
    db = get_db()
    try:
        row = db.execute("SELECT id FROM zlecenia_transportowe WHERE id=?", (tid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono zlecenia"}), 404
        _VALID_TRANSPORT_ORDER_STATUSES = {"nowe", "w_trakcie", "zamkniete"}
        updates = {}
        for k, v in data.items():
            if k in allowed:
                if k == "status":
                    if v not in _VALID_TRANSPORT_ORDER_STATUSES:
                        continue
                elif k in _to_max_lens:
                    v = str(v or "")[:_to_max_lens[k]]
                updates[k] = v
        if not updates:
            return jsonify({"ok": True})
        set_clause = ", ".join(f"{k}=?" for k in updates)
        db.execute(
            # bandit: nazwy kolumn z białej listy w kodzie (nie z kluczy żądania); wartości przez ?
            f"UPDATE zlecenia_transportowe SET {set_clause}, updated_at=datetime('now') WHERE id=?",  # nosec B608
            list(updates.values()) + [tid]
        )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


# ── Krok 5b: spedytor przypisuje agenta ───────────────────────────────────
@app.route("/api/transport-orders/<int:tid>/agent", methods=["POST"])
@login_required
@require_role("manager")
@csrf_protect
def api_transport_order_set_agent(tid):
    data = request.get_json(silent=True) or {}
    agent_name  = str(data.get("agent_name") or "").strip()[:200]
    agent_email = str(data.get("agent_email") or "").strip()[:254]
    agent_phone = str(data.get("agent_phone") or "").strip()[:50]
    if not agent_name:
        return jsonify({"error": "Nazwa agenta jest wymagana"}), 400
    db = get_db()
    try:
        row = db.execute("SELECT id, numer FROM zlecenia_transportowe WHERE id=?", (tid,)).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono zlecenia"}), 404
        uid = session["user_id"]
        db.execute(
            "UPDATE zlecenia_transportowe SET agent_name=?, agent_email=?, agent_phone=?, "
            "agent_set_by=?, agent_set_at=datetime('now'), status='w_trakcie', "
            "updated_at=datetime('now') WHERE id=?",
            (agent_name, agent_email, agent_phone, uid, tid)
        )
        # Notify all POs in this transport order
        po_rows = db.execute(
            "SELECT po_number FROM zlecenia_transport_items WHERE transport_id=?", (tid,)
        ).fetchall()
        for po_row in po_rows:
            po_nr = po_row["po_number"]
            _deliver_notification(
                db=db, user_id=None, rola="manager",
                zlecenie_id=None, po_number=po_nr,
                typ="status",
                tresc=f"Agent przypisany do zlecenia {row['numer']} (PO: {po_nr}): {agent_name}"
            )
        db.commit()
        _log_audit("transport_agent_set", session["username"],
                   f"tid={tid} agent={agent_name}")
        return jsonify({"ok": True, "agent_name": agent_name})
    finally:
        db.close()


def _deliver_notification(db, user_id, rola, zlecenie_id, po_number, typ, tresc):
    """Insert a notification into kolejka_powiadomienia."""
    try:
        db.execute(
            "INSERT INTO kolejka_powiadomienia (user_id, rola, zlecenie_id, typ, tresc) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, rola or "", zlecenie_id, typ, tresc)
        )
    except Exception as _e:
        logger.debug("_deliver_notification error: %s", _e)


# ── Krok 5c: potwierdzenie wypłynięcia kontenera ──────────────────────────
@app.route("/api/dostawy/<path:nr>/confirm-departure", methods=["POST"])
@login_required
@require_role("manager")
@csrf_protect
def api_dostawa_confirm_departure(nr):
    data = request.get_json(silent=True) or {}
    etd_real     = str(data.get("etd_real") or "").strip()[:20]
    seal_number  = str(data.get("seal_number") or "").strip()[:100]
    numer_kontenera = str(data.get("numer_kontenera") or "").strip()[:100]
    if not etd_real:
        return jsonify({"error": "Data wypłynięcia (ETD) jest wymagana"}), 400
    db = get_db()
    try:
        row = db.execute(
            "SELECT z.id, z.kontener_id, z.status FROM kolejka_zlecenia z "
            "WHERE z.nr_zamowienia=?", (nr,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Nie znaleziono zamówienia"}), 404
        uid = session["user_id"]
        kontener_id = row["kontener_id"]
        if kontener_id:
            db.execute(
                # bandit: WHERE/SQL składany ze stałych fragmentów w kodzie; wartości przez ?
                "UPDATE kolejka_kontenery SET etd_real=?, departure_confirmed=1, "  # nosec B608
                "departure_confirmed_by=?, departure_confirmed_at=datetime('now'), "
                "updated_at=datetime('now')"
                + (", seal_number=?" if seal_number else "")
                + " WHERE id=?",
                ([etd_real, uid] + ([seal_number] if seal_number else []) + [kontener_id])
            )
        elif numer_kontenera:
            cur = db.execute(
                "INSERT INTO kolejka_kontenery (numer_kontenera, etd_real, departure_confirmed, "
                "departure_confirmed_by, departure_confirmed_at, seal_number, created_by) "
                "VALUES (?, ?, 1, ?, datetime('now'), ?, ?)",
                (numer_kontenera, etd_real, uid, seal_number, uid)
            )
            kontener_id = cur.lastrowid
            db.execute(
                "UPDATE kolejka_zlecenia SET kontener_id=? WHERE nr_zamowienia=?",
                (kontener_id, nr)
            )
        current = row["status"] or ""
        # Po potwierdzeniu ETD/wypłynięcia przesuwamy na krok „Porównanie ETD — SAP"
        # (etd_sap) ze stanów sprzed spedycji. Nie cofamy dostaw, które już są dalej.
        _pre_boarding = ("artwork_oczekiwanie", "zatwierdzone_do_wyplyniecia",
                         "artwork_zgodne", "artwork_niezgodne", "artwork_bledne")
        if current in _pre_boarding:
            db.execute(
                "UPDATE kolejka_zlecenia SET status='etd_sap', updated_at=datetime('now') "
                "WHERE nr_zamowienia=?", (nr,)
            )
            db.execute(
                "INSERT INTO kolejka_status_log (zlecenie_id, pole, stara_wartosc, nowa_wartosc, changed_by) "
                "VALUES (?, 'status', ?, 'etd_sap', ?)",
                (row["id"], current, uid)
            )
        db.commit()
        _log_audit("departure_confirmed", session["username"], f"po={nr} etd={etd_real}")
        return jsonify({"ok": True})
    finally:
        db.close()


if __name__ == "__main__":
    print("=" * 60)
    print("  DocCompare — lokalny silnik (bez API)")
    print("  http://localhost:5000")
    print()
    print("  Nowe funkcje:")
    print("    /batch      — batch processing wielu par PDF")
    print("    /suppliers  — profile dostawców")
    print()
    print("  Eksport: PDF i Excel z każdego raportu")
    print("=" * 60)
    _debug = os.environ.get("FLASK_DEBUG", "0") == "1" or os.environ.get("FLASK_ENV") == "development"
    # Debugger Werkzeuga (RCE) nigdy nie wystawiany w sieci — w debug tylko localhost.
    # bandit: 0.0.0.0 celowo: dostęp z LAN wg README.txt; w trybie debug host=127.0.0.1
    app.run(debug=_debug, host="127.0.0.1" if _debug else "0.0.0.0", port=5000)  # nosec B104
