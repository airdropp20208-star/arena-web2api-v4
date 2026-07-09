"""
Unit tests cho SSE parser + retry backoff.
Chạy:  python3 tests/test_sse.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import RETRY_MAX_DELAY
from src.sse_parser import SSEDecoder, parse_arena_event
from src.utils import backoff_delay


def feed_all(text: str):
    dec = SSEDecoder()
    out = []
    for sse in dec.feed(text):
        out.append(sse)
    for sse in dec.flush():
        out.append(sse)
    return out


def test_multiline_data():
    """Nhiều dòng `data:` phải được ghép bằng \\n."""
    raw = "data: line1\ndata: line2\n\n"
    evs = feed_all(raw)
    assert len(evs) == 1
    assert evs[0].data == "line1\nline2"
    print("✓ multi-line data joined by newline")


def test_partial_chunks():
    """Chunk cắt giữa chừng phải được buffer đúng."""
    dec = SSEDecoder()
    part1 = list(dec.feed('data: {"content":"hel'))
    part2 = list(dec.feed('lo"}\n\n'))
    assert part1 == []  # chưa hoàn chỉnh
    assert len(part2) == 1
    ae = parse_arena_event(part2[0])
    assert ae.content == "hello", ae.content
    print("✓ partial chunk buffering")


def test_comment_and_event_type():
    raw = ": this is a comment\nevent: ping\ndata: {}\n\n"
    evs = feed_all(raw)
    assert len(evs) == 1
    assert evs[0].event == "ping"
    assert evs[0].comment == "this is a comment"
    print("✓ comment + event type")


def test_openai_delta():
    raw = (
        "data: "
        + json.dumps({"choices": [{"delta": {"content": "Hi", "role": "assistant"}}]})
        + "\n\n"
    )
    ae = parse_arena_event(feed_all(raw)[0])
    assert ae.kind == "delta"
    assert ae.content == "Hi"
    assert ae.role == "assistant"
    print("✓ OpenAI delta shape")


def test_finish_reason():
    raw = "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "length"}]}) + "\n\n"
    ae = parse_arena_event(feed_all(raw)[0])
    assert ae.finish_reason == "length"
    print("✓ finish_reason extracted")


def test_done_marker():
    ae = parse_arena_event(feed_all("data: [DONE]\n\n")[0])
    assert ae.kind == "done"
    assert ae.finish_reason == "stop"
    print("✓ [DONE] marker")


def test_error_event():
    for raw in [
        'data: {"error":"upstream boom"}\n\n',
        'data: {"error":{"message":"detailed"}}\n\n',
    ]:
        ae = parse_arena_event(feed_all(raw)[0])
        assert ae.kind == "error"
        assert ae.error
    print("✓ error event (str + object)")


def test_battle_model_index_variants():
    for val, expect in [
        ("0", "a"),
        ("1", "b"),
        ("a", "a"),
        ("b", "b"),
        ("modelA", "a"),
        ("modelB", "b"),
    ]:
        raw = "data: " + json.dumps({"content": "x", "model_index": val}) + "\n\n"
        ae = parse_arena_event(feed_all(raw)[0])
        assert ae.model_index == expect, f"{val} → {ae.model_index} (want {expect})"
    print("✓ battle model_index variants (0/1/a/b/modelA/modelB)")


def test_reveal_event():
    raw = (
        "data: "
        + json.dumps({"type": "reveal", "modelA": "gpt-5.4", "modelB": "claude-opus-4-6"})
        + "\n\n"
    )
    ae = parse_arena_event(feed_all(raw)[0])
    assert ae.kind == "reveal"
    assert ae.model_a == "gpt-5.4"
    assert ae.model_b == "claude-opus-4-6"
    print("✓ reveal event (battle model names)")


def test_plain_text_fallback():
    """JSON lỗi → coi data là text thường."""
    ae = parse_arena_event(feed_all("data: just plain text\n\n")[0])
    assert ae.content == "just plain text"
    print("✓ plain-text fallback")


def test_empty_stream():
    assert feed_all("") == []
    assert feed_all("\n\n") == []
    print("✓ empty stream handled")


def test_backoff_monotonic_and_capped():
    delays = [backoff_delay(i) for i in range(1, 8)]
    assert all(d >= 0 for d in delays)
    assert all(d <= RETRY_MAX_DELAY for d in delays)
    # retry_after ưu tiên
    assert backoff_delay(1, retry_after=5.0) == min(5.0, RETRY_MAX_DELAY)
    print("✓ backoff non-negative, capped, honors retry_after")


def test_iter_arena_events_async():
    async def src():
        yield 'data: {"content":"a"}\n\ndata: {"content":"b"}\n\n'
        yield "data: [DONE]\n\n"

    from src.sse_parser import iter_arena_events

    async def run():
        out = []
        async for ev in iter_arena_events(src()):
            out.append(ev)
        return out

    evs = asyncio.run(run())
    assert evs[0].content == "a"
    assert evs[1].content == "b"
    assert evs[2].kind == "done"
    print("✓ iter_arena_events async over text chunks")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n🎉 {len(tests)} SSE/backoff unit tests PASS")


if __name__ == "__main__":
    main()
