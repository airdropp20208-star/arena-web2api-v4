"""
reCAPTCHA token generator — sử dụng browser automation để generate token.

Do Arena yêu cầu reCAPTCHA Enterprise token cho mỗi request,
cần browser để generate token hợp lệ.
"""

from __future__ import annotations

import asyncio
import json
import time

from src.logger import setup_logger

logger = setup_logger(__name__)

# Cache token (token có hiệu lực ~2 phút)
_cached_token: str | None = None
_cached_at: float = 0.0
TOKEN_TTL = 100  # giây


async def get_recaptcha_token() -> str | None:
    """
    Lấy reCAPTCHA token từ browser.
    Trả về None nếu không lấy được.
    """
    global _cached_token, _cached_at

    now = time.time()

    # Kiểm tra cache
    if _cached_token and (now - _cached_at) < TOKEN_TTL:
        return _cached_token

    try:
        # Sử dụng agent-browser để lấy token
        token = await _fetch_token_from_browser()
        if token:
            _cached_token = token
            _cached_at = now
            logger.debug(f"Got reCAPTCHA token ({len(token)} chars)")
        return token
    except Exception as e:
        logger.warning(f"Failed to get reCAPTCHA token: {e}")
        return None


async def _fetch_token_from_browser() -> str | None:
    """Sử dụng agent-browser để lấy reCAPTCHA token."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "agent-browser", "eval",
            """
            (async () => {
                if (typeof grecaptcha === 'undefined' || !grecaptcha.enterprise) {
                    return null;
                }
                const token = await grecaptcha.enterprise.execute(
                    '6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0',
                    {action: 'submit'}
                );
                return token;
            })()
            """,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=15)

        if proc.returncode == 0 and stdout:
            result = json.loads(stdout.decode().strip())
            if result and isinstance(result, str) and len(result) > 100:
                return result
        return None
    except Exception as e:
        logger.debug(f"Browser token fetch failed: {e}")
        return None


def invalidate_token() -> None:
    """Xóa cache token."""
    global _cached_token
    _cached_token = None
