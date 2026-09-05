"""Health, readiness and service metadata.

Three distinct endpoints, because orchestrators need different answers:

* ``/health/live`` — is the process running? Never touches a dependency, so a
  Supabase outage cannot cause a restart loop.
* ``/health/ready`` — should this instance receive traffic? Checks dependencies
  and returns 503 when a required one is down.
* ``/health`` — the detailed view, for humans and dashboards.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Response, status

from app import __version__
from app.api.deps import read_limit
from app.core.cache import cache
from app.core.config import settings
from app.db import supabase
from app.models.schemas import HealthResponse, SimpleStatus
from app.services import ai as ai_service

router = APIRouter(tags=["health"])

# The three probes below are deliberately *not* rate limited. An orchestrator
# polls them every few seconds from a single address, which is exactly the
# traffic shape a limiter is built to reject — throttling them would make the
# platform believe the service was unhealthy.


@router.get(
    "",
    response_model=SimpleStatus,
    dependencies=[Depends(read_limit)],
    summary="Service banner",
)
async def root() -> Dict[str, Any]:
    return {
        "status": "ok",
        "detail": f"{settings.service_name} v{__version__} ({settings.environment})",
    }


@router.get("/health/live", response_model=SimpleStatus, summary="Liveness probe")
async def live() -> Dict[str, Any]:
    # Intentionally dependency-free: this answers "is the process up", and a
    # dependency check here would let a database blip trigger a rolling restart.
    return {"status": "ok", "detail": "process is running"}


@router.get("/health/ready", summary="Readiness probe")
async def ready(response: Response) -> Dict[str, Any]:
    checks: Dict[str, str] = {}

    if settings.has_supabase:
        checks["database"] = "ok" if await supabase.ping() else "unavailable"
    else:
        checks["database"] = "not_configured"

    # A cache outage degrades but does not break the service, so it is reported
    # without affecting readiness.
    checks["cache"] = str(cache.stats().get("backend", "unknown"))

    required_down = checks["database"] == "unavailable"
    if required_down:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": "degraded" if required_down else "ok", "checks": checks}


@router.get("/health", response_model=HealthResponse, summary="Detailed health")
async def health() -> Dict[str, Any]:
    database_state = "not_configured"
    if settings.has_supabase:
        database_state = "ok" if await supabase.ping() else "unavailable"

    return {
        "status": "degraded" if database_state == "unavailable" else "ok",
        "service": settings.service_name,
        "release": settings.release,
        "environment": settings.environment,
        # What actually works on this deployment, rather than what the code can
        # do in principle. The client uses this to hide features it cannot use
        # instead of showing a button that always errors.
        "features": {
            "air_quality": bool(settings.waqi_token.get_secret_value()),
            "ai": ai_service.is_available(),
            "community_reports": settings.has_supabase,
            "favorites": settings.has_supabase,
            "push_notifications": settings.has_supabase,
            "admin_dashboard": settings.has_admin_auth,
        },
        "dependencies": {
            "database": database_state,
            "cache": str(cache.stats().get("backend", "unknown")),
            "rate_limiter": "redis" if settings.redis_url else "memory",
        },
    }


@router.get(
    "/meta/cache-stats",
    dependencies=[Depends(read_limit)],
    summary="Cache and limiter diagnostics",
)
async def cache_stats() -> Dict[str, Any]:
    return {
        "cache": cache.stats(),
        "rate_limit": {
            "enabled": settings.rate_limit_enabled,
            "window_seconds": settings.rate_limit_window_seconds,
            "backend": "redis" if settings.redis_url else "memory",
            "limits": {
                "read": settings.rate_limit_read,
                "write": settings.rate_limit_write,
                "ai": settings.rate_limit_ai,
                "auth": settings.rate_limit_auth,
            },
        },
    }
