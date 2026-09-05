"""Tests for the push dispatch pass.

Three properties are load-bearing, and nearly every test below defends one.

**Nobody is woken twice for one event.** The ledger's composite primary key is
the guarantee, so the *mechanism* is asserted alongside the outcome: a claim
that merged instead of doing nothing would hand back every row as though this
pass had won it, and the resulting double-send is invisible in the response
shape alone.

**Suppression is not loss.** Quiet hours and the volume cap hold candidates back
*before* the claim, so a later pass can still deliver them. Every suppression
test therefore also asserts that ``sent_alerts`` stayed empty — a candidate that
was claimed and then dropped looks identical at the transport layer, but would
never be delivered by anything, ever.

**No push token reaches a log record or a database column.** Checked against the
flattened structured fields of every record, not only ``caplog.text``.

Only the feed is faked. :func:`app.services.push.send` runs for real against the
``upstream`` mock transport, so chunking, ticket alignment, error classification
and the dead-token prune are exercised rather than assumed — a test that stubbed
``push.send`` would assert the dispatcher's arithmetic back to itself.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.core.config import settings
from app.db.repositories import DELIVERY_FAILED, DELIVERY_PENDING, DELIVERY_SENT
from app.services import alert_dispatch, push
from app.services.alert_match import EventMatch
from tests.conftest import favorite_row, iso_ago
from tests.fakes import FakeDBError, FakeSupabaseClient, UpstreamRouter

#: Matches the Expo send endpoint. Escaped, because ``UpstreamRouter`` searches
#: the full URL with a regex.
EXPO = r"exp\.host/--/api/v2/push/send"

# Delhi, matching ``favorite_row``'s defaults so a seeded favourite and these
# coordinates cannot drift apart.
HOME_LAT, HOME_LON = 28.6139, 77.2090
#: ~5.7 km from HOME — inside every radius used below.
NEAR_LAT, NEAR_LON = 28.65, 77.25
#: ~11 km from HOME, so "from Home" is the correct attribution for a NEAR event.
OFFICE_LAT, OFFICE_LON = 28.70, 77.35
#: ~75 km from HOME: inside the 100 km default, outside a 50 km radius.
MID_LAT, MID_LON = 29.2, 77.6
#: Mumbai, ~1150 km from HOME — outside anything.
FAR_LAT, FAR_LON = 19.0760, 72.8777

ZONE = "Asia/Kolkata"

# Fixed instants for the quiet-hours arithmetic. IST is UTC+5:30, so:
_MIDNIGHT_IST = datetime(2026, 9, 2, 18, 30, tzinfo=UTC)  # 00:00 IST -> hour 0
_MORNING_IST = datetime(2026, 9, 2, 4, 0, tzinfo=UTC)  # 09:30 IST -> hour 9
_EVENING_IST = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)  # 17:30 IST -> hour 17


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def dispatch_on(settings_override: Any) -> None:
    """Turn delivery on. Off is the default, so every send test needs this."""
    settings_override(push_enabled=True)


class Feed:
    """Stands in for ``disasters.aggregate``, the pass's only upstream read.

    The feed is the one thing faked here, and only because coupling these tests
    to four providers' wire formats would mean a GDACS field rename breaking a
    test about quiet hours.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: List[Dict[str, Any]] = []
        self.failed: List[str] = []
        #: The kwargs of every call, so a test can assert the lookback window.
        self.calls: List[Dict[str, Any]] = []

        async def aggregate(**kwargs: Any) -> Any:
            self.calls.append(kwargs)
            return list(self.events), list(self.failed)

        monkeypatch.setattr(alert_dispatch.disasters, "aggregate", aggregate)

    def serve(self, *events: Dict[str, Any], failed: Sequence[str] = ()) -> Feed:
        self.events = list(events)
        self.failed = list(failed)
        return self


@pytest.fixture
def feed(monkeypatch: pytest.MonkeyPatch) -> Feed:
    """An installed, empty feed. Call ``feed.serve(...)`` to fill it."""
    return Feed(monkeypatch)


def event(
    event_id: str = "usgs:quake-1",
    *,
    severity: str = "Moderate",
    lat: float = NEAR_LAT,
    lon: float = NEAR_LON,
    title: str = "M 5.2 - 6 km WNW of Delhi",
    category: str = "earthquake",
    **extra: Any,
) -> Dict[str, Any]:
    """One feed event, in the shape ``disasters.aggregate`` returns."""
    row: Dict[str, Any] = {
        "id": event_id,
        "source": "usgs",
        "category": category,
        "title": title,
        "time": iso_ago(0.5),
        "lat": lat,
        "lon": lon,
        "severity": severity,
        "url": "https://example.test/quake-1",
    }
    row.update(extra)
    return row


def token_row(
    device_id: str,
    *,
    token: Optional[str] = None,
    alerts_enabled: bool = True,
    min_severity: Optional[str] = None,
    quiet_hours_start: Optional[int] = None,
    quiet_hours_end: Optional[int] = None,
    timezone: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """A ``push_tokens`` row, for seeding.

    The token embeds the device id so an assertion can name the recipient
    without the test having to carry two identifiers around.
    """
    row: Dict[str, Any] = {
        "device_id": device_id,
        "token": f"ExponentPushToken[{device_id}]" if token is None else token,
        "platform": "android",
        "lat": HOME_LAT,
        "lon": HOME_LON,
        "alerts_enabled": alerts_enabled,
        "min_severity": min_severity,
        "quiet_hours_start": quiet_hours_start,
        "quiet_hours_end": quiet_hours_end,
        "timezone": timezone,
        "updated_at": iso_ago(1),
    }
    row.update(extra)
    return row


def ledger_row(
    device_id: str,
    event_id: str,
    *,
    status: str = DELIVERY_PENDING,
    attempts: int = 0,
    claimed_hours_ago: float = 2.0,
    last_error: Optional[str] = None,
) -> Dict[str, Any]:
    """A ``sent_alerts`` row, for seeding.

    ``claimed_hours_ago`` defaults to two hours, which is past the 900-second
    staleness threshold, so a seeded row is retryable unless a test says
    otherwise.
    """
    return {
        "device_id": device_id,
        "event_id": event_id,
        "status": status,
        "attempts": attempts,
        "claimed_at": iso_ago(claimed_hours_ago),
        "sent_at": None,
        "last_error": last_error,
    }


def expo_ok(upstream: UpstreamRouter) -> UpstreamRouter:
    """Accept every message, with the ticket count derived from the request.

    Derived rather than fixed so that a change to the chunk boundary shows up as
    a real behavioural difference instead of a ``TicketCountMismatch``.
    """

    def respond(request: httpx.Request) -> httpx.Response:
        count = len(json.loads(request.content))
        tickets = [{"status": "ok", "id": f"ticket-{index}"} for index in range(count)]
        return httpx.Response(200, json={"data": tickets})

    return upstream.add(EXPO, respond)


def expo_error(
    upstream: UpstreamRouter, code: str, *, text: str = "delivery failed"
) -> UpstreamRouter:
    """Fail every ticket with ``code``, at HTTP 200 — the way Expo really does."""

    def respond(request: httpx.Request) -> httpx.Response:
        count = len(json.loads(request.content))
        tickets = [
            {"status": "error", "message": text, "details": {"error": code}} for _ in range(count)
        ]
        return httpx.Response(200, json={"data": tickets})

    return upstream.add(EXPO, respond)


def expo_dead_token(upstream: UpstreamRouter) -> UpstreamRouter:
    """Reject every message as an unregistered recipient.

    The error text quotes each message's own ``to`` value, because that is what
    Expo does — and it is the reason ``_scrub_error`` exists. A test that used a
    token-free message here would prove nothing about the scrubbing.
    """

    def respond(request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)
        tickets = [
            {
                "status": "error",
                "message": f'"{message["to"]}" is not a registered push recipient',
                "details": {"error": push.DEVICE_NOT_REGISTERED},
            }
            for message in messages
        ]
        return httpx.Response(200, json={"data": tickets})

    return upstream.add(EXPO, respond)


def sent_messages(upstream: UpstreamRouter) -> List[Dict[str, Any]]:
    """Every message handed to Expo, flattened across requests, in send order."""
    out: List[Dict[str, Any]] = []
    for request in upstream.requests:
        if "push/send" in str(request.url):
            out.extend(json.loads(request.content))
    return out


def quiet_window_covering_now(zone: str = ZONE) -> Dict[str, Any]:
    """A quiet window that certainly contains this moment.

    Two hours wide on purpose: a one-hour window derived from the current hour
    could stop covering "now" if the clock ticked over between the seeding and
    the assertion, and a suite that fails once an hour is worse than no suite.
    """
    hour = datetime.now(UTC).astimezone(ZoneInfo(zone)).hour
    return {
        "quiet_hours_start": hour,
        "quiet_hours_end": (hour + 2) % 24,
        "timezone": zone,
    }


def quiet_window_excluding_now(zone: str = ZONE) -> Dict[str, Any]:
    """A quiet window that certainly does not contain this moment.

    The companion to the above, and the guard against vacuity: "quiet hours
    suppressed the alert" passes just as well against code that suppresses
    unconditionally, so something has to prove the window is being read.
    """
    hour = datetime.now(UTC).astimezone(ZoneInfo(zone)).hour
    return {
        "quiet_hours_start": (hour + 2) % 24,
        "quiet_hours_end": (hour + 4) % 24,
        "timezone": zone,
    }


def log_surface(caplog: pytest.LogCaptureFixture) -> str:
    """Every captured record's message *and* structured fields, as one string.

    ``caplog.text`` covers only the formatted message. The dispatcher puts its
    counters in ``extra``, which becomes attributes on the record and is where a
    leaked token would actually land, so the token-safety assertion has to
    flatten ``record.__dict__`` too.
    """
    parts: List[str] = [caplog.text]
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.extend(f"{key}={value!r}" for key, value in record.__dict__.items())
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Delivery disabled
# ---------------------------------------------------------------------------


async def test_disabled_delivery_does_not_even_read_the_feed(
    feed: Feed, db: FakeSupabaseClient, device_id: str
) -> None:
    """Off is the default, and it costs nothing — not even a provider request."""
    db.seed("push_tokens", token_row(device_id))
    db.seed("favorites", favorite_row(device_id))
    feed.serve(event())

    stats = await alert_dispatch.run_pass()

    assert stats.aborted == "push_disabled"
    assert feed.calls == []
    assert db.count("sent_alerts") == 0


async def test_the_feed_is_fetched_for_the_configured_lookback(
    dispatch_on: None, settings_override: Any, feed: Feed, db: FakeSupabaseClient
) -> None:
    settings_override(push_lookback_hours=6)

    stats = await alert_dispatch.run_pass()

    assert stats.aborted is None
    assert feed.calls == [{"hours": 6}]


# ---------------------------------------------------------------------------
# The happy path, and what a notification actually says
# ---------------------------------------------------------------------------


async def test_one_matching_event_sends_one_notification(
    dispatch_on: None,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
) -> None:
    db.seed("push_tokens", token_row(device_id))
    db.seed("favorites", favorite_row(device_id))
    feed.serve(event())
    expo_ok(upstream)

    stats = await alert_dispatch.run_pass()

    assert stats.aborted is None
    assert stats.devices == 1
    assert stats.candidates == 1
    assert stats.claimed == 1
    assert stats.sent == 1
    assert stats.failed == 0

    messages = sent_messages(upstream)
    assert len(messages) == 1
    assert messages[0]["to"] == f"ExponentPushToken[{device_id}]"
    assert messages[0]["title"] == "M 5.2 - 6 km WNW of Delhi"
    # How bad, how far, from where — the only three things a lock screen answers.
    assert messages[0]["body"] == "Moderate · 6 km from Home"
    assert messages[0]["data"]["eventId"] == "usgs:quake-1"

    row = db.find("sent_alerts", device_id=device_id, event_id="usgs:quake-1")
    assert row is not None
    assert row["status"] == DELIVERY_SENT
    assert row["attempts"] == 1
    assert row["last_error"] is None
    assert row["sent_at"] is not None


async def test_notifications_are_high_priority_with_a_bounded_ttl(
    dispatch_on: None,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
) -> None:
    """High priority so a phone in Doze still buzzes; a TTL so it cannot arrive stale.

    Both are on the wire rather than left to Expo's defaults, and both matter at
    3 a.m. — the case the whole feature exists for.
    """
    db.seed("push_tokens", token_row(device_id))
    db.seed("favorites", favorite_row(device_id))
    feed.serve(event())
    expo_ok(upstream)

    await alert_dispatch.run_pass()

    message = sent_messages(upstream)[0]
    assert message["priority"] == "high"
    assert message["ttl"] == push.PUSH_TTL_SECONDS
    assert message["sound"] == "default"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_the_same_event_is_never_sent_twice(
    dispatch_on: None,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
) -> None:
    """Two passes over an unchanged feed, one notification. The 3 a.m. requirement."""
    db.seed("push_tokens", token_row(device_id))
    db.seed("favorites", favorite_row(device_id))
    feed.serve(event())
    expo_ok(upstream)

    first = await alert_dispatch.run_pass()
    second = await alert_dispatch.run_pass()

    assert first.sent == 1
    assert second.candidates == 1, "the event is still in the feed and still matches"
    assert second.claimed == 0, "but this pass did not win the claim"
    assert second.sent == 0
    assert len(sent_messages(upstream)) == 1
    assert db.count("sent_alerts") == 1


async def test_the_claim_is_an_on_conflict_do_nothing(
    dispatch_on: None,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
) -> None:
    """Assert the mechanism, not only the outcome.

    An upsert that merged would return every row as though this pass had won it,
    and the double-send that follows is indistinguishable from correct behaviour
    in the response shape. So the conflict resolution itself is pinned here.
    """
    db.seed("push_tokens", token_row(device_id))
    db.seed("favorites", favorite_row(device_id))
    feed.serve(event())
    expo_ok(upstream)

    await alert_dispatch.run_pass()

    claims = [q for q in db.queries_for("sent_alerts", "upsert") if q.ignore_duplicates]
    assert len(claims) == 1, "the claim must be exactly one statement, so it is atomic"
    assert claims[0].on_conflict == "device_id,event_id"


# ---------------------------------------------------------------------------
# Severity thresholds
# ---------------------------------------------------------------------------


async def test_event_below_the_device_threshold_is_not_a_candidate(
    dispatch_on: None, feed: Feed, db: FakeSupabaseClient, upstream: UpstreamRouter, device_id: str
) -> None:
    db.seed("push_tokens", token_row(device_id, min_severity="Severe"))
    db.seed("favorites", favorite_row(device_id))
    feed.serve(event(severity="Moderate"))

    stats = await alert_dispatch.run_pass()

    assert stats.candidates == 0
    assert db.count("sent_alerts") == 0
    assert not upstream.called(EXPO)


async def test_configured_default_applies_when_the_device_chose_no_threshold(
    dispatch_on: None,
    settings_override: Any,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
) -> None:
    """A null ``min_severity`` means the deployment default, not "no filtering"."""
    settings_override(push_default_min_severity="Extreme")
    db.seed("push_tokens", token_row(device_id, min_severity=None))
    db.seed("favorites", favorite_row(device_id))
    feed.serve(event(severity="Severe"))

    stats = await alert_dispatch.run_pass()

    assert stats.candidates == 0
    assert not upstream.called(EXPO)


# ---------------------------------------------------------------------------
# One notification per event, not per favourite
# ---------------------------------------------------------------------------


async def test_two_favourites_matching_one_event_send_one_notification(
    dispatch_on: None,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
) -> None:
    """One thing happening in the world is one notification, from the nearer place."""
    db.seed("push_tokens", token_row(device_id))
    db.seed(
        "favorites",
        favorite_row(device_id, name="Home"),
        favorite_row(device_id, name="Office", lat=OFFICE_LAT, lon=OFFICE_LON),
    )
    feed.serve(event())
    expo_ok(upstream)

    stats = await alert_dispatch.run_pass()

    assert stats.candidates == 1
    messages = sent_messages(upstream)
    assert len(messages) == 1
    assert messages[0]["body"] == "Moderate · 6 km from Home"
    assert messages[0]["data"]["favoriteName"] == "Home"


async def test_radius_decides_which_device_hears_about_an_event(
    dispatch_on: None,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
    other_device_id: str,
) -> None:
    """Two devices, one event ~75 km away, different radii. Only one is told.

    Written as a comparison rather than a single exclusion so that code which
    matched nothing at all could not pass it.
    """
    db.seed("push_tokens", token_row(device_id), token_row(other_device_id))
    db.seed(
        "favorites",
        favorite_row(device_id, alert_radius_km=50.0),
        favorite_row(other_device_id, alert_radius_km=100.0),
    )
    feed.serve(event(lat=MID_LAT, lon=MID_LON))
    expo_ok(upstream)

    stats = await alert_dispatch.run_pass()

    assert stats.devices == 2
    assert stats.candidates == 1
    assert [m["to"] for m in sent_messages(upstream)] == [f"ExponentPushToken[{other_device_id}]"]


async def test_event_far_outside_every_radius_is_ignored(
    dispatch_on: None, feed: Feed, db: FakeSupabaseClient, upstream: UpstreamRouter, device_id: str
) -> None:
    db.seed("push_tokens", token_row(device_id))
    db.seed("favorites", favorite_row(device_id))
    feed.serve(event(lat=FAR_LAT, lon=FAR_LON))

    stats = await alert_dispatch.run_pass()

    assert stats.candidates == 0
    assert not upstream.called(EXPO)


async def test_event_without_an_id_is_dropped(
    dispatch_on: None,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
) -> None:
    """An unclaimable alert would be re-sent on every pass for as long as it lasted."""
    db.seed("push_tokens", token_row(device_id))
    db.seed("favorites", favorite_row(device_id))
    feed.serve(event(""), event("usgs:quake-2"))
    expo_ok(upstream)

    stats = await alert_dispatch.run_pass()

    assert stats.candidates == 1
    assert [m["data"]["eventId"] for m in sent_messages(upstream)] == ["usgs:quake-2"]


async def test_device_without_favourites_is_visited_and_skipped(
    dispatch_on: None, feed: Feed, db: FakeSupabaseClient, upstream: UpstreamRouter, device_id: str
) -> None:
    """No saved locations means nothing to match against. Not an error."""
    db.seed("push_tokens", token_row(device_id))
    feed.serve(event())

    stats = await alert_dispatch.run_pass()

    assert stats.devices == 1
    assert stats.candidates == 0
    assert db.count("sent_alerts") == 0
    assert not upstream.called(EXPO)


async def test_muted_device_is_never_paged_in(
    dispatch_on: None, feed: Feed, db: FakeSupabaseClient, device_id: str
) -> None:
    """``alerts_enabled`` is filtered in the query, not after it."""
    db.seed("push_tokens", token_row(device_id, alerts_enabled=False))
    db.seed("favorites", favorite_row(device_id))
    feed.serve(event())

    stats = await alert_dispatch.run_pass()

    assert stats.devices == 0
    page = db.queries_for("push_tokens", "select")[0]
    assert page.value_for("alerts_enabled") is True


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


async def test_devices_are_paged_by_keyset_on_device_id(
    dispatch_on: None,
    settings_override: Any,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
) -> None:
    """Keyset on ``device_id``, not on ``updated_at``.

    The dispatcher writes ``updated_at`` on rows it walks, so paginating on it
    would make the cursor move under the walk and skip devices silently.
    """
    settings_override(push_batch_size=1)
    for name in ("device-a", "device-b", "device-c"):
        db.seed("push_tokens", token_row(name))
        db.seed("favorites", favorite_row(name))
    feed.serve(event())
    expo_ok(upstream)

    stats = await alert_dispatch.run_pass()

    assert stats.devices == 3
    assert stats.sent == 3
    pages = db.queries_for("push_tokens", "select")
    # Three full pages, then one that comes back empty and ends the walk.
    assert len(pages) == 4
    assert not pages[0].filtered_on("device_id")
    assert pages[1].value_for("device_id") == "device-a"
    assert pages[2].value_for("device_id") == "device-b"
    assert pages[3].value_for("device_id") == "device-c"


async def test_page_ceiling_is_logged_rather_than_silently_truncating(
    dispatch_on: None,
    settings_override: Any,
    feed: Feed,
    db: FakeSupabaseClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pathological table must not quietly leave devices unvisited."""
    settings_override(push_batch_size=1)
    monkeypatch.setattr(alert_dispatch, "MAX_PAGES_PER_PASS", 2)
    for name in ("device-a", "device-b", "device-c"):
        db.seed("push_tokens", token_row(name))
    feed.serve(event())

    with caplog.at_level(logging.WARNING):
        stats = await alert_dispatch.run_pass()

    assert stats.devices == 2
    assert "stopped at the page ceiling" in caplog.text


# ---------------------------------------------------------------------------
# Quiet hours: the arithmetic, against fixed instants
# ---------------------------------------------------------------------------


def test_same_day_quiet_window_is_half_open() -> None:
    """09:00-17:00 includes 09 and excludes 17, so two adjacent windows cannot overlap."""
    row = {"quiet_hours_start": 9, "quiet_hours_end": 17, "timezone": ZONE}
    assert alert_dispatch.in_quiet_hours(row, _MORNING_IST) is True
    assert alert_dispatch.in_quiet_hours(row, _EVENING_IST) is False


def test_quiet_window_wraps_midnight() -> None:
    """22:00-07:00 is the common case, and it is not an empty range."""
    row = {"quiet_hours_start": 22, "quiet_hours_end": 7, "timezone": ZONE}
    assert alert_dispatch.in_quiet_hours(row, _MIDNIGHT_IST) is True
    assert alert_dispatch.in_quiet_hours(row, _MORNING_IST) is False


def test_zero_width_window_means_no_quiet_hours() -> None:
    """Both readings are defensible; only one can silence a device by accident."""
    row = {"quiet_hours_start": 3, "quiet_hours_end": 3, "timezone": ZONE}
    assert alert_dispatch.in_quiet_hours(row, _MIDNIGHT_IST) is False


def test_quiet_window_without_a_timezone_delivers() -> None:
    """Assuming UTC would silence a user in IST for the wrong nine hours of their day."""
    row = {"quiet_hours_start": 22, "quiet_hours_end": 7, "timezone": None}
    assert alert_dispatch.in_quiet_hours(row, _MIDNIGHT_IST) is False


def test_half_specified_window_delivers() -> None:
    row = {"quiet_hours_start": 22, "quiet_hours_end": None, "timezone": ZONE}
    assert alert_dispatch.in_quiet_hours(row, _MIDNIGHT_IST) is False


def test_unresolvable_timezone_delivers_and_says_why(caplog: pytest.LogCaptureFixture) -> None:
    """An image without ``tzdata`` must not silently mute every device that set a window."""
    row = {"quiet_hours_start": 22, "quiet_hours_end": 7, "timezone": "Mars/Olympus_Mons"}

    with caplog.at_level(logging.WARNING):
        assert alert_dispatch.in_quiet_hours(row, _MIDNIGHT_IST) is False

    assert "delivering rather than guessing" in caplog.text


# ---------------------------------------------------------------------------
# Quiet hours: the pass
# ---------------------------------------------------------------------------


async def test_quiet_hours_suppress_without_claiming(
    dispatch_on: None, feed: Feed, db: FakeSupabaseClient, device_id: str
) -> None:
    """The suppressed alert must stay unclaimed so a later pass can still deliver it.

    ``sent_alerts`` being empty is the whole assertion. A candidate that was
    claimed and then dropped looks identical from the transport layer, but no
    pass would ever deliver it.
    """
    db.seed("push_tokens", token_row(device_id, **quiet_window_covering_now()))
    db.seed("favorites", favorite_row(device_id))
    feed.serve(event(severity="Moderate"))

    stats = await alert_dispatch.run_pass()

    assert stats.suppressed_quiet == 1
    assert stats.claimed == 0
    assert stats.sent == 0
    assert db.count("sent_alerts") == 0


async def test_a_window_that_excludes_now_does_not_suppress(
    dispatch_on: None,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
) -> None:
    """The companion to the test above: prove the window is actually being read."""
    db.seed("push_tokens", token_row(device_id, **quiet_window_excluding_now()))
    db.seed("favorites", favorite_row(device_id))
    feed.serve(event(severity="Moderate"))
    expo_ok(upstream)

    stats = await alert_dispatch.run_pass()

    assert stats.suppressed_quiet == 0
    assert stats.sent == 1


async def test_severe_event_breaks_through_quiet_hours(
    dispatch_on: None,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
) -> None:
    """A tsunami at 3 a.m. is exactly what the user installed this for."""
    db.seed("push_tokens", token_row(device_id, **quiet_window_covering_now()))
    db.seed("favorites", favorite_row(device_id))
    feed.serve(event("usgs:big", severity="Severe"), event("usgs:small", severity="Moderate"))
    expo_ok(upstream)

    stats = await alert_dispatch.run_pass()

    assert stats.suppressed_quiet == 1
    assert stats.sent == 1
    assert [m["data"]["eventId"] for m in sent_messages(upstream)] == ["usgs:big"]


async def test_the_breakthrough_line_is_configurable(
    dispatch_on: None,
    settings_override: Any,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
) -> None:
    settings_override(push_quiet_hours_breakthrough="Moderate")
    db.seed("push_tokens", token_row(device_id, **quiet_window_covering_now()))
    db.seed("favorites", favorite_row(device_id))
    feed.serve(event(severity="Moderate"))
    expo_ok(upstream)

    stats = await alert_dispatch.run_pass()

    assert stats.suppressed_quiet == 0
    assert stats.sent == 1


async def test_unknown_severity_does_not_break_through_quiet_hours(
    dispatch_on: None, feed: Feed, db: FakeSupabaseClient, device_id: str
) -> None:
    """The one place in the alerting path where unknown means no.

    Matching is fail-open, so an unrecognised severity still becomes a candidate
    — but "wake the user at 3 a.m." is not a default anything should fall into.
    """
    db.seed("push_tokens", token_row(device_id, **quiet_window_covering_now()))
    db.seed("favorites", favorite_row(device_id))
    feed.serve(event(severity="Catastrophic"))

    stats = await alert_dispatch.run_pass()

    assert stats.candidates == 0
    assert stats.suppressed_quiet == 1
    assert stats.sent == 0
    assert db.count("sent_alerts") == 0


# ---------------------------------------------------------------------------
# The volume cap
# ---------------------------------------------------------------------------


async def test_volume_cap_counts_the_last_hour_not_just_this_pass(
    dispatch_on: None,
    settings_override: Any,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
) -> None:
    """Without the lookback, a three-per-pass cap on a 15-minute timer is 12 an hour.

    Two prior deliveries are seeded, only one of them inside the window. If the
    count were unbounded the budget would be zero and nothing would go out at
    all, so this pins the window as well as the cap.

    The survivor is the most severe of the three, because the cap truncates a
    ranked list rather than taking whatever the feed happened to list first.
    """
    settings_override(push_max_per_device_per_pass=2)
    db.seed("push_tokens", token_row(device_id))
    db.seed("favorites", favorite_row(device_id))
    db.seed(
        "sent_alerts",
        ledger_row(
            device_id,
            "usgs:earlier",
            status=DELIVERY_SENT,
            attempts=1,
            claimed_hours_ago=0.25,
        ),
        ledger_row(
            device_id,
            "usgs:yesterday",
            status=DELIVERY_SENT,
            attempts=1,
            claimed_hours_ago=2.0,
        ),
    )
    feed.serve(
        event("usgs:a", severity="Moderate"),
        event("usgs:b", severity="Extreme"),
        event("usgs:c", severity="Severe"),
    )
    expo_ok(upstream)

    stats = await alert_dispatch.run_pass()

    assert stats.suppressed_cap == 2
    assert stats.sent == 1
    assert [m["data"]["eventId"] for m in sent_messages(upstream)] == ["usgs:b"]


async def test_capped_candidates_are_dropped_unclaimed(
    dispatch_on: None,
    settings_override: Any,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
) -> None:
    """A capped alert is deferred, not discarded — so the next pass can send it."""
    settings_override(push_max_per_device_per_pass=1)
    db.seed("push_tokens", token_row(device_id))
    db.seed("favorites", favorite_row(device_id))
    feed.serve(event("usgs:a", severity="Extreme"), event("usgs:b", severity="Moderate"))
    expo_ok(upstream)

    first = await alert_dispatch.run_pass()

    assert first.suppressed_cap == 1
    assert db.find("sent_alerts", event_id="usgs:b") is None

    # The budget is spent for the hour, but nothing about usgs:b was recorded, so
    # the claim is still available to whichever pass has room for it.
    settings_override(push_max_per_device_per_pass=5)
    second = await alert_dispatch.run_pass()

    assert second.sent == 1
    assert db.find("sent_alerts", event_id="usgs:b")["status"] == DELIVERY_SENT


# ---------------------------------------------------------------------------
# Settling: what happens to a claim when the send does not succeed
# ---------------------------------------------------------------------------


async def test_dead_token_is_pruned_and_its_claim_is_closed_for_good(
    dispatch_on: None,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
) -> None:
    """``DeviceNotRegistered`` is the only error that means "delete this row".

    Retrying it would fail identically forever while consuming the attempt budget
    a genuinely transient failure needs, so ``attempts`` is written straight to
    the ceiling. And Expo puts the token in the message text, so the scrubbing is
    asserted here rather than assumed.
    """
    db.seed("push_tokens", token_row(device_id))
    db.seed("favorites", favorite_row(device_id))
    feed.serve(event())
    expo_dead_token(upstream)

    stats = await alert_dispatch.run_pass()

    assert stats.sent == 0
    assert stats.failed == 1
    assert stats.dead_tokens_pruned == 1
    assert db.count("push_tokens") == 0

    row = db.find("sent_alerts", device_id=device_id, event_id="usgs:quake-1")
    assert row["status"] == DELIVERY_FAILED
    assert row["attempts"] == settings.push_max_attempts
    assert "[token]" in row["last_error"]
    assert "ExponentPushToken" not in row["last_error"]
    assert device_id not in row["last_error"]


async def test_transient_failure_keeps_the_token_and_is_not_swept_by_its_own_pass(
    dispatch_on: None,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
) -> None:
    """Two properties, and the failure is what makes the second one meaningful.

    ``ProviderError`` is transient, so the row is left at one attempt with the
    token intact. It is now ``failed`` with ``attempts`` below the ceiling, which
    means the retry sweep's status and attempt filters both accept it — leaving
    ``claimed_at`` as the only thing holding it back. A second Expo request would
    therefore mean the staleness guard is gone, and every pass would re-send
    everything it had just sent. Had the send succeeded, the status filter alone
    would explain the absence and the test would prove nothing.
    """
    db.seed("push_tokens", token_row(device_id))
    db.seed("favorites", favorite_row(device_id))
    feed.serve(event())
    expo_error(upstream, "ProviderError")

    stats = await alert_dispatch.run_pass()

    assert stats.failed == 1
    assert stats.dead_tokens_pruned == 0
    assert db.count("push_tokens") == 1
    assert upstream.call_count(EXPO) == 1

    row = db.find("sent_alerts", device_id=device_id, event_id="usgs:quake-1")
    assert row["status"] == DELIVERY_FAILED
    assert row["attempts"] == 1
    assert row["sent_at"] is None

    sweep = next(q for q in db.queries_for("sent_alerts", "select") if q.filtered_on("attempts"))
    assert sweep.value_for("attempts") == settings.push_max_attempts
    assert sweep.filtered_on("claimed_at"), "the staleness guard is what stops a self-resend"


async def test_a_failed_request_settles_every_claim_in_the_batch(
    dispatch_on: None,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
    other_device_id: str,
) -> None:
    """One HTTP failure, two claims, and neither is left ``pending``.

    A claim left pending is recoverable — the sweep finds it — but it is
    recoverable a quarter of an hour later. Settling the whole batch means the
    next pass can retry immediately instead of waiting for the row to go stale.
    """
    db.seed("push_tokens", token_row(device_id), token_row(other_device_id))
    db.seed("favorites", favorite_row(device_id), favorite_row(other_device_id))
    feed.serve(event())
    upstream.status(EXPO, 503)

    stats = await alert_dispatch.run_pass()

    assert stats.claimed == 2
    assert stats.sent == 0
    assert stats.failed == 2
    assert db.count("push_tokens") == 2, "a transport failure says nothing about a token"
    for who in (device_id, other_device_id):
        row = db.find("sent_alerts", device_id=who, event_id="usgs:quake-1")
        assert row["status"] == DELIVERY_FAILED
        assert row["attempts"] == 1


# ---------------------------------------------------------------------------
# The retry sweep
# ---------------------------------------------------------------------------


async def test_sweep_resends_a_claim_left_pending_by_a_crashed_pass(
    dispatch_on: None,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
) -> None:
    """This is what makes claim-before-send cost at most one delayed alert.

    A pass that died between the claim and the send leaves a ``pending`` row that
    no later pass would re-claim. The sweep is the only thing that recovers it.
    """
    db.seed("push_tokens", token_row(device_id))
    db.seed("favorites", favorite_row(device_id))
    db.seed("sent_alerts", ledger_row(device_id, "usgs:quake-1", claimed_hours_ago=2.0))
    feed.serve(event())
    expo_ok(upstream)

    stats = await alert_dispatch.run_pass()

    assert stats.claimed == 0, "the walk cannot re-claim it; only the sweep reaches it"
    assert stats.sent == 1
    assert upstream.call_count(EXPO) == 1

    row = db.find("sent_alerts", device_id=device_id, event_id="usgs:quake-1")
    assert row["status"] == DELIVERY_SENT
    assert row["attempts"] == 1


async def test_sweep_abandons_a_claim_whose_device_has_no_token_row(
    dispatch_on: None, feed: Feed, db: FakeSupabaseClient, device_id: str
) -> None:
    """The app was uninstalled between the claim and the retry."""
    db.seed("sent_alerts", ledger_row(device_id, "usgs:quake-1", claimed_hours_ago=2.0))
    feed.serve(event())

    stats = await alert_dispatch.run_pass()

    assert stats.failed == 1
    row = db.find("sent_alerts", device_id=device_id, event_id="usgs:quake-1")
    assert row["status"] == DELIVERY_FAILED
    assert row["attempts"] == settings.push_max_attempts
    assert row["last_error"] == "device no longer accepts alerts"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"alerts_enabled": False}, "the user turned alerts off after the failure"),
        ({"token": ""}, "the row survived but the token did not"),
    ],
    ids=["alerts-off", "blank-token"],
)
async def test_sweep_abandons_a_claim_the_device_can_no_longer_receive(
    dispatch_on: None,
    feed: Feed,
    db: FakeSupabaseClient,
    device_id: str,
    overrides: Dict[str, Any],
    reason: str,
) -> None:
    db.seed("push_tokens", token_row(device_id, **overrides))
    db.seed("favorites", favorite_row(device_id))
    db.seed("sent_alerts", ledger_row(device_id, "usgs:quake-1", claimed_hours_ago=2.0))
    feed.serve(event())

    stats = await alert_dispatch.run_pass()

    assert stats.failed == 1, reason
    row = db.find("sent_alerts", device_id=device_id, event_id="usgs:quake-1")
    assert row["status"] == DELIVERY_FAILED
    assert row["last_error"] == "device no longer accepts alerts"
    assert row["attempts"] == settings.push_max_attempts


async def test_sweep_abandons_a_claim_whose_event_left_the_feed(
    dispatch_on: None,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
) -> None:
    """Abandoned rather than sent late, for the reason ``PUSH_TTL_SECONDS`` exists.

    Retries are rebuilt from the current feed, never replayed from a stored
    payload, so an event that has aged out has nothing to rebuild from — and a
    hazard notification that arrives six hours late reads as a current one.
    """
    db.seed("push_tokens", token_row(device_id))
    db.seed("favorites", favorite_row(device_id))
    db.seed("sent_alerts", ledger_row(device_id, "usgs:gone", claimed_hours_ago=2.0))
    feed.serve(event("usgs:quake-1"))
    expo_ok(upstream)

    stats = await alert_dispatch.run_pass()

    assert stats.sent == 1, "the event that is still in the feed still goes out"
    assert stats.failed == 1
    assert [m["data"]["eventId"] for m in sent_messages(upstream)] == ["usgs:quake-1"]

    row = db.find("sent_alerts", device_id=device_id, event_id="usgs:gone")
    assert row["status"] == DELIVERY_FAILED
    assert row["attempts"] == settings.push_max_attempts
    assert row["last_error"] == "event no longer matches this device"


async def test_a_retry_is_refiltered_against_current_preferences(
    dispatch_on: None, feed: Feed, db: FakeSupabaseClient, device_id: str
) -> None:
    """Raising the severity floor after a failure abandons the old alert.

    A consequence of rebuilding from the feed rather than replaying a payload,
    and the desirable one: the user's current preference wins.
    """
    db.seed("push_tokens", token_row(device_id, min_severity="Extreme"))
    db.seed("favorites", favorite_row(device_id))
    db.seed("sent_alerts", ledger_row(device_id, "usgs:quake-1", claimed_hours_ago=2.0))
    feed.serve(event(severity="Moderate"))

    stats = await alert_dispatch.run_pass()

    assert stats.sent == 0
    row = db.find("sent_alerts", device_id=device_id, event_id="usgs:quake-1")
    assert row["last_error"] == "event no longer matches this device"


async def test_quiet_hours_leave_a_retry_unsettled(
    dispatch_on: None, feed: Feed, db: FakeSupabaseClient, device_id: str
) -> None:
    """Not abandoned, not retried — left exactly as it was.

    The claim has to stay stale and retryable so the first pass after the window
    ends picks it up. Writing anything at all would either reset ``claimed_at``
    or burn an attempt, and either one loses the alert.
    """
    db.seed("push_tokens", token_row(device_id, **quiet_window_covering_now()))
    db.seed("favorites", favorite_row(device_id))
    before = ledger_row(
        device_id,
        "usgs:quake-1",
        status=DELIVERY_FAILED,
        attempts=1,
        claimed_hours_ago=2.0,
        last_error="upstream unavailable",
    )
    db.seed("sent_alerts", before)
    feed.serve(event(severity="Moderate"))

    stats = await alert_dispatch.run_pass()

    # Once in the walk, once in the sweep: the same event, counted at both gates.
    assert stats.suppressed_quiet == 2
    assert stats.sent == 0
    assert stats.failed == 0

    row = db.find("sent_alerts", device_id=device_id, event_id="usgs:quake-1")
    assert {key: row[key] for key in before} == before


async def test_sweep_does_not_reapply_the_volume_cap(
    dispatch_on: None,
    settings_override: Any,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
) -> None:
    """A retry is an alert the device was already cleared for.

    Its own claim is already inside the counting window and ``attempts`` bounds
    it, so capping it again would starve exactly the alerts the sweep exists to
    rescue. Here the walk's budget is zero and the retry still goes out.
    """
    settings_override(push_max_per_device_per_pass=1)
    db.seed("push_tokens", token_row(device_id))
    db.seed("favorites", favorite_row(device_id))
    db.seed(
        "sent_alerts",
        ledger_row(
            device_id,
            "usgs:earlier",
            status=DELIVERY_SENT,
            attempts=1,
            claimed_hours_ago=0.25,
        ),
        ledger_row(
            device_id,
            "usgs:retry",
            status=DELIVERY_FAILED,
            attempts=1,
            claimed_hours_ago=2.0,
            last_error="upstream unavailable",
        ),
    )
    feed.serve(event("usgs:retry"))
    expo_ok(upstream)

    stats = await alert_dispatch.run_pass()

    assert stats.suppressed_cap == 1, "the walk had no budget left"
    assert stats.sent == 1
    assert [m["data"]["eventId"] for m in sent_messages(upstream)] == ["usgs:retry"]
    assert db.find("sent_alerts", event_id="usgs:retry")["status"] == DELIVERY_SENT


async def test_a_claim_at_the_attempt_ceiling_is_never_retried_again(
    dispatch_on: None,
    settings_override: Any,
    feed: Feed,
    db: FakeSupabaseClient,
    device_id: str,
) -> None:
    """Without the bound, a broken token is retried on every pass until pruned."""
    settings_override(push_max_attempts=2)
    db.seed("push_tokens", token_row(device_id))
    db.seed("favorites", favorite_row(device_id))
    db.seed(
        "sent_alerts",
        ledger_row(
            device_id,
            "usgs:quake-1",
            status=DELIVERY_FAILED,
            attempts=2,
            claimed_hours_ago=3.0,
            last_error="upstream unavailable",
        ),
    )
    feed.serve(event())

    stats = await alert_dispatch.run_pass()

    assert stats.sent == 0
    assert stats.failed == 0
    assert db.find("sent_alerts", event_id="usgs:quake-1")["attempts"] == 2


# ---------------------------------------------------------------------------
# Feed and datastore failure
# ---------------------------------------------------------------------------


async def test_empty_feed_with_failed_providers_aborts_the_whole_pass(
    dispatch_on: None, feed: Feed, db: FakeSupabaseClient, device_id: str
) -> None:
    """An outage is not a quiet planet, and the difference is load-bearing here.

    The sweep abandons claims whose event is absent from the feed, so running it
    against an empty feed caused by four dead upstreams would discard live alerts
    as "no longer matching". The seeded claim surviving is the real assertion.
    """
    db.seed("push_tokens", token_row(device_id))
    db.seed("favorites", favorite_row(device_id))
    db.seed("sent_alerts", ledger_row(device_id, "usgs:quake-1", claimed_hours_ago=2.0))
    feed.serve(failed=["usgs", "gdacs"])

    stats = await alert_dispatch.run_pass()

    assert stats.aborted == "feed_unavailable"
    assert stats.sources_failed == ["usgs", "gdacs"]
    assert stats.devices == 0
    assert db.find("sent_alerts", event_id="usgs:quake-1")["status"] == DELIVERY_PENDING


async def test_a_genuinely_quiet_feed_is_not_treated_as_an_outage(
    dispatch_on: None, feed: Feed, db: FakeSupabaseClient, device_id: str
) -> None:
    """No events and no failures is a good day, and it must log as one."""
    db.seed("push_tokens", token_row(device_id))
    db.seed("favorites", favorite_row(device_id))
    feed.serve()

    stats = await alert_dispatch.run_pass()

    assert stats.aborted is None
    assert stats.events == 0
    assert stats.sent == 0
    assert db.count("sent_alerts") == 0


async def test_a_partly_failed_feed_still_delivers_what_it_did_fetch(
    dispatch_on: None,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
) -> None:
    """One dead provider must not cost the alerts the others returned."""
    db.seed("push_tokens", token_row(device_id))
    db.seed("favorites", favorite_row(device_id))
    feed.serve(event(), failed=["gdacs"])
    expo_ok(upstream)

    stats = await alert_dispatch.run_pass()

    assert stats.aborted is None
    assert stats.sent == 1
    assert stats.sources_failed == ["gdacs"]
    assert stats.as_log_fields()["sources_failed"] == "gdacs"


async def test_a_datastore_outage_is_recorded_rather_than_raised(
    dispatch_on: None, feed: Feed, db: FakeSupabaseClient
) -> None:
    """The worker's loop is a plain timer, so a broken pass must return, not throw."""
    feed.serve(event())
    db.failure = FakeDBError("connection reset")

    stats = await alert_dispatch.run_pass()

    assert stats.aborted == "ServiceUnavailableError"
    assert stats.events == 1
    assert stats.sent == 0


def test_log_fields_report_no_failed_sources_as_absent_rather_than_empty() -> None:
    """An empty string in a log field reads as "unknown", not "none failed"."""
    fields = alert_dispatch.DispatchStats().as_log_fields()

    assert fields["sources_failed"] is None
    assert fields["aborted"] is None


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


async def test_prune_ledger_drops_claims_past_the_retention_window(
    settings_override: Any, db: FakeSupabaseClient, device_id: str
) -> None:
    """Without this, every event a device ever matched is suppression state forever."""
    settings_override(sent_alerts_retention_days=7)
    db.seed(
        "sent_alerts",
        ledger_row(device_id, "usgs:ancient", status=DELIVERY_SENT, claimed_hours_ago=24 * 30),
        ledger_row(device_id, "usgs:recent", status=DELIVERY_SENT, claimed_hours_ago=24 * 2),
    )

    removed = await alert_dispatch.prune_ledger()

    assert removed == 1
    assert db.find("sent_alerts", event_id="usgs:ancient") is None
    assert db.find("sent_alerts", event_id="usgs:recent") is not None


# ---------------------------------------------------------------------------
# Message composition
# ---------------------------------------------------------------------------


def _candidate(
    *, severity: str = "Moderate", distance_km: float = 12.4, name: str = "Home"
) -> alert_dispatch.Candidate:
    payload = event(severity=severity)
    return alert_dispatch.Candidate(
        event_id=str(payload["id"]),
        match=EventMatch(event=payload, distance_km=distance_km),
        favorite_name=name,
    )


def test_the_notification_says_how_bad_how_far_and_from_where() -> None:
    """The body is the whole message on a lock screen; the title is the feed's."""
    message = alert_dispatch.build_message("ExponentPushToken[abc]", _candidate(severity="Severe"))

    assert message.title == "M 5.2 - 6 km WNW of Delhi"
    assert message.body == "Severe · 12 km from Home"
    assert message.data["eventId"] == "usgs:quake-1"
    assert message.data["category"] == "earthquake"
    assert message.data["severity"] == "Severe"
    assert message.data["distanceKm"] == 12.4
    assert message.data["favoriteName"] == "Home"
    assert (message.data["lat"], message.data["lon"]) == (NEAR_LAT, NEAR_LON)


def test_a_sub_kilometre_distance_is_a_phrase_not_a_rounded_zero() -> None:
    """A rounded zero reads as a formatting bug at exactly the wrong moment."""
    message = alert_dispatch.build_message("ExponentPushToken[abc]", _candidate(distance_km=0.4))

    assert message.body == "Moderate · under 1 km from Home"


def test_candidates_are_ranked_most_severe_first_then_nearest() -> None:
    """The volume cap truncates this list, so its order decides what gets through."""
    candidates = alert_dispatch.candidates_for_device(
        [favorite_row("device-a")],
        [
            event("near-moderate", severity="Moderate", lat=NEAR_LAT, lon=NEAR_LON),
            event("far-severe", severity="Severe", lat=MID_LAT, lon=MID_LON),
            event("near-severe", severity="Severe", lat=NEAR_LAT, lon=NEAR_LON),
        ],
        min_severity=None,
    )

    assert [candidate.event_id for candidate in candidates] == [
        "near-severe",
        "far-severe",
        "near-moderate",
    ]


def test_the_nearer_favourite_names_a_shared_event() -> None:
    """One event, two favourites: attributed to whichever is closer to it."""
    candidates = alert_dispatch.candidates_for_device(
        [
            favorite_row("device-a", name="Office", lat=OFFICE_LAT, lon=OFFICE_LON),
            favorite_row("device-a", name="Home", lat=HOME_LAT, lon=HOME_LON),
        ],
        [event()],
        min_severity=None,
    )

    assert [candidate.favorite_name for candidate in candidates] == ["Home"]


# ---------------------------------------------------------------------------
# Token safety
# ---------------------------------------------------------------------------


async def test_no_push_token_reaches_a_log_record_or_a_stored_error(
    dispatch_on: None,
    feed: Feed,
    db: FakeSupabaseClient,
    upstream: UpstreamRouter,
    device_id: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A push token is a capability to notify someone. It belongs in one column.

    The failure mode this guards is not theoretical: Expo puts the token verbatim
    into the *text* of a ``DeviceNotRegistered`` error, so the obvious
    implementation — log the upstream message, store it in ``last_error`` —
    publishes a live token to the log sink and a database column at once.

    Run at DEBUG so httpx's own request logging is in scope too, and assert the
    pass actually ran first: every other assertion here is a negative, and
    negatives pass beautifully against an empty log.
    """
    canary = "ExponentPushToken[secret-canary-1]"
    db.seed("push_tokens", token_row(device_id, token=canary))
    db.seed("favorites", favorite_row(device_id))
    feed.serve(event())
    expo_dead_token(upstream)

    with caplog.at_level(logging.DEBUG):
        stats = await alert_dispatch.run_pass()

    surface = log_surface(caplog)
    assert "Dispatch pass complete" in surface, "nothing was logged; the negatives are vacuous"
    assert stats.dead_tokens_pruned == 1
    assert "secret-canary-1" not in surface
    assert "ExponentPushToken" not in surface

    row = db.find("sent_alerts", device_id=device_id, event_id="usgs:quake-1")
    assert row["status"] == DELIVERY_FAILED
    assert "secret-canary-1" not in row["last_error"]
    assert "[token]" in row["last_error"], "the scrub ran, rather than the text being empty"
    assert db.count("push_tokens") == 0
