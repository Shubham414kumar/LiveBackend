"""Geocoding and nearby places (Nominatim + Overpass).

Both upstreams are volunteer-run OpenStreetMap infrastructure with published
usage policies, and both enforce them by banning IPs rather than throttling.
That shapes this module:

* Requests go through :mod:`app.core.http`, which paces Nominatim to one request
  per 1.1 s and Overpass to two concurrent requests, and sends a contactable
  ``User-Agent`` built from ``CONTACT_EMAIL``.
* Results are cached for a day. Reverse geocodes and hospital locations do not
  change minute to minute, so re-asking is pure waste and pure risk.
* Coordinates are quantised before they become part of a cache key, which turns
  a continuous stream of GPS readings into a bounded keyspace.

The original code called these APIs with a hardcoded ``AirLens/1.0`` UA and no
pacing at all, from a fresh client per request.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.core.cache import cache, cache_key, round_coord
from app.core.config import settings
from app.core.geoutils import haversine_km
from app.core.http import UpstreamError, get_json, post_json
from app.core.logging import get_logger
from app.services.emergency import emergency_contacts_for

logger = get_logger(__name__)

NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"

NOMINATIM_PROVIDER = "Nominatim"
OVERPASS_PROVIDER = "Overpass"

OSM_ATTRIBUTION = "© OpenStreetMap contributors"

# Overpass charges by area. 25 km covers "hospitals near me" without asking a
# shared volunteer service to scan a region.
MAX_HOSPITAL_RADIUS_KM = 25.0
DEFAULT_HOSPITAL_RADIUS_KM = 10.0
MAX_HOSPITAL_RESULTS = 30

# Overpass occasionally sits on a request for a long time; cap it below the
# global HTTP timeout's usefulness threshold and fail fast instead.
_OVERPASS_TIMEOUT = 25.0


def _pick_city(address: Dict[str, Any]) -> Optional[str]:
    """Nominatim labels the settlement differently by country and place size."""
    for field in (
        "city",
        "town",
        "village",
        "municipality",
        "suburb",
        "county",
        "state_district",
    ):
        value = address.get(field)
        if value:
            return str(value)
    return None


async def reverse_geocode(lat: float, lon: float) -> Dict[str, Any]:
    """Resolve a coordinate to a place, with emergency numbers for its country.

    On failure this returns the coordinates with ``resolved: False`` rather than
    raising, because the caller's real question is usually "which country am I
    in, and what number do I dial" — and answering "unknown country, here is the
    universal GSM number, flagged unverified" is more useful than an error page
    to someone who may be in trouble.
    """
    key = cache_key("geo:reverse", lat=round_coord(lat, 3), lon=round_coord(lon, 3))

    async def _fetch() -> Dict[str, Any]:
        payload = await get_json(
            NOMINATIM_REVERSE,
            provider=NOMINATIM_PROVIDER,
            params={
                "lat": lat,
                "lon": lon,
                "format": "jsonv2",
                # 10 = city level. Finer zoom returns building-level detail we
                # do not need and would be a privacy regression to cache.
                "zoom": 10,
                "addressdetails": 1,
            },
        )
        if not isinstance(payload, dict):
            raise UpstreamError(NOMINATIM_PROVIDER, "unexpected response shape")

        address = payload.get("address") or {}
        iso2 = (address.get("country_code") or "").upper() or None

        return {
            "iso2": iso2,
            "country": address.get("country"),
            "state": address.get("state"),
            "city": _pick_city(address),
            "display_name": payload.get("display_name"),
        }

    try:
        place = await cache.get_or_set(key, settings.cache_ttl_geocode, _fetch)
        resolved = True
    except Exception as exc:
        logger.warning(
            "Reverse geocode failed; returning coordinates only",
            extra={"error": type(exc).__name__},
        )
        place = {
            "iso2": None,
            "country": None,
            "state": None,
            "city": None,
            "display_name": None,
        }
        resolved = False

    return {
        "lat": lat,
        "lon": lon,
        **place,
        "contacts": emergency_contacts_for(place.get("iso2")),
        "resolved": resolved,
    }


async def country_code(lat: float, lon: float) -> Optional[str]:
    """ISO2 for a coordinate, or ``None``. Never raises."""
    result = await reverse_geocode(lat, lon)
    iso2 = result.get("iso2")
    return str(iso2) if iso2 else None


def _overpass_query(lat: float, lon: float, radius_m: int) -> str:
    """Build an Overpass QL query for hospitals and clinics.

    ``nwr`` covers nodes, ways and relations in one pass — a large hospital is
    usually mapped as a building way or a site relation, not a point, so a
    node-only query (what the original code sent) misses most of them.
    """
    return f"""
[out:json][timeout:25];
(
  nwr["amenity"="hospital"](around:{radius_m},{lat},{lon});
  nwr["amenity"="clinic"]["emergency"="yes"](around:{radius_m},{lat},{lon});
);
out tags center {MAX_HOSPITAL_RESULTS * 3};
""".strip()


def _element_coords(element: Dict[str, Any]) -> Optional[tuple[float, float]]:
    if isinstance(element.get("lat"), (int, float)) and isinstance(
        element.get("lon"), (int, float)
    ):
        return float(element["lat"]), float(element["lon"])
    center = element.get("center")
    if isinstance(center, dict) and isinstance(center.get("lat"), (int, float)):
        return float(center["lat"]), float(center["lon"])
    return None


def _element_phone(tags: Dict[str, Any]) -> Optional[str]:
    for field in ("phone", "contact:phone", "phone:emergency", "contact:mobile"):
        value = tags.get(field)
        if value:
            return str(value)[:64]
    return None


async def nearby_hospitals(
    lat: float,
    lon: float,
    radius_km: float = DEFAULT_HOSPITAL_RADIUS_KM,
) -> Dict[str, Any]:
    """Hospitals and emergency clinics near a point, nearest first."""
    radius = max(0.5, min(radius_km, MAX_HOSPITAL_RADIUS_KM))
    radius_m = int(radius * 1000)

    key = cache_key(
        "geo:hospitals",
        lat=round_coord(lat, 2),
        lon=round_coord(lon, 2),
        r=int(radius),
    )

    async def _fetch() -> List[Dict[str, Any]]:
        payload = await post_json(
            OVERPASS_ENDPOINT,
            provider=OVERPASS_PROVIDER,
            data={"data": _overpass_query(lat, lon, radius_m)},
            timeout=_OVERPASS_TIMEOUT,
            # Overpass is slow and shared; retrying a timeout adds load without
            # improving the odds.
            retries=0,
        )
        if not isinstance(payload, dict):
            raise UpstreamError(OVERPASS_PROVIDER, "unexpected response shape")

        seen_names: set[str] = set()
        hospitals: List[Dict[str, Any]] = []

        for element in payload.get("elements") or []:
            if not isinstance(element, dict):
                continue
            coords = _element_coords(element)
            if coords is None:
                continue
            tags = element.get("tags") or {}
            name = tags.get("name") or tags.get("official_name")
            if not name:
                # An unnamed pin is not actionable for someone trying to get to
                # a hospital, so it is dropped rather than shown as "Hospital".
                continue

            element_lat, element_lon = coords
            distance = haversine_km(lat, lon, element_lat, element_lon)
            if distance > radius:
                continue

            # A hospital mapped as both a way and a relation appears twice.
            dedupe_key = f"{str(name).strip().lower()}:{round(element_lat, 3)}"
            if dedupe_key in seen_names:
                continue
            seen_names.add(dedupe_key)

            hospitals.append(
                {
                    "name": str(name)[:200],
                    "lat": element_lat,
                    "lon": element_lon,
                    "distance_km": round(distance, 2),
                    "phone": _element_phone(tags),
                    "emergency": tags.get("emergency"),
                    "website": (tags.get("website") or tags.get("contact:website")),
                }
            )

        hospitals.sort(key=lambda h: h["distance_km"])
        return hospitals[:MAX_HOSPITAL_RESULTS]

    hospitals = await cache.get_or_set(key, settings.cache_ttl_hospitals, _fetch)

    return {
        "hospitals": hospitals,
        "count": len(hospitals),
        "radius_km": radius,
        "attribution": OSM_ATTRIBUTION,
    }


__all__ = [
    "DEFAULT_HOSPITAL_RADIUS_KM",
    "MAX_HOSPITAL_RADIUS_KM",
    "OSM_ATTRIBUTION",
    "country_code",
    "nearby_hospitals",
    "reverse_geocode",
]
