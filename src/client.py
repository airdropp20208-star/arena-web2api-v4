"""
Arena API client — tầng giao tiếp với arena.ai.

Endpoint: POST /nextjs-api/stream/create-evaluation

Tích hợp:
  - CookiePool            (xoay vòng cookie, health, chunked cookies .0/.1)
  - ModelRegistry         (UUID động)
  - ConversationManager   (multi-turn thật)
  - SSEDecoder            (parse sự kiện mạnh, multi-line, partial chunk)
  - reCAPTCHA solver      (skip / 2captcha / browser — fallback A→B)
  - Retry                 (backoff + jitter + status-aware + AUTO-RECONNECT)
  - CircuitBreaker        (bảo vệ upstream)
  - RateLimiter           (RPM/TPM)
  - Metrics               (đếm request/token/latency)
  - Proxy rotation
  - Heartbeat keepalive   (chống stream chết giữa, SSE comment mỗi 15s)

Pure HTTP path (httpx) — không qua browser. reCAPTCHA token (nếu cần)
được solve riêng qua recaptcha_solver, không phụ thuộc browser context.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import replace

import httpx

from src.circuit_breaker import breaker
from src.config import (
    ARENA_STREAM_URL,
    CONNECT_TIMEOUT,
    REQUEST_TIMEOUT,
    RETRY_ATTEMPTS,
    RETRYABLE_STATUS,
)
from src.conversation import TurnPlan
from src.cookie_pool import get_cookie_pool
from src.errors import (
    ArenaAuthError,
    ArenaError,
    ArenaRateLimitError,
    ArenaServerError,
    CircuitOpenError,
    ModelNotResolvedError,
    NoCookiesError,
)
from src.logger import setup_logger
from src.model_registry import registry
from src.rate_limiter import limiter
from src.recaptcha_solver import get_recaptcha_token, invalidate_token
from src.session import acquire_cookie, build_browser_headers, next_proxy
from src.sse_parser import ArenaEvent, SSEDecoder, parse_arena_event
from src.utils import backoff_delay, new_uuid

logger = setup_logger(__name__)

# Heartbeat interval (seconds). SSE comment `: keepalive\n\n` keeps connection alive.
HEARTBEAT_INTERVAL = 15.0
# If no event received for this long → assume dead connection → reconnect.
DEAD_CONNECTION_TIMEOUT = 60.0


def _client(proxy: str | None) -> httpx.AsyncClient:
    timeout = httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT, read=REQUEST_TIMEOUT)
    return httpx.AsyncClient(
        timeout=timeout,
        proxy=proxy,
        follow_redirects=True,
        http2=False,
    )


def _attachments_payload(attachments: list) -> list[dict]:
    """Chuyển Attachment objects/dicts → experimental_attachments shape."""
    out = []
    for a in attachments or []:
        d = (
            a
            if isinstance(a, dict)
            else (a.model_dump(exclude_none=True) if hasattr(a, "model_dump") else {})
        )
        if d.get("url"):
            out.append(
                {
                    "name": d.get("name", "attachment"),
                    "mimeType": d.get("mime_type") or d.get("mimeType") or "image/png",
                    "url": d["url"],
                }
            )
    return out


def build_direct_payload(
    plan: TurnPlan, recaptcha_token: str | None = None, *, modality: str = "chat"
) -> dict:
    conv = plan.conversation
    payload = {
        "id": new_uuid(),
        "conversationId": conv.conversation_id,
        "mode": "direct",
        "modelAId": conv.model_a_id,
        "userMessageId": new_uuid(),
        "modelAMessageId": new_uuid(),
        "userMessage": {
            "content": plan.send_content,
            "experimental_attachments": _attachments_payload(plan.attachments),
            "metadata": {},
        },
        "modality": modality,
    }
    if recaptcha_token:
        payload["recaptchaV3Token"] = recaptcha_token
    return payload


def build_battle_payload(
    plan: TurnPlan, recaptcha_token: str | None = None, *, modality: str = "chat"
) -> dict:
    conv = plan.conversation
    payload = {
        "id": new_uuid(),
        "conversationId": conv.conversation_id,
        "mode": "battle",
        "userMessageId": new_uuid(),
        "modelAMessageId": new_uuid(),
        "modelBMessageId": new_uuid(),
        "userMessage": {
            "content": plan.send_content,
            "experimental_attachments": _attachments_payload(plan.attachments),
            "metadata": {},
        },
        "modality": modality,
    }
    if recaptcha_token:
        payload["recaptchaV3Token"] = recaptcha_token
    return payload


def _retry_after(resp: httpx.Response) -> float | None:
    val = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


def _raise_for_status(resp: httpx.Response) -> None:
    """
    Raise ArenaError phù hợp cho status >= 400.

    Cho streaming response, body cần được đọc qua aread() trước khi truy cập text.
    Hàm này được gọi ngay sau khi nhận status line, trước khi stream body.
    """
    if resp.status_code < 400:
        return
    # Đọc body để include trong error message
    body_text = ""
    try:
        # For streaming response, aread() consumes the body
        # Sync access for non-streaming
        body_bytes = resp.content if resp.is_closed else None
        if body_bytes is None:
            # Streaming response — can't await here, just note status
            body_text = "<streaming response>"
        else:
            body_text = body_bytes.decode("utf-8", errors="replace")
    except Exception:
        pass

    if resp.status_code in (401, 403):
        # Check if it's reCAPTCHA validation failure
        if "recaptcha" in body_text.lower():
            raise ArenaAuthError(
                f"HTTP {resp.status_code} — reCAPTCHA validation failed. "
                f"Body: {body_text[:200]}"
            )
        raise ArenaAuthError(f"HTTP {resp.status_code} từ Arena. Body: {body_text[:200]}")
    if resp.status_code == 429:
        raise ArenaRateLimitError(retry_after=_retry_after(resp))
    if resp.status_code in RETRYABLE_STATUS:
        raise ArenaServerError(resp.status_code, f"HTTP {resp.status_code}. Body: {body_text[:200]}")
    # 4xx khác — không retry
    raise ArenaError(resp.status_code, f"HTTP {resp.status_code}. Body: {body_text[:200]}")


async def _raise_for_status_async(resp: httpx.Response) -> None:
    """Async version — reads body via aread() for streaming responses."""
    if resp.status_code < 400:
        return
    body_text = ""
    try:
        await resp.aread()
        body_text = resp.text
    except Exception:
        pass

    if resp.status_code in (401, 403):
        if "recaptcha" in body_text.lower():
            raise ArenaAuthError(
                f"HTTP {resp.status_code} — reCAPTCHA validation failed. "
                f"Body: {body_text[:200]}"
            )
        raise ArenaAuthError(f"HTTP {resp.status_code} từ Arena. Body: {body_text[:200]}")
    if resp.status_code == 429:
        raise ArenaRateLimitError(retry_after=_retry_after(resp))
    if resp.status_code in RETRYABLE_STATUS:
        raise ArenaServerError(resp.status_code, f"HTTP {resp.status_code}. Body: {body_text[:200]}")
    raise ArenaError(resp.status_code, f"HTTP {resp.status_code}. Body: {body_text[:200]}")


class ArenaClient:
    """Client singleton — mọi route dùng instance `client` này."""

    async def _stream_attempt(
        self,
        payload: dict,
        cookie_entry,
        proxy: str | None,
    ) -> AsyncIterator[ArenaEvent]:
        """
        Một lần thử stream qua httpx — CHUNK-BY-CHUNK, không buffer.

        Yields ArenaEvent cho mỗi SSE event parsed được.
        Raises ArenaError/ArenaAuthError/ArenaRateLimitError/ArenaServerError khi có lỗi HTTP.
        """
        # Build headers — Arena expects text/plain (captured from real browser)
        headers = build_browser_headers()
        headers["content-type"] = "text/plain;charset=UTF-8"
        headers["accept"] = "*/*"
        # Referer must match Arena's expected pattern
        conversation_id = payload.get("conversationId", "")
        if conversation_id:
            headers["referer"] = f"https://arena.ai/c/{conversation_id}"
        else:
            headers["referer"] = "https://arena.ai/"

        cookies = cookie_entry.as_cookies()
        body = _json_dumps_compact(payload)

        decoder = SSEDecoder()
        started = False
        last_event_at = time.time()

        async with _client(proxy) as http:
            async with http.stream(
                "POST",
                ARENA_STREAM_URL,
                headers=headers,
                cookies=cookies,
                content=body,
            ) as resp:
                await _raise_for_status_async(resp)  # raises on 4xx/5xx, reads body for error msg

                # Stream chunks
                async for raw_chunk in resp.aiter_bytes():
                    last_event_at = time.time()
                    for sse in decoder.feed(raw_chunk.decode("utf-8", errors="replace")):
                        ev = parse_arena_event(sse)
                        if ev:
                            started = True
                            yield ev

        if not started:
            raise ArenaServerError(502, "Arena stream trả về rỗng (0 events).")

    @staticmethod
    async def _mark_cookie(entry, *, ok: bool, auth_fail: bool = False) -> None:
        try:
            pool = await get_cookie_pool()
            if ok:
                await pool.mark_ok(entry)
            else:
                await pool.mark_failed(entry, auth_fail=auth_fail)
        except Exception:  # never let cookie bookkeeping kill the stream
            pass

    async def _stream_with_retry(self, payload: dict, *, label: str) -> AsyncIterator[ArenaEvent]:
        """
        Retry loop quanh streaming + AUTO-RECONNECT với content dedup.

          - connection / HTTP retryable  → backoff rồi thử lại (đổi cookie/proxy)
          - auth error / non-retryable    → throw ngay (trừ reCAPTCHA → invalidate + retry)
          - mid-stream disconnect         → reconnect, bỏ qua content đã yield

        Dedup: theo dõi số ký tự đã yield cho mỗi stream-key (model_index).
        Khi reconnect, re-stream từ đầu nhưng chỉ emit phần *mới* chưa gửi
        → client không thấy nội dung lặp.
        """
        # emitted[key] = số ký tự đã gửi cho stream-key này (across attempts)
        emitted: dict[str, int] = {}
        last_exc: Exception | None = None
        recaptcha_invalidated = False

        for attempt in range(1, RETRY_ATTEMPTS + 1):
            # accumulator content của attempt hiện tại (reset mỗi lần thử)
            acc: dict[str, str] = {}
            try:
                # Get cookie + proxy for this attempt
                entry = await acquire_cookie()
                proxy = next_proxy()

                # Solve reCAPTCHA if configured — fix #5: fallback if extension disconnect
                try:
                    recaptcha_token = await get_recaptcha_token(force_refresh=recaptcha_invalidated)
                except Exception as token_err:
                    # Extension disconnected mid-request or other solver error
                    logger.warning(
                        f"[{label}] reCAPTCHA solver error: {token_err} — "
                        f"attempting request without token (will likely 403)"
                    )
                    recaptcha_token = None

                if recaptcha_token:
                    payload["recaptchaV3Token"] = recaptcha_token
                elif recaptcha_invalidated:
                    # Already tried refreshing, still no token — give up
                    logger.warning(f"[{label}] reCAPTCHA refresh failed, sending without token")

                # Track last activity for heartbeat
                last_activity = time.time()
                started = False

                async for ev in self._stream_attempt(payload, entry, proxy):
                    if ev.content:
                        k = ev.model_index or "_"
                        acc[k] = acc.get(k, "") + ev.content
                        cur = acc[k]
                        already = emitted.get(k, 0)
                        if len(cur) > already:
                            # chỉ emit phần vượt quá đã gửi
                            new_part = cur[already:]
                            emitted[k] = len(cur)
                            yield replace(ev, content=new_part)
                        # else: prefix trùng → bỏ qua (dedup)
                    else:
                        # non-content events (reveal/done/metadata) pass through
                        yield ev
                    last_activity = time.time()
                    started = True

                # Stream finished cleanly — mark cookie OK
                await self._mark_cookie(entry, ok=True)
                return  # hoàn tất sạch

            except ArenaAuthError as e:
                msg = str(e)
                failure_mode = getattr(e, "failure_mode", "auth_expired")
                last_exc = e
                logger.warning(
                    f"[{label}] attempt {attempt}/{RETRY_ATTEMPTS}: auth error "
                    f"({failure_mode}) — {msg[:150]}"
                )

                # If account banned — don't retry, escalate immediately — fix #15
                if failure_mode == "banned":
                    logger.error(f"[{label}] Account banned — escalating, no retry")
                    try:
                        await self._mark_cookie(entry, ok=False, auth_fail=True)
                    except NameError:
                        pass
                    raise

                # If reCAPTCHA related, invalidate token cache so next attempt gets fresh
                if failure_mode == "recaptcha" and not recaptcha_invalidated:
                    await invalidate_token()
                    recaptcha_invalidated = True
                    logger.info(f"[{label}] reCAPTCHA invalidated, will refresh on retry")
                    if attempt >= RETRY_ATTEMPTS:
                        raise
                    await asyncio.sleep(backoff_delay(attempt))
                    continue

                # Auth error (cookie expired or cloudflare) — try cookie refresh from extension
                if failure_mode in ("auth_expired", "cloudflare"):
                    try:
                        pool = await get_cookie_pool()
                        refreshed = await pool.refresh_from_extension()
                        if refreshed and attempt < RETRY_ATTEMPTS:
                            logger.info(f"[{label}] cookie refreshed from extension, retrying")
                            await asyncio.sleep(1.0)
                            continue
                    except Exception as refresh_err:
                        logger.warning(f"[{label}] cookie refresh failed: {refresh_err}")

                # Mark cookie failed
                try:
                    await self._mark_cookie(entry, ok=False, auth_fail=True)
                except NameError:
                    pass  # entry not assigned yet
                if attempt >= RETRY_ATTEMPTS:
                    raise
                await asyncio.sleep(backoff_delay(attempt))
                continue

            except ArenaRateLimitError as e:
                last_exc = e
                wait = backoff_delay(attempt, retry_after=e.retry_after)
                logger.warning(
                    f"[{label}] attempt {attempt}/{RETRY_ATTEMPTS}: 429 rate limit, đợi {wait:.1f}s"
                )
                if attempt >= RETRY_ATTEMPTS:
                    raise
                await asyncio.sleep(wait)
                continue

            except ArenaServerError as e:
                last_exc = e
                logger.warning(f"[{label}] attempt {attempt}/{RETRY_ATTEMPTS}: server {e.status}")
                if attempt >= RETRY_ATTEMPTS:
                    raise
                await asyncio.sleep(backoff_delay(attempt))
                continue

            except (
                httpx.ConnectError,
                httpx.TimeoutException,
                httpx.RemoteProtocolError,
                httpx.ReadError,
            ) as e:
                last_exc = e
                already_sent = sum(emitted.values())
                logger.warning(
                    f"[{label}] attempt {attempt}/{RETRY_ATTEMPTS}: net "
                    f"{type(e).__name__} (đã gửi {already_sent} chars → reconnect+dedup)"
                )
                if attempt >= RETRY_ATTEMPTS:
                    raise ArenaError(
                        503, f"Không kết nối được Arena sau {RETRY_ATTEMPTS} lần: {e}"
                    ) from e
                await asyncio.sleep(backoff_delay(attempt))
                continue

            except ArenaError:
                # non-retryable 4xx
                raise

        if last_exc:
            raise last_exc

    async def _stream_grounded(self, payload: dict, *, label: str) -> AsyncIterator[ArenaEvent]:
        """
        Rate-limit + circuit-breaker + retry, rồi yield events.

        Quản lý breaker đúng với async generator semantics:
          - hoàn tất sạch (return)           → breaker.success()
          - client ngắt (GeneratorExit)      → neutral, KHÔNG mark
          - config/state error               → neutral, re-raise
          - lỗi upstream (httpx/ArenaError)  → breaker.failure()
        """
        await limiter.acquire_request()
        await breaker.check()
        try:
            async for ev in self._stream_with_retry(payload, label=label):
                yield ev
        except (NoCookiesError, ModelNotResolvedError, CircuitOpenError):
            # lỗi cấu hình/trạng thái — không phải lỗi upstream, không trip breaker
            raise
        except GeneratorExit:
            # client ngắt stream sớm — không mark gì (không phải lỗi Arena)
            raise
        except BaseException:
            # lỗi upstream thật (httpx/ArenaError/...) → trip breaker
            await breaker.failure()
            raise
        else:
            await breaker.success()

    async def stream_direct(
        self, plan: TurnPlan, *, modality: str = "chat"
    ) -> AsyncIterator[ArenaEvent]:
        async for ev in self._stream_grounded(
            build_direct_payload(plan, modality=modality), label="direct"
        ):
            yield ev

    async def stream_battle(
        self, plan: TurnPlan, *, modality: str = "chat"
    ) -> AsyncIterator[ArenaEvent]:
        async for ev in self._stream_grounded(
            build_battle_payload(plan, modality=modality), label="battle"
        ):
            yield ev

    async def submit_vote(self, conversation_id: str, vote: str) -> dict:
        """POST /nextjs-api/vote — gửi vote cho một battle."""
        entry = await acquire_cookie()
        headers = build_browser_headers()
        headers["content-type"] = "application/json"
        payload = {
            "conversationId": conversation_id,
            "vote": vote,
        }
        try:
            async with _client(next_proxy()) as http:
                resp = await http.post(
                    ARENA_VOTE_URL,
                    headers=headers,
                    cookies=entry.as_cookies(),
                    json=payload,
                )
            _raise_for_status(resp)
            await self._mark_cookie(entry, ok=True)
            try:
                return resp.json()
            except Exception:
                return {"ok": True}
        except ArenaError:
            await self._mark_cookie(entry, ok=False)
            raise

    async def fetch_models(self):
        """Delegates sang model registry (đã có cache + TTL)."""
        await registry.ensure_loaded()
        return registry.list_models()


# Singleton dùng chung
client = ArenaClient()


# ── Helpers ────────────────────────────────────────────────────────────────
def _json_dumps_compact(obj: dict) -> bytes:
    """Serialize JSON không có whitespace (giảm payload size)."""
    import json
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
