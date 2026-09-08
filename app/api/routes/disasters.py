"""Disaster and hazard feed endpoints.

Every list response carries ``sources_failed`` and ``partial``. This is the
single most important behavioural change in the API: the previous version
swallowed upstream failures into an empty list, so "GDACS is down" and "there
are no active disasters" produced byte-identical responses and the app showed a
green all-clear during an outage.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Path, Query

from app.api.deps import read_limit
from app.models.schemas import CategoryMeta, DisasterFeed, DisasterNewsResponse, EmergencyContacts
from app.services import disasters as disasters_service, emergency as emergency_service

router = APIRouter(prefix="/disasters", tags=["disasters"], dependencies=[Depends(read_limit)])


def _feed(events: List[Dict[str, Any]], failed: Optional[List[str]] = None) -> Dict[str, Any]:
    failures = failed or []
    return {
        "events": events,
        "count": len(events),
        "sources_failed": failures,
        "partial": bool(failures),
    }


@router.get(
    "/categories",
    response_model=Dict[str, CategoryMeta],
    summary="Category labels, icons and colours",
)
async def categories() -> Dict[str, Any]:
    return disasters_service.CATEGORY_META


@router.get("/earthquakes", response_model=DisasterFeed, summary="Recent earthquakes")
async def earthquakes(
    min_mag: float = Query(4.5, ge=0, le=10, description="Minimum magnitude"),
    hours: int = Query(24, ge=1, le=720, description="Look-back window in hours"),
) -> Dict[str, Any]:
    return _feed(await disasters_service.earthquakes(min_mag=min_mag, hours=hours))


@router.get("/events", response_model=DisasterFeed, summary="NASA EONET open events")
async def eonet_events(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(100, ge=1, le=300),
) -> Dict[str, Any]:
    return _feed(await disasters_service.eonet_events(days=days, limit=limit))


@router.get("/gdacs", response_model=DisasterFeed, summary="GDACS alerts")
async def gdacs(days: int = Query(10, ge=1, le=90)) -> Dict[str, Any]:
    return _feed(await disasters_service.gdacs_events(days=days))


@router.get("/news", response_model=DisasterNewsResponse, summary="Recent disaster news")
async def news(category: Optional[str] = Query(None, max_length=30), limit: int = Query(20, ge=1, le=50)) -> Dict[str, Any]:
    try:
        articles = await disasters_service.disaster_news(category, limit)
    except Exception:
        return {"articles": [], "count": 0, "source_available": False, "partial": True}
    return {"articles": articles, "count": len(articles), "source_available": True, "partial": False}


@router.get(
    "/all",
    response_model=DisasterFeed,
    summary="Every source, merged and deduplicated",
)
async def all_events(
    hours: int = Query(48, ge=1, le=720),
    min_mag: float = Query(4.5, ge=0, le=10),
    categories: Optional[str] = Query(
        None,
        max_length=200,
        description="Comma-separated category filter, e.g. flood,wildfire",
    ),
    include_pandemics: bool = Query(True),
) -> Dict[str, Any]:
    events, failed = await disasters_service.aggregate(
        hours=hours,
        min_mag=min_mag,
        categories=categories,
        include_pandemics=include_pandemics,
    )
    return _feed(events, failed)


@router.get(
    "/emergency/{iso2}",
    response_model=EmergencyContacts,
    summary="Emergency numbers for a country",
)
async def emergency(
    iso2: str = Path(..., min_length=2, max_length=2, pattern="^[A-Za-z]{2}$"),
) -> Dict[str, Any]:
    return emergency_service.emergency_contacts_for(iso2)


@router.get(
    "/emergency",
    response_model=List[str],
    summary="Countries with verified emergency numbers on file",
)
async def emergency_countries() -> List[str]:
    return emergency_service.supported_countries()
