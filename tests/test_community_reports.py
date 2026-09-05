"""Community hazard reports.

The behaviours that matter here are the ones that hold the feed together without
a login: post-moderation visibility, one vote per device, a per-device daily
quota, and distance filtering that happens in the database rather than in Python
over the whole table.

Cross-device isolation for this router lives in ``test_security.py``, next to the
same guarantee for favourites, because it is one rule and it should fail in one
place.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from app.db.repositories import MAX_REPORTS_PER_DEVICE_PER_DAY
from tests.conftest import assert_problem, iso_ago, report_row

DELHI = {"lat": 28.6139, "lon": 77.2090}


# ---------------------------------------------------------------------------
# Category metadata
# ---------------------------------------------------------------------------


def test_categories_are_served_by_the_api(client: TestClient) -> None:
    """Labels, icons and colours are server-side on purpose.

    Two clients (Expo and the admin dashboard) render the same categories. Held
    in each client they drift, and adding a category needs an app-store release.
    """
    response = client.get("/api/reports/categories")

    assert response.status_code == 200
    payload = response.json()
    assert payload["severities"] == ["Low", "Moderate", "Severe", "Extreme"]
    assert set(payload["categories"]) == {
        "flood",
        "fire",
        "accident",
        "road_block",
        "pollution",
        "water_logging",
        "disease_cluster",
        "other",
    }
    for meta in payload["categories"].values():
        assert meta["label"] and meta["icon"] and meta["color"].startswith("#")


def test_the_category_list_is_the_one_the_writer_enforces(client: TestClient) -> None:
    """A category the metadata advertises must be accepted by ``POST /reports``.

    The list is defined in the router and the ``Literal`` in the schema is what
    validates a write. If they diverge, a client renders a chip the API rejects.
    """
    advertised = set(client.get("/api/reports/categories").json()["categories"])
    schema = client.get("/api/openapi.json").json()
    accepted = set(
        schema["components"]["schemas"]["ReportCreate"]["properties"]["category"]["enum"]
    )
    assert advertised == accepted


# ---------------------------------------------------------------------------
# Reading the feed
# ---------------------------------------------------------------------------


def test_the_feed_requires_a_coordinate(client: TestClient) -> None:
    """ "Reports near me" has no meaning without a "me"."""
    body = assert_problem(client.get("/api/reports"), 422, code="validation_error")
    assert {err["field"] for err in body["errors"]} == {"lat", "lon"}


def test_the_feed_is_readable_without_a_device_id(client: TestClient, db: Any) -> None:
    """Reads are public. Someone who has just installed the app, or who has
    denied the app its keychain, still needs to see what is happening nearby."""
    db.seed("community_reports", report_row(uuid.uuid4().hex, title="Water on Ring Road"))

    response = client.get("/api/reports", params=DELHI)

    assert response.status_code == 200
    payload = response.json()
    assert [r["title"] for r in payload["reports"]] == ["Water on Ring Road"]
    # Without a device id there is no "mine" and no vote history to report.
    assert payload["reports"][0]["is_mine"] is False
    assert payload["reports"][0]["has_voted"] is False
    # And no vote lookup was issued, since there is nothing to look up.
    assert db.queries_for("report_votes") == []


def test_reports_are_ordered_nearest_first_with_a_distance(client: TestClient, db: Any) -> None:
    """The client renders distance directly, so it is computed server-side from
    one authoritative coordinate rather than in two clients from two."""
    device = uuid.uuid4().hex
    db.seed("community_reports", report_row(device, title="Far", lat=28.7139, lon=77.2090))
    db.seed("community_reports", report_row(device, title="Near", **DELHI))

    reports = client.get("/api/reports", params=DELHI).json()["reports"]

    assert [r["title"] for r in reports] == ["Near", "Far"]
    assert reports[0]["distance_km"] == 0.0
    # 0.1 degree of latitude is ~11 km anywhere on Earth.
    assert 10.5 < reports[1]["distance_km"] < 11.5


def test_reports_outside_the_radius_are_excluded(client: TestClient, db: Any) -> None:
    device = uuid.uuid4().hex
    db.seed("community_reports", report_row(device, title="Delhi", **DELHI))
    db.seed("community_reports", report_row(device, title="Mumbai", lat=19.0760, lon=72.8777))

    reports = client.get("/api/reports", params={**DELHI, "radius_km": 50}).json()["reports"]

    assert [r["title"] for r in reports] == ["Delhi"]


def test_the_radius_filter_runs_in_the_database_first(client: TestClient, db: Any) -> None:
    """A bounding box is pushed down, then refined by exact haversine here.

    The previous implementation selected the whole table and filtered in Python,
    which is fine with fifty rows and a full table scan per request at scale.
    """
    db.seed("community_reports", report_row(uuid.uuid4().hex, **DELHI))

    client.get("/api/reports", params={**DELHI, "radius_km": 10})

    select = db.queries_for("community_reports", "select")[-1]
    for column in ("lat", "lon", "created_at", "status"):
        assert select.filtered_on(column), f"{column} was not pushed into the query"


def test_hidden_and_removed_reports_are_not_served(client: TestClient, db: Any) -> None:
    """Post-moderation only works if hiding something actually hides it."""
    device = uuid.uuid4().hex
    db.seed("community_reports", report_row(device, title="Visible", status="visible"))
    db.seed("community_reports", report_row(device, title="Hidden", status="hidden"))
    db.seed("community_reports", report_row(device, title="Removed", status="removed"))

    reports = client.get("/api/reports", params=DELHI).json()["reports"]

    assert [r["title"] for r in reports] == ["Visible"]


def test_stale_reports_age_out_of_the_feed(client: TestClient, db: Any) -> None:
    """A hazard report is a statement about *now*.

    Yesterday's water logging shown as current is worse than showing nothing —
    it teaches users the feed cannot be trusted.
    """
    device = uuid.uuid4().hex
    db.seed("community_reports", report_row(device, title="Fresh", created_at=iso_ago(2)))
    db.seed("community_reports", report_row(device, title="Stale", created_at=iso_ago(100)))

    default_window = client.get("/api/reports", params=DELHI).json()["reports"]
    assert [r["title"] for r in default_window] == ["Fresh"]

    # And the window is caller-controlled, for a "what happened this week" view.
    wider = client.get("/api/reports", params={**DELHI, "hours": 168}).json()["reports"]
    assert {r["title"] for r in wider} == {"Fresh", "Stale"}


def test_the_feed_can_be_filtered_by_category(client: TestClient, db: Any) -> None:
    device = uuid.uuid4().hex
    db.seed("community_reports", report_row(device, title="Water", category="flood"))
    db.seed("community_reports", report_row(device, title="Smoke", category="fire"))

    reports = client.get("/api/reports", params={**DELHI, "category": "fire"}).json()["reports"]

    assert [r["title"] for r in reports] == ["Smoke"]


def test_an_unknown_category_filter_is_an_error_not_an_empty_list(
    client: TestClient,
) -> None:
    """An empty list looks like "no hazards nearby", which is the most dangerous
    thing this API can say incorrectly. A typo'd filter must not produce it."""
    response = client.get("/api/reports", params={**DELHI, "category": "flooding"})

    body = assert_problem(response, 422, code="validation_error")
    assert "/api/reports/categories" in body["detail"]


@pytest.mark.parametrize(
    "params",
    [
        {"lat": 91, "lon": 0},
        {"lat": 0, "lon": 181},
        {"lat": "north", "lon": 0},
        {**DELHI, "radius_km": 0},
        {**DELHI, "radius_km": 201},
        {**DELHI, "hours": 0},
        {**DELHI, "hours": 721},
        {**DELHI, "limit": 0},
        {**DELHI, "limit": 201},
    ],
)
def test_out_of_range_query_parameters_are_rejected(
    client: TestClient, params: Dict[str, Any]
) -> None:
    """Bounds are declared on the route, so they are enforced before a query is
    built and they appear in the OpenAPI schema the clients are generated from."""
    assert_problem(client.get("/api/reports", params=params), 422, code="validation_error")


def test_the_limit_is_applied_after_the_distance_sort(client: TestClient, db: Any) -> None:
    """A generous slice is fetched and trimmed after the exact distance pass, so
    ``limit=1`` returns the *nearest* report rather than an arbitrary one."""
    device = uuid.uuid4().hex
    db.seed("community_reports", report_row(device, title="Far", lat=28.7139, lon=77.2090))
    db.seed("community_reports", report_row(device, title="Near", **DELHI))

    reports = client.get("/api/reports", params={**DELHI, "limit": 1}).json()["reports"]

    assert [r["title"] for r in reports] == ["Near"]


# ---------------------------------------------------------------------------
# Filing a report
# ---------------------------------------------------------------------------


def test_a_report_is_visible_immediately(
    client: TestClient, db: Any, device_id: str, device_headers: Dict[str, str]
) -> None:
    """Post-moderation, asserted.

    Holding a flood report in an approval queue defeats the point of a
    public-safety feed, so a new report is ``visible`` on write and an admin can
    hide it afterwards.
    """
    response = client.post(
        "/api/reports",
        json={
            "category": "flood",
            "title": "Underpass flooded, knee deep",
            "description": "Traffic diverted at the roundabout.",
            "severity": "Severe",
            **DELHI,
        },
        headers=device_headers,
    )

    assert response.status_code == 201, response.text
    created = response.json()
    assert created["status"] == "visible"
    assert created["created_at"]

    stored = db.find("community_reports", id=created["id"])
    assert stored is not None
    assert stored["device_id"] == device_id
    assert stored["severity"] == "Severe"
    assert stored["upvotes"] == 0
    # And it is in the feed on the very next read.
    titles = [r["title"] for r in client.get("/api/reports", params=DELHI).json()["reports"]]
    assert titles == ["Underpass flooded, knee deep"]


def test_severity_defaults_rather_than_failing(
    client: TestClient, db: Any, device_headers: Dict[str, str]
) -> None:
    """Someone reporting a hazard should not have to classify it first."""
    response = client.post(
        "/api/reports",
        json={"category": "other", "title": "Something is wrong", **DELHI},
        headers=device_headers,
    )

    assert response.status_code == 201
    assert db.find("community_reports", id=response.json()["id"])["severity"] == "Moderate"


@pytest.mark.parametrize(
    "payload",
    [
        {"category": "flood", "lat": 28.6, "lon": 77.2},  # no title
        {"title": "No category", "lat": 28.6, "lon": 77.2},
        {"category": "flood", "title": "No coordinates"},
        {"category": "earthquake", "title": "Unknown category", "lat": 28.6, "lon": 77.2},
        {
            "category": "flood",
            "title": "Bad severity",
            "severity": "medium",
            "lat": 28.6,
            "lon": 77.2,
        },
        {"category": "flood", "title": "   ", "lat": 28.6, "lon": 77.2},
        {"category": "flood", "title": "x" * 201, "lat": 28.6, "lon": 77.2},
        {"category": "flood", "title": "Out of range", "lat": 200, "lon": 77.2},
    ],
)
def test_invalid_report_payloads_are_rejected(
    client: TestClient, db: Any, device_headers: Dict[str, str], payload: Dict[str, Any]
) -> None:
    response = client.post("/api/reports", json=payload, headers=device_headers)

    assert_problem(response, 422, code="validation_error")
    assert db.count("community_reports") == 0


def test_severity_is_case_sensitive_on_the_wire(
    client: TestClient, device_headers: Dict[str, str]
) -> None:
    """``Severity`` is capitalised to stay wire-compatible with the shipped mobile
    client. Asserted explicitly so nobody "tidies" it to lowercase and breaks
    every installed app."""
    response = client.post(
        "/api/reports",
        json={"category": "flood", "title": "Casing", "severity": "severe", **DELHI},
        headers=device_headers,
    )
    assert_problem(response, 422, code="validation_error")


def test_an_unknown_field_is_rejected_rather_than_ignored(
    client: TestClient, device_headers: Dict[str, str]
) -> None:
    """A silently-dropped ``lattitude`` is a bug that surfaces days later as a
    map pin in the wrong place."""
    response = client.post(
        "/api/reports",
        json={
            "category": "flood",
            "title": "Typo in the payload",
            "lat": 28.6139,
            "lon": 77.2090,
            "sevrity": "Severe",
        },
        headers=device_headers,
    )

    body = assert_problem(response, 422, code="validation_error")
    assert any("sevrity" in err["field"] for err in body["errors"])


def test_malformed_json_is_a_422_not_a_500(
    client: TestClient, device_headers: Dict[str, str]
) -> None:
    response = client.post(
        "/api/reports",
        content=b'{"category": "flood", "title": ',
        headers={**device_headers, "Content-Type": "application/json"},
    )
    assert_problem(response, 422, code="validation_error")


def test_report_text_is_sanitised_before_storage(
    client: TestClient, db: Any, device_headers: Dict[str, str]
) -> None:
    """Control characters are stripped on the way in.

    The mobile client escapes on render too, but data written once is read by
    every client forever — including the admin dashboard and any future export —
    so it is cleaned at the boundary rather than trusted to each reader.
    """
    response = client.post(
        "/api/reports",
        json={
            "category": "pollution",
            "title": "Smoke\x00 near the depot\x1b[31m",
            "description": "Visible from\x07 the flyover",
            **DELHI,
        },
        headers=device_headers,
    )

    assert response.status_code == 201
    stored = db.find("community_reports", id=response.json()["id"])
    assert stored["title"] == "Smoke near the depot[31m"
    assert stored["description"] == "Visible from the flyover"


def test_a_device_has_a_daily_report_quota(
    client: TestClient, db: Any, device_id: str, device_headers: Dict[str, str]
) -> None:
    """A daily quota, distinct from the 60-second rate limit.

    The failure being prevented is one device drip-feeding junk onto the map over
    several hours, which a per-minute window does nothing about.
    """
    for index in range(MAX_REPORTS_PER_DEVICE_PER_DAY):
        db.seed("community_reports", report_row(device_id, title=f"Earlier {index}"))

    response = client.post(
        "/api/reports",
        json={"category": "flood", "title": "One too many", **DELHI},
        headers=device_headers,
    )

    body = assert_problem(response, 429, code="rate_limited")
    assert body["limit"] == MAX_REPORTS_PER_DEVICE_PER_DAY
    assert body["retry_after"] == 3600
    assert db.count("community_reports") == MAX_REPORTS_PER_DEVICE_PER_DAY


def test_the_quota_only_counts_the_last_day(
    client: TestClient, db: Any, device_id: str, device_headers: Dict[str, str]
) -> None:
    """Otherwise a heavy reporting day would silence a device permanently."""
    for index in range(MAX_REPORTS_PER_DEVICE_PER_DAY):
        db.seed(
            "community_reports",
            report_row(device_id, title=f"Yesterday {index}", created_at=iso_ago(30)),
        )

    response = client.post(
        "/api/reports",
        json={"category": "flood", "title": "A new day", **DELHI},
        headers=device_headers,
    )
    assert response.status_code == 201, response.text


def test_the_quota_is_per_device(
    client: TestClient,
    db: Any,
    device_id: str,
    other_device_headers: Dict[str, str],
) -> None:
    """One device exhausting its quota must not silence a whole neighbourhood."""
    for index in range(MAX_REPORTS_PER_DEVICE_PER_DAY):
        db.seed("community_reports", report_row(device_id, title=f"Theirs {index}"))

    response = client.post(
        "/api/reports",
        json={"category": "fire", "title": "Mine", **DELHI},
        headers=other_device_headers,
    )
    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# Voting
# ---------------------------------------------------------------------------


def test_a_vote_is_recorded_and_the_count_updated(
    client: TestClient, db: Any, device_headers: Dict[str, str]
) -> None:
    report = db.seed("community_reports", report_row(uuid.uuid4().hex))[0]

    response = client.post(f"/api/reports/{report['id']}/vote", headers=device_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is True
    assert body["upvotes"] == 1
    # Denormalised onto the report so the feed does not need a join per row.
    assert db.find("community_reports", id=report["id"])["upvotes"] == 1


def test_a_device_can_only_vote_once(
    client: TestClient, db: Any, device_headers: Dict[str, str]
) -> None:
    """Enforced by a unique index on ``(report_id, device_id)``, not by a check
    in the handler — two concurrent requests would both pass a check."""
    report = db.seed("community_reports", report_row(uuid.uuid4().hex))[0]

    first = client.post(f"/api/reports/{report['id']}/vote", headers=device_headers)
    second = client.post(f"/api/reports/{report['id']}/vote", headers=device_headers)

    assert first.json()["accepted"] is True
    assert second.status_code == 200
    assert second.json()["accepted"] is False
    assert second.json()["upvotes"] == 1
    assert "already confirmed" in second.json()["detail"]
    assert db.count("report_votes") == 1


def test_a_duplicate_vote_is_not_an_error(
    client: TestClient, db: Any, device_headers: Dict[str, str]
) -> None:
    """200 with ``accepted: false`` rather than 409.

    The user's intent — "this report is real" — is already satisfied. A red error
    toast for a tap that changed nothing is worse than a no-op.
    """
    report = db.seed("community_reports", report_row(uuid.uuid4().hex))[0]
    client.post(f"/api/reports/{report['id']}/vote", headers=device_headers)

    second = client.post(f"/api/reports/{report['id']}/vote", headers=device_headers)
    assert second.status_code == 200
    assert second.headers["content-type"].startswith("application/json")


def test_votes_from_different_devices_accumulate(
    client: TestClient,
    db: Any,
    device_headers: Dict[str, str],
    other_device_headers: Dict[str, str],
) -> None:
    report = db.seed("community_reports", report_row(uuid.uuid4().hex))[0]

    client.post(f"/api/reports/{report['id']}/vote", headers=device_headers)
    second = client.post(f"/api/reports/{report['id']}/vote", headers=other_device_headers)

    assert second.json() == {
        "report_id": report["id"],
        "upvotes": 2,
        "accepted": True,
        "detail": "Thanks — your confirmation was recorded.",
    }


def test_the_vote_count_is_recounted_not_incremented(
    client: TestClient, db: Any, device_headers: Dict[str, str]
) -> None:
    """A read-modify-write on ``upvotes`` loses one of two concurrent votes, so
    the column is set from a fresh count of the votes table.

    Asserted by seeding a wrong cached value and watching it be corrected.
    """
    report = db.seed("community_reports", report_row(uuid.uuid4().hex, upvotes=99))[0]

    response = client.post(f"/api/reports/{report['id']}/vote", headers=device_headers)

    assert response.json()["upvotes"] == 1
    assert db.find("community_reports", id=report["id"])["upvotes"] == 1


def test_voting_on_a_missing_report_is_a_404(
    client: TestClient, device_headers: Dict[str, str]
) -> None:
    response = client.post(f"/api/reports/{uuid.uuid4()}/vote", headers=device_headers)
    assert_problem(response, 404, code="not_found")


def test_the_feed_reports_what_this_device_has_voted_on(
    client: TestClient, db: Any, device_headers: Dict[str, str]
) -> None:
    """So the client can render the button disabled instead of letting the user
    tap it and receive a rejection."""
    voted = db.seed("community_reports", report_row(uuid.uuid4().hex, title="Voted"))[0]
    db.seed("community_reports", report_row(uuid.uuid4().hex, title="Not voted"))
    client.post(f"/api/reports/{voted['id']}/vote", headers=device_headers)

    reports = client.get("/api/reports", params=DELHI, headers=device_headers).json()["reports"]

    by_title = {r["title"]: r for r in reports}
    assert by_title["Voted"]["has_voted"] is True
    assert by_title["Not voted"]["has_voted"] is False


def test_vote_history_is_per_device(
    client: TestClient,
    db: Any,
    device_headers: Dict[str, str],
    other_device_headers: Dict[str, str],
) -> None:
    report = db.seed("community_reports", report_row(uuid.uuid4().hex))[0]
    client.post(f"/api/reports/{report['id']}/vote", headers=device_headers)

    theirs = client.get("/api/reports", params=DELHI, headers=other_device_headers)

    assert theirs.json()["reports"][0]["has_voted"] is False
    # Their upvote total is still the shared, true one.
    assert theirs.json()["reports"][0]["upvotes"] == 1


# ---------------------------------------------------------------------------
# Withdrawing a report
# ---------------------------------------------------------------------------


def test_a_device_can_withdraw_its_own_report(
    client: TestClient, db: Any, device_id: str, device_headers: Dict[str, str]
) -> None:
    report = db.seed("community_reports", report_row(device_id))[0]

    response = client.delete(f"/api/reports/{report['id']}", headers=device_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    assert db.count("community_reports") == 0


def test_withdrawing_a_missing_report_is_a_404(
    client: TestClient, device_headers: Dict[str, str]
) -> None:
    response = client.delete(f"/api/reports/{uuid.uuid4()}", headers=device_headers)
    assert_problem(response, 404, code="not_found")


# ---------------------------------------------------------------------------
# Degraded database
# ---------------------------------------------------------------------------


def test_the_feed_is_a_503_without_a_database(client: TestClient, no_db: None) -> None:
    """Not an empty list. "No reports nearby" and "we cannot tell you" are
    different answers, and only one of them should be rendered as reassurance."""
    body = assert_problem(client.get("/api/reports", params=DELHI), 503, code="service_unavailable")
    assert "database" in body["detail"].lower()


def test_filing_a_report_is_a_503_without_a_database(
    client: TestClient, no_db: None, device_headers: Dict[str, str]
) -> None:
    response = client.post(
        "/api/reports",
        json={"category": "flood", "title": "Nowhere to store this", **DELHI},
        headers=device_headers,
    )
    assert_problem(response, 503, code="service_unavailable")


def test_a_database_error_while_writing_is_a_503(
    client: TestClient, db: Any, device_headers: Dict[str, str]
) -> None:
    db.failure = RuntimeError("connection reset by peer")

    response = client.post(
        "/api/reports",
        json={"category": "flood", "title": "Lost write", **DELHI},
        headers=device_headers,
    )
    assert_problem(response, 503, code="service_unavailable")


def test_categories_do_not_need_a_database(client: TestClient, no_db: None) -> None:
    """Static metadata must keep serving during an outage, so a client that
    fetches it at startup can still render its report form."""
    assert client.get("/api/reports/categories").status_code == 200
