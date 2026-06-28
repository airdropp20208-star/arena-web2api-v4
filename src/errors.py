"""
Hệ thống lỗi phân cấp — giúp route trả đúng HTTP status & client retry đúng.

CamelCase của upstream được giữ trong `.status`.
"""

from __future__ import annotations


class ArenaWeb2APIError(Exception):
    """Base error cho toàn bộ app."""

    status: int = 500

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        if status is not None:
            self.status = status
        self.message = message


# ── Cookie / auth ───────────────────────────────────────────────────────────
class NoCookiesError(ArenaWeb2APIError):
    status = 503

    def __init__(self, message: str = "Không có cookie nào khả dụng trong pool. / No cookies available in pool."):
        super().__init__(message)


class CookieError(ArenaWeb2APIError):
    status = 503


# ── Model registry ──────────────────────────────────────────────────────────
class ModelNotResolvedError(ArenaWeb2APIError):
    status = 400

    def __init__(self, model: str):
        super().__init__(
            f"Không phân giải được UUID cho model '{model}'. / Could not resolve UUID for model '{model}'. "
            "Gọi GET /v1/models để xem danh sách, hoặc bật MODEL_REGISTRY_ON_STARTUP. / "
            "Call GET /v1/models to see available models, or enable MODEL_REGISTRY_ON_STARTUP."
        )
        self.model = model


# ── Rate limit / circuit breaker ────────────────────────────────────────────
class RateLimitedError(ArenaWeb2APIError):
    status = 429


class CircuitOpenError(ArenaWeb2APIError):
    status = 503

    def __init__(self, cooldown: float):
        super().__init__(
            f"Circuit breaker đang OPEN (upstream đang lỗi). Thử lại sau ~{cooldown:.0f}s. / "
            f"Circuit breaker is OPEN (upstream error). Retry after ~{cooldown:.0f}s."
        )


# ── SSE ─────────────────────────────────────────────────────────────────────
class SSEParseError(ArenaWeb2APIError):
    status = 502


# ── Upstream Arena errors ───────────────────────────────────────────────────
class ArenaError(ArenaWeb2APIError):
    """Lỗi trả về từ arena.ai, mang HTTP status thật."""

    def __init__(self, status: int, message: str, *, retry_after: float | None = None):
        super().__init__(message, status=status)
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        from src.config import RETRYABLE_STATUS

        return self.status in RETRYABLE_STATUS


class ArenaAuthError(ArenaError):
    """401/403 — cookie hết hạn hoặc bị Cloudflare chặn."""

    def __init__(self, message: str):
        hint = (
            f"{message}\n→ Cookie hết hạn hoặc bị Cloudflare chặn.\n"
            "→ Cập nhật ARENA_AUTH_COOKIE + CF_CLEARANCE rồi gọi POST /admin/cookies/refresh"
        )
        super().__init__(403, hint)


class ArenaRateLimitError(ArenaError):
    def __init__(self, retry_after: float | None = None):
        super().__init__(429, "Rate limited bởi Arena.", retry_after=retry_after)


class ArenaServerError(ArenaError):
    def __init__(self, status: int, message: str):
        super().__init__(status, message)
