"""
Integration test: tool calling qua HTTP endpoint đầy đủ + idempotency qua HTTP.
Chạy:  python3 tests/test_tools_integration.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import src.client as client_mod
from src.conversation_store import store
from src.sse_parser import ArenaEvent


def make_mock(reply_events):
    state = {"i": 0}

    async def fake_stream(self, payload):
        idx = min(state["i"], len(reply_events) - 1)
        state["i"] += 1
        for ev in reply_events[idx]:
            yield ev

    return fake_stream


TOOL_REPLY = [
    ArenaEvent(
        kind="delta",
        content=(
            "I will check the weather.\n"
            "<tool_call>\n"
            '{"name": "get_weather", "arguments": {"location": "Hanoi"}}\n'
            "</tool_call>"
        ),
    ),
    ArenaEvent(kind="done", finish_reason="stop"),
]
NORMAL_REPLY = [
    ArenaEvent(kind="delta", content="The capital of Vietnam is Hanoi."),
    ArenaEvent(kind="done", finish_reason="stop"),
]

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather",
            "parameters": {
                "type": "object",
                "properties": {"location": {"type": "string"}},
                "required": ["location"],
            },
        },
    }
]


def main():
    store._convs.clear()
    client_mod.ArenaClient._stream_attempt = make_mock([TOOL_REPLY, NORMAL_REPLY])

    with TestClient(__import__("main").app) as c:
        # ── 1. Tool call emitted (non-stream) ───────────────────────────
        print("=== TEST 1: tool calling (non-stream) ===")
        r = c.post(
            "/v1/chat/completions",
            json={
                "model": "claude-opus-4-6",
                "messages": [{"role": "user", "content": "weather in Hanoi?"}],
                "tools": TOOLS,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        choice = body["choices"][0]
        print("finish_reason:", choice["finish_reason"])
        msg = choice["message"]
        print("tool_calls:", json.dumps(msg.get("tool_calls"), indent=2))
        assert choice["finish_reason"] == "tool_calls"
        assert msg.get("tool_calls")
        tc = msg["tool_calls"][0]
        assert tc["function"]["name"] == "get_weather"
        assert json.loads(tc["function"]["arguments"]) == {"location": "Hanoi"}
        print("✓ tool_calls emitted in OpenAI format")

        # ── 2. Tool call streaming ──────────────────────────────────────
        print("\n=== TEST 2: tool calling (stream) ===")
        client_mod.ArenaClient._stream_attempt = make_mock([TOOL_REPLY])
        store._convs.clear()
        with c.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "claude-opus-4-6",
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": TOOLS,
                "stream": True,
            },
        ) as r:
            chunks = [ln[6:] for ln in r.iter_lines() if ln.startswith("data: ")]
        # find tool_calls chunk
        found_tool = False
        finish = None
        for ch in chunks:
            if ch == "[DONE]":
                continue
            d = json.loads(ch)
            if d.get("choices"):
                c0 = d["choices"][0]
                if c0.get("delta", {}).get("tool_calls"):
                    found_tool = True
                    print("streamed tool_call:", c0["delta"]["tool_calls"][0]["function"])
                if c0.get("finish_reason"):
                    finish = c0["finish_reason"]
        assert found_tool, "no tool_calls in stream"
        assert finish == "tool_calls", f"finish={finish}"
        print("✓ tool_calls streamed + finish_reason=tool_calls")

        # ── 3. Idempotency via header ───────────────────────────────────
        print("\n=== TEST 3: idempotency ===")
        client_mod.ArenaClient._stream_attempt = make_mock([NORMAL_REPLY])
        store._convs.clear()
        call_count = [0]
        orig = client_mod.ArenaClient._stream_attempt

        async def counting(self, payload):
            call_count[0] += 1
            async for ev in orig(self, payload):
                yield ev

        client_mod.ArenaClient._stream_attempt = counting

        body1 = c.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.4",
                "messages": [{"role": "user", "content": "capital of Vietnam?"}],
            },
            headers={"Idempotency-Key": "req-001"},
        ).json()
        first_calls = call_count[0]
        body2 = c.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.4",
                "messages": [{"role": "user", "content": "capital of Vietnam?"}],
            },
            headers={"Idempotency-Key": "req-001"},
        ).json()
        print(f"upstream calls: first={first_calls}, total={call_count[0]}")
        assert call_count[0] == first_calls, "idempotency should NOT hit upstream twice"
        assert body1["id"] == body2["id"], "should return cached response"
        print("✓ idempotency: same key → cached, upstream called once")

    print("\n" + "=" * 50)
    print("🎉 TOOL CALLING + IDEMPOTENCY INTEGRATION PASS")
    print("=" * 50)


if __name__ == "__main__":
    main()
