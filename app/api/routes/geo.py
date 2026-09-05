"""Geocoding and nearby-places endpoints."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Query

from app.api.deps import read_limit
from app.models.schemas import (
    HospitalsResponse,
    Latitude,
    Longitude,
    ReverseGeocode,
)
from app.services import geo as geo_service

router = APIRouter(tags=["places"], dependencies=[Depends(read_limit)])


@router.get(
    "/geo/reverse",
    response_model=ReverseGeocode,
    summary="Resolve a coordinate to a place and its emergency numbers",
    description=(
        "`resolved` is false when the geocoder was unavailable; the coordinates "
        "are still returned and `contacts` falls back to the universal GSM "
        "number flagged `verified: false`."
    ),
)
async def reverse_geocode(
    lat: Latitude = Query(...),
    lon: Longitude = Query(...),
) -> Dict[str, Any]:
    return await geo_service.reverse_geocode(lat, lon)


@router.get(
    "/nearby/hospitals",
    response_model=HospitalsResponse,
    summary="Hospitals and emergency clinics near a coordinate",
)
async def nearby_hospitals(
    lat: Latitude = Query(...),
    lon: Longitude = Query(...),
    radius_km: float = Query(
        geo_service.DEFAULT_HOSPITAL_RADIUS_KM,
        gt=0,
        le=geo_service.MAX_HOSPITAL_RADIUS_KM,
        description="Search radius in kilometres",
    ),
) -> Dict[str, Any]:
    return await geo_service.nearby_hospitals(lat, lon, radius_km)
