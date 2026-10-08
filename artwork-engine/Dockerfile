# ─────────────────────────────────────────────────────────────────────────────
# artwork-engine/Dockerfile — standalone compare_artworks() service
#
# Reuses the already-published ML base image (torch/CV/OCR stack) — same
# pattern as Dockerfile.slim. No heavy image authored here: only the source
# modules this service actually imports (see COPY below) plus the thin Flask
# wrapper (app.py) are added on top.
#
# Override the base image with a build-arg (e.g. pin a SHA instead of latest):
#   docker build -f artwork-engine/Dockerfile \
#     --build-arg ML_BASE_IMAGE=ghcr.io/tomaszwu14/doccompare-ml-base:<sha> .
#
# ponytail: this container shares the uploads/ and instance/ volumes with the
# web/worker process (path-based compare_artworks contract + progress-file
# polling, see docs/KOLEJKOWANIE.md #10) — mount the same volumes at deploy
# time in Coolify. Not a code change; tracked as the manual UAT item in
# 04-VALIDATION.md. Upgrade path if this becomes a real pain point: swap the
# shared-path contract for a content-addressed upload (POST the file bytes
# instead of a path) so the two processes stop sharing a filesystem.
# ─────────────────────────────────────────────────────────────────────────────
ARG ML_BASE_IMAGE=ghcr.io/tomaszwu14/doccompare-ml-base:latest
FROM ${ML_BASE_IMAGE}

WORKDIR /app

# Only the modules this service's handle_compare() call path actually
# imports (traced transitively: artwork_comparator -> api_usage_tracker,
# normalizer, db, barcode_validator). artwork_report_engine.py/ai_validator.py
# are NOT reachable from here — handle_compare() only calls compare_artworks()
# and cmp.to_dict() (confirmed by grep: artwork_comparator has zero references
# to artwork_report_engine) — so they're intentionally left out. Everything
# else the service needs is already in the base image (torch/CV/OCR/flask/gunicorn).
COPY artwork_comparator.py barcode_validator.py db.py constants.py \
     api_usage_tracker.py normalizer.py ./
COPY artwork-engine/app.py ./

EXPOSE 5001

# Container-internal port only — NOT published publicly at the Coolify layer,
# matching the RQ worker's no-public-port posture (T-04-05). Reachable only
# from the web/worker service on the internal Coolify network via
# ARTWORK_ENGINE_URL (e.g. http://artwork-engine:5001).
CMD ["gunicorn", "app:app", \
     "--workers", "2", "--worker-class", "gthread", "--threads", "2", \
     "--timeout", "300", "--bind", "0.0.0.0:5001"]
