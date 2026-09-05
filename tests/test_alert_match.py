"""Matching saved locations against the feed — the rules both callers share.

``GET /api/favorites/alerts`` and the push dispatcher ask the same question, one
when the app opens and one on a timer. They must never disagree, so they share
this module, and these are its tests. Three properties are load-bearing.

**Filtering is fail-open.** An event whose severity string we do not recognise is
delivered, not suppressed. Going quiet for a reason the user cannot see is the
same class of bug as rendering a failed provider as "all clear" — and it is the
one this app exists not to have.

**Nothing here mutates an event.** ``aggregate`` serves events out of the shared
cache, and with the in-memory backend the dicts *are* the cached objects. Writing
a per-favourite distance onto one would stamp it on the feed every other favourite
and every other device then reads. There is a test below that holds this line.

**A favourite that cannot be placed is dropped, never reported clear.** "Nothing
near Home" for a row with no usable coordinates is a false all-clear.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List

import pytest

from app.core.geoutils import EARTH_RADIUS_KM
from app.services.alert_match import (
    DEFAULT_ALERT_RADIUS_KM,
    MAX_ALERT_RADIUS_KM,
    MAX_ALERTS_PER_FAVORITE,
    SEVERITY_ORDER,
    UNRANKED,
    EventMatch,
    alert_radius_km,
    event_coords,
    favorite_coords,
    match_favorites,
    matches_for_point,
    most_significant,
    passes_severity,
    severity_rank,
)

#: One degree of latitude, from the identity the haversine collapses to when the
#: longitude delta is zero: ``2R * asin(sin(dphi/2)) == R * dphi``. Used instead of
#: a hardcoded 111 so the tests move with ``EARTH_RADIUS_KM`` rather than silently
#: disagreeing with it.
KM_PER_DEGREE = EARTH_RADIUS_KM * math.radians(1.0)

DELHI = (28.6139, 77.2090)


def event(
    event_id: str = "usgs:quake-1",
    *,
    lat: Any = 0.0,
    lon: Any = 0.0,
    severity: Any = "Moderate",
    title: Any = "M 5.2 - 6 km WNW of Delhi",
    **extra: Any,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "id": event_id,
        "lat": lat,
        "lon": lon,
        "severity": severity,
        "title": title,
        "category": "earthquake",
    }
    row.update(extra)
    return row


def favorite(
    *, lat: Any = DELHI[0], lon: Any = DELHI[1], name: str = "Home", **extra: Any
) -> Dict[str, Any]:
    row: Dict[str, Any] = {"name": name, "lat": lat, "lon": lon}
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------


def test_the_vocabulary_is_ordered_lowest_first() -> None:
    """``SEVERITY_ORDER`` is the public list an API client picks a threshold from,
    so its order is a contract, not a presentation detail."""
    ranks = [severity_rank(name) for name in SEVERITY_ORDER]

    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)
    assert all(rank > UNRANKED for rank in ranks)


def test_severity_is_matched_case_and_whitespace_insensitively() -> None:
    """Four providers, four spellings; none of them agreed to a house style."""
    assert severity_rank("  SEVERE ") == severity_rank("severe") == severity_rank("Severe")


def test_eonet_active_is_ranked_as_moderate() -> None:
    """EONET reports no severity at all, and only tracks events big enough to see
    from orbit. A fixed floor means a user's threshold behaves predictably instead
    of depending on which provider happened to report the event."""
    assert severity_rank("Active") == severity_rank("Moderate")


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "catastrophic", 5, True, ["severe"]],
    ids=["none", "empty", "blank", "unknown-word", "int", "bool", "list"],
)
def test_anything_we_do_not_recognise_is_unranked(value: Any) -> None:
    """Non-strings included. ``UNRANKED`` is negative so it can never be mistaken
    for a real rank by arithmetic that forgot to check."""
    assert severity_rank(value) == UNRANKED


@pytest.mark.parametrize("minimum", [None, "", "banana", 3], ids=["none", "empty", "word", "int"])
def test_a_threshold_we_cannot_read_disables_the_filter(minimum: Any) -> None:
    """Not "filter nothing through" — filter *nothing out*. A preference row we
    cannot interpret must not be able to silence a device."""
    assert passes_severity(event(severity="Low"), minimum) is True


def test_an_event_of_unknown_severity_is_never_held_back() -> None:
    """The fail-open rule, at its most uncomfortable: an unreadable severity gets
    through a threshold of Extreme. The alternative is a system that goes quiet
    because a provider renamed a field, which is indistinguishable from safety."""
    assert passes_severity(event(severity="unheard-of"), "Extreme") is True
    assert passes_severity(event(severity=None), "Extreme") is True


def test_the_threshold_is_inclusive_and_otherwise_strict() -> None:
    assert passes_severity(event(severity="Moderate"), "Moderate") is True
    assert passes_severity(event(severity="Severe"), "Moderate") is True
    assert passes_severity(event(severity="Minor"), "Moderate") is False


# ---------------------------------------------------------------------------
# Coordinates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lat,lon",
    [
        (None, 77.0),
        (28.6, None),
        ("28.6", "77.2"),
        (91.0, 77.0),
        (-91.0, 77.0),
        (28.6, 181.0),
        (28.6, -181.0),
        (True, 77.0),
    ],
    ids=["no-lat", "no-lon", "strings", "lat-high", "lat-low", "lon-high", "lon-low", "bool"],
)
def test_an_event_without_a_usable_pair_cannot_be_distance_matched(lat: Any, lon: Any) -> None:
    """``haversine_km(None, ...)`` is a 500 on the screen that opens at launch.

    Strings are rejected rather than coerced: an event is upstream data, and a
    provider sending ``"28.6"`` has changed its contract in a way worth noticing.
    A *favourite* is our own row, so that one is coerced — see below.
    """
    assert event_coords(event(lat=lat, lon=lon)) is None
    assert matches_for_point(28.6139, 77.2090, 500.0, [event(lat=lat, lon=lon)]) == []


def test_a_favourite_row_is_coerced_because_it_is_our_own_data() -> None:
    """Rows predating the NOT NULL constraint in ``0001_initial_schema.sql``, and
    numerics that postgrest hands back as strings, both have to keep working."""
    assert favorite_coords(favorite(lat="28.6139", lon="77.2090")) == DELHI


@pytest.mark.parametrize(
    "row",
    [{}, {"lat": 28.6}, {"lat": None, "lon": 77.2}, {"lat": "north", "lon": "east"}],
    ids=["empty", "no-lon", "null-lat", "unparseable"],
)
def test_a_favourite_we_cannot_place_is_dropped_not_reported_clear(row: Dict[str, Any]) -> None:
    """Returning it with zero matches would render as "nothing near Home"."""
    assert favorite_coords(row) is None
    assert match_favorites([row], [event(lat=28.65, lon=77.25)]) == []


# ---------------------------------------------------------------------------
# Radius
# ---------------------------------------------------------------------------


def test_the_radius_boundary_is_inclusive() -> None:
    """``<=``, not ``<``. Asserted at zero distance, where the haversine is exactly
    0.0 for identical inputs and the comparison needs no tolerance."""
    here = event(lat=DELHI[0], lon=DELHI[1])

    matches = matches_for_point(DELHI[0], DELHI[1], 0.0, [here])

    assert len(matches) == 1
    assert matches[0].distance_km == 0.0


def test_a_radius_decides_what_is_inside_it() -> None:
    one_degree_north = event(lat=1.0, lon=0.0)

    inside = matches_for_point(0.0, 0.0, KM_PER_DEGREE + 1.0, [one_degree_north])
    outside = matches_for_point(0.0, 0.0, KM_PER_DEGREE - 1.0, [one_degree_north])

    assert [match.distance_km for match in inside] == [pytest.approx(KM_PER_DEGREE, abs=0.01)]
    assert outside == []


@pytest.mark.parametrize(
    "value",
    [None, 0, 0.0, -5.0, "not a number", ""],
    ids=["none", "zero-int", "zero-float", "negative", "unparseable", "empty"],
)
def test_a_radius_we_cannot_use_falls_back_to_the_default(value: Any) -> None:
    """Zero and negative are folded into the default deliberately: a radius of zero
    would silently match nothing, which reads on screen as "all clear"."""
    assert alert_radius_km(favorite(alert_radius_km=value)) == DEFAULT_ALERT_RADIUS_KM


def test_a_missing_radius_falls_back_to_the_default() -> None:
    assert alert_radius_km(favorite()) == DEFAULT_ALERT_RADIUS_KM


def test_an_oversized_radius_is_clamped_rather_than_rejected() -> None:
    """A stored row from an older client with a 5000 km radius must still work — it
    just cannot reach halfway round the planet."""
    assert alert_radius_km(favorite(alert_radius_km=99_000)) == MAX_ALERT_RADIUS_KM
    assert alert_radius_km(favorite(alert_radius_km="250")) == 250.0


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_feed_order_is_preserved_rather_than_sorted_by_distance() -> None:
    """``aggregate`` returns the feed newest-first and the pull endpoint truncates
    to twenty, so re-sorting here would silently change which twenty a client sees.
    Callers that want severity order ask ``most_significant`` for it."""
    far = event("far", lat=1.0, lon=0.0)
    near = event("near", lat=0.01, lon=0.0)

    matches = matches_for_point(0.0, 0.0, 500.0, [far, near])

    assert [match.event_id for match in matches] == ["far", "near"]


def test_distance_travels_beside_the_event_and_never_on_it() -> None:
    """The event dicts are the cache's own objects. Stamping one favourite's
    distance onto one would publish it to every other favourite, every other
    device, and every reader of the feed until the entry expired."""
    shared = event(lat=0.01, lon=0.0)
    before = dict(shared)

    matches = matches_for_point(0.0, 0.0, 500.0, [shared])

    assert matches[0].event is shared, "the event is referenced, not copied"
    assert shared == before, "and referenced read-only"
    assert "distance_km" not in shared


def test_the_severity_filter_applies_inside_the_radius() -> None:
    minor = event("minor", lat=0.01, lon=0.0, severity="Minor")
    severe = event("severe", lat=0.01, lon=0.0, severity="Severe")

    matches = matches_for_point(0.0, 0.0, 500.0, [minor, severe], min_severity="Moderate")

    assert [match.event_id for match in matches] == ["severe"]


def test_every_favourite_is_matched_against_the_same_feed_and_echoed_back() -> None:
    """The route hands in wire-shaped dicts and the dispatcher hands in raw rows;
    whatever came in is what comes back on ``FavoriteMatches.favorite``."""
    home = favorite(name="Home", lat=0.0, lon=0.0)
    away = favorite(name="Away", lat=40.0, lon=0.0)
    nearby = event(lat=0.01, lon=0.0)

    results = match_favorites([home, away], [nearby])

    assert [result.favorite["name"] for result in results] == ["Home", "Away"]
    assert results[0].favorite is home
    assert results[0].total == 1
    assert results[1].total == 0


def test_the_summary_truncates_the_list_but_not_the_count() -> None:
    """The card shows a handful; the count has to stay true or the screen lies."""
    events: List[Dict[str, Any]] = [
        event(f"usgs:{index}", lat=0.01, lon=0.0) for index in range(MAX_ALERTS_PER_FAVORITE + 5)
    ]

    matched = match_favorites([favorite(lat=0.0, lon=0.0)], events)[0]

    assert matched.total == MAX_ALERTS_PER_FAVORITE + 5
    assert len(matched.top()) == MAX_ALERTS_PER_FAVORITE
    assert [row["id"] for row in matched.top(3)] == ["usgs:0", "usgs:1", "usgs:2"]
    assert matched.events[0] is events[0]


# ---------------------------------------------------------------------------
# What a match reads as
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title", [None, "", "   ", 42], ids=["none", "empty", "blank", "int"])
def test_an_event_with_no_usable_title_gets_a_neutral_one(title: Any) -> None:
    """This string becomes a notification title on a lock screen. An empty one is
    an empty notification."""
    assert EventMatch(event=event(title=title), distance_km=1.0).title == "Hazard alert"


def test_a_missing_severity_reads_as_unknown_and_a_missing_id_as_blank() -> None:
    """Blank rather than ``None`` for the id: the dispatcher tests it for truthiness
    to decide whether the event can be claimed at all."""
    match = EventMatch(event={}, distance_km=1.0)

    assert match.severity == "Unknown"
    assert match.event_id == ""
    assert match.title == "Hazard alert"


def test_an_id_is_stringified_rather_than_trusted_to_be_one() -> None:
    """It becomes half of ``sent_alerts``' primary key, so its type is not optional."""
    assert EventMatch(event={"id": 1234}, distance_km=1.0).event_id == "1234"


# ---------------------------------------------------------------------------
# Headline selection
# ---------------------------------------------------------------------------


def test_the_headline_is_the_most_severe_then_the_nearest() -> None:
    near_moderate = EventMatch(event=event("near-moderate", severity="Moderate"), distance_km=2.0)
    far_severe = EventMatch(event=event("far-severe", severity="Severe"), distance_km=90.0)
    near_severe = EventMatch(event=event("near-severe", severity="Severe"), distance_km=8.0)

    assert most_significant([near_moderate, far_severe, near_severe]).event_id == "near-severe"


def test_an_unranked_severity_does_not_get_to_outrank_a_confirmed_extreme() -> None:
    """It still qualifies for an alert — ``passes_severity`` lets it through. It just
    does not get to be the one event named in the title."""
    unknown = EventMatch(event=event("unknown", severity="???"), distance_km=1.0)
    extreme = EventMatch(event=event("extreme", severity="Extreme"), distance_km=95.0)

    assert most_significant([unknown, extreme]).event_id == "extreme"


def test_nothing_matched_has_no_headline() -> None:
    """``None``, so a caller cannot render a notification for an empty list."""
    assert most_significant([]) is None
