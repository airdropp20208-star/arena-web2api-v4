"""
Session: headers + cookies cho Arena API.

Giờ lấy cookie từ CookiePool (xoay vòng) thay vì 1 cookie tĩnh.
Giữ get_cookies() cho backwards-compat với code legacy.
"""

from __future__ import annotations

from src.config import ARENA_BASE, DEFAULT_USER_AGENT, PROXY, PROXY_POOL
from src.cookie_pool import CookieEntry, get_cookie_pool
from src.logger import setup_logger

logger = setup_logger(__name__)


def build_browser_headers(extra: dict | None = None) -> dict:
    """Headers giả lập browser thật cho request tới Arena."""
    headers = {
        "accept": "text/event-stream",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": ARENA_BASE,
        "referer": f"{ARENA_BASE}/",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": DEFAULT_USER_AGENT,
    }
    if extra:
        headers.update(extra)
    return headers


async def acquire_cookie() -> CookieEntry:
    """Lấy 1 cookie healthy từ pool."""
    pool = await get_cookie_pool()
    return await pool.acquire()


async def get_cookies() -> dict:
    """Backwards-compat: trả về cookie dict của entry kế tiếp."""
    entry = await acquire_cookie()
    return entry.as_cookies()


def get_headers(json_content: bool = True, extra: dict | None = None) -> dict:
    h = build_browser_headers(extra)
    if json_content:
        h["content-type"] = "application/json"
    return h


# ── Proxy rotation ──────────────────────────────────────────────────────────
_proxy_rr = 0


def next_proxy() -> str | None:
    """Trả về proxy kế tiếp (xoay vòng) hoặc PROXY đơn lẻ."""
    global _proxy_rr
    if PROXY_POOL:
        p = PROXY_POOL[_proxy_rr % len(PROXY_POOL)]
        _proxy_rr += 1
        return p
    return PROXY or None


# Legacy map — giữ để code cũ vẫn import được, nhưng không dùng nữa.
MODEL_ID_MAP: dict[str, str] = {}
