"""
Debug request/response logging middleware.

Khi DEBUG=true, log chi tiết mọi request/response (headers, body, timing).
Tắt trong production để tránh leak data.
"""

from __future__ import annotations

import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from src.config import DEBUG
from src.logger import setup_logger

logger = setup_logger("debug.http")


class DebugLoggingMiddleware(BaseHTTPMiddleware):
    """Log request/response details when DEBUG=true."""

    async def dispatch(self, request: Request, call_next):
        if not DEBUG:
            return await call_next(request)

        start = time.time()
        method = request.method
        path = request.url.path
        query = str(request.url.query) if request.url.query else ""

        # Log request
        body_preview = ""
        if method in ("POST", "PUT", "PATCH"):
            try:
                body = await request.body()
                body_preview = body[:500].decode("utf-8", errors="replace")
            except Exception:
                body_preview = "<unreadable>"

        logger.debug(
            f"→ {method} {path}{'?' + query if query else ''} "
            f"body={body_preview[:200] if body_preview else '-'}"
        )

        # Process
        response = await call_next(request)

        elapsed_ms = round((time.time() - start) * 1000, 1)
        logger.debug(f"← {method} {path} → {response.status_code} ({elapsed_ms}ms)")

        return response
