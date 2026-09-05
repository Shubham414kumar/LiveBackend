"""Weather, radar and flood-risk service (Open-Meteo + RainViewer).

Three fixes carried over from the audit.

**UV index was read against the wrong clock.** The original matched
``datetime.now().strftime("%Y-%m-%dT%H:00")`` — the *server's* local hour —
against ``hourly.time``, which Open-Meteo returns in the *queried location's*
timezone when ``timezone=auto``. Anywhere outside the server's own timezone the
lookup missed and UV silently reported 0, i.e. "no sun". It now anchors on the
API's own ``current.time`` so both sides of the comparison are in the same frame.

**The radar tile timestamp was fabricated.** ``int(time.time()/600)*600``
produced a plausible-looking epoch that RainViewer had almost certainly never
published a frame for, so the overlay was usually blank. The real frame index is
now fetched from RainViewer's manifest.

**Failures were indistinguishable from calm weather.** Each response reports
whether it is complete.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional, Tuple

from app.core.cache import cache, cache_key, round_coord
from app.core.config import settings
from app.core.geoutils import haversine_km
from app.core.http import UpstreamError, get_json
from app.core.logging import get_logger

logger = get_logger(__name__)

PROVIDER = "Open-Meteo"
RADAR_PROVIDER = "RainViewer"

OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"
RAINVIEWER_INDEX = "https://api.rainviewer.com/public/weather-maps.json"

ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0)"
RADAR_ATTRIBUTION = "Radar imagery © RainViewer"

_CURRENT_VARS = (
    "temperature_2m",
    "relative_humidity_2m",
    "apparent_temperature",
    "precipitation",
    "rain",
    "showers",
    "snowfall",
    "weather_code",
    "surface_pressure",
    "wind_speed_10m",
    "wind_direction_10m",
)


def _resolve_uv_index(payload: Dict[str, Any]) -> Optional[float]:
    """Pull the UV index for the current hour out of the hourly series.

    Both ``current.time`` and ``hourly.time`` are returned by Open-Meteo in the
    queried location's timezone, so comparing them is sound. Comparing either
    against the server's own clock is not, which is what used to happen.
    """
    current = payload.get("current") or {}
    hourly = payload.get("hourly") or {}
    times: List[str] = hourly.get("time") or []
    values: List[Optional[float]] = hourly.get("uv_index") or []

    if not times or not values:
        return None

    current_time = current.get("time")
    if isinstance(current_time, str) and len(current_time) >= 13:
        target = current_time[:13] + ":00"
        try:
            index = times.index(target)
        except ValueError:
            index = None
        if index is not None and index < len(values):
            value = values[index]
            return float(value) if isinstance(value, (int, float)) else None

    # Fall back to the first non-null reading rather than reporting a
    # confident zero.
    for value in values:
        if isinstance(value, (int, float)):
            return float(value)
    return None


async def current(lat: float, lon: float) -> Dict[str, Any]:
    """Current conditions for a coordinate, including a real UV index."""
    key = cache_key("weather:current", lat=round_coord(lat), lon=round_coord(lon))

    async def _fetch() -> Dict[str, Any]:
        payload = await get_json(
            OPEN_METEO_BASE,
            provider=PROVIDER,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": ",".join(_CURRENT_VARS),
                "hourly": "uv_index",
                "forecast_days": 1,
                "timezone": "auto",
            },
        )
        if not isinstance(payload, dict):
            raise UpstreamError(PROVIDER, "unexpected response shape")

        current_block = dict(payload.get("current") or {})
        current_block["uv_index"] = _resolve_uv_index(payload)

        return {
            "latitude": payload.get("latitude"),
            "longitude": payload.get("longitude"),
            "timezone": payload.get("timezone"),
            "elevation": payload.get("elevation"),
            "current": current_block,
        }

    return await cache.get_or_set(key, settings.cache_ttl_weather, _fetch)


async def _rainfall_forecast(lat: float, lon: float) -> Dict[str, Any]:
    key = cache_key("weather:rain7d", lat=round_coord(lat, 1), lon=round_coord(lon, 1))

    async def _fetch() -> Dict[str, Any]:
        payload = await get_json(
            OPEN_METEO_BASE,
            provider=PROVIDER,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "precipitation_sum,rain_sum",
                "forecast_days": 7,
                "timezone": "auto",
            },
        )
        if not isinstance(payload, dict):
            raise UpstreamError(PROVIDER, "unexpected response shape")
        return payload

    return await cache.get_or_set(key, settings.cache_ttl_weather, _fetch)


def _score_flood_risk(total_rain_mm: float, nearby_events: int) -> Tuple[int, str]:
    """Combine forecast rainfall and nearby active floods into a 0-100 score.

    This is an explicit heuristic, not a hydrological model. It has no knowledge
    of elevation, drainage, soil saturation or river levels, so it is presented
    to users as an indicator alongside official warnings, never in place of them.
    """
    score = min(100, int(total_rain_mm * 0.8 + nearby_events * 15))
    if score > 70:
        return score, "High"
    if score > 40:
        return score, "Moderate"
    if score > 15:
        return score, "Low"
    return score, "Very Low"


async def flood_risk(lat: float, lon: float) -> Dict[str, Any]:
    """Rainfall-and-events based flood indicator."""
    from app.services import disasters as disasters_service

    rainfall_result, events_result = await asyncio.gather(
        _rainfall_forecast(lat, lon),
        disasters_service.gdacs_events(days=14),
        return_exceptions=True,
    )

    complete = True

    total_rain = 0.0
    daily: List[Dict[str, Any]] = []
    if isinstance(rainfall_result, dict):
        block = rainfall_result.get("daily") or {}
        times: List[str] = block.get("time") or []
        precip: List[Optional[float]] = block.get("precipitation_sum") or []
        for index, day in enumerate(times):
            value = precip[index] if index < len(precip) else None
            millimetres = float(value) if isinstance(value, (int, float)) else 0.0
            total_rain += millimetres
            daily.append({"date": day, "rain_mm": round(millimetres, 1)})
    else:
        complete = False
        logger.warning(
            "flood_risk: rainfall leg failed",
            extra={"error": type(rainfall_result).__name__},
        )

    nearby_floods = 0
    if isinstance(events_result, list):
        for event in events_result:
            if event.get("category") != "flood":
                continue
            event_lat, event_lon = event.get("lat"), event.get("lon")
            if not isinstance(event_lat, (int, float)) or not isinstance(event_lon, (int, float)):
                continue
            if haversine_km(lat, lon, float(event_lat), float(event_lon)) <= 500:
                nearby_floods += 1
    else:
        complete = False
        logger.warning(
            "flood_risk: GDACS leg failed",
            extra={"error": type(events_result).__name__},
        )

    if not complete and not daily:
        # Nothing usable came back. Say so instead of scoring zero risk, which
        # would render as a reassuring "Very Low".
        return {
            "flood_probability": 0,
            "risk_level": "Unknown",
            "total_rain_7d_mm": 0.0,
            "daily_forecast": [],
            "nearby_flood_events": 0,
            "advice": (
                "Flood risk could not be assessed right now — the rainfall and "
                "disaster feeds are unavailable. Check official local warnings."
            ),
            "complete": False,
        }

    score, level = _score_flood_risk(total_rain, nearby_floods)
    rounded = round(total_rain, 1)

    if score > 40:
        advice = (
            f"{rounded} mm of rain forecast over 7 days with {nearby_floods} active "
            "flood event(s) within 500 km. Stay alert and follow official warnings."
        )
    else:
        advice = (
            f"{rounded} mm of rain forecast over 7 days. Flood indicators are low "
            "for your area, but this is an estimate, not an official forecast."
        )
    if not complete:
        advice += " Some data sources were unavailable, so this estimate is incomplete."

    return {
        "flood_probability": score,
        "risk_level": level,
        "total_rain_7d_mm": rounded,
        "daily_forecast": daily,
        "nearby_flood_events": nearby_floods,
        "advice": advice,
        "complete": complete,
    }


async def radar_tile_url() -> Dict[str, Any]:
    """Resolve the newest published RainViewer radar frame.

    RainViewer only serves tiles for timestamps listed in its manifest, so the
    manifest has to be read rather than guessed. Cached briefly: frames appear
    roughly every ten minutes.
    """
    key = cache_key("weather:radar-frame")

    async def _fetch() -> Dict[str, Any]:
        payload = await get_json(RAINVIEWER_INDEX, provider=RADAR_PROVIDER)
        if not isinstance(payload, dict):
            raise UpstreamError(RADAR_PROVIDER, "unexpected response shape")

        host = payload.get("host") or "https://tilecache.rainviewer.com"
        radar = payload.get("radar") or {}
        frames = radar.get("nowcast") or []
        past = radar.get("past") or []

        # Prefer the most recent observed frame over a forecast frame: this
        # overlay is labelled as observed radar.
        frame = (past or frames)[-1] if (past or frames) else None
        if not frame or not frame.get("path"):
            raise UpstreamError(RADAR_PROVIDER, "no radar frames published")

        frame_time = frame.get("time")
        iso_time = (
            datetime.fromtimestamp(frame_time, tz=UTC).isoformat()
            if isinstance(frame_time, (int, float))
            else None
        )
        return {
            # 256px tiles, colour scheme 2, smoothed with no snow layer.
            "tile_url": f"{host}{frame['path']}/256/{{z}}/{{x}}/{{y}}/2/1_1.png",
            "attribution": RADAR_ATTRIBUTION,
            "frame_time": iso_time,
        }

    # 5 minutes: long enough to collapse a burst of map pans, short enough that
    # the overlay never lags more than one frame behind.
    return await cache.get_or_set(key, 300, _fetch)


__all__ = [
    "ATTRIBUTION",
    "RADAR_ATTRIBUTION",
    "current",
    "flood_risk",
    "radar_tile_url",
]
