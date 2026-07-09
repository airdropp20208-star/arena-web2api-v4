"""
Tool / function calling — lớp dịch tương thích OpenAI.

Arena không expose tool calling native qua endpoint này, nên ta dịch:
  1. Biến `tools` schema → 1 system instruction (mô tả tools + format output).
  2. Inject system message vào messages trước khi gửi.
  3. Sau khi nhận response, parse các <tool_call> blocks → OpenAI tool_calls.

Format yêu cầu model dùng:
    <tool_call>
    {"name": "get_weather", "arguments": {"location": "Hanoi"}}
    </tool_call>

  - Chịu cả fenced ```json blocks có "name"+"arguments".
  - Hỗ trợ nhiều tool call song song.
  - Streaming: buffer rồi emit tool_calls (không thể biết trước).
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from src.config import TOOLS_ENABLED, TOOLS_MAX_PARALLEL
from src.logger import setup_logger

logger = setup_logger(__name__)

_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)
# fallback: ```json ... ``` hoặc ```tool_code ... ```
_FENCE_RE = re.compile(r"```(?:json|tool_code|tool_call)?\s*(.*?)\s*```", re.DOTALL)

TOOL_SYSTEM_PROMPT = """You have access to the following tools. Use them when needed to answer the user's request.

# Available tools
{tool_schemas}

# How to call a tool
When you decide a tool is necessary, respond with ONLY one or more tool-call blocks in this exact format (no other text around a block that you want executed):

<tool_call>
{"name": "<tool_name>", "arguments": {<arguments as JSON object>}}
</tool_call>

Rules:
- Emit each call in its own <tool_call> block.
- "arguments" must be a valid JSON object matching the tool's parameters.
- If multiple independent calls are needed, emit multiple blocks (max {max_parallel}).
- If NO tool is needed, answer the user normally in plain text — do not invent a tool_call.
- Never wrap normal prose in <tool_call>."""


@dataclass
class ParsedToolCall:
    id: str
    name: str
    arguments: str  # JSON string (OpenAI convention)

    def to_openai(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass
class ToolParseResult:
    tool_calls: list[ParsedToolCall]
    content: str | None  # phần text còn lại (ngoài tool_call blocks)


def _serialize_tools(tools: list[dict]) -> str:
    lines = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if t.get("type") == "function" else t
        if not isinstance(fn, dict):
            continue
        name = fn.get("name", "unknown")
        desc = fn.get("description", "")
        params = fn.get("parameters", {})
        try:
            params_str = json.dumps(params, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            params_str = "{}"
        lines.append(f"## {name}\n{desc}\nParameters:\n{params_str}")
    return "\n\n".join(lines)


def build_tools_system_message(tools: list[dict]) -> str:
    """System instruction hoàn chỉnh để inject."""
    return TOOL_SYSTEM_PROMPT.replace("{tool_schemas}", _serialize_tools(tools)).replace(
        "{max_parallel}", str(TOOLS_MAX_PARALLEL)
    )


def inject_tools(messages: list[dict], tools: list[dict], tool_choice: Any = None) -> list[dict]:
    """
    Trả messages mới có system tool instruction được prepend.
    Nếu đã có system message → gộp vào sau tool instruction.
    """
    if not tools:
        return messages
    tool_sys = build_tools_system_message(tools)
    out: list[dict] = []
    injected = False
    for m in messages:
        if m.get("role") == "system" and not injected:
            combined = f"{tool_sys}\n\n---\n\n{m.get('content', '')}"
            out.append({"role": "system", "content": combined})
            injected = True
        else:
            out.append(m)
    if not injected:
        out.insert(0, {"role": "system", "content": tool_sys})
    return out


def _try_parse_json(raw: str) -> dict | None:
    raw = raw.strip()
    if not raw:
        return None
    # cắt dấu phẩy cuối (trailing comma)
    raw_clean = re.sub(r",\s*([}\]])", r"\1", raw)
    try:
        obj = json.loads(raw_clean)
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list):
            # list of calls
            return None  # handled by caller via list path
    except (json.JSONDecodeError, ValueError):
        pass
    # cố gắng trích object con đầu tiên {...}
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def parse_tool_calls(text: str) -> ToolParseResult:
    """Tìm mọi tool_call trong text → ToolParseResult."""
    calls: list[ParsedToolCall] = []
    spans: list[tuple[int, int]] = []

    # 1) <tool_call> blocks
    for m in _TOOL_CALL_RE.finditer(text):
        obj = _try_parse_json(m.group(1))
        if obj and "name" in obj:
            args = obj.get("arguments", obj.get("args", {}))
            if isinstance(args, dict):
                args_str = json.dumps(args, ensure_ascii=False)
            elif isinstance(args, str):
                args_str = args
            else:
                args_str = json.dumps(args, ensure_ascii=False)
            calls.append(
                ParsedToolCall(
                    id=f"call_{uuid.uuid4().hex[:24]}",
                    name=str(obj["name"]),
                    arguments=args_str,
                )
            )
            spans.append((m.start(), m.end()))

    # 2) fallback fenced blocks (chỉ nếu chưa có <tool_call>)
    if not calls:
        for m in _FENCE_RE.finditer(text):
            inner = m.group(1).strip()
            obj = _try_parse_json(inner)
            if obj and "name" in obj and ("arguments" in obj or "args" in obj):
                args = obj.get("arguments", obj.get("args", {}))
                args_str = (
                    json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)
                )
                calls.append(
                    ParsedToolCall(
                        id=f"call_{uuid.uuid4().hex[:24]}",
                        name=str(obj["name"]),
                        arguments=args_str,
                    )
                )
                spans.append((m.start(), m.end()))

    # tính content còn lại = text trừ các block tool_call
    content: str | None = None
    if spans:
        # xoá các span ra khỏi text
        cleaned = []
        last = 0
        for s, e in sorted(spans):
            cleaned.append(text[last:s])
            last = e
        cleaned.append(text[last:])
        remaining = "".join(cleaned).strip()
        content = remaining or None
    return ToolParseResult(tool_calls=calls, content=content)


def is_tool_request(tools: list | None, tool_choice: Any = None) -> bool:
    if not TOOLS_ENABLED:
        return False
    return bool(tools)
