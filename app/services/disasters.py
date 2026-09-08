"""Disaster and hazard aggregation (USGS, NASA EONET, GDACS, disease.sh).

The important behavioural change: **a failed source is reported, not hidden.**
``fetch_gdacs`` and ``fetch_pandemics`` previously wrapped their whole body in
``except Exception: return []``. An outage at GDACS was therefore
indistinguishable from "there are no active floods anywhere on Earth", and the
app rendered the second interpretation. Every fetcher here raises on failure;
:func:`aggregate` catches per-source and returns ``sources_failed`` alongside
the events so the client can say "1 of 4 feeds unavailable".
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.core.cache import cache, cache_key
from app.core.config import settings
from app.core.http import UpstreamError, get_json
from app.core.logging import get_logger

logger = get_logger(__name__)

USGS_BASE = "https://earthquake.usgs.gov/fdsnws/event/1/query"
EONET_BASE = "https://eonet.gsfc.nasa.gov/api/v3/events"
GDACS_BASE = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
DISEASE_BASE = "https://disease.sh/v3/covid-19/countries"
GDELT_DOC_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"

# ---------------------------------------------------------------------------
# Category metadata
# ---------------------------------------------------------------------------
CATEGORY_META: Dict[str, Dict[str, str]] = {
    "earthquake": {"label": "Earthquake", "icon": "pulse", "color": "#F44336"},
    "flood": {"label": "Flood", "icon": "water", "color": "#2196F3"},
    "wildfire": {"label": "Wildfire", "icon": "flame", "color": "#FF5722"},
    "storm": {"label": "Storm", "icon": "thunderstorm", "color": "#9C27B0"},
    "volcano": {"label": "Volcano", "icon": "triangle", "color": "#E65100"},
    "drought": {"label": "Drought", "icon": "sunny", "color": "#FFB300"},
    "conflict": {"label": "Conflict", "icon": "warning", "color": "#B71C1C"},
    "pandemic": {"label": "Pandemic", "icon": "medical", "color": "#E91E63"},
    "other": {"label": "Other", "icon": "alert-circle", "color": "#8A8A8A"},
}

EONET_CAT_MAP = {
    "wildfires": "wildfire",
    "severeStorms": "storm",
    "volcanoes": "volcano",
    "floods": "flood",
    "drought": "drought",
    "seaLakeIce": "other",
    "snow": "other",
    "landslides": "other",
    "earthquakes": "earthquake",
    "manmade": "conflict",
    "dustHaze": "other",
    "tempExtremes": "other",
    "waterColor": "other",
}

GDACS_CAT_MAP = {
    "EQ": "earthquake",
    "FL": "flood",
    "TC": "storm",  # tropical cyclone
    "VO": "volcano",
    "DR": "drought",
    "WF": "wildfire",
}

_GDACS_SEVERITY = {"Green": "Low", "Orange": "Moderate", "Red": "Severe"}


async def disaster_news(category: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    """Fetch recent disaster reporting without treating news as an alert source."""
    key = cache_key("disaster-news", category=category or "all", limit=limit)

    async def _fetch() -> List[Dict[str, Any]]:
        topic = {
            "earthquake": "earthquake",
            "flood": "flood OR flooding",
            "wildfire": "wildfire OR forest fire",
            "storm": "cyclone OR hurricane OR severe storm",
            "volcano": "volcanic eruption",
        }.get(category or "", "earthquake OR flood OR wildfire OR cyclone OR volcano")
        payload = await get_json(
            GDELT_DOC_BASE,
            provider="GDELT",
            params={"query": f"({topic})", "mode": "artlist", "format": "json", "maxrecords": min(limit, 50), "sort": "datedesc"},
        )
        if not isinstance(payload, dict):
            raise UpstreamError("GDELT", "unexpected response shape")
        articles: List[Dict[str, Any]] = []
        for index, article in enumerate(payload.get("articles") or []):
            url = article.get("url")
            title = article.get("title")
            if not isinstance(url, str) or not url.startswith("https://") or not isinstance(title, str) or not title.strip():
                continue
            image = article.get("socialimage")
            image_url = image if isinstance(image, str) and image.startswith("https://") else None
            stable_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
            articles.append({"id": f"gdelt-{stable_id}", "title": title.strip(), "url": url, "source": article.get("domain"), "published_at": article.get("seendate"), "image_url": image_url, "language": article.get("language")})
        return articles

    return await cache.get_or_set(key, max(settings.cache_ttl_disasters, 900), _fetch)

# Country name -> ISO2, for the free-text country names USGS puts in `place`.
COUNTRY_NAME_TO_ISO2: Dict[str, str] = {
    "united states": "US",
    "usa": "US",
    "u.s.a.": "US",
    "u.s.": "US",
    "united kingdom": "GB",
    "uk": "GB",
    "russia": "RU",
    "russian federation": "RU",
    "china": "CN",
    "japan": "JP",
    "india": "IN",
    "pakistan": "PK",
    "indonesia": "ID",
    "philippines": "PH",
    "vietnam": "VN",
    "thailand": "TH",
    "malaysia": "MY",
    "singapore": "SG",
    "south korea": "KR",
    "korea": "KR",
    "north korea": "KP",
    "taiwan": "TW",
    "hong kong": "HK",
    "nepal": "NP",
    "sri lanka": "LK",
    "bangladesh": "BD",
    "myanmar": "MM",
    "afghanistan": "AF",
    "iran": "IR",
    "iraq": "IQ",
    "israel": "IL",
    "syria": "SY",
    "turkey": "TR",
    "saudi arabia": "SA",
    "uae": "AE",
    "united arab emirates": "AE",
    "kazakhstan": "KZ",
    "uzbekistan": "UZ",
    "kyrgyzstan": "KG",
    "tajikistan": "TJ",
    "turkmenistan": "TM",
    "azerbaijan": "AZ",
    "armenia": "AM",
    "georgia": "GE",
    "germany": "DE",
    "france": "FR",
    "italy": "IT",
    "spain": "ES",
    "portugal": "PT",
    "netherlands": "NL",
    "belgium": "BE",
    "greece": "GR",
    "switzerland": "CH",
    "austria": "AT",
    "sweden": "SE",
    "norway": "NO",
    "finland": "FI",
    "denmark": "DK",
    "poland": "PL",
    "ukraine": "UA",
    "romania": "RO",
    "hungary": "HU",
    "czech republic": "CZ",
    "czechia": "CZ",
    "iceland": "IS",
    "ireland": "IE",
    "canada": "CA",
    "mexico": "MX",
    "brazil": "BR",
    "argentina": "AR",
    "chile": "CL",
    "peru": "PE",
    "colombia": "CO",
    "venezuela": "VE",
    "ecuador": "EC",
    "bolivia": "BO",
    "uruguay": "UY",
    "paraguay": "PY",
    "guatemala": "GT",
    "honduras": "HN",
    "el salvador": "SV",
    "nicaragua": "NI",
    "costa rica": "CR",
    "panama": "PA",
    "cuba": "CU",
    "haiti": "HT",
    "dominican republic": "DO",
    "jamaica": "JM",
    "puerto rico": "PR",
    "australia": "AU",
    "new zealand": "NZ",
    "papua new guinea": "PG",
    "fiji": "FJ",
    "solomon islands": "SB",
    "vanuatu": "VU",
    "tonga": "TO",
    "samoa": "WS",
    "new caledonia": "NC",
    "egypt": "EG",
    "morocco": "MA",
    "algeria": "DZ",
    "tunisia": "TN",
    "libya": "LY",
    "sudan": "SD",
    "ethiopia": "ET",
    "kenya": "KE",
    "tanzania": "TZ",
    "uganda": "UG",
    "rwanda": "RW",
    "burundi": "BI",
    "somalia": "SO",
    "south africa": "ZA",
    "nigeria": "NG",
    "ghana": "GH",
    "senegal": "SN",
    "ivory coast": "CI",
    "cameroon": "CM",
    "democratic republic of the congo": "CD",
    "congo": "CG",
    "zambia": "ZM",
    "zimbabwe": "ZW",
    "mozambique": "MZ",
    "angola": "AO",
    "madagascar": "MG",
}


def _iso2_from_place(place: Optional[str]) -> Optional[str]:
    """Extract a country code from a USGS place string.

    e.g. "134 km E of Bitung, Indonesia" -> "ID".
    """
    if not place:
        return None
    tail = (place.rsplit(",", 1)[-1] if "," in place else place).strip().lower()
    if tail in COUNTRY_NAME_TO_ISO2:
        return COUNTRY_NAME_TO_ISO2[tail]
    # USGS uses two-letter US state codes for domestic quakes.
    if len(tail) == 2 and tail.isalpha():
        return "US"
    return None


def _severity_from_mag(mag: float) -> str:
    if mag >= 7:
        return "Extreme"
    if mag >= 6:
        return "Severe"
    if mag >= 5:
        return "Moderate"
    if mag >= 4:
        return "Minor"
    return "Low"


def _iso_hours_ago(hours: int) -> str:
    moment = datetime.now(UTC) - timedelta(hours=hours)
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    return None


# ---------------------------------------------------------------------------
# Fetchers — each raises UpstreamError rather than returning an empty list
# ---------------------------------------------------------------------------
async def earthquakes(min_mag: float = 4.5, hours: int = 24) -> List[Dict[str, Any]]:
    hours = max(1, min(hours, 720))
    min_mag = max(0.0, min(min_mag, 10.0))
    key = cache_key("dis:usgs", mag=min_mag, hours=hours)

    async def _fetch() -> List[Dict[str, Any]]:
        payload = await get_json(
            USGS_BASE,
            provider="USGS",
            params={
                "format": "geojson",
                "starttime": _iso_hours_ago(hours),
                "minmagnitude": min_mag,
                "orderby": "time",
                "limit": 200,
            },
        )
        if not isinstance(payload, dict):
            raise UpstreamError("USGS", "unexpected response shape")

        events: List[Dict[str, Any]] = []
        for feature in payload.get("features") or []:
            props = feature.get("properties") or {}
            coords = (feature.get("geometry") or {}).get("coordinates") or []
            magnitude = _as_float(props.get("mag"))
            if magnitude is None or len(coords) < 2:
                continue

            place = props.get("place")
            iso2 = _iso2_from_place(place)
            depth = coords[2] if len(coords) > 2 else None
            epoch_ms = props.get("time") or 0

            events.append(
                {
                    "id": feature.get("id"),
                    "category": "earthquake",
                    "title": place or "Earthquake",
                    "description": (
                        f"Magnitude {magnitude}"
                        + (f" — depth {depth} km" if depth is not None else "")
                    ),
                    "magnitude": magnitude,
                    "severity": _severity_from_mag(magnitude),
                    "lat": coords[1],
                    "lon": coords[0],
                    "time": datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).isoformat(),
                    "url": props.get("url"),
                    "country": iso2,
                    "iso2": iso2,
                    "source": "USGS",
                    "tsunami": bool(props.get("tsunami")),
                }
            )
        return events

    return await cache.get_or_set(key, settings.cache_ttl_disasters, _fetch)


async def eonet_events(days: int = 30, limit: int = 100) -> List[Dict[str, Any]]:
    days = max(1, min(days, 365))
    limit = max(1, min(limit, 300))
    key = cache_key("dis:eonet", days=days, limit=limit)

    async def _fetch() -> List[Dict[str, Any]]:
        payload = await get_json(
            EONET_BASE,
            provider="NASA EONET",
            params={"status": "open", "limit": limit, "days": days},
        )
        if not isinstance(payload, dict):
            raise UpstreamError("NASA EONET", "unexpected response shape")

        events: List[Dict[str, Any]] = []
        for event in payload.get("events") or []:
            categories = event.get("categories") or []
            raw_category = (categories[0].get("id") if categories else "other") or "other"
            category = EONET_CAT_MAP.get(raw_category, "other")

            geometry = event.get("geometry") or []
            if not geometry:
                continue
            latest = geometry[-1]
            coords = latest.get("coordinates") or []

            if latest.get("type") == "Point" and len(coords) >= 2:
                lat, lon = coords[1], coords[0]
            elif latest.get("type") == "Polygon" and coords:
                ring = coords[0]
                if not ring:
                    continue
                # Centroid of the ring — good enough to place a map pin.
                lat = sum(point[1] for point in ring) / len(ring)
                lon = sum(point[0] for point in ring) / len(ring)
            else:
                continue

            magnitude = latest.get("magnitudeValue")
            unit = latest.get("magnitudeUnit")
            description = event.get("description") or (
                categories[0].get("title") if categories else "Event"
            )
            if magnitude and unit:
                description = f"{magnitude} {unit} — {description}"

            events.append(
                {
                    "id": f"eonet_{event.get('id')}",
                    "category": category,
                    "title": event.get("title") or "Event",
                    "description": description,
                    "magnitude": magnitude,
                    "severity": "Active",
                    "lat": lat,
                    "lon": lon,
                    "time": latest.get("date"),
                    "url": event.get("link"),
                    "country": None,
                    "source": "NASA EONET",
                }
            )
        return events

    return await cache.get_or_set(key, settings.cache_ttl_disasters, _fetch)


async def gdacs_events(days: int = 10) -> List[Dict[str, Any]]:
    days = max(1, min(days, 90))
    key = cache_key("dis:gdacs", days=days)

    async def _fetch() -> List[Dict[str, Any]]:
        now = datetime.now(UTC)
        payload = await get_json(
            GDACS_BASE,
            provider="GDACS",
            params={
                "eventlist": "EQ,TC,FL,VO,DR,WF",
                "fromDate": (now - timedelta(days=days)).strftime("%Y-%m-%d"),
                "toDate": now.strftime("%Y-%m-%d"),
                "alertlevel": "Green;Orange;Red",
            },
        )
        if not isinstance(payload, dict):
            raise UpstreamError("GDACS", "unexpected response shape")

        events: List[Dict[str, Any]] = []
        for feature in payload.get("features") or []:
            props = feature.get("properties") or {}
            coords = (feature.get("geometry") or {}).get("coordinates") or []
            if len(coords) < 2:
                continue

            category = GDACS_CAT_MAP.get(props.get("eventtype"), "other")
            alert = (props.get("alertlevel") or "Green").capitalize()

            events.append(
                {
                    "id": f"gdacs_{props.get('eventtype')}_{props.get('eventid')}",
                    "category": category,
                    "title": (
                        props.get("name") or props.get("eventname") or f"{category.title()} event"
                    ),
                    "description": props.get("description") or props.get("htmldescription") or "",
                    "magnitude": (props.get("severitydata") or {}).get("severity"),
                    "severity": _GDACS_SEVERITY.get(alert, alert),
                    "alert_level": alert,
                    "lat": coords[1],
                    "lon": coords[0],
                    "time": props.get("fromdate"),
                    "url": (props.get("url") or {}).get("report"),
                    "country": props.get("country"),
                    "iso3": props.get("iso3"),
                    "source": "GDACS",
                }
            )
        return events

    return await cache.get_or_set(key, settings.cache_ttl_disasters, _fetch)


async def pandemic_hotspots(min_active: int = 10_000) -> List[Dict[str, Any]]:
    key = cache_key("dis:pandemic", min_active=min_active)

    async def _fetch() -> List[Dict[str, Any]]:
        payload = await get_json(DISEASE_BASE, provider="disease.sh")
        if not isinstance(payload, list):
            raise UpstreamError("disease.sh", "unexpected response shape")

        events: List[Dict[str, Any]] = []
        for country in payload:
            if not isinstance(country, dict):
                continue
            active = country.get("active") or 0
            if not isinstance(active, (int, float)) or active < min_active:
                continue

            info = country.get("countryInfo") or {}
            if active > 500_000:
                severity = "Extreme"
            elif active > 100_000:
                severity = "Severe"
            elif active > 50_000:
                severity = "Moderate"
            else:
                severity = "Minor"

            events.append(
                {
                    "id": f"pandemic_{info.get('iso2')}",
                    "category": "pandemic",
                    "title": f"Elevated case load: {country.get('country')}",
                    "description": (
                        f"Active cases: {int(active):,} | "
                        f"New today: {int(country.get('todayCases') or 0):,}"
                    ),
                    "magnitude": active,
                    "severity": severity,
                    "lat": info.get("lat"),
                    "lon": info.get("long"),
                    "time": datetime.now(UTC).isoformat(),
                    "url": "https://disease.sh",
                    "country": country.get("country"),
                    "iso2": info.get("iso2"),
                    "source": "disease.sh",
                }
            )
        return events

    # Longer TTL: this dataset updates daily at best.
    return await cache.get_or_set(key, max(settings.cache_ttl_disasters, 3600), _fetch)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def _dedupe(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for event in events:
        event_id = event.get("id")
        if not event_id or event_id in seen:
            continue
        seen.add(str(event_id))
        out.append(event)
    return out


def _sort_newest_first(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Times arrive in mixed formats and some are None, so sort on a string key
    # with a stable floor rather than parsing every value.
    return sorted(events, key=lambda e: e.get("time") or "", reverse=True)


async def aggregate(
    *,
    hours: int = 48,
    min_mag: float = 4.5,
    categories: Optional[str] = None,
    include_pandemics: bool = True,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Fetch every source concurrently.

    Returns ``(events, failed_source_names)``. A caller that ignores the second
    element is reintroducing the bug this signature exists to prevent.
    """
    days = max(1, hours // 24 + 1)

    labelled: List[Tuple[str, Any]] = [
        ("USGS", earthquakes(min_mag=min_mag, hours=hours)),
        ("NASA EONET", eonet_events(days=days, limit=100)),
        ("GDACS", gdacs_events(days=days)),
    ]
    if include_pandemics:
        labelled.append(("disease.sh", pandemic_hotspots()))

    results = await asyncio.gather(*(coro for _, coro in labelled), return_exceptions=True)

    combined: List[Dict[str, Any]] = []
    failed: List[str] = []

    for (name, _), result in zip(labelled, results):
        if isinstance(result, list):
            combined.extend(result)
        else:
            failed.append(name)
            logger.warning(
                "Disaster source unavailable",
                extra={"provider": name, "error": type(result).__name__},
            )

    combined = _dedupe(combined)

    if categories:
        wanted = {c.strip().lower() for c in categories.split(",") if c.strip()}
        unknown = wanted - set(CATEGORY_META)
        if unknown:
            logger.info("Ignoring unknown categories", extra={"unknown": sorted(unknown)})
        combined = [e for e in combined if e.get("category") in wanted]

    return _sort_newest_first(combined), failed


__all__ = [
    "CATEGORY_META",
    "COUNTRY_NAME_TO_ISO2",
    "aggregate",
    "earthquakes",
    "eonet_events",
    "gdacs_events",
    "pandemic_hotspots",
]
