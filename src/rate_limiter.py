"""
Rate limiter — token bucket cho RPM + TPM.

  - RPM (requests/phút): refill liên tục theo thời gian.
  - TPM (tokens/phút): trừ sau khi biết số token (trước đó ước lượng bằng prompt tokens).

Nếu RATE_LIMIT_ENABLED=false → acquire() trả về ngay.
"""

from __future__ import annotations

import asyncio
import time

from src.config import RATE_LIMIT_ENABLED, RATE_LIMIT_RPM, RATE_LIMIT_TPM
from src.errors import RateLimitedError
from src.logger import setup_logger

logger = setup_logger(__name__)


class TokenBucket:
    def __init__(self, capacity: float, refill_per_sec: float):
        self.capacity = max(0.0, capacity)
        self.refill_per_sec = max(0.0, refill_per_sec)
        self.tokens = self.capacity
        self.last = time.time()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.time()
        elapsed = now - self.last
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        self.last = now

    async def take(self, n: float = 1.0, *, wait: bool = True) -> bool:
        async with self._lock:
            self._refill()
            if self.tokens >= n:
                self.tokens -= n
                return True
            if not wait:
                return False
            # đợi đủ token
            deficit = n - self.tokens
            sleep_for = deficit / self.refill_per_sec if self.refill_per_sec else 0
        if sleep_for > 0:
            logger.debug(f"Rate limit: đợi {sleep_for:.2f}s cho {n} token(s)")
            await asyncio.sleep(sleep_for)
        # re-check sau sleep — fix B11: bucket có thể bị vắt kiệt lúc chờ
        async with self._lock:
            self._refill()
            if self.tokens >= n:
                self.tokens -= n
                return True
            # vẫn thiếu → admit nhưng clamping (không âm) — trade-off chấp nhận
            self.tokens = max(0.0, self.tokens - n)
            return True


class RateLimiter:
    def __init__(self) -> None:
        self.enabled = RATE_LIMIT_ENABLED
        self.rpm_bucket = TokenBucket(RATE_LIMIT_RPM, RATE_LIMIT_RPM / 60.0)
        self.tpm_bucket = (
            TokenBucket(RATE_LIMIT_TPM, RATE_LIMIT_TPM / 60.0) if RATE_LIMIT_TPM > 0 else None
        )

    async def acquire_request(self) -> None:
        if not self.enabled:
            return
        ok = await self.rpm_bucket.take(1, wait=True)
        if not ok:
            raise RateLimitedError("Vượt giới hạn RPM nội bộ.")

    async def acquire_tokens(self, n: int) -> None:
        if not self.enabled or not self.tpm_bucket or n <= 0:
            return
        await self.tpm_bucket.take(n, wait=True)

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "rpm_limit": RATE_LIMIT_RPM,
            "tpm_limit": RATE_LIMIT_TPM,
            "rpm_tokens": round(self.rpm_bucket.tokens, 1),
            "tpm_tokens": (round(self.tpm_bucket.tokens, 1) if self.tpm_bucket else None),
        }


limiter = RateLimiter()
