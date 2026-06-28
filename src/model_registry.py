"""
Model registry — Dynamic UUID sync.

Tự fetch UUID thật của mỗi model từ GET /nextjs-api/models, cache có TTL,
fallback về static map nếu Arena không trả về.

Giải quyết vấn đề #1 của Codex review: MODEL_ID_MAP dùng UUID giả.
"""

from __future__ import annotations

import asyncio
import time

from src.config import (
    ARENA_MODELS_URL,
    MODEL_REGISTRY_ON_STARTUP,
    MODEL_REGISTRY_TTL,
)
from src.logger import setup_logger
from src.models import ModelInfo
from src.session import build_browser_headers
from src.utils import DEFAULT_MODELS

logger = setup_logger(__name__)

# Static fallback — chỉ dùng khi registry fetch thất bại hoàn toàn.
# Không phải UUID thật (giá trị này chỉ là placeholder an toàn).
STATIC_FALLBACK: dict[str, str] = {
    "arena-auto": "arena-max",
    "arena-battle": "battle",
}


class ModelRegistry:
    """
    Lưu name → internal id/uuid của Arena.

    `id` Arena cho mỗi model có thể là UUID hoặc slug tuỳ response.
    Registry cố gắng map: name (hiển thị) → id (gửi trong payload modelAId).
    """

    def __init__(self) -> None:
        self._name_to_id: dict[str, str] = {}
        self._id_to_name: dict[str, str] = {}
        self._full: list[dict] = []
        self._loaded_at: float = 0.0
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None

    def _ingest(self, raw) -> int:
        """Parse raw response → điền 2 map. Trả về số model nạp."""
        if not raw:
            return 0
        items = raw if isinstance(raw, list) else (raw.get("models") or raw.get("data") or [])
        count = 0
        name_map: dict[str, str] = {}
        id_map: dict[str, str] = {}
        full: list[dict] = []
        for m in items:
            if not isinstance(m, dict):
                continue
            mid = m.get("id") or m.get("uuid")
            display = m.get("name") or m.get("slug") or m.get("model") or mid
            if not display:
                continue
            mid = mid or display
            name_map[display] = mid
            id_map[mid] = display
            full.append(m)
            count += 1
        if count:
            self._name_to_id = name_map
            self._id_to_name = id_map
            self._full = full
            self._loaded_at = time.time()
        return count

    async def refresh(self) -> int:
        """Fetch models từ Arena browser. Trả về số model."""
        import json as json_mod

        async with self._lock:
            try:
                # Dùng agent-browser để lấy models (browser có cookies đúng)
                proc = await asyncio.create_subprocess_exec(
                    "agent-browser", "eval",
                    """
                    (async () => {
                        const resp = await fetch('/nextjs-api/v1/models');
                        const html = await resp.text();
                        const match = html.match(/initialModels.*?(\\[\\{.*?\\}\\])/s);
                        if (!match) return null;
                        let dataStr = match[1].replace(/\\\\\\"/g, '"').replace(/\\\\"/g, '"');
                        try {
                            const models = JSON.parse(dataStr);
                            return models.map(m => ({id: m.id, publicName: m.publicName, displayName: m.displayName}));
                        } catch(e) {
                            return null;
                        }
                    })()
                    """,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)

                if proc.returncode == 0 and stdout:
                    models_list = json_mod.loads(stdout.decode().strip())
                    if models_list and isinstance(models_list, list):
                        count = self._ingest(models_list)
                        logger.info(f"Model registry: nạp {count} model từ Arena browser.")
                        return count

                logger.warning("Model registry: browser fetch thất bại, giữ cache cũ.")
                return len(self._name_to_id)
            except Exception as e:
                logger.warning(f"Model registry refresh lỗi: {e}")
                return len(self._name_to_id)

    def _stale(self) -> bool:
        return (time.time() - self._loaded_at) > MODEL_REGISTRY_TTL

    async def ensure_loaded(self) -> None:
        """Lazy load nếu chưa có hoặc đã stale."""
        if not self._name_to_id or self._stale():
            await self.refresh()

    def resolve(self, name: str) -> str:
        """
        name → internal id. Thứ tự:
          1. arena-battle / arena-auto → hằng số
          2. map động từ Arena
          3. static fallback
          4. trả name nguyên (Arena có thể chấp nhận slug)
        Không raise — client tự quyết định có cảnh báo không.
        """
        if name in STATIC_FALLBACK:
            return STATIC_FALLBACK[name]
        if name in self._name_to_id:
            return self._name_to_id[name]
        # thử fuzzy: lowercase match
        low = {k.lower(): v for k, v in self._name_to_id.items()}
        if name.lower() in low:
            return low[name.lower()]
        return name

    def has(self, name: str) -> bool:
        return name in self._name_to_id or name in STATIC_FALLBACK

    def display_name(self, internal_id: str) -> str:
        return self._id_to_name.get(internal_id, internal_id)

    def list_models(self) -> list[ModelInfo]:
        """Danh sách model đã biết — ưu tiên registry, fallback DEFAULT_MODELS."""
        ids = list(self._name_to_id.keys()) if self._name_to_id else list(DEFAULT_MODELS)
        out = [ModelInfo(id=m) for m in ids]
        existing = {m.id for m in out}
        for special in ("arena-auto", "arena-battle"):
            if special not in existing:
                out.append(ModelInfo(id=special))
        return out

    def reveal_name(self, internal_id: str) -> str | None:
        """Map internal model id (từ battle reveal) → tên hiển thị."""
        return self._id_to_name.get(internal_id)

    async def start_refresh_loop(self) -> None:
        if not MODEL_REGISTRY_ON_STARTUP or self._refresh_task:
            return

        async def loop():
            # load ngay lúc khởi động
            await self.refresh()
            while True:
                await asyncio.sleep(MODEL_REGISTRY_TTL)
                try:
                    await self.refresh()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"Registry refresh loop lỗi: {e}")

        self._refresh_task = asyncio.create_task(loop())

    async def stop(self) -> None:
        import contextlib

        if self._refresh_task:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._refresh_task
            self._refresh_task = None

    def snapshot(self) -> dict:
        return {
            "loaded_models": len(self._name_to_id),
            "loaded_at": int(self._loaded_at),
            "ttl_sec": MODEL_REGISTRY_TTL,
            "stale": self._stale(),
            "sample": dict(list(self._name_to_id.items())[:10]),
        }


# Singleton
registry = ModelRegistry()
