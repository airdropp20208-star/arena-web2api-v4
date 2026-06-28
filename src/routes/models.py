"""GET /v1/models — danh sách model từ registry (dynamic)."""

from fastapi import APIRouter

from src.logger import setup_logger
from src.model_registry import registry
from src.models import ModelList

router = APIRouter()
logger = setup_logger(__name__)


@router.get("/models", response_model=ModelList)
async def list_models():
    await registry.ensure_loaded()
    models = registry.list_models()
    return ModelList(data=models)


@router.get("/models/refresh")
async def refresh_models():
    """Force refresh UUID map từ Arena."""
    count = await registry.refresh()
    return {"ok": True, "loaded": count, "registry": registry.snapshot()}
