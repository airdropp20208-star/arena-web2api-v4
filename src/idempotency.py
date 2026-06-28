"""
Idempotency — cache response theo `Idempotency-Key` + single-flight.

Cho request non-streaming: cùng key → trả cùng response (trong TTL).
Single-flight: 2 request đồng thời cùng key → request thứ 2 chờ request
thứ 1 xong rồi nhận cached result (fix B4 — TOCTOU race).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from src.config import IDEMPOTENCY_ENABLED, IDEMPOTENCY_TTL
from src.logger import setup_logger

logger = setup_logger(__name__)


class _Entry:
    __slots__ = ("expires_at", "value")

    def __init__(self, value: Any, ttl: int):
        self.value = value
        self.expires_at = time.time() + ttl


class _Inflight:
    """Đánh dấu 1 request đang chạy cho key này."""

    __slots__ = ("event",)

    def __init__(self) -> None:
        self.event = asyncio.Event()


class IdempotencyStore:
    def __init__(self) -> None:
        self._store: dict[str, _Entry] = {}
        self._inflight: dict[str, _Inflight] = {}
        self._lock = asyncio.Lock()
        self._enabled = IDEMPOTENCY_ENABLED

    def enabled(self) -> bool:
        return self._enabled

    async def get_cached(self, key: str) -> Any:
        """Trả cached value hoặc None (đã hết hạn thì xoá)."""
        if not self._enabled or not key:
            return None
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if time.time() > entry.expires_at:
                del self._store[key]
                return None
            return entry.value

    async def put(self, key: str, value: Any) -> None:
        if not self._enabled or not key:
            return
        async with self._lock:
            self._store[key] = _Entry(value, IDEMPOTENCY_TTL)
            # giải phóng inflight marker
            inflight = self._inflight.pop(key, None)
            if len(self._store) > 1000:
                now = time.time()
                self._store = {k: v for k, v in self._store.items() if v.expires_at > now}
        if inflight:
            inflight.event.set()

    async def acquire(self, key: str) -> Any | _Inflight | None:
        """
        Single-flight entry point (fix B4):
          - Trả cached value nếu có      → caller return ngay
          - Trả None nếu caller sở hữu   → caller chạy upstream rồi put()
          - Trả _Inflight nếu đang chạy  → caller chờ inflight.event rồi get_cached
        """
        if not self._enabled or not key:
            return None
        # fast path: đã có cache?
        cached = await self.get_cached(key)
        if cached is not None:
            return cached
        async with self._lock:
            # double-check sau lock
            entry = self._store.get(key)
            if entry is not None and time.time() <= entry.expires_at:
                return entry.value
            if key in self._inflight:
                return self._inflight[key]
            self._inflight[key] = _Inflight()
            return None

    async def release(self, key: str) -> None:
        """Hủy inflight marker khi request lỗi (không put được)."""
        if not self._enabled or not key:
            return
        async with self._lock:
            inflight = self._inflight.pop(key, None)
        if inflight:
            inflight.event.set()

    async def wait_for(self, inflight: _Inflight) -> None:
        """Chờ request đang chạy cho cùng key."""
        await inflight.event.wait()

    def snapshot(self) -> dict:
        return {
            "enabled": self._enabled,
            "ttl_sec": IDEMPOTENCY_TTL,
            "cached": len(self._store),
            "inflight": len(self._inflight),
        }


idempotency = IdempotencyStore()
