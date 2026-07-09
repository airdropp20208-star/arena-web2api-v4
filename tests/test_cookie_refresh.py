"""
Test cookie refresh + relogin flows qua token broker.
Mock extension sẽ:
  - Respond to need_cookies với fake cookies
  - Respond to relogin với ok=true
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, '/home/z/my-project/scripts/arena-web2api-v4')

# Set env BEFORE importing
os.environ['RECAPTCHA_SOLVER'] = 'extension'
os.environ['TOKEN_BROKER_HOST'] = '127.0.0.1'
os.environ['TOKEN_BROKER_PORT'] = '18770'

import importlib
import src.config
importlib.reload(src.config)
import src.recaptcha_solver
importlib.reload(src.recaptcha_solver)
import src.token_broker
importlib.reload(src.token_broker)
import src.cookie_pool
importlib.reload(src.cookie_pool)

import websockets
from src.token_broker import broker


FAKE_COOKIES = {
    "arena-auth-prod-v1.0": "base64-fake-chunk-0-" + "x" * 2000,
    "arena-auth-prod-v1.1": "fake-chunk-1-" + "y" * 1500,
    "cf_clearance": "fake-cf-clearance-" + "z" * 300,
    "__cf_bm": "fake-cf-bm",
    "user_country_code": "VN",
}


async def mock_extension_with_cookies(server_port: int, ready_event: asyncio.Event):
    """Mock extension that handles token, cookies, and relogin requests."""
    uri = f"ws://127.0.0.1:{server_port}"
    async with websockets.connect(uri) as ws:
        ready_event.set()
        await ws.send(json.dumps({
            "type": "hello", "agent": "mock-ext-v2", "version": "1.0.0",
        }))
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "need_token":
                await asyncio.sleep(0.3)
                await ws.send(json.dumps({
                    "type": "token", "id": msg["id"], "ok": True,
                    "token": "FAKE_TOKEN_" + msg["id"],
                }))
            elif msg.get("type") == "need_cookies":
                await asyncio.sleep(0.2)
                await ws.send(json.dumps({
                    "type": "cookies", "id": msg["id"], "ok": True,
                    "cookies": FAKE_COOKIES,
                }))
            elif msg.get("type") == "relogin":
                await asyncio.sleep(0.5)
                await ws.send(json.dumps({
                    "type": "relogin_result", "id": msg["id"], "ok": True,
                }))
            elif msg.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong", "id": msg["id"]}))


async def test_cookie_refresh():
    """Test request_cookies → returns dict with chunked cookies."""
    print("\n=== TEST 1: cookie refresh via broker ===")
    port = 18770
    await broker.start(host="127.0.0.1", port=port)
    await asyncio.sleep(0.2)

    ready = asyncio.Event()
    ext_task = asyncio.create_task(mock_extension_with_cookies(port, ready))
    await ready.wait()
    await asyncio.sleep(0.2)

    cookies = await broker.request_cookies(timeout=5.0)
    assert "arena-auth-prod-v1.0" in cookies, f"missing chunk 0: {list(cookies.keys())}"
    assert "arena-auth-prod-v1.1" in cookies, f"missing chunk 1: {list(cookies.keys())}"
    assert "cf_clearance" in cookies
    print(f"✓ got cookies: {list(cookies.keys())}")

    ext_task.cancel()
    try:
        await ext_task
    except asyncio.CancelledError:
        pass
    await broker.stop()
    print("✓ test 1 passed")


async def test_relogin():
    """Test request_relogin → returns True."""
    print("\n=== TEST 2: relogin via broker ===")
    port = 18771
    await broker.start(host="127.0.0.1", port=port)
    await asyncio.sleep(0.2)

    ready = asyncio.Event()
    ext_task = asyncio.create_task(mock_extension_with_cookies(port, ready))
    await ready.wait()
    await asyncio.sleep(0.2)

    ok = await broker.request_relogin(timeout=5.0)
    assert ok is True, f"relogin should return True, got {ok}"
    print(f"✓ relogin returned: {ok}")

    ext_task.cancel()
    try:
        await ext_task
    except asyncio.CancelledError:
        pass
    await broker.stop()
    print("✓ test 2 passed")


async def test_cookie_pool_refresh():
    """Test CookiePool.refresh_from_extension updates the default entry."""
    print("\n=== TEST 3: cookie pool auto-refresh ===")
    port = 18772
    await broker.start(host="127.0.0.1", port=port)
    await asyncio.sleep(0.2)

    ready = asyncio.Event()
    ext_task = asyncio.create_task(mock_extension_with_cookies(port, ready))
    await ready.wait()
    await asyncio.sleep(0.2)

    # Get pool (singleton)
    from src.cookie_pool import get_cookie_pool, CookieEntry
    pool = await get_cookie_pool()

    # Inject a fake "expired" cookie entry labeled "default"
    async with pool._lock:
        pool._entries = [CookieEntry(arena_auth="EXPIRED", cf_clearance="EXPIRED", label="default")]

    # Refresh
    ok = await pool.refresh_from_extension()
    assert ok is True, "refresh should succeed"

    # Verify entry was updated
    snap = pool.snapshot()
    assert len(snap) == 1
    entry = snap[0]
    assert entry["label"] == "default"
    assert entry["healthy"] is True
    assert entry["fail_count"] == 0
    assert "EXPIRED" not in pool._entries[0].arena_auth
    assert "base64-fake-chunk-0" in pool._entries[0].arena_auth
    print(f"✓ cookie pool refreshed: label={entry['label']}, healthy={entry['healthy']}")

    # Verify as_cookies returns chunked
    cookies = pool._entries[0].as_cookies()
    assert "arena-auth-prod-v1.0" in cookies
    assert "arena-auth-prod-v1.1" in cookies
    print(f"✓ chunked cookies emitted: {list(cookies.keys())}")

    ext_task.cancel()
    try:
        await ext_task
    except asyncio.CancelledError:
        pass
    await broker.stop()
    print("✓ test 3 passed")


async def main():
    print(f"strategy: {os.environ.get('RECAPTCHA_SOLVER')}")
    await test_cookie_refresh()
    await test_relogin()
    await test_cookie_pool_refresh()
    print("\n🎉 All cookie/relogin tests PASS")


asyncio.run(main())
