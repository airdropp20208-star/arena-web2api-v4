"""
Logging thống nhất: 1 format, 1 place cấu hình level.

Hỗ trợ:
  - DEBUG flag
  - log có màu nhẹ khi chạy trên terminal
  - LOG_JSON=true → JSON structured logs (cho ELK/Loki/datadog)
  - tự động kèm request_id (từ contextvar) cho mọi line
"""

import json
import logging
import sys
import time
from typing import ClassVar

from src.config import DEBUG, LOG_JSON, LOG_LEVEL


def _effective_level() -> int:
    if DEBUG:
        return logging.DEBUG
    return getattr(logging, LOG_LEVEL, logging.INFO)


class _ColorFormatter(logging.Formatter):
    _COLORS: ClassVar[dict[str, str]] = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35m",
    }
    _RESET: ClassVar[str] = "\033[0m"

    def __init__(self, fmt: str, datefmt: str, use_color: bool = True):
        super().__init__(fmt, datefmt)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        # inject request_id nếu có
        try:
            from src.request_id import current_request_id

            rid = current_request_id()
            if rid:
                record.req_id = rid[:8]
            else:
                record.req_id = "-"
        except Exception:
            record.req_id = "-"
        if self.use_color and record.levelname in self._COLORS:
            record.levelname = f"{self._COLORS[record.levelname]}{record.levelname:<7}{self._RESET}"
        return super().format(record)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        try:
            from src.request_id import current_request_id

            rid = current_request_id()
            if rid:
                payload["request_id"] = rid
        except Exception:
            pass
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for k in ("model", "status", "attempt"):
            if hasattr(record, k):
                payload[k] = getattr(record, k)
        return json.dumps(payload, ensure_ascii=False)


def setup_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        if LOG_JSON:
            handler.setFormatter(_JsonFormatter())
        else:
            fmt = "%(asctime)s %(levelname)s %(name)s [%(req_id)s]: %(message)s"
            datefmt = "%H:%M:%S"
            use_color = sys.stdout.isatty()
            handler.setFormatter(_ColorFormatter(fmt, datefmt, use_color=use_color))
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(_effective_level())
    return logger
