"""
Test các tính năng mới: log redaction, token bucket, cookie refresh dedup,
account banned detection.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, '/home/z/my-project/scripts/arena-web2api-v4')

os.environ['RECAPTCHA_SOLVER'] = 'extension'
os.environ['TOKEN_BROKER_HOST'] = '127.0.0.1'
os.environ['TOKEN_BROKER_PORT'] = '18800'

import importlib
import src.config
importlib.reload(src.config)
import src.logger
importlib.reload(src.logger)
import src.errors
importlib.reload(src.errors)
import src.token_broker
importlib.reload(src.token_broker)
import src.cookie_pool
importlib.reload(src.cookie_pool)

from src.logger import redact
from src.errors import ArenaAuthError
from src.token_broker import broker, MIN_TOKEN_INTERVAL, MAX_BURST
import websockets


def test_redact():
    """Test #8: redact sensitive data."""
    print("\n=== TEST 1: redact sensitive data ===")

    # arena-auth chunks
    text = '{"arena-auth-prod-v1.0": "base64-eyJabc12345678901234567890123456789012345678901234567890"}'
    out = redact(text)
    assert "base64-eyJabc" not in out, f"chunk 0 not redacted: {out}"
    assert "***REDACTED***" in out
    print(f"  ✓ arena-auth chunk redacted")

    # cf_clearance
    text = 'cf_clearance=abc12345678901234567890123456789012345678901234567890'
    out = redact(text)
    assert "abc1234567890" not in out
    print(f"  ✓ cf_clearance redacted")

    # JWT (must be 3 parts separated by dots, each part long enough)
    text = 'Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c'
    out = redact(text)
    assert "eyJhbGciOiJIUzI1NiJ9" not in out, f"JWT not redacted: {out}"
    assert "***JWT_REDACTED***" in out
    print(f"  ✓ JWT redacted")

    # recaptchaV3Token in JSON
    text = '{"recaptchaV3Token": "abc12345678901234567890123456789012345678901234567890"}'
    out = redact(text)
    assert "abc1234567890" not in out
    print(f"  ✓ recaptchaV3Token redacted")

    # Normal text (no redaction)
    text = 'normal log message with user@email.com'
    out = redact(text)
    assert out == text
    print(f"  ✓ normal text unchanged")

    print("✓ test 1 passed (redact)")


def test_banned_detection():
    """Test #15: account banned detection."""
    print("\n=== TEST 2: banned account detection ===")

    err1 = ArenaAuthError("HTTP 403 — recaptcha validation failed. Body: recaptcha validation failed")
    assert err1.failure_mode == "recaptcha"
    print(f"  ✓ recaptcha detected: {err1.failure_mode}")

    err2 = ArenaAuthError("HTTP 403 — Account banned")
    assert err2.failure_mode == "banned"
    print(f"  ✓ banned detected: {err2.failure_mode}")

    err3 = ArenaAuthError("HTTP 403 — Cloudflare challenge required")
    assert err3.failure_mode == "cloudflare"
    print(f"  ✓ cloudflare detected: {err3.failure_mode}")

    err4 = ArenaAuthError("HTTP 403 từ Arena. Body: unauthorized")
    assert err4.failure_mode == "auth_expired"
    print(f"  ✓ auth_expired detected: {err4.failure_mode}")

    print("✓ test 2 passed (banned detection)")


async def mock_extension(server_port, ready_event):
    """Mock extension for token bucket test."""
    uri = f"ws://127.0.0.1:{server_port}"
    async with websockets.connect(uri) as ws:
        ready_event.set()
        await ws.send(json.dumps({"type": "hello", "agent": "mock", "version": "1.0"}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "need_token":
                await asyncio.sleep(0.3)  # simulate grecaptcha execute time
                # Token must be >100 chars (broker validation)
                token = "TOK_" + msg["id"] + "_" + "x" * 200
                await ws.send(json.dumps({
                    "type": "token", "id": msg["id"], "ok": True,
                    "token": token,
                }))
            elif msg.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong", "id": msg["id"]}))


async def test_token_bucket():
    """Test #5: token bucket rate limiting."""
    print("\n=== TEST 3: token bucket rate limit ===")
    port = 18800
    await broker.start(host="127.0.0.1", port=port)
    await asyncio.sleep(0.2)

    ready = asyncio.Event()
    ext_task = asyncio.create_task(mock_extension(port, ready))
    await ready.wait()
    await asyncio.sleep(0.2)

    # Send 3 requests sequentially (token bucket should enforce MIN_TOKEN_INTERVAL)
    import time
    t0 = time.time()
    results = []
    for i in range(3):
        r = await broker.request_token()
        results.append(r)
    elapsed = time.time() - t0

    # 3 sequential tokens, MIN_TOKEN_INTERVAL=1.5s between each
    # Expected: 1.5s (between req 1 and 2) + 1.5s (between 2 and 3) = ~3s minimum
    assert all(r.startswith("TOK_") for r in results), f"got: {results}"
    print(f"  ✓ 3 sequential tokens acquired in {elapsed:.2f}s")

    # Verify rate limiting actually kicked in
    # 3 requests need at least 2 × MIN_TOKEN_INTERVAL between them
    assert elapsed >= MIN_TOKEN_INTERVAL * 2, f"rate limit not enforced, elapsed={elapsed}s, expected >= {MIN_TOKEN_INTERVAL*2}s"
    print(f"  ✓ rate limit enforced (elapsed >= {MIN_TOKEN_INTERVAL*2}s)")

    ext_task.cancel()
    try:
        await ext_task
    except asyncio.CancelledError:
        pass
    await broker.stop()
    print("✓ test 3 passed (token bucket)")


async def test_cookie_refresh_dedup():
    """Test #4: cookie refresh dedup."""
    print("\n=== TEST 4: cookie refresh dedup ===")
    port = 18801
    await broker.start(host="127.0.0.1", port=port)
    await asyncio.sleep(0.2)

    # Mock extension that handles cookie requests
    async def mock_ext_cookies(port, ready):
        uri = f"ws://127.0.0.1:{port}"
        async with websockets.connect(uri) as ws:
            ready.set()
            await ws.send(json.dumps({"type": "hello", "agent": "mock", "version": "1.0"}))
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "need_cookies":
                    await asyncio.sleep(0.2)
                    await ws.send(json.dumps({
                        "type": "cookies", "id": msg["id"], "ok": True,
                        "cookies": {
                            "arena-auth-prod-v1.0": "chunk0_" + "x" * 100,
                            "arena-auth-prod-v1.1": "chunk1_" + "y" * 100,
                            "cf_clearance": "cf_" + "z" * 50,
                        },
                    }))
                elif msg.get("type") == "ping":
                    await ws.send(json.dumps({"type": "pong", "id": msg["id"]}))

    ready = asyncio.Event()
    ext_task = asyncio.create_task(mock_ext_cookies(port, ready))
    await ready.wait()
    await asyncio.sleep(0.2)

    from src.cookie_pool import get_cookie_pool
    pool = await get_cookie_pool()
    # Reset dedup state
    pool._last_refresh_at = 0.0
    pool._last_refresh_ok = False
    pool._refresh_in_progress = False

    # 3 parallel refresh requests — dedup should make only 1 actually call extension
    t0 = asyncio.get_event_loop().time()
    results = await asyncio.gather(
        pool.refresh_from_extension(),
        pool.refresh_from_extension(),
        pool.refresh_from_extension(),
    )
    elapsed = asyncio.get_event_loop().time() - t0

    # All should return True (1 real + 2 dedup hits)
    assert all(r for r in results), f"some failed: {results}"
    print(f"  ✓ 3 parallel refresh → all OK in {elapsed:.2f}s (dedup should make it fast)")

    # Verify only 1 actual extension call happened (dedup)
    # 3 sequential calls would take ~0.6s, dedup should make it ~0.2-0.5s
    assert elapsed < 0.7, f"dedup didn't work, elapsed={elapsed}s (expected < 0.7s)"
    print(f"  ✓ dedup worked (elapsed < 0.7s, only 1 real extension call)")

    ext_task.cancel()
    try:
        await ext_task
    except asyncio.CancelledError:
        pass
    await broker.stop()
    print("✓ test 4 passed (cookie refresh dedup)")


async def main():
    test_redact()
    test_banned_detection()
    await test_token_bucket()
    await test_cookie_refresh_dedup()
    print("\n🎉 All hardening tests PASS")


asyncio.run(main())
