"""
Cookie pool — quản lý nhiều cookie (account) của Arena.

Tính năng:
  - Xoay vòng (round-robin) giữa các cookie healthy
  - Đếm fail → tự đánh dấu unhealthy khi vượt threshold
  - Health-check định kỳ (tuỳ chọn, COOKIE_AUTO_REFRESH)
  - Single-cookie legacy vẫn hoạt động qua ARENA_AUTH_COOKIE/CF_CLEARANCE
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from src.config import (
    ARENA_AUTH,
    ARENA_MODELS_URL,
    CF_CLEARANCE,
    COOKIE_AUTO_REFRESH,
    COOKIE_FAIL_THRESHOLD,
    COOKIE_HEALTH_TTL,
    COOKIE_POOL_RAW,
    DEFAULT_USER_AGENT,
)
from src.logger import setup_logger

logger = setup_logger(__name__)


@dataclass
class CookieEntry:
    arena_auth: str
    cf_clearance: str = ""
    label: str = "cookie-0"
    healthy: bool = True
    fail_count: int = 0
    request_count: int = 0
    last_used: float = 0.0
    last_validated: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    def as_cookies(self) -> dict:
        c: dict = {}
        if self.arena_auth:
            c["arena-auth-prod-v1"] = self.arena_auth
        if self.cf_clearance:
            c["cf_clearance"] = self.cf_clearance
        return c


def _parse_pool(raw: str) -> list[tuple[str, str]]:
    """'auth1|cf1,auth2|cf2' → [(auth1, cf1), (auth2, cf2)]."""
    entries = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "|" in item:
            a, c = item.split("|", 1)
        else:
            a, c = item, ""
        a, c = a.strip(), c.strip()
        if a:
            entries.append((a, c))
    return entries


class CookiePool:
    """Pool singleton-ish; dùng get_cookie_pool()."""

    def __init__(self) -> None:
        self._entries: list[CookieEntry] = []
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None
        self._build()

    def _build(self) -> None:
        parsed = _parse_pool(COOKIE_POOL_RAW)
        seen_auths: set[str] = set()
        if parsed:
            for i, (a, c) in enumerate(parsed):
                if a not in seen_auths:
                    seen_auths.add(a)
                    self._entries.append(CookieEntry(a, c, label=f"pool-{i}"))
        # luôn có entry từ single-cookie config (trừ khi đã có trong pool)
        if ARENA_AUTH and ARENA_AUTH not in seen_auths:
            self._entries.append(CookieEntry(ARENA_AUTH, CF_CLEARANCE, label="default"))
        if not self._entries:
            logger.warning("⚠️  Cookie pool trống — set ARENA_AUTH_COOKIE trong .env")

    @property
    def size(self) -> int:
        return len(self._entries)

    def healthy_count(self) -> int:
        return sum(1 for e in self._entries if e.healthy)

    async def acquire(self) -> CookieEntry:
        """
        Lấy cookie healthy — least-recently-used (fix B10).
        Round-robin cũ không ổn định khi healthy list thay đổi (cookie vào/ra unhealthy).
        LRU chọn healthy có last_used nhỏ nhất → phân phối đều và ổn định.
        """
        async with self._lock:
            if not self._entries:
                from src.errors import NoCookiesError

                raise NoCookiesError()
            healthy = [e for e in self._entries if e.healthy]
            if not healthy:
                logger.error("Toàn bộ cookie trong pool đều unhealthy — khôi phục để thử lại.")
                for e in self._entries:
                    e.healthy = True
                    e.fail_count = 0
                healthy = self._entries
            # least-recently-used: cookie chưa dùng lâu nhất
            entry = min(healthy, key=lambda e: e.last_used)
            entry.request_count += 1
            entry.last_used = time.time()
            return entry

    async def mark_ok(self, entry: CookieEntry) -> None:
        async with self._lock:
            entry.fail_count = 0
            entry.healthy = True
            entry.last_validated = time.time()

    async def mark_failed(self, entry: CookieEntry, *, auth_fail: bool = False) -> None:
        async with self._lock:
            entry.fail_count += 1
            if auth_fail or entry.fail_count >= COOKIE_FAIL_THRESHOLD:
                entry.healthy = False
                logger.warning(
                    f"Cookie '{entry.label}' → unhealthy "
                    f"(fail={entry.fail_count}, auth_fail={auth_fail})"
                )

    async def _validate(self, entry: CookieEntry) -> bool:
        """Health-check nhẹ: GET /nextjs-api/models với cookie này."""
        import httpx

        headers = {
            "accept": "application/json",
            "user-agent": DEFAULT_USER_AGENT,
        }
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(
                    ARENA_MODELS_URL, headers=headers, cookies=entry.as_cookies()
                )
            ok = resp.status_code < 400
            if ok:
                await self.mark_ok(entry)
            else:
                await self.mark_failed(entry, auth_fail=resp.status_code in (401, 403))
            return ok
        except Exception as e:
            logger.debug(f"Validate cookie '{entry.label}' lỗi: {e}")
            return False

    async def validate_all(self) -> dict:
        """Validate toàn bộ pool — trả về báo cáo."""
        results = {}
        tasks = [self._validate(e) for e in self._entries]
        outcomes = await asyncio.gather(*tasks, return_exceptions=False)
        for e, ok in zip(self._entries, outcomes, strict=True):
            results[e.label] = "healthy" if ok else "unhealthy"
        return results

    async def start_refresh_loop(self) -> None:
        if not COOKIE_AUTO_REFRESH or self._refresh_task:
            return

        async def loop():
            logger.info(f"Cookie auto-refresh bật (mỗi {COOKIE_HEALTH_TTL}s)")
            while True:
                await asyncio.sleep(COOKIE_HEALTH_TTL)
                try:
                    await self.validate_all()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"Refresh loop lỗi: {e}")

        self._refresh_task = asyncio.create_task(loop())

    async def stop(self) -> None:
        import contextlib

        if self._refresh_task:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._refresh_task
            self._refresh_task = None

    def snapshot(self) -> list[dict]:
        return [
            {
                "label": e.label,
                "healthy": e.healthy,
                "fail_count": e.fail_count,
                "requests": e.request_count,
                "last_used": int(e.last_used),
                "has_auth": bool(e.arena_auth),
                "has_cf": bool(e.cf_clearance),
            }
            for e in self._entries
        ]


# ── Module-level singleton ──────────────────────────────────────────────────
_pool: CookiePool | None = None
_pool_lock = asyncio.Lock()


async def get_cookie_pool() -> CookiePool:
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                _pool = CookiePool()
    return _pool
