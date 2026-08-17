"""Browser-context transport for Arena Desktop mode.

Chrome performs the request so Arena sees the same browser fingerprint, cookies,
referrer and TLS/browser context that produced the reCAPTCHA token.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from urllib.request import urlopen

import websocket

from src.sse_parser import ArenaEvent


class DesktopCDPTransport:
    def __init__(self, url: str, timeout: float = 120.0) -> None:
        self.url = url
        self.timeout = timeout
        self.port = int(os.getenv("DESKTOP_CDP_PORT", "9223"))

    def _targets(self) -> list[dict]:
        last_error = "unavailable"
        for host in ("127.0.0.1", "[::1]"):
            try:
                with urlopen(f"http://{host}:{self.port}/json/list", timeout=8) as response:
                    data = json.loads(response.read().decode("utf-8"))
                pages = [x for x in data if x.get("type") == "page"]
                arena_pages = [x for x in pages if str(x.get("url", "")).startswith("https://arena.ai")]
                if arena_pages:
                    return arena_pages
                if pages:
                    return pages
                last_error = "no page targets"
            except Exception as exc:
                last_error = type(exc).__name__
        raise RuntimeError(f"Chrome CDP unavailable: {last_error}")

    @staticmethod
    def _call(ws, sequence: list[int], method: str, params: dict | None = None) -> dict:
        sequence[0] += 1
        request_id = sequence[0]
        ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + 130.0
        while time.monotonic() < deadline:
            message = json.loads(ws.recv())
            if message.get("id") == request_id:
                return message
        raise TimeoutError(f"CDP call timed out: {method}")

    def _post_sync(self, payload: dict) -> tuple[int, str]:
        pages = self._targets()
        page = pages[0]
        ws = websocket.create_connection(
            page["webSocketDebuggerUrl"],
            timeout=15,
            origin="http://localhost",
            http_proxy_host=None,
            http_proxy_port=None,
        )
        sequence = [0]
        try:
            self._call(ws, sequence, "Runtime.enable")
            expression = """(async ({url, body}) => {
                try {
                    const response = await fetch(url, {
                        method: 'POST',
                        credentials: 'include',
                        headers: {'Content-Type': 'text/plain;charset=UTF-8', 'Accept': '*/*'},
                        body
                    });
                    return {status: response.status, body: await response.text()};
                } catch (error) {
                    return {status: 599, body: String(error && error.message || error)};
                }
            })"""
            result = self._call(
                ws,
                sequence,
                "Runtime.evaluate",
                {
                    "expression": f"{expression}({json.dumps({'url': self.url, 'body': json.dumps(payload, separators=(',', ':'))})})",
                    "awaitPromise": True,
                    "returnByValue": True,
                    "userGesture": True,
                },
            )
            value = result.get("result", {}).get("result", {}).get("value") or {}
            return int(value.get("status") or 599), str(value.get("body") or "")
        finally:
            ws.close()

    async def post(self, payload: dict) -> tuple[int, str]:
        return await asyncio.wait_for(asyncio.to_thread(self._post_sync, payload), timeout=self.timeout)

    @staticmethod
    def parse_flight(body: str):
        """Parse Arena's browser response lines such as a0:"text" and ad:{...}."""
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line or ":" not in line:
                continue
            prefix, encoded = line.split(":", 1)
            side = prefix[:1].lower()
            tag = prefix[1:].lower()
            try:
                value = json.loads(encoded)
            except (json.JSONDecodeError, TypeError):
                continue
            if tag in ("0", "1"):
                if isinstance(value, str) and value:
                    yield ArenaEvent(kind="delta", content=value, model_index=side, raw={"flight": prefix})
                elif isinstance(value, dict):
                    text = value.get("content") or value.get("text") or ""
                    if text:
                        yield ArenaEvent(kind="delta", content=str(text), model_index=side, raw=value)
            elif tag in ("d", "done", "end"):
                finish = value.get("finishReason") if isinstance(value, dict) else "stop"
                yield ArenaEvent(kind="done", finish_reason=finish or "stop", model_index=side, raw=value if isinstance(value, dict) else None)
            elif isinstance(value, list) and any(isinstance(x, dict) and x.get("type") == "heartbeat" for x in value):
                continue
            elif isinstance(value, dict) and value.get("type") in ("heartbeat", "metadata"):
                yield ArenaEvent(kind="metadata", metadata=value, model_index=side, raw=value)
