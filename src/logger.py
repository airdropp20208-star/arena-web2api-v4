"""
Logging thống nhất: 1 format, 1 place cấu hình level.

Hỗ trợ:
  - DEBUG flag
  - log có màu nhẹ khi chạy trên terminal
  - LOG_JSON=true → JSON structured logs (cho ELK/Loki/datadog)
  - tự động kèm request_id (từ contextvar) cho mọi line
  - redact sensitive data (cookies, tokens) — fix #8
"""

import json
import logging
import re
import sys
import time
from typing import ClassVar

from src.config import DEBUG, LOG_JSON, LOG_LEVEL


def _effective_level() -> int:
    if DEBUG:
        return logging.DEBUG
    return getattr(logging, LOG_LEVEL, logging.INFO)


# ── Redaction patterns — fix #8 (log leak sensitive data) ──────────────────
# Match cookies and tokens in logs. Patterns are conservative to avoid false positives.
_REDACT_PATTERNS = [
    # arena-auth-prod-v1.0 / .1 cookie values (long base64-ish)
    (re.compile(r'(arena-auth-prod-v1\.\d["\']?\s*[:=]\s*["\']?)([A-Za-z0-9_\-+/=.]{50,})'), r'\1***REDACTED***'),
    # arena-auth-prod-v1 (legacy single)
    (re.compile(r'(arena-auth-prod-v1["\']?\s*[:=]\s*["\']?)([A-Za-z0-9_\-+/=.]{50,})'), r'\1***REDACTED***'),
    # cf_clearance
    (re.compile(r'(cf_clearance["\']?\s*[:=]\s*["\']?)([A-Za-z0-9_\-]{50,})'), r'\1***REDACTED***'),
    # __cf_bm
    (re.compile(r'(__cf_bm["\']?\s*[:=]\s*["\']?)([A-Za-z0-9_\-]{50,})'), r'\1***REDACTED***'),
    # JWT-style tokens (eyJ...)
    (re.compile(r'(eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)'), '***JWT_REDACTED***'),
    # recaptchaV3Token
    (re.compile(r'("recaptchaV3Token"\s*:\s*")([A-Za-z0-9_\-]{50,})'), r'\1***REDACTED***"'),
    # Set-Cookie header value
    (re.compile(r'(Set-Cookie:\s*[^;=]+=)([^\s;]+)'), r'\1***REDACTED***'),
    # Authorization Bearer
    (re.compile(r'(Authorization:\s*Bearer\s+)([A-Za-z0-9_\-\.]+)'), r'\1***REDACTED***'),
]

_REDACT_KEYWORDS = ("arena-auth-prod-v1", "cf_clearance", "__cf_bm", "recaptchaV3Token")


def redact(text: str) -> str:
    """Redact sensitive patterns from a string."""
    if not text:
        return text
    # Fast path: skip if no keywords present
    if not any(kw in text for kw in _REDACT_KEYWORDS):
        # Still check JWT/Set-Cookie/Authorization
        if "eyJ" not in text and "Set-Cookie" not in text and "Authorization" not in text:
            return text
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


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
        # Redact sensitive data từ message
        record.msg = redact(str(record.msg))
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: redact(str(v)) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(redact(str(a)) for a in record.args)
        return super().format(record)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
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
