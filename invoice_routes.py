"""Blueprint modułu Faktury -> Excel: upload, coverage, master, review, pdf, export."""

import os
import io
import threading
import uuid

from flask import (Blueprint, request, render_template, redirect, url_for,
                    send_file, jsonify, abort, session)
from werkzeug.utils import secure_filename

from core.security import login_required, require_role, csrf_protect
from core.audit import log_audit
from constants import InvoiceJobStatus, DocKind, INVOICE_LIKE_KINDS
import doc_splitter
from db import get_db
import invoice_jobs as ij
import invoice_pipeline as pipeline
import material_master
import invoice_excel_export as excel
from invoice_mapper import resolve_chosen_refs
from supplier_profiles import get_all_suppliers, detect_supplier

invoice_bp = Blueprint("invoices", __name__, url_prefix="/invoices")
UPLOAD_DIR = "uploads"


def _process_async(job_id):
    # własne połączenie w wątku (get_db per-thread wg abstrakcji db.py);
    # brak kontekstu żądania => trzeba jawnie zamknąć, inaczej pula się wyczerpie
    db = get_db()
    try:
        pipeline.process_job(db, job_id)
    finally:
        db.close()


@invoice_bp.route("/")
@login_required
@require_role("user")
def home():
    return render_template("invoices_home.html", suppliers=get_all_suppliers())


def _split_or_whole(path):
    """Części z doc_splittera; gdy cięcie padnie (uszkodzony/zaszyfrowany PDF) —
    cały plik jako jedna część z klasyfikacją starą metodą. Nie blokuje uploadu."""
    try:
        parts = doc_splitter.split_pdf(path)
        if parts:
            return parts
    except Exception:
        pass
    from packing_list_extractor import first_pages_text
    text = first_pages_text(path)
    kind = (DocKind.PACKING_LIST if doc_splitter.classify_page(text) == DocKind.PACKING_LIST
            else DocKind.INVOICE)
    return [{"kind": kind, "page_from": 1, "page_to": 1, "out_path": path}]


def ingest_files(db, files, forced_supplier=""):
    """Wspólny ingest batcha (UI upload + API /api/v1): zapis plików, split,
    create_job per część. Zwraca (batch_id, jobs, pending_jids); wątki
    przetwarzania startuje wołający — dopiero gdy CAŁY batch jest w bazie."""
    batch_id = uuid.uuid4().hex[:12]
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    jobs, pending_jids = [], []
    for idx, f in enumerate(files):
        if not f or not f.filename:
            continue
        fn = secure_filename(f.filename)
        path = os.path.join(UPLOAD_DIR, f"{batch_id}_{idx}_{fn}")
        f.save(path)
        parts = _split_or_whole(path)
        # Forced supplier tylko gdy plik daje DOKŁADNIE jedną fakturę/proformę —
        # przy wielu dostawcach w jednym pliku (komplet kontenerowy) auto-detekcja per dokument.
        invoice_parts = sum(1 for p in parts if p["kind"] in INVOICE_LIKE_KINDS)
        for part in parts:
            kind = part["kind"]
            sup = forced_supplier if (kind in INVOICE_LIKE_KINDS
                                       and invoice_parts == 1) else ""
            jid = ij.create_job(db, batch_id, fn, part["out_path"], sup,
                                doc_kind=kind, source_file=path,
                                page_from=part["page_from"], page_to=part["page_to"])
            if kind == DocKind.PACKING_LIST:
                ij.update_job(db, jid, status=InvoiceJobStatus.PACKING_LIST)
            elif kind == DocKind.OTHER:
                ij.update_job(db, jid, status=InvoiceJobStatus.IGNORED)
            else:
                pending_jids.append(jid)
            jobs.append({"id": jid, "filename": fn,
                         "doc_kind": getattr(kind, "value", kind)})
    return batch_id, jobs, pending_jids


def start_processing(pending_jids):
    """Odpal wątki przetwarzania dla jobów batcha."""
    for jid in pending_jids:
        threading.Thread(target=_process_async, args=(jid,), daemon=True).start()


@invoice_bp.route("/upload", methods=["POST"])
@login_required
@require_role("user")
@csrf_protect
def upload():
    db = get_db()
    files = request.files.getlist("pdf")
    forced_supplier = (request.form.get("supplier_code") or "").strip()
    batch_id, _jobs, pending_jids = ingest_files(db, files, forced_supplier)
    start_processing(pending_jids)
    log_audit("invoice_upload", session.get("username"),
              detail=f"batch {batch_id}: {len(files)} plik(ów)")
    return redirect(url_for("invoices.coverage", batch_id=batch_id))


@invoice_bp.route("/coverage")
@login_required
@require_role("user")
def coverage():
    db = get_db()
    batch_id = request.args.get("batch_id", "")
    suppliers = get_all_suppliers()
    jobs = ij.list_jobs(db, batch_id) if batch_id else []
    # ponytail: jedno grupujące zapytanie zamiast N+1 per dostawca
    stats = {r["supplier_code"]: dict(r) for r in db.execute(
        "SELECT supplier_code, COUNT(*) AS n, MAX(created_at) AS last_at "
        "FROM invoice_jobs GROUP BY supplier_code").fetchall()}
    for s in suppliers:
        st = stats.get(s.get("code"), {})
        s["invoice_count"] = st.get("n", 0)
        s["last_invoice_at"] = st.get("last_at", "")
    # Gating Draft SAD: link aktywny tylko gdy WSZYSTKIE dokumenty faktura-podobne
    # (CI/proforma) batcha są zatwierdzone (por. sad.collect_batch).
    invoice_like = [j for j in jobs if (j.get("doc_kind") or "invoice") in INVOICE_LIKE_KINDS]
    sad_total = len(invoice_like)
    sad_confirmed = sum(1 for j in invoice_like if j.get("status") == InvoiceJobStatus.CONFIRMED)
    return render_template("invoice_coverage.html",
                            suppliers=suppliers, jobs=jobs, batch_id=batch_id,
                            sad_total=sad_total, sad_confirmed=sad_confirmed)


@invoice_bp.route("/master")
@login_required
@require_role("user")
def master():
    db = get_db()
    material_master.ensure_table(db)  # tabela powstaje leniwie — utwórz, jeśli jeszcze nie ma
    q = request.args.get("q", "")
    if q:
        like = f"%{q}%"
        rows = db.execute(
            "SELECT * FROM material_master WHERE ref_code LIKE ? OR opis_pl LIKE ? "
            "ORDER BY ref_code LIMIT 200", (like, like)).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM material_master ORDER BY ref_code LIMIT 200").fetchall()
    return render_template("invoice_master.html", rows=[dict(r) for r in rows], q=q)


@invoice_bp.route("/review/<int:job_id>", methods=["GET"])
@login_required
@require_role("user")
def review(job_id):
    db = get_db()
    job = ij.get_job(db, job_id)
    if job is None:
        abort(404)
    items = ij.get_items(db, job_id)
    return render_template("invoice_review.html", job=job, items=items)


@invoice_bp.route("/review/<int:job_id>", methods=["POST"])
@login_required
@require_role("user")
@csrf_protect
def review_save(job_id):
    db = get_db()
    payload = request.get_json(force=True)
    ij.save_items(db, job_id, payload.get("items", []))
    resolve_chosen_refs(db, job_id)
    if payload.get("confirm"):
        ok, msg = pipeline.confirm_job(db, job_id)
        if ok:
            log_audit("invoice_confirm", session.get("username"), detail=f"job {job_id}")
        return jsonify({"ok": ok, "msg": msg})
    return jsonify({"ok": True, "msg": "Zapisano"})


@invoice_bp.route("/reprocess/<int:job_id>", methods=["POST"])
@login_required
@require_role("user")
@csrf_protect
def reprocess(job_id):
    db = get_db()
    job = ij.get_job(db, job_id)
    if not job:
        abort(404)
    if job.get("status") != InvoiceJobStatus.ERROR:
        return jsonify({"ok": False, "msg": "Tylko dokumenty z błędem można ponowić"}), 409
    ij.update_job(db, job_id, status=InvoiceJobStatus.UPLOADED, error="")
    threading.Thread(target=_process_async, args=(job_id,), daemon=True).start()
    log_audit("invoice_reprocess", session.get("username"), detail=f"job {job_id}")
    return jsonify({"ok": True, "msg": "Ponowiono przetwarzanie"})


@invoice_bp.route("/pdf/<int:job_id>")
@login_required
@require_role("user")
def pdf(job_id):
    db = get_db()
    job = ij.get_job(db, job_id)
    if not job:
        return "Not found", 404
    path = os.path.abspath(job["pdf_path"])
    base = os.path.abspath(UPLOAD_DIR)
    if os.path.commonpath([base, path]) != base:   # ochrona przed path traversal
        return "Forbidden", 403
    return send_file(path)


@invoice_bp.route("/export/<batch_id>")
@login_required
@require_role("user")
def export(batch_id):
    db = get_db()
    job_id = request.args.get("job_id", type=int)
    jobs = [ij.get_job(db, job_id)] if job_id else ij.list_jobs(db, batch_id)
    rows = []
    for job in jobs:
        if not job or job["status"] != InvoiceJobStatus.CONFIRMED:
            continue
        for it in ij.get_items(db, job["id"]):
            it = dict(it)
            it["invoice_number"] = job["invoice_number"]
            rows.append(it)
    log_audit("invoice_export", session.get("username"),
              detail=f"batch {batch_id}: {len(rows)} pozycji")
    wb = excel.build_workbook(rows)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"faktury_{batch_id}.xlsx",
                      mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@invoice_bp.route("/sad/<batch_id>")
@login_required
@require_role("user")
def sad_export(batch_id):
    db = get_db()
    import sad_draft_export as sad
    try:
        header, positions = sad.collect_batch(db, batch_id)
    except ValueError as e:
        return str(e), 409
    log_audit("invoice_sad_export", session.get("username"),
              detail=f"batch {batch_id}: {len(positions)} pozycji")
    wb = sad.build_sad_workbook(header, positions)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f"draft_sad_{batch_id}.xlsx",
                     mimetype=sad._XLSX_MIME)
