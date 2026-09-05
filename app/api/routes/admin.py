"""Admin endpoints.

The admin dashboard shipped with a "DEV MODE — No Auth" badge and called
``POST /api/admin/login``, which did not exist. This module is that endpoint,
plus the moderation surface it needs.

Every route except login is guarded by :func:`~app.core.admin_auth.require_admin`,
which verifies a real signed JWT. Admin access deliberately does *not* use the
device-id mechanism: a device id is client-supplied and unauthenticated, and must
never be the only thing standing between a request and the ability to delete
other people's content.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Path, Query, Request

from app.api.deps import auth_limit, read_limit, write_limit
from app.core.admin_auth import create_access_token, require_admin, verify_credentials
from app.core.cache import cache
from app.core.config import settings
from app.core.errors import AuthError, NotFoundError
from app.core.logging import get_logger
from app.core.security import client_ip
from app.db.repositories import REPORT_STATUSES, push_tokens_repo, reports_repo
from app.models.schemas import (
    AdminLoginRequest,
    AdminLoginResponse,
    AdminModerateRequest,
    AdminReportsResponse,
    AdminStats,
    Report,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(row.get("id")),
        "category": row.get("category") or "other",
        "title": row.get("title") or "",
        "description": row.get("description"),
        "lat": row.get("lat"),
        "lon": row.get("lon"),
        "severity": row.get("severity") or "Moderate",
        "status": row.get("status") or "visible",
        "upvotes": int(row.get("upvotes") or 0),
        "created_at": row.get("created_at"),
    }


@router.post(
    "/login",
    response_model=AdminLoginResponse,
    dependencies=[Depends(auth_limit)],
    summary="Exchange admin credentials for a short-lived JWT",
)
async def login(payload: AdminLoginRequest, request: Request) -> Dict[str, Any]:
    """Verify credentials and issue a token.

    Sits in the ``auth`` rate-limit bucket (5 attempts per minute by default),
    which is what makes credential stuffing impractical. The failure message is
    identical for an unknown username and a wrong password, so it cannot be used
    to enumerate accounts.
    """
    if not verify_credentials(payload.username, payload.password):
        logger.warning(
            "Failed admin login",
            extra={"client_ip": client_ip(request), "username": payload.username},
        )
        raise AuthError("Incorrect username or password.")

    logger.info(
        "Admin signed in",
        extra={"client_ip": client_ip(request), "username": payload.username},
    )
    return create_access_token(payload.username)


@router.get(
    "/me",
    dependencies=[Depends(read_limit)],
    summary="Verify the current token",
)
async def me(claims: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Lets the dashboard check a stored token on load instead of discovering it
    is expired on the first real action."""
    return {
        "username": claims.get("sub"),
        "role": claims.get("role"),
        "expires_at": claims.get("exp"),
    }


@router.get(
    "/stats",
    response_model=AdminStats,
    dependencies=[Depends(read_limit), Depends(require_admin)],
    summary="Dashboard counters",
)
async def stats() -> Dict[str, Any]:
    return {
        "reports": await reports_repo.admin_stats(),
        "push_tokens": await push_tokens_repo.count(),
        "cache_backend": str(cache.stats().get("backend", "unknown")),
        "rate_limit_backend": "redis" if settings.redis_url else "memory",
    }


@router.get(
    "/reports",
    response_model=AdminReportsResponse,
    dependencies=[Depends(read_limit), Depends(require_admin)],
    summary="All reports, including hidden and removed",
)
async def list_reports(
    status: Optional[str] = Query(
        None, description=f"Filter by status: {', '.join(REPORT_STATUSES)}"
    ),
    category: Optional[str] = Query(None, max_length=50),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    if status and status not in REPORT_STATUSES:
        raise NotFoundError(
            f"Unknown status '{status}'. Valid values: {', '.join(REPORT_STATUSES)}."
        )

    rows, total = await reports_repo.admin_list(
        status=status, category=category, limit=limit, offset=offset
    )
    reports: List[Dict[str, Any]] = [_shape(row) for row in rows]
    return {"reports": reports, "total": total, "limit": limit, "offset": offset}


@router.patch(
    "/reports/{report_id}",
    response_model=Report,
    dependencies=[Depends(write_limit)],
    summary="Hide, remove or restore a report",
)
async def moderate(
    payload: AdminModerateRequest,
    report_id: str = Path(..., min_length=1, max_length=64),
    claims: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """Change a report's visibility.

    A status change, not a delete: the row stays so a moderation decision can be
    reviewed or reversed, and so a pattern of abuse from one device remains
    visible to the next moderator.
    """
    moderator = str(claims.get("sub") or "unknown")
    row = await reports_repo.admin_set_status(report_id, payload.status, moderator=moderator)
    if not row:
        raise NotFoundError("No report matches that id.")

    logger.info(
        "Report moderated",
        extra={
            "report_id": report_id,
            "new_status": payload.status,
            "moderator": moderator,
            "reason": payload.reason,
        },
    )
    return _shape(row)
