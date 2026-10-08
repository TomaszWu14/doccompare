"""tests/test_auth_blueprint.py — regresja dla blueprints/auth.py (Phase 3, REFACTOR-01/02/03).

Sprawdza rzeczy, których żaden istniejący test nie pilnował przed wydzieleniem
`auth` do blueprintu:
  1. `url_for("auth.login")` rozwiązuje się w CAŁEJ aplikacji — anonimowe żądanie do
     dowolnej chronionej trasy przekierowuje (302) do /login, a nie 500 (BuildError).
  2. Prawdziwe logowanie przez przeniesioną trasę /login nadal działa end-to-end
     (rate-limiter, sesja).
  3. core/validation.py's validate_body decorator (REFACTOR-03).
  4. reset-password rejects a too-short password BEFORE touching users.password_hash,
     and the happy path still updates the hash (REFACTOR-03).
"""
import secrets

import pytest

pytest.importorskip("flask")

from tests.conftest import CSRF_TOKEN, TEST_USERS, set_session_csrf  # noqa: E402


def test_anonymous_protected_route_redirects_to_login(client):
    """Would 500 with a werkzeug BuildError if core/security.py's url_for("login")
    rename to url_for("auth.login") were missed (RESEARCH.md Pitfall 2)."""
    resp = client.get("/compare")
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("Location", "")


def test_login_via_blueprint_route_works(admin_client):
    """Proves the moved /login route (+ its rate-limiter) still works end-to-end —
    admin_client fixture logs in via the real POST /login flow (see conftest.py)."""
    resp = admin_client.get("/home")
    assert resp.status_code == 200


def test_login_oversized_password_rerenders_without_500(client):
    """LoginForm's Field(max_length=1000) replaces the old inline len()>1000 check —
    must still re-render login.html, not 500."""
    set_session_csrf(client, CSRF_TOKEN)
    resp = client.post("/login", data={
        "identifier": "it_admin",
        "password": "x" * 1001,
        "_csrf_token": CSRF_TOKEN,
    })
    assert resp.status_code == 200
    assert b"user_id" not in resp.data


# ── core/validation.py: validate_body decorator ───────────────────────────────
def test_validate_body_rejects_missing_field():
    from flask import Flask
    from pydantic import BaseModel

    from core.validation import validate_body

    class _Dummy(BaseModel):
        name: str

    dummy_app = Flask(__name__)

    @dummy_app.route("/dummy", methods=["POST"])
    @validate_body(_Dummy)
    def _dummy_view(body):
        return {"name": body.name}

    resp = dummy_app.test_client().post("/dummy", json={})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_validate_body_passes_through_valid_body():
    from flask import Flask
    from pydantic import BaseModel

    from core.validation import validate_body

    class _Dummy(BaseModel):
        name: str

    dummy_app = Flask(__name__)

    @dummy_app.route("/dummy", methods=["POST"])
    @validate_body(_Dummy)
    def _dummy_view(body):
        return {"name": body.name}

    resp = dummy_app.test_client().post("/dummy", json={"name": "x"})
    assert resp.status_code == 200
    assert resp.get_json()["name"] == "x"


# ── reset-password: Pydantic-gated, no write on invalid input ─────────────────
def _make_reset_token(app_mod, username="it_admin", minutes_valid=60):
    db = app_mod.get_db()
    try:
        user = db.execute(
            "SELECT id, password_hash FROM users WHERE username=?", (username,)
        ).fetchone()
        token = secrets.token_urlsafe(16)
        db.execute(
            "INSERT INTO password_reset_tokens(user_id, token, expires_at) "
            "VALUES(?,?,datetime('now',?))",
            (user["id"], token, f"+{minutes_valid} minutes"),
        )
        db.commit()
        return token, user["password_hash"]
    finally:
        db.close()


def test_reset_password_short_password_no_write(client):
    import app as _app_mod

    token, original_hash = _make_reset_token(_app_mod)
    set_session_csrf(client, CSRF_TOKEN)
    resp = client.post("/reset-password", data={
        "token": token,
        "password": "short1",
        "confirm": "short1",
        "_csrf_token": CSRF_TOKEN,
    })
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "znak" in body  # Polish "co najmniej 8 znaków" error rendered

    db = _app_mod.get_db()
    try:
        row = db.execute(
            "SELECT password_hash FROM users WHERE username='it_admin'"
        ).fetchone()
    finally:
        db.close()
    assert row["password_hash"] == original_hash


def test_reset_password_valid_password_updates_hash(client):
    import app as _app_mod
    from werkzeug.security import check_password_hash

    token, original_hash = _make_reset_token(_app_mod)
    set_session_csrf(client, CSRF_TOKEN)
    resp = client.post("/reset-password", data={
        "token": token,
        "password": "nowehaslo123",
        "confirm": "nowehaslo123",
        "_csrf_token": CSRF_TOKEN,
    })
    assert resp.status_code == 200

    db = _app_mod.get_db()
    try:
        row = db.execute(
            "SELECT password_hash FROM users WHERE username='it_admin'"
        ).fetchone()
    finally:
        db.close()
    assert row["password_hash"] != original_hash
    assert check_password_hash(row["password_hash"], "nowehaslo123")
    # Restore the seeded password so later tests (it_admin login) keep working.
    from werkzeug.security import generate_password_hash
    db = _app_mod.get_db()
    try:
        db.execute("UPDATE users SET password_hash=? WHERE username='it_admin'",
                   (generate_password_hash(TEST_USERS["it_admin"][0]),))
        db.commit()
    finally:
        db.close()
