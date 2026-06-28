"""
Central configuration — mọi giá trị được đọc từ environment (.env).

Một module duy nhất để toàn bộ app tham chiếu, tránh hardcode rải rác.
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _get_bool(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes", "on")


def _get_int(key: str, default: str) -> int:
    try:
        return int(os.getenv(key, default))
    except (TypeError, ValueError):
        return int(default)


def _get_float(key: str, default: str) -> float:
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        return float(default)


def _get_list(key: str, default: str = "") -> list[str]:
    raw = os.getenv(key, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


# ── Server ─────────────────────────────────────────────────────────────────
HOST = os.getenv("HOST", "0.0.0.0")
PORT = _get_int("PORT", "8000")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
DEBUG = _get_bool("DEBUG", "false")
# khoá API đơn giản cho endpoint admin nhạy cảm (trống = cho phép tất cả)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# ── Arena endpoints ────────────────────────────────────────────────────────
ARENA_BASE = os.getenv("ARENA_BASE", "https://arena.ai").rstrip("/")
ARENA_STREAM_URL = f"{ARENA_BASE}/nextjs-api/stream/create-evaluation"
ARENA_MODELS_URL = f"{ARENA_BASE}/nextjs-api/v1/models"
ARENA_VOTE_URL = f"{ARENA_BASE}/nextjs-api/vote"

# ── Cookie (single account, backwards-compatible) ──────────────────────────
ARENA_AUTH = os.getenv("ARENA_AUTH_COOKIE", "")
CF_CLEARANCE = os.getenv("CF_CLEARANCE", "")

# ── Cookie pool (nhiều account, xoay vòng) ─────────────────────────────────
# CSV: "arena-auth-1|cf-clearance-1,arena-auth-2|cf-clearance-2"
COOKIE_POOL_RAW = os.getenv("COOKIE_POOL", "")
COOKIE_HEALTH_TTL = _get_int("COOKIE_HEALTH_TTL", "300")  # giây giữa 2 lần health-check
COOKIE_FAIL_THRESHOLD = _get_int("COOKIE_FAIL_THRESHOLD", "3")  # số lần fail liên tiếp → unhealthy
COOKIE_AUTO_REFRESH = _get_bool("COOKIE_AUTO_REFRESH", "false")

# ── Retry & timeout ────────────────────────────────────────────────────────
RETRY_ATTEMPTS = _get_int("RETRY_ATTEMPTS", "3")
RETRY_BASE_DELAY = _get_float("RETRY_BASE_DELAY", "1.5")  # backoff cơ sở (giây)
RETRY_MAX_DELAY = _get_float("RETRY_MAX_DELAY", "30.0")  # backoff trần (giây)
RETRY_JITTER = _get_float("RETRY_JITTER", "0.3")  # tỉ lệ jitter (0-1)
# Validate: base delay phải > 0 để tránh division by zero
if RETRY_BASE_DELAY <= 0:
    RETRY_BASE_DELAY = 1.5
if RETRY_MAX_DELAY < RETRY_BASE_DELAY:
    RETRY_MAX_DELAY = RETRY_BASE_DELAY
REQUEST_TIMEOUT = _get_int("REQUEST_TIMEOUT", "120")
CONNECT_TIMEOUT = _get_float("CONNECT_TIMEOUT", "15.0")
# tự thử lại cho các status này (429/5xx); 4xx khác throw ngay
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# ── Proxy ──────────────────────────────────────────────────────────────────
PROXY = os.getenv("PROXY", "") or None
# pool proxy xoay vòng (CSV)
PROXY_POOL = _get_list("PROXY_POOL", "")

# ── Model registry (dynamic UUID sync) ─────────────────────────────────────
MODEL_REGISTRY_TTL = _get_int("MODEL_REGISTRY_TTL", "600")  # giây — refresh UUID map
MODEL_REGISTRY_ON_STARTUP = _get_bool("MODEL_REGISTRY_ON_STARTUP", "true")

# ── Conversation store (multi-turn thật) ───────────────────────────────────
CONVERSATION_TTL = _get_int("CONVERSATION_TTL", "1800")  # giây — sống của 1 conversation
CONVERSATION_MAX_TURNS = _get_int("CONVERSATION_MAX_TURNS", "50")
CONVERSATION_STORE_FILE = os.getenv("CONVERSATION_STORE_FILE", "")  # trống = chỉ RAM

# ── Rate limiter (token bucket) ────────────────────────────────────────────
RATE_LIMIT_ENABLED = _get_bool("RATE_LIMIT_ENABLED", "false")
RATE_LIMIT_RPM = _get_int("RATE_LIMIT_RPM", "60")  # requests / phút
RATE_LIMIT_TPM = _get_int("RATE_LIMIT_TPM", "0")  # tokens / phút (0 = bỏ qua)

# ── Circuit breaker ────────────────────────────────────────────────────────
CB_ENABLED = _get_bool("CB_ENABLED", "true")
CB_FAILURE_THRESHOLD = _get_int("CB_FAILURE_THRESHOLD", "5")
CB_COOLDOWN = _get_float("CB_COOLDOWN", "30.0")  # giây open → half-open
CB_HALF_OPEN_MAX = _get_int("CB_HALF_OPEN_MAX", "1")  # số request thử trong half-open

# ── Tokenizer ──────────────────────────────────────────────────────────────
# tiktoken không có encoding chính xác cho mọi vendor, dùng o200k_base (GPT-4o)
# làm xấp xỉ hợp lý cho mọi model.
TOKENIZER_ENCODING = os.getenv("TOKENIZER_ENCODING", "o200k_base")
TOKENIZER_FALLBACK_HEURISTIC = _get_bool("TOKENIZER_FALLBACK_HEURISTIC", "true")

# ── Metrics ────────────────────────────────────────────────────────────────
METRICS_ENABLED = _get_bool("METRICS_ENABLED", "true")

# ── Auth (API key cho /v1/*) ───────────────────────────────────────────────
# Danh sách key hợp lệ (CSV). Trống = không yêu cầu auth (chỉ dùng local).
API_KEYS = _get_list("API_KEYS", "")
# Tên header kiểm tra (Authorization: Bearer ... hoặc X-API-Key: ...)
API_KEY_ENABLED = _get_bool("API_KEY_ENABLED", "false")

# ── Concurrency control ───────────────────────────────────────────────────
MAX_CONCURRENT_REQUESTS = _get_int("MAX_CONCURRENT_REQUESTS", "16")  # semaphore toàn cục
MAX_QUEUE_SIZE = _get_int("MAX_QUEUE_SIZE", "64")  # hàng đợi chờ
PER_CONVERSATION_LOCK = _get_bool("PER_CONVERSATION_LOCK", "true")

# ── Idempotency ───────────────────────────────────────────────────────────
IDEMPOTENCY_ENABLED = _get_bool("IDEMPOTENCY_ENABLED", "true")
IDEMPOTENCY_TTL = _get_int("IDEMPOTENCY_TTL", "300")  # giây

# ── Tools / function calling ──────────────────────────────────────────────
# Lớp dịch: inject tool schema vào prompt, parse tool_call từ response.
TOOLS_ENABLED = _get_bool("TOOLS_ENABLED", "true")
TOOLS_MAX_PARALLEL = _get_int("TOOLS_MAX_PARALLEL", "5")

# ── Vision / attachments ──────────────────────────────────────────────────
MAX_ATTACHMENT_BYTES = _get_int("MAX_ATTACHMENT_BYTES", str(20 * 1024 * 1024))  # 20 MB
MAX_ATTACHMENTS = _get_int("MAX_ATTACHMENTS", "5")

# ── Observability ─────────────────────────────────────────────────────────
REQUEST_ID_HEADER = os.getenv("REQUEST_ID_HEADER", "X-Request-ID")
LOG_JSON = _get_bool("LOG_JSON", "false")

# ── Misc ───────────────────────────────────────────────────────────────────
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
APP_VERSION = "3.1.0"
