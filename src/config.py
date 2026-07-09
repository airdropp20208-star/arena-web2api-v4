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
# Default bind 127.0.0.1 (localhost only) for security.
# Set HOST=0.0.0.0 ONLY if you need access from other devices on LAN.
# WARNING: 0.0.0.0 exposes server publicly — always set API_KEYS + ADMIN_TOKEN.
HOST = os.getenv("HOST", "127.0.0.1")
PORT = _get_int("PORT", "8000")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
DEBUG = _get_bool("DEBUG", "false")
# Bảo vệ endpoint admin nhạy cảm (trống = cho phép tất cả — KHÔNG recommended)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# ── Security warnings ──────────────────────────────────────────────────────
# Warn at import time if insecure defaults are used
def _security_warnings():
    warnings = []
    if HOST == "0.0.0.0" and not API_KEYS and not API_KEY_ENABLED:
        warnings.append(
            "⚠️  HOST=0.0.0.0 but no API_KEYS set — server is PUBLIC + UNAUTHENTICATED. "
            "Anyone on your network can use it. Set API_KEYS=yourkey or HOST=127.0.0.1."
        )
    if HOST == "0.0.0.0" and not ADMIN_TOKEN:
        warnings.append(
            "⚠️  HOST=0.0.0.0 but no ADMIN_TOKEN set — /admin/* endpoints are PUBLIC. "
            "Anyone can view cookies, metrics, broker status. Set ADMIN_TOKEN."
        )
    if DEBUG and HOST == "0.0.0.0":
        warnings.append(
            "⚠️  DEBUG=true + HOST=0.0.0.0 — full request/response logged + publicly accessible. "
            "Set DEBUG=false or HOST=127.0.0.1."
        )
    return warnings

# ── Arena endpoints ────────────────────────────────────────────────────────
ARENA_BASE = os.getenv("ARENA_BASE", "https://arena.ai").rstrip("/")
ARENA_STREAM_URL = f"{ARENA_BASE}/nextjs-api/stream/create-evaluation"
ARENA_MODELS_URL = f"{ARENA_BASE}/nextjs-api/v1/models"
ARENA_VOTE_URL = f"{ARENA_BASE}/nextjs-api/vote"

# ── Cookie (single account, backwards-compatible) ──────────────────────────
ARENA_AUTH = os.getenv("ARENA_AUTH_COOKIE", "")
CF_CLEARANCE = os.getenv("CF_CLEARANCE", "")

# ── Arena credentials (cho browser proxy auto-login) ──────────────────────
ARENA_EMAIL = os.getenv("ARENA_EMAIL", "")
ARENA_PASSWORD = os.getenv("ARENA_PASSWORD", "")

# ── reCAPTCHA solver (Approach A→B fallback) ──────────────────────────────
# Chiến lược:
#   "skip"       — không gửi recaptchaV3Token (hy vọng Arena backend không enforce)
#   "2captcha"   — gọi 2Captcha API để lấy token (cần TWO_CAPTCHA_API_KEY, $)
#   "browser"    — gen token qua Playwright (chỉ chạy được trên máy có display)
#                  KHÔNG khuyến nghị dùng cho production server
#   "extension"  — ✅ RECOMMENDED cho ĐT/VPS free. Kiwi Browser cài extension,
#                  extension gen token trong arena.ai tab, gửi về server qua WS.
RECAPTCHA_SOLVER = os.getenv("RECAPTCHA_SOLVER", "skip").lower().strip()
TWO_CAPTCHA_API_KEY = os.getenv("TWO_CAPTCHA_API_KEY", "")
# Site key reCAPTCHA Enterprise của Arena (extracted from page)
RECAPTCHA_SITE_KEY = os.getenv("RECAPTCHA_SITE_KEY", "6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0")
# Action mà Arena expects (from captured payload)
RECAPTCHA_ACTION = os.getenv("RECAPTCHA_ACTION", "chat_submit")
# Cache token trong bao lâu (giây). reCAPTCHA v3 token valid ~120s, conservative 90s.
# Lưu ý: extension strategy không cache (token single-use).
RECAPTCHA_TOKEN_TTL = _get_int("RECAPTCHA_TOKEN_TTL", "90")
# Timeout cho solver (giây) — 2Captcha 10-30s, extension ~2s, browser ~1s
RECAPTCHA_SOLVE_TIMEOUT = _get_int("RECAPTCHA_SOLVE_TIMEOUT", "30")
# Min score yêu cầu (cho 2captcha request)
RECAPTCHA_MIN_SCORE = _get_float("RECAPTCHA_MIN_SCORE", "0.7")

# ── Token broker (cho extension strategy) ─────────────────────────────────
# WebSocket server mà extension kết nối tới. Mặc định localhost:8765.
TOKEN_BROKER_HOST = os.getenv("TOKEN_BROKER_HOST", "127.0.0.1")
TOKEN_BROKER_PORT = _get_int("TOKEN_BROKER_PORT", "8765")
# Bật token broker server (chỉ tắt khi không dùng extension strategy)
TOKEN_BROKER_ENABLED = _get_bool("TOKEN_BROKER_ENABLED", "true")

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
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
APP_VERSION = "3.1.0"
