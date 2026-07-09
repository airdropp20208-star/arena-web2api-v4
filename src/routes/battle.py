"""
POST /v1/battle          — Battle mode, trả 2 response tách biệt + reveal.
POST /v1/battle/vote     — Vote cho một battle đã xong.
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from src.client import client
from src.concurrency import gate
from src.conversation import manager
from src.errors import ArenaWeb2APIError
from src.logger import setup_logger
from src.metrics import metrics
from src.models import BattleRequest, BattleResponse, BattleSide, VoteRequest, VoteResponse
from src.tokenizer import count_message_tokens

router = APIRouter()
logger = setup_logger(__name__)


async def _run_battle(plan):
    """Thu thập full content của A & B + reveal. Trả (a, b, model_a, model_b)."""
    a = b = ""
    model_a = model_b = None
    async for ev in client.stream_battle(plan):
        if ev.kind == "error" and ev.error:
            raise ArenaWeb2APIError(ev.error, status=502)
        if ev.kind == "done":
            continue
        if ev.kind == "reveal":
            model_a = ev.model_a or model_a
            model_b = ev.model_b or model_b
            continue
        if not ev.content:
            continue
        which = ev.model_index or "a"
        if which == "a":
            a += ev.content
        else:
            b += ev.content
    return a, b, model_a, model_b


@router.post("/battle")
async def battle(req: BattleRequest):
    msgs = [{"role": m.role, "content": m.text_content()} for m in req.messages]
    attachments = [att for m in req.messages if m.role == "user" for att in m.attachments()]
    attachments = attachments[-10:] if attachments else []
    prompt_tokens = count_message_tokens(msgs)
    started = time.time()

    try:
        plan = manager.plan_turn(msgs, "arena-battle", attachments=attachments)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    if req.stream:

        async def gen():
            a = b = ""
            model_a = model_b = None
            try:
                async with gate.slot():
                    async for ev in client.stream_battle(plan):
                        if ev.kind == "error" and ev.error:
                            yield f"data: {json.dumps({'error': ev.error})}\n\n"
                            return
                        if ev.kind == "done":
                            continue
                        if ev.kind == "reveal":
                            model_a = ev.model_a or model_a
                            model_b = ev.model_b or model_b
                            yield f"data: {json.dumps({'reveal': True, 'model_a': model_a, 'model_b': model_b})}\n\n"
                            continue
                        if not ev.content:
                            continue
                        which = ev.model_index or "a"
                        if which == "a":
                            a += ev.content
                        else:
                            b += ev.content
                        yield f"data: {json.dumps({'model': which, 'content': ev.content})}\n\n"
                manager.commit_response(plan, a or b)
                yield f"data: {json.dumps({'done': True, 'conversation_id': plan.conversation.conversation_id, 'model_a': model_a, 'model_b': model_b})}\n\n"
                yield "data: [DONE]\n\n"
                await metrics.record(
                    model="arena-battle",
                    ok=True,
                    latency_ms=(time.time() - started) * 1000,
                    tokens_in=prompt_tokens,
                    tokens_out=count_tokens_safe(a) + count_tokens_safe(b),
                )
            except ArenaWeb2APIError as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                await metrics.record(
                    model="arena-battle",
                    ok=False,
                    latency_ms=(time.time() - started) * 1000,
                    error_type=type(e).__name__,
                )

        return StreamingResponse(gen(), media_type="text/event-stream")

    # non-stream
    try:
        async with gate.slot():
            a, b, model_a, model_b = await _run_battle(plan)
    except ArenaWeb2APIError as e:
        await metrics.record(
            model="arena-battle",
            ok=False,
            latency_ms=(time.time() - started) * 1000,
            error_type=type(e).__name__,
        )
        raise HTTPException(status_code=e.status, detail=str(e)) from None
    manager.commit_response(plan, a or b)
    await metrics.record(
        model="arena-battle",
        ok=True,
        latency_ms=(time.time() - started) * 1000,
        tokens_in=prompt_tokens,
        tokens_out=count_tokens_safe(a) + count_tokens_safe(b),
    )

    return BattleResponse(
        conversation_id=plan.conversation.conversation_id,
        model_a=BattleSide(content=a, model=model_a),
        model_b=BattleSide(content=b, model=model_b),
        revealed=bool(model_a or model_b),
    )


@router.post("/battle/vote", response_model=VoteResponse)
async def battle_vote(req: VoteRequest):
    """Gửi vote lên Arena cho một battle."""
    try:
        await client.submit_vote(req.conversation_id, req.vote)
    except ArenaWeb2APIError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from None
    return VoteResponse(conversation_id=req.conversation_id, vote=req.vote)


def count_tokens_safe(text: str) -> int:
    from src.tokenizer import count_tokens

    return count_tokens(text or "")
