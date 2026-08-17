"""
Model registry â€” Dynamic UUID sync.

Tá»± fetch UUID tháº­t cá»§a má»—i model tá»« GET /nextjs-api/models, cache cÃ³ TTL,
fallback vá» static map náº¿u Arena khÃ´ng tráº£ vá».

Giáº£i quyáº¿t váº¥n Ä‘á» #1 cá»§a Codex review: MODEL_ID_MAP dÃ¹ng UUID giáº£.
"""

from __future__ import annotations

import asyncio
import time

from src.config import (
    MODEL_REGISTRY_ON_STARTUP,
    MODEL_REGISTRY_TTL,
)
from src.logger import setup_logger
from src.models import ModelInfo
from src.utils import DEFAULT_MODELS

logger = setup_logger(__name__)

# Static fallback â€” chá»‰ dÃ¹ng khi registry fetch tháº¥t báº¡i hoÃ n toÃ n.
# KhÃ´ng pháº£i UUID tháº­t (giÃ¡ trá»‹ nÃ y chá»‰ lÃ  placeholder an toÃ n).
STATIC_FALLBACK: dict[str, str] = {
    "arena-auto": "arena-max",
    "arena-battle": "battle",
}


class ModelRegistry:
    """
    LÆ°u name â†’ internal id/uuid cá»§a Arena.

    `id` Arena cho má»—i model cÃ³ thá»ƒ lÃ  UUID hoáº·c slug tuá»³ response.
    Registry cá»‘ gáº¯ng map: name (hiá»ƒn thá»‹) â†’ id (gá»­i trong payload modelAId).
    """

    def __init__(self) -> None:
        self._name_to_id: dict[str, str] = {}
        self._id_to_name: dict[str, str] = {}
        self._full: list[dict] = []
        self._loaded_at: float = 0.0
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None

    def _ingest(self, raw) -> int:
        """Parse raw response â†’ Ä‘iá»n 2 map. Tráº£ vá» sá»‘ model náº¡p."""
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
        """Fetch models tá»« Arena browser. Retry 3 láº§n vá»›i backoff â€” fix #20."""
        import json as json_mod
        import shutil

        max_retries = 3
        backoff_base = 1.5

        # Windows Desktop mode uses real Chrome/CDP; agent-browser is optional.
        if shutil.which("agent-browser") is None:
            logger.info("Model registry: agent-browser unavailable; using static fallback")
            self._loaded_at = time.time()
            return 0

        async with self._lock:
            for attempt in range(1, max_retries + 1):
                try:
                    # DÃ¹ng agent-browser Ä‘á»ƒ láº¥y models (browser cÃ³ cookies Ä‘Ãºng)
                    proc = await asyncio.create_subprocess_exec(
                        "agent-browser",
                        "eval",
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
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=20)

                    if proc.returncode == 0 and stdout:
                        models_list = json_mod.loads(stdout.decode().strip())
                        if models_list and isinstance(models_list, list):
                            count = self._ingest(models_list)
                            logger.info(f"Model registry: náº¡p {count} model tá»« Arena browser (attempt {attempt}).")
                            return count

                    # Empty result â€” retry if attempts left
                    if attempt < max_retries:
                        wait = backoff_base * (2 ** (attempt - 1))
                        logger.warning(
                            f"Model registry: browser fetch tháº¥t báº¡i (attempt {attempt}/{max_retries}), "
                            f"retry trong {wait:.1f}s"
                        )
                        await asyncio.sleep(wait)
                        continue
                except asyncio.TimeoutError:
                    if attempt < max_retries:
                        wait = backoff_base * (2 ** (attempt - 1))
                        logger.warning(
                            f"Model registry: timeout (attempt {attempt}/{max_retries}), "
                            f"retry trong {wait:.1f}s"
                        )
                        await asyncio.sleep(wait)
                        continue
                    logger.warning(f"Model registry: timeout sau {max_retries} láº§n, giá»¯ cache cÅ©")
                except Exception as e:
                    if attempt < max_retries:
                        wait = backoff_base * (2 ** (attempt - 1))
                        logger.warning(
                            f"Model registry refresh lá»—i (attempt {attempt}/{max_retries}): {e}, "
                            f"retry trong {wait:.1f}s"
                        )
                        await asyncio.sleep(wait)
                        continue
                    logger.warning(f"Model registry refresh lá»—i sau {max_retries} láº§n: {e}")

            # All retries failed â€” keep old cache or fallback to defaults
            logger.warning("Model registry: all retries failed, giá»¯ cache cÅ© hoáº·c fallback DEFAULT_MODELS")
            return len(self._name_to_id)

    def _stale(self) -> bool:
        return (time.time() - self._loaded_at) > MODEL_REGISTRY_TTL

    async def ensure_loaded(self) -> None:
        """Lazy load náº¿u chÆ°a cÃ³ hoáº·c Ä‘Ã£ stale."""
        if not self._name_to_id or self._stale():
            await self.refresh()

    def resolve(self, name: str) -> str:
        """
        name â†’ internal id. Thá»© tá»±:
          1. arena-battle / arena-auto â†’ háº±ng sá»‘
          2. map Ä‘á»™ng tá»« Arena
          3. static fallback
          4. tráº£ name nguyÃªn (Arena cÃ³ thá»ƒ cháº¥p nháº­n slug)
        KhÃ´ng raise â€” client tá»± quyáº¿t Ä‘á»‹nh cÃ³ cáº£nh bÃ¡o khÃ´ng.
        """
        if name in STATIC_FALLBACK:
            return STATIC_FALLBACK[name]
        if name in self._name_to_id:
            return self._name_to_id[name]
        # thá»­ fuzzy: lowercase match
        low = {k.lower(): v for k, v in self._name_to_id.items()}
        if name.lower() in low:
            return low[name.lower()]
        return name

    def has(self, name: str) -> bool:
        return name in self._name_to_id or name in STATIC_FALLBACK

    def display_name(self, internal_id: str) -> str:
        return self._id_to_name.get(internal_id, internal_id)

    def list_models(self) -> list[ModelInfo]:
        """Danh sÃ¡ch model Ä‘Ã£ biáº¿t â€” Æ°u tiÃªn registry, fallback DEFAULT_MODELS."""
        ids = list(self._name_to_id.keys()) if self._name_to_id else list(DEFAULT_MODELS)
        out = [ModelInfo(id=m) for m in ids]
        existing = {m.id for m in out}
        for special in ("arena-auto", "arena-battle"):
            if special not in existing:
                out.append(ModelInfo(id=special))
        return out

    def reveal_name(self, internal_id: str) -> str | None:
        """Map internal model id (tá»« battle reveal) â†’ tÃªn hiá»ƒn thá»‹."""
        return self._id_to_name.get(internal_id)

    async def start_refresh_loop(self) -> None:
        if not MODEL_REGISTRY_ON_STARTUP or self._refresh_task:
            return

        async def loop():
            # load ngay lÃºc khá»Ÿi Ä‘á»™ng
            await self.refresh()
            while True:
                await asyncio.sleep(MODEL_REGISTRY_TTL)
                try:
                    await self.refresh()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"Registry refresh loop lá»—i: {e}")

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
