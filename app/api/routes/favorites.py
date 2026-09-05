"""Saved locations and their alerts.

Every route here is device-scoped. The version this replaces had two real data
leaks: ``GET /api/favorites`` selected the whole table with no filter, so each
user saw every user's saved locations, and ``DELETE /api/favorites/{uid}``
deleted by *station* id with no ownership check, so any client could delete
anyone's. Both are structurally impossible now — the repository's scoped methods
take ``device_id`` as a required argument.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Path, status

from app.api.deps import read_limit, write_limit
from app.core.errors import NotFoundError, RateLimitedError
from app.core.identity import require_device_id
from app.core.logging import get_logger
from app.db.repositories import MAX_FAVORITES_PER_DEVICE, favorites_repo
from app.models.schemas import (
    Favorite,
    FavoriteAlertsResponse,
    FavoriteCreate,
    FavoritesResponse,
    SimpleStatus,
)
from app.services import alert_match, disasters as disasters_service

logger = get_logger(__name__)

router = APIRouter(prefix="/favorites", tags=["favorites"])

# Cap on how far ahead an alert radius can reach, so one favourite cannot match
# every event on the planet. Defined in the matching service and re-exported
# here: the push worker applies the same cap, and two constants that must agree
# is one constant waiting to drift.
MAX_ALERT_RADIUS_KM = alert_match.MAX_ALERT_RADIUS_KM


def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
    """Project a row to the wire shape, dropping ``device_id``.

    The device id is deliberately not serialised: it is the closest thing this
    app has to a user identifier, and it has no business being echoed back to a
    client that might log or forward it.
    """
    return {
        "id": str(row.get("id")),
        "name": row.get("name") or "",
        "lat": row.get("lat"),
        "lon": row.get("lon"),
        "station_uid": row.get("station_uid"),
        "alert_radius_km": float(row.get("alert_radius_km") or 100.0),
        "created_at": row.get("created_at"),
    }


@router.get(
    "",
    response_model=FavoritesResponse,
    dependencies=[Depends(read_limit)],
    summary="List this device's saved locations",
)
async def list_favorites(
    device_id: str = Depends(require_device_id),
) -> Dict[str, Any]:
    rows = await favorites_repo.list_for_device(device_id)
    favorites = [_shape(row) for row in rows]
    return {
        "favorites": favorites,
        "count": len(favorites),
        "limit": MAX_FAVORITES_PER_DEVICE,
    }


@router.post(
    "",
    response_model=Favorite,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(write_limit)],
    summary="Save a location",
)
async def create_favorite(
    payload: FavoriteCreate,
    device_id: str = Depends(require_device_id),
) -> Dict[str, Any]:
    existing = await favorites_repo.count_for_device(device_id)
    if existing >= MAX_FAVORITES_PER_DEVICE:
        raise RateLimitedError(
            f"You can save up to {MAX_FAVORITES_PER_DEVICE} locations. Remove one to add another.",
            retry_after=0,
            limit=MAX_FAVORITES_PER_DEVICE,
        )

    row, created = await favorites_repo.create(
        device_id,
        name=payload.name,
        lat=payload.lat,
        lon=payload.lon,
        station_uid=payload.station_uid,
        alert_radius_km=min(payload.alert_radius_km, MAX_ALERT_RADIUS_KM),
    )
    if not row:
        raise NotFoundError("The location could not be saved. Please try again.")
    if not created:
        logger.info("Favourite already existed; returning the existing row")
    return _shape(row)


@router.get(
    "/alerts",
    response_model=FavoriteAlertsResponse,
    dependencies=[Depends(read_limit)],
    summary="Active events near each saved location",
)
async def favorite_alerts(
    device_id: str = Depends(require_device_id),
) -> Dict[str, Any]:
    """Match this device's saved locations against the merged disaster feed.

    One aggregate fetch is matched against every favourite in memory, rather than
    one upstream round trip per favourite. ``sources_failed`` is passed straight
    through: "no alerts near your saved locations" and "we could not check" must
    not look the same to someone deciding whether to travel.

    The matching itself is :func:`app.services.alert_match.match_favorites`,
    shared with the push dispatcher. This screen and the notifications a user
    receives must never disagree about what is happening near them, and the only
    way to guarantee that is for both to run the same code.
    """
    rows = await favorites_repo.list_for_device(device_id)
    if not rows:
        return {"favorites": [], "partial": False, "sources_failed": []}

    events, failed = await disasters_service.aggregate(hours=72)

    matched = alert_match.match_favorites([_shape(row) for row in rows], events)

    results: List[Dict[str, Any]] = [
        {
            "favorite": entry.favorite,
            # The true total, even though the list below is truncated: the client
            # renders a summary card, and a favourite with a wide radius during an
            # active season can match hundreds.
            "alert_count": entry.total,
            "alerts": entry.top(alert_match.MAX_ALERTS_PER_FAVORITE),
        }
        for entry in matched
    ]

    return {
        "favorites": results,
        "partial": bool(failed),
        "sources_failed": failed,
    }


@router.delete(
    "/{favorite_id}",
    response_model=SimpleStatus,
    dependencies=[Depends(write_limit)],
    summary="Remove a saved location",
)
async def delete_favorite(
    favorite_id: str = Path(..., min_length=1, max_length=64),
    device_id: str = Depends(require_device_id),
) -> Dict[str, Any]:
    """Delete by favourite id, scoped to this device.

    Note the change of key: the old endpoint took a WAQI *station* uid, which is
    not unique per user and is guessable, so it could delete another device's row.
    """
    deleted = await favorites_repo.delete(device_id, favorite_id)
    if not deleted:
        raise NotFoundError("No saved location of yours matches that id.")
    return {"status": "deleted", "detail": "Location removed."}
