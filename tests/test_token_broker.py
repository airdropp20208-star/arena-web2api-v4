"""
Test token broker end-to-end with mock WebSocket client (simulates extension).
Verify:
1. Broker starts
2. Mock extension connects via WS
3. Server requests token
4. Mock extension "generates" token (mock)
5. Server receives token
6. Multi-session parallel requests
"""
import asyncio
import json
import sys
import os

sys.path.insert(0, '/home/z/my-project/scripts/arena-web2api-v4')

# Set env BEFORE importing
os.environ['RECAPTCHA_SOLVER'] = 'extension'
os.environ['TOKEN_BROKER_HOST'] = '127.0.0.1'
os.environ['TOKEN_BROKER_PORT'] = '18765'  # different port for testing

import importlib
import src.config
importlib.reload(src.config)
import src.recaptcha_solver
importlib.reload(src.recaptcha_solver)
import src.token_broker
importlib.reload(src.token_broker)

import websockets
from src.token_broker import broker
from src.recaptcha_solver import get_recaptcha_token, current_strategy


async def mock_extension_client(server_port: int, ready_event: asyncio.Event):
    """Simulate extension: connect to broker, respond to token requests."""
    uri = f"ws://127.0.0.1:{server_port}"
    print(f"[mock-ext] connecting to {uri}...")
    async with websockets.connect(uri) as ws:
        print("[mock-ext] connected")
        ready_event.set()

        # Send hello
        await ws.send(json.dumps({
            "type": "hello",
            "agent": "mock-extension",
            "version": "1.0.0",
            "hasArenaTab": True,
        }))

        # Listen for messages
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "need_token":
                print(f"[mock-ext] got token request {msg['id']}, genning...")
                # Simulate grecaptcha.enterprise.execute() delay
                await asyncio.sleep(0.5)
                fake_token = f"MOCK_TOKEN_{msg['id']}_" + "x" * 2000
                await ws.send(json.dumps({
                    "type": "token",
                    "id": msg["id"],
                    "ok": True,
                    "token": fake_token,
                }))
                print(f"[mock-ext] sent token for {msg['id']}")
            elif msg.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong", "id": msg["id"]}))


async def test_basic():
    """Test 1: single token request."""
    print("\n=== TEST 1: single token request ===")
    port = 18765
    await broker.start(host="127.0.0.1", port=port)
    await asyncio.sleep(0.2)

    ready = asyncio.Event()
    ext_task = asyncio.create_task(mock_extension_client(port, ready))
    await ready.wait()
    await asyncio.sleep(0.2)  # let hello message arrive

    assert broker.is_extension_connected, "extension should be connected"

    token = await get_recaptcha_token(force_refresh=True)
    assert token is not None, "token should not be None"
    assert token.startswith("MOCK_TOKEN_"), f"wrong token: {token[:50]}"
    print(f"✓ got token: {token[:60]}... (len={len(token)})")

    ext_task.cancel()
    try:
        await ext_task
    except asyncio.CancelledError:
        pass
    await broker.stop()
    print("✓ test 1 passed")


async def test_parallel():
    """Test 2: parallel multi-session token requests."""
    print("\n=== TEST 2: parallel token requests ===")
    port = 18766
    await broker.start(host="127.0.0.1", port=port)
    await asyncio.sleep(0.2)

    ready = asyncio.Event()
    ext_task = asyncio.create_task(mock_extension_client(port, ready))
    await ready.wait()
    await asyncio.sleep(0.2)

    # 3 parallel requests
    print("[test] dispatching 3 parallel requests...")
    t0 = asyncio.get_event_loop().time()
    results = await asyncio.gather(
        get_recaptcha_token(force_refresh=True),
        get_recaptcha_token(force_refresh=True),
        get_recaptcha_token(force_refresh=True),
    )
    elapsed = asyncio.get_event_loop().time() - t0

    assert all(r is not None for r in results), f"some None: {results}"
    ids = [r.split("_")[2] for r in results]  # MOCK_TOKEN_<id>_
    assert len(set(ids)) == 3, f"should be 3 unique tokens, got {ids}"
    print(f"✓ got 3 unique tokens in {elapsed:.2f}s")
    print(f"  token ids: {ids}")

    ext_task.cancel()
    try:
        await ext_task
    except asyncio.CancelledError:
        pass
    await broker.stop()
    print("✓ test 2 passed")


async def test_no_extension():
    """Test 3: server requests token when no extension connected → graceful fail."""
    print("\n=== TEST 3: no extension connected ===")
    port = 18767
    await broker.start(host="127.0.0.1", port=port)
    await asyncio.sleep(0.1)

    # No extension connects
    assert not broker.is_extension_connected, "should not be connected"
    token = await get_recaptcha_token(force_refresh=True)
    assert token is None, f"should be None when no extension, got {token}"
    print("✓ graceful None when no extension")

    await broker.stop()
    print("✓ test 3 passed")


async def main():
    print(f"strategy: {current_strategy()}")
    await test_basic()
    await test_parallel()
    await test_no_extension()
    print("\n🎉 All token broker tests PASS")


asyncio.run(main())
