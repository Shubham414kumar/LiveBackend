"""``/api/notifications`` — registration and the alert preferences screen.

Three properties are load-bearing here.

**Registration is keyed on the device, not on the token.** The app re-registers
on almost every cold start, and an Expo token rotates on reinstall. Keying on the
token would accumulate rows that fail delivery forever — and worse, a
re-registration that reset the preference columns would switch alerts back on for
a user who had deliberately switched them off.

**The token never comes back out.** Nothing on the client needs it returned; the
client is where it came from. A response body is one of the easiest things in a
system to end up in a proxy log.

**A quiet window that cannot be evaluated is refused, not stored.** "22:00 to
06:00" means nothing without a zone, and ``alert_dispatch.in_quiet_hours``
resolves that ambiguity by delivering. Delivering is the right fallback, but the
user should find out here rather than at 3 a.m.

Cross-device isolation for the register routes lives in ``test_security.py``,
which drives every device-scoped route through the same table.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, get_args

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app.api.routes.notifications import _NOT_REGISTERED
from app.core.config import SEVERITY_LEVELS
from app.models.schemas import MAX_TIMEZONE_LENGTH, PushSeverity
from tests.conftest import assert_problem
from tests.fakes import FakeSupabaseClient

REGISTER = "/api/notifications/register"
PREFERENCES = "/api/notifications/preferences"

#: Two syntactically valid Expo tokens. The shape matters — the schema rejects
#: anything without the ``ExponentPushToken[`` prefix and the ``]`` suffix.
TOKEN = "ExponentPushToken[aaaaaaaaaaaaaaaaaaaaaa]"
ROTATED = "ExponentPushToken[bbbbbbbbbbbbbbbbbbbbbb]"

KOLKATA = "Asia/Kolkata"


def register(
    client: TestClient, headers: Dict[str, str], *, token: str = TOKEN, **extra: Any
) -> Response:
    return client.post(REGISTER, json={"token": token, **extra}, headers=headers)


def enrolled(client: TestClient, headers: Dict[str, str], **extra: Any) -> None:
    """A device that has registered — the precondition for every PATCH below."""
    assert register(client, headers, **extra).status_code == 200


def read_preferences(client: TestClient, headers: Dict[str, str]) -> Dict[str, Any]:
    response = client.get(PREFERENCES, headers=headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


def patch_preferences(client: TestClient, headers: Dict[str, str], **payload: Any) -> Response:
    """PATCH with exactly the keys passed in.

    Omitting a keyword leaves it off the wire entirely, which is how the tests
    below tell "leave this alone" apart from an explicit ``null``.
    """
    return client.patch(PREFERENCES, json=payload, headers=headers)


def rejected_fields(response: Response) -> List[str]:
    """The fields a 422 named, so a test can prove the *right* one was rejected."""
    body = assert_problem(response, 422, code="validation_error")
    return [str(error["field"]) for error in body["errors"]]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_registering_stores_the_token_against_this_device(
    client: TestClient, db: FakeSupabaseClient, device_headers: Dict[str, str], device_id: str
) -> None:
    response = register(client, device_headers, platform="android", lat=28.6139, lon=77.2090)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "registered"

    row = db.find("push_tokens", device_id=device_id)
    assert row is not None
    assert row["token"] == TOKEN
    assert row["platform"] == "android"
    assert (row["lat"], row["lon"]) == (28.6139, 77.2090)


def test_a_rotated_token_replaces_the_row_instead_of_adding_one(
    client: TestClient, db: FakeSupabaseClient, device_headers: Dict[str, str], device_id: str
) -> None:
    """Keyed on ``device_id``, so a reinstall does not leave a dead token behind
    that the dispatcher will fail to deliver to forever."""
    enrolled(client, device_headers)

    assert register(client, device_headers, token=ROTATED).status_code == 200

    assert db.count("push_tokens") == 1
    row = db.find("push_tokens", device_id=device_id)
    assert row is not None and row["token"] == ROTATED

    upserts = [q for q in db.executed if q.table == "push_tokens" and q.op == "upsert"]
    assert [q.on_conflict for q in upserts] == ["device_id", "device_id"]


def test_re_registering_leaves_the_stored_preferences_alone(
    client: TestClient, db: FakeSupabaseClient, device_headers: Dict[str, str], device_id: str
) -> None:
    """The one that matters. Preferences live on the token row, and the app
    re-registers on almost every cold start; if that reset them, a user who turned
    alerts off would be woken by the next Severe event anyway."""
    enrolled(client, device_headers)
    configured = patch_preferences(
        client,
        device_headers,
        alerts_enabled=False,
        min_severity="Extreme",
        quiet_hours_start=22,
        quiet_hours_end=6,
        timezone=KOLKATA,
    )
    assert configured.status_code == 200, configured.text

    assert register(client, device_headers, token=ROTATED).status_code == 200

    after = read_preferences(client, device_headers)
    assert after["alerts_enabled"] is False
    assert after["min_severity"] == "Extreme"
    assert (after["quiet_hours_start"], after["quiet_hours_end"]) == (22, 6)
    assert after["timezone"] == KOLKATA
    row = db.find("push_tokens", device_id=device_id)
    assert row is not None and row["token"] == ROTATED, "the token itself did refresh"


@pytest.mark.parametrize(
    "token",
    [
        "",
        "   ",
        "fcm:APA91bHun4MxP5egoKMwt2KZFBaFUH-1RYqx",
        "ExponentPushToken[no-closing-bracket",
        "exponentpushtoken[lowercased]",
        "ExponentPushToken[" + "x" * 500 + "]",
    ],
    ids=["empty", "blank", "fcm", "unterminated", "lowercased", "too-long"],
)
def test_a_token_that_is_not_an_expo_token_is_refused(
    client: TestClient, db: FakeSupabaseClient, device_headers: Dict[str, str], token: str
) -> None:
    """A junk token fails silently at send time, which is the worst kind of failure
    for an alerting system: the row looks registered and nothing ever arrives."""
    assert rejected_fields(register(client, device_headers, token=token)) == ["token"]
    assert db.count("push_tokens") == 0


# ---------------------------------------------------------------------------
# Unregistering
# ---------------------------------------------------------------------------


def test_unregistering_removes_the_token_and_the_screen_still_renders(
    client: TestClient, db: FakeSupabaseClient, device_headers: Dict[str, str]
) -> None:
    enrolled(client, device_headers)

    response = client.delete(REGISTER, headers=device_headers)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "unregistered"
    assert db.count("push_tokens") == 0
    assert read_preferences(client, device_headers)["registered"] is False


def test_unregistering_twice_is_not_an_error(
    client: TestClient, device_headers: Dict[str, str]
) -> None:
    """The app calls this from a toggle. A 404 on the second tap would surface as a
    failure the user can do nothing about, for an outcome they already have."""
    enrolled(client, device_headers)

    assert client.delete(REGISTER, headers=device_headers).status_code == 200
    assert client.delete(REGISTER, headers=device_headers).status_code == 200


def test_unregistering_leaves_other_devices_registered(
    client: TestClient,
    db: FakeSupabaseClient,
    device_headers: Dict[str, str],
    other_device_headers: Dict[str, str],
    other_device_id: str,
) -> None:
    enrolled(client, device_headers)
    enrolled(client, other_device_headers)

    assert client.delete(REGISTER, headers=device_headers).status_code == 200

    assert db.count("push_tokens") == 1
    assert db.find("push_tokens", device_id=other_device_id) is not None


# ---------------------------------------------------------------------------
# Reading preferences
# ---------------------------------------------------------------------------


def test_preferences_are_readable_before_the_device_has_ever_registered(
    client: TestClient, device_headers: Dict[str, str], settings_override: Any
) -> None:
    """200 with the server defaults, not a 404. The settings screen is reachable
    before notifications have ever been enabled and it has to render something; a
    404 would push that decision into every client.

    The two policy values are overridden rather than compared against ``settings``,
    so this asserts the response is *read from* configuration instead of asserting
    the configuration against itself.
    """
    settings_override(
        push_default_min_severity="Severe",
        push_quiet_hours_breakthrough="Extreme",
        push_enabled=True,
    )

    body = read_preferences(client, device_headers)

    assert body["registered"] is False
    assert body["alerts_enabled"] is True
    assert body["min_severity"] == "Severe"
    assert (body["quiet_hours_start"], body["quiet_hours_end"]) == (None, None)
    assert body["timezone"] is None
    assert body["quiet_hours_breakthrough"] == "Extreme"
    assert body["delivery_enabled"] is True


def test_a_registered_device_with_no_stated_choice_is_opted_in(
    client: TestClient, device_headers: Dict[str, str]
) -> None:
    """Registration writes the token and none of the preference columns, so
    ``alerts_enabled`` is absent on a fresh row — the column's default in Postgres,
    and simply missing here. Absent has to read as opted in: the device asked for
    notifications, and defaulting to off would mean registering achieved nothing.
    """
    enrolled(client, device_headers)

    body = read_preferences(client, device_headers)

    assert body["registered"] is True
    assert body["alerts_enabled"] is True


def test_the_response_says_whether_this_deployment_dispatches_at_all(
    client: TestClient, device_headers: Dict[str, str], settings_override: Any
) -> None:
    """``PUSH_ENABLED`` false is a real deployment state: preferences are stored and
    honoured, and nothing is sent. The screen can only be honest about that if the
    server tells it, so this value is not a constant on the client."""
    settings_override(push_enabled=False)
    assert read_preferences(client, device_headers)["delivery_enabled"] is False

    settings_override(push_enabled=True)
    assert read_preferences(client, device_headers)["delivery_enabled"] is True


def test_no_response_body_echoes_the_push_token(
    client: TestClient, db: FakeSupabaseClient, device_headers: Dict[str, str], device_id: str
) -> None:
    """Every response on this router, checked against the stored token as a canary.

    The client already has the token; nothing here needs it back. A response body
    is one of the easiest things in a system to end up in a proxy log.
    """
    canary = "ExponentPushToken[secret-canary-2]"

    responses = [register(client, device_headers, token=canary)]
    stored = db.find("push_tokens", device_id=device_id)
    assert stored is not None and stored["token"] == canary, "the canary was really stored"

    responses.append(client.get(PREFERENCES, headers=device_headers))
    responses.append(patch_preferences(client, device_headers, min_severity="Severe"))
    responses.append(client.delete(REGISTER, headers=device_headers))

    for response in responses:
        assert response.status_code == 200, response.text
        assert "secret-canary-2" not in response.text
        assert "ExponentPushToken" not in response.text

    assert db.find("push_tokens", device_id=device_id) is None, "and the row is gone"


# ---------------------------------------------------------------------------
# Updating preferences
# ---------------------------------------------------------------------------


def test_an_update_changes_only_the_fields_it_sent(
    client: TestClient, device_headers: Dict[str, str]
) -> None:
    """PATCH, and partial rather than whole-object. The preferences share a row with
    the push token, so a screen holding stale state that PUT its whole idea of them
    would silently undo a change made a moment earlier on another screen."""
    enrolled(client, device_headers)

    first = patch_preferences(client, device_headers, min_severity="Extreme")
    assert first.status_code == 200, first.text
    assert first.json()["min_severity"] == "Extreme"
    assert first.json()["alerts_enabled"] is True

    second = patch_preferences(client, device_headers, alerts_enabled=False)
    assert second.status_code == 200, second.text
    assert second.json()["alerts_enabled"] is False
    assert second.json()["min_severity"] == "Extreme", "untouched by the second call"


def test_an_empty_update_is_accepted_and_changes_nothing(
    client: TestClient, device_headers: Dict[str, str]
) -> None:
    """A settings screen saving with nothing changed is not an error."""
    enrolled(client, device_headers)
    before = read_preferences(client, device_headers)

    response = patch_preferences(client, device_headers)

    assert response.status_code == 200, response.text
    assert response.json() == before


def test_a_device_that_has_not_registered_has_nothing_to_update(
    client: TestClient, db: FakeSupabaseClient, device_headers: Dict[str, str]
) -> None:
    """404 here, unlike on the GET, and the message says what to do about it.
    Quietly creating a row would mean storing preferences for a device that cannot
    receive anything."""
    response = patch_preferences(client, device_headers, alerts_enabled=False)

    body = assert_problem(response, 404, code="not_found")
    assert body["detail"] == _NOT_REGISTERED
    assert db.count("push_tokens") == 0, "no row was conjured for it"


@pytest.mark.parametrize("level", list(SEVERITY_LEVELS))
def test_every_configured_severity_is_accepted_as_a_threshold(
    client: TestClient, device_headers: Dict[str, str], level: str
) -> None:
    """Including "Minor", which a report author is never offered but which USGS
    emits: a user who can see a Minor event on the map must be able to set a floor
    that includes it."""
    enrolled(client, device_headers)

    response = patch_preferences(client, device_headers, min_severity=level)

    assert response.status_code == 200, response.text
    assert response.json()["min_severity"] == level


@pytest.mark.parametrize(
    "level",
    ["moderate", "MODERATE", "Catastrophic", "", "None"],
    ids=["lowercased", "uppercased", "invented", "empty", "stringified-null"],
)
def test_a_threshold_outside_the_vocabulary_is_refused(
    client: TestClient, device_headers: Dict[str, str], level: str
) -> None:
    """Rejected at the schema, so the CHECK constraint on ``push_tokens`` is never
    the thing that answers — a constraint violation reaches the client as a 503 it
    cannot interpret."""
    enrolled(client, device_headers)

    response = patch_preferences(client, device_headers, min_severity=level)

    assert rejected_fields(response) == ["min_severity"]


def test_the_threshold_vocabulary_matches_the_configured_one() -> None:
    """The drift pin the ``PushSeverity`` comment promises. A ``Literal`` cannot be
    built from a runtime tuple, so the two lists are written out separately and held
    together here."""
    assert get_args(PushSeverity) == SEVERITY_LEVELS


def test_the_threshold_vocabulary_matches_the_database_constraint() -> None:
    """Third spelling of the same list, and the one that fails hardest when it
    drifts: a value the API accepts and the CHECK rejects is a 503 on save."""
    migration = Path(__file__).resolve().parents[1] / "migrations" / "0002_alert_delivery.sql"
    sql = migration.read_text(encoding="utf-8")

    clause = re.search(r"min_severity in \(([^)]*)\)", sql)

    assert clause is not None, f"no min_severity CHECK found in {migration.name}"
    assert tuple(re.findall(r"'([^']*)'", clause.group(1))) == SEVERITY_LEVELS


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------


def test_a_quiet_window_is_stored_with_its_zone(
    client: TestClient, db: FakeSupabaseClient, device_headers: Dict[str, str], device_id: str
) -> None:
    enrolled(client, device_headers)

    response = patch_preferences(
        client, device_headers, quiet_hours_start=22, quiet_hours_end=6, timezone=KOLKATA
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["quiet_hours_start"], body["quiet_hours_end"]) == (22, 6)
    assert body["timezone"] == KOLKATA
    row = db.find("push_tokens", device_id=device_id)
    assert row is not None
    assert (row["quiet_hours_start"], row["quiet_hours_end"]) == (22, 6)


@pytest.mark.parametrize(("start", "end"), [(22, None), (None, 6)], ids=["start-only", "end-only"])
def test_a_half_specified_window_is_refused(
    client: TestClient, device_headers: Dict[str, str], start: Any, end: Any
) -> None:
    """Mirrors the ``push_tokens_quiet_hours_paired`` CHECK, caught early so the
    answer names the missing field instead of arriving as a 503 the client cannot
    interpret."""
    enrolled(client, device_headers)

    response = patch_preferences(
        client, device_headers, quiet_hours_start=start, quiet_hours_end=end, timezone=KOLKATA
    )

    body = assert_problem(response, 422, code="validation_error")
    assert any("together" in str(error["message"]) for error in body["errors"]), body


@pytest.mark.parametrize(
    ("start", "end", "fields"),
    [
        (-1, 6, ["quiet_hours_start"]),
        (22, 24, ["quiet_hours_end"]),
        (24, 25, ["quiet_hours_start", "quiet_hours_end"]),
    ],
    ids=["before-midnight", "past-23", "both"],
)
def test_hours_outside_the_clock_are_refused(
    client: TestClient, device_headers: Dict[str, str], start: int, end: int, fields: List[str]
) -> None:
    """Hours, not timestamps: the stored value is a local wall-clock hour, so 24 is
    not a late evening, it is a row the dispatcher cannot evaluate."""
    enrolled(client, device_headers)

    response = patch_preferences(
        client, device_headers, quiet_hours_start=start, quiet_hours_end=end, timezone=KOLKATA
    )

    assert rejected_fields(response) == fields


def test_a_window_without_a_timezone_is_refused_rather_than_stored_inert(
    client: TestClient, db: FakeSupabaseClient, device_headers: Dict[str, str], device_id: str
) -> None:
    """``in_quiet_hours`` cannot evaluate "22:00 local" without knowing whose local,
    so it delivers. That is the right fallback and the wrong silence: the user has to
    learn it now rather than at 3 a.m."""
    enrolled(client, device_headers)

    response = patch_preferences(client, device_headers, quiet_hours_start=22, quiet_hours_end=6)

    body = assert_problem(response, 422, code="validation_error")
    assert "timezone" in body["detail"]
    row = db.find("push_tokens", device_id=device_id)
    assert row is not None
    assert row.get("quiet_hours_start") is None, "nothing inert was written"


def test_an_unregistered_device_sending_a_window_is_told_to_register(
    client: TestClient, device_headers: Dict[str, str]
) -> None:
    """404 rather than the 422 above. Both are true — there is no row *and* no zone —
    but only one of them is the client's next move."""
    response = patch_preferences(client, device_headers, quiet_hours_start=22, quiet_hours_end=6)

    body = assert_problem(response, 404, code="not_found")
    assert body["detail"] == _NOT_REGISTERED


def test_a_zone_stored_earlier_is_enough_for_a_window_sent_later(
    client: TestClient, device_headers: Dict[str, str]
) -> None:
    """The settings screen sets the zone once, from the device locale, and need not
    resend it with every later change."""
    enrolled(client, device_headers)
    assert patch_preferences(client, device_headers, timezone=KOLKATA).status_code == 200

    response = patch_preferences(client, device_headers, quiet_hours_start=22, quiet_hours_end=6)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["quiet_hours_start"], body["quiet_hours_end"]) == (22, 6)
    assert body["timezone"] == KOLKATA


def test_the_window_is_cleared_by_sending_both_ends_as_null(
    client: TestClient, db: FakeSupabaseClient, device_headers: Dict[str, str], device_id: str
) -> None:
    """Absent means "leave it alone" and null means "remove it", told apart by
    ``model_fields_set`` rather than by a sentinel hour nobody would guess."""
    enrolled(client, device_headers)
    configured = patch_preferences(
        client, device_headers, quiet_hours_start=22, quiet_hours_end=6, timezone=KOLKATA
    )
    assert configured.status_code == 200, configured.text

    response = patch_preferences(
        client, device_headers, quiet_hours_start=None, quiet_hours_end=None
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["quiet_hours_start"], body["quiet_hours_end"]) == (None, None)
    assert body["timezone"] == KOLKATA, "only the window was cleared"
    row = db.find("push_tokens", device_id=device_id)
    assert row is not None
    assert (row["quiet_hours_start"], row["quiet_hours_end"]) == (None, None)


@pytest.mark.parametrize(
    "timezone",
    [
        "Mars/Olympus_Mons",
        "Kolkata/Asia",
        "   ",
        "../../etc/passwd",
        "x" * (MAX_TIMEZONE_LENGTH + 1),
    ],
    ids=["unknown", "reversed", "blank", "traversal", "too-long"],
)
def test_a_timezone_we_cannot_resolve_is_refused(
    client: TestClient, device_headers: Dict[str, str], timezone: str
) -> None:
    """Resolved here so an unknown zone is a 422 the user can act on. Stored
    unchecked it would instead have the dispatcher log an unresolvable zone on every
    pass and deliver *through* the window the user believed they had set."""
    enrolled(client, device_headers)

    response = patch_preferences(client, device_headers, timezone=timezone)

    assert rejected_fields(response) == ["timezone"]
