"""Air quality service (WAQI).

Two things changed here relative to the original implementation.

**The token no longer reaches the client.** ``GET /api/aqi/tile-url`` used to
return ``https://tiles.waqi.info/...?token=<WAQI_TOKEN>``, so every install of
the app held a working copy of the server's API key, extractable with a proxy
in about a minute. Tiles are now fetched server-side by :func:`fetch_tile` and
the client is handed a tokenless URL on this API.

**Every upstream read is cached and single-flighted.** WAQI station data updates
hourly at best, so a 5 minute TTL is generous, and :func:`~app.core.cache.Cache.get_or_set`
collapses concurrent misses for the same key into one upstream call.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any, Dict, List, Optional, Tuple

from app.core.cache import cache, cache_key, round_coord
from app.core.config import settings
from app.core.errors import ConfigurationMissingError, ValidationError
from app.core.http import UpstreamError, error_fields, get_client, get_json
from app.core.logging import get_logger

logger = get_logger(__name__)

PROVIDER = "WAQI"
WAQI_BASE = "https://api.waqi.info"
WAQI_TILE_BASE = "https://tiles.waqi.info/tiles/usepa-aqi"

# WAQI publishes tiles up to zoom 11; anything beyond is upscaled client-side.
MAX_TILE_ZOOM = 11
TILE_CONTENT_TYPE = "image/png"

ATTRIBUTION = "Air quality data © World Air Quality Index Project (waqi.info)"


# ---------------------------------------------------------------------------
# Categorisation
# ---------------------------------------------------------------------------
# US EPA breakpoints. Kept as an ordered table rather than a chain of ifs so the
# thresholds are auditable against the published standard at a glance.
_AQI_BANDS: Tuple[Tuple[int, str, str, int, str], ...] = (
    (
        50,
        "Good",
        "#4CAF50",
        1,
        "Air quality is satisfactory. Enjoy your usual outdoor activities.",
    ),
    (
        100,
        "Moderate",
        "#C9A227",
        2,
        "Acceptable air quality. Unusually sensitive people should consider "
        "reducing prolonged outdoor exertion.",
    ),
    (
        150,
        "Unhealthy for Sensitive Groups",
        "#FF9800",
        3,
        "Children, older adults, and people with heart or lung conditions "
        "should limit prolonged outdoor exertion.",
    ),
    (
        200,
        "Unhealthy",
        "#F44336",
        4,
        "Everyone may begin to experience health effects. Limit outdoor "
        "activity and wear a well-fitted mask outdoors.",
    ),
    (
        300,
        "Very Unhealthy",
        "#9C27B0",
        5,
        "Health alert: risk of serious effects for everyone. Avoid outdoor "
        "exertion and keep windows closed.",
    ),
)

_HAZARDOUS = (
    "Hazardous",
    "#800000",
    6,
    "Health warning of emergency conditions. Stay indoors, seal gaps, and use "
    "an air purifier if available.",
)

_UNKNOWN = (
    "Unknown",
    "#9E9E9E",
    0,
    "Air quality data is currently unavailable for this location.",
)


def aqi_category(aqi: Optional[int]) -> Dict[str, Any]:
    """Map an AQI value to a label, colour, level and health advisory.

    ``None`` maps to an explicit "Unknown" level 0 rather than to "Good".
    Rendering missing data as green is the single most dangerous failure mode
    for an app whose purpose is warning people.
    """
    if aqi is None:
        label, color, level, advice = _UNKNOWN
        return {"label": label, "color": color, "level": level, "advice": advice}

    for upper, label, color, level, advice in _AQI_BANDS:
        if aqi <= upper:
            return {"label": label, "color": color, "level": level, "advice": advice}

    label, color, level, advice = _HAZARDOUS
    return {"label": label, "color": color, "level": level, "advice": advice}


def _coerce_int(value: Any) -> Optional[int]:
    """WAQI returns AQI as an int, a numeric string, or ``"-"``."""
    if value is None or value == "-":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _iaqi_flat(iaqi: Optional[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {}
    for key, entry in (iaqi or {}).items():
        if isinstance(entry, dict):
            value = entry.get("v")
            if isinstance(value, (int, float)):
                out[key] = float(value)
    return out


def _shape_feed(raw: Dict[str, Any]) -> Dict[str, Any]:
    data = raw.get("data") or {}
    aqi = _coerce_int(data.get("aqi"))
    city = data.get("city") or {}
    geo = city.get("geo") or [None, None]
    time_block = data.get("time") or {}

    return {
        "uid": _coerce_int(data.get("idx")),
        "aqi": aqi,
        "dominant_pollutant": data.get("dominentpol"),
        "city": {
            "name": city.get("name"),
            "url": city.get("url"),
            "lat": geo[0] if len(geo) > 0 else None,
            "lon": geo[1] if len(geo) > 1 else None,
        },
        "time": time_block.get("iso") or time_block.get("s"),
        "iaqi": _iaqi_flat(data.get("iaqi")),
        "attributions": data.get("attributions") or [],
        "forecast": (data.get("forecast") or {}).get("daily") or {},
        "category": aqi_category(aqi),
    }


# ---------------------------------------------------------------------------
# Upstream access
# ---------------------------------------------------------------------------
def _require_token() -> str:
    token = settings.waqi_token.get_secret_value()
    if not token:
        raise ConfigurationMissingError(
            "Air quality data is unavailable: WAQI_TOKEN is not configured on this deployment."
        )
    return token


async def _waqi_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Call WAQI and unwrap its envelope.

    WAQI signals failure with HTTP 200 plus ``{"status": "error"}``, so the
    status field has to be checked explicitly — ``raise_for_status`` alone would
    let an error body through as valid data.
    """
    query = dict(params or {})
    query["token"] = _require_token()

    payload = await get_json(f"{WAQI_BASE}{path}", provider=PROVIDER, params=query)
    if not isinstance(payload, dict):
        raise UpstreamError(PROVIDER, "unexpected response shape")

    status = payload.get("status")
    if status != "ok":
        detail = payload.get("data")
        # "Unknown station" is a legitimate not-found, not an outage.
        if isinstance(detail, str) and "unknown" in detail.lower():
            raise ValidationError("No air quality station matches that location.")
        # The message may echo the query; log it, don't return it.
        logger.warning("WAQI returned a non-ok status", extra={"waqi_status": status})
        raise UpstreamError(PROVIDER, f"status={status}")
    return payload


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def by_coords(lat: float, lon: float) -> Dict[str, Any]:
    key = cache_key("aqi:geo", lat=round_coord(lat), lon=round_coord(lon))

    async def _fetch() -> Dict[str, Any]:
        raw = await _waqi_get(f"/feed/geo:{lat};{lon}/")
        return _shape_feed(raw)

    return await cache.get_or_set(key, settings.cache_ttl_aqi, _fetch)


async def by_city(city: str) -> Dict[str, Any]:
    slug = city.strip()
    if not slug or "/" in slug or ".." in slug:
        raise ValidationError("Invalid city name.")

    key = cache_key("aqi:city", city=slug.lower())

    async def _fetch() -> Dict[str, Any]:
        raw = await _waqi_get(f"/feed/{slug}/")
        return _shape_feed(raw)

    return await cache.get_or_set(key, settings.cache_ttl_aqi, _fetch)


async def by_station(uid: int) -> Dict[str, Any]:
    key = cache_key("aqi:station", uid=uid)

    async def _fetch() -> Dict[str, Any]:
        raw = await _waqi_get(f"/feed/@{uid}/")
        return _shape_feed(raw)

    return await cache.get_or_set(key, settings.cache_ttl_aqi, _fetch)


async def by_ip() -> Dict[str, Any]:
    """AQI for the *server's* IP location.

    Retained for backwards compatibility, but note the semantics: WAQI geolocates
    the caller, and the caller is this server, so in any real deployment this
    returns the datacentre's air quality, not the user's. The client should send
    coordinates instead. Deliberately not cached under a shared key for long,
    since it is effectively a constant per deployment.
    """
    key = cache_key("aqi:here")

    async def _fetch() -> Dict[str, Any]:
        raw = await _waqi_get("/feed/here/")
        return _shape_feed(raw)

    return await cache.get_or_set(key, settings.cache_ttl_aqi, _fetch)


async def search(keyword: str) -> List[Dict[str, Any]]:
    term = keyword.strip()
    if not term:
        raise ValidationError("Search keyword cannot be empty.")

    key = cache_key("aqi:search", q=term.lower())

    async def _fetch() -> List[Dict[str, Any]]:
        raw = await _waqi_get("/search/", {"keyword": term})
        results: List[Dict[str, Any]] = []
        for item in raw.get("data") or []:
            station = item.get("station") or {}
            geo = station.get("geo") or [None, None]
            aqi_val = _coerce_int(item.get("aqi"))
            results.append(
                {
                    "uid": _coerce_int(item.get("uid")),
                    "name": station.get("name"),
                    "country": station.get("country"),
                    "lat": geo[0] if len(geo) > 0 else None,
                    "lon": geo[1] if len(geo) > 1 else None,
                    "time": (item.get("time") or {}).get("stime"),
                    "aqi": aqi_val,
                    "category": aqi_category(aqi_val),
                }
            )
        return results

    return await cache.get_or_set(key, settings.cache_ttl_aqi, _fetch)


def _parse_bounds(latlng: str) -> Tuple[float, float, float, float]:
    parts = [p.strip() for p in latlng.split(",")]
    if len(parts) != 4:
        raise ValidationError("latlng must be four comma-separated numbers: lat1,lon1,lat2,lon2")
    try:
        lat1, lon1, lat2, lon2 = (float(p) for p in parts)
    except ValueError:
        raise ValidationError("latlng values must be numbers.") from None

    for lat in (lat1, lat2):
        if not -90 <= lat <= 90:
            raise ValidationError(f"Latitude out of range: {lat}")
    for lon in (lon1, lon2):
        if not -180 <= lon <= 180:
            raise ValidationError(f"Longitude out of range: {lon}")

    # A box spanning the whole planet would ask WAQI for every station on Earth.
    if abs(lat2 - lat1) > 40 or abs(lon2 - lon1) > 40:
        raise ValidationError("Bounding box is too large. Request at most 40 degrees per side.")
    return lat1, lon1, lat2, lon2


async def stations_in_bounds(latlng: str, networks: str = "all") -> List[Dict[str, Any]]:
    lat1, lon1, lat2, lon2 = _parse_bounds(latlng)
    normalised = (
        f"{round_coord(lat1, 1)},{round_coord(lon1, 1)},"
        f"{round_coord(lat2, 1)},{round_coord(lon2, 1)}"
    )
    net = networks if networks in ("all", "official") else "all"
    key = cache_key("aqi:bounds", box=normalised, net=net)

    async def _fetch() -> List[Dict[str, Any]]:
        raw = await _waqi_get("/map/bounds", {"latlng": normalised, "networks": net})
        stations: List[Dict[str, Any]] = []
        for item in raw.get("data") or []:
            station = item.get("station") or {}
            aqi_val = _coerce_int(item.get("aqi"))
            stations.append(
                {
                    "uid": _coerce_int(item.get("uid")),
                    "name": station.get("name"),
                    "lat": item.get("lat"),
                    "lon": item.get("lon"),
                    "time": station.get("time"),
                    "aqi": aqi_val,
                    "category": aqi_category(aqi_val),
                }
            )
        return stations

    return await cache.get_or_set(key, settings.cache_ttl_aqi, _fetch)


async def nearby_ranking(lat: float, lon: float, span: float = 1.0) -> List[Dict[str, Any]]:
    """Stations around a point with a known AQI, cleanest first."""
    latlng = f"{lat - span},{lon - span},{lat + span},{lon + span}"
    stations = await stations_in_bounds(latlng)
    scored = [
        {"name": s.get("name"), "aqi": s["aqi"]} for s in stations if isinstance(s.get("aqi"), int)
    ]
    scored.sort(key=lambda s: s["aqi"])
    return scored


# ---------------------------------------------------------------------------
# Tile proxy
# ---------------------------------------------------------------------------
def validate_tile_coords(z: int, x: int, y: int) -> None:
    """Reject out-of-range tile coordinates.

    These values are interpolated into an upstream URL, so they are validated
    as integers within the zoom's valid grid. Without this the endpoint would
    be an open request-forwarder.
    """
    if not 0 <= z <= MAX_TILE_ZOOM:
        raise ValidationError(f"Zoom must be between 0 and {MAX_TILE_ZOOM}.")
    limit = 1 << z
    if not 0 <= x < limit or not 0 <= y < limit:
        raise ValidationError(f"Tile coordinates out of range for zoom {z}.")


async def fetch_tile(z: int, x: int, y: int) -> bytes:
    """Fetch one AQI raster tile, authenticating with the server-side token."""
    validate_tile_coords(z, x, y)
    token = _require_token()

    key = cache_key("aqi:tile", z=z, x=x, y=y)
    cached = await cache.get(key)
    if isinstance(cached, str):
        # Cached as base64 because the cache serialises through JSON.
        return base64.b64decode(cached)

    url = f"{WAQI_TILE_BASE}/{z}/{x}/{y}.png"
    client = get_client()
    try:
        response = await client.get(url, params={"token": token})
        response.raise_for_status()
    except Exception as exc:
        raise UpstreamError(PROVIDER, f"tile fetch failed: {type(exc).__name__}") from exc

    content = response.content
    # Don't let an unexpectedly large response evict the whole cache.
    if len(content) <= 2 * 1024 * 1024:
        await cache.set(key, base64.b64encode(content).decode("ascii"), settings.cache_ttl_aqi)
    return content


# ---------------------------------------------------------------------------
# Combined dashboard payload
# ---------------------------------------------------------------------------
async def location_intel(lat: float, lon: float) -> Dict[str, Any]:
    """AQI + current weather + local ranking, fetched concurrently.

    Each sub-request can fail independently. When one does, its slot is ``None``
    *and* the provider is named in ``unavailable`` with ``partial=True`` — the
    client needs to distinguish "no alerts" from "we couldn't check", and a bare
    ``None`` does not carry that distinction.
    """
    from app.services import weather as weather_service

    aqi_result, weather_result, ranking_result = await asyncio.gather(
        by_coords(lat, lon),
        weather_service.current(lat, lon),
        nearby_ranking(lat, lon),
        return_exceptions=True,
    )

    unavailable: List[str] = []

    aqi_data: Optional[Dict[str, Any]] = None
    if isinstance(aqi_result, dict):
        aqi_data = aqi_result
    else:
        unavailable.append("air quality")
        logger.warning("location_intel: AQI leg failed", extra=error_fields(aqi_result))

    weather_data: Optional[Dict[str, Any]] = None
    if isinstance(weather_result, dict):
        weather_data = weather_result.get("current") or {}
    else:
        unavailable.append("weather")
        logger.warning("location_intel: weather leg failed", extra=error_fields(weather_result))

    nearby: List[Dict[str, Any]] = ranking_result if isinstance(ranking_result, list) else []
    if not isinstance(ranking_result, list):
        unavailable.append("station ranking")
        # This leg used to fail silently into `unavailable` with no log line at
        # all, so a persistently broken ranking was invisible in production.
        logger.warning("location_intel: ranking leg failed", extra=error_fields(ranking_result))

    rank: Optional[int] = None
    if nearby and aqi_data is not None:
        user_aqi = aqi_data.get("aqi")
        if isinstance(user_aqi, int):
            # Rank among nearby stations, cleanest first.
            rank = 1 + sum(1 for s in nearby if s["aqi"] < user_aqi)

    return {
        "aqi": aqi_data,
        "weather": weather_data,
        "ranking": {
            "rank": rank,
            "total": len(nearby),
            "nearby_stations": nearby[:10],
        },
        "partial": bool(unavailable),
        "unavailable": unavailable,
    }


__all__ = [
    "ATTRIBUTION",
    "MAX_TILE_ZOOM",
    "TILE_CONTENT_TYPE",
    "aqi_category",
    "by_city",
    "by_coords",
    "by_ip",
    "by_station",
    "fetch_tile",
    "location_intel",
    "nearby_ranking",
    "search",
    "stations_in_bounds",
    "validate_tile_coords",
]
