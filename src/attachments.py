"""
Attachment (vision) handling — chuẩn hoá + validate.

Chấp nhận:
  - data: URI  (data:image/png;base64,...)
  - http(s) URL

Validate: MIME, kích thước (decode base64 → bytes), số lượng.
Trả về list[Attachment] sạch để đưa vào experimental_attachments.
"""

from __future__ import annotations

import base64
import re
from urllib.parse import urlparse

from src.config import MAX_ATTACHMENT_BYTES, MAX_ATTACHMENTS
from src.errors import ArenaWeb2APIError
from src.logger import setup_logger
from src.models import Attachment

logger = setup_logger(__name__)

_DATA_URI_RE = re.compile(
    r"^data:(?P<mime>[\w.+-]+/[\w.+-]+)?(?:;(?P<enc>base64))?,(?P<data>.*)$",
    re.DOTALL,
)


def detect_mime(url: str) -> str:
    """Đoán MIME từ data URI hoặc đuôi file URL."""
    m = _DATA_URI_RE.match(url)
    if m and m.group("mime"):
        return m.group("mime")
    path = urlparse(url).path.lower()
    ext_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
    }
    for ext, mime in ext_map.items():
        if path.endswith(ext):
            return mime
    return "application/octet-stream"


def data_uri_size(url: str) -> int:
    """Số byte thật của payload base64 trong data URI."""
    m = _DATA_URI_RE.match(url)
    if not m:
        return 0
    data = m.group("data") or ""
    if m.group("enc") == "base64":
        try:
            return len(base64.b64decode(data, validate=False))
        except Exception:
            return len(data)
    return len(data.encode("utf-8"))


def normalize_attachment(raw, *, name_hint: str = "attachment") -> Attachment:
    """Chuyển 1 image_url part / dict → Attachment validate."""
    d = (
        raw
        if isinstance(raw, dict)
        else (raw.model_dump(exclude_none=True) if hasattr(raw, "model_dump") else {})
    )
    # hỗ trợ cả {"type":"image_url","image_url":{"url":...}}
    if d.get("type") == "image_url":
        d = d.get("image_url") or {}
    url = d.get("url")
    if not url:
        raise ArenaWeb2APIError(400, "Attachment thiếu url.")
    mime = detect_mime(url)
    size = d.get("size")
    if size is None:
        size = data_uri_size(url)
    if url.startswith("data:") and size > MAX_ATTACHMENT_BYTES:
        raise ArenaWeb2APIError(
            413,
            f"Attachment quá lớn ({size} bytes > {MAX_ATTACHMENT_BYTES}). "
            f"Giới hạn {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB.",
        )
    if not url.startswith(("http://", "https://", "data:")):
        raise ArenaWeb2APIError(400, "Attachment url phải là http(s) hoặc data: URI.")
    return Attachment(
        name=d.get("name") or name_hint,
        mime_type=mime,
        url=url,
        size=size or None,
    )


def normalize_attachments(items: list, *, max_n: int = MAX_ATTACHMENTS) -> list[Attachment]:
    """Validate + cắt theo giới hạn số lượng."""
    if not items:
        return []
    if len(items) > max_n:
        logger.warning(f"Trim attachments {len(items)} → {max_n}")
        items = items[:max_n]
    out = []
    for i, it in enumerate(items):
        out.append(normalize_attachment(it, name_hint=f"attachment-{i + 1}"))
    return out
