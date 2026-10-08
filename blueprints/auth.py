"""
blueprints/auth.py — logowanie/wylogowanie/wybór języka/reset hasła (auth domain,
wydzielony z app.py).

Trasy przeniesione dosłownie z app.py: `/login`, `/set-language/<lang>`, `/logout`,
`/forgot-password`, `/reset-password` (plus stan obu rate-limiterów: logowania i
resetu hasła). `ts_ago`/`send_reset_email` są dzielone z app.py (druga strona
_send_reset_email jest wołana przez endpoint admina) — stąd core/timeutil.py i
email_helpers.py zamiast lokalnych kopii.

Import wyłącznie core/db (blueprint → core, nigdy → app). core/security.py's
url_for("login") calls target `auth.login` — patrz Pitfall 2 w 03-RESEARCH.md.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for
from pydantic import BaseModel, Field, ValidationError, model_validator
from werkzeug.security import check_password_hash, generate_password_hash

from core.audit import log_audit as _log_audit
from core.security import csrf_protect, login_required
from core.timeutil import ts_ago as _ts_ago
from db import get_db
from email_helpers import send_reset_email

logger = logging.getLogger("doccompare")

bp = Blueprint("auth", __name__)


# ── Pydantic input models (REFACTOR-03) ────────────────────────────────────────
# Auth routes render HTML on error (not JSON), so these are used INLINE via
# .model_validate() inside each view — not via core.validation.validate_body,
# which is for the JSON-body suppliers/warehouse routes.
class LoginForm(BaseModel):
    identifier: str = Field(max_length=254)
    password: str = Field(max_length=1000)


class ForgotForm(BaseModel):
    identifier: str = Field(default="", max_length=254)


class PasswordResetForm(BaseModel):
    password: str = Field(min_length=8, max_length=1000)
    confirm: str

    @model_validator(mode="after")
    def _passwords_match(self):
        if self.password != self.confirm:
            raise ValueError("Hasła nie są zgodne.")
        return self


def _password_reset_error(exc: ValidationError) -> str:
    """Map PasswordResetForm's ValidationError to the same Polish messages the
    pre-Pydantic hand-rolled checks produced."""
    first = exc.errors()[0]
    if first["type"] == "string_too_short":
        return "Hasło musi mieć co najmniej 8 znaków."
    if first["type"] == "string_too_long":
        return "Hasło jest za długie."
    msg = str(first.get("msg", ""))
    return msg.split("Value error, ", 1)[-1] if msg.startswith("Value error, ") else msg

# Rate limiting: { ip: [timestamp, ...] }
_login_attempts: dict = {}
_login_attempts_lock = threading.Lock()   # guards read-modify-write on _login_attempts
_RATE_LIMIT_MAX = 5       # max prób na okno
_RATE_LIMIT_WINDOW = 900  # 15 minut (sekundy)


def _check_rate_limit(ip: str) -> bool:
    """Return True if IP has too many recent failed login attempts.

    Queries audit_log so the limit survives server restarts and works across
    multiple worker processes.  Falls back to the in-memory dict on DB errors.
    """
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                           time.gmtime(time.time() - _RATE_LIMIT_WINDOW))
    try:
        db = get_db()
        try:
            count = db.execute(
                "SELECT COUNT(*) FROM audit_log WHERE event='login_fail' AND ip=? AND created_at > ?",
                (ip, cutoff)
            ).fetchone()[0]
            return count >= _RATE_LIMIT_MAX
        finally:
            db.close()
    except Exception:
        now = time.time()
        with _login_attempts_lock:
            attempts = _login_attempts.get(ip, [])
            attempts = [t for t in attempts if now - t < _RATE_LIMIT_WINDOW]
            _login_attempts[ip] = attempts
            return len(attempts) >= _RATE_LIMIT_MAX


def _clear_login_attempts(ip: str):
    with _login_attempts_lock:
        _login_attempts.pop(ip, None)


_forgot_attempts: dict = {}  # rate limit for password reset requests
_forgot_attempts_lock = threading.Lock()  # separate lock to avoid contention with _login_attempts_lock
_FORGOT_LIMIT_MAX = 3
_FORGOT_LIMIT_WINDOW = 900   # 15 minut


def _check_forgot_limit(ip: str) -> bool:
    """Return True if IP has exceeded the password-reset request rate limit.

    Uses audit_log so it persists across restarts.  Falls back to in-memory.
    """
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                           time.gmtime(time.time() - _FORGOT_LIMIT_WINDOW))
    try:
        db = get_db()
        try:
            count = db.execute(
                "SELECT COUNT(*) FROM audit_log WHERE event='forgot_request' AND ip=? AND created_at > ?",
                (ip, cutoff)
            ).fetchone()[0]
            return count >= _FORGOT_LIMIT_MAX
        finally:
            db.close()
    except Exception:
        now = time.time()
        with _forgot_attempts_lock:
            attempts = _forgot_attempts.get(ip, [])
            attempts = [t for t in attempts if now - t < _FORGOT_LIMIT_WINDOW]
            _forgot_attempts[ip] = attempts
            return len(attempts) >= _FORGOT_LIMIT_MAX


def _record_forgot_attempt(ip: str):
    now = time.time()
    with _forgot_attempts_lock:
        attempts = _forgot_attempts.get(ip, [])
        attempts.append(now)
        _forgot_attempts[ip] = attempts[-(_FORGOT_LIMIT_MAX + 5):]


@bp.route("/login", methods=["GET", "POST"])
@csrf_protect
def login():
    if "user_id" in session:
        return redirect(url_for("index"))
    error = None
    ip = request.remote_addr or "unknown"

    if request.method == "POST":
        if _check_rate_limit(ip):
            remaining = _RATE_LIMIT_WINDOW // 60
            return render_template("login.html",
                error=f"Zbyt wiele prób. Spróbuj za {remaining} min."), 429

        try:
            _form = LoginForm.model_validate({
                "identifier": request.form.get("identifier", "").strip(),
                "password": request.form.get("password", ""),
            })
        except ValidationError:
            error = "Nieprawidłowy email/login lub hasło."
            return render_template("login.html", error=error)
        identifier = _form.identifier
        password = _form.password
        db = get_db()
        login_ok = False
        login_user = None
        try:
            user = db.execute(
                """SELECT * FROM users
                   WHERE (LOWER(email)=LOWER(?) OR LOWER(username)=LOWER(?))
                   AND COALESCE(is_active,1)=1""",
                (identifier, identifier)
            ).fetchone()
            # Always run a hash check regardless of whether the user exists to prevent
            # user enumeration via response-time analysis.
            _dummy = "pbkdf2:sha256:260000$x$" + "a" * 64
            _pw_ok = check_password_hash(user["password_hash"] if user else _dummy, password)
            if user and _pw_ok:
                _clear_login_attempts(ip)
                session.clear()
                session.permanent = True
                session["user_id"]  = user["id"]
                session["username"] = user["username"]
                session["role"]     = user["role"]
                _u = dict(user)   # sqlite3.Row nie ma .get(); dict() działa na obu backendach
                session["forwarder_id"] = _u.get("forwarder_id")
                session["customs_agency_id"] = _u.get("customs_agency_id")
                session["department"] = _u.get("department") or ""
                session["lang"]     = _u.get("language", "pl") or "pl"
                db.execute("UPDATE users SET last_login=datetime('now') WHERE id=?", (user["id"],))
                db.commit()
                login_ok = True
                login_user = user
        finally:
            db.close()
        if login_ok:
            _log_audit("login", login_user["username"], f"IP {ip}")
            return redirect(url_for("index"))
        with _login_attempts_lock:
            _attempts = _login_attempts.get(ip, [])
            _attempts.append(time.time())
            _login_attempts[ip] = _attempts[-(_RATE_LIMIT_MAX + 5):]
            remaining_tries = max(0, _RATE_LIMIT_MAX - len(_login_attempts[ip]))
        _log_audit("login_fail", identifier, f"Błędne hasło, IP {ip}")
        error = "Nieprawidłowy email/login lub hasło."
        if remaining_tries < 3:
            error += f" Pozostało prób: {remaining_tries}"
    return render_template("login.html", error=error)


@bp.route("/set-language/<lang>")
@login_required
def set_language(lang):
    if lang not in ("pl", "en"):
        lang = "pl"
    session["lang"] = lang
    uid = session.get("user_id")
    if uid:
        db = get_db()
        try:
            db.execute("UPDATE users SET language=? WHERE id=?", (lang, uid))
            db.commit()
        finally:
            db.close()
    from urllib.parse import urlparse as _urlparse
    _ref = request.referrer or ""
    _safe = (_ref and _urlparse(_ref).netloc == _urlparse(request.host_url).netloc)
    return redirect(_ref if _safe else "/home")


@bp.route("/logout")
def logout():
    uname = session.get("username", "?")
    _log_audit("logout", uname)
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/forgot-password", methods=["GET", "POST"])
@csrf_protect
def forgot_password():
    message = None
    email_sent = False
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        if _check_forgot_limit(ip):
            return render_template("forgot_password.html",
                message="Zbyt wiele prób resetu hasła. Spróbuj ponownie za 15 minut.",
                email_sent=False), 429
        _record_forgot_attempt(ip)
        _log_audit("forgot_request", None, f"IP={ip}")
        try:
            identifier = ForgotForm.model_validate(
                {"identifier": request.form.get("identifier", "").strip()}
            ).identifier
        except ValidationError:
            # Oversized/malformed input: fall through to the same generic
            # anti-enumeration message as "no matching account" (never reveal
            # which case occurred).
            identifier = ""
        db = get_db()
        try:
            user = db.execute(
                "SELECT * FROM users WHERE LOWER(email)=LOWER(?) OR LOWER(username)=LOWER(?)",
                (identifier, identifier)
            ).fetchone()
            # Per-user throttle: cap reset tokens generated for one account in a
            # short window so an attacker can't email-bomb a user from rotating
            # IPs (the IP limit above only covers a single source).
            _throttled = False
            if user and user["email"]:
                _recent = db.execute(
                    "SELECT COUNT(*) FROM password_reset_tokens WHERE user_id=? "
                    "AND created_at > ?",
                    (user["id"], _ts_ago(minutes=15))
                ).fetchone()
                _throttled = bool(_recent and (_recent[0] or 0) >= 3)
            if user and user["email"] and not _throttled:
                token = secrets.token_urlsafe(32)
                # Invalidate any old unused tokens for this user to prevent token accumulation
                db.execute(
                    "UPDATE password_reset_tokens SET used=1 WHERE user_id=? AND used=0",
                    (user["id"],)
                )
                db.execute(
                    "INSERT INTO password_reset_tokens(user_id,token,expires_at) "
                    "VALUES(?,?,datetime('now','+24 hours'))",
                    (user["id"], token)
                )
                db.commit()
                _log_audit("password_reset_request", user["username"], "Token wygenerowany")
                base_url = os.environ.get("APP_BASE_URL", request.host_url.rstrip("/"))
                reset_url = f"{base_url}/reset-password?token={token}"
                email_sent = send_reset_email(user["email"], user["username"], reset_url)
                if not email_sent:
                    logger.error("password_reset: email send failed for user_id=%s", user["id"])
        finally:
            db.close()
        # Always show a generic success message regardless of whether the account
        # exists or the email send succeeded, to prevent user enumeration.
        message = "Jeśli podany adres e-mail lub nazwa użytkownika istnieje w systemie, wyślemy na nią link do resetu hasła. Sprawdź skrzynkę (i folder spam)."
        email_sent = True
    return render_template("forgot_password.html", message=message, email_sent=email_sent)


@bp.route("/reset-password", methods=["GET", "POST"])
@csrf_protect
def reset_password():
    token = request.args.get("token") or request.form.get("token", "")
    if not token:
        return redirect(url_for("auth.login"))

    db = get_db()
    error = None
    success = False
    expired = False
    username = ""
    try:
        row = db.execute(
            """SELECT pr.*, u.username FROM password_reset_tokens pr
               JOIN users u ON u.id=pr.user_id
               WHERE pr.token=? AND pr.used=0""",
            (token,)
        ).fetchone()
        # Compare expiry in Python — avoids TEXT vs TIMESTAMPTZ operator error on PostgreSQL.
        # Truncate to [:19] so "+00:00" timezone suffix from PostgreSQL TIMESTAMPTZ is ignored.
        from datetime import datetime as _dt, timezone as _tz
        _now_str = _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")
        if row and str(row["expires_at"])[:19] <= _now_str:
            row = None

        if not row:
            expired = True
        else:
            username = row["username"]
            if request.method == "POST":
                try:
                    _form = PasswordResetForm.model_validate({
                        "password": request.form.get("password", ""),
                        "confirm": request.form.get("confirm", ""),
                    })
                except ValidationError as _exc:
                    error = _password_reset_error(_exc)
                else:
                    # Atomically claim the token — prevents double-use race condition
                    _claim = db.execute(
                        "UPDATE password_reset_tokens SET used=1 WHERE token=? AND used=0",
                        (token,)
                    )
                    if _claim.rowcount == 0:
                        expired = True
                    else:
                        db.execute("UPDATE users SET password_hash=? WHERE id=?",
                                   (generate_password_hash(_form.password), row["user_id"]))
                        db.commit()
                        success = True
                        session.clear()
                        _log_audit("password_reset", row["username"], "Hasło zmienione")
    finally:
        db.close()
    if expired:
        return render_template("reset_password.html", expired=True, token=token)
    return render_template("reset_password.html", token=token, error=error,
                           success=success, username=username)
