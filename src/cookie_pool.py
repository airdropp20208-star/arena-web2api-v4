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
        """
        Build cookie dict cho httpx.

        Arena lưu auth JWT trong cookie CHUNKED: `arena-auth-prod-v1.0` + `.1`.
        Chunked cookie là cơ chế Next.js khi JWT dài > 4096 bytes → browser tự
        ghép. Với httpx phải gửi cả 2 chunk với tên gốc (server Next.js sẽ tự join).

        Hỗ trợ cả 2 format:
          - Cũ: arena_auth là JWT string → gửi 1 cookie `arena-auth-prod-v1`
          - Mới: arena_auth là JSON `{"0": "...", "1": "..."}` hoặc string có
                 separator `|` → tách thành `.0` và `.1`
        """
        c: dict = {}
        if self.arena_auth:
            chunks = self._parse_auth_chunks(self.arena_auth)
            if chunks:
                # Chunked format: emit .0, .1, ...
                for i, ch in enumerate(chunks):
                    c[f"arena-auth-prod-v1.{i}"] = ch
            else:
                # Legacy: single cookie (không chunk)
                c["arena-auth-prod-v1"] = self.arena_auth
        if self.cf_clearance:
            c["cf_clearance"] = self.cf_clearance
        return c

    @staticmethod
    def _parse_auth_chunks(raw: str) -> list[str]:
        """
        Parse arena_auth thành list of chunks.
        Trả về [] nếu là legacy single-cookie format.

        Accepted formats:
          - JSON: '{"0":"...","1":"..."}' → [".0 value", ".1 value"]
          - Pipe: 'chunk0|chunk1'
          - Comma-separated: 'chunk0,chunk1' (legacy COOKIE_POOL format)
          - Nếu raw bắt đầu bằng 'base64-' → đây là chunk 0 thật từ browser,
            coi như single chunk (sẽ bị server reject nếu quá dài, nhưng giữ
            backwards-compat)
        """
        if not raw:
            return []
        # JSON format
        if raw.startswith("{"):
            try:
                import json
                d = json.loads(raw)
                # sort keys numerically: "0", "1", "2", ...
                keys = sorted([k for k in d.keys() if k.isdigit()], key=int)
                return [d[k] for k in keys]
            except Exception:
                return []
        # Pipe format
        if "|" in raw and not raw.startswith("base64-"):
            parts = [p.strip() for p in raw.split("|") if p.strip()]
            if len(parts) >= 2:
                return parts
        # Otherwise: single cookie (legacy)
        return []


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
        # Dedup cho refresh_from_extension
        self._refresh_lock = asyncio.Lock()
        self._last_refresh_at: float = 0.0
        self._last_refresh_ok: bool = False
        self._refresh_in_progress: bool = False
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

    async def refresh_from_extension(self, *, force: bool = False) -> bool:
        """
        Auto-refresh cookie from Kiwi Browser extension (if connected).

        Returns True if refreshed successfully (or recently refreshed within dedup window).
        Use when arena-auth expires — server requests fresh cookies via broker,
        updates the default entry in pool.

        Dedup: nếu đã refresh trong 30s qua, không refresh lại (tránh race khi
        nhiều request fail auth cùng lúc). force=True để bỏ dedup.
        """
        # Dedup lock — chỉ 1 refresh trong 30s
        now = time.time()
        async with self._refresh_lock:
            if not force and self._last_refresh_at > 0 and (now - self._last_refresh_at) < 30:
                logger.debug(
                    f"Cookie refresh skipped — recent refresh {now-self._last_refresh_at:.1f}s ago"
                )
                return self._last_refresh_ok

            # Check if refresh already in progress
            if self._refresh_in_progress:
                logger.debug("Cookie refresh already in progress, waiting...")
                # Wait for in-progress refresh to complete (max 30s)
                for _ in range(60):
                    if not self._refresh_in_progress:
                        return self._last_refresh_ok
                    await asyncio.sleep(0.5)
                logger.warning("Cookie refresh wait timed out")
                return self._last_refresh_ok

            self._refresh_in_progress = True

        try:
            from src.token_broker import broker
            from src.config import RECAPTCHA_SOLVER

            if RECAPTCHA_SOLVER != "extension" or not broker.is_extension_connected:
                self._last_refresh_at = now
                self._last_refresh_ok = False
                return False

            logger.info("Requesting fresh cookies from extension...")
            cookies = await broker.request_cookies(timeout=15.0)

            # Build new arena_auth string in JSON chunked format
            import json as _json
            chunks = {}
            for k, v in cookies.items():
                if k.startswith("arena-auth-prod-v1."):
                    chunk_idx = k.split(".")[-1]
                    chunks[chunk_idx] = v
            cf = cookies.get("cf_clearance", "")

            if not chunks or not cf:
                logger.error(f"Missing required cookies from extension: chunks={bool(chunks)}, cf={bool(cf)}")
                self._last_refresh_at = now
                self._last_refresh_ok = False
                return False

            arena_auth_json = _json.dumps(chunks)
            cf_clearance = cf

            # Update the default cookie entry (or create new one)
            async with self._lock:
                default_entry = next(
                    (e for e in self._entries if e.label == "default"),
                    None,
                )
                if default_entry:
                    default_entry.arena_auth = arena_auth_json
                    default_entry.cf_clearance = cf_clearance
                    default_entry.healthy = True
                    default_entry.fail_count = 0
                    default_entry.last_validated = time.time()
                    logger.info(f"Cookie '{default_entry.label}' refreshed from extension")
                else:
                    new_entry = CookieEntry(
                        arena_auth=arena_auth_json,
                        cf_clearance=cf_clearance,
                        label="default",
                    )
                    self._entries.append(new_entry)
                    logger.info("Added new 'default' cookie entry from extension")

            self._last_refresh_at = now
            self._last_refresh_ok = True
            return True
        except Exception as e:
            logger.error(f"Cookie refresh from extension failed: {e}")
            self._last_refresh_at = now
            self._last_refresh_ok = False
            return False
        finally:
            self._refresh_in_progress = False

    def snapshot(self) -> list[dict]:
        return [
            {
                "label": e.label,
                "healthy": e.healthy,
                "fail_count": e.fail_count,
                "requests": e.request_count,
                "last_used": int(e.last_used),
                "last_validated": int(e.last_validated),
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
