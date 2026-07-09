"""
Conversation store — lưu state multi-turn trong RAM (tuỳ chọn persist ra file).

Mỗi conversation được khoá hoá bởi fingerprint của (model + toàn bộ message
history). Khi client gửi lượt tiếp theo, manager tìm conversation có prefix
trùng → tiếp tục thật (gửi incremental), không ghép string lại.

Thread-safety: sync accessors dùng threading.Lock (không có await point nên
asyncio.Lock vô dụng). Async API dùng asyncio.Lock cho persist/cleanup.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field

from src.config import CONVERSATION_MAX_TURNS, CONVERSATION_STORE_FILE, CONVERSATION_TTL
from src.logger import setup_logger

logger = setup_logger(__name__)


@dataclass
class Conversation:
    key: str  # fingerprint(model + full history)
    model: str
    conversation_id: str  # uuid4 ổn định — link các turn với Arena
    model_a_id: str  # UUID nội bộ Arena của model A
    model_b_id: str | None = None
    history: list[dict] = field(default_factory=list)  # [{role, content}]
    turns: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def is_expired(self, ttl: int) -> bool:
        return (time.time() - self.updated_at) > ttl

    def snapshot(self) -> dict:
        return {
            "model": self.model,
            "conversation_id": self.conversation_id,
            "turns": self.turns,
            "messages": len(self.history),
            "age_sec": int(time.time() - self.created_at),
        }


class ConversationStore:
    def __init__(self) -> None:
        self._convs: dict[str, Conversation] = {}
        # sync lock (không await point trong get/put/delete) — fix B8
        self._sync_lock = threading.Lock()
        self._async_lock = asyncio.Lock()

    # ── Sync accessors (hot path) ──────────────────────────────────────
    def _live(self, key: str) -> Conversation | None:
        c = self._convs.get(key)
        if c and c.is_expired(CONVERSATION_TTL):
            del self._convs[key]
            return None
        return c

    def get_sync(self, key: str) -> Conversation | None:
        with self._sync_lock:
            return self._live(key)

    def find_by_prefix_sync(self, prefix_key: str) -> Conversation | None:
        """Tìm conversation mà full-key == prefix_key (lượt tiếp theo)."""
        if not prefix_key:
            return None
        with self._sync_lock:
            return self._live(prefix_key)

    def put_sync(self, conv: Conversation) -> None:
        with self._sync_lock:
            conv.updated_at = time.time()
            self._convs[conv.key] = conv
            if len(self._convs) > 5000:
                self._cleanup_locked()

    def delete_sync(self, key: str) -> None:
        with self._sync_lock:
            self._convs.pop(key, None)

    def _cleanup_locked(self) -> None:
        before = len(self._convs)
        # Remove expired conversations
        self._convs = {k: v for k, v in self._convs.items() if not v.is_expired(CONVERSATION_TTL)}
        # Size cap — fix #34: avoid unbounded growth
        # Keep most-recently-used 1000 conversations max
        MAX_CONVERSATIONS = 1000
        if len(self._convs) > MAX_CONVERSATIONS:
            # Sort by last_activity desc, keep top MAX_CONVERSATIONS
            sorted_items = sorted(
                self._convs.items(),
                key=lambda x: getattr(x[1], "last_activity", 0),
                reverse=True,
            )
            self._convs = dict(sorted_items[:MAX_CONVERSATIONS])
            logger.info(f"Conversation store size cap: trimmed to {MAX_CONVERSATIONS}")
        if len(self._convs) != before:
            logger.debug(f"Conversation store cleanup: {before} → {len(self._convs)}")

    # ── Async API (cho admin/persist) ──────────────────────────────────
    async def get(self, key: str) -> Conversation | None:
        async with self._async_lock:
            return self._live(key)

    async def find_by_prefix(self, prefix_key: str) -> Conversation | None:
        async with self._async_lock:
            return self.find_by_prefix_sync(prefix_key)

    async def put(self, conv: Conversation) -> None:
        async with self._async_lock:
            self.put_sync(conv)

    async def delete(self, key: str) -> None:
        async with self._async_lock:
            self.delete_sync(key)

    async def cleanup(self) -> int:
        # dùng cả 2 lock để tránh race với sync mutations
        with self._sync_lock:
            async with self._async_lock:
                before = len(self._convs)
                self._cleanup_locked()
                return before - len(self._convs)

    @property
    def size(self) -> int:
        return len(self._convs)

    def snapshot(self) -> dict:
        return {
            "active": len(self._convs),
            "ttl_sec": CONVERSATION_TTL,
            "max_turns": CONVERSATION_MAX_TURNS,
            "conversations": [c.snapshot() for c in list(self._convs.values())[:20]],
        }

    # ── Atomic file persistence — fix B9 ───────────────────────────────
    async def persist(self) -> None:
        if not CONVERSATION_STORE_FILE:
            return
        async with self._async_lock:
            data = [asdict(c) for c in self._convs.values()]
        try:
            # Backup versioning — fix #25: rotate .bak → .bak.1 → .bak.2 → .bak.3
            # Keep 4 historical versions for recovery
            if os.path.exists(CONVERSATION_STORE_FILE):
                # Rotate .bak.3 → delete, .bak.2 → .bak.3, .bak.1 → .bak.2, .bak → .bak.1
                for i in [3, 2, 1]:
                    src = f"{CONVERSATION_STORE_FILE}.bak.{i}" if i > 1 else f"{CONVERSATION_STORE_FILE}.bak"
                    dst = f"{CONVERSATION_STORE_FILE}.bak.{i+1}"
                    if os.path.exists(src):
                        try:
                            if i == 3:
                                os.unlink(src)  # delete oldest
                            else:
                                os.rename(src, dst)
                        except OSError:
                            pass
                # Current file → .bak
                backup_path = CONVERSATION_STORE_FILE + ".bak"
                try:
                    import shutil
                    shutil.copy2(CONVERSATION_STORE_FILE, backup_path)
                except Exception as backup_err:
                    logger.warning(f"Backup conversation file failed: {backup_err}")
                    # Continue anyway — better to write new than abort

            # ghi vào temp file rồi rename nguyên tử (tránh file corrupt)
            dirname = os.path.dirname(os.path.abspath(CONVERSATION_STORE_FILE)) or "."
            os.makedirs(dirname, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=dirname, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp_path, CONVERSATION_STORE_FILE)
                logger.debug(f"Persisted {len(data)} conversations → {CONVERSATION_STORE_FILE}")
            except Exception:
                # dọn temp file nếu lỗi giữa chừng
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
                raise
        except Exception as e:
            logger.warning(f"Persist conversations lỗi: {e}")

    async def load(self) -> None:
        if not CONVERSATION_STORE_FILE:
            return
        # Try main file first; if corrupt, try .bak, then .bak.1, .bak.2, .bak.3
        candidates = [CONVERSATION_STORE_FILE, CONVERSATION_STORE_FILE + ".bak"]
        for i in [1, 2, 3]:
            candidates.append(f"{CONVERSATION_STORE_FILE}.bak.{i}")
        for path in candidates:
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                async with self._async_lock:
                    for d in data:
                        c = Conversation(**d)
                        if not c.is_expired(CONVERSATION_TTL):
                            self._convs[c.key] = c
                logger.info(f"Loaded {len(self._convs)} conversations từ {path}")
                return
            except json.JSONDecodeError as e:
                logger.warning(f"File {path} corrupt (JSON decode error): {e} — trying next")
                continue
            except Exception as e:
                logger.warning(f"Load từ {path} lỗi: {e} — trying next")
                continue
        logger.info("No conversation file to load (fresh start)")


store = ConversationStore()
