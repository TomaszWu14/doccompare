"""
constants.py — wspólne stałe dla DocCompare v6.

Importuj zamiast używać magic strings:
    from constants import ComparisonStatus, DocType, Severity
"""

from enum import Enum


# FIX 20: Status values (Polish): ok=match, roznica=difference, brak_w_a=missing in doc A,
# brak_w_b=missing in doc B, blad=error, format=format-only difference
class ComparisonStatus(str, Enum):
    OK      = "ok"
    WARNING = "warning"
    ERROR   = "error"
    CRITICAL = "critical"
    PENDING = "pending"
    FORMAT  = "format"
    MISSING_A = "brak_w_a"
    MISSING_B = "brak_w_b"
    ONLY_A  = "tylko_w_a"
    ONLY_B  = "tylko_w_b"
    DIFF    = "roznica"


class DocType(str, Enum):
    AUTO  = "auto"
    PO    = "PO"
    PI    = "PI"
    CI    = "CI"
    PL    = "PL"
    SAD   = "SAD"
    BL    = "BL"
    WZ    = "WZ"
    FV    = "FV"
    MULTI = "MULTI"
    CMR   = "CMR"
    ARTWORK = "artwork"


class Severity(str, Enum):
    CRITICAL  = "critical"
    IMPORTANT = "important"
    INFO      = "info"


class InvoiceJobStatus(str, Enum):
    """Cykl życia joba faktury w module Faktury → Excel."""
    UPLOADED     = "uploaded"
    EXTRACTED    = "extracted"
    CONFIRMED    = "confirmed"
    EXPORTED     = "exported"
    ERROR        = "error"
    PACKING_LIST = "packing_list"   # plik sklasyfikowany jako PL — źródło wag, nie faktura
    IGNORED      = "ignored"        # B/L, skan bez tekstu itp. — poza pipeline'em faktur


class MatchStatus(str, Enum):
    """Status dopasowania pozycji faktury do master daty."""
    MATCHED   = "matched"
    AMBIGUOUS = "ambiguous"   # kilku kandydatów (X vs X1) — operator rozstrzyga, blokuje confirm
    UNMATCHED = "unmatched"   # brak w master — zostaje z pustymi polami, NIE blokuje


class DocKind(str, Enum):
    """Typ dokumentu wyciętego z PDF-zestawu (CIPL / komplet kontenerowy)."""
    INVOICE      = "invoice"        # Commercial Invoice
    PROFORMA     = "proforma"       # Proforma -S = próbki (por. spec 2026-09-14)
    PACKING_LIST = "packing_list"
    OTHER        = "other"          # B/L, strona-skan, nierozpoznane


# Dokumenty przechodzące pipeline faktury (w odróżnieniu od PL/other).
INVOICE_LIKE_KINDS = (DocKind.INVOICE, DocKind.PROFORMA)


class UserRole(str, Enum):
    USER      = "user"
    MANAGER   = "manager"
    SUPERUSER = "superuser"
    ADMIN     = "admin"
    FORWARDER = "forwarder"   # spedytor zewnętrzny — portal Spedycja, poza hierarchią wewnętrzną
    CUSTOMS_AGENT = "customs_agent"  # agencja celna — portal Agencji, poza hierarchią wewnętrzną


# Mapowanie roli na poziom (wyższy = więcej uprawnień).
# UWAGA: 'forwarder' celowo NIE jest tu wpisany — ma poziom 0, więc require_role(...)
# dla stron wewnętrznych go zablokuje. Dostęp ma tylko do portalu Spedycja.
ROLE_LEVEL: dict[str, int] = {
    UserRole.USER:      1,
    UserRole.MANAGER:   2,
    UserRole.SUPERUSER: 3,
    UserRole.ADMIN:     4,
}

# Role zewnętrzne (poza hierarchią) i pełny zbiór dozwolonych ról (walidacja przy tworzeniu konta)
EXTERNAL_ROLES: set[str] = {UserRole.FORWARDER.value, UserRole.CUSTOMS_AGENT.value}
VALID_ROLES: set[str] = {r.value for r in UserRole}

# Limity API
API_RATE_MAX    = 30    # max zapytań per user w oknie
API_RATE_WINDOW = 60    # okno w sekundach
KPI_CACHE_TTL   = 300   # czas życia cache KPI w sekundach (5 min)

# Rozmiary plików
MAX_UPLOAD_MB   = 200   # max rozmiar uploadowanego pliku (MB)
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# OCR / porównanie artworków
ARTWORK_OCR_DPI     = 220   # DPI do OCR
ARTWORK_PREVIEW_DPI = 150   # DPI do podglądu
ARTWORK_DE_THRESH   = 7.0   # próg ΔE dla trybu tekstowego
ARTWORK_DE_THRESH_GRAPHIC = 18.0  # próg ΔE dla trybu graficznego

# Progi tolerancji cen
DEFAULT_PRICE_TOLERANCE_PCT = 0.0   # 0% tolerance — any price difference is flagged
DEFAULT_QTY_TOLERANCE_PCT   = 0.0   # 0% tolerancja ilości (zawsze dokładnie)

# Wersja aplikacji
APP_VERSION = "6.0.0"

# Rate limiting — logowanie
RATE_LIMIT_LOGIN_MAX    = 5     # max prób na okno
RATE_LIMIT_LOGIN_WINDOW = 900   # 15 minut (sekundy)

# Rate limiting — reset hasła
RATE_LIMIT_FORGOT_MAX    = 3
RATE_LIMIT_FORGOT_WINDOW = 900  # 15 minut

# Async job store
ASYNC_JOB_TTL = 3600   # seconds before completed job is evicted
ASYNC_JOB_MAX = 500    # max in-flight jobs — evict oldest on overflow

# Artwork cache
ARTWORK_CACHE_TTL = 7200   # 2 hours in seconds
ARTWORK_CACHE_MAX = 50

# FIX 18: Removed "binary/octet-stream" (too permissive); kept application/octet-stream only as fallback
ALLOWED_PDF_MIMES: frozenset = frozenset({
    "application/pdf", "application/x-pdf", "application/octet-stream"
})

# Dostawcy śledzenia przesyłek
TRACKING_PROVIDERS: frozenset = frozenset({
    "17track", "maersk", "trackcargo", "safecube"
})

# Ścieżki dostępne dla spedytora zewnętrznego (rola forwarder)
FORWARDER_ALLOWED_PREFIXES: tuple = (
    "/spedycja", "/api/spedycja", "/login", "/logout", "/static",
    "/set-language", "/favicon", "/health",
)

# Ścieżki dostępne dla agencji celnej (rola customs_agent)
CUSTOMS_AGENT_ALLOWED_PREFIXES: tuple = (
    "/agencja", "/api/agencja", "/login", "/logout", "/static",
    "/set-language", "/favicon", "/health",
)

# Mapowanie roli zewnętrznej → endpoint jej portalu (do przekierowań po logowaniu)
EXTERNAL_ROLE_HOME: dict = {
    UserRole.FORWARDER.value: "spedycja_page",
    UserRole.CUSTOMS_AGENT.value: "agencja_page",
}

# Dozwolone kolumny w _update_progress
PROGRESS_ALLOWED_COLS: frozenset = frozenset({
    'status', 'progress', 'message', 'result_id', 'error',
    'total_pages', 'current_page', 'items_found', 'step',
})

# FIX 19: Added missing operational constants
NOTIFICATION_LIMIT = 100   # max notifications returned per API call
ALERT_CFG_CACHE_TTL = 120  # alert config cache TTL in seconds
AI_TIMEOUT = 60            # urllib timeout for AI API calls in seconds
