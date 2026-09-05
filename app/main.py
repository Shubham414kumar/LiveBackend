"""Application factory.

Everything with a process lifetime is wired here: logging, the shared HTTP
client pool, the cache, the rate limiter, middleware and the router tree.

The version this replaces created its resources at module import time and tore
them down in an ``@app.on_event("shutdown")`` handler that called
``client.close()`` on a name that did not exist in that scope. Shutdown
therefore raised ``NameError`` every time, connections were never released, and
because the handler crashed the remaining cleanup never ran. Lifespan is used
here instead: setup and teardown sit in one function, so a resource that is
acquired is visibly released.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.api.router import api_router
from app.core.cache import cache
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.http import shutdown_http, startup_http
from app.core.logging import configure_logging, get_logger
from app.core.rate_limit import limiter
from app.core.security import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.db import supabase

logger = get_logger(__name__)

DESCRIPTION = """
Real-time environmental and disaster intelligence API.

**Identity.** There are no user accounts. Each install generates a UUID and
sends it as an `X-Device-Id` header; that header scopes saved locations, hazard
reports and push registrations. It is an *identifier*, not a credential — it is
never the only thing protecting a privileged operation.

**Partial results are labelled.** Endpoints that fan out to several public
providers return `sources_failed` and `partial`. A feed that is empty because a
provider is down is reported as such rather than rendered as a quiet day.

**Errors** follow RFC 7807 (`application/problem+json`).
"""

TAGS_METADATA = [
    {"name": "health", "description": "Liveness, readiness and feature discovery."},
    {"name": "air quality", "description": "WAQI station readings, map tiles and search."},
    {"name": "weather", "description": "Open-Meteo conditions, radar tiles and flood indicators."},
    {"name": "disasters", "description": "Merged USGS, NASA EONET, GDACS and disease.sh feeds."},
    {"name": "places", "description": "Reverse geocoding and nearby hospitals (OpenStreetMap)."},
    {"name": "ai", "description": "Gemini-backed guidance, with deterministic fallbacks."},
    {"name": "community reports", "description": "Device-scoped crowd-sourced hazard reports."},
    {"name": "favorites", "description": "Saved locations and their active alerts."},
    {"name": "notifications", "description": "Expo push token registration."},
    {"name": "admin", "description": "Authenticated moderation surface."},
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Acquire process-wide resources on boot, release them on shutdown."""
    configure_logging()
    logger.info(
        "Starting %s",
        settings.service_name,
        extra={
            "release": settings.release,
            "version": __version__,
            "environment": settings.environment,
            "docs_enabled": settings.docs_enabled,
        },
    )

    await startup_http()
    await cache.connect()
    await limiter.connect()

    # Initialise the database client at boot rather than on the first request,
    # so a bad SUPABASE_URL surfaces in the startup logs instead of as a user's
    # 503 several minutes later.
    supabase.get_client()

    # Optional capabilities are announced once at boot. Without this, a
    # deployment missing GEMINI_API_KEY looks healthy while silently serving
    # static fallback text as though it were analysis.
    if not settings.has_gemini:
        logger.warning("GEMINI_API_KEY not set; AI endpoints will serve deterministic fallbacks.")
    if not settings.has_admin_auth:
        logger.warning(
            "Admin auth is not configured; /api/admin/* will reject every request. "
            "Set ADMIN_USERNAME, ADMIN_PASSWORD_HASH and ADMIN_JWT_SECRET to enable it."
        )
    if not settings.redis_url:
        logger.warning(
            "REDIS_URL not set; cache and rate limits are per-worker. Acceptable for "
            "a single process, but two workers will each allow the full rate limit."
        )

    try:
        yield
    finally:
        # Reverse acquisition order, and each step is independent so one failure
        # cannot skip the rest.
        logger.info("Shutting down %s", settings.service_name)
        for label, closer in (
            ("rate limiter", limiter.close),
            ("cache", cache.close),
            ("http client", shutdown_http),
        ):
            try:
                await closer()
            except Exception as exc:
                logger.warning("Failed to close %s cleanly: %s", label, exc)


def create_app() -> FastAPI:
    """Build the ASGI application."""
    configure_logging()

    app = FastAPI(
        title="SentinelAI API",
        description=DESCRIPTION,
        version=__version__,
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
        root_path=settings.root_path,
        # Gated on configuration: in production the schema is a map of the
        # attack surface, and _validate_production refuses to boot with docs on.
        docs_url="/api/docs" if settings.docs_enabled else None,
        openapi_url="/api/openapi.json" if settings.docs_enabled else None,
        # ReDoc pulls a second CDN bundle for no additional benefit here, and
        # every CDN in the CSP is another origin that must be trusted.
        redoc_url=None,
        # Trailing-slash redirects turn a POST into a GET on some clients and
        # silently drop the body. Paths are declared exactly as clients call them.
        redirect_slashes=False,
    )

    # ---- Middleware -------------------------------------------------------
    # Starlette applies these outermost-first in *reverse* registration order:
    # the middleware added last wraps everything. Registration below is
    # therefore innermost-first, which reads backwards on purpose.
    #
    # Effective request path:
    #   RequestContext -> SecurityHeaders -> CORS -> TrustedHost -> BodySize -> routes
    #
    # RequestContext is outermost so every response — including a rejected Host
    # or an oversized body — carries a request id and appears in the access log.
    # BodySize is innermost of the middlewares but still ahead of any handler,
    # so an oversized request is refused on its Content-Length header before a
    # single body byte is buffered.

    app.add_middleware(BodySizeLimitMiddleware)

    if "*" not in settings.trusted_hosts:
        # Absent this, a forged Host header is reflected into absolute URLs the
        # app generates (password-reset style links, the AQI tile URL), which is
        # how Host-header poisoning works.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        # Only true once origins are enumerated; the spec forbids credentials
        # with a wildcard origin and browsers reject the combination outright.
        allow_credentials=settings.allow_credentials,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        # Enumerated rather than "*", so the header list stays valid when
        # credentials are enabled.
        allow_headers=[
            "Accept",
            "Accept-Language",
            "Authorization",
            "Content-Type",
            "X-Device-Id",
            "X-Request-ID",
        ],
        # Without this a browser client cannot read its own rate-limit budget or
        # correlate a failure with a server log line.
        expose_headers=[
            "X-Request-ID",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "Retry-After",
            "Server-Timing",
        ],
        max_age=600,
    )

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    # ---- Errors and routes ----------------------------------------------
    register_exception_handlers(app)
    app.include_router(api_router)

    @app.get("/", include_in_schema=False)
    async def index() -> Dict[str, Any]:
        """Root banner.

        Platform health checks and uptime monitors default to ``/``; without this
        they record a permanent 404 and report the service as down.
        """
        return {
            "service": settings.service_name,
            "version": __version__,
            "status": "ok",
            "api": "/api",
            "docs": "/api/docs" if settings.docs_enabled else None,
        }

    return app


app = create_app()

__all__ = ["app", "create_app", "lifespan"]
