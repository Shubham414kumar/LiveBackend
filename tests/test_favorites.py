"""Saved locations and per-location alerts.

This is where the data-isolation defect lived, so this is where the library of
tests that would have caught it belongs. Cross-device isolation itself is
asserted in ``test_security.py``, which drives every device-scoped route through
the same table; the focus here is on what a *correctly scoped* set of favourites
does: idempotent creation across two unique indexes, the 50-row cap, the alert
match against the merged disaster feed, and the changed delete key.

The alert tests seed all four sources ``disasters.aggregate`` fans out to. That is
not defensive padding: ``aggregate`` calls USGS, NASA EONET, GDACS *and*
disease.sh (``include_pandemics`` defaults to true), and the ``upstream`` fixture
fails the test at teardown if any outbound request went unstubbed. Stubbing three
of four would fail every test in this section for a reason unrelated to what it
is checking.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Sequence

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app.api.routes.favorites import MAX_ALERT_RADIUS_KM
from app.db.repositories import MAX_FAVORITES_PER_DEVICE
from tests.conftest import assert_problem, favorite_row, iso_ago
from tests.fakes import FakeSupabaseClient, UpstreamRouter

DELHI: Dict[str, float] = {"lat": 28.6139, "lon": 77.2090}
MUMBAI: Dict[str, float] = {"lat": 19.0760, "lon": 72.8777}

# Roughly 1,150 km apart, which is what makes the radius assertions below
# meaningful: outside the default 100 km, inside the 2,000 km cap.
DELHI_TO_MUMBAI_KM = 1150

# Each source's host, as a regex. Registered one per source per test so that no
# route shadows another — `UpstreamRouter` matches in registration order and the
# first pattern to `search` the URL wins.
USGS = r"earthquake\.usgs\.gov"
EONET = r"eonet\.gsfc\.nasa\.gov"
GDACS = r"gdacs\.org"
DISEASE = r"disease\.sh"


# ---------------------------------------------------------------------------
# Upstream payload builders
#
# Shaped to match what the parsers in `app/services/disasters.py` actually
# accept. The subtle one is USGS: a feature whose `properties.mag` is missing or
# None is skipped outright, so a test that omits it seeds an event that silently
# never arrives.
# ---------------------------------------------------------------------------


def _epoch_ms(minutes_ago: float = 30.0) -> int:
    moment = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return int(moment.timestamp() * 1000)


def quake(
    event_id: str,
    *,
    lat: float,
    lon: float,
    mag: float = 4.8,
    place: str = "40 km NE of New Delhi, India",
    minutes_ago: float = 30.0,
) -> Dict[str, Any]:
    """One USGS GeoJSON feature. Note the ``[lon, lat, depth]`` coordinate order."""
    return {
        "id": event_id,
        "geometry": {"type": "Point", "coordinates": [lon, lat, 10.0]},
        "properties": {
            "mag": mag,
            "place": place,
            "time": _epoch_ms(minutes_ago),
            "url": f"https://earthquake.usgs.gov/earthquakes/eventpage/{event_id}",
            "tsunami": 0,
        },
    }


def seed_feed(
    upstream: UpstreamRouter,
    *,
    quakes: Sequence[Dict[str, Any]] = (),
    eonet: Sequence[Dict[str, Any]] = (),
    gdacs: Sequence[Dict[str, Any]] = (),
    pandemics: Sequence[Dict[str, Any]] = (),
) -> None:
    """Stub the whole merged disaster feed in one call.

    Every source is registered exactly once, here, with its contents passed in —
    rather than a broad empty route that a later narrower registration is
    expected to override. It would not: the first matching pattern wins, so the
    later registration would be dead and the injected events would never appear.

    ``pandemics`` is a JSON *list* because ``pandemic_hotspots`` raises
    ``UpstreamError`` on anything else.
    """
    upstream.json(USGS, {"features": list(quakes)})
    upstream.json(EONET, {"events": list(eonet)})
    upstream.json(GDACS, {"features": list(gdacs)})
    upstream.json(DISEASE, list(pandemics))


def alerts_for(response: Response) -> Dict[str, Any]:
    """The single favourite's alert block, for the common one-favourite case."""
    body = response.json()
    assert len(body["favorites"]) == 1, body
    return dict(body["favorites"][0])


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_an_empty_list_is_empty_for_this_device(
    client: TestClient, db: FakeSupabaseClient, device_headers: Dict[str, str]
) -> None:
    response = client.get("/api/favorites", headers=device_headers)

    assert response.status_code == 200
    assert response.json() == {
        "favorites": [],
        "count": 0,
        "limit": MAX_FAVORITES_PER_DEVICE,
    }


def test_saved_locations_are_ordered_most_recent_first(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    device_headers: Dict[str, str],
) -> None:
    """The client renders this list without re-sorting, so the order is contract."""
    db.seed(
        "favorites",
        favorite_row(device_id, name="Oldest", lat=28.0, lon=77.0, created_at=iso_ago(72)),
        favorite_row(device_id, name="Newest", lat=28.1, lon=77.1, created_at=iso_ago(1)),
        favorite_row(device_id, name="Middle", lat=28.2, lon=77.2, created_at=iso_ago(24)),
    )

    response = client.get("/api/favorites", headers=device_headers)

    assert response.status_code == 200
    body = response.json()
    assert [f["name"] for f in body["favorites"]] == ["Newest", "Middle", "Oldest"]
    assert body["count"] == 3


def test_the_device_id_is_never_echoed_back(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    device_headers: Dict[str, str],
) -> None:
    """The device id is the closest thing this app has to a user identifier.

    It is accepted in a header and never serialised in a response, so a client
    that logs or forwards its own API payloads cannot leak it onwards.
    """
    db.seed("favorites", favorite_row(device_id, name="Home"))

    response = client.get("/api/favorites", headers=device_headers)

    assert response.status_code == 200
    assert device_id not in response.text
    assert "device_id" not in response.json()["favorites"][0]


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------


def test_a_location_can_be_saved(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    device_headers: Dict[str, str],
) -> None:
    response = client.post("/api/favorites", json={"name": "Home", **DELHI}, headers=device_headers)

    assert response.status_code == 201, response.text
    favorite = response.json()
    assert favorite["name"] == "Home"
    assert favorite["lat"] == DELHI["lat"]
    assert favorite["lon"] == DELHI["lon"]
    assert favorite["alert_radius_km"] == 100.0
    assert favorite["station_uid"] is None
    assert favorite["id"]
    # Written scoped to the calling device, not inserted bare.
    stored = db.find("favorites", id=favorite["id"])
    assert stored is not None
    assert stored["device_id"] == device_id


def test_a_station_uid_is_optional_and_kept_when_given(
    client: TestClient, db: FakeSupabaseClient, device_headers: Dict[str, str]
) -> None:
    """A favourite created from a WAQI station keeps the station id, so the app can
    show that station's live reading without a reverse lookup."""
    response = client.post(
        "/api/favorites",
        json={"name": "Office", **DELHI, "station_uid": "A0A1B2"},
        headers=device_headers,
    )

    assert response.status_code == 201, response.text
    assert response.json()["station_uid"] == "A0A1B2"


def test_saving_the_same_coordinates_twice_is_idempotent(
    client: TestClient, db: FakeSupabaseClient, device_headers: Dict[str, str]
) -> None:
    """The second insert violates ``(device_id, lat, lon)``.

    The repository catches the duplicate-key error and returns the existing row,
    so a client whose retry succeeded after a dropped response sees one stable
    favourite rather than an error or a second copy.
    """
    first = client.post("/api/favorites", json={"name": "Home", **DELHI}, headers=device_headers)
    second = client.post("/api/favorites", json={"name": "Home", **DELHI}, headers=device_headers)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]
    assert db.count("favorites") == 1


def test_saving_the_same_station_at_new_coordinates_returns_the_original(
    client: TestClient, db: FakeSupabaseClient, device_headers: Dict[str, str]
) -> None:
    """Two unique indexes, and a duplicate-key error does not say which one fired.

    Here it is ``(device_id, station_uid)``, not the coordinate index. The lookup
    tries the station id first for exactly this case — searching only by
    coordinates would find nothing and the caller would report "could not be
    saved" for a location that is already saved.
    """
    first = client.post(
        "/api/favorites",
        json={"name": "Station", **DELHI, "station_uid": "A0A1B2"},
        headers=device_headers,
    )
    second = client.post(
        "/api/favorites",
        json={"name": "Station moved", **MUMBAI, "station_uid": "A0A1B2"},
        headers=device_headers,
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["lat"] == DELHI["lat"]
    assert db.count("favorites") == 1


def test_two_custom_locations_without_a_station_do_not_collide(
    client: TestClient, db: FakeSupabaseClient, device_headers: Dict[str, str]
) -> None:
    """NULLs are distinct in a unique index, as in Postgres.

    Without that, saving a second custom location would be reported as "already
    saved" because both rows have ``station_uid IS NULL``.
    """
    first = client.post("/api/favorites", json={"name": "Home", **DELHI}, headers=device_headers)
    second = client.post("/api/favorites", json={"name": "Work", **MUMBAI}, headers=device_headers)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] != second.json()["id"]
    assert db.count("favorites") == 2


def test_an_oversized_alert_radius_is_capped(
    client: TestClient, db: FakeSupabaseClient, device_headers: Dict[str, str]
) -> None:
    """The schema accepts up to 20,000 km, which reaches every event on Earth.

    The route clamps to 2,000 km on the way in, so the stored value can never
    turn one favourite into a global feed.
    """
    response = client.post(
        "/api/favorites",
        json={"name": "Home", **DELHI, "alert_radius_km": 20000},
        headers=device_headers,
    )

    assert response.status_code == 201, response.text
    assert response.json()["alert_radius_km"] == MAX_ALERT_RADIUS_KM
    stored = db.find("favorites", name="Home")
    assert stored is not None
    assert stored["alert_radius_km"] == MAX_ALERT_RADIUS_KM


def test_the_name_is_sanitised(
    client: TestClient, db: FakeSupabaseClient, device_headers: Dict[str, str]
) -> None:
    """Control characters are stripped before storage, not at render time.

    A name is echoed into logs and into the mobile UI; a raw escape sequence in
    either is somebody else's problem to defend against.
    """
    response = client.post(
        "/api/favorites",
        json={"name": "  Home\x00\x1b[31m  ", **DELHI},
        headers=device_headers,
    )

    assert response.status_code == 201, response.text
    assert response.json()["name"] == "Home[31m"


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"lat": 28.6, "lon": 77.2}, "no name"),
        ({"name": "   ", "lat": 28.6, "lon": 77.2}, "name is whitespace only"),
        ({"name": "Home", "lat": 91, "lon": 77.2}, "latitude out of range"),
        ({"name": "Home", "lat": 28.6, "lon": 181}, "longitude out of range"),
        ({"name": "Home", "lat": 28.6}, "no longitude"),
        ({"name": "Home", "lat": 28.6, "lon": 77.2, "alert_radius_km": 0}, "zero radius"),
        ({"name": "Home", "lat": 28.6, "lon": 77.2, "alert_radius_km": -1}, "negative radius"),
        ({"name": "Home", "lat": 28.6, "lon": 77.2, "nmae": "typo"}, "unknown field"),
        ({"name": "Home", "lat": "north", "lon": 77.2}, "latitude is not a number"),
    ],
)
def test_invalid_payloads_are_rejected(
    client: TestClient,
    db: FakeSupabaseClient,
    device_headers: Dict[str, str],
    payload: Dict[str, Any],
    why: str,
) -> None:
    response = client.post("/api/favorites", json=payload, headers=device_headers)

    assert_problem(response, 422, code="validation_error")
    assert db.count("favorites") == 0, f"a row was written despite {why}"


def test_a_device_can_save_up_to_the_cap_then_no_more(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    device_headers: Dict[str, str],
) -> None:
    """Counted before the insert, so the table never exceeds the cap for a device."""
    for index in range(MAX_FAVORITES_PER_DEVICE):
        db.seed(
            "favorites",
            favorite_row(device_id, name=f"Place {index}", lat=20.0 + index, lon=70.0),
        )

    response = client.post(
        "/api/favorites", json={"name": "One too many", **DELHI}, headers=device_headers
    )

    body = assert_problem(response, 429, code="rate_limited")
    assert body["limit"] == MAX_FAVORITES_PER_DEVICE
    # A quota, not a cooldown: waiting changes nothing, removing a favourite does.
    assert body["retry_after"] == 0
    assert response.headers["Retry-After"] == "0"
    assert str(MAX_FAVORITES_PER_DEVICE) in body["detail"]
    assert db.count("favorites") == MAX_FAVORITES_PER_DEVICE


def test_the_cap_is_per_device(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    other_device_headers: Dict[str, str],
) -> None:
    """One device filling its quota must not block another.

    A cap implemented as a bare ``count(*)`` on the table would.
    """
    for index in range(MAX_FAVORITES_PER_DEVICE):
        db.seed(
            "favorites",
            favorite_row(device_id, name=f"Theirs {index}", lat=20.0 + index, lon=70.0),
        )

    response = client.post(
        "/api/favorites", json={"name": "Mine", **DELHI}, headers=other_device_headers
    )

    assert response.status_code == 201, response.text


def test_two_devices_can_save_the_same_location(
    client: TestClient,
    db: FakeSupabaseClient,
    device_headers: Dict[str, str],
    other_device_headers: Dict[str, str],
) -> None:
    """Both unique indexes lead with ``device_id``, so two users each saving their
    own "Home" at the same coordinates are both right."""
    mine = client.post("/api/favorites", json={"name": "Home", **DELHI}, headers=device_headers)
    theirs = client.post(
        "/api/favorites", json={"name": "Home", **DELHI}, headers=other_device_headers
    )

    assert mine.status_code == 201, mine.text
    assert theirs.status_code == 201, theirs.text
    assert mine.json()["id"] != theirs.json()["id"]
    assert db.count("favorites") == 2


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def test_no_favourites_means_no_upstream_calls(
    client: TestClient,
    db: FakeSupabaseClient,
    device_headers: Dict[str, str],
    upstream: UpstreamRouter,
) -> None:
    """The commonest case — a user who has saved nothing yet — must not cost four
    round trips to third-party disaster APIs on every app open."""
    response = client.get("/api/favorites/alerts", headers=device_headers)

    assert response.status_code == 200
    assert response.json() == {"favorites": [], "partial": False, "sources_failed": []}
    assert upstream.requests == [], "the feed was fetched with nothing to match it against"


def test_the_feed_is_read_scoped_to_the_calling_device(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    other_device_id: str,
    device_headers: Dict[str, str],
    upstream: UpstreamRouter,
) -> None:
    seed_feed(upstream)
    db.seed("favorites", favorite_row(other_device_id, name="Theirs", **DELHI))
    db.seed("favorites", favorite_row(device_id, name="Mine", **MUMBAI))

    response = client.get("/api/favorites/alerts", headers=device_headers)

    assert response.status_code == 200
    result = alerts_for(response)
    assert result["favorite"]["name"] == "Mine"
    select = db.queries_for("favorites", "select")[-1]
    assert select.filtered_on("device_id")
    assert select.value_for("device_id") == device_id


def test_an_empty_disaster_feed_means_no_alerts(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    device_headers: Dict[str, str],
    upstream: UpstreamRouter,
) -> None:
    seed_feed(upstream)
    db.seed("favorites", favorite_row(device_id, name="Home", **DELHI))

    response = client.get("/api/favorites/alerts", headers=device_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["partial"] is False
    assert body["sources_failed"] == []
    result = alerts_for(response)
    assert result["alert_count"] == 0
    assert result["alerts"] == []


def test_an_event_inside_the_radius_is_matched(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    device_headers: Dict[str, str],
    upstream: UpstreamRouter,
) -> None:
    # ~13 km from the saved location, well inside the default 100 km radius.
    seed_feed(upstream, quakes=[quake("near-delhi", lat=28.70, lon=77.30)])
    db.seed("favorites", favorite_row(device_id, name="Home", **DELHI))

    response = client.get("/api/favorites/alerts", headers=device_headers)

    assert response.status_code == 200
    result = alerts_for(response)
    assert result["alert_count"] == 1
    alert = result["alerts"][0]
    assert alert["id"] == "near-delhi"
    assert alert["source"] == "USGS"
    assert alert["category"] == "earthquake"


def test_an_event_outside_the_radius_is_not_matched(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    device_headers: Dict[str, str],
    upstream: UpstreamRouter,
) -> None:
    seed_feed(upstream, quakes=[quake("near-mumbai", lat=MUMBAI["lat"], lon=MUMBAI["lon"])])
    db.seed("favorites", favorite_row(device_id, name="Home", **DELHI))

    response = client.get("/api/favorites/alerts", headers=device_headers)

    assert alerts_for(response)["alert_count"] == 0


def test_a_wider_radius_matches_the_same_event(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    device_headers: Dict[str, str],
    upstream: UpstreamRouter,
) -> None:
    """The pair with the test above: the same event, the same location, a bigger
    radius. Proves the stored radius is what decides, not a constant."""
    seed_feed(upstream, quakes=[quake("near-mumbai", lat=MUMBAI["lat"], lon=MUMBAI["lon"])])
    db.seed(
        "favorites",
        favorite_row(device_id, name="Home", alert_radius_km=DELHI_TO_MUMBAI_KM + 100, **DELHI),
    )

    response = client.get("/api/favorites/alerts", headers=device_headers)

    assert alerts_for(response)["alert_count"] == 1


def test_each_favourite_is_matched_independently(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    device_headers: Dict[str, str],
    upstream: UpstreamRouter,
) -> None:
    seed_feed(upstream, quakes=[quake("near-delhi", lat=28.70, lon=77.30)])
    db.seed("favorites", favorite_row(device_id, name="Delhi", created_at=iso_ago(1), **DELHI))
    db.seed("favorites", favorite_row(device_id, name="Mumbai", created_at=iso_ago(2), **MUMBAI))

    response = client.get("/api/favorites/alerts", headers=device_headers)

    assert response.status_code == 200
    by_name = {f["favorite"]["name"]: f for f in response.json()["favorites"]}
    assert by_name["Delhi"]["alert_count"] == 1
    assert by_name["Mumbai"]["alert_count"] == 0


def test_the_feed_is_fetched_once_for_every_favourite(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    device_headers: Dict[str, str],
    upstream: UpstreamRouter,
) -> None:
    """One aggregate fetch, matched in memory — not one round trip per favourite.

    At 50 favourites and four sources, the alternative is 200 upstream requests
    for a single screen, which is how a free-tier API key gets banned.
    """
    seed_feed(upstream, quakes=[quake("near-delhi", lat=28.70, lon=77.30)])
    for index in range(5):
        db.seed(
            "favorites",
            favorite_row(device_id, name=f"Place {index}", lat=28.6 + index, lon=77.2),
        )

    response = client.get("/api/favorites/alerts", headers=device_headers)

    assert response.status_code == 200
    assert len(response.json()["favorites"]) == 5
    for source in (USGS, EONET, GDACS, DISEASE):
        assert upstream.call_count(source) == 1, source


def test_alerts_are_capped_at_twenty_newest_per_favourite(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    device_headers: Dict[str, str],
    upstream: UpstreamRouter,
) -> None:
    """A wide radius in an active season can match hundreds of events.

    The client renders a summary card, so the list is truncated — but the count is
    the true total, and the events kept are the newest, because that is the
    ordering the aggregate already guarantees.
    """
    quakes = [quake(f"quake-{index}", lat=28.6, lon=77.2, minutes_ago=index) for index in range(25)]
    seed_feed(upstream, quakes=quakes)
    db.seed(
        "favorites",
        favorite_row(device_id, name="Home", alert_radius_km=MAX_ALERT_RADIUS_KM, **DELHI),
    )

    response = client.get("/api/favorites/alerts", headers=device_headers)

    result = alerts_for(response)
    assert result["alert_count"] == 25
    assert len(result["alerts"]) == 20
    assert [a["id"] for a in result["alerts"]] == [f"quake-{i}" for i in range(20)]


def test_events_from_every_source_are_matched(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    device_headers: Dict[str, str],
    upstream: UpstreamRouter,
) -> None:
    """A favourite is matched against the merged feed, not just earthquakes."""
    seed_feed(
        upstream,
        quakes=[quake("quake-1", lat=28.70, lon=77.30)],
        eonet=[
            {
                "id": "EONET_1",
                "title": "Flooding in northern India",
                "link": "https://eonet.gsfc.nasa.gov/api/v3/events/EONET_1",
                "categories": [{"id": "floods", "title": "Floods"}],
                "geometry": [
                    {
                        "type": "Point",
                        "coordinates": [77.25, 28.65],
                        "date": "2026-08-24T00:00:00Z",
                    }
                ],
            }
        ],
        gdacs=[
            {
                "geometry": {"type": "Point", "coordinates": [77.15, 28.55]},
                "properties": {
                    "eventtype": "FL",
                    "eventid": 4242,
                    "alertlevel": "Orange",
                    "name": "Flood in Delhi",
                    "fromdate": "2026-08-23T00:00:00",
                    "url": {"report": "https://www.gdacs.org/report.aspx?eventid=4242"},
                },
            }
        ],
    )
    db.seed("favorites", favorite_row(device_id, name="Home", **DELHI))

    response = client.get("/api/favorites/alerts", headers=device_headers)

    result = alerts_for(response)
    assert result["alert_count"] == 3
    assert {a["source"] for a in result["alerts"]} == {"USGS", "NASA EONET", "GDACS"}
    assert {a["id"] for a in result["alerts"]} == {
        "quake-1",
        "eonet_EONET_1",
        "gdacs_FL_4242",
    }


def test_an_event_with_no_coordinates_is_skipped_not_fatal(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    device_headers: Dict[str, str],
    upstream: UpstreamRouter,
) -> None:
    """disease.sh reports some countries with no ``countryInfo`` coordinates.

    The event is still real and still belongs in the global feed; it just cannot
    be distance-matched. Skipping it must not take the whole response down —
    ``haversine_km(None, ...)`` would be a 500 on a screen that opens on launch.
    """
    seed_feed(
        upstream,
        pandemics=[
            {
                "country": "Nowhere",
                "active": 250_000,
                "todayCases": 400,
                "countryInfo": {"iso2": "NW"},
            }
        ],
        quakes=[quake("near-delhi", lat=28.70, lon=77.30)],
    )
    db.seed("favorites", favorite_row(device_id, name="Home", **DELHI))

    response = client.get("/api/favorites/alerts", headers=device_headers)

    assert response.status_code == 200
    result = alerts_for(response)
    assert result["alert_count"] == 1
    assert result["alerts"][0]["id"] == "near-delhi"


def test_a_favourite_with_no_coordinates_is_skipped(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    device_headers: Dict[str, str],
    upstream: UpstreamRouter,
) -> None:
    """Defence against a row that predates the NOT NULL constraint."""
    seed_feed(upstream, quakes=[quake("near-delhi", lat=28.70, lon=77.30)])
    # Built and then edited rather than passed as `lat=None`, because the helper's
    # signature says `float` and a test should not be the thing that lies about it.
    broken = favorite_row(device_id, name="Broken", created_at=iso_ago(1))
    broken["lat"] = None
    db.seed("favorites", broken)
    db.seed(
        "favorites",
        favorite_row(device_id, name="Fine", created_at=iso_ago(2), **DELHI),
    )

    response = client.get("/api/favorites/alerts", headers=device_headers)

    assert response.status_code == 200
    result = alerts_for(response)
    assert result["favorite"]["name"] == "Fine"


def test_a_failed_source_is_reported_not_hidden(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    device_headers: Dict[str, str],
    upstream: UpstreamRouter,
) -> None:
    """A source that failed and a source that found nothing must not look alike.

    The version this replaces wrapped each fetcher in ``except Exception: return
    []``, so a GDACS outage rendered as "no active floods anywhere on Earth" to
    someone deciding whether to travel.
    """
    upstream.json(USGS, {"features": []})
    upstream.status(EONET, 503)
    upstream.json(GDACS, {"features": []})
    upstream.json(DISEASE, [])
    db.seed("favorites", favorite_row(device_id, name="Home", **DELHI))

    response = client.get("/api/favorites/alerts", headers=device_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["partial"] is True
    assert body["sources_failed"] == ["NASA EONET"]
    assert alerts_for(response)["alert_count"] == 0


def test_the_surviving_sources_still_produce_alerts(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    device_headers: Dict[str, str],
    upstream: UpstreamRouter,
) -> None:
    """A partial feed is still served. Failing the whole request because one of
    four providers is down would mean an earthquake nearby goes unreported."""
    upstream.json(USGS, {"features": [quake("near-delhi", lat=28.70, lon=77.30)]})
    upstream.network_error(EONET)
    upstream.status(GDACS, 500)
    upstream.json(DISEASE, [])
    db.seed("favorites", favorite_row(device_id, name="Home", **DELHI))

    response = client.get("/api/favorites/alerts", headers=device_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["partial"] is True
    assert sorted(body["sources_failed"]) == ["GDACS", "NASA EONET"]
    assert alerts_for(response)["alert_count"] == 1


def test_alerts_require_a_device_id(client: TestClient, db: FakeSupabaseClient) -> None:
    response = client.get("/api/favorites/alerts")

    body = assert_problem(response, 422, code="validation_error")
    assert "X-Device-Id" in body["detail"]


# ---------------------------------------------------------------------------
# Deleting
# ---------------------------------------------------------------------------


def test_a_favourite_can_be_removed_by_id(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    device_headers: Dict[str, str],
) -> None:
    favorite = db.seed("favorites", favorite_row(device_id, name="Home"))[0]

    response = client.delete(f"/api/favorites/{favorite['id']}", headers=device_headers)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "deleted"
    assert db.count("favorites") == 0


def test_only_the_named_favourite_is_removed(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    device_headers: Dict[str, str],
) -> None:
    keep = db.seed("favorites", favorite_row(device_id, name="Keep", lat=28.0, lon=77.0))[0]
    drop = db.seed("favorites", favorite_row(device_id, name="Drop", lat=19.0, lon=72.0))[0]

    response = client.delete(f"/api/favorites/{drop['id']}", headers=device_headers)

    assert response.status_code == 200, response.text
    remaining: List[Dict[str, Any]] = db.rows("favorites")
    assert [row["id"] for row in remaining] == [keep["id"]]


def test_the_delete_key_is_the_favourite_id_not_the_station_uid(
    client: TestClient,
    db: FakeSupabaseClient,
    device_id: str,
    device_headers: Dict[str, str],
) -> None:
    """The old endpoint deleted by WAQI *station* uid.

    A station id is shared between everyone who saved that station and is
    trivially guessable, so the old route could remove another user's row.
    Deleting by the caller's own favourite id — a server-generated UUID, filtered
    by device — is the fix, and the old key must no longer work.
    """
    db.seed("favorites", favorite_row(device_id, name="A", station_uid="A0A1B2", lat=28.0))
    saved = db.seed("favorites", favorite_row(device_id, name="B", station_uid="C3D4E5", lat=19.0))[
        0
    ]

    gone = client.delete("/api/favorites/C3D4E5", headers=device_headers)

    assert_problem(gone, 404, code="not_found")
    assert db.count("favorites") == 2

    ok = client.delete(f"/api/favorites/{saved['id']}", headers=device_headers)

    assert ok.status_code == 200, ok.text
    assert db.count("favorites") == 1


def test_removing_a_missing_favourite_is_a_404(
    client: TestClient, db: FakeSupabaseClient, device_headers: Dict[str, str]
) -> None:
    response = client.delete(f"/api/favorites/{uuid.uuid4()}", headers=device_headers)

    body = assert_problem(response, 404, code="not_found")
    # Phrased as "none of yours", which is also all a caller is entitled to know:
    # whether someone *else* has that id is not their business.
    assert "yours" in body["detail"]


def test_an_overlong_id_is_rejected_by_the_path_validator(
    client: TestClient, db: FakeSupabaseClient, device_headers: Dict[str, str]
) -> None:
    """Bounded before it reaches the database, so a 10 KB path segment is not a
    query for the database to plan."""
    response = client.delete(f"/api/favorites/{'x' * 65}", headers=device_headers)

    assert_problem(response, 422, code="validation_error")
    assert db.queries_for("favorites", "delete") == []


# ---------------------------------------------------------------------------
# Degraded database
#
# Supabase being unreachable is a supported state, not a crash: every upstream
# read still works and only persistence degrades. These routes must say so with
# a 503, because a 500 tells the client to report a bug.
# ---------------------------------------------------------------------------


def test_listing_is_a_503_without_a_database(
    client: TestClient, no_db: None, device_headers: Dict[str, str]
) -> None:
    body = assert_problem(
        client.get("/api/favorites", headers=device_headers),
        503,
        code="service_unavailable",
    )
    assert "database" in body["detail"].lower()


def test_creating_is_a_503_without_a_database(
    client: TestClient, no_db: None, device_headers: Dict[str, str]
) -> None:
    response = client.post("/api/favorites", json={"name": "Home", **DELHI}, headers=device_headers)

    assert_problem(response, 503, code="service_unavailable")


def test_alerts_are_a_503_without_a_database(
    client: TestClient,
    no_db: None,
    device_headers: Dict[str, str],
    upstream: UpstreamRouter,
) -> None:
    response = client.get("/api/favorites/alerts", headers=device_headers)

    assert_problem(response, 503, code="service_unavailable")
    assert upstream.requests == [], "the feed was fetched before the favourites read failed"


def test_deleting_is_a_503_without_a_database(
    client: TestClient, no_db: None, device_headers: Dict[str, str]
) -> None:
    response = client.delete(f"/api/favorites/{uuid.uuid4()}", headers=device_headers)

    assert_problem(response, 503, code="service_unavailable")


def test_a_query_failure_is_a_503_with_no_driver_detail(
    client: TestClient, db: FakeSupabaseClient, device_headers: Dict[str, str]
) -> None:
    """A broken query must not leak the driver's message.

    Postgres error text carries table names, column names and sometimes the
    offending value; none of that belongs in a mobile client's error toast.
    """
    db.failure = RuntimeError("connection to server at 10.0.0.5 failed: FATAL password")

    body = assert_problem(
        client.get("/api/favorites", headers=device_headers),
        503,
        code="service_unavailable",
    )
    assert "10.0.0.5" not in body["detail"]
    assert "password" not in body["detail"].lower()
