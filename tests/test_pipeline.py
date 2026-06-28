"""
Integration test — verify full pipeline với Arena stream được mock.

Chạy:  python3 tests/test_pipeline.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import src.client as client_mod
from src.conversation_store import store
from src.sse_parser import ArenaEvent

# ── Mock Arena stream ──────────────────────────────────────────────────────
_call_log: list[dict] = []


def make_mock(events_per_call):
    """events_per_call: list của list ArenaEvent — mỗi call lấy 1 list."""
    state = {"i": 0}

    async def fake_stream(self, payload):
        idx = min(state["i"], len(events_per_call) - 1)
        state["i"] += 1
        _call_log.append(
            {
                "mode": payload.get("mode"),
                "modelAId": payload.get("modelAId"),
                "content_len": len(payload["userMessage"]["content"]),
                "conversationId": payload.get("conversationId"),
            }
        )
        for ev in events_per_call[idx]:
            yield ev

    return fake_stream


DIRECT_REPLY = [
    ArenaEvent(kind="delta", content="Hello", role="assistant"),
    ArenaEvent(kind="delta", content=" there!"),
    ArenaEvent(kind="done", finish_reason="stop"),
]
DIRECT_REPLY_2 = [
    ArenaEvent(kind="delta", content="Yes, "),
    ArenaEvent(kind="delta", content="I remember."),
    ArenaEvent(kind="done", finish_reason="stop"),
]
BATTLE_REPLY = [
    ArenaEvent(kind="delta", content="Model A says hi", model_index="a"),
    ArenaEvent(kind="delta", content="Model B says hello", model_index="b"),
    ArenaEvent(kind="reveal", model_a="gpt-5.4", model_b="claude-opus-4-6"),
    ArenaEvent(kind="done", finish_reason="stop"),
]


def reset_mock(events):
    global _call_log
    _call_log = []
    client_mod.ArenaClient._stream_attempt = make_mock(events)
    store._convs.clear()


def main():
    with TestClient(__import__("main").app) as c:
        # ── 1. Direct chat (non-stream) ────────────────────────────────
        print("\n=== TEST 1: Direct chat (non-stream) ===")
        reset_mock([DIRECT_REPLY])
        r = c.post(
            "/v1/chat/completions",
            json={
                "model": "claude-opus-4-6",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        print("content:", repr(body["choices"][0]["message"]["content"]))
        print("usage:", body["usage"])
        assert body["choices"][0]["message"]["content"] == "Hello there!"
        assert body["usage"]["prompt_tokens"] > 0
        assert body["usage"]["completion_tokens"] > 0
        assert store.size == 1, f"expected 1 conversation, got {store.size}"
        print("✓ direct chat + conversation created + token usage")

        # ── 2. Multi-turn — second message should be continuation ──────
        print("\n=== TEST 2: Multi-turn continuation ===")
        reset_mock([DIRECT_REPLY, DIRECT_REPLY_2])
        # first turn
        r1 = c.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.4",
                "messages": [{"role": "user", "content": "remember the number 42"}],
            },
        )
        a1 = r1.json()["choices"][0]["message"]["content"]  # exact reply
        first_payload = dict(_call_log[-1])
        # second turn: append REAL assistant reply + new user msg
        r2 = c.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.4",
                "messages": [
                    {"role": "user", "content": "remember the number 42"},
                    {"role": "assistant", "content": a1},
                    {"role": "user", "content": "what number?"},
                ],
            },
        )
        second_payload = _call_log[-1]
        print("turn1 content_len:", first_payload["content_len"])
        print(
            "turn2 content_len:", second_payload["content_len"], "(should be SMALL = incremental)"
        )
        print("turn2 reply:", repr(r2.json()["choices"][0]["message"]["content"]))
        # KEY ASSERTION: turn 2 sends only the new message, not full history
        assert second_payload["content_len"] < first_payload["content_len"], (
            "Multi-turn không gửi incremental! (lỗi #2 chưa fix)"
        )
        assert second_payload["conversationId"] == first_payload["conversationId"], (
            "conversationId phải khớp giữa các turn!"
        )
        print("✓ multi-turn: incremental message + reused conversationId")

        # ── 3. Battle mode (non-stream) + reveal ───────────────────────
        print("\n=== TEST 3: Battle mode + reveal ===")
        reset_mock([BATTLE_REPLY])
        r = c.post(
            "/v1/battle",
            json={
                "messages": [{"role": "user", "content": "hello both"}],
            },
        )
        body = r.json()
        print("model_a:", body["model_a"]["model"], "->", repr(body["model_a"]["content"]))
        print("model_b:", body["model_b"]["model"], "->", repr(body["model_b"]["content"]))
        print("revealed:", body["revealed"])
        assert body["model_a"]["content"] == "Model A says hi"
        assert body["model_b"]["content"] == "Model B says hello"
        assert body["model_a"]["model"] == "gpt-5.4"
        assert body["revealed"] is True
        print("✓ battle: split responses + model reveal")

        # ── 4. Streaming direct ────────────────────────────────────────
        print("\n=== TEST 4: Streaming ===")
        reset_mock([DIRECT_REPLY])
        with c.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "claude-sonnet-4-6",
                "messages": [{"role": "user", "content": "stream test"}],
                "stream": True,
            },
        ) as r:
            chunks = []
            for line in r.iter_lines():
                if line.startswith("data: "):
                    chunks.append(line[6:])
        import json

        texts = []
        for ch in chunks:
            if ch == "[DONE]":
                continue
            d = json.loads(ch)
            if d.get("choices"):
                delta = d["choices"][0].get("delta", {})
                if delta.get("content"):
                    texts.append(delta["content"])
        streamed = "".join(texts)
        print("streamed:", repr(streamed))
        assert streamed == "Hello there!", streamed
        print("✓ streaming: OpenAI SSE chunks + [DONE]")

        # ── 5. Metrics recorded ────────────────────────────────────────
        print("\n=== TEST 5: Metrics ===")
        r = c.get("/admin/metrics")
        m = r.json()
        print("by_model keys:", list(m["by_model"].keys()))
        print("totals:", m["totals"])
        assert m["totals"]["requests"] >= 4
        print("✓ metrics recorded per model")

    print("\n" + "=" * 50)
    print("🎉 TẤT CẢ TEST PASS — pipeline hoạt động đúng!")
    print("=" * 50)


if __name__ == "__main__":
    main()
