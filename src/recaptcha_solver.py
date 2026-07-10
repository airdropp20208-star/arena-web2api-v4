"""
reCAPTCHA solver — abstraction layer cho 4 chiến lược:

  1. "skip"       — không gửi token (Approach A, hy vọng backend không enforce)
  2. "2captcha"   — gọi 2Captcha API để solve reCAPTCHA Enterprise v3
                    (chi phí $1-3/1000 solves, latency 10-30s)
  3. "browser"    — gen token qua Playwright (chỉ chạy được trên máy có display
                    thật, headless sẽ bị Google flag score thấp)
  4. "extension"  — ✅ RECOMMENDED cho ĐT/VPS free. Kiwi Browser (Android)
                    cài extension, extension gen token trong arena.ai tab
                    (real Chrome fingerprint = high score), gửi về server qua WS.

Auto-fallback: nếu "skip" fail với 403 reCAPTCHA → caller có thể retry với
strategy khác (xem client._stream_with_retry).

Token cache: 90s (reCAPTCHA v3 token valid ~120s, conservative).
"""

from __future__ import annotations

import asyncio
import time
from typing import Literal

from src.config import (
    RECAPTCHA_ACTION,
    RECAPTCHA_MIN_SCORE,
    RECAPTCHA_SITE_KEY,
    RECAPTCHA_SOLVE_TIMEOUT,
    RECAPTCHA_SOLVER,
    RECAPTCHA_TOKEN_TTL,
    TWO_CAPTCHA_API_KEY,
)
from src.logger import setup_logger

logger = setup_logger(__name__)

SolverStrategy = Literal["skip", "2captcha", "browser", "extension"]

# ── Module-level cache ─────────────────────────────────────────────────────
_cached_token: str | None = None
_cached_at: float = 0.0
_cache_lock = asyncio.Lock()


async def get_recaptcha_token(
    strategy: SolverStrategy | None = None,
    *,
    force_refresh: bool = False,
) -> str | None:
    """
    Lấy reCAPTCHA v3 token theo strategy config.

    Trả về:
      - token string nếu strategy != "skip" và solve thành công
      - None nếu strategy == "skip" hoặc solve fail (caller nên skip field)

    Cache: token cached RECAPTCHA_TOKEN_TTL giây. force_refresh=True để bỏ cache.
    Với "extension" strategy, cache thường skip (token single-use).
    """
    global _cached_token, _cached_at
    strat = strategy or RECAPTCHA_SOLVER  # type: ignore[assignment]
    strat = strat.lower().strip()  # type: ignore[union-attr]

    if strat == "skip":
        return None

    # Extension strategy: skip cache because tokens are single-use
    use_cache = strat != "extension"

    async with _cache_lock:
        now = time.time()
        if use_cache and not force_refresh and _cached_token and (now - _cached_at) < RECAPTCHA_TOKEN_TTL:
            logger.debug(f"reCAPTCHA token cache hit (age={now-_cached_at:.0f}s)")
            return _cached_token

        try:
            if strat == "2captcha":
                token = await _solve_via_2captcha()
            elif strat == "browser":
                token = await _solve_via_browser()
            elif strat == "extension":
                token = await _solve_via_extension()
            else:
                logger.warning(f"Unknown reCAPTCHA strategy: {strat!r}, falling back to skip")
                return None

            if token:
                if use_cache:
                    _cached_token = token
                    _cached_at = now
                logger.info(f"reCAPTCHA token acquired (strategy={strat}, len={len(token)})")
                return token
            logger.warning(f"reCAPTCHA solve returned None (strategy={strat})")
            return None
        except Exception as e:
            logger.error(f"reCAPTCHA solve error (strategy={strat}): {e}")
            return None


async def invalidate_token() -> None:
    """Xóa cache token — gọi khi Arena trả 403 reCAPTCHA failed."""
    global _cached_token
    async with _cache_lock:
        _cached_token = None


def current_strategy() -> SolverStrategy:
    return RECAPTCHA_SOLVER  # type: ignore[return-value]


# ── 2Captcha integration ───────────────────────────────────────────────────
async def _solve_via_2captcha() -> str | None:
    """
    Submit reCAPTCHA Enterprise v3 task → poll → return token.

    2Captcha API:
      POST https://2captcha.com/in.php — submit task
        params: key, method=userrecaptcha, googlekey, pageurl, action, version=v3,
                score, json=1
      GET https://2captcha.com/res.php — poll result
        params: key, action=get, id=<task_id>, json=1

    Returns token string (text response from 2Captcha is the g-recaptcha-response).
    """
    if not TWO_CAPTCHA_API_KEY:
        logger.error("TWO_CAPTCHA_API_KEY not set — cannot use 2captcha strategy")
        return None

    import httpx

    page_url = "https://arena.ai/"

    async with httpx.AsyncClient(timeout=30.0) as http:
        # Submit task
        submit_resp = await http.post(
            "https://2captcha.com/in.php",
            data={
                "key": TWO_CAPTCHA_API_KEY,
                "method": "userrecaptcha",
                "googlekey": RECAPTCHA_SITE_KEY,
                "pageurl": page_url,
                "action": RECAPTCHA_ACTION,
                "version": "v3",
                "score": RECAPTCHA_MIN_SCORE,
                "json": 1,
            },
        )
        submit_data = submit_resp.json()
        if submit_data.get("status") != 1:
            logger.error(f"2captcha submit failed: {submit_data}")
            return None

        task_id = submit_data["request"]
        logger.info(f"2captcha task submitted: id={task_id}, polling...")

        # Poll for result (2captcha recommends 5-10s wait between polls)
        for attempt in range(RECAPTCHA_SOLVE_TIMEOUT // 5):
            await asyncio.sleep(5)
            poll_resp = await http.get(
                "https://2captcha.com/res.php",
                params={
                    "key": TWO_CAPTCHA_API_KEY,
                    "action": "get",
                    "id": task_id,
                    "json": 1,
                },
            )
            poll_data = poll_resp.json()

            if poll_data.get("status") == 1:
                token = poll_data["request"]
                logger.info(f"2captcha solved in ~{(attempt+1)*5}s")
                return token
            if poll_data.get("request") != "CAPCHA_NOT_READY":
                logger.error(f"2captcha error: {poll_data}")
                return None
            # else: still processing, poll again

        logger.error(f"2captcha timeout after {RECAPTCHA_SOLVE_TIMEOUT}s")
        return None


# ── Browser-based solver (last resort, requires display) ───────────────────
_browser_page = None
_browser_lock = asyncio.Lock()
_login_expiry: float = 0.0


async def _solve_via_browser() -> str | None:
    """
    Gen reCAPTCHA v3 token qua Playwright.

    Yêu cầu:
      - DISPLAY env var set (Linux) hoặc chạy trên Windows/macOS có display
      - playwright + chromium installed
      - ARENA_EMAIL + ARENA_PASSWORD set trong env

    Cache login 50 phút, chỉ re-login khi hết hạn.
    """
    global _browser_page, _login_expiry

    from src.config import ARENA_EMAIL, ARENA_PASSWORD

    if not ARENA_EMAIL or not ARENA_PASSWORD:
        logger.error("ARENA_EMAIL/ARENA_PASSWORD not set — cannot use browser strategy")
        return None

    async with _browser_lock:
        # Initialize browser if needed
        if _browser_page is not None:
            try:
                await _browser_page.evaluate("1")
            except Exception:
                _browser_page = None

        if _browser_page is None:
            try:
                from playwright.async_api import async_playwright

                pw = await async_playwright().start()
                browser = await pw.chromium.launch(
                    headless=False,  # Must be False for reCAPTCHA score
                    args=[
                        "--no-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                    ],
                )
                ctx = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    ),
                    locale="en-US",
                    viewport={"width": 1280, "height": 900},
                )
                await ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                    "window.chrome={runtime:{}};"
                    "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
                )
                _browser_page = await ctx.new_page()
                await _browser_page.goto("https://arena.ai", wait_until="domcontentloaded", timeout=45000)
                await _browser_page.wait_for_timeout(3000)
                _login_expiry = 0.0  # force login
            except Exception as e:
                logger.error(f"Browser init failed: {e}")
                return None

        # Login if expired
        if time.time() >= _login_expiry:
            try:
                r = await _browser_page.evaluate(
                    """
                    async ({email, password}) => {
                        const r = await fetch('/nextjs-api/sign-in/email', {
                            method: 'POST', credentials: 'include',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({email, password, shouldLinkHistory: true})
                        });
                        let body = null; try { body = await r.json(); } catch(e) {}
                        return {status: r.status, body};
                    }
                    """,
                    {"email": ARENA_EMAIL, "password": ARENA_PASSWORD},
                )
                if r.get("status") == 200 and r.get("body", {}).get("success"):
                    _login_expiry = time.time() + 3000  # 50 min
                    logger.info("Browser login OK")
                else:
                    logger.error(f"Browser login failed: {r}")
                    return None
            except Exception as e:
                logger.error(f"Browser login error: {e}")
                return None

        # Wait for grecaptcha to load
        for _ in range(20):
            ready = await _browser_page.evaluate(
                "() => typeof grecaptcha !== 'undefined' && !!grecaptcha.enterprise"
            )
            if ready:
                break
            await _browser_page.wait_for_timeout(500)
        if not ready:
            logger.error("grecaptcha not loaded after 10s")
            return None

        # Generate token
        try:
            token = await _browser_page.evaluate(
                """
                (siteKey, action) => new Promise((resolve, reject) => {
                    grecaptcha.enterprise.ready(async () => {
                        try {
                            const t = await grecaptcha.enterprise.execute(siteKey, {action: action});
                            resolve(t);
                        } catch(e) { reject(e); }
                    });
                })
                """,
                {"siteKey": RECAPTCHA_SITE_KEY, "action": RECAPTCHA_ACTION},
            )
            return token
        except Exception as e:
            logger.error(f"grecaptcha.execute failed: {e}")
            return None


# ── Extension (Kiwi Browser) solver — HTTP polling, không WebSocket ─────────
async def _solve_via_extension() -> str | None:
    """
    Request token từ extension qua HTTP polling.

    Extension (Kiwi) poll GET /admin/poll mỗi 2s.
    Server trả token request → extension gen → POST /admin/token.
    Không cần WebSocket → không bị disconnect khi Android kill background.
    """
    from src.token_bridge import bridge

    try:
        token = await bridge.request_token(timeout=RECAPTCHA_SOLVE_TIMEOUT)
        return token
    except Exception as e:
        logger.error(f"Extension token request failed: {e}")
        return None
