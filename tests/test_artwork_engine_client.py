"""tests/test_artwork_engine_client.py — artwork_engine_client + artwork-engine/app.py.

Mocks strictly at the _local_compare / urllib.request.urlopen / compare_artworks
boundary — no torch, no live HTTP, no real ARTWORK_ENGINE_URL server (CI has no
ML deps, per 04-RESEARCH.md Pitfall 6).
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import artwork_engine_client as client
from artwork_comparator import ArtworkCompareResult, PageDiff
from artwork_report_engine import build_report_from_comparison


def _sample_page_diff(page_num=1):
    return PageDiff(
        page_num=page_num, pixel_diff_pct=1.5, diff_regions=[], text_diffs=[],
        field_diffs=[], img_a_b64=None, img_b_b64=None, img_a_annot_b64=None,
        img_b_annot_b64=None, img_diff_b64=None,
        img_a_hires_b64="hires-a-b64",
    )


def _sample_result():
    return ArtworkCompareResult(
        file_a="a.pdf", file_b="b.pdf", pages_a=1, pages_b=1,
        page_diffs=[_sample_page_diff()], critical_count=3,
    )


def test_local_fallback_when_url_unset(monkeypatch):
    monkeypatch.delenv("ARTWORK_ENGINE_URL", raising=False)
    sentinel = _sample_result()
    calls = []

    def fake_local(path_a, path_b, **kwargs):
        calls.append((path_a, path_b, kwargs))
        return sentinel

    monkeypatch.setattr(client, "_local_compare", fake_local)
    result = client.compare_artworks("a", "b", use_ai=False)
    assert result is sentinel
    assert len(calls) == 1


def test_remote_path_posts_and_reconstructs(monkeypatch):
    monkeypatch.setenv("ARTWORK_ENGINE_URL", "http://engine.local")
    payload = _sample_result().to_dict(include_images=True)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            import json
            return json.dumps(payload).encode()

    def fake_urlopen(req, timeout=None):
        return FakeResponse()

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    result = client.compare_artworks("a", "b")
    assert isinstance(result, ArtworkCompareResult)
    assert isinstance(result.page_diffs[0], PageDiff)
    assert result.critical_count == 3
    # CR-04: hires page images must survive the HTTP round-trip (to_dict ->
    # JSON -> _reconstruct), not silently collapse to None.
    assert result.page_diffs[0].img_a_hires_b64 == "hires-a-b64"


def test_reconstruct_round_trip_feeds_report():
    reconstructed = client._reconstruct(_sample_result().to_dict(include_images=True))
    report = build_report_from_comparison(reconstructed)
    assert report is not None


def test_engine_error_falls_back(monkeypatch):
    monkeypatch.setenv("ARTWORK_ENGINE_URL", "http://engine.local")
    sentinel = _sample_result()

    def fake_local(path_a, path_b, **kwargs):
        return sentinel

    def raising_urlopen(req, timeout=None):
        import urllib.error
        raise urllib.error.URLError("boom")

    monkeypatch.setattr(client, "_local_compare", fake_local)
    monkeypatch.setattr(client.urllib.request, "urlopen", raising_urlopen)
    result = client.compare_artworks("a", "b")
    assert result is sentinel


def test_engine_http_error_logs_as_error_and_falls_back(monkeypatch, caplog):
    """WR-01: a 4xx from the engine (misconfig/validation bug) must be logged
    louder than a plain timeout/connection error, while still falling back."""
    monkeypatch.setenv("ARTWORK_ENGINE_URL", "http://engine.local")
    sentinel = _sample_result()

    def fake_local(path_a, path_b, **kwargs):
        return sentinel

    def raising_urlopen(req, timeout=None):
        import io
        import urllib.error
        raise urllib.error.HTTPError(
            "http://engine.local/compare", 400, "Bad Request", {}, io.BytesIO(b"")
        )

    monkeypatch.setattr(client, "_local_compare", fake_local)
    monkeypatch.setattr(client.urllib.request, "urlopen", raising_urlopen)
    with caplog.at_level("ERROR", logger=client.logger.name):
        result = client.compare_artworks("a", "b")
    assert result is sentinel
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_handle_compare_rejects_traversal(monkeypatch):
    service_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "artwork-engine", "app.py")
    spec = importlib.util.spec_from_file_location("artwork_engine_service", service_path)
    service = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(service)

    def boom(*a, **kw):
        raise AssertionError("compare_artworks should not be called for a rejected path")

    import artwork_comparator
    monkeypatch.setattr(artwork_comparator, "compare_artworks", boom)

    body, status = service.handle_compare({"path_a": "../../../../etc/passwd", "path_b": "x"})
    assert status == 400


def _load_engine_service():
    service_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "artwork-engine", "app.py")
    spec = importlib.util.spec_from_file_location("artwork_engine_service", service_path)
    service = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(service)
    return service


def test_handle_compare_rejects_non_dict_body():
    """WR-02: a non-dict JSON body (list/scalar) must return a clean 400,
    not an unhandled 500 from AttributeError on body.get()."""
    service = _load_engine_service()
    body, status = service.handle_compare(["not", "a", "dict"])
    assert status == 400
    assert "error" in body


def test_handle_compare_rejects_non_string_paths():
    """WR-02: non-string path_a/path_b must return a clean 400, not a
    TypeError from os.path.realpath()."""
    service = _load_engine_service()
    body, status = service.handle_compare({"path_a": 123, "path_b": {"nested": True}})
    assert status == 400
    assert "error" in body
