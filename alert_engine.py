"""
alert_engine.py — email alerty dla krytycznych wyników porównania.

Konfiguracja przez zmienne środowiskowe lub tabelę settings w SQLite.
Ustawienia:
  ALERT_SMTP_HOST    — serwer SMTP (np. smtp.gmail.com)
  ALERT_SMTP_PORT    — port (domyślnie 587)
  ALERT_SMTP_USER    — login SMTP
  ALERT_SMTP_PASS    — hasło SMTP
  ALERT_FROM         — adres nadawcy
  ALERT_TO           — adres docelowy (lub kilka oddzielonych przecinkiem)
  ALERT_MIN_ERRORS   — min. błędów aby wysłać alert (domyślnie 1)
  ALERT_ENABLED      — '1' aby włączyć
"""

import os
import smtplib
import threading as _threading
import time as _time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from datetime import datetime
from typing import Optional
from db import get_db
from constants import APP_VERSION
import logging

logger = logging.getLogger(__name__)

# ── Alert config TTL cache ────────────────────────────────────────────────────
_alert_cfg_cache: dict = {}
_alert_cfg_lock = _threading.Lock()
_ALERT_CFG_TTL = 120  # 2 minutes

__all__ = [
    "get_alert_config",
    "save_alert_config",
    "should_alert",
    "build_email_html",
    "send_alert",
    "test_smtp",
]

# Module-level risk display constants
RISK_COLORS: dict[str, str] = {
    "ok":          "#4fce8e",
    "ostrzezenie": "#f7a84f",
    "blad":        "#f74f4f",
}

RISK_LABELS: dict[str, str] = {
    "ok":          "ZGODNE",
    "ostrzezenie": "OSTRZEŻENIA",
    "blad":        "BŁĘDY KRYTYCZNE",
}

RISK_EMOJI: dict[str, str] = {
    "ok":          "✅",
    "ostrzezenie": "⚠️",
    "blad":        "❌",
}


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# KONFIGURACJA
# ─────────────────────────────────────────────────────────────────────────────

def get_alert_config(db_path: str = "instance/doccompare.db") -> dict:
    """Czyta konfigurację alertów z tabeli settings lub zmiennych środowiskowych.
    Wynik jest cachowany przez _ALERT_CFG_TTL sekund."""
    # FIX 2: Check TTL cache first
    now = _time.time()
    with _alert_cfg_lock:
        cached = _alert_cfg_cache.get("cfg")
        cached_at = _alert_cfg_cache.get("ts", 0.0)
        if cached is not None and (now - cached_at) < _ALERT_CFG_TTL:
            return cached

    cfg = {
        "enabled":   os.environ.get("ALERT_ENABLED", "0") == "1",
        "smtp_host": os.environ.get("ALERT_SMTP_HOST", ""),
        "smtp_port": _safe_int(os.environ.get("ALERT_SMTP_PORT", "587"), 587),
        "smtp_user": os.environ.get("ALERT_SMTP_USER", ""),
        "smtp_pass": os.environ.get("ALERT_SMTP_PASS", ""),
        "from_addr": os.environ.get("ALERT_FROM", ""),
        "to_addrs":  [a.strip() for a in os.environ.get("ALERT_TO", "").split(",") if a.strip()],
        "min_errors": _safe_int(os.environ.get("ALERT_MIN_ERRORS", "1"), 1),
    }

    # Uzupełnij z bazy danych (settings table)
    try:
        db = get_db()
        try:
            rows = db.execute("SELECT key, value FROM settings WHERE category='alert'").fetchall()
        finally:
            db.close()
        mapping = {
            "alert_enabled":    ("enabled",   lambda v: v == "1"),
            "alert_smtp_host":  ("smtp_host", str),
            "alert_smtp_port":  ("smtp_port", int),
            "alert_smtp_user":  ("smtp_user", str),
            "alert_smtp_pass":  ("smtp_pass", str),
            "alert_from":       ("from_addr", str),
            "alert_to":         ("to_addrs",  lambda v: [a.strip() for a in v.split(",") if a.strip()]),
            "alert_min_errors": ("min_errors",int),
        }
        for row in rows:
            if row["key"] in mapping:
                cfg_key, converter = mapping[row["key"]]
                try:
                    cfg[cfg_key] = converter(row["value"])
                except Exception:
                    pass
    except Exception:
        pass  # Tabela settings może nie istnieć

    # Store in cache
    with _alert_cfg_lock:
        _alert_cfg_cache["cfg"] = cfg
        _alert_cfg_cache["ts"] = _time.time()

    return cfg


def save_alert_config(cfg: dict, db_path: str = "instance/doccompare.db"):
    """Zapisuje konfigurację alertów do bazy."""
    db = get_db()
    try:
        mapping = {
            "alert_enabled":    "1" if cfg.get("enabled") else "0",
            "alert_smtp_host":  cfg.get("smtp_host", ""),
            "alert_smtp_port":  str(cfg.get("smtp_port", 587)),
            "alert_smtp_user":  cfg.get("smtp_user", ""),
            "alert_smtp_pass":  cfg.get("smtp_pass", ""),
            "alert_from":       cfg.get("from_addr", ""),
            "alert_to":         ", ".join(cfg.get("to_addrs", [])),
            "alert_min_errors": str(cfg.get("min_errors", 1)),
        }
        for key, value in mapping.items():
            db.execute(
                "INSERT INTO settings(category, key, value) VALUES('alert',?,?) "
                "ON CONFLICT(category,key) DO UPDATE SET value=excluded.value",
                (key, value)
            )
        db.commit()
    finally:
        db.close()
    # Invalidate cache after save
    with _alert_cfg_lock:
        _alert_cfg_cache.clear()


# ─────────────────────────────────────────────────────────────────────────────
# SPRAWDZENIE CZY WYSŁAĆ ALERT
# ─────────────────────────────────────────────────────────────────────────────

def should_alert(report: dict, cfg: dict) -> tuple[bool, list[str]]:
    """
    Sprawdza czy raport wymaga alertu.
    Zwraca (bool, lista_powodów).
    """
    if not cfg.get("enabled"):
        return False, []

    reasons = []
    # min_errors<=0 alarmowałoby OK porównania (total_errors>=0 zawsze prawda) —
    # traktujemy taki próg jak co najmniej 1 krytyczną rozbieżność.
    try:
        min_errors = int(cfg.get("min_errors", 1) or 1)
    except (TypeError, ValueError):
        min_errors = 1
    if min_errors < 1:
        min_errors = 1

    total_errors = report.get("total_errors", 0)
    if total_errors >= min_errors:
        reasons.append(f"{total_errors} krytycznych rozbieżności")

    # Specjalne przypadki zawsze alertowane
    modules = report.get("modules", {})

    # Zamiany I↔1 w numerach — zawsze krytyczne
    typo_m = modules.get("typo", {})
    if typo_m.get("ok"):
        i1_errors = [f for f in typo_m.get("findings", [])
                     if f.get("severity") == "error" and f.get("category") == "I_vs_1"]
        if i1_errors:
            reasons.append(f"Zamiana I↔1 w numerach dokumentów ({len(i1_errors)} przypadków)")

    # Rozbieżności ilościowe >5%
    tbl_m = modules.get("table", {})
    if tbl_m.get("ok"):
        for item in tbl_m.get("items", []):
            if item.get("status") == "roznica":
                try:
                    from normalizer import normalize_number
                    _qa = normalize_number(item.get("qty_a", "0"))
                    _qb = normalize_number(item.get("qty_b", "0"))
                    qa = float(_qa) if _qa is not None else 0.0
                    qb = float(_qb) if _qb is not None else 0.0
                    # BUGFIX: zmiana 0→X (np. 0→10) była pomijana przez warunek qa>0
                    if (qa > 0 and abs(qa - qb) / qa > 0.05) or (qa == 0 and qb > 0):
                        reasons.append(f"Różnica ilości >5% dla {item.get('ref','?')}: {qa}→{qb}")
                except Exception:
                    pass

    return len(reasons) > 0, reasons


# ─────────────────────────────────────────────────────────────────────────────
# BUDOWANIE EMAILA
# ─────────────────────────────────────────────────────────────────────────────

def build_email_html(report: dict, reasons: list[str],
                     comparison_id: int, username: str,
                     app_url: str = "http://localhost:5000") -> str:
    """Generuje HTML treść emaila alertu."""
    risk = report.get("risk_level", "ok")
    risk_color = RISK_COLORS.get(risk, "#666")
    risk_label = f"{RISK_EMOJI.get(risk, '⚠')} {RISK_LABELS.get(risk, risk.upper())}"
    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    crits_html = "".join(
        f'<tr><td style="padding:6px 10px;border-bottom:1px solid #f0e0e0;color:#c0392b">❌ {r}</td></tr>'
        for r in reasons
    )

    # Zbierz najważniejsze błędy z raportu
    errors_html = ""
    m = report.get("modules", {})
    errors = []
    if m.get("table", {}).get("ok"):
        for it in m["table"].get("items", [])[:5]:
            if it.get("status") == "roznica":
                errors.append(f"[Tabela] {it.get('ref','')}: {'; '.join(it.get('issues',[]))}")
    if m.get("typo", {}).get("ok"):
        for f in m["typo"].get("findings", [])[:3]:
            if f.get("severity") == "error":
                errors.append(f"[I↔1] {f.get('description','')}")
    errors_html = "".join(
        f'<tr style="background:{"#fff8f8" if i%2==0 else "#fff"}">'
        f'<td style="padding:5px 10px;font-family:monospace;font-size:12px;color:#333">{e}</td></tr>'
        for i, e in enumerate(errors)
    )

    # Sekcja "Najważniejsze błędy" jako osobny string — unikamy zagnieżdżonych f-stringów
    # z tymi samymi potrójnymi cudzysłowami (niedozwolone w Python 3.11, wymaga 3.12+).
    errors_block = ""
    if errors_html:
        errors_block = (
            '<div style="padding:16px 28px 0">'
            '<div style="font-size:12px;color:#666;margin-bottom:8px;'
            'text-transform:uppercase;letter-spacing:1px">Najważniejsze błędy</div>'
            '<table style="width:100%;border-collapse:collapse;'
            'border:1px solid #e0e3ec;border-radius:6px;overflow:hidden">'
            f'{errors_html}'
            '</table></div>'
        )

    # Znaczniki typów dokumentów (także wydzielone, aby nie zagnieżdżać f"""...""")
    doc_a_tag = ""
    if report.get("doc_type_a", "") != "auto":
        doc_a_tag = f' <span style="color:#4f8ef7">[{report.get("doc_type_a","")}]</span>'
    doc_b_tag = ""
    if report.get("doc_type_b", "") != "auto":
        doc_b_tag = f' <span style="color:#4fce8e">[{report.get("doc_type_b","")}]</span>'

    # FIX 1: Use APP_VERSION from constants instead of hardcoded "v1.0"
    app_version_str = f"v{APP_VERSION}"

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:#f5f6fa;margin:0;padding:20px">
<div style="max-width:680px;margin:0 auto;background:#fff;border-radius:10px;
  overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.08)">

  <!-- Header -->
  <div style="background:#1e2130;padding:20px 28px">
    <div style="font-size:18px;font-weight:600;color:#4f8ef7">DocCompare</div>
    <div style="font-size:12px;color:#6b7080;margin-top:2px">ACME</div>
  </div>

  <!-- Status banner -->
  <div style="background:{risk_color};padding:14px 28px">
    <div style="font-size:15px;font-weight:600;color:#fff">{risk_label}</div>
    <div style="font-size:12px;color:rgba(255,255,255,.8);margin-top:2px">
      Porównanie #{comparison_id} · {now} · przez {username}
    </div>
  </div>

  <!-- Dokumenty -->
  <div style="padding:20px 28px 0">
    <div style="font-size:12px;color:#666;margin-bottom:10px;text-transform:uppercase;letter-spacing:1px">
      Porównywane dokumenty
    </div>
    <table style="width:100%;border-collapse:collapse">
      <tr>
        <td style="padding:6px 0;font-size:13px"><strong>Dok. A:</strong></td>
        <td style="padding:6px 0;font-size:13px;font-family:monospace;color:#1a1d26">
          {report.get("file_a","—")}{doc_a_tag}
        </td>
      </tr>
      <tr>
        <td style="padding:6px 0;font-size:13px"><strong>Dok. B:</strong></td>
        <td style="padding:6px 0;font-size:13px;font-family:monospace;color:#1a1d26">
          {report.get("file_b","—")}{doc_b_tag}
        </td>
      </tr>
    </table>
  </div>

  <!-- Powody alertu -->
  <div style="padding:16px 28px 0">
    <div style="font-size:12px;color:#666;margin-bottom:8px;text-transform:uppercase;letter-spacing:1px">
      Powody alertu
    </div>
    <table style="width:100%;border-collapse:collapse;background:#fff8f8;
      border:1px solid #f7c5c5;border-radius:6px;overflow:hidden">
      {crits_html}
    </table>
  </div>

  <!-- Błędy -->
  {errors_block}

  <!-- Statystyki -->
  <div style="padding:16px 28px 0">
    <table style="width:100%;border-collapse:collapse">
      <tr>
        <td style="padding:8px;background:#fde8e8;border-radius:6px;text-align:center;width:50%">
          <div style="font-size:24px;font-weight:700;color:#e74c3c">
            {report.get("total_errors",0)}
          </div>
          <div style="font-size:11px;color:#666">Rozbieżności</div>
        </td>
        <td style="width:10px"></td>
        <td style="padding:8px;background:#fef3e2;border-radius:6px;text-align:center;width:50%">
          <div style="font-size:24px;font-weight:700;color:#e67e22">
            {report.get("total_warnings",0)}
          </div>
          <div style="font-size:11px;color:#666">Ostrzeżenia</div>
        </td>
      </tr>
    </table>
  </div>

  <!-- CTA -->
  <div style="padding:20px 28px;text-align:center">
    <a href="{app_url}/history" style="background:#4f8ef7;color:#fff;padding:10px 24px;
      border-radius:7px;text-decoration:none;font-size:13px;font-weight:500">
      Zobacz szczegółowy raport →
    </a>
  </div>

  <!-- Footer -->
  <div style="background:#f8f9fc;padding:14px 28px;border-top:1px solid #e8eaf0">
    <div style="font-size:11px;color:#aaa;text-align:center">
      Ten alert wysłał DocCompare {app_version_str} · ACME ·
      <a href="{app_url}" style="color:#4f8ef7;text-decoration:none">Otwórz aplikację</a>
    </div>
  </div>

</div>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# WYSYŁANIE EMAILA
# ─────────────────────────────────────────────────────────────────────────────

def send_alert(report: dict, comparison_id: int, username: str,
               pdf_bytes: bytes = None,
               app_url: str = "http://localhost:5000",
               db_path: str = "instance/doccompare.db") -> dict:
    """
    Sprawdza czy wysłać alert i wysyła email.
    Zwraca: {"sent": bool, "reason": str, "recipients": list}
    """
    try:
        cfg = get_alert_config(db_path)
    except Exception as e:
        return {"sent": False, "reason": f"Błąd konfiguracji: {str(e)[:200]}", "recipients": []}

    if not cfg["enabled"]:
        return {"sent": False, "reason": "Alerty wyłączone", "recipients": []}

    # FIX 5: Log warning when SMTP host or recipients are missing
    if not cfg["smtp_host"] or not cfg["to_addrs"]:
        logger.warning("Alert not sent: SMTP host or recipient addresses not configured")
        return {"sent": False, "reason": "Brak konfiguracji SMTP", "recipients": []}

    # Nadawca (from_addr lub smtp_user) musi być ustawiony — inaczej sendmail
    # odrzuca pustą kopertę z mało czytelnym błędem SMTP.
    sender = (cfg.get("from_addr") or cfg.get("smtp_user") or "").strip()
    if not sender:
        logger.warning("Alert not sent: sender address (from_addr/smtp_user) not configured")
        return {"sent": False, "reason": "Brak adresu nadawcy (from_addr)", "recipients": []}

    should_send, reasons = should_alert(report, cfg)
    if not should_send:
        return {"sent": False, "reason": "Brak krytycznych problemów", "recipients": []}

    # Zbuduj email
    risk = report.get("risk_level", "ok")
    risk_emoji = RISK_EMOJI.get(risk, "⚠️")

    # FIX 4: Truncate subject to max 200 chars to avoid oversized headers
    raw_subject = (f"{risk_emoji} DocCompare Alert: {report.get('total_errors',0)} błędów "
                   f"— {report.get('file_a','?')} vs {report.get('file_b','?')}")
    subject_part = raw_subject[:200] if len(raw_subject) > 200 else raw_subject

    html_body = build_email_html(report, reasons, comparison_id, username, app_url)

    msg = MIMEMultipart('mixed')
    msg['Subject'] = subject_part
    msg['From'] = sender
    msg['To'] = ", ".join(cfg["to_addrs"])
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))

    # Dołącz PDF jeśli dostępny
    if pdf_bytes:
        att = MIMEApplication(pdf_bytes, _subtype='pdf')
        att.add_header('Content-Disposition', 'attachment',
                       filename=f'raport_{comparison_id}.pdf')
        msg.attach(att)

    # FIX 3: SMTP timeout configurable via cfg key smtp_timeout (default 15)
    timeout = int(cfg.get("smtp_timeout", 15))

    # Wyślij. Port 465 = implicit TLS → SMTP_SSL; 587/25 = STARTTLS.
    try:
        _port = cfg["smtp_port"]
        _smtp_cm = (smtplib.SMTP_SSL(cfg["smtp_host"], _port, timeout=timeout)
                    if _port == 465
                    else smtplib.SMTP(cfg["smtp_host"], _port, timeout=timeout))
        with _smtp_cm as server:
            server.ehlo()
            if _port != 465:
                server.starttls()
            if cfg["smtp_user"] and cfg["smtp_pass"]:
                server.login(cfg["smtp_user"], cfg["smtp_pass"])
            server.sendmail(msg['From'], cfg["to_addrs"], msg.as_string())
        return {"sent": True, "reason": f"Wysłano do: {', '.join(cfg['to_addrs'])}",
                "recipients": cfg["to_addrs"]}
    except Exception as e:
        return {"sent": False, "reason": f"Błąd wysyłki: {str(e)[:200]}", "recipients": []}


def test_smtp(cfg: dict) -> dict:
    """Testuje połączenie SMTP."""
    try:
        _port = cfg["smtp_port"]
        _smtp_cm = (smtplib.SMTP_SSL(cfg["smtp_host"], _port, timeout=10)
                    if _port == 465
                    else smtplib.SMTP(cfg["smtp_host"], _port, timeout=10))
        with _smtp_cm as server:
            server.ehlo()
            if _port != 465:
                server.starttls()
            if cfg["smtp_user"] and cfg["smtp_pass"]:
                server.login(cfg["smtp_user"], cfg["smtp_pass"])
        return {"ok": True, "message": "Połączenie SMTP działa poprawnie"}
    except Exception as e:
        return {"ok": False, "message": str(e)[:200]}
