"""Real Chrome Desktop reCAPTCHA Enterprise v3 solver via CDP.

The launcher starts an isolated Chrome profile with a loopback DevTools port.
This module never logs page cookies, API keys, or generated token values.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from urllib.request import urlopen

import websocket


class DesktopCDPSolver:
    def __init__(self, site_key: str, action: str, timeout: float = 20.0) -> None:
        self.site_key = site_key
        self.action = action
        self.timeout = timeout
        self.port = int(os.getenv("DESKTOP_CDP_PORT", "9223"))
        self.connect_timeout = min(8.0, timeout)

    def _json_targets(self) -> list[dict]:
        errors: list[str] = []
        for host in ("127.0.0.1", "[::1]"):
            try:
                with urlopen(f"http://{host}:{self.port}/json/list", timeout=self.connect_timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                if isinstance(data, list):
                    return data
            except Exception as exc:
                errors.append(type(exc).__name__)
        raise RuntimeError(f"Chrome DevTools endpoint unavailable ({'/'.join(errors)})")

    @staticmethod
    def _call(ws, sequence: list[int], method: str, params: dict | None = None) -> dict:
        sequence[0] += 1
        request_id = sequence[0]
        ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            message = json.loads(ws.recv())
            if message.get("id") == request_id:
                return message
        raise TimeoutError(f"CDP call timed out: {method}")

    def _solve_sync(self) -> str | None:
        targets = self._json_targets()
        pages = [t for t in targets if t.get("type") == "page"]
        page = next((t for t in pages if str(t.get("url", "")).startswith("https://arena.ai")), None)
        if page is None and pages:
            page = pages[0]
        if page is None or not page.get("webSocketDebuggerUrl"):
            raise RuntimeError("No controllable Arena tab in Desktop Chrome")

        ws = websocket.create_connection(
            page["webSocketDebuggerUrl"],
            timeout=self.connect_timeout,
            origin="http://localhost",
            http_proxy_host=None,
            http_proxy_port=None,
        )
        sequence = [0]
        try:
            self._call(ws, sequence, "Runtime.enable")
            self._call(ws, sequence, "Page.enable")
            if not str(page.get("url", "")).startswith("https://arena.ai"):
                self._call(ws, sequence, "Page.navigate", {"url": "https://arena.ai/"})
                time.sleep(3.0)

            expression = """(async ({siteKey, action}) => {
                const deadline = Date.now() + 12000;
                while (!(window.grecaptcha && window.grecaptcha.enterprise)) {
                    if (Date.now() > deadline) return {ok:false, error:'grecaptcha not ready'};
                    await new Promise(resolve => setTimeout(resolve, 200));
                }
                try {
                    const token = await new Promise((resolve, reject) => {
                        window.grecaptcha.enterprise.ready(async () => {
                            try { resolve(await window.grecaptcha.enterprise.execute(siteKey, {action})); }
                            catch (error) { reject(error); }
                        });
                    });
                    return token && token.length >= 50 ? {ok:true, token} : {ok:false, error:'invalid token'};
                } catch (error) {
                    return {ok:false, error:String(error && error.message || error)};
                }
            })"""
            result = self._call(
                ws,
                sequence,
                "Runtime.evaluate",
                {
                    "expression": f"{expression}({json.dumps({'siteKey': self.site_key, 'action': self.action})})",
                    "awaitPromise": True,
                    "returnByValue": True,
                    "userGesture": True,
                },
            )
            remote = result.get("result", {}).get("result", {}).get("value") or {}
            if remote.get("ok") and isinstance(remote.get("token"), str):
                return remote["token"]
            raise RuntimeError(str(remote.get("error") or "grecaptcha returned no token"))
        finally:
            ws.close()

    async def solve(self) -> str | None:
        return await asyncio.wait_for(asyncio.to_thread(self._solve_sync), timeout=self.timeout)
