"""
Đếm token — dùng tiktoken (encoding o200k_base) làm xấp xỉ cho mọi model,
fallback heuristic nếu tiktoken không cài hoặc encoding tải không được.

Không có vendor nào công bố tokenizer, nên đây là ước lượng tốt nhất khả thi.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache

from src.config import TOKENIZER_ENCODING, TOKENIZER_FALLBACK_HEURISTIC
from src.logger import setup_logger

logger = setup_logger(__name__)


def _heuristic_count(text: str) -> int:
    """Ước lượng ~4 ký tự / token (gần đúng cho tiếng Anh)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


@lru_cache(maxsize=4)
def _get_encoder():
    try:
        import tiktoken

        enc = tiktoken.get_encoding(TOKENIZER_ENCODING)
        logger.debug(f"Tokenizer: tiktoken encoding '{TOKENIZER_ENCODING}'")
        return enc
    except Exception as e:  # pragma: no cover - phụ thuộc môi trường
        logger.warning(f"tiktoken không khả dụng ({e}), dùng heuristic.")
        return None


def count_tokens(text: str) -> int:
    if not text:
        return 0
    enc = _get_encoder()
    if enc is None:
        return _heuristic_count(text)
    try:
        return len(enc.encode(text))
    except Exception:
        return _heuristic_count(text) if TOKENIZER_FALLBACK_HEURISTIC else 0


def count_message_tokens(messages: Iterable[dict]) -> int:
    """
    Xấp xỉ tổng token của một message list theo OpenAI:
    mỗi message cộng thêm ~4 token (overhead role/separator) + 3 token chốt.
    """
    total = 3
    for m in messages:
        total += 4
        content = m.get("content")
        if isinstance(content, str):
            total += count_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        total += count_tokens(part.get("text", ""))
                    # image_url: gán ~85 (low detail) như OpenAI
                    elif part.get("type") == "image_url":
                        total += 85
        if m.get("name"):
            total -= 1
    return max(0, total)
