"""tests/test_app_boots.py — smoke test: aplikacja Flask buduje się i rejestruje trasy.

Bramka CI uruchamia tylko `compileall` + `pytest`. Bez tego testu błąd w
`create_app()` lub w rejestracji blueprintu (refaktor rozbicia app.py) przeszedłby
przez bramkę — wykryłby go dopiero deploy, już PO auto-merge do main. Ten test
konstruuje aplikację i sprawdza, że kluczowe ścieżki nadal istnieją.

UWAGA: sprawdzamy ŚCIEŻKI URL (stabilne), nie nazwy endpointów — po rozbiciu na
blueprinty nazwy dostają prefiks (np. `auth.login`), ale ścieżki się nie zmieniają.

Test pomija się, gdy Flask nie jest zainstalowany (np. minimalne środowisko).
"""
import os
import tempfile

import pytest

pytest.importorskip("flask")

# Środowisko MUSI być ustawione przed importem `app` — moduł robi inicjalizację
# (tworzy tabele, użytkowników) na poziomie modułu. Izolujemy bazę w temp.
_TMP = tempfile.mkdtemp(prefix="doccompare_boot_")
os.environ.setdefault("SECRET_KEY", "test-boot-secret")
os.environ["SQLITE_PATH"] = os.path.join(_TMP, "boot.db")
os.environ.pop("DATABASE_URL", None)

import app as _app_mod  # noqa: E402

# Kluczowe ścieżki, które muszą istnieć niezależnie od struktury blueprintów.
# Po przeniesieniu danej grupy tras do blueprintu jej ścieżki nadal tu są —
# jeśli rejestracja się urwie, znikną i test padnie.
_KEY_PATHS = [
    "/login", "/logout", "/",
    "/dostawy", "/materials", "/suppliers", "/admin",
    "/tracking", "/kpi", "/api/compare",
]


def test_app_constructs():
    assert _app_mod.app is not None


def test_routes_registered():
    rules = list(_app_mod.app.url_map.iter_rules())
    # Aplikacja ma setki tras; pilnujemy, że rejestracja nie urwała się w połowie.
    assert len(rules) > 250, f"zarejestrowano tylko {len(rules)} tras"


@pytest.mark.parametrize("path", _KEY_PATHS)
def test_key_path_exists(path):
    paths = {r.rule for r in _app_mod.app.url_map.iter_rules()}
    assert path in paths, f"brak trasy {path} — rejestracja blueprintu mogła się nie powieść"


def test_no_duplicate_endpoint_names():
    # Dwa blueprinty z funkcją o tej samej nazwie endpointu = błąd rejestracji.
    seen = {}
    for r in _app_mod.app.url_map.iter_rules():
        seen.setdefault(r.endpoint, 0)
        seen[r.endpoint] += 1
    dups = {e: n for e, n in seen.items() if n > 1 and e != "static"}
    assert not dups, f"zduplikowane nazwy endpointów: {dups}"


def test_artwork_cache_cleanup_handles_3tuple_entries():
    # Regresja: wpis _artwork_cache to 3-krotka (result, inserted_at, owner_id),
    # a pętla czyszcząca (_cache_cleanup) rozpakowywała go na 2-krotkę
    # → ValueError zabijał po cichu wątek daemon przy pierwszym niepustym
    # przebiegu. Sprawdzamy kształt wpisu i że wyrażenie eksmisji nie rzuca.
    import time as _t
    _app_mod._cache_put("regr-cleanup-key", {"x": 1}, owner_id=42)
    entry = _app_mod._artwork_cache["regr-cleanup-key"]
    assert len(entry) == 3 and entry[2] == 42
    now = _t.time()
    # Identyczne wyrażenie jak w _cache_cleanup — nie może rzucić ValueError.
    expired = [k for k, v in list(_app_mod._artwork_cache.items())
               if (now - v[1]) >= _app_mod._CACHE_TTL]
    assert isinstance(expired, list)
    _app_mod._cache_evict("regr-cleanup-key")
    assert "regr-cleanup-key" not in _app_mod._artwork_cache
