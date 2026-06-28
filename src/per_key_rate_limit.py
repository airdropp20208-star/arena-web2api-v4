"""
Per-API-key rate limiting — token bucket cho từng key.

Khi RATE_LIMIT_PER_KEY_ENABLED=true, mỗi API key có bucket riêng.
Nếu không có key (local use) → dùng bucket global.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from src.config import RATE_LIMIT_RPM
from src.logger import setup_logger

logger = setup_logger(__name__)

# Per-key config (có thể mở rộng qua env sau)
PER_KEY_RPM: int = RATE_LIMIT_RPM  # default = global limit
PER_KEY_ENABLED: bool = False


class _KeyBucket:
    __slots__ = ("tokens", "last", "capacity", "refill_rate")

    def __init__(self, capacity: float):
        self.capacity = capacity
        self.refill_rate = capacity / 60.0  # tokens per second
        self.tokens = capacity
        self.last = time.time()

    def try_take(self) -> bool:
        now = time.time()
        elapsed = now - self.last
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class PerKeyRateLimiter:
    """Per-API-key rate limiter (token bucket)."""

    def __init__(self) -> None:
        self._buckets: dict[str, _KeyBucket] = {}
        self._lock = asyncio.Lock()
        self._cleanup_at: float = time.time()

    async def check(self, key: str) -> bool:
        """
        Trả True nếu request được phép, False nếu bị rate limit.
        """
        if not PER_KEY_ENABLED or not key:
            return True
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _KeyBucket(PER_KEY_RPM)
                self._buckets[key] = bucket
            allowed = bucket.try_take()
            # periodic cleanup expired buckets
            now = time.time()
            if now - self._cleanup_at > 300:
                self._cleanup_at = now
                self._buckets = {
                    k: v for k, v in self._buckets.items() if now - v.last < 600
                }
            return allowed

    def snapshot(self) -> dict:
        return {
            "enabled": PER_KEY_ENABLED,
            "per_key_rpm": PER_KEY_RPM,
            "tracked_keys": len(self._buckets),
        }


per_key_limiter = PerKeyRateLimiter()
