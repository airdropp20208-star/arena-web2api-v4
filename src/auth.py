"""
API key auth — bảo vệ /v1/* nếu API_KEY_ENABLED=true.

Hỗ trợ cả `Authorization: Bearer <key>` và `X-API-Key: <key>`.
Endpoint công khai (/health, /ready, /docs...) không bị chặn.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware

from src.config import API_KEY_ENABLED, API_KEYS
from src.logger import setup_logger
from src.per_key_rate_limit import per_key_limiter

logger = setup_logger(__name__)

# path không cần auth
_PUBLIC_PREFIXES = ("/health", "/ready", "/docs", "/redoc", "/openapi.json")


def _key_is_valid(provided: str | None) -> bool:
    """
    So sánh constant-time với mọi key hợp lệ (fix B7 — timing attack).
    Trả True nếu provided khớp ít nhất 1 key.
    """
    if not provided:
        return False
    provided_bytes = provided.encode("utf-8")
    return any(secrets.compare_digest(provided_bytes, k.encode("utf-8")) for k in API_KEYS)


def _extract_key(authorization: str | None, x_api_key: str | None) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    if authorization:
        a = authorization.strip()
        if a.lower().startswith("bearer "):
            return a[7:].strip()
        return a
    return None


def require_api_key(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Dependency: raise 401 nếu key sai (khi bật)."""
    if not API_KEY_ENABLED:
        return
    key = _extract_key(authorization, x_api_key)
    if not _key_is_valid(key):
        logger.warning(
            f"Auth failed: {request.client.host if request.client else '?'} "
            f"{request.method} {request.url.path}"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key. Send Authorization: Bearer <key> "
            "or X-API-Key header.",
            headers={"WWW-Authenticate": "Bearer"},
        )


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Middleware áp cho toàn /v1/* một lần duy nhất (constant-time check)."""

    async def dispatch(self, request: Request, call_next):
        if not API_KEY_ENABLED:
            return await call_next(request)
        path = request.url.path
        if path.startswith(_PUBLIC_PREFIXES):
            return await call_next(request)
        if not path.startswith("/v1"):
            return await call_next(request)
        key = _extract_key(
            request.headers.get("authorization"),
            request.headers.get("x-api-key"),
        )
        if not _key_is_valid(key):
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "message": "Invalid or missing API key.",
                        "type": "authentication_error",
                    }
                },
            )
        # Per-key rate limiting
        if not await per_key_limiter.check(key or ""):
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "message": "Rate limit exceeded for this API key.",
                        "type": "rate_limit_error",
                    }
                },
                headers={"Retry-After": "60"},
            )
        return await call_next(request)
