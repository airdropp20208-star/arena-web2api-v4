"""
Circuit breaker — bảo vệ khi upstream Arena lỗi liên tục.

Trạng thái:
  CLOSED    → mọi request đi bình thường, đếm failure
  OPEN      → fail đủ threshold, reject ngay (tránh dồn request)
  HALF_OPEN → sau cooldown, cho tối đa CB_HALF_OPEN_MAX probe requests
              thử lại → thành công → CLOSED, lỗi → OPEN lại
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import TypeVar

from src.config import (
    CB_COOLDOWN,
    CB_ENABLED,
    CB_FAILURE_THRESHOLD,
    CB_HALF_OPEN_MAX,
)
from src.errors import CircuitOpenError
from src.logger import setup_logger

logger = setup_logger(__name__)

T = TypeVar("T")


class State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, name: str = "arena") -> None:
        self.name = name
        self.state = State.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_probes = 0  # số probe đang chạy trong HALF_OPEN
        self._lock = asyncio.Lock()

    def _cooldown_left(self) -> float:
        return max(0.0, CB_COOLDOWN - (time.time() - self._opened_at))

    async def check(self) -> None:
        """
        Raise CircuitOpenError nếu phải reject request.
        - OPEN: reject (trừ khi hết cooldown → chuyển HALF_OPEN)
        - HALF_OPEN: reject nếu đã đủ CB_HALF_OPEN_MAX probe (fix B2)
        """
        if self.state == State.OPEN:
            left = self._cooldown_left()
            if left <= 0:
                async with self._lock:
                    if self.state == State.OPEN:
                        logger.info(f"[CB:{self.name}] OPEN → HALF_OPEN (thử lại)")
                        self.state = State.HALF_OPEN
                        self._half_open_probes = 0
            else:
                raise CircuitOpenError(left)
        if self.state == State.HALF_OPEN:
            async with self._lock:
                if self._half_open_probes >= CB_HALF_OPEN_MAX:
                    raise CircuitOpenError(self._cooldown_left())
                self._half_open_probes += 1

    async def success(self) -> None:
        async with self._lock:
            self._failures = 0
            self._half_open_probes = max(0, self._half_open_probes - 1)
            if self.state in (State.HALF_OPEN, State.OPEN):
                logger.info(f"[CB:{self.name}] → CLOSED")
            self.state = State.CLOSED

    async def failure(self) -> None:
        async with self._lock:
            self._failures += 1
            self._half_open_probes = max(0, self._half_open_probes - 1)
            if self.state == State.HALF_OPEN or self._failures >= CB_FAILURE_THRESHOLD:
                self._trip()

    def _trip(self) -> None:
        self.state = State.OPEN
        self._opened_at = time.time()
        logger.warning(
            f"[CB:{self.name}] → OPEN (failures={self._failures}, cooldown={CB_COOLDOWN}s)"
        )

    async def call(self, fn: Callable[..., Awaitable[T]], *args, **kwargs) -> T:
        """Chạy async fn qua breaker. Raise CircuitOpenError nếu phải reject."""
        if CB_ENABLED:
            await self.check()
        try:
            result = await fn(*args, **kwargs)
        except CircuitOpenError:
            raise
        except Exception:
            if CB_ENABLED:
                await self.failure()
            raise
        else:
            if CB_ENABLED:
                await self.success()
            return result

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "failures": self._failures,
            "half_open_probes": self._half_open_probes,
            "cooldown_left": round(self._cooldown_left(), 1),
        }


# Singleton — dùng chung cho upstream Arena
breaker = CircuitBreaker("arena")
