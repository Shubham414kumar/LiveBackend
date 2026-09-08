"""Weather, radar and flood-risk endpoints."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Query

from app.api.deps import read_limit
from app.models.schemas import (
    FloodRisk,
    ForecastResponse,
    NowcastResponse,
    Latitude,
    Longitude,
    TileUrlResponse,
    WeatherResponse,
)
from app.services import weather as weather_service

router = APIRouter(tags=["weather"], dependencies=[Depends(read_limit)])


@router.get("/weather/forecast", response_model=ForecastResponse, summary="Ensemble weather forecast")
async def weather_forecast(lat: Latitude = Query(...), lon: Longitude = Query(...), hours: int = Query(48, ge=1, le=48), days: int = Query(7, ge=1, le=7)) -> Dict[str, Any]:
    return await weather_service.forecast(lat, lon, hours, days)


@router.get("/weather/nowcast", response_model=NowcastResponse, summary="Short-range precipitation nowcast")
async def weather_nowcast(lat: Latitude = Query(...), lon: Longitude = Query(...)) -> Dict[str, Any]:
    return await weather_service.nowcast(lat, lon)


@router.get("/weather/current", response_model=WeatherResponse, summary="Current weather")
async def current_weather(
    lat: Latitude = Query(...),
    lon: Longitude = Query(...),
) -> Dict[str, Any]:
    return await weather_service.current(lat, lon)


@router.get(
    "/weather/radar/tile-url",
    response_model=TileUrlResponse,
    summary="Newest published radar frame",
)
async def radar_tile_url() -> Dict[str, Any]:
    """Resolve a radar tile template from RainViewer's manifest.

    ``frame_time`` is part of the response because the previous implementation
    fabricated a timestamp and the overlay silently rendered blank or stale; the
    client can now display how old the frame it is showing actually is.
    """
    return await weather_service.radar_tile_url()


@router.get(
    "/weather/clouds/tile-url",
    response_model=TileUrlResponse,
    summary="Radar tile template (legacy path)",
    deprecated=True,
    description="Alias of /weather/radar/tile-url, kept for older app builds.",
)
async def clouds_tile_url() -> Dict[str, Any]:
    return await weather_service.radar_tile_url()


@router.get(
    "/flood-risk",
    response_model=FloodRisk,
    summary="Rainfall and event based flood indicator",
    description=(
        "An indicator, not a hydrological forecast: it combines 7-day forecast "
        "rainfall with nearby active GDACS flood events and knows nothing about "
        "elevation, drainage or river levels. `complete` is false when a source "
        "was unavailable, and `risk_level` is then 'Unknown' rather than a "
        "reassuring low score."
    ),
)
async def flood_risk(
    lat: Latitude = Query(...),
    lon: Longitude = Query(...),
) -> Dict[str, Any]:
    return await weather_service.flood_risk(lat, lon)
