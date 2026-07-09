"""
Conversation manager — multi-turn THẬT với Arena.

Cách hoạt động:
  - OpenAI client luôn gửi FULL history mỗi request.
  - Manager hash (model + history). Nếu prefix (trừ msg cuối) khớp với 1
    conversation đang sống → đó là lượt tiếp theo:
      • gửi CHỈ message mới nhất (incremental), KHÔNG ghép toàn bộ history
      • tái dùng conversation_id / arena_session_id để Arena giữ context
  - Nếu không khớp → conversation mới:
      • gửi full history flattened làm message đầu (Arena chưa có context)
      • đăng ký conversation để các lượt sau tiếp tục thật

Giải quyết vấn đề #2 của Codex review: multi-turn "giả".
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from src.conversation_store import Conversation, store
from src.logger import setup_logger
from src.model_registry import registry
from src.utils import (
    messages_fingerprint,
    messages_prefix_fingerprint,
    new_uuid,
)

logger = setup_logger(__name__)


def _flatten_history(messages: list[dict]) -> str:
    """
    Cho message đầu của conversation mới: ghép history thành 1 user message
    có cấu trúc rõ (chỉ dùng khi Arena chưa có context).
    """
    parts: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            parts.insert(0, f"[System]\n{content}")
        elif role == "assistant":
            parts.append(f"[Assistant]\n{content}")
        else:
            parts.append(f"[User]\n{content}")
    return "\n\n".join(parts)


@dataclass
class TurnPlan:
    is_continuation: bool
    conversation: Conversation
    # Nội dung thực sự gửi lên Arena (chỉ 1 user message):
    send_content: str
    send_role: str = "user"
    # Attachments (vision) đi kèm message này:
    attachments: list = None  # type: ignore[assignment]


class ConversationManager:
    def __init__(self) -> None:
        self.store = store

    def plan_turn(
        self,
        messages: list[dict],
        model: str,
        *,
        model_b: str | None = None,
        attachments: list | None = None,
    ) -> TurnPlan:
        """
        Quyết định nội dung gửi cho lượt này.

        Trả về TurnPlan; conversation đã được tạo/đăng ký trong store.
        Client tự sinh UUID per-evaluation (id/userMessageId/...) từ plan.
        """
        if not messages:
            raise ValueError("messages trống")

        # model B id cho battle (có thể None)
        model_a_id = registry.resolve(model)
        model_b_id = registry.resolve(model_b) if model_b else None

        full_key = messages_fingerprint(messages, model)
        prefix_key = messages_prefix_fingerprint(messages, model)

        existing = None
        if prefix_key:
            existing = self.store.find_by_prefix_sync(prefix_key)

        if existing is not None:
            # ── Tiếp tục conversation ───────────────────────────────────
            last = messages[-1]
            send_content = last.get("content", "")
            if not isinstance(send_content, str):
                send_content = str(send_content)
            existing.updated_at = time.time()
            logger.debug(
                f"Multi-turn continue conv={existing.conversation_id[:8]} turn={existing.turns + 1}"
            )
            return TurnPlan(
                is_continuation=True,
                conversation=existing,
                send_content=send_content,
                send_role=last.get("role", "user"),
                attachments=attachments or [],
            )

        # ── Conversation mới ────────────────────────────────────────────
        conv_id = new_uuid()
        # message đầu: nếu chỉ 1 msg → gửi nguyên; nhiều msg → flatten
        if len(messages) == 1:
            send_content = messages[0].get("content", "")
            if not isinstance(send_content, str):
                send_content = str(send_content)
        else:
            send_content = _flatten_history(messages)

        conv = Conversation(
            key=full_key,
            model=model,
            conversation_id=conv_id,
            model_a_id=model_a_id,
            model_b_id=model_b_id,
            history=[
                {"role": m.get("role", "user"), "content": m.get("content", "")} for m in messages
            ],
            turns=1,
        )
        # đăng ký ngay để request trùng lặp không tạo 2 conv
        self.store.put_sync(conv)

        logger.debug(f"Multi-turn new conv={conv_id[:8]} model={model}")
        return TurnPlan(
            is_continuation=False,
            conversation=conv,
            send_content=send_content,
            send_role="user",
            attachments=attachments or [],
        )

    def commit_response(self, plan: TurnPlan, assistant_content: str) -> Conversation:
        """
        Ghi nhận response của Arena vào conversation.
        Cập nhật history + fingerprint mới để lượt sau match đúng.
        """
        conv = plan.conversation
        old_key = conv.key
        # append user message đã gửi + assistant response vào history THẬT
        if plan.is_continuation:
            conv.history.append({"role": plan.send_role, "content": plan.send_content})
        conv.history.append({"role": "assistant", "content": assistant_content})
        conv.turns += 1
        # cập nhật key = fingerprint của history mới (xoá key cũ tránh trùng)
        conv.key = messages_fingerprint(conv.history, conv.model)
        conv.updated_at = time.time()
        self.store.delete_sync(old_key)
        self.store.put_sync(conv)
        return conv


manager = ConversationManager()
