"""
Token Broker — WebSocket server cho arena-web2api.

Pipeline:
  1. Kiwi Browser extension connect WS tới broker (ws://localhost:8765)
  2. Khi client.py cần reCAPTCHA token → gọi request_token()
  3. Broker gửi WS message {"type":"need_token","id":"..."} tới extension
  4. Extension gen token trong arena.ai tab → gửi back {"type":"token","id":"...","token":"..."}
  5. Broker resolve future với token

Multi-session support: mỗi request có unique id, broker track pending requests.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Optional

from websockets.protocol import State as WsState

from src.logger import setup_logger

logger = setup_logger(__name__)

# Default WS URL — extension connects to this
DEFAULT_BROKER_HOST = "127.0.0.1"
DEFAULT_BROKER_PORT = 8765

# How long to wait for token from extension (seconds)
TOKEN_REQUEST_TIMEOUT = 30.0

# How often to ping extension to keep WS alive
# Fix #22: 10s (was 20s) — Android Doze mode timeout can be 60s+,
# shorter ping ensures connection stays alive through Doze cycles.
PING_INTERVAL = 10.0

# Token bucket: limit grecaptcha.enterprise.execute() calls to avoid Google rate-limit
# v3 Enterprise typically allows 1 req/sec sustained, burst ~5
MIN_TOKEN_INTERVAL = 1.5  # seconds between token requests (queued)
MAX_BURST = 3  # max 3 requests can be in-flight at once


class TokenBroker:
    """WebSocket server that bridges client.py and Kiwi Browser extension."""

    def __init__(self) -> None:
        self._server: Optional[asyncio.AbstractServer] = None
        self._extension_ws: Optional[object] = None
        self._extension_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._cookie_pending: dict[str, asyncio.Future[dict]] = {}
        self._relogin_pending: dict[str, asyncio.Future[bool]] = {}
        self._task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._token_count = 0
        self._connect_count = 0
        # Token bucket
        self._token_lock = asyncio.Lock()
        self._last_token_request_at: float = 0.0
        self._in_flight_tokens: int = 0

    @property
    def is_extension_connected(self) -> bool:
        if self._extension_ws is None:
            return False
        try:
            return self._extension_ws.protocol.state == WsState.OPEN
        except AttributeError:
            # Fallback for older websockets API
            try:
                return not self._extension_ws.closed
            except AttributeError:
                return False

    @property
    def token_count(self) -> int:
        return self._token_count

    async def start(self, host: str = DEFAULT_BROKER_HOST, port: int = DEFAULT_BROKER_PORT) -> None:
        """Start WS server. Idempotent."""
        if self._server is not None:
            return
        import websockets

        self._server = await websockets.serve(
            self._handler,
            host,
            port,
            ping_interval=None,  # we handle our own pings
            ping_timeout=None,
            max_size=2**20,  # 1MB — token can be 2KB but messages are small
        )
        self._ping_task = asyncio.create_task(self._ping_loop())
        logger.info(f"Token broker listening on ws://{host}:{port} (extension connects here)")

    async def stop(self) -> None:
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
            self._ping_task = None
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        # Fail all pending requests
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("Token broker shutting down"))
        self._pending.clear()
        for fut in list(self._cookie_pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("Token broker shutting down"))
        self._cookie_pending.clear()
        for fut in list(self._relogin_pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("Token broker shutting down"))
        self._relogin_pending.clear()
        logger.info("Token broker stopped")

    async def _handler(self, ws) -> None:
        """Handle a single WS connection (only 1 extension expected)."""
        peer = ws.remote_address
        self._connect_count += 1
        logger.info(f"Extension connected from {peer}")

        async with self._extension_lock:
            # If another extension is already connected, disconnect the old one
            if self._extension_ws is not None and self.is_extension_connected:
                logger.warning("New extension connection replacing existing one")
                try:
                    await self._extension_ws.close()
                except Exception:
                    pass
            self._extension_ws = ws

        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON from extension: {raw[:200]}")
                    continue

                msg_type = msg.get("type")
                msg_id = msg.get("id")

                if msg_type == "hello":
                    logger.info(
                        f"Extension hello: agent={msg.get('agent')}, version={msg.get('version')}"
                    )
                elif msg_type == "pong":
                    pass  # just keeping connection alive
                elif msg_type == "status":
                    logger.info(
                        f"Extension status: hasArenaTab={msg.get('hasArenaTab')}, "
                        f"tabCount={msg.get('tabCount')}"
                    )
                elif msg_type == "token":
                    await self._resolve_token(msg_id, msg)
                elif msg_type == "cookies":
                    await self._resolve_cookies(msg_id, msg)
                elif msg_type == "relogin_result":
                    await self._resolve_relogin(msg_id, msg)
                else:
                    logger.debug(f"Unknown message type: {msg_type}")
        except Exception as e:
            logger.error(f"Extension handler error: {e}")
        finally:
            async with self._extension_lock:
                if self._extension_ws is ws:
                    self._extension_ws = None
            logger.info(f"Extension disconnected from {peer}")

    async def _resolve_token(self, msg_id: Optional[str], msg: dict) -> None:
        """Resolve pending token request with extension's response."""
        if not msg_id:
            logger.warning(f"Token message without id: {msg}")
            return
        fut = self._pending.pop(msg_id, None)
        if fut is None:
            logger.warning(f"Token response for unknown id: {msg_id}")
            return
        if fut.done():
            return
        if msg.get("ok"):
            token = msg.get("token")
            if token and len(token) > 100:
                self._token_count += 1
                logger.info(f"Token received for {msg_id} (len={len(token)})")
                fut.set_result(token)
            else:
                fut.set_exception(
                    RuntimeError(f"Extension returned invalid token (len={len(token) if token else 0})")
                )
        else:
            err = msg.get("error", "unknown error")
            logger.error(f"Extension returned error for {msg_id}: {err}")
            fut.set_exception(RuntimeError(f"Extension error: {err}"))

    async def _resolve_cookies(self, msg_id: Optional[str], msg: dict) -> None:
        """Resolve pending cookie request with extension's response."""
        if not msg_id:
            return
        fut = self._cookie_pending.pop(msg_id, None)
        if fut is None or fut.done():
            return
        if msg.get("ok"):
            cookies = msg.get("cookies", {})
            if cookies:
                logger.info(f"Cookies received for {msg_id} (keys={list(cookies.keys())})")
                fut.set_result(cookies)
            else:
                fut.set_exception(RuntimeError("Extension returned empty cookies"))
        else:
            err = msg.get("error", "unknown error")
            logger.error(f"Extension returned cookie error for {msg_id}: {err}")
            fut.set_exception(RuntimeError(f"Extension cookie error: {err}"))

    async def _resolve_relogin(self, msg_id: Optional[str], msg: dict) -> None:
        """Resolve pending relogin request."""
        if not msg_id:
            return
        fut = self._relogin_pending.pop(msg_id, None)
        if fut is None or fut.done():
            return
        ok = bool(msg.get("ok"))
        logger.info(f"Relogin result for {msg_id}: ok={ok}")
        fut.set_result(ok)

    async def request_token(self, timeout: float = TOKEN_REQUEST_TIMEOUT) -> str:
        """
        Request a fresh reCAPTCHA token from extension.

        Token bucket: ensures max MIN_TOKEN_INTERVAL between requests, max
        MAX_BURST concurrent. Prevents Google rate-limit when many requests
        arrive simultaneously.

        Raises:
          RuntimeError: if no extension connected
          asyncio.TimeoutError: if extension doesn't respond in time
        """
        if not self.is_extension_connected:
            raise RuntimeError(
                "No Kiwi Browser extension connected. "
                "Open Kiwi → install extension → open arena.ai → check popup shows 'Connected'."
            )

        # Token bucket: enforce min interval between requests
        async with self._token_lock:
            # Wait if too many in flight
            while self._in_flight_tokens >= MAX_BURST:
                logger.debug(f"Token bucket full ({self._in_flight_tokens}/{MAX_BURST}), waiting...")
                await asyncio.sleep(0.3)

            # Wait if last request was too recent
            now = time.time()
            wait_time = self._last_token_request_at + MIN_TOKEN_INTERVAL - now
            if wait_time > 0:
                logger.debug(f"Token bucket: waiting {wait_time:.1f}s for rate limit")
                await asyncio.sleep(wait_time)

            self._last_token_request_at = time.time()
            self._in_flight_tokens += 1

        try:
            request_id = uuid.uuid4().hex[:12]
            fut: asyncio.Future[str] = asyncio.get_event_loop().create_future()
            self._pending[request_id] = fut

            async with self._extension_lock:
                ws = self._extension_ws
            if ws is None or not self.is_extension_connected:
                raise RuntimeError("Extension disconnected during request")

            await ws.send(json.dumps({
                "type": "need_token",
                "id": request_id,
                "ts": int(time.time() * 1000),
            }))
            logger.info(f"Token request {request_id} sent to extension")

            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            logger.error(f"Token request {request_id} timed out after {timeout}s")
            raise
        except Exception:
            self._pending.pop(request_id, None)
            raise
        finally:
            async with self._token_lock:
                self._in_flight_tokens = max(0, self._in_flight_tokens - 1)

    async def request_cookies(self, timeout: float = 15.0) -> dict[str, str]:
        """
        Request fresh Arena cookies from extension.

        Extension extracts cookies via chrome.cookies.get API.
        Returns dict with keys: arena-auth-prod-v1.0, arena-auth-prod-v1.1,
        cf_clearance, __cf_bm, user_country_code.

        Use this when arena-auth expires (~1-2 weeks) or when server returns 401.
        """
        if not self.is_extension_connected:
            raise RuntimeError("No extension connected for cookie refresh")

        request_id = uuid.uuid4().hex[:12]
        fut: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self._cookie_pending[request_id] = fut

        try:
            async with self._extension_lock:
                ws = self._extension_ws
            if ws is None or not self.is_extension_connected:
                raise RuntimeError("Extension disconnected during cookie request")

            await ws.send(json.dumps({
                "type": "need_cookies",
                "id": request_id,
                "ts": int(time.time() * 1000),
            }))
            logger.info(f"Cookie request {request_id} sent to extension")

            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._cookie_pending.pop(request_id, None)
            logger.error(f"Cookie request {request_id} timed out")
            raise
        except Exception:
            self._cookie_pending.pop(request_id, None)
            raise

    async def request_relogin(self, timeout: float = 20.0) -> bool:
        """
        Request extension to relogin to arena.ai (refresh arena-auth cookie).

        Use this when arena-auth expires. Returns True if relogin successful.
        """
        if not self.is_extension_connected:
            raise RuntimeError("No extension connected for relogin")

        request_id = uuid.uuid4().hex[:12]
        fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
        self._relogin_pending[request_id] = fut

        try:
            async with self._extension_lock:
                ws = self._extension_ws
            if ws is None or not self.is_extension_connected:
                raise RuntimeError("Extension disconnected during relogin request")

            await ws.send(json.dumps({
                "type": "relogin",
                "id": request_id,
                "ts": int(time.time() * 1000),
            }))
            logger.info(f"Relogin request {request_id} sent to extension")

            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._relogin_pending.pop(request_id, None)
            logger.error(f"Relogin request {request_id} timed out")
            raise
        except Exception:
            self._relogin_pending.pop(request_id, None)
            raise

    async def _ping_loop(self) -> None:
        """Send periodic pings to keep WS alive (Kiwi may throttle background)."""
        while True:
            try:
                await asyncio.sleep(PING_INTERVAL)
                async with self._extension_lock:
                    ws = self._extension_ws
                if ws is not None and self.is_extension_connected:
                    try:
                        await ws.send(json.dumps({
                            "type": "ping",
                            "id": uuid.uuid4().hex[:8],
                            "ts": int(time.time() * 1000),
                        }))
                    except Exception as e:
                        logger.debug(f"Ping failed: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Ping loop error: {e}")

    def snapshot(self) -> dict:
        return {
            "extension_connected": self.is_extension_connected,
            "token_count": self._token_count,
            "connect_count": self._connect_count,
            "pending_requests": len(self._pending),
        }


# Singleton
broker = TokenBroker()
