"""
Browser proxy — dùng Playwright để gọi Arena API.

Tự động:
1. Login qua /nextjs-api/sign-in/email
2. Generate reCAPTCHA token (anti-detection)
3. Gọi Arena API với retry (reCAPTCHA có xác suất)
4. Stream SSE response
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from src.logger import setup_logger

logger = setup_logger(__name__)

# Singleton browser instance
_browser_context: Any = None
_browser_page: Any = None
_browser_lock = asyncio.Lock()
_login_expiry: float = 0.0

RECAPTCHA_SITE_KEY = "6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0"
ARENA_ORIGIN = "https://arena.ai"
MAX_RETRIES = 3


def _uuid7() -> str:
    """Generate UUIDv7 (timestamp-based)."""
    now_ms = int(time.time() * 1000)
    rand_a = uuid.uuid4().int & 0xFFF
    rand_b = uuid.uuid4().int & 0x3FFFFFFFFFFFFFFF
    val = (now_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0x2 << 62) | rand_b
    return str(uuid.UUID(int=val))


async def _get_browser_page():
    """Get or create a Playwright browser page with anti-detection."""
    global _browser_context, _browser_page, _login_expiry

    async with _browser_lock:
        if _browser_page is not None:
            try:
                # Check if page is still alive
                await _browser_page.evaluate("1")
                return _browser_page
            except Exception:
                _browser_page = None

        from playwright.async_api import async_playwright

        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/tmp/pw-browsers")

        p = await async_playwright().start()
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 900})

        # Anti-detection
        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            delete navigator.__proto__.webdriver;
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            """
        )

        page = await context.new_page()
        await page.goto(ARENA_ORIGIN, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)

        _browser_context = context
        _browser_page = page
        _login_expiry = 0.0  # Force login on next use
        return page


async def _ensure_logged_in(page) -> bool:
    """Ensure we're logged in to Arena."""
    global _login_expiry

    if time.time() < _login_expiry:
        return True

    email = os.environ.get("ARENA_EMAIL", "tooltdshaha00001@gmail.com")
    password = os.environ.get("ARENA_PASSWORD", "")

    if not password:
        logger.error("ARENA_PASSWORD not set in environment")
        return False

    result = await page.evaluate(
        """
        async (args) => {
            const r = await fetch('/nextjs-api/sign-in/email', {
                method: 'POST', credentials: 'include',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({email: args.email, password: args.password, shouldLinkHistory: true})
            });
            return {status: r.status, body: await r.json()};
        }
        """,
        {"email": email, "password": password},
    )

    if result.get("status") == 200 and result.get("body", {}).get("success"):
        _login_expiry = time.time() + 3000  # Re-login every ~50 min
        logger.info("Arena login successful")
        return True

    logger.warning(f"Arena login failed: {result}")
    return False


async def _generate_recaptcha_token(page) -> str | None:
    """Generate reCAPTCHA v3 token with anti-detection."""
    try:
        token = await page.evaluate(
            """
            (siteKey) => {
                return new Promise((resolve, reject) => {
                    if (typeof grecaptcha === 'undefined' || !grecaptcha.enterprise) {
                        return reject(new Error('reCAPTCHA not loaded'));
                    }
                    grecaptcha.enterprise.ready(async () => {
                        try {
                            const t = await grecaptcha.enterprise.execute(siteKey, {action: 'chat_submit'});
                            resolve(t);
                        } catch(e) { reject(e); }
                    });
                });
            }
            """,
            RECAPTCHA_SITE_KEY,
        )
        return token
    except Exception as e:
        logger.warning(f"reCAPTCHA token generation failed: {e}")
        return None


async def _call_arena_api(page, payload: dict, token: str) -> dict:
    """Call Arena API from browser context."""
    payload["recaptchaV3Token"] = token

    return await page.evaluate(
        """
        (payload) => {
            return new Promise((resolve) => {
                const xhr = new XMLHttpRequest();
                xhr.open('POST', '/nextjs-api/stream/create-evaluation', true);
                xhr.setRequestHeader('Content-Type', 'application/json');
                xhr.withCredentials = true;
                xhr.onload = function() {
                    resolve({status: xhr.status, body: xhr.responseText.substring(0, 50000)});
                };
                xhr.onerror = function() { resolve({error: 'network error'}); };
                xhr.send(JSON.stringify(payload));
            });
        }
        """,
        payload,
    )


async def stream_via_browser(
    payload: dict,
    *,
    timeout: float = 120,
    modality: str = "chat",
) -> AsyncIterator[str]:
    """
    Gọi Arena API qua Playwright browser và yield SSE text chunks.
    Tự động login + generate reCAPTCHA + retry.
    """
    page = await _get_browser_page()

    # Ensure login
    if not await _ensure_logged_in(page):
        logger.error("Cannot login to Arena")
        return

    # Wait for reCAPTCHA to warm up
    await page.wait_for_timeout(5000)

    # Prepare payload with UUIDv7
    for key in ["id", "conversationId", "userMessageId", "modelAMessageId", "modelBMessageId"]:
        if key not in payload or not payload[key]:
            payload[key] = _uuid7()

    # Try with retry
    for attempt in range(1, MAX_RETRIES + 1):
        # Generate reCAPTCHA token
        token = await _generate_recaptcha_token(page)
        if not token:
            logger.warning(f"[attempt {attempt}] No reCAPTCHA token")
            await page.wait_for_timeout(2000)
            continue

        # Call API
        result = await _call_arena_api(page, payload, token)
        status = result.get("status", 0)
        body = result.get("body", result.get("error", ""))

        if status == 200:
            logger.info(f"Arena API success (attempt {attempt})")
            yield body
            return

        if status == 403 and "recaptcha" in body.lower():
            logger.warning(f"[attempt {attempt}] reCAPTCHA validation failed, retrying...")
            await page.wait_for_timeout(2000)
            continue

        # Non-retryable error
        logger.warning(f"Arena API error: {status} - {body[:200]}")
        return

    logger.error(f"Arena API failed after {MAX_RETRIES} attempts")
