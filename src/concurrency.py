"""
Concurrency control — tránh quá tải upstream Arena.

ConcurrencyGate: semaphore toàn cục + bounded queue (đợi chỗ trống, timeout).
Khi đầy, request vào hàng đợi (queue) — vượt MAX_QUEUE_SIZE → reject 503.

ConversationLock: per-conversation lock — fix #23. Tránh 2 request cùng
conversation_id gửi đồng thời tới Arena (race condition server-side).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from src.config import (
    MAX_CONCURRENT_REQUESTS,
    MAX_QUEUE_SIZE,
    PER_CONVERSATION_LOCK,
)
from src.errors import ArenaWeb2APIError
from src.logger import setup_logger

logger = setup_logger(__name__)


class ConcurrencyGate:
    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self._waiting = 0
        self._active = 0
        self._lock = asyncio.Lock()

    @property
    def active(self) -> int:
        return self._active

    @property
    def waiting(self) -> int:
        return self._waiting

    @asynccontextmanager
    async def slot(self, queue_timeout: float = 30.0) -> AsyncIterator[None]:
        async with self._lock:
            if self._active + self._waiting >= MAX_CONCURRENT_REQUESTS + MAX_QUEUE_SIZE:
                raise ArenaWeb2APIError(
                    503,
                    f"Server quá tải ({self._active} active, {self._waiting} queued). Thử lại sau.",
                )
            self._waiting += 1
        acquired = False
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=queue_timeout)
            acquired = True
        except asyncio.TimeoutError:
            raise ArenaWeb2APIError(
                503, f"Queue timeout sau {queue_timeout}s (server quá tải)."
            ) from None
        finally:
            async with self._lock:
                self._waiting = max(0, self._waiting - 1)
                if acquired:
                    self._active += 1
        try:
            yield
        finally:
            if acquired:
                async with self._lock:
                    self._active = max(0, self._active - 1)
                self._sem.release()

    def snapshot(self) -> dict:
        return {
            "max_concurrent": MAX_CONCURRENT_REQUESTS,
            "max_queue": MAX_QUEUE_SIZE,
            "active": self._active,
            "waiting": self._waiting,
        }


class ConversationLockManager:
    """
    Per-conversation lock — fix #23.

    Khi 2 request cùng conversation_id tới đồng thời → Arena có thể race
    (reject 1, conflict state). Lock này serialize request theo conversation_id.

    Locks auto-evict sau 5 phút không dùng để tránh memory leak.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_used: dict[str, float] = {}
        self._cleanup_lock = asyncio.Lock()
        self._evict_ttl = 300.0  # 5 min

    @asynccontextmanager
    async def acquire(self, conversation_id: str | None) -> AsyncIterator[None]:
        """Acquire lock for conversation. None = no lock (new conversation)."""
        if not PER_CONVERSATION_LOCK or not conversation_id:
            yield
            return

        async with self._cleanup_lock:
            if conversation_id not in self._locks:
                self._locks[conversation_id] = asyncio.Lock()
            self._last_used[conversation_id] = asyncio.get_event_loop().time()
            lock = self._locks[conversation_id]

        async with lock:
            yield

        # Periodic cleanup (every 60s, lazy)
        await self._maybe_cleanup()

    async def _maybe_cleanup(self) -> None:
        """Evict locks not used in _evict_ttl seconds."""
        async with self._cleanup_lock:
            now = asyncio.get_event_loop().time()
            if now % 60 > 5:  # only ~once per minute
                return
            to_evict = [
                k for k, t in self._last_used.items()
                if (now - t) > self._evict_ttl
            ]
            for k in to_evict:
                # Only evict if not currently held
                lock = self._locks.get(k)
                if lock and not lock.locked():
                    self._locks.pop(k, None)
                    self._last_used.pop(k, None)

    def snapshot(self) -> dict:
        return {
            "enabled": PER_CONVERSATION_LOCK,
            "active_locks": len(self._locks),
            "held_locks": sum(1 for l in self._locks.values() if l.locked()),
        }


gate = ConcurrencyGate()
conv_locks = ConversationLockManager()
