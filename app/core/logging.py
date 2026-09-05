"""Structured logging with request correlation.

Emits one JSON object per line in production so log aggregators can index
fields directly, and human-readable lines in development. Every log record
carries the current ``request_id`` and ``device_id`` when one is in scope,
which is what makes a production incident traceable across services.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Dict, Optional

from app.core.config import settings

# Populated by RequestContextMiddleware for the lifetime of one request.
request_id_ctx: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
device_id_ctx: ContextVar[Optional[str]] = ContextVar("device_id", default=None)

# Attributes present on every LogRecord; anything else a caller attaches via
# `extra=` is treated as structured context and merged into the payload.
_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)

# Values that must never reach a log sink even if a caller passes them.
_REDACT_KEYS = frozenset(
    {
        "authorization",
        "password",
        "token",
        "secret",
        "api_key",
        "apikey",
        "supabase_key",
        "waqi_token",
        "gemini_api_key",
        "admin_password_hash",
        "admin_jwt_secret",
        "push_token",
    }
)
_REDACTED = "[redacted]"


def _scrub(key: str, value: Any) -> Any:
    if key.lower() in _REDACT_KEYS:
        return _REDACTED
    return value


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": settings.service_name,
            "environment": settings.environment,
            "release": settings.release,
        }

        request_id = request_id_ctx.get()
        if request_id:
            payload["request_id"] = request_id
        device_id = device_id_ctx.get()
        if device_id:
            payload["device_id"] = device_id

        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = _scrub(key, value)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Compact, readable format for local development."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s %(name)-28s %(message)s",
            datefmt="%H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        request_id = request_id_ctx.get()
        if request_id:
            base = f"{base}  [req={request_id[:8]}]"
        return base


def configure_logging() -> None:
    """Install our handler on the root logger. Idempotent."""
    formatter: logging.Formatter = (
        JsonFormatter() if settings.log_format == "json" else ConsoleFormatter()
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # uvicorn installs its own colourised handlers; drop them so we emit one
    # consistently formatted stream rather than two competing ones.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "gunicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    # These are chatty at INFO and drown out application logs.
    for name in ("httpx", "httpcore", "hpack", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
