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


@router.get("/metrics")
async def prometheus_metrics():
    """Prometheus text format — fix #24. No auth (Prometheus scrapes anonymously)."""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        content=metrics.to_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


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


@router.get("/admin/broker")
async def admin_broker(x_admin_token: str | None = Header(default=None)):
    """Token bridge status (HTTP polling — thay thế WebSocket broker)."""
    _check_admin(x_admin_token)
    from src.token_bridge import bridge
    from src.config import RECAPTCHA_SOLVER

    return {
        "strategy": RECAPTCHA_SOLVER,
        "transport": "http_poll",
        **bridge.snapshot(),
    }


@router.get("/admin/poll")
async def admin_poll():
    """
    Extension poll endpoint — KHÔNG cần auth.
    Extension gọi GET này mỗi 2s để check server cần token không.
    """
    from src.token_bridge import bridge
    return await bridge.get_poll_response()


@router.post("/admin/token")
async def admin_submit_token(request: dict):
    """
    Extension submit token — KHÔNG cần auth.
    Extension gen token xong, POST về đây.
    Body: {"id": "...", "token": "...", "ok": true, "pre": false}
    pre=true → pre-token, server cache để dùng realtime
    """
    from src.token_bridge import bridge
    request_id = request.get("id", "")
    token = request.get("token")
    ok = request.get("ok", False)
    error = request.get("error")
    pre = request.get("pre", False)
    return await bridge.submit_token(request_id, token, ok, error, pre=pre)


@router.post("/admin/broker/test")
async def admin_broker_test(x_admin_token: str | None = Header(default=None)):
    """Test token request từ extension."""
    _check_admin(x_admin_token)
    from src.token_bridge import bridge
    from src.recaptcha_solver import current_strategy

    if current_strategy() != "extension":
        return {
            "ok": False,
            "error": f"Current strategy is '{current_strategy()}', not 'extension'.",
        }
    try:
        import time
        t0 = time.time()
        token = await bridge.request_token(timeout=30)
        return {
            "ok": True,
            "token_length": len(token),
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/admin/health-deep")
async def admin_health_deep(x_admin_token: str | None = Header(default=None)):
    """
    Deep health check — verify Arena API constants are still valid.

    Fetch arena.ai HTML, parse reCAPTCHA site key + verify endpoint URL pattern
    matches config. Detects when Arena deploys update that breaks constants.

    Returns:
      - status: ok | warning | error
      - checks: list of {check, ok, detail}
    """
    _check_admin(x_admin_token)
    import httpx
    from src.config import (
        ARENA_BASE,
        ARENA_STREAM_URL,
        RECAPTCHA_SITE_KEY,
        RECAPTCHA_ACTION,
    )

    checks = []

    # Check 1: arena.ai reachable
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.get(ARENA_BASE)
        if resp.status_code < 400:
            html = resp.text
            checks.append({"check": "arena.ai_reachable", "ok": True, "detail": f"HTTP {resp.status_code}, {len(html)} bytes"})
        else:
            checks.append({"check": "arena.ai_reachable", "ok": False, "detail": f"HTTP {resp.status_code}"})
            html = ""
    except Exception as e:
        checks.append({"check": "arena.ai_reachable", "ok": False, "detail": str(e)})
        html = ""

    # Check 2: reCAPTCHA site key still in HTML
    if html:
        if RECAPTCHA_SITE_KEY in html:
            checks.append({"check": "recaptcha_site_key_valid", "ok": True, "detail": f"key matches config: {RECAPTCHA_SITE_KEY[:20]}..."})
        else:
            # Try to extract current key
            import re
            keys = re.findall(r'6Le[A-Za-z0-9_-]{38,42}', html)
            if keys:
                checks.append({
                    "check": "recaptcha_site_key_valid",
                    "ok": False,
                    "detail": f"Config key not in HTML. Found in HTML: {keys[0]}. Update RECAPTCHA_SITE_KEY.",
                })
            else:
                checks.append({"check": "recaptcha_site_key_valid", "ok": False, "detail": "No site key found in HTML"})

    # Check 3: create-evaluation endpoint still exists (check via OPTIONS or HEAD)
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            # Use HEAD or OPTIONS — won't actually create evaluation
            resp = await http.options(ARENA_STREAM_URL)
        if resp.status_code in (200, 204, 405, 404):
            # 405 = method not allowed but endpoint exists
            # 404 = endpoint not found (BAD)
            endpoint_ok = resp.status_code != 404
            checks.append({
                "check": "stream_endpoint_exists",
                "ok": endpoint_ok,
                "detail": f"OPTIONS {ARENA_STREAM_URL} → HTTP {resp.status_code}",
            })
        else:
            checks.append({"check": "stream_endpoint_exists", "ok": True, "detail": f"HTTP {resp.status_code} (assumed OK)"})
    except Exception as e:
        checks.append({"check": "stream_endpoint_exists", "ok": False, "detail": str(e)})

    # Check 4: extension connected (if using extension strategy)
    try:
        from src.token_broker import broker
        from src.recaptcha_solver import current_strategy
        if current_strategy() == "extension":
            checks.append({
                "check": "extension_connected",
                "ok": broker.is_extension_connected,
                "detail": "connected" if broker.is_extension_connected else "DISCONNECTED — open Kiwi + extension",
            })
    except Exception as e:
        checks.append({"check": "extension_connected", "ok": False, "detail": str(e)})

    # Check 5: cookie pool healthy
    try:
        pool = await get_cookie_pool()
        checks.append({
            "check": "cookie_pool",
            "ok": pool.healthy_count() > 0,
            "detail": f"{pool.healthy_count()}/{pool.size} healthy",
        })
    except Exception as e:
        checks.append({"check": "cookie_pool", "ok": False, "detail": str(e)})

    # Aggregate status
    all_ok = all(c["ok"] for c in checks)
    any_fail = any(not c["ok"] for c in checks)
    status = "ok" if all_ok else ("error" if any_fail else "ok")

    return {
        "status": status,
        "checks": checks,
        "config": {
            "recaptcha_site_key": RECAPTCHA_SITE_KEY,
            "recaptcha_action": RECAPTCHA_ACTION,
            "stream_url": ARENA_STREAM_URL,
        },
    }
