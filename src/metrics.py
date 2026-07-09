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

    def to_prometheus(self) -> str:
        """Export metrics in Prometheus text format — fix #24."""
        if not self.enabled:
            return "# Metrics disabled (METRICS_ENABLED=false)\n"

        lines = [
            "# HELP arena_requests_total Total requests by model",
            "# TYPE arena_requests_total counter",
            "# HELP arena_tokens_total Total tokens by model (in/out)",
            "# TYPE arena_tokens_total counter",
            "# HELP arena_latency_ms_total Total latency in ms by model",
            "# TYPE arena_latency_ms_total counter",
            "# HELP arena_errors_total Total errors by type and model",
            "# TYPE arena_errors_total counter",
            "# HELP arena_uptime_seconds Server uptime",
            "# TYPE arena_uptime_seconds gauge",
        ]

        # Uptime
        uptime = time.time() - self.started_at
        lines.append(f'arena_uptime_seconds {uptime:.0f}')

        # Per-model metrics
        for model, c in self._by_model.items():
            # Escape model name for Prometheus label
            safe_model = model.replace("\\", "\\\\").replace('"', '\\"')
            label = f'model="{safe_model}"'
            lines.append(f'arena_requests_total{{{label},status="success"}} {c.successes}')
            lines.append(f'arena_requests_total{{{label},status="failure"}} {c.failures}')
            lines.append(f'arena_requests_total{{{label},status="total"}} {c.requests}')
            lines.append(f'arena_tokens_total{{{label},direction="in"}} {c.tokens_in}')
            lines.append(f'arena_tokens_total{{{label},direction="out"}} {c.tokens_out}')
            lines.append(f'arena_latency_ms_total{{{label}}} {c.latency_ms_total:.1f}')
            # Errors by type
            for err_type, count in c.errors.items():
                safe_err = err_type.replace("\\", "\\\\").replace('"', '\\"')
                err_label = f'model="{safe_model}",error_type="{safe_err}"'
                lines.append(f'arena_errors_total{{{err_label}}} {count}')

        return "\n".join(lines) + "\n"


metrics = Metrics()
