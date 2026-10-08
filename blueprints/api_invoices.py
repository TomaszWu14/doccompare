"""
blueprints/api_invoices.py — maszynowe API Faktury → Excel dla integracji
z TIMPORYE (server-to-server, auth przez X-Api-Key, bez sesji/CSRF).

Flow: TIMPORYE wgrywa PDF-y → Compare tnie/ekstrahuje (istniejący pipeline
invoice_*) → operator potwierdza w UI Compare → TIMPORYE polluje status
batcha → gdy ready, pobiera gotowy Excel. Blueprint → invoice_routes/core/db,
nigdy → app.
"""
from __future__ import annotations

import io

from flask import Blueprint, jsonify, request, send_file

from core.security import require_api_key
from core.audit import log_audit
from constants import InvoiceJobStatus, INVOICE_LIKE_KINDS
from db import get_db
import invoice_jobs as ij
import invoice_excel_export as excel

bp = Blueprint("api_invoices", __name__, url_prefix="/api/v1/invoices")

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@bp.route("/upload", methods=["POST"])
@require_api_key
def api_upload():
    """Multipart: pole `pdf` (1..n plików), opcjonalnie `supplier_code`.
    Zwraca batch_id do pollowania."""
    from invoice_routes import ingest_files, start_processing  # lazy: unika cyklu przy imporcie app
    files = request.files.getlist("pdf")
    if not files or all(not f or not f.filename for f in files):
        return jsonify({"error": "Brak plików PDF (pole multipart 'pdf')"}), 400
    forced_supplier = (request.form.get("supplier_code") or "").strip()
    db = get_db()
    try:
        batch_id, jobs, pending_jids = ingest_files(db, files, forced_supplier)
    finally:
        db.close()
    start_processing(pending_jids)
    log_audit("api_invoice_upload", "api:timporye",
              detail=f"batch {batch_id}: {len(jobs)} dokument(ów)")
    return jsonify({"batch_id": batch_id, "jobs": jobs}), 201


@bp.route("/batch/<batch_id>", methods=["GET"])
@require_api_key
def api_batch_status(batch_id):
    """Status batcha: per-job status + flaga `ready` (wszystkie dokumenty
    faktura-podobne potwierdzone przez operatora w UI Compare)."""
    db = get_db()
    try:
        ij.ensure_invoice_tables(db)   # tabele powstają leniwie (świeża baza)
        jobs = ij.list_jobs(db, batch_id)
    finally:
        db.close()
    if not jobs:
        return jsonify({"error": "Nie znaleziono batcha"}), 404
    invoice_like = [j for j in jobs if (j.get("doc_kind") or "invoice") in INVOICE_LIKE_KINDS]
    confirmed = [j for j in invoice_like
                 if j.get("status") in (InvoiceJobStatus.CONFIRMED, InvoiceJobStatus.EXPORTED)]
    errors = [j for j in invoice_like if j.get("status") == InvoiceJobStatus.ERROR]
    return jsonify({
        "batch_id": batch_id,
        "ready": bool(invoice_like) and len(confirmed) == len(invoice_like),
        "total": len(invoice_like),
        "confirmed": len(confirmed),
        "errors": len(errors),
        "jobs": [{"id": j["id"], "filename": j.get("filename"),
                  "doc_kind": j.get("doc_kind"), "status": j.get("status"),
                  "invoice_number": j.get("invoice_number"),
                  "error": j.get("error")} for j in jobs],
    })


@bp.route("/batch/<batch_id>/excel", methods=["GET"])
@require_api_key
def api_batch_excel(batch_id):
    """Excel z potwierdzonych faktur batcha. 409 dopóki batch nie jest ready
    (chyba że ?partial=1 — wtedy to, co już potwierdzone)."""
    db = get_db()
    try:
        ij.ensure_invoice_tables(db)
        jobs = ij.list_jobs(db, batch_id)
        if not jobs:
            return jsonify({"error": "Nie znaleziono batcha"}), 404
        invoice_like = [j for j in jobs
                        if (j.get("doc_kind") or "invoice") in INVOICE_LIKE_KINDS]
        done = [j for j in invoice_like
                if j.get("status") in (InvoiceJobStatus.CONFIRMED, InvoiceJobStatus.EXPORTED)]
        if not done or (len(done) < len(invoice_like)
                        and request.args.get("partial") != "1"):
            return jsonify({"error": "Batch niegotowy — nie wszystkie faktury potwierdzone",
                            "confirmed": len(done), "total": len(invoice_like)}), 409
        rows = []
        for job in done:
            for it in ij.get_items(db, job["id"]):
                it = dict(it)
                it["invoice_number"] = job["invoice_number"]
                rows.append(it)
    finally:
        db.close()
    log_audit("api_invoice_export", "api:timporye",
              detail=f"batch {batch_id}: {len(rows)} pozycji")
    wb = excel.build_workbook(rows)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f"faktury_{batch_id}.xlsx", mimetype=_XLSX_MIME)
