"""
blueprints/kolejka.py — Kolejka document automation (Phase 5, KOLEJKA-02..05).

Wave-1 tracer (KOLEJKA-04): POST /api/dostawy/<nr>/stamp auto-stamps an
existing shipping/customs PDF with a text+date overlay (order/MRN, date,
company). Logika PDF-owa jest w pdf_stamper.py — trasy to cienkie opakowanie
(blueprint → core/db, nigdy → app), poza jednym lazy-importem `app` per
funkcja, potrzebnym wyłącznie do reużycia `_ensure_shipment_folder`/
`_save_doc_to_shipment` (istniejący, audytowalny substrat zapisu plików —
patrz 05-PATTERNS.md) bez tworzenia cyklu importu na poziomie modułu.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile

from flask import Blueprint, jsonify, session
from pydantic import BaseModel, Field

from db import get_db
from core.security import require_role, csrf_protect
from core.audit import log_audit as _log_audit
from core.validation import validate_body
import customs_checklist as _customs
import document_assembly as _assembly
import forwarding_order as _fwd
import pdf_stamper as _stamper

logger = logging.getLogger("doccompare")

bp = Blueprint("kolejka", __name__)


# ── Pydantic models ─────────────────────────────────────────────────────────
# Mass-assignment guard: no nr/id field — nr always comes from the URL path.

class StampRequest(BaseModel):
    doc_type: str = Field(min_length=1, max_length=20)
    text: str | None = Field(default=None, max_length=500)


@bp.route("/api/dostawy/<path:nr>/stamp", methods=["POST"])
@require_role("manager")
@csrf_protect
@validate_body(StampRequest)
def api_dostawa_stamp(body: StampRequest, nr):
    """Auto-stamp the latest uploaded document of `body.doc_type` for order
    `nr` with a text+date overlay, recording the stamped copy as a NEW
    shipment_documents row (source='stamped'). The original upload is never
    mutated (T-05-01/T-05-04, 05-01-PLAN.md threat_model)."""
    import app as _app_mod  # lazy: app.py imports this blueprint at module load

    doc_type = re.sub(r'[^\w]', '', body.doc_type.strip().upper())[:20] or "OTHER"

    db = get_db()
    try:
        zlecenie = db.execute(
            "SELECT mir7_number FROM kolejka_zlecenia WHERE nr_zamowienia=?", (nr,)
        ).fetchone()
        if not zlecenie:
            return jsonify({"error": "Nie znaleziono dostawy"}), 404

        doc_row = db.execute(
            "SELECT filename, original_name FROM shipment_documents "
            "WHERE po_number=? AND doc_type=? ORDER BY uploaded_at DESC LIMIT 1",
            (nr, doc_type),
        ).fetchone()
        if not doc_row:
            return jsonify({"error": f"Brak dokumentu typu {doc_type} dla dostawy {nr}"}), 400
    finally:
        db.close()

    folder = _app_mod._ensure_shipment_folder(nr)
    if not folder:
        return jsonify({"error": "Nieprawidłowy numer dostawy"}), 400
    src_path = os.path.join(folder, doc_row["filename"])
    if not os.path.exists(src_path):
        return jsonify({"error": "Plik źródłowy nie istnieje na dysku"}), 400

    stamp_text = (body.text or "").strip()
    if not stamp_text:
        stamp_text = _stamper.default_stamp_text(nr, zlecenie["mir7_number"])

    fd, tmp_dest = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        _stamper.stamp_pdf(src_path, tmp_dest, stamp_text)
        stamped_name = "STAMPED__" + (doc_row["original_name"] or doc_row["filename"])
        ok = _app_mod._save_doc_to_shipment(
            nr, tmp_dest, stamped_name, doc_type, session["user_id"], source="stamped"
        )
    finally:
        try:
            os.remove(tmp_dest)
        except OSError:
            pass

    if not ok:
        return jsonify({"error": "Nie udało się zapisać ostemplowanego pliku"}), 500

    _log_audit("dostawa_stamp", session.get("username"), f"nr={nr} doc_type={doc_type}")
    return jsonify({"ok": True, "filename": stamped_name})


@bp.route("/api/dostawy/<path:nr>/forwarding-order", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_dostawa_forwarding_order(nr):
    """Generate a forwarding order (zlecenie spedycyjne) PDF from a CONFIRMED
    Kolejka order (zgoda_wyplyniecie=1, D-02), record it as a NEW
    shipment_documents row (doc_type='ZS', source='forwarding_order') and
    bridge the order into transport_queue with forwarder_id/sent_to_forwarder_at
    so it becomes visible in the /spedycja portal. Unconfirmed orders are
    rejected with 409 — no confirm escape hatch (T-05-06)."""
    import app as _app_mod  # lazy: app.py imports this blueprint at module load

    uid = session["user_id"]
    db = get_db()
    try:
        zlecenie = db.execute(
            "SELECT nr_zamowienia, supplier_name, rodzaj_transportu, planowane_etd, "
            "data_dostawy, zgoda_wyplyniecie, kontener_id "
            "FROM kolejka_zlecenia WHERE nr_zamowienia=?", (nr,)
        ).fetchone()
        if not zlecenie:
            return jsonify({"error": "Nie znaleziono dostawy"}), 404
        zlecenie = dict(zlecenie)

        kontener = None
        if zlecenie.get("kontener_id"):
            kr = db.execute(
                "SELECT numer_kontenera, typ_transportu, forwarder_id, etd_plan, eta, "
                "origin_port, dest_port FROM kolejka_kontenery WHERE id=?",
                (zlecenie["kontener_id"],)
            ).fetchone()
            kontener = dict(kr) if kr else None

        forwarder_id = (kontener or {}).get("forwarder_id")
        zlecenie["forwarder_id"] = forwarder_id

        missing = _fwd.required_fields(zlecenie)
        if missing:
            return jsonify({
                "error": "Zlecenie niepotwierdzone lub brak spedytora — nie można "
                         "wygenerować zlecenia spedycyjnego",
                "missing": missing,
            }), 409

        forwarder = db.execute(
            "SELECT name, company, email, phone, address FROM freight_forwarders WHERE id=?",
            (forwarder_id,)
        ).fetchone()
        forwarder = dict(forwarder) if forwarder else None

        data = _fwd.build_forwarding_order_data(zlecenie, kontener, forwarder)
        pdf_bytes = _fwd.render_forwarding_order_pdf(data)
    finally:
        db.close()

    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pdf_bytes)
        original_name = "zlecenie_spedycyjne.pdf"
        ok = _app_mod._save_doc_to_shipment(
            nr, tmp_path, original_name, "ZS", uid, source="forwarding_order"
        )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if not ok:
        return jsonify({"error": "Nie udało się zapisać zlecenia spedycyjnego"}), 500

    # Bridge do /spedycja: wpis w transport_queue + przypisanie spedytora.
    # Reużycie istniejącego mechanizmu widoczności (forwarder_id +
    # sent_to_forwarder_at, patrz app.py api_transport_queue_update) — bez
    # nowego portalu ani roli (D-02).
    db = get_db()
    try:
        _app_mod._ensure_transport_queue_for_delivery(db, nr, uid)
        db.execute(
            "UPDATE transport_queue SET forwarder_id=?, "
            "sent_to_forwarder_at=CASE WHEN sent_to_forwarder_at IS NULL OR "
            "sent_to_forwarder_at='' THEN datetime('now') ELSE sent_to_forwarder_at END, "
            "updated_at=datetime('now') WHERE po_numbers LIKE ?",
            (forwarder_id, f"%{nr}%"),
        )
        db.commit()
    finally:
        db.close()

    _log_audit("dostawa_forwarding_order", session.get("username"),
               f"nr={nr} forwarder_id={forwarder_id}")
    return jsonify({"ok": True, "filename": original_name})


@bp.route("/api/dostawy/<path:nr>/assemble-customs-set", methods=["POST"])
@require_role("manager")
@csrf_protect
def api_dostawa_assemble_customs_set(nr):
    """Auto-assemble the customs document set (KOLEJKA-05): for each required
    customs doc_type (customs_checklist.CUSTOMS_DOC_TYPES — the same source of
    truth as the hard gate) pick the LATEST uploaded shipment_documents file,
    merge them into one combined PDF and record it as a NEW shipment_documents
    row (doc_type='SET', source='assembled') in the per-PO folder. Originals
    are never mutated (T-05-15/T-05-18); zero readable documents → 400 with no
    partial artifact."""
    import app as _app_mod  # lazy: app.py imports this blueprint at module load

    uid = session["user_id"]
    db = get_db()
    try:
        zlecenie = db.execute(
            "SELECT id FROM kolejka_zlecenia WHERE nr_zamowienia=?", (nr,)
        ).fetchone()
        if not zlecenie:
            return jsonify({"error": "Nie znaleziono dostawy"}), 404

        latest = []  # (doc_type, filename) — najnowszy plik per wymagany typ
        for doc_type in _customs.CUSTOMS_DOC_TYPES:
            row = db.execute(
                "SELECT filename FROM shipment_documents "
                "WHERE po_number=? AND doc_type=? ORDER BY uploaded_at DESC LIMIT 1",
                (nr, doc_type),
            ).fetchone()
            if row:
                latest.append((doc_type, row["filename"]))
    finally:
        db.close()

    folder = _app_mod._ensure_shipment_folder(nr)
    if not folder:
        return jsonify({"error": "Nieprawidłowy numer dostawy"}), 400

    paths, included = [], []
    for doc_type, filename in latest:
        p = os.path.join(folder, filename)
        if os.path.exists(p):
            paths.append(p)
            included.append(doc_type)
    if not paths:
        return jsonify({"error": "Brak dokumentów do złożenia zestawu"}), 400

    try:
        pdf_bytes = _assembly.assemble_pdf(paths)
    except ValueError:
        return jsonify({"error": "Brak dokumentów do złożenia zestawu"}), 400

    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pdf_bytes)
        original_name = "zestaw_celny.pdf"
        ok = _app_mod._save_doc_to_shipment(
            nr, tmp_path, original_name, "SET", uid, source="assembled"
        )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if not ok:
        return jsonify({"error": "Nie udało się zapisać zestawu celnego"}), 500

    _log_audit("dostawa_assemble_set", session.get("username"),
               f"nr={nr} included={','.join(included)}")
    return jsonify({"ok": True, "filename": original_name, "included": included})
