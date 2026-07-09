"""
Test reCAPTCHA solver logic without real 2Captcha API.
Verify:
1. skip strategy returns None
2. 2captcha strategy calls API correctly (mocked)
3. Token cache works
4. invalidate_token forces refresh
"""
import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, patch

sys.path.insert(0, '/home/z/my-project/scripts/arena-web2api-v4')

# Set env BEFORE importing config
os.environ['RECAPTCHA_SOLVER'] = '2captcha'
os.environ['TWO_CAPTCHA_API_KEY'] = 'fake_key_for_testing'
os.environ['RECAPTCHA_TOKEN_TTL'] = '5'  # short for testing

import importlib
import src.config
importlib.reload(src.config)
import src.recaptcha_solver
importlib.reload(src.recaptcha_solver)

from src.recaptcha_solver import (
    get_recaptcha_token,
    invalidate_token,
    current_strategy,
    _cached_token,
    _cached_at,
)


def test_skip_strategy():
    """skip → None."""
    # Force strategy to skip for this test
    import src.recaptcha_solver as rs
    rs.RECAPTCHA_SOLVER = "skip"
    result = asyncio.run(rs.get_recaptcha_token())
    assert result is None, f"skip should return None, got {result}"
    # Restore for subsequent tests
    rs.RECAPTCHA_SOLVER = "2captcha"
    print("✓ skip strategy returns None")


def test_2captcha_strategy_with_mock():
    """2captcha strategy → calls API → returns token (mocked)."""
    # Already reloaded in main — just use current state
    from src.recaptcha_solver import get_recaptcha_token, invalidate_token

    # Reset cache
    asyncio.run(invalidate_token())

    # Mock httpx.AsyncClient
    class FakeResponse:
        def __init__(self, data):
            self._data = data
        def json(self):
            return self._data

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            self.calls = []
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, data=None):
            self.calls.append(('post', url, data))
            return FakeResponse({"status": 1, "request": "task_123"})
        async def get(self, url, params=None):
            self.calls.append(('get', url, params))
            return FakeResponse({"status": 1, "request": "FAKE_TOKEN_FROM_2CAPTCHA_12345"})

    fake_client = FakeAsyncClient()
    with patch('httpx.AsyncClient', return_value=fake_client):
        token = asyncio.run(get_recaptcha_token(force_refresh=True))

    assert token == "FAKE_TOKEN_FROM_2CAPTCHA_12345", f"got {token}"
    assert len(fake_client.calls) >= 2, f"should call submit + poll, got {fake_client.calls}"
    print(f"✓ 2captcha strategy returns token via API (mocked): {token[:30]}...")

    # Verify cache works (no reload between, should hit cache)
    fake_client2 = FakeAsyncClient()
    with patch('httpx.AsyncClient', return_value=fake_client2):
        token2 = asyncio.run(get_recaptcha_token())  # no force_refresh — should hit cache
    assert token2 == token, f"cached token should match: {token2} vs {token}"
    assert len(fake_client2.calls) == 0, f"should hit cache, not call API, but got {fake_client2.calls}"
    print("✓ token cache works (no API call on 2nd get)")


def test_invalidate_forces_refresh():
    """invalidate_token → next get forces refresh."""
    from src.recaptcha_solver import get_recaptcha_token, invalidate_token

    call_count = [0]

    class FakeResponse:
        def json(self):
            call_count[0] += 1
            return {"status": 1, "request": f"token_{call_count[0]}"}

    class FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, data=None):
            return FakeResponse()
        async def get(self, url, params=None):
            return FakeResponse()

    with patch('httpx.AsyncClient', return_value=FakeAsyncClient()):
        t1 = asyncio.run(get_recaptcha_token(force_refresh=True))
        t2 = asyncio.run(get_recaptcha_token())  # cached
        asyncio.run(invalidate_token())
        t3 = asyncio.run(get_recaptcha_token())  # should refresh

    assert t1 == t2, "cache should return same token"
    assert t3 != t1, "after invalidate, should get fresh token"
    print(f"✓ invalidate forces refresh: t1={t1}, t3={t3}")


def test_no_api_key_fails_gracefully():
    """2captcha strategy without API key → returns None."""
    import src.recaptcha_solver as rs
    # Temporarily clear API key
    orig_key = rs.TWO_CAPTCHA_API_KEY
    rs.TWO_CAPTCHA_API_KEY = ""
    asyncio.run(rs.invalidate_token())
    try:
        result = asyncio.run(rs.get_recaptcha_token(force_refresh=True))
        assert result is None, "should return None without API key"
    finally:
        rs.TWO_CAPTCHA_API_KEY = orig_key
    print("✓ 2captcha without API key returns None gracefully")


if __name__ == "__main__":
    # Setup env once before all tests
    os.environ['RECAPTCHA_SOLVER'] = '2captcha'
    os.environ['TWO_CAPTCHA_API_KEY'] = 'fake_key_for_testing'
    os.environ['RECAPTCHA_TOKEN_TTL'] = '300'  # long enough for cache test
    importlib.reload(src.config)
    importlib.reload(src.recaptcha_solver)

    test_skip_strategy()
    test_2captcha_strategy_with_mock()
    test_invalidate_forces_refresh()
    test_no_api_key_fails_gracefully()
    print("\n🎉 All reCAPTCHA solver tests PASS")
