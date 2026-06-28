"""
POST /v1/chat/completions — OpenAI-compatible (đầy đủ).

Tích hợp:
  - Tool/function calling (inject + parse)
  - Vision attachments (normalize + validate)
  - Concurrency gate (semaphore + queue)
  - Idempotency (Idempotency-Key)
  - Multi-turn thật, metrics, streaming SSE

  model == 'arena-battle'  → Battle mode (2 model ẩn danh, ghép [A]/[B])
  model == 'arena-auto'    → Direct mode, Arena Max router
  model == <khác>          → Direct mode với model cụ thể
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse

from src.attachments import normalize_attachments
from src.client import client
from src.concurrency import gate
from src.conversation import manager
from src.errors import ArenaWeb2APIError
from src.idempotency import idempotency
from src.logger import setup_logger
from src.metrics import metrics
from src.models import (
    Attachment,
    ChatRequest,
    ChatResponse,
    Choice,
    ChoiceMessage,
    FunctionCall,
    Message,
    ToolCall,
    Usage,
)
from src.rate_limiter import limiter
from src.tokenizer import count_message_tokens, count_tokens
from src.tools import inject_tools, is_tool_request, parse_tool_calls
from src.utils import (
    make_error_chunk,
    make_stream_chunk,
    make_stream_done,
    make_stream_tool_calls,
    make_stream_usage,
    new_chat_id,
)

router = APIRouter()
logger = setup_logger(__name__)


def _collect_attachments(messages: list[Message]) -> list[Attachment]:
    """Lấy image_url parts của mọi user message (đặc biệt message cuối)."""
    items = []
    for m in messages:
        if m.role == "user":
            items.extend(m.attachments())
    return items


def _msgs_for_arena(req: ChatRequest) -> tuple[list[dict], bool]:
    """
    Trả (messages plain-text, tools_active).
    Nếu có tools → inject tool system message.
    """
    msgs = [{"role": m.role, "content": m.text_content()} for m in req.messages]
    # gộp tool result messages (role=tool) vào user content cho Arena
    merged: list[dict] = []
    for m in msgs:
        if m["role"] == "tool":
            if merged and merged[-1]["role"] == "user":
                merged[-1]["content"] += f"\n\n[tool result {m.get('content', '')[:500]}]"
            else:
                merged.append({"role": "user", "content": f"[tool result] {m.get('content', '')}"})
        else:
            merged.append(m)

    tools_active = is_tool_request(req.tools, req.tool_choice)
    if tools_active:
        merged = inject_tools(merged, req.tools or [], req.tool_choice)
    return merged, tools_active


def _finish_for(tools_active: bool, has_tool_calls: bool) -> str:
    if tools_active and has_tool_calls:
        return "tool_calls"
    return "stop"


@router.post("/chat/completions")
async def chat_completions(
    req: ChatRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    msgs, tools_active = _msgs_for_arena(req)
    is_battle = req.model == "arena-battle"
    raw_attachments = _collect_attachments(req.messages)
    try:
        attachments = normalize_attachments(raw_attachments)
    except ArenaWeb2APIError as e:
        raise HTTPException(e.status, str(e)) from None
    cid = new_chat_id()
    ts = int(time.time())
    prompt_tokens = count_message_tokens(msgs)
    started = time.time()

    # ── Idempotency single-flight (non-stream) — fix B4 ────────────────────
    if idempotency.enabled() and not req.stream and idempotency_key:
        result = await idempotency.acquire(idempotency_key)
        # result = cached value | _Inflight | None
        if result is not None and not hasattr(result, "event"):
            logger.info(f"Idempotency cache hit: {idempotency_key[:12]}")
            return result
        if result is not None and hasattr(result, "event"):
            # có request khác đang chạy cho cùng key → chờ nó xong
            logger.info(f"Idempotency single-flight wait: {idempotency_key[:12]}")
            await idempotency.wait_for(result)
            cached = await idempotency.get_cached(idempotency_key)
            if cached is not None:
                return cached
            # request kia lỗi → ta sở hữu lại
            result = await idempotency.acquire(idempotency_key)
            if result is not None and not hasattr(result, "event"):
                return result

    # ── Rate limit TPM (prompt tokens) — fix B12 ───────────────────────────
    await limiter.acquire_tokens(prompt_tokens)

    # ── Plan multi-turn ────────────────────────────────────────────────────
    try:
        if is_battle:
            plan = manager.plan_turn(msgs, "arena-battle", attachments=attachments)
        else:
            plan = manager.plan_turn(msgs, req.model, attachments=attachments)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    # ── Streaming ──────────────────────────────────────────────────────────
    if req.stream:

        async def gen():
            full = ""
            try:
                async with gate.slot():
                    stream = client.stream_battle(plan) if is_battle else client.stream_direct(plan)
                    first = True
                    async for ev in stream:
                        if ev.kind == "error" and ev.error:
                            yield make_error_chunk(ev.error, "upstream_error")
                            return
                        if ev.kind == "done":
                            break
                        if not ev.content:
                            continue
                        if is_battle:
                            which = ev.model_index or "a"
                            label = "[A] " if which == "a" else "[B] "
                            text = label + ev.content
                        else:
                            text = ev.content
                        full += ev.content
                        role = "assistant" if first else None
                        first = False
                        yield make_stream_chunk(text, req.model, cid, ts, role=role)

                # ── Tool call parsing (buffer xong rồi emit) ───────────────
                parsed = parse_tool_calls(full) if (tools_active and not is_battle) else None
                if parsed and parsed.tool_calls:
                    manager.commit_response(plan, full)
                    yield make_stream_tool_calls(
                        [tc.to_openai() for tc in parsed.tool_calls],
                        req.model,
                        cid,
                        ts,
                    )
                    yield make_stream_done(req.model, cid, ts, finish_reason="tool_calls")
                    await metrics.record(
                        model=req.model,
                        ok=True,
                        latency_ms=(time.time() - started) * 1000,
                        tokens_in=prompt_tokens,
                        tokens_out=count_tokens(full),
                    )
                    return

                manager.commit_response(plan, full)
                completion_tokens = count_tokens(full)
                yield make_stream_usage(
                    req.model,
                    cid,
                    ts,
                    {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                )
                yield make_stream_done(req.model, cid, ts)
                await metrics.record(
                    model=req.model,
                    ok=True,
                    latency_ms=(time.time() - started) * 1000,
                    tokens_in=prompt_tokens,
                    tokens_out=completion_tokens,
                )
            except ArenaWeb2APIError as e:
                logger.error(f"chat stream ArenaError: {e}")
                yield make_error_chunk(str(e))
                await metrics.record(
                    model=req.model,
                    ok=False,
                    latency_ms=(time.time() - started) * 1000,
                    error_type=type(e).__name__,
                )
            except Exception as e:
                logger.exception("chat stream unexpected")
                yield make_error_chunk(f"Internal error: {e}")
                await metrics.record(
                    model=req.model,
                    ok=False,
                    latency_ms=(time.time() - started) * 1000,
                    error_type="internal",
                )

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── Non-streaming ──────────────────────────────────────────────────────
    try:
        async with gate.slot():
            stream = client.stream_battle(plan) if is_battle else client.stream_direct(plan)
            full_a = full_b = ""
            async for ev in stream:
                if ev.kind == "error" and ev.error:
                    raise ArenaWeb2APIError(ev.error, status=502)
                if ev.kind == "done":
                    break
                if not ev.content:
                    continue
                if is_battle:
                    which = ev.model_index or "a"
                    if which == "a":
                        full_a += ev.content
                    else:
                        full_b += ev.content
                else:
                    full_a += ev.content
    except ArenaWeb2APIError as e:
        await metrics.record(
            model=req.model,
            ok=False,
            latency_ms=(time.time() - started) * 1000,
            error_type=type(e).__name__,
        )
        if idempotency.enabled() and idempotency_key:
            await idempotency.release(idempotency_key)
        raise HTTPException(status_code=e.status, detail=str(e)) from None
    except Exception as e:
        logger.exception("chat non-stream unexpected")
        await metrics.record(
            model=req.model,
            ok=False,
            latency_ms=(time.time() - started) * 1000,
            error_type="internal",
        )
        if idempotency.enabled() and idempotency_key:
            await idempotency.release(idempotency_key)
        raise HTTPException(500, detail=str(e)) from e
    # ── Tool call parsing (non-stream) ─────────────────────────────────────
    parsed = None
    if tools_active and not is_battle:
        parsed = parse_tool_calls(full_a)

    if is_battle:
        content = f"**Model A:**\n{full_a}\n\n---\n\n**Model B:**\n{full_b}"
        full_for_commit = full_a or full_b
        manager.commit_response(plan, full_for_commit)
        completion_tokens = count_tokens(content)
        response = ChatResponse(
            id=cid,
            model=req.model,
            choices=[
                Choice(
                    index=0,
                    message=ChoiceMessage(role="assistant", content=content),
                    finish_reason="stop",
                )
            ],
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )
    elif parsed and parsed.tool_calls:
        # emit tool_calls
        tool_calls_out = [
            ToolCall(id=tc.id, function=FunctionCall(name=tc.name, arguments=tc.arguments))
            for tc in parsed.tool_calls
        ]
        full_for_commit = full_a
        manager.commit_response(plan, full_for_commit)
        completion_tokens = count_tokens(full_a)
        response = ChatResponse(
            id=cid,
            model=req.model,
            choices=[
                Choice(
                    index=0,
                    message=ChoiceMessage(
                        role="assistant",
                        content=parsed.content,
                        tool_calls=tool_calls_out,
                    ),
                    finish_reason="tool_calls",
                )
            ],
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )
    else:
        content = full_a
        manager.commit_response(plan, content)
        completion_tokens = count_tokens(content)
        response = ChatResponse(
            id=cid,
            model=req.model,
            choices=[
                Choice(
                    index=0,
                    message=ChoiceMessage(role="assistant", content=content),
                    finish_reason="stop",
                )
            ],
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )

    await metrics.record(
        model=req.model,
        ok=True,
        latency_ms=(time.time() - started) * 1000,
        tokens_in=prompt_tokens,
        tokens_out=completion_tokens,
    )
    if idempotency.enabled() and idempotency_key:
        await idempotency.put(idempotency_key, response)
    return response
