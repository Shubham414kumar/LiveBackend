"""Matching saved locations against the merged disaster feed.

One implementation, two callers. ``GET /api/favorites/alerts`` answers "what is
happening near my saved locations right now" when the app is opened; the push
dispatcher in :mod:`app.services.alert_dispatch` answers the same question on a
schedule so the user does not have to open the app. Those two must never
disagree — a notification for an event the screen does not list, or a screen
listing an event no notification was ever sent for, is worse than either
behaviour alone, because it destroys the user's trust in both.

So the matching lives here and neither caller reimplements it.

Two decisions in this module are deliberate and load-bearing:

**Nothing here mutates an event.** The dicts arrive from
:func:`app.services.disasters.aggregate`, which serves them out of the shared
cache. With the in-memory backend the returned dicts *are* the cached objects,
so writing ``event["distance_km"] = ...`` would stamp one favourite's distance
onto the feed every other favourite and every other device then reads. Distance
travels beside the event in :class:`EventMatch` instead.

**An unrecognised severity passes the threshold filter.** Suppressing an event
because its severity string is not in our table would be the same class of bug
as rendering a failed provider as "all clear": the system would go quiet for a
reason the user cannot see. Filtering is therefore fail-open, and only strings
we actually understand can hold an alert back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.geoutils import haversine_km

# Cap on how far ahead an alert radius can reach, so one favourite cannot match
# every event on the planet. Clamped rather than rejected: a stored row from an
# older client with a larger radius must still work.
MAX_ALERT_RADIUS_KM = 2000.0
DEFAULT_ALERT_RADIUS_KM = 100.0

# The pull endpoint renders a summary card, so its list is truncated while the
# count stays true.
MAX_ALERTS_PER_FAVORITE = 20

# Severity vocabulary across the four sources, lowest first:
#
#   USGS        Low, Minor, Moderate, Severe, Extreme   (derived from magnitude)
#   GDACS       Low, Moderate, Severe                   (Green/Orange/Red)
#   disease.sh  Minor, Moderate, Severe, Extreme        (derived from case load)
#   NASA EONET  Active                                  (no severity at all)
#
# "Active" is mapped to Moderate rather than left unranked. EONET only tracks
# events large enough to be observed from orbit, so Moderate is a defensible
# floor, and a fixed value means a user's threshold behaves predictably instead
# of depending on which provider happened to report the event.
SEVERITY_ORDER: Tuple[str, ...] = ("low", "minor", "moderate", "severe", "extreme")

_SEVERITY_RANK: Dict[str, int] = {
    "low": 1,
    "minor": 2,
    "moderate": 3,
    "active": 3,
    "severe": 4,
    "extreme": 5,
}

# Returned for a severity string we do not recognise. Negative so it can never
# be confused with a real rank, and handled explicitly by :func:`passes_severity`.
UNRANKED = -1


def severity_rank(severity: Any) -> int:
    """Rank a severity string, or :data:`UNRANKED` if we do not know it."""
    if not isinstance(severity, str):
        return UNRANKED
    return _SEVERITY_RANK.get(severity.strip().lower(), UNRANKED)


def passes_severity(event: Dict[str, Any], minimum: Optional[str]) -> bool:
    """Is this event at least as severe as ``minimum``?

    ``None`` or an unrecognised ``minimum`` disables the filter. An event whose
    own severity is unrecognised passes — see the module docstring: going quiet
    for an invisible reason is the failure mode this app exists to avoid.
    """
    threshold = severity_rank(minimum)
    if threshold == UNRANKED:
        return True

    rank = severity_rank(event.get("severity"))
    if rank == UNRANKED:
        return True

    return rank >= threshold


def event_coords(event: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """Extract ``(lat, lon)`` from an event, or ``None`` if it has no usable pair.

    Some events legitimately have no coordinates — a disease.sh row for a country
    the dataset has no centroid for, for instance. Those belong in the global
    feed but cannot be distance-matched, and ``haversine_km(None, ...)`` would be
    a 500 on the screen that opens at launch.
    """
    lat, lon = event.get("lat"), event.get("lon")
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    if not (-90.0 <= float(lat) <= 90.0) or not (-180.0 <= float(lon) <= 180.0):
        return None
    return float(lat), float(lon)


def favorite_coords(favorite: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """Extract ``(lat, lon)`` from a favourite row, or ``None`` if unusable.

    Defence against a row that predates the NOT NULL constraint in
    ``0001_initial_schema.sql``.
    """
    try:
        lat = float(favorite["lat"])
        lon = float(favorite["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None
    return lat, lon


def alert_radius_km(favorite: Dict[str, Any]) -> float:
    """This favourite's alert radius, clamped to :data:`MAX_ALERT_RADIUS_KM`."""
    try:
        radius = float(favorite.get("alert_radius_km") or DEFAULT_ALERT_RADIUS_KM)
    except (TypeError, ValueError):
        radius = DEFAULT_ALERT_RADIUS_KM
    if radius <= 0:
        radius = DEFAULT_ALERT_RADIUS_KM
    return min(radius, MAX_ALERT_RADIUS_KM)


@dataclass(frozen=True)
class EventMatch:
    """One event that fell inside one favourite's radius.

    ``event`` is the feed's own dict, referenced rather than copied — see the
    module docstring. Treat it as read-only: it is shared with the cache and with
    every other favourite matched in the same pass. ``distance_km`` is
    per-favourite and therefore cannot live on it.
    """

    event: Dict[str, Any]
    distance_km: float

    @property
    def event_id(self) -> str:
        return str(self.event.get("id") or "")

    @property
    def severity(self) -> str:
        value = self.event.get("severity")
        return value if isinstance(value, str) else "Unknown"

    @property
    def title(self) -> str:
        value = self.event.get("title")
        return value if isinstance(value, str) and value.strip() else "Hazard alert"


@dataclass(frozen=True)
class FavoriteMatches:
    """Everything currently active near one saved location."""

    favorite: Dict[str, Any]
    matches: List[EventMatch]

    @property
    def total(self) -> int:
        """The true number of matches, before any truncation for display."""
        return len(self.matches)

    @property
    def events(self) -> List[Dict[str, Any]]:
        return [match.event for match in self.matches]

    def top(self, limit: int = MAX_ALERTS_PER_FAVORITE) -> List[Dict[str, Any]]:
        """The first ``limit`` matched events, in the order they were matched."""
        return self.events[:limit]


def matches_for_point(
    lat: float,
    lon: float,
    radius_km: float,
    events: Sequence[Dict[str, Any]],
    *,
    min_severity: Optional[str] = None,
) -> List[EventMatch]:
    """Events within ``radius_km`` of a point, in the order they were given.

    Input order is preserved rather than sorted by distance: ``aggregate``
    already returns the feed newest-first, and the pull endpoint truncates to
    twenty, so re-sorting here would silently change which twenty a client sees.
    Callers that want a different order sort the result themselves —
    :func:`most_significant` is the one the dispatcher uses.
    """
    found: List[EventMatch] = []
    for event in events:
        coords = event_coords(event)
        if coords is None:
            continue
        if not passes_severity(event, min_severity):
            continue
        distance = haversine_km(lat, lon, coords[0], coords[1])
        if distance <= radius_km:
            found.append(EventMatch(event=event, distance_km=round(distance, 2)))
    return found


def match_favorites(
    favorites: Sequence[Dict[str, Any]],
    events: Sequence[Dict[str, Any]],
    *,
    min_severity: Optional[str] = None,
) -> List[FavoriteMatches]:
    """Match every favourite against one already-fetched feed.

    ``favorites`` may hold either raw database rows or the wire-shaped dicts the
    favourites route builds; only ``lat``, ``lon`` and ``alert_radius_km`` are
    read, and whatever was passed in is echoed back on
    :attr:`FavoriteMatches.favorite` unchanged.

    A favourite without usable coordinates is dropped from the result rather
    than returned with zero matches. Reporting "nothing near Home" for a row we
    could not place would be a false all-clear.
    """
    results: List[FavoriteMatches] = []
    for favorite in favorites:
        coords = favorite_coords(favorite)
        if coords is None:
            continue
        results.append(
            FavoriteMatches(
                favorite=favorite,
                matches=matches_for_point(
                    coords[0],
                    coords[1],
                    alert_radius_km(favorite),
                    events,
                    min_severity=min_severity,
                ),
            )
        )
    return results


def most_significant(matches: Sequence[EventMatch]) -> Optional[EventMatch]:
    """The one match that should headline a notification.

    Most severe first, nearest as the tie-break. An unranked severity sorts
    below every known one *for this purpose only*: it still qualifies for an
    alert (see :func:`passes_severity`), it just does not get to outrank a
    confirmed Extreme when only one event can be named in the title.
    """
    if not matches:
        return None
    return max(
        matches,
        key=lambda match: (severity_rank(match.event.get("severity")), -match.distance_km),
    )


__all__ = [
    "DEFAULT_ALERT_RADIUS_KM",
    "MAX_ALERTS_PER_FAVORITE",
    "MAX_ALERT_RADIUS_KM",
    "SEVERITY_ORDER",
    "UNRANKED",
    "EventMatch",
    "FavoriteMatches",
    "alert_radius_km",
    "event_coords",
    "favorite_coords",
    "match_favorites",
    "matches_for_point",
    "most_significant",
    "passes_severity",
    "severity_rank",
]
