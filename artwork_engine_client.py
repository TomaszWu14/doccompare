"""artwork_engine_client.py — client wrapper for the artwork-engine HTTP service.

ZASADA BEZPIECZEŃSTWA — brak twardej zależności od artwork-engine (mirrors jobs.py's
REDIS_URL pattern):
- jeśli `ARTWORK_ENGINE_URL` jest ustawiony → POST do <url>/compare, wynik
  rekonstruowany z JSON z powrotem do ArtworkCompareResult/PageDiff,
- w przeciwnym razie (lub przy błędzie/timeoucie) → liczy w tym samym procesie,
  wołając artwork_comparator.compare_artworks() bezpośrednio (DOTYCHCZASOWE
  zachowanie — dev/CI działają bez zmian, dopóki nie ustawisz tej zmiennej).

Import na poziomie modułu jest wyłącznie stdlib + artwork_comparator — a
artwork_comparator ładuje torch/cv2 leniwie wewnątrz swoich funkcji, więc samo
`import artwork_engine_client` nie ciągnie ciężkich zależności CV.
"""

import os
import json
import logging
import urllib.error
import urllib.request

from artwork_comparator import ArtworkCompareResult, PageDiff, compare_artworks as _local_compare

logger = logging.getLogger(__name__)


def engine_url_configured() -> bool:
    """True, jeśli skonfigurowano zdalny artwork-engine."""
    return bool(os.environ.get("ARTWORK_ENGINE_URL", "").strip())


def _reconstruct(data: dict) -> ArtworkCompareResult:
    """Odtwarza ArtworkCompareResult (z zagnieżdżonymi PageDiff) z dict-a to_dict()."""
    data = dict(data)
    data["page_diffs"] = [PageDiff(**pd) for pd in (data.get("page_diffs") or [])]
    return ArtworkCompareResult(**data)


def compare_artworks(path_a, path_b, **kwargs) -> ArtworkCompareResult:
    """Porównuje artworki lokalnie albo przez zdalny artwork-engine.

    Zwraca zawsze prawdziwą instancję dataclass (nigdy surowy dict), żeby
    dotychczasowi wywołujący (cmp.critical_count, cmp.page_diffs[0]...) działali
    bez zmian niezależnie od trybu.
    """
    url = os.environ.get("ARTWORK_ENGINE_URL", "").strip()
    if not url:
        return _local_compare(path_a, path_b, **kwargs)
    if not url.startswith(("http://", "https://")):
        logger.error("ARTWORK_ENGINE_URL musi zaczynać się od http:// lub https:// — fallback lokalny")
        return _local_compare(path_a, path_b, **kwargs)

    payload = json.dumps({"path_a": path_a, "path_b": path_b, **kwargs}).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/compare", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        # bandit: schemat ARTWORK_ENGINE_URL sprawdzony wyżej (tylko http/https)
        with urllib.request.urlopen(req, timeout=180) as resp:  # nosec B310
            return _reconstruct(json.loads(resp.read()))
    except urllib.error.HTTPError as exc:
        # A 4xx here means the engine rejected the request (e.g. path-traversal
        # guard, misconfigured ARTWORK_ENGINE_ROOT, or a dataclass-shape drift
        # causing _reconstruct() to raise upstream) — a real bug/misconfig, not
        # a transient outage. Log louder so it doesn't look "fine" from the
        # outside while silently degrading to always-local-compute.
        logger.error("artwork-engine zwrócił HTTP %s (%s) — fallback lokalny", exc.code, exc)
        return _local_compare(path_a, path_b, **kwargs)
    except Exception as exc:  # timeout / 5xx / connection error — graceful degrade
        logger.warning("artwork-engine niedostępny (%s) — fallback lokalny", exc)
        return _local_compare(path_a, path_b, **kwargs)
