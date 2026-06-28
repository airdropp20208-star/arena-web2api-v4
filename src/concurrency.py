"""
Concurrency control — tránh quá tải upstream Arena.

ConcurrencyGate: semaphore toàn cục + bounded queue (đợi chỗ trống, timeout).
Khi đầy, request vào hàng đợi (queue) — vượt MAX_QUEUE_SIZE → reject 503.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from src.config import (
    MAX_CONCURRENT_REQUESTS,
    MAX_QUEUE_SIZE,
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


gate = ConcurrencyGate()
