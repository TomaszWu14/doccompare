"""tests/test_artwork_batch_concurrency.py — run_artwork_batch() ThreadPoolExecutor concurrency.

Mocks compare_artworks at the artwork_engine_client boundary (the same seam
run_artwork_batch imports from) — no torch, no live Claude calls, no real HTTP
(per 04-RESEARCH.md Pitfall 6). Verifies three invariants of the AIPERF-01
refactor:
  - pairs run concurrently under the bounded pool (max in-flight >= 2)
  - results yield in original pair index order despite out-of-order completion
  - the api_usage budget gate is reached once per pair (no bypass/hoist)
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import api_usage_tracker
import artwork_engine_client
from artwork_batch_processor import run_artwork_batch
from artwork_comparator import ArtworkCompareResult, PageDiff


def _make_pairs(n):
    return [
        {"file_a": f"pair{i}_a.pdf", "file_b": f"pair{i}_b.pdf", "prefix": f"pair{i}"}
        for i in range(n)
    ]


def _touch_upload_files(upload_dir, uid, pairs):
    """Create empty placeholder files matching _uploaded_artwork_path's naming
    convention ({uid}_aw_{side}_{filename}) so run_artwork_batch's existence
    check passes without touching real PDF content."""
    for pair in pairs:
        for side, fname in (("a", pair["file_a"]), ("b", pair["file_b"])):
            path = os.path.join(upload_dir, f"{uid}_aw_{side}_{fname}")
            with open(path, "wb") as fh:
                fh.write(b"%PDF-1.4 fake")


def _sample_cmp_result():
    pd = PageDiff(
        page_num=1, pixel_diff_pct=0.0, diff_regions=[], text_diffs=[],
        field_diffs=[], img_a_b64=None, img_b_b64=None, img_a_annot_b64=None,
        img_b_annot_b64=None, img_diff_b64=None,
    )
    return ArtworkCompareResult(
        file_a="a.pdf", file_b="b.pdf", pages_a=1, pages_b=1,
        page_diffs=[pd], critical_count=0,
    )


def test_pairs_run_concurrently(tmp_path, monkeypatch):
    n = 4
    pairs = _make_pairs(n)
    _touch_upload_files(str(tmp_path), 1, pairs)

    lock = threading.Lock()
    state = {"current": 0, "max": 0}

    def fake_compare(path_a, path_b, **kwargs):
        with lock:
            state["current"] += 1
            state["max"] = max(state["max"], state["current"])
        time.sleep(0.05)
        with lock:
            state["current"] -= 1
        return _sample_cmp_result()

    monkeypatch.setattr(artwork_engine_client, "compare_artworks", fake_compare)

    results = list(run_artwork_batch(pairs, str(tmp_path), 1, use_ai=False))

    assert len(results) == n
    assert all(r["status"] == "done" for r in results), results
    # A strictly sequential loop could never observe more than 1 in-flight.
    assert state["max"] >= 2, f"expected >=2 concurrent compares, observed max={state['max']}"


def test_yield_order_preserved(tmp_path, monkeypatch):
    n = 4
    pairs = _make_pairs(n)
    _touch_upload_files(str(tmp_path), 2, pairs)

    def fake_compare(path_a, path_b, **kwargs):
        # Pair 0 finishes LAST — if run_artwork_batch yielded completion order
        # instead of original index order, index 0 would land at the end.
        time.sleep(0.15 if "pair0_a" in path_a else 0.01)
        return _sample_cmp_result()

    monkeypatch.setattr(artwork_engine_client, "compare_artworks", fake_compare)

    results = list(run_artwork_batch(pairs, str(tmp_path), 2, use_ai=False))

    assert [r["index"] for r in results] == list(range(n))
    assert all(r["status"] == "done" for r in results), results


def test_streams_incrementally_not_after_full_batch(tmp_path, monkeypatch):
    """CR-01 regression guard: the previous buffering implementation only
    yielded after ALL futures completed. Drive the generator with next()
    directly and prove the first result is observable while other pairs are
    still deterministically parked (threading.Event, no sleep-based timing)."""
    n = 4
    pairs = _make_pairs(n)
    _touch_upload_files(str(tmp_path), 5, pairs)

    release_others = threading.Event()
    lock = threading.Lock()
    other_done = {"count": 0}

    def fake_compare(path_a, path_b, **kwargs):
        if "pair0_a" in path_a:
            return _sample_cmp_result()
        # Pairs 1-3 block here until the test has already consumed pair 0's
        # result — if the batch buffered everything before yielding, next()
        # below would block until this wait() times out/returns too.
        release_others.wait(timeout=5)
        with lock:
            other_done["count"] += 1
        return _sample_cmp_result()

    monkeypatch.setattr(artwork_engine_client, "compare_artworks", fake_compare)

    gen = run_artwork_batch(pairs, str(tmp_path), 5, use_ai=False)
    first = next(gen)
    assert first["index"] == 0
    with lock:
        assert other_done["count"] == 0, "batch buffered all results before yielding the first"

    release_others.set()
    rest = list(gen)
    assert len(rest) == n - 1
    assert [r["index"] for r in rest] == [1, 2, 3]


def test_budget_gate_reached_per_pair(tmp_path, monkeypatch):
    n = 3
    pairs = _make_pairs(n)
    _touch_upload_files(str(tmp_path), 3, pairs)

    lock = threading.Lock()
    calls = {"count": 0}

    def fake_budget_check():
        with lock:
            calls["count"] += 1
        return True

    def fake_compare(path_a, path_b, **kwargs):
        # Stand-in for compare_artworks: its Claude call sites all fetch the key
        # via artwork_comparator._get_api_key_artwork(), which is where the
        # budget gate lives (see test_budget_gate_blocks_api_key below).
        import artwork_comparator
        artwork_comparator._get_api_key_artwork()
        return _sample_cmp_result()

    monkeypatch.setattr(api_usage_tracker, "budget_allows_optional_ai", fake_budget_check)
    monkeypatch.setattr(artwork_engine_client, "compare_artworks", fake_compare)

    results = list(run_artwork_batch(pairs, str(tmp_path), 3, use_ai=False))

    assert len(results) == n
    assert calls["count"] == n, f"expected budget gate reached {n} times, got {calls['count']}"


def test_budget_gate_blocks_api_key(monkeypatch):
    """Budget over -> _get_api_key_artwork() returns "" so every optional Claude
    call site in artwork_comparator degrades to its non-AI fallback."""
    import artwork_comparator

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")

    monkeypatch.setattr(api_usage_tracker, "budget_allows_optional_ai", lambda: False)
    assert artwork_comparator._get_api_key_artwork() == ""

    monkeypatch.setattr(api_usage_tracker, "budget_allows_optional_ai", lambda: True)
    assert artwork_comparator._get_api_key_artwork() == "sk-test-123"
