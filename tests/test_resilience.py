"""
Tests cho resilience layers + regression tests cho các bug đã fix (B1-B12).

Phủ: concurrency gate, idempotency single-flight, auth constant-time,
auto-reconnect dedup, circuit breaker (kể cả HALF_OPEN probe limit),
cookie pool LRU, store thread-safety, atomic persist, token bucket.
Chạy:  python3 tests/test_resilience.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.circuit_breaker import CircuitBreaker
from src.concurrency import ConcurrencyGate
from src.errors import CircuitOpenError
from src.idempotency import IdempotencyStore
from src.rate_limiter import RateLimiter


# ── Concurrency gate ────────────────────────────────────────────────────────
def test_gate_limits_concurrency():
    import src.concurrency as cm

    cm.MAX_CONCURRENT_REQUESTS = 3
    cm.MAX_QUEUE_SIZE = 64
    g = ConcurrencyGate()
    active = {"max": 0, "cur": 0}

    async def worker():
        async with g.slot(queue_timeout=10):
            active["cur"] += 1
            active["max"] = max(active["max"], active["cur"])
            await asyncio.sleep(0.05)
            active["cur"] -= 1

    async def run():
        await asyncio.gather(*[worker() for _ in range(15)])

    asyncio.run(run())
    assert active["max"] == 3, f"should cap at 3, got {active['max']}"
    print(f"✓ gate caps concurrency (peak {active['max']} == limit 3)")


def test_gate_releases_on_error():
    g = ConcurrencyGate()

    async def run():
        try:
            async with g.slot():
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        async with g.slot():
            pass

    asyncio.run(run())
    assert g.active == 0
    print("✓ gate releases slot on exception")


def test_gate_rejects_when_full():
    """B-check: queue đầy → 503."""
    import src.concurrency as cm

    cm.MAX_CONCURRENT_REQUESTS = 1
    cm.MAX_QUEUE_SIZE = 1
    g = ConcurrencyGate()
    from src.errors import ArenaWeb2APIError

    async def run():
        async with g.slot():
            # thử 2 cái nữa → vượt 1 active + 1 queue = 2 → reject
            try:
                async with g.slot(queue_timeout=0.1):
                    pass
                async with g.slot(queue_timeout=0.1):
                    pass
                return "no-reject"
            except ArenaWeb2APIError:
                return "rejected"

    result = asyncio.run(run())
    assert result == "rejected", f"expected reject, got {result}"
    print("✓ gate rejects 503 when active+queue full")


# ── Idempotency single-flight (B4) ──────────────────────────────────────────
def test_idempotency_cache():
    store = IdempotencyStore()
    store._enabled = True

    async def run():
        assert await store.get_cached("k1") is None
        await store.put("k1", {"x": 1})
        assert await store.get_cached("k1") == {"x": 1}
        assert await store.get_cached("missing") is None

    asyncio.run(run())
    print("✓ idempotency put/get_cached")


def test_idempotency_single_flight_no_dup():
    """B4 regression: 2 request đồng thời cùng key → chỉ 1 chạy upstream."""
    store = IdempotencyStore()
    store._enabled = True
    upstream_calls = {"n": 0}

    async def do_request(key):
        result = await store.acquire(key)
        if result is not None and not hasattr(result, "event"):
            return result  # cache hit
        if result is not None and hasattr(result, "event"):
            await store.wait_for(result)
            cached = await store.get_cached(key)
            return cached
        # sở hữu → chạy upstream
        upstream_calls["n"] += 1
        await asyncio.sleep(0.05)
        value = {"data": "resp"}
        await store.put(key, value)
        return value

    async def run():
        await asyncio.gather(do_request("k"), do_request("k"), do_request("k"))

    asyncio.run(run())
    assert upstream_calls["n"] == 1, f"single-flight failed: {upstream_calls['n']} upstream calls"
    print(f"✓ idempotency single-flight: 3 concurrent → {upstream_calls['n']} upstream call")


# ── Circuit breaker (B2 HALF_OPEN probe limit) ─────────────────────────────
def test_breaker_trips_and_recovers():
    import src.circuit_breaker as cb_mod

    cb_mod.CB_FAILURE_THRESHOLD = 2
    cb_mod.CB_COOLDOWN = 0.1
    b = CircuitBreaker("t")

    async def run():
        await b.failure()
        await b.failure()
        try:
            await b.check()
            assert False, "should be open"
        except CircuitOpenError:
            pass
        await asyncio.sleep(0.15)
        await b.check()
        await b.success()
        await b.check()

    asyncio.run(run())
    print("✓ breaker trips → half-open → closed")


def test_breaker_half_open_probe_limit():
    """B2 regression: HALF_OPEN giới hạn CB_HALF_OPEN_MAX probe."""
    import src.circuit_breaker as cb_mod

    cb_mod.CB_FAILURE_THRESHOLD = 1
    cb_mod.CB_COOLDOWN = 0.05
    cb_mod.CB_HALF_OPEN_MAX = 1
    b = CircuitBreaker("t")

    async def run():
        await b.failure()  # trip ngay (threshold=1)
        await asyncio.sleep(0.06)  # hết cooldown
        # request đầu → HALF_OPEN, chiếm 1 probe slot
        await b.check()
        assert b.state.value == "half_open"
        # request thứ 2 → vượt probe limit → reject
        try:
            await b.check()
            assert False, "should reject 2nd probe"
        except CircuitOpenError:
            pass

    asyncio.run(run())
    print("✓ breaker HALF_OPEN limits probes to CB_HALF_OPEN_MAX")


# ── Rate limiter (B11 re-check) ─────────────────────────────────────────────
def test_rate_limiter_takes_token():
    from src.rate_limiter import TokenBucket

    rl = RateLimiter()
    rl.enabled = True
    rl.rpm_bucket = TokenBucket(2, 100.0)

    async def run():
        await rl.acquire_request()
        await rl.acquire_request()
        await rl.acquire_request()

    asyncio.run(run())
    print("✓ rate limiter take/replenish")


# ── Auto-reconnect dedup (B1/B3 path) ──────────────────────────────────────
def test_auto_reconnect_dedup():
    import httpx

    import src.client as cm
    from src.sse_parser import ArenaEvent

    class FakeClient(cm.ArenaClient):
        def __init__(self):
            self._call = 0

        async def _stream_attempt(self, payload):
            self._call += 1
            if self._call == 1:
                yield ArenaEvent(kind="delta", content="Hello")
                raise httpx.ReadError("disconnected mid-stream")
            else:
                yield ArenaEvent(kind="delta", content="Hello there!")
                yield ArenaEvent(kind="done", finish_reason="stop")

    fc = FakeClient()

    async def run():
        return [ev.content async for ev in fc._stream_with_retry({"mode": "direct"}, label="t")]

    out = asyncio.run(run())
    joined = "".join(out)
    assert joined == "Hello there!", f"got {joined!r}"
    assert joined.count("Hello") == 1
    print(f"✓ auto-reconnect dedup → {joined!r}")


def test_empty_stream_raises():
    """B3 regression: stream rỗng → ArenaServerError (không coi thành công)."""
    import src.client as cm
    from src.errors import ArenaError, ArenaServerError

    class FakeClient(cm.ArenaClient):
        async def _stream_attempt(self, payload):
            # mô phỏng stream rỗng: yield 0 events rồi raise ArenaServerError
            # (giống logic thật trong _stream_attempt khi `not started`)
            if False:
                yield  # keep it a generator
            raise ArenaServerError(502, "Arena stream trả về rỗng (0 events).")

    fc = FakeClient()

    async def run():
        try:
            async for _ in fc._stream_with_retry({}, label="t"):
                pass
            return "no-error"
        except ArenaError as e:
            return f"raised-{e.status}"

    result = asyncio.run(run())
    assert "raised-502" in result, f"empty stream should raise 502, got {result}"
    print("✓ empty stream → ArenaServerError 502 (not silent success)")


# ── Auth constant-time (B7) ─────────────────────────────────────────────────
def test_auth_constant_time():
    """B7 regression: so sánh constant-time, vẫn đúng logic."""
    import src.auth as auth_mod

    auth_mod.API_KEY_ENABLED = True
    auth_mod.API_KEYS = {"secret123", "another-key"}
    assert auth_mod._key_is_valid("secret123") is True
    assert auth_mod._key_is_valid("another-key") is True
    assert auth_mod._key_is_valid("wrong") is False
    assert auth_mod._key_is_valid("") is False
    assert auth_mod._key_is_valid(None) is False
    auth_mod.API_KEY_ENABLED = False
    print("✓ auth constant-time compare validates keys correctly")


# ── Cookie pool LRU (B10) ───────────────────────────────────────────────────
def test_cookie_pool_lru():
    """B10 regression: LRU phân phối đều, không lệ thuộc round-robin index."""
    from src.cookie_pool import CookieEntry, CookiePool

    cp = CookiePool()
    cp._entries = []
    for i in range(3):
        cp._entries.append(CookieEntry(f"auth{i}", f"cf{i}", label=f"c{i}"))

    async def run():
        order = []
        for _ in range(6):
            e = await cp.acquire()
            order.append(e.label)
        return order

    order = asyncio.run(run())
    # LRU: mỗi cookie được dùng lần lượt trước khi lặp
    assert sorted(order[:3]) == ["c0", "c1", "c2"], f"first round: {order[:3]}"
    assert sorted(order[3:]) == ["c0", "c1", "c2"], f"second round: {order[3:]}"
    print(f"✓ cookie pool LRU distributes evenly: {order}")


def test_cookie_pool_marks_unhealthy():
    import src.cookie_pool as cpm
    from src.cookie_pool import CookieEntry, CookiePool

    cpm.COOKIE_FAIL_THRESHOLD = 2
    cp = CookiePool()
    cp._entries = [CookieEntry("a", "c", label="solo")]

    async def run():
        e = await cp.acquire()
        await cp.mark_failed(e)
        assert e.healthy is True
        await cp.mark_failed(e)
        assert e.healthy is False
        await cp.mark_ok(e)
        assert e.healthy is True

    asyncio.run(run())
    print("✓ cookie pool fail threshold → unhealthy, mark_ok recovers")


# ── Store thread-safety + atomic persist (B8/B9) ────────────────────────────
def test_store_atomic_persist(tmp_path=None):
    """B9 regression: persist dùng atomic rename (temp + os.replace)."""
    import os

    import src.conversation_store as csm

    csm.CONVERSATION_STORE_FILE = "/tmp/_arena_test_conv.json"
    from src.conversation_store import Conversation, store

    if os.path.exists(csm.CONVERSATION_STORE_FILE):
        os.unlink(csm.CONVERSATION_STORE_FILE)

    async def run():
        store._convs.clear()
        store.put_sync(
            Conversation(
                key="k",
                model="gpt",
                conversation_id="cid",
                model_a_id="mid",
                history=[{"role": "user", "content": "hi"}],
                turns=1,
            )
        )
        await store.persist()
        # file phải tồn tại và parse được
        with open(csm.CONVERSATION_STORE_FILE) as f:
            data = json.load(f)
        assert len(data) == 1
        assert data[0]["conversation_id"] == "cid"
        # không có temp file leftover
        assert not os.path.exists(csm.CONVERSATION_STORE_FILE + ".tmp")
        # load lại
        store._convs.clear()
        await store.load()
        assert "k" in store._convs

    asyncio.run(run())
    os.unlink(csm.CONVERSATION_STORE_FILE)
    print("✓ store atomic persist + load round-trip")


def test_store_sync_lock_present():
    """B8 regression: sync accessors dùng threading.Lock."""
    import threading

    from src.conversation_store import store

    assert isinstance(store._sync_lock, type(threading.Lock()))
    print("✓ store sync accessors use threading.Lock")


# ── Breaker success on GeneratorExit (B1) ───────────────────────────────────
def test_breaker_neutral_on_generator_close():
    """
    B1 regression: khi async generator bị close() giữa chừng (GeneratorExit),
    breaker không bị stuck ở failure và không mark sai.
    """
    import src.circuit_breaker as cb_mod

    cb_mod.CB_ENABLED = True
    from src.client import ArenaClient, breaker
    from src.sse_parser import ArenaEvent

    class FakeClient(ArenaClient):
        async def _stream_attempt(self, payload):
            yield ArenaEvent(content="partial")
            # generator sẽ bị close ở đây → GeneratorExit

    fc = FakeClient()
    breaker.state = cb_mod.State.CLOSED
    breaker._failures = 0

    async def run():
        gen = fc._stream_grounded({}, label="t")
        ev = await gen.__anext__()
        assert ev.content == "partial"
        await gen.aclose()  # simulate client disconnect
        return breaker.state.value, breaker._failures

    state, failures = asyncio.run(run())
    # GeneratorExit → neutral, không trip, không success
    assert failures == 0, f"breaker should not count failure on disconnect, got {failures}"
    print(f"✓ breaker neutral on GeneratorExit (state={state}, failures={failures})")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n🎉 {len(tests)} resilience + regression tests PASS")


if __name__ == "__main__":
    main()
