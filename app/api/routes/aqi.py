"""Air quality endpoints."""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from app.api.deps import read_limit
from app.models.schemas import (
    AqiBoundsResponse,
    AqiReading,
    AqiSearchResponse,
    Latitude,
    LocationIntel,
    Longitude,
    TileUrlResponse,
)
from app.services import aqi as aqi_service

router = APIRouter(prefix="/aqi", tags=["air quality"], dependencies=[Depends(read_limit)])


@router.get("/geo", response_model=AqiReading, summary="AQI at a coordinate")
async def aqi_by_coords(
    lat: Latitude = Query(..., description="Latitude"),
    lon: Longitude = Query(..., description="Longitude"),
) -> Dict[str, Any]:
    return await aqi_service.by_coords(lat, lon)


@router.get("/city/{city}", response_model=AqiReading, summary="AQI for a named city")
async def aqi_by_city(
    city: str = Path(..., min_length=1, max_length=120),
) -> Dict[str, Any]:
    return await aqi_service.by_city(city)


@router.get("/station/{uid}", response_model=AqiReading, summary="AQI for one station")
async def aqi_by_station(uid: int = Path(..., ge=1)) -> Dict[str, Any]:
    return await aqi_service.by_station(uid)


@router.get(
    "/here",
    response_model=AqiReading,
    summary="AQI at the server's IP location",
    deprecated=True,
    description=(
        "Geolocates the *caller of WAQI*, which is this server — so in any real "
        "deployment it returns the datacentre's air quality, not the user's. "
        "Kept for backwards compatibility; use /aqi/geo with coordinates."
    ),
)
async def aqi_here() -> Dict[str, Any]:
    return await aqi_service.by_ip()


@router.get("/search", response_model=AqiSearchResponse, summary="Search stations")
async def aqi_search(
    keyword: str = Query(..., min_length=1, max_length=120),
) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = await aqi_service.search(keyword)
    return {"results": results}


@router.get("/bounds", response_model=AqiBoundsResponse, summary="Stations in a box")
async def aqi_bounds(
    latlng: str = Query(
        ...,
        max_length=100,
        description="lat1,lon1,lat2,lon2 — at most 40 degrees per side",
        examples=["28.4,76.8,28.9,77.4"],
    ),
    networks: str = Query("all", pattern="^(all|official)$"),
) -> Dict[str, Any]:
    stations = await aqi_service.stations_in_bounds(latlng, networks)
    return {"stations": stations, "count": len(stations)}


@router.get(
    "/tile-url",
    response_model=TileUrlResponse,
    summary="Tokenless template URL for the AQI tile layer",
)
async def aqi_tile_url(request: Request) -> Dict[str, Any]:
    """Return a tile template pointing at *this* API, not at WAQI.

    The previous implementation returned WAQI's URL with the deployment's token
    embedded in the query string, which handed a working API key to every
    installed copy of the app. Tiles now come from the proxy below, which
    attaches the token server-side.
    """
    base = str(request.base_url).rstrip("/")
    return {
        "tile_url": f"{base}/api/aqi/tiles/{{z}}/{{x}}/{{y}}.png",
        "attribution": aqi_service.ATTRIBUTION,
        "max_zoom": aqi_service.MAX_TILE_ZOOM,
    }


@router.get(
    "/tiles/{z}/{x}/{y}.png",
    summary="AQI raster tile proxy",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
)
async def aqi_tile(
    z: int = Path(..., ge=0, le=aqi_service.MAX_TILE_ZOOM),
    x: int = Path(..., ge=0),
    y: int = Path(..., ge=0),
) -> Response:
    content = await aqi_service.fetch_tile(z, x, y)
    return Response(
        content=content,
        media_type=aqi_service.TILE_CONTENT_TYPE,
        headers={
            # Tiles are immutable for the life of a data refresh, so unlike the
            # JSON endpoints these are safe (and worth) caching in the client.
            "Cache-Control": "public, max-age=300",
        },
    )


@router.get(
    "/location-intel",
    response_model=LocationIntel,
    summary="AQI, weather and local ranking in one call",
)
async def location_intel(
    lat: Latitude = Query(...),
    lon: Longitude = Query(...),
) -> Dict[str, Any]:
    return await aqi_service.location_intel(lat, lon)
