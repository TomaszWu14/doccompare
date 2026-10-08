"""Testy maszynowego API /api/v1/invoices (integracja TIMPORYE)."""
import os

import app as appmod


def _client():
    return appmod.app.test_client()


def test_routes_registered():
    rules = {r.rule for r in appmod.app.url_map.iter_rules()}
    assert "/api/v1/invoices/upload" in rules
    assert "/api/v1/invoices/batch/<batch_id>" in rules
    assert "/api/v1/invoices/batch/<batch_id>/excel" in rules


def test_no_key_configured_returns_503(monkeypatch):
    monkeypatch.delenv("INTEGRATION_API_KEY", raising=False)
    r = _client().get("/api/v1/invoices/batch/xyz")
    assert r.status_code == 503


def test_wrong_key_returns_401(monkeypatch):
    monkeypatch.setenv("INTEGRATION_API_KEY", "sekret")
    r = _client().get("/api/v1/invoices/batch/xyz",
                      headers={"X-Api-Key": "zly"})
    assert r.status_code == 401


def test_missing_key_header_returns_401(monkeypatch):
    monkeypatch.setenv("INTEGRATION_API_KEY", "sekret")
    r = _client().get("/api/v1/invoices/batch/xyz")
    assert r.status_code == 401


def test_unknown_batch_returns_404(monkeypatch):
    monkeypatch.setenv("INTEGRATION_API_KEY", "sekret")
    r = _client().get("/api/v1/invoices/batch/nie-ma-takiego",
                      headers={"X-Api-Key": "sekret"})
    assert r.status_code == 404


def test_upload_without_files_returns_400(monkeypatch):
    monkeypatch.setenv("INTEGRATION_API_KEY", "sekret")
    r = _client().post("/api/v1/invoices/upload",
                       headers={"X-Api-Key": "sekret"}, data={})
    assert r.status_code == 400
