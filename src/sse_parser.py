"""
Robust SSE parser cho Arena stream.

Hai tầng:
  1. SSEDecoder  — triển khai đúng SSE wire protocol (RFC-ish):
                   xử lý data/event/id/retry, multi-line data, comment,
                   buffer khi chunk bị cắt giữa dòng, dispatch trên dòng trống.
  2. parse_arena_event — biến 1 SSE event thành ArenaEvent có cấu trúc,
                   chịu nhiều biến thể JSON shape của Arena (delta/content/text,
                   battle model_index, reveal, error, metadata).

Giải quyết vấn đề #4 của Codex review: parser quá đơn giản.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass, field

from src.logger import setup_logger

logger = setup_logger(__name__)


# ── Tầng 1: generic SSE wire decoder ────────────────────────────────────────
@dataclass
class SSEEvent:
    event: str | None = None  # giá trị field `event:`
    data: str = ""  # các dòng `data:` ghép bằng \n
    id: str | None = None
    retry: int | None = None
    comment: str | None = None  # dòng bắt đầu bằng `:`


class SSEDecoder:
    """
    Feed text (có thể cắt giữa chừng) → yield SSEEvent hoàn chỉnh.

    Dùng:  for ev in decoder.feed(chunk): ...
    """

    def __init__(self) -> None:
        self._event: str | None = None
        self._data_lines: list[str] = []
        self._id: str | None = None
        self._retry: int | None = None
        self._last_comment: str | None = None
        self._buffer = ""
        self.last_event_id: str | None = None

    def feed(self, chunk: str) -> Iterator[SSEEvent]:
        self._buffer += chunk
        # Xử lý từng dòng đã hoàn chỉnh (giữ phần chưa kết thúc \n trong buffer)
        lines = self._buffer.split("\n")
        self._buffer = lines.pop()  # phần cuối có thể chưa hoàn chỉnh
        for line in lines:
            yield from self._handle_line(line)

    def feed_lines(self, lines: Iterable[str]) -> Iterator[SSEEvent]:
        for line in lines:
            yield from self._handle_line(line)

    def flush(self) -> Iterator[SSEEvent]:
        """Xử lý nốt phần còn trong buffer khi stream kết thúc."""
        if self._buffer:
            yield from self._handle_line(self._buffer)
            self._buffer = ""
        if self._data_lines or self._event or self._id:
            yield from self._dispatch()

    def _handle_line(self, line: str) -> Iterator[SSEEvent]:
        if line == "":
            # dòng trống → dispatch event
            yield from self._dispatch()
            return
        if line.startswith(":"):
            self._last_comment = line[1:].strip()
            return
        if ":" in line:
            field_name, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
        else:
            field_name, value = line, ""

        if field_name == "event":
            self._event = value
        elif field_name == "data":
            self._data_lines.append(value)
        elif field_name == "id":
            self._id = value
        elif field_name == "retry":
            with contextlib.suppress(ValueError):
                self._retry = int(value)
        # field lạ → bỏ qua (spec)

    def _dispatch(self) -> Iterator[SSEEvent]:
        if not self._data_lines and self._event is None and self._id is None:
            # reset dù vậy
            self._reset()
            return
        data = "\n".join(self._data_lines)
        ev = SSEEvent(
            event=self._event,
            data=data,
            id=self._id,
            retry=self._retry,
            comment=self._last_comment,
        )
        if self._id:
            self.last_event_id = self._id
        self._reset()
        yield ev

    def _reset(self) -> None:
        self._event = None
        self._data_lines = []
        self._id = None
        self._retry = None


# ── Tầng 2: Arena-specific parsing ──────────────────────────────────────────
@dataclass
class ArenaEvent:
    kind: str = "delta"
    """delta | reveal | error | metadata | done"""
    content: str = ""
    role: str | None = None
    finish_reason: str | None = None
    model_index: str | None = None  # "a" | "b" | None
    model_a: str | None = None  # tên reveal
    model_b: str | None = None
    error: str | None = None
    metadata: dict = field(default_factory=dict)
    raw: dict | None = None


def _norm_index(val) -> str | None:
    if val is None:
        return None
    s = str(val).strip().lower()
    if s in ("a", "0", "modela", "model_a", "left", "1"):
        # 0 thường = model A (index đầu)
        if s in ("a", "modela", "model_a", "left"):
            return "a"
        if s == "0":
            return "a"
        if s == "1":
            return "b"
        return "a"
    if s in ("b", "1", "modelb", "model_b", "right"):
        if s == "1":
            return "b"
        return "b"
    return None


def parse_arena_event(sse: SSEEvent) -> ArenaEvent | None:
    """Biến 1 SSEEvent → ArenaEvent (hoặc None nếu không có gì)."""
    data = sse.data

    # [DONE] marker
    if data.strip() == "[DONE]":
        return ArenaEvent(kind="done", finish_reason="stop")

    # parse JSON — nếu fail, coi data là plain text content
    try:
        chunk = json.loads(data)
    except (json.JSONDecodeError, ValueError):
        if data.strip():
            return ArenaEvent(content=data)
        return None

    if not isinstance(chunk, dict):
        if isinstance(chunk, str) and chunk:
            return ArenaEvent(content=chunk)
        return None

    return _interpret_chunk(chunk)


def _interpret_chunk(chunk: dict) -> ArenaEvent:
    raw = chunk

    # ── Error ───────────────────────────────────────────────────────────
    err = chunk.get("error")
    if err is not None:
        msg = (
            err
            if isinstance(err, str)
            else (err.get("message") or err.get("detail") or json.dumps(err))
        )
        return ArenaEvent(kind="error", error=str(msg), raw=raw)

    ctype = (chunk.get("type") or chunk.get("event") or "").lower()

    # ── Reveal (battle — lộ model) ──────────────────────────────────────
    if ctype in ("reveal", "models", "revealed", "result"):
        ma = (
            chunk.get("modelA")
            or chunk.get("model_a")
            or chunk.get("modelAName")
            or chunk.get("modelNameA")
        )
        mb = (
            chunk.get("modelB")
            or chunk.get("model_b")
            or chunk.get("modelBName")
            or chunk.get("modelNameB")
        )
        # có thể nested trong models list
        models = chunk.get("models")
        if isinstance(models, list) and len(models) >= 2 and not (ma and mb):
            ma = ma or (models[0].get("name") if isinstance(models[0], dict) else models[0])
            mb = mb or (models[1].get("name") if isinstance(models[1], dict) else models[1])
        return ArenaEvent(kind="reveal", model_a=ma, model_b=mb, metadata=chunk, raw=raw)

    # ── Metadata ────────────────────────────────────────────────────────
    if ctype in ("metadata", "meta", "info"):
        return ArenaEvent(kind="metadata", metadata=chunk, raw=raw)

    # ── Content extraction (nhiều shape) ────────────────────────────────
    content = ""
    role: str | None = None
    finish: str | None = None
    model_index: str | None = None

    choices = chunk.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        delta = first.get("delta") or first.get("message") or {}
        if isinstance(delta, dict):
            content = delta.get("content") or delta.get("text") or ""
            role = delta.get("role")
        finish = first.get("finish_reason") or first.get("finishReason")

    if not content:
        content = (
            chunk.get("content")
            or chunk.get("text")
            or chunk.get("message")
            or chunk.get("delta")
            or chunk.get("data")
            or ""
        )
        if not isinstance(content, str):
            content = ""

    if not role:
        role = chunk.get("role")

    if not finish:
        finish = chunk.get("finish_reason") or chunk.get("finishReason")

    # ── Battle model index ──────────────────────────────────────────────
    model_index = (
        _norm_index(chunk.get("model_index"))
        or _norm_index(chunk.get("modelIndex"))
        or _norm_index(chunk.get("side"))
        or _norm_index(chunk.get("index"))
    )

    # type done/stop
    if ctype in ("done", "stop", "end", "complete") and not content:
        finish = finish or "stop"

    # nếu hoàn toàn rỗng → metadata nhẹ
    if not content and not finish and model_index is None:
        return ArenaEvent(kind="metadata", metadata=chunk, raw=raw)

    kind = "delta"
    if finish and not content:
        kind = "done"

    return ArenaEvent(
        kind=kind,
        content=content,
        role=role,
        finish_reason=finish,
        model_index=model_index,
        raw=raw,
    )


# ── Convenience iterator ────────────────────────────────────────────────────
async def iter_arena_events(text_chunks: AsyncIterator[str]) -> AsyncIterator[ArenaEvent]:
    """
    Nhận async text chunks → yield ArenaEvent.
    Dùng trong client:  async for ev in iter_arena_events(resp.aiter_text()):
    """
    decoder = SSEDecoder()
    async for chunk in text_chunks:
        for sse in decoder.feed(chunk):
            ev = parse_arena_event(sse)
            if ev:
                yield ev
    for sse in decoder.flush():
        ev = parse_arena_event(sse)
        if ev:
            yield ev
