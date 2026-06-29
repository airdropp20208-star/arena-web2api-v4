"""
Admin / health endpoints.

  GET  /health               — liveness
  GET  /cookie-status        — (legacy) cookie config
  GET  /admin/status         — tổng quan toàn hệ thống
  GET  /admin/cookies        — cookie pool snapshot
  POST /admin/cookies/validate — health-check toàn pool
  POST /admin/cookies/refresh  — alias validate
  GET  /admin/registry       — model registry snapshot
  GET  /admin/metrics        — metrics
  GET  /admin/breaker        — circuit breaker state
  GET  /admin/ratelimit      — rate limiter state
  GET  /admin/conversations  — conversation store snapshot
"""

from __future__ import annotations

import os
import time

import httpx
from fastapi import APIRouter, Header, HTTPException

from src.circuit_breaker import breaker
from src.concurrency import gate
from src.config import (
    ADMIN_TOKEN,
    APP_VERSION,
    ARENA_AUTH,
    ARENA_BASE,
    CF_CLEARANCE,
    DEFAULT_USER_AGENT,
)
from src.conversation_store import store
from src.cookie_pool import get_cookie_pool
from src.idempotency import idempotency
from src.logger import setup_logger
from src.metrics import metrics
from src.model_registry import registry
from src.per_key_rate_limit import per_key_limiter
from src.rate_limiter import limiter

router = APIRouter()
logger = setup_logger(__name__)


def _check_admin(x_admin_token: str | None):
    """Bảo vệ endpoint admin nếu ADMIN_TOKEN được set."""
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(401, "Invalid admin token (set X-Admin-Token header).")


def _get_memory_usage() -> dict:
    """Process memory usage (RSS, VMS)."""
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        return {
            "rss_mb": round(usage.ru_maxrss / 1024, 1),
            "pid": os.getpid(),
        }
    except Exception:
        return {"pid": os.getpid()}


async def _check_arena_latency() -> dict:
    """Ping Arena /nextjs-api/models — trả về latency ms hoặc error."""
    try:
        start = time.time()
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(
                f"{ARENA_BASE}/nextjs-api/models",
                headers={"accept": "application/json", "user-agent": DEFAULT_USER_AGENT},
            )
        latency_ms = round((time.time() - start) * 1000, 1)
        return {
            "reachable": resp.status_code < 500,
            "status_code": resp.status_code,
            "latency_ms": latency_ms,
        }
    except Exception as e:
        return {"reachable": False, "error": str(e)[:200]}


@router.get("/health")
async def health():
    """Liveness + memory + Arena connectivity."""
    mem = _get_memory_usage()
    return {
        "status": "ok",
        "service": "arena-web2api",
        "version": APP_VERSION,
        "memory": mem,
        "pid": os.getpid(),
    }


@router.get("/health/detailed")
async def health_detailed():
    """Detailed health: memory + Arena latency + pool status."""
    mem = _get_memory_usage()
    pool = await get_cookie_pool()
    arena = await _check_arena_latency()
    return {
        "status": "ok",
        "service": "arena-web2api",
        "version": APP_VERSION,
        "memory": mem,
        "arena": arena,
        "cookie_pool": {
            "size": pool.size,
            "healthy": pool.healthy_count(),
        },
        "breaker": breaker.snapshot(),
    }


@router.get("/ready")
async def ready():
    """Readiness — cần ít nhất 1 cookie healthy để serve."""
    pool = await get_cookie_pool()
    healthy = pool.healthy_count()
    ready_ok = healthy > 0
    return {
        "ready": ready_ok,
        "healthy_cookies": healthy,
        "cookie_pool_size": pool.size,
        "breaker_open": breaker.state == "open",
    }


@router.get("/cookie-status")
async def cookie_status():
    pool = await get_cookie_pool()
    return {
        "arena_auth_cookie": "✅ set" if ARENA_AUTH else "❌ chưa set",
        "cf_clearance": "✅ set" if CF_CLEARANCE else "⚠️ chưa set",
        "cookie_pool_size": pool.size,
        "cookie_pool_healthy": pool.healthy_count(),
        "guide": "Xem README.md → mục Lấy cookie",
    }


@router.get("/admin/status")
async def admin_status(x_admin_token: str | None = Header(default=None)):
    _check_admin(x_admin_token)
    pool = await get_cookie_pool()
    return {
        "service": "arena-web2api",
        "version": APP_VERSION,
        "cookie_pool": {"size": pool.size, "healthy": pool.healthy_count()},
        "registry": registry.snapshot(),
        "breaker": breaker.snapshot(),
        "rate_limiter": limiter.snapshot(),
        "concurrency": gate.snapshot(),
        "idempotency": idempotency.snapshot(),
        "metrics": metrics.snapshot()["totals"],
        "conversations": store.size,
    }


@router.get("/admin/concurrency")
async def admin_concurrency(x_admin_token: str | None = Header(default=None)):
    _check_admin(x_admin_token)
    return gate.snapshot()


@router.get("/admin/idempotency")
async def admin_idempotency(x_admin_token: str | None = Header(default=None)):
    _check_admin(x_admin_token)
    return idempotency.snapshot()


@router.get("/admin/cookies")
async def admin_cookies(x_admin_token: str | None = Header(default=None)):
    _check_admin(x_admin_token)
    pool = await get_cookie_pool()
    return {
        "size": pool.size,
        "healthy": pool.healthy_count(),
        "cookies": pool.snapshot(),
    }


@router.post("/admin/cookies/validate")
async def admin_cookies_validate(x_admin_token: str | None = Header(default=None)):
    _check_admin(x_admin_token)
    pool = await get_cookie_pool()
    results = await pool.validate_all()
    return {"validated": results, "healthy": pool.healthy_count(), "size": pool.size}


@router.post("/admin/cookies/refresh")
async def admin_cookies_refresh(x_admin_token: str | None = Header(default=None)):
    return await admin_cookies_validate(x_admin_token)


@router.get("/admin/registry")
async def admin_registry(x_admin_token: str | None = Header(default=None)):
    _check_admin(x_admin_token)
    return registry.snapshot()


@router.get("/admin/metrics")
async def admin_metrics(x_admin_token: str | None = Header(default=None)):
    _check_admin(x_admin_token)
    return metrics.snapshot()


@router.get("/admin/breaker")
async def admin_breaker(x_admin_token: str | None = Header(default=None)):
    _check_admin(x_admin_token)
    return breaker.snapshot()


@router.post("/admin/breaker/reset")
async def admin_breaker_reset(x_admin_token: str | None = Header(default=None)):
    _check_admin(x_admin_token)
    from src.circuit_breaker import State

    breaker.state = State.CLOSED
    breaker._failures = 0
    breaker._half_open_probes = 0
    return breaker.snapshot()


@router.get("/admin/ratelimit")
async def admin_ratelimit(x_admin_token: str | None = Header(default=None)):
    _check_admin(x_admin_token)
    return {"global": limiter.snapshot(), "per_key": per_key_limiter.snapshot()}


@router.get("/admin/conversations")
async def admin_conversations(x_admin_token: str | None = Header(default=None)):
    _check_admin(x_admin_token)
    purged = await store.cleanup()
    return {"purged": purged, "store": store.snapshot()}
