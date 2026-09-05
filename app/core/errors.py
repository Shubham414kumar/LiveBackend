"""Error handling.

All errors leave the API in one shape (RFC 7807 ``application/problem+json``)
so clients can parse failures uniformly instead of guessing between FastAPI's
``{"detail": ...}``, a bare string, and an HTML 500 page.

Just as important: unhandled exceptions never leak a traceback or an upstream
URL to the client. They are logged in full with the request id, and the client
receives that id so a user report can be traced to a specific log line.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.http import UpstreamError
from app.core.logging import get_logger, request_id_ctx

logger = get_logger(__name__)

PROBLEM_JSON = "application/problem+json"


class AppError(Exception):
    """Base class for errors we raise deliberately and can describe to clients."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    title: str = "Internal Server Error"
    code: str = "internal_error"

    def __init__(self, detail: str, *, extra: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra or {}


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    title = "Not Found"
    code = "not_found"


class ValidationError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    title = "Validation Error"
    code = "validation_error"


class AuthError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    title = "Unauthorized"
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    title = "Forbidden"
    code = "forbidden"


class RateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    title = "Too Many Requests"
    code = "rate_limited"

    def __init__(self, detail: str, retry_after: int, limit: int) -> None:
        super().__init__(detail, extra={"retry_after": retry_after, "limit": limit})
        self.retry_after = retry_after


class ServiceUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    title = "Service Unavailable"
    code = "service_unavailable"


class ConfigurationMissingError(AppError):
    """A feature was requested but its credentials were never configured.

    Distinct from a generic 503 so clients can show "this feature is off"
    rather than "try again later".
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    title = "Feature Unavailable"
    code = "feature_unavailable"


def problem_response(
    *,
    status_code: int,
    title: str,
    detail: str,
    code: str,
    extra: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> JSONResponse:
    body: Dict[str, Any] = {
        "type": f"https://sentinelai.app/errors/{code}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "code": code,
    }
    request_id = request_id_ctx.get()
    if request_id:
        body["request_id"] = request_id
    if extra:
        body.update(extra)

    response_headers = {"Content-Type": PROBLEM_JSON}
    if headers:
        response_headers.update(headers)

    return JSONResponse(status_code=status_code, content=body, headers=response_headers)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError) -> JSONResponse:
        headers = None
        if isinstance(exc, RateLimitedError):
            headers = {"Retry-After": str(exc.retry_after)}
        return problem_response(
            status_code=exc.status_code,
            title=exc.title,
            detail=exc.detail,
            code=exc.code,
            extra=exc.extra,
            headers=headers,
        )

    @app.exception_handler(UpstreamError)
    async def _upstream_error(_request: Request, exc: UpstreamError) -> JSONResponse:
        # 504 when the upstream ran out of time, 502 when it answered badly.
        status_code = (
            status.HTTP_504_GATEWAY_TIMEOUT if exc.timeout else status.HTTP_502_BAD_GATEWAY
        )
        logger.warning(
            "Upstream failure surfaced to client",
            extra={
                "provider": exc.provider,
                "upstream_status": exc.status_code,
                "timeout": exc.timeout,
            },
        )
        return problem_response(
            status_code=status_code,
            title="Upstream Data Provider Error",
            # Names the provider (useful, non-sensitive) but never the URL,
            # query string, or token.
            detail=f"The {exc.provider} data provider is currently unavailable.",
            code="upstream_unavailable",
            extra={"provider": exc.provider},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "field": ".".join(str(part) for part in err.get("loc", ())[1:]) or "body",
                "message": err.get("msg", "invalid value"),
                "type": err.get("type", "value_error"),
            }
            for err in exc.errors()
        ]
        return problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            title="Validation Error",
            detail="One or more request parameters were invalid.",
            code="validation_error",
            extra={"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        headers = dict(exc.headers) if getattr(exc, "headers", None) else None
        return problem_response(
            status_code=exc.status_code,
            title=_TITLES.get(exc.status_code, "Request Failed"),
            detail=detail,
            code=_CODES.get(exc.status_code, "http_error"),
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled exception",
            extra={"path": request.url.path, "method": request.method},
        )
        detail = "An unexpected error occurred. Please try again."
        if not settings.is_production:
            # Only in non-production: surfacing the exception message locally
            # saves a trip to the logs during development.
            detail = f"{type(exc).__name__}: {exc}"
        return problem_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            title="Internal Server Error",
            detail=detail,
            code="internal_error",
        )


_TITLES = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Payload Too Large",
    415: "Unsupported Media Type",
    422: "Validation Error",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}

_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    502: "upstream_unavailable",
    503: "service_unavailable",
    504: "upstream_timeout",
}

# Re-exported so callers can keep raising HTTPException where that reads better.
__all__ = [
    "AppError",
    "AuthError",
    "ConfigurationMissingError",
    "ForbiddenError",
    "HTTPException",
    "NotFoundError",
    "RateLimitedError",
    "ServiceUnavailableError",
    "ValidationError",
    "problem_response",
    "register_exception_handlers",
]
