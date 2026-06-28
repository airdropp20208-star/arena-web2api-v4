"""
Metrics — đếm request, latency, token, lỗi theo model/endpoint.

Lưu trong RAM, expose qua /admin/metrics. Nhẹ, thread-safe (async lock).
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field

from src.config import METRICS_ENABLED
from src.logger import setup_logger

logger = setup_logger(__name__)


@dataclass
class _Counters:
    requests: int = 0
    successes: int = 0
    failures: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms_total: float = 0.0
    errors: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def snapshot(self) -> dict:
        avg = (self.latency_ms_total / self.requests) if self.requests else 0.0
        return {
            "requests": self.requests,
            "successes": self.successes,
            "failures": self.failures,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "avg_latency_ms": round(avg, 1),
            "errors": dict(self.errors),
        }


class Metrics:
    def __init__(self) -> None:
        self.enabled = METRICS_ENABLED
        self._by_model: dict[str, _Counters] = defaultdict(_Counters)
        self._lock = asyncio.Lock()
        self.started_at = time.time()

    async def record(
        self,
        *,
        model: str,
        ok: bool,
        latency_ms: float,
        tokens_in: int = 0,
        tokens_out: int = 0,
        error_type: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        async with self._lock:
            c = self._by_model[model]
            c.requests += 1
            c.latency_ms_total += latency_ms
            c.tokens_in += tokens_in
            c.tokens_out += tokens_out
            if ok:
                c.successes += 1
            else:
                c.failures += 1
                if error_type:
                    c.errors[error_type] += 1

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "uptime_sec": round(time.time() - self.started_at, 0),
            "by_model": {k: v.snapshot() for k, v in self._by_model.items()},
            "totals": {
                "requests": sum(c.requests for c in self._by_model.values()),
                "tokens": sum(c.tokens_in + c.tokens_out for c in self._by_model.values()),
            },
        }


metrics = Metrics()
