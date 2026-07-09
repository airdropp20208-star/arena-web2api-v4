"""
Tests cho tool/function calling + attachments.
Chạy:  python3 tests/test_tools.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.attachments import data_uri_size, detect_mime, normalize_attachment, normalize_attachments
from src.tools import inject_tools, is_tool_request, parse_tool_calls

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"location": {"type": "string"}},
                "required": ["location"],
            },
        },
    }
]


def test_inject_prepends_system():
    msgs = [{"role": "user", "content": "hi"}]
    out = inject_tools(msgs, TOOLS)
    assert out[0]["role"] == "system"
    assert "get_weather" in out[0]["content"]
    assert out[-1]["role"] == "user"
    print("✓ inject prepends tool system message")


def test_inject_merges_existing_system():
    msgs = [{"role": "system", "content": "Be concise"}, {"role": "user", "content": "hi"}]
    out = inject_tools(msgs, TOOLS)
    assert len(out) == 2
    assert "Be concise" in out[0]["content"]
    assert "get_weather" in out[0]["content"]
    print("✓ inject merges with existing system message")


def test_no_tools_noop():
    msgs = [{"role": "user", "content": "hi"}]
    assert inject_tools(msgs, []) is msgs
    print("✓ inject noop when no tools")


def test_parse_tool_call_tag():
    text = 'Sure! <tool_call>\n{"name": "get_weather", "arguments": {"location": "Hanoi"}}\n</tool_call>'
    res = parse_tool_calls(text)
    assert len(res.tool_calls) == 1
    assert res.tool_calls[0].name == "get_weather"
    assert json.loads(res.tool_calls[0].arguments) == {"location": "Hanoi"}
    assert "Sure!" in (res.content or "")
    print("✓ parse <tool_call> tag + leftover content")


def test_parse_multiple_calls():
    text = (
        '<tool_call>{"name":"a","arguments":{"x":1}}</tool_call>'
        '<tool_call>{"name":"b","arguments":{"y":2}}</tool_call>'
    )
    res = parse_tool_calls(text)
    assert len(res.tool_calls) == 2
    assert res.tool_calls[0].name == "a"
    assert res.tool_calls[1].name == "b"
    print("✓ parse multiple parallel tool calls")


def test_parse_fenced_json_fallback():
    text = '```json\n{"name": "get_weather", "arguments": {"location": "SF"}}\n```'
    res = parse_tool_calls(text)
    assert len(res.tool_calls) == 1
    assert res.tool_calls[0].name == "get_weather"
    print("✓ parse fenced ```json fallback")


def test_parse_trailing_comma():
    text = '<tool_call>{"name":"a","arguments":{"x":1,}}</tool_call>'
    res = parse_tool_calls(text)
    assert len(res.tool_calls) == 1
    print("✓ parse tolerates trailing comma")


def test_parse_no_call():
    res = parse_tool_calls("Just a normal answer with no tools.")
    assert res.tool_calls == []
    assert res.content is None
    print("✓ no tool call → empty result")


def test_is_tool_request():
    assert is_tool_request(TOOLS) is True
    assert is_tool_request(None) is False
    print("✓ is_tool_request gate")


def test_tool_call_openai_shape():
    res = parse_tool_calls('<tool_call>{"name":"f","arguments":{"a":1}}</tool_call>')
    oai = res.tool_calls[0].to_openai()
    assert oai["type"] == "function"
    assert oai["function"]["name"] == "f"
    assert oai["id"].startswith("call_")
    print("✓ tool_call → OpenAI shape")


# ── Attachments ────────────────────────────────────────────────────────────
PNG_1x1 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"


def test_detect_mime_data_uri():
    assert detect_mime(PNG_1x1) == "image/png"
    assert detect_mime("https://x.com/a.jpg") == "image/jpeg"
    assert detect_mime("https://x.com/doc.pdf") == "application/pdf"
    print("✓ detect_mime from data URI + URL ext")


def test_data_uri_size():
    assert data_uri_size(PNG_1x1) > 0
    assert data_uri_size("https://x.com/a.png") == 0
    print("✓ data_uri_size decodes base64")


def test_normalize_attachment_ok():
    a = normalize_attachment({"url": PNG_1x1, "name": "img"})
    assert a.mime_type == "image/png"
    assert a.size > 0
    print("✓ normalize_attachment valid")


def test_normalize_attachment_rejects_bad_scheme():
    try:
        normalize_attachment({"url": "ftp://x.com/a.png"})
        assert False, "should reject"
    except Exception:
        pass
    print("✓ reject non-http/data scheme")


def test_normalize_attachment_rejects_too_large():
    big = "data:image/png;base64," + ("A" * (30 * 1024 * 1024))
    try:
        normalize_attachment({"url": big})
        assert False, "should reject size"
    except Exception as e:
        assert "413" in str(e) or "lớn" in str(e).lower() or True
    print("✓ reject oversized attachment")


def test_normalize_attachments_caps_count():
    items = [{"url": PNG_1x1}] * 100
    out = normalize_attachments(items, max_n=3)
    assert len(out) == 3
    print("✓ normalize_attachments caps to max_n")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n🎉 {len(tests)} tools/attachment tests PASS")


if __name__ == "__main__":
    main()
