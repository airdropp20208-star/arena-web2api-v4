"""
Arena API client — tầng giao tiếp với arena.ai.

Endpoint: POST /nextjs-api/stream/create-evaluation

Tích hợp:
  - CookiePool     (xoay vòng cookie, health)
  - ModelRegistry  (UUID động)
  - ConversationManager (multi-turn thật)
  - SSEDecoder     (parse sự kiện mạnh)
  - Retry           (backoff + jitter + status-aware)
  - CircuitBreaker  (bảo vệ upstream)
  - RateLimiter     (RPM/TPM)
  - Metrics         (đếm request/token/latency)
  - Proxy rotation
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace

import httpx

from src.circuit_breaker import breaker
from src.config import (
    ARENA_VOTE_URL,
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
from src.recaptcha import get_recaptcha_token
from src.session import acquire_cookie, build_browser_headers, next_proxy
from src.sse_parser import ArenaEvent, SSEDecoder, parse_arena_event
from src.utils import backoff_delay, new_uuid

logger = setup_logger(__name__)


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


def build_direct_payload(plan: TurnPlan, recaptcha_token: str | None = None, *, modality: str = "chat") -> dict:
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


def build_battle_payload(plan: TurnPlan, recaptcha_token: str | None = None, *, modality: str = "chat") -> dict:
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
    if resp.status_code < 400:
        return
    if resp.status_code in (401, 403):
        raise ArenaAuthError(f"HTTP {resp.status_code} từ Arena.")
    if resp.status_code == 429:
        raise ArenaRateLimitError(retry_after=_retry_after(resp))
    if resp.status_code in RETRYABLE_STATUS:
        raise ArenaServerError(resp.status_code, f"HTTP {resp.status_code}")
    # 4xx khác — không retry
    raise ArenaError(resp.status_code, f"HTTP {resp.status_code}")


class ArenaClient:
    """Client singleton — mọi route dùng instance `client` này."""

    async def _stream_attempt(self, payload: dict) -> AsyncIterator[ArenaEvent]:
        """
        Một lần thử stream qua browser proxy (reCAPTCHA chỉ hoạt động trong browser).
        """
        from src.browser_proxy import stream_via_browser

        started = False
        async for text_chunk in stream_via_browser(payload):
            for sse in SSEDecoder().feed(text_chunk):
                ev = parse_arena_event(sse)
                if ev:
                    started = True
                    yield ev
        if not started:
            raise ArenaServerError(502, "Arena stream trả về rỗng (0 events).")

    @staticmethod
    async def _mark_cookie(entry, *, ok: bool) -> None:
        try:
            pool = await get_cookie_pool()
            if ok:
                await pool.mark_ok(entry)
            else:
                await pool.mark_failed(entry)
        except Exception:  # never let cookie bookkeeping kill the stream
            pass

    async def _stream_with_retry(self, payload: dict, *, label: str) -> AsyncIterator[ArenaEvent]:
        """
        Retry loop quanh streaming + AUTO-RECONNECT với content dedup.

          - connection / HTTP retryable  → backoff rồi thử lại (đổi cookie/proxy)
          - auth error / non-retryable    → throw ngay
          - mid-stream disconnect         → reconnect, bỏ qua content đã yield

        Dedup: theo dõi số ký tự đã yield cho mỗi stream-key (model_index).
        Khi reconnect, re-stream từ đầu nhưng chỉ emit phần *mới* chưa gửi
        → client không thấy nội dung lặp.
        """
        # emitted[key] = số ký tự đã gửi cho stream-key này (across attempts)
        emitted: dict[str, int] = {}
        last_exc: Exception | None = None

        for attempt in range(1, RETRY_ATTEMPTS + 1):
            # accumulator content của attempt hiện tại (reset mỗi lần thử)
            acc: dict[str, str] = {}
            try:
                async for ev in self._stream_attempt(payload):
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
                return  # hoàn tất sạch

            except ArenaAuthError:
                last_exc = ArenaError(403, "Auth/CF bị chặn.")
                logger.warning(f"[{label}] attempt {attempt}/{RETRY_ATTEMPTS}: auth/CF blocked")
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
          - client ngắt (GeneratorExit)      → neutral, KHÔNG mark (không phải lỗi upstream)
          - config/state error               → neutral, re-raise
          - lỗi upstream (httpx/ArenaError)  → breaker.failure()
        Trước đây dùng try/except/else → else KHÔNG chạy khi generator bị
        close() → breaker kẹt. Fix B1.
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

    async def stream_direct(self, plan: TurnPlan, *, modality: str = "chat") -> AsyncIterator[ArenaEvent]:
        recaptcha_token = await get_recaptcha_token()
        async for ev in self._stream_grounded(build_direct_payload(plan, recaptcha_token, modality=modality), label="direct"):
            yield ev

    async def stream_battle(self, plan: TurnPlan, *, modality: str = "chat") -> AsyncIterator[ArenaEvent]:
        recaptcha_token = await get_recaptcha_token()
        async for ev in self._stream_grounded(build_battle_payload(plan, recaptcha_token, modality=modality), label="battle"):
            yield ev

    async def submit_vote(self, conversation_id: str, vote: str) -> dict:
        """POST /nextjs-api/vote — gửi vote cho một battle."""
        entry = await acquire_cookie()
        headers = build_browser_headers()
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
