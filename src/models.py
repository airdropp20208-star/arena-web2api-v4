"""
Pydantic schemas — OpenAI-compatible + phần mở rộng cho Arena
(battle, reveal, vote, attachments).
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]


# ── Attachments (vision / file) ─────────────────────────────────────────────
class ImageURL(BaseModel):
    url: str
    detail: str | None = "auto"


class ContentPart(BaseModel):
    """Một phần của message content (text hoặc image_url) — theo OpenAI format."""

    type: Literal["text", "image_url"]
    text: str | None = None
    image_url: ImageURL | None = None


class Attachment(BaseModel):
    """Mô tả attachment gửi lên Arena (experimental_attachments)."""

    name: str | None = None
    mime_type: str | None = None
    url: str | None = None  # data: URI hoặc https URL
    size: int | None = None


# ── Request ─────────────────────────────────────────────────────────────────
class Message(BaseModel):
    role: Role
    # content có thể là string hoặc list[ContentPart] (vision)
    content: Any
    name: str | None = None
    # tool calling: assistant message có thể kèm tool_calls; tool message kèm tool_call_id
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None

    def text_content(self) -> str:
        """Trả về content dạng plain text (ghép nếu là multipart)."""
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            parts: list[str] = []
            for p in self.content:
                if isinstance(p, dict):
                    if p.get("type") == "text":
                        parts.append(p.get("text", ""))
                elif isinstance(p, ContentPart) and p.type == "text":
                    parts.append(p.text or "")
            return "\n".join(parts)
        # content có thể là None (tool_calls-only message)
        if self.content is None:
            return ""
        return str(self.content)

    def attachments(self) -> list[Attachment]:
        """Trích image_url parts → Attachment (cho vision)."""
        result: list[Attachment] = []
        if not isinstance(self.content, list):
            return result
        for p in self.content:
            d = p if isinstance(p, dict) else p.model_dump()
            if d.get("type") == "image_url":
                url = (d.get("image_url") or {}).get("url")
                if url:
                    result.append(Attachment(url=url, mime_type="image/auto"))
        return result


class ChatRequest(BaseModel):
    model: str = "arena-auto"
    messages: list[Message]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    # Arena không cho tuỳ chỉnh thực sự, nhưng giữ cho tương thích client
    stop: Any | None = None
    user: str | None = None
    # OpenAI passthrough (không ảnh hưởng upstream, nhưng giữ schema hợp lệ)
    n: int | None = 1
    seed: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    response_format: dict | None = None
    stream_options: dict | None = None
    # Tool / function calling
    tools: list[dict] | None = None
    tool_choice: Any | None = None

    def model_post_init(self, __context: Any) -> None:
        if len(self.messages) > 500:
            raise ValueError("Too many messages (max 500).")
        if len(self.messages) == 0:
            raise ValueError("messages must not be empty.")


class BattleRequest(BaseModel):
    messages: list[Message]
    stream: bool = False
    # vote optional ngay trong request battle (a/b/tie/bothbad)
    vote: Literal["a", "b", "tie", "bothbad"] | None = None


class VoteRequest(BaseModel):
    conversation_id: str
    vote: Literal["a", "b", "tie", "bothbad"]


# ── Response (OpenAI shape) ─────────────────────────────────────────────────
class FunctionCall(BaseModel):
    name: str
    arguments: str


class ToolCall(BaseModel):
    id: str
    type: str = "function"
    function: FunctionCall


class ChoiceMessage(BaseModel):
    role: str = "assistant"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None


class Choice(BaseModel):
    index: int = 0
    message: ChoiceMessage | None = None
    delta: dict | None = None
    finish_reason: str | None = None
    logprobs: Any | None = None


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:24]}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[Choice]
    usage: Usage = Field(default_factory=Usage)
    system_fingerprint: str | None = None


# ── Models ──────────────────────────────────────────────────────────────────
class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "arena.ai"


class ModelList(BaseModel):
    object: str = "list"
    data: list[ModelInfo]


# ── Battle response ─────────────────────────────────────────────────────────
class BattleSide(BaseModel):
    content: str = ""
    model: str | None = None  # được reveal sau khi xong
    model_id: str | None = None


class BattleResponse(BaseModel):
    conversation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    model_a: BattleSide = Field(default_factory=BattleSide)
    model_b: BattleSide = Field(default_factory=BattleSide)
    revealed: bool = False


class VoteResponse(BaseModel):
    ok: bool = True
    conversation_id: str
    vote: str


# ── Error ───────────────────────────────────────────────────────────────────
class ErrorDetail(BaseModel):
    message: str
    type: str = "api_error"
    code: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
