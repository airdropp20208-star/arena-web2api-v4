"""
arena-web2api — Biến arena.ai thành OpenAI-compatible API.

Chạy:  python main.py
Docs:  http://localhost:8000/docs
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.auth import APIKeyMiddleware
from src.config import (
    APP_VERSION,
    HOST,
    LOG_LEVEL,
    PORT,
    RECAPTCHA_SOLVER,
    TOKEN_BROKER_ENABLED,
    TOKEN_BROKER_HOST,
    TOKEN_BROKER_PORT,
)
from src.conversation_store import store
from src.cookie_pool import get_cookie_pool
from src.debug_logging import DebugLoggingMiddleware
from src.logger import setup_logger
from src.model_registry import registry
from src.request_id import RequestIDMiddleware
from src.routes.admin import router as admin_router
from src.routes.battle import router as battle_router
from src.routes.chat import router as chat_router
from src.routes.models import router as models_router
from src.token_broker import broker

logger = setup_logger("main")

# Track active streams for graceful shutdown — fix #12, #29
_active_streams: int = 0
_active_streams_lock = asyncio.Lock()
_shutdown_requested: bool = False


async def register_stream() -> None:
    """Register an active stream — for graceful shutdown tracking."""
    global _active_streams
    async with _active_streams_lock:
        _active_streams += 1


async def unregister_stream() -> None:
    """Unregister a stream when done."""
    global _active_streams
    async with _active_streams_lock:
        _active_streams = max(0, _active_streams - 1)


async def get_active_streams() -> int:
    async with _active_streams_lock:
        return _active_streams


def _request_shutdown(signum, frame):
    """SIGTERM/SIGINT handler — graceful shutdown."""
    global _shutdown_requested
    if _shutdown_requested:
        logger.warning("Shutdown đã được request, force quit")
        sys.exit(1)
    _shutdown_requested = True
    sig_name = signal.Signals(signum).name
    logger.info(f"📡 Nhận {sig_name} — graceful shutdown (đợi active streams xong, max 30s)")
    # uvicorn will catch this and start shutdown
    # Raise KeyboardInterrupt to trigger uvicorn shutdown
    raise KeyboardInterrupt


# Register signal handlers (only works in main thread)
try:
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)
except (ValueError, OSError):
    # Not in main thread — signal handlers must be set in main thread
    pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ───────────────────────────────────────────────────────────
    logger.info("=" * 56)
    logger.info(f"🚀 arena-web2api v{APP_VERSION} đang khởi động...")
    logger.info(f"📡 Server: http://{HOST}:{PORT}")
    logger.info(f"📖 Docs:   http://localhost:{PORT}/docs")
    logger.info(f"🔑 reCAPTCHA strategy: {RECAPTCHA_SOLVER}")

    # Security warnings — fix #6, #7, #8
    from src.config import _security_warnings
    for warning in _security_warnings():
        logger.warning(warning)

    await store.load()
    pool = await get_cookie_pool()
    logger.info(f"🍪 Cookie pool: {pool.healthy_count()}/{pool.size} healthy")
    await pool.start_refresh_loop()

    await registry.start_refresh_loop()
    logger.info(f"🧠 Model registry: {len(registry.list_models())} model (loading...)")

    # Start token broker (for extension strategy)
    if TOKEN_BROKER_ENABLED:
        try:
            await broker.start(host=TOKEN_BROKER_HOST, port=TOKEN_BROKER_PORT)
            logger.info(
                f"🔌 Token broker: ws://{TOKEN_BROKER_HOST}:{TOKEN_BROKER_PORT} "
                f"(extension connects here)"
            )
        except Exception as e:
            logger.error(f"Failed to start token broker: {e}")
    else:
        logger.info("🔌 Token broker: disabled (TOKEN_BROKER_ENABLED=false)")

    logger.info("=" * 56)
    yield
    # ── shutdown ──────────────────────────────────────────────────────────
    logger.info("🛑 Đang tắt...")
    if TOKEN_BROKER_ENABLED:
        await broker.stop()
    await registry.stop()
    pool2 = await get_cookie_pool()
    await pool2.stop()
    await store.persist()
    logger.info("✅ Đã tắt sạch.")


app = FastAPI(
    title="Arena Web2API",
    description="OpenAI-compatible API cho arena.ai",
    version=APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # Restrict origins for security. Default: only localhost.
    # Set CORS_ORIGINS=* in .env to allow all (NOT recommended for production).
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(","),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Admin-Token", "Idempotency-Key"],
)
# Order matters: outermost runs first. APIKey check before RequestID is fine;
# RequestID should wrap so even 401 responses get an id.
app.add_middleware(APIKeyMiddleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(DebugLoggingMiddleware)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    """OpenAI-compatible validation error shape."""
    errors = exc.errors()
    msg = "; ".join(
        f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg', '')}" for e in errors[:5]
    )
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "message": f"Invalid request: {msg}",
                "type": "invalid_request_error",
                "code": "invalid_request",
            }
        },
    )


# Routes
app.include_router(admin_router, tags=["Admin"])  # /health, /cookie-status, /admin/*
app.include_router(chat_router, prefix="/v1", tags=["Chat"])  # /v1/chat/completions
app.include_router(battle_router, prefix="/v1", tags=["Battle"])  # /v1/battle, /v1/battle/vote
app.include_router(models_router, prefix="/v1", tags=["Models"])  # /v1/models


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=HOST,
        port=PORT,
        log_level=LOG_LEVEL.lower(),
        reload=False,
    )
