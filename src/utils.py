"""
Helpers dùng chung: build OpenAI SSE chunks, hash conversation, backoff calc,
parse model list, ID generation.
"""

from __future__ import annotations

import hashlib
import json
import random
import uuid
from typing import Any

from src.config import (
    ARENA_BASE,
    RETRY_BASE_DELAY,
    RETRY_JITTER,
    RETRY_MAX_DELAY,
)
from src.logger import setup_logger
from src.models import Message, ModelInfo

logger = setup_logger(__name__)

# Models mặc định khi registry chưa fetch được
DEFAULT_MODELS = [
    "arena-auto",
    "arena-battle",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "gpt-5.4",
    "gpt-5.4-high",
    "gemini-3.1-pro",
    "grok-4.20",
]


# ── OpenAI SSE chunk builders ───────────────────────────────────────────────
def make_stream_chunk(content: str, model: str, cid: str, ts: int, role: str | None = None) -> str:
    delta: dict[str, Any] = {"content": content}
    if role:
        delta["role"] = role
    return (
        "data: "
        + json.dumps(
            {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": ts,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": delta,
                        "finish_reason": None,
                    }
                ],
            }
        )
        + "\n\n"
    )


def make_stream_done(model: str, cid: str, ts: int, *, finish_reason: str = "stop") -> str:
    return (
        "data: "
        + json.dumps(
            {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": ts,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            }
        )
        + "\n\n"
        "data: [DONE]\n\n"
    )


def make_stream_usage(model: str, cid: str, ts: int, usage: dict) -> str:
    """Chunk usage cuối cùng (cho client hỗ trợ stream_options.include_usage)."""
    return (
        "data: "
        + json.dumps(
            {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": ts,
                "model": model,
                "choices": [],
                "usage": usage,
            }
        )
        + "\n\n"
    )


def make_error_chunk(error: str, error_type: str = "api_error") -> str:
    return "data: " + json.dumps({"error": {"message": error, "type": error_type}}) + "\n\n"


def make_stream_tool_calls(tool_calls: list[dict], model: str, cid: str, ts: int) -> str:
    """
    Emit tool_call deltas (mỗi tool_call = 1 chunk với index riêng).
    Buffers trước nên arguments gửi trọn 1 lần.
    """
    out = ""
    for i, tc in enumerate(tool_calls):
        chunk = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": ts,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": i,
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["function"]["name"],
                                    "arguments": tc["function"]["arguments"],
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        }
        if i == 0:
            chunk["choices"][0]["delta"]["role"] = "assistant"
        out += "data: " + json.dumps(chunk) + "\n\n"
    return out


# ── IDs ─────────────────────────────────────────────────────────────────────
def new_chat_id() -> str:
    """ID tương thích OpenAI (uuid4 — không rò r timestamp như uuid1)."""
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def new_uuid() -> str:
    return str(uuid.uuid4())


# ── Conversation hashing (cho multi-turn matching) ─────────────────────────
def messages_fingerprint(messages: list[dict], model: str) -> str:
    """
    Hash (model + toàn bộ messages) — dùng làm khoá conversation store.
    Hai request có cùng history sẽ match.
    """
    h = hashlib.sha1()
    h.update(model.encode())
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, sort_keys=True, ensure_ascii=False)
        h.update(f"{role}:{content}".encode())
    return h.hexdigest()


def messages_prefix_fingerprint(messages: list[dict], model: str) -> str:
    """
    Hash (model + tất cả message TRỪ message cuối).
    Dùng để phát hiện: request này là lượt tiếp theo của conversation nào.
    """
    return messages_fingerprint(messages[:-1], model) if len(messages) >= 2 else ""


# ── Backoff ─────────────────────────────────────────────────────────────────
def backoff_delay(attempt: int, retry_after: float | None = None) -> float:
    """
    Exponential backoff + jitter, không bao giờ vượt RETRY_MAX_DELAY.
    `retry_after` (từ header Retry-After) nếu có sẽ ưu tiên.
    """
    if retry_after and retry_after > 0:
        return min(retry_after, RETRY_MAX_DELAY)
    base = RETRY_BASE_DELAY * (2 ** (attempt - 1))
    base = min(base, RETRY_MAX_DELAY)
    jitter = base * RETRY_JITTER * (2 * random.random() - 1)  # ±jitter%
    return min(RETRY_MAX_DELAY, max(0.1, base + jitter))


# ── Model list parsing ──────────────────────────────────────────────────────
def parse_arena_models(raw: Any) -> list[ModelInfo]:
    """
    Parse response từ /nextjs-api/models — chấp nhận nhiều shape:
      [ {id, name, ...}, ... ]
      { "models": [...] } / { "data": [...] }
    Trả về list[ModelInfo] luôn có arena-battle.
    """
    if not raw:
        return [ModelInfo(id=m) for m in DEFAULT_MODELS]
    items = raw if isinstance(raw, list) else (raw.get("models") or raw.get("data") or [])
    result: list[ModelInfo] = []
    seen: set[str] = set()
    for m in items:
        if not isinstance(m, dict):
            continue
        mid = m.get("name") or m.get("slug") or m.get("id")
        if mid and mid not in seen:
            seen.add(mid)
            result.append(ModelInfo(id=mid))
    if not result:
        result = [ModelInfo(id=m) for m in DEFAULT_MODELS]
    # luôn có arena-auto + arena-battle
    existing = {m.id for m in result}
    if "arena-auto" not in existing:
        result.insert(0, ModelInfo(id="arena-auto"))
    if "arena-battle" not in existing:
        result.append(ModelInfo(id="arena-battle"))
    return result


def to_plain_messages(messages: list[Message]) -> list[dict]:
    """Convert list[Message] → list[dict] (content luôn là str)."""
    out = []
    for m in messages:
        out.append({"role": m.role, "content": m.text_content()})
    return out


def arena_origin() -> str:
    return ARENA_BASE
