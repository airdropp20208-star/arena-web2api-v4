"""
HTTP Polling Token Bridge — thay thế WebSocket.

Extension (Kiwi) poll http://127.0.0.1:8000/admin/poll mỗi 2s.
Server trả về token request nếu cần.
Extension gen token, POST về http://127.0.0.1:8000/admin/token.

Không cần WebSocket → không bị disconnect khi Android kill background.
Không cần broker-only.sh → 1 process, 1 lệnh.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Optional

from src.logger import setup_logger

logger = setup_logger(__name__)

# Token request queue — server thêm request vào đây, extension poll lấy ra
_pending_request: dict | None = None  # {"id": "...", "ts": ...}
_pending_response: dict | None = None  # {"id": "...", "token": "...", "ok": bool}
_lock = asyncio.Lock()
_token_count = 0
_last_request_at: float = 0.0


class HttpTokenBridge:
    """
    HTTP-based token bridge với pre-token caching.

    Flow pre-token (realtime):
      1. Extension tự gen token mỗi 80s → POST /admin/token (pre=true)
      2. Server cache token
      3. Chat request đến → server dùng cached token ngay (0ms latency)
      4. Nếu cache trống → fallback on-demand: server queue request, extension poll, gen, submit

    Pre-token TTL: 110s (token valid ~120s)
    """

    def __init__(self) -> None:
        self._pending_request: dict | None = None
        self._pending_response: dict | None = None
        self._future: asyncio.Future[str] | None = None
        self._lock = asyncio.Lock()
        self._token_count = 0
        self._last_poll_at: float = 0.0
        self._extension_connected: bool = False
        self._extension_last_seen: float = 0.0
        # Pre-token cache
        self._cached_token: str | None = None
        self._cached_token_at: float = 0.0
        self._pre_token_ttl: float = 110.0  # 110s

    @property
    def is_extension_connected(self) -> bool:
        """Extension "connected" nếu đã poll trong 5s qua."""
        if self._extension_last_seen == 0.0:
            return False
        return (time.time() - self._extension_last_seen) < 5.0

    @property
    def token_count(self) -> int:
        return self._token_count

    async def request_token(self, timeout: float = 30.0) -> str:
        """Server gọi: cần token. Dùng cached pre-token nếu có, fallback on-demand."""
        # Fast path: dùng cached pre-token nếu còn hạn
        async with self._lock:
            if self._cached_token and (time.time() - self._cached_token_at) < self._pre_token_ttl:
                token = self._cached_token
                self._cached_token = None  # consume
                logger.info(f"Using cached pre-token (age={time.time()-self._cached_token_at:.0f}s) — REALTIME")
                return token

        # Slow path: on-demand gen
        async with self._lock:
            request_id = uuid.uuid4().hex[:12]
            self._pending_request = {"id": request_id, "ts": int(time.time() * 1000)}
            self._pending_response = None
            self._future = asyncio.get_event_loop().create_future()
            logger.info(f"Token request {request_id} queued (no cached token), waiting for extension poll...")

        try:
            return await asyncio.wait_for(self._future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"Token request {request_id} timed out after {timeout}s")
            if not self.is_extension_connected:
                raise RuntimeError(
                    "Extension không poll — mở Kiwi Browser + extension + tab arena.ai"
                )
            raise
        finally:
            async with self._lock:
                self._pending_request = None
                self._future = None

    async def get_poll_response(self) -> dict:
        """Extension gọi GET /admin/poll. Trả về token request nếu có."""
        self._extension_last_seen = time.time()
        self._extension_connected = True

        async with self._lock:
            if self._pending_request:
                return {"need_token": True, **self._pending_request}
            return {"need_token": False}

    async def submit_token(self, request_id: str, token: str | None, ok: bool, error: str | None = None, pre: bool = False) -> dict:
        """Extension gọi POST /admin/token. Submit token đã gen."""
        self._extension_last_seen = time.time()

        # Pre-token: cache nó, không resolve future
        if pre and ok and token:
            async with self._lock:
                self._cached_token = token
                self._cached_token_at = time.time()
                self._token_count += 1
                logger.info(f"Pre-token cached (len={len(token)}) — ready for realtime use")
                return {"ok": True}

        # On-demand token: resolve pending future
        async with self._lock:
            if not self._future or self._pending_request is None:
                return {"ok": False, "error": "No pending request"}

            if self._pending_request.get("id") != request_id:
                return {"ok": False, "error": "Stale request id"}

            if ok and token and len(token) > 50:
                self._token_count += 1
                logger.info(f"Token received from extension (id={request_id}, len={len(token)})")
                if not self._future.done():
                    self._future.set_result(token)
                return {"ok": True}
            else:
                err = error or "Invalid token"
                logger.error(f"Extension token failed (id={request_id}): {err}")
                if not self._future.done():
                    self._future.set_exception(RuntimeError(f"Extension error: {err}"))
                return {"ok": False, "error": err}

    def snapshot(self) -> dict:
        return {
            "extension_connected": self.is_extension_connected,
            "token_count": self._token_count,
            "pending_request": self._pending_request is not None,
            "cached_token": self._cached_token is not None,
            "cached_token_age": int(time.time() - self._cached_token_at) if self._cached_token_at else -1,
            "last_poll_ago": int(time.time() - self._extension_last_seen) if self._extension_last_seen else -1,
            "transport": "http_poll",
        }


# Singleton
bridge = HttpTokenBridge()
