"""Security middleware and input sanitisation."""

from __future__ import annotations

import re
import time
import unicodedata
import uuid
from typing import Optional

from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.config import settings
from app.core.errors import problem_response
from app.core.logging import device_id_ctx, get_logger, request_id_ctx

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
DEVICE_ID_HEADER = "X-Device-Id"

# 256 KiB. No endpoint accepts anything close to this; the cap exists so a
# malicious client cannot force the server to buffer an arbitrary body.
MAX_BODY_BYTES = 256 * 1024

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# Patterns used to hijack an LLM prompt. Stripped from anything we interpolate
# into a Gemini prompt.
_PROMPT_INJECTION = re.compile(
    r"(?:"
    r"```"  # code fences that can close our block
    r"|\{\{|\}\}"  # template delimiters
    r"|<\|[^|]*\|>"  # chat-template special tokens
    r"|\[/?(?:INST|SYS|SYSTEM|ASSISTANT|USER)\]"
    r"|^\s*(?:system|assistant|user)\s*:"  # fake role turns
    r"|ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions"
    r"|disregard\s+(?:all\s+)?(?:previous|prior|above)"
    r"|you\s+are\s+now\s+"
    r"|new\s+instructions?\s*:"
    r")",
    re.IGNORECASE | re.MULTILINE,
)


def sanitize_string(value: Optional[str], max_length: int = 500) -> str:
    """Normalise, strip control characters, and truncate.

    NFKC normalisation first, so visually-identical Unicode variants can't be
    used to slip past the pattern filters below.
    """
    if not value:
        return ""
    normalised = unicodedata.normalize("NFKC", value)
    cleaned = _CONTROL_CHARS.sub("", normalised)
    return cleaned.strip()[:max_length]


def sanitize_ai_input(value: Optional[str], max_length: int = 2000) -> str:
    """Sanitise text that will be interpolated into an LLM prompt.

    Defence in depth only. The prompts themselves are also written to treat
    interpolated values as untrusted data rather than instructions.
    """
    cleaned = sanitize_string(value, max_length)
    if not cleaned:
        return ""
    return _PROMPT_INJECTION.sub(" ", cleaned).strip()


def client_ip(request: Request) -> str:
    """Best-effort client IP.

    ``X-Forwarded-For`` is attacker-controlled unless a proxy you trust
    appends to it, so it is only consulted when ``TRUSTED_PROXY_HOPS`` says how
    many trailing entries were added by infrastructure you control. Getting
    this wrong lets a client forge a new identity per request and bypass rate
    limiting entirely.
    """
    hops = settings.trusted_proxy_hops
    if hops > 0:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            if parts:
                index = max(0, len(parts) - hops)
                return parts[index]
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, binds log context, and emits one access log line."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        # Only reuse a client-supplied id if it looks like one, so it can't be
        # used to inject content into log lines.
        request_id = (
            incoming
            if incoming and len(incoming) <= 64 and re.fullmatch(r"[A-Za-z0-9._-]+", incoming)
            else uuid.uuid4().hex
        )
        request_token = request_id_ctx.set(request_id)

        device_id = sanitize_string(request.headers.get(DEVICE_ID_HEADER), 64) or None
        device_token = device_id_ctx.set(device_id)

        request.state.request_id = request_id
        request.state.device_id = device_id
        request.state.client_ip = client_ip(request)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "Request failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round(duration_ms, 2),
                    "client_ip": request.state.client_ip,
                },
            )
            request_id_ctx.reset(request_token)
            device_id_ctx.reset(device_token)
            raise

        duration_ms = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers["Server-Timing"] = f"app;dur={duration_ms:.1f}"

        # Health checks fire constantly; logging them buries real traffic.
        if request.url.path not in ("/api/health/live", "/api/health/ready"):
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round(duration_ms, 2),
                    "client_ip": request.state.client_ip,
                },
            )

        request_id_ctx.reset(request_token)
        device_id_ctx.reset(device_token)
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds hardening response headers."""

    # The API returns JSON only, so it needs no script/style/image sources at
    # all. The one exception is the interactive docs page, which loads Swagger
    # UI from a CDN.
    _API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    _DOCS_CSP = (
        "default-src 'none'; "
        "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
        "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
        "img-src 'self' https://fastapi.tiangolo.com data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        headers = response.headers

        headers["X-Content-Type-Options"] = "nosniff"
        headers["X-Frame-Options"] = "DENY"
        headers["Referrer-Policy"] = "no-referrer"
        headers["Permissions-Policy"] = (
            "geolocation=(), camera=(), microphone=(), usb=(), payment=()"
        )
        headers["Cross-Origin-Opener-Policy"] = "same-origin"
        headers["Cross-Origin-Resource-Policy"] = "same-site"
        headers["X-Permitted-Cross-Domain-Policies"] = "none"

        is_docs = request.url.path in ("/api/docs", "/api/openapi.json")
        headers["Content-Security-Policy"] = self._DOCS_CSP if is_docs else self._API_CSP

        if settings.hsts_enabled:
            headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"

        # Nothing this API returns should ever land in a shared cache; upstream
        # data is already cached server-side with explicit TTLs.
        headers.setdefault("Cache-Control", "no-store")

        return response


class BodySizeLimitMiddleware:
    """Rejects oversized request bodies before they are buffered.

    Implemented as raw ASGI rather than BaseHTTPMiddleware so the check happens
    on the ``Content-Length`` header before any body bytes are read.
    """

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        content_length = headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self.max_bytes:
                    response = problem_response(
                        status_code=413,
                        title="Payload Too Large",
                        detail=(f"Request body exceeds the {self.max_bytes // 1024} KiB limit."),
                        code="payload_too_large",
                    )
                    await response(scope, receive, send)
                    return
            except ValueError:
                response = problem_response(
                    status_code=400,
                    title="Bad Request",
                    detail="Malformed Content-Length header.",
                    code="bad_request",
                )
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


__all__ = [
    "DEVICE_ID_HEADER",
    "MAX_BODY_BYTES",
    "REQUEST_ID_HEADER",
    "BodySizeLimitMiddleware",
    "RequestContextMiddleware",
    "SecurityHeadersMiddleware",
    "client_ip",
    "sanitize_ai_input",
    "sanitize_string",
]
