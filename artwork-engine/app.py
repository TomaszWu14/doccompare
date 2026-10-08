"""artwork-engine/app.py — thin Flask service exposing compare_artworks() over HTTP.

Standalone service (separate process/container) that does the CV-heavy work
`artwork_comparator.compare_artworks()` performs today in the web/worker process.
Deliberately plain Flask, not FastAPI — one route does not justify a second
framework in this project.

Trust boundary: `path_a`/`path_b` in the request body are filesystem paths that
must resolve inside the shared uploads/instance volume (ARTWORK_ENGINE_ROOT) —
this is the one genuinely new input surface this phase introduces (T-04-01).
Binds to 127.0.0.1 by default (internal-only, same posture as the RQ worker;
override with ARTWORK_ENGINE_HOST only for an explicit, deliberate deploy change).
"""

import os

from flask import Flask, jsonify, request

# Kwargs mirrored from artwork_comparator.compare_artworks()'s signature
# (artwork_comparator.py:7058-7067).
_COMPARE_KWARGS = (
    "use_ai", "use_ai_sections", "max_pages",
    "page_a", "page_b", "profile_id", "field_overrides_b",
    "profile_perf", "progress_id",
)


def _allowed_roots() -> list:
    """Roots a request path_a/path_b must resolve inside of."""
    custom = os.environ.get("ARTWORK_ENGINE_ROOT", "").strip()
    if custom:
        return [os.path.realpath(custom)]
    base = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(base)
    return [
        os.path.realpath(os.path.join(repo_root, "uploads")),
        os.path.realpath(os.path.join(repo_root, "instance")),
    ]


def _path_allowed(path: str) -> bool:
    real = os.path.realpath(path)
    return any(real == root or real.startswith(root + os.sep) for root in _allowed_roots())


def handle_compare(body: dict) -> tuple:
    """Validates paths and runs the comparison. Returns (response_dict, status_code).

    Pure function (no Flask request/response objects) so it is unit-testable
    without a running server.
    """
    if not isinstance(body, dict):
        return {"error": "request body must be a JSON object"}, 400
    path_a = body.get("path_a")
    path_b = body.get("path_b")
    if not path_a or not path_b:
        return {"error": "path_a and path_b are required"}, 400
    if not isinstance(path_a, str) or not isinstance(path_b, str):
        return {"error": "path_a and path_b must be strings"}, 400
    for label, path in (("path_a", path_a), ("path_b", path_b)):
        if not _path_allowed(path):
            return {"error": f"{label} resolves outside the allowed root"}, 400

    kwargs = {k: body[k] for k in _COMPARE_KWARGS if k in body}

    from artwork_comparator import compare_artworks  # lazy — keeps module import light for tests

    cmp = compare_artworks(path_a, path_b, **kwargs)
    return cmp.to_dict(include_images=True), 200


app = Flask(__name__)


@app.route("/compare", methods=["POST"])
def compare_route():
    body = request.get_json(force=True) or {}
    result, status = handle_compare(body)
    return jsonify(result), status


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    host = os.environ.get("ARTWORK_ENGINE_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5001"))
    app.run(host=host, port=port)
