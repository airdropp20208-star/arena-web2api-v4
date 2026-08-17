from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.request import urlopen

import websocket

REPO = Path(__file__).resolve().parents[1]
PORT = int(os.getenv("DESKTOP_CDP_PORT", "9223"))

def env_cookies() -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = REPO / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("ARENA_AUTH_COOKIE="):
            values["auth"] = line.split("=", 1)[1]
        elif line.startswith("CF_CLEARANCE="):
            values["cf_clearance"] = line.split("=", 1)[1].strip()
    cookies: dict[str, str] = {}
    for index, value in (json.loads(values.get("auth", "{}")) or {}).items():
        if value:
            cookies[f"arena-auth-prod-v1.{index}"] = value
    if values.get("cf_clearance"):
        cookies["cf_clearance"] = values["cf_clearance"]
    return cookies

def call(ws, seq, method, params=None):
    seq[0] += 1
    ws.send(json.dumps({"id": seq[0], "method": method, "params": params or {}}))
    while True:
        message = json.loads(ws.recv())
        if message.get("id") == seq[0]:
            return message

def main() -> None:
    targets = json.loads(urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=10).read())
    page = next((t for t in targets if t.get("type") == "page" and str(t.get("url", "")).startswith("https://arena.ai")), None)
    if not page:
        raise RuntimeError("No Arena page is available for cookie seeding")
    cookies = env_cookies()
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=15, origin="http://localhost")
    seq = [0]
    try:
        call(ws, seq, "Network.enable")
        ok = 0
        for name, value in cookies.items():
            result = call(ws, seq, "Network.setCookie", {"name": name, "value": value, "domain": ".arena.ai", "path": "/", "secure": True, "sameSite": "Lax"})
            ok += int(bool(result.get("result", {}).get("success", False)))
        call(ws, seq, "Page.reload", {"ignoreCache": True})
        time.sleep(4)
        print(f"Arena Desktop cookies seeded: {ok}/{len(cookies)}")
    finally:
        ws.close()

if __name__ == "__main__":
    main()
