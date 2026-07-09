"""
Request ID / correlation tracing.

  - Middleware gán X-Request-ID (dùng header incoming hoặc sinh uuid4).
  - contextvar chứa request_id → mọi log line kèm id → trace được.
"""

from __future__ import annotations

import contextvars
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from src.config import REQUEST_ID_HEADER

# contextvar đọc được từ bất kỳ tầng nào (logger, client, …)
request_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


def current_request_id() -> str | None:
    return request_id_ctx.get()


def new_request_id() -> str:
    return uuid.uuid4().hex


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get(REQUEST_ID_HEADER.lower()) or new_request_id()
        token = request_id_ctx.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        response.headers[REQUEST_ID_HEADER] = rid
        return response
