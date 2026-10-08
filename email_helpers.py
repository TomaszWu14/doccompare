"""
email_helpers.py — transakcyjne maile wysyłane przez Resend, wydzielone z app.py
(Phase 3, REFACTOR-01).

`send_reset_email` ma DWA wywołujące: blueprints/auth.py (forgot_password) i
app.py (endpoint admina generujący link resetu — app.py:~4078). Stąd wspólny
top-level moduł zamiast wewnętrznego auth-helpera (auth nie może być importowany
z powrotem do app.py — patrz ARCHITECTURE.md "blueprint → core/db, nigdy app").
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("doccompare")


def send_reset_email(to_email: str, username: str, reset_url: str) -> bool:
    """Send password reset email via Resend. Returns True if sent."""
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        return False
    email_from = os.environ.get(
        "EMAIL_FROM", "DocCompare <noreply@doccompare.app>"
    )
    from markupsafe import escape as _esc
    username_safe = str(_esc(username))
    html_body = (
        f"<p>Cześć <strong>{username_safe}</strong>,</p>"
        "<p>Otrzymaliśmy prośbę o reset hasła do Twojego konta DocCompare.</p>"
        f'<p><a href="{reset_url}" style="background:#5b9bff;color:#fff;padding:10px 20px;'
        f'border-radius:6px;text-decoration:none;font-weight:600">Ustaw nowe hasło →</a></p>'
        "<p style='color:#999;font-size:12px'>Link ważny 24 godziny. "
        "Jeśli nie prosiłeś o reset, zignoruj tę wiadomość.</p>"
    )
    try:
        import httpx
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "DocCompare/1.0",
            },
            json={"from": email_from, "to": [to_email],
                  "subject": "Reset hasła — DocCompare", "html": html_body},
            timeout=15,
        )
        if resp.status_code in (200, 201):
            logger.info("Reset email sent to %s", to_email)
            return True
        logger.warning("Resend error %s: %s", resp.status_code, resp.text)
        return False
    except Exception as exc:
        logger.warning("Resend exception: %s", exc)
        return False
