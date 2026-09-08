"""Community hazard reports.

This is the surface where anonymous device identity earns its keep. Reads are
public (anyone can see what is happening nearby), but writes require a device id,
and the device id is what makes ``is_mine``, one-vote-per-report and per-device
quotas possible without a login screen.

Moderation is post-hoc: a report is visible the moment it is filed. For a
public-safety feed, holding a flood report in a queue until a human approves it
defeats the purpose. Admins hide or remove after the fact through
``/api/admin/reports``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import read_limit, write_limit
from app.core.errors import NotFoundError, RateLimitedError, ValidationError
from app.core.identity import optional_device_id, require_device_id
from app.db.repositories import (
    MAX_REPORTS_PER_DEVICE_PER_DAY,
    reports_repo,
)
from app.services import report_images
from app.models.schemas import (
    Latitude,
    Longitude,
    ReportCategoriesResponse,
    ReportCreate,
    ReportCreated,
    ReportImageUploadRequest,
    ReportImageUploadResponse,
    ReportsResponse,
    SimpleStatus,
    VoteResponse,
)

router = APIRouter(prefix="/reports", tags=["community reports"])

# Labels, icons and colours live server-side so the two clients cannot drift
# apart, and so a new category ships without an app-store release.
REPORT_CATEGORIES: Dict[str, Dict[str, str]] = {
    "flood": {"label": "Flood / Water logging", "icon": "water", "color": "#2196F3"},
    "fire": {"label": "Fire", "icon": "flame", "color": "#FF5722"},
    "accident": {"label": "Accident", "icon": "car", "color": "#F44336"},
    "road_block": {"label": "Road blocked", "icon": "close-circle", "color": "#FF9800"},
    "pollution": {"label": "Pollution / Smoke", "icon": "cloud", "color": "#9E9E9E"},
    "water_logging": {"label": "Water logging", "icon": "rainy", "color": "#03A9F4"},
    "disease_cluster": {"label": "Illness cluster", "icon": "medical", "color": "#E91E63"},
    "other": {"label": "Other hazard", "icon": "alert-circle", "color": "#8A8A8A"},
}

SEVERITIES = ["Low", "Moderate", "Severe", "Extreme"]

MAX_REPORT_RADIUS_KM = 200.0


@router.post(
    "/image-upload-url",
    response_model=ReportImageUploadResponse,
    dependencies=[Depends(write_limit)],
    summary="Create a private signed report-image upload URL",
)
async def image_upload_url(
    payload: ReportImageUploadRequest,
    device_id: str = Depends(require_device_id),
) -> Dict[str, Any]:
    return await report_images.create_upload_url(device_id, payload.content_type)


@router.get(
    "/categories",
    response_model=ReportCategoriesResponse,
    dependencies=[Depends(read_limit)],
    summary="Report categories and severity levels",
)
async def categories() -> Dict[str, Any]:
    return {"categories": REPORT_CATEGORIES, "severities": SEVERITIES}


@router.get(
    "",
    response_model=ReportsResponse,
    dependencies=[Depends(read_limit)],
    summary="Visible reports near a coordinate",
)
async def list_reports(
    lat: Latitude = Query(...),
    lon: Longitude = Query(...),
    radius_km: float = Query(25.0, gt=0, le=MAX_REPORT_RADIUS_KM),
    hours: int = Query(72, ge=1, le=720, description="Maximum report age"),
    category: Optional[str] = Query(None, max_length=50),
    limit: int = Query(100, ge=1, le=200),
    device_id: Optional[str] = Depends(optional_device_id),
) -> Dict[str, Any]:
    if category and category not in REPORT_CATEGORIES:
        raise ValidationError(f"Unknown category '{category}'. See GET /api/reports/categories.")

    rows = await reports_repo.list_nearby(
        lat=lat,
        lon=lon,
        radius_km=radius_km,
        limit=limit,
        max_age_hours=hours,
        category=category,
    )

    voted: List[str] = []
    if device_id and rows:
        voted = await reports_repo.voted_report_ids(
            device_id, [str(row["id"]) for row in rows if row.get("id")]
        )

    reports: List[Dict[str, Any]] = []
    for row in rows:
        report_id = str(row.get("id"))
        image_url = None
        if row.get("image_path"):
            try:
                image_url = await report_images.signed_read_url(row["image_path"])
            except Exception:
                # A missing storage object must not take the text report feed down.
                image_url = None
        reports.append(
            {
                "id": report_id,
                "category": row.get("category") or "other",
                "title": row.get("title") or "",
                "description": row.get("description"),
                "image_url": image_url,
                "lat": row.get("lat"),
                "lon": row.get("lon"),
                "severity": row.get("severity") or "Moderate",
                "status": row.get("status") or "visible",
                "upvotes": int(row.get("upvotes") or 0),
                "created_at": row.get("created_at"),
                "distance_km": row.get("distance_km"),
                # Computed per caller. The raw device_id column is never
                # serialised, so one user cannot enumerate another's reports.
                "is_mine": bool(device_id) and row.get("device_id") == device_id,
                "has_voted": report_id in voted,
            }
        )

    return {"reports": reports, "count": len(reports), "radius_km": radius_km}


@router.post(
    "",
    response_model=ReportCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(write_limit)],
    summary="File a hazard report",
)
async def create_report(
    payload: ReportCreate,
    device_id: str = Depends(require_device_id),
) -> Dict[str, Any]:
    if payload.image_path and not report_images.is_owned_path(device_id, payload.image_path):
        raise ValidationError("The attached image does not belong to this device.")

    recent = await reports_repo.count_recent_for_device(device_id, hours=24)
    if recent >= MAX_REPORTS_PER_DEVICE_PER_DAY:
        # A daily quota rather than a pure rate limit: the failure mode being
        # prevented is one device flooding the map over hours, which a 60-second
        # window does nothing about.
        raise RateLimitedError(
            f"You have reached the daily limit of {MAX_REPORTS_PER_DEVICE_PER_DAY} "
            "reports. This limit exists to keep the community feed trustworthy.",
            retry_after=3600,
            limit=MAX_REPORTS_PER_DEVICE_PER_DAY,
        )

    row = await reports_repo.create(
        device_id,
        category=payload.category,
        title=payload.title,
        description=payload.description,
        image_path=payload.image_path,
        lat=payload.lat,
        lon=payload.lon,
        severity=payload.severity,
        status="hidden" if payload.image_path else "visible",
    )
    if not row:
        raise ValidationError("The report could not be saved. Please try again.")

    return {
        "id": str(row.get("id")),
        "status": row.get("status") or "visible",
        "created_at": row.get("created_at"),
    }


@router.post(
    "/{report_id}/vote",
    response_model=VoteResponse,
    dependencies=[Depends(write_limit)],
    summary="Confirm a report (one vote per device)",
)
async def vote(
    report_id: str = Path(..., min_length=1, max_length=64),
    device_id: str = Depends(require_device_id),
) -> Dict[str, Any]:
    report = await reports_repo.get(report_id)
    if not report:
        raise NotFoundError("That report no longer exists.")

    accepted, upvotes = await reports_repo.add_vote(report_id, device_id)
    return {
        "report_id": report_id,
        "upvotes": upvotes,
        "accepted": accepted,
        "detail": (
            "Thanks — your confirmation was recorded."
            if accepted
            else "You have already confirmed this report."
        ),
    }


@router.delete(
    "/{report_id}",
    response_model=SimpleStatus,
    dependencies=[Depends(write_limit)],
    summary="Withdraw your own report",
)
async def delete_report(
    report_id: str = Path(..., min_length=1, max_length=64),
    device_id: str = Depends(require_device_id),
) -> Dict[str, Any]:
    """Delete a report this device filed.

    The ``device_id`` equality filter in the repository *is* the authorisation
    check, so a request for someone else's report matches zero rows and returns
    404 — it does not reveal that the report exists.
    """
    report = await reports_repo.get(report_id)
    if not report or report.get("device_id") != device_id:
        raise NotFoundError("No report of yours matches that id.")

    deleted = await reports_repo.delete_own(device_id, report_id)
    if not deleted:
        raise NotFoundError("No report of yours matches that id.")

    if report.get("image_path"):
        try:
            await report_images.delete_image(report["image_path"])
        except Exception:
            # The database deletion is already complete. Keep the user-facing
            # operation successful and let storage cleanup be retried by ops.
            pass
    return {"status": "deleted", "detail": "Your report has been withdrawn."}
