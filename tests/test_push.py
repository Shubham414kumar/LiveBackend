"""The Expo push client — the one module in this process that makes a phone buzz.

Three properties are load-bearing, and each has tests here rather than a comment
in :mod:`app.services.push` asserting it:

* **One delivery per message, in input order, always.** The caller settles its
  ledger positionally from this list, so a short or reordered return does not
  degrade delivery — it records one device's failure against another's claim.
* **Permanent is separated from transient.** Retrying a retired token burns the
  attempt budget a recoverable failure needs; treating a recoverable failure as
  permanent loses the alert outright.
* **No push token reaches a log record.** ``_REDACT_KEYS`` covers structured
  fields, but Expo embeds the token in the *text* of some error messages, so the
  guarantee has to be checked against what the module actually logs.

Nothing here stubs ``push.send``. The ``upstream`` fixture patches the transport,
so chunking, the narrowed retry set, ticket alignment and status mapping all run
for real; the only thing faked is the far end of the socket.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx
import pytest
from pydantic import SecretStr

from app.services import push
from tests.fakes import UpstreamRouter

#: The send endpoint, as a regex for `UpstreamRouter`. Anything this module calls
#: that is not this URL fails the test in the fixture's teardown.
EXPO = r"exp\.host/--/api/v2/push/send"


@pytest.fixture
def push_on(settings_override: Any) -> None:
    """``PUSH_ENABLED=true``.

    Off in ``_TEST_ENVIRONMENT`` and turned on only here, so a test that forgets
    this fixture raises :class:`push.PushDisabledError` instead of quietly
    exercising a code path no deployment reaches with delivery disabled.
    """
    settings_override(push_enabled=True)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def message(index: int = 0, *, token: Optional[str] = None) -> push.PushMessage:
    """One notification, distinguishable from its neighbours.

    The index goes into the token, the title and the payload so an ordering bug
    shows up as a mismatched pair rather than as two identical objects comparing
    equal by accident.
    """
    return push.PushMessage(
        token=token if token is not None else f"ExponentPushToken[device-{index}]",
        title=f"Flood warning {index}",
        body=f"Rising water reported near you ({index}).",
        data={"eventId": f"event-{index}"},
    )


def ok_tickets(count: int) -> Dict[str, Any]:
    """Expo's success shape: a ``data`` array positionally matching the request."""
    return {"data": [{"status": "ok", "id": f"ticket-{index}"} for index in range(count)]}


def error_ticket(code: str, *, text: str = "the message could not be delivered") -> Dict[str, Any]:
    return {"status": "error", "message": text, "details": {"error": code}}


def sent_bodies(upstream: UpstreamRouter) -> List[List[Dict[str, Any]]]:
    """The decoded JSON body of every send request, in the order they were made."""
    return [
        json.loads(request.content.decode())
        for request in upstream.requests
        if "push/send" in str(request.url)
    ]


def log_surface(caplog: pytest.LogCaptureFixture) -> str:
    """Everything a log sink could emit, rendered text and structured extras alike.

    ``caplog.text`` alone is not enough: our formatters serialise the ``extra``
    mapping, so a token passed as a structured field would never appear in the
    rendered message and a check against it would pass while leaking.
    """
    parts: List[str] = [caplog.text]
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.extend(f"{key}={value!r}" for key, value in record.__dict__.items())
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# The delivery gate
# ---------------------------------------------------------------------------
async def test_send_refuses_when_delivery_is_disabled(upstream: UpstreamRouter) -> None:
    """A wiring bug must be loud here, not silent on someone's phone.

    The dispatcher checks ``push_enabled`` before it builds anything, so a call
    reaching this guard means a new caller was wired up without the gate. A quiet
    no-op would make that indistinguishable from a successful pass.
    """
    with pytest.raises(push.PushDisabledError):
        await push.send([message()])

    assert upstream.requests == []


async def test_an_empty_batch_makes_no_request(push_on: None, upstream: UpstreamRouter) -> None:
    """Expo rejects an empty array, and there is nothing to report anyway."""
    assert await push.send([]) == []
    assert upstream.requests == []


# ---------------------------------------------------------------------------
# One delivery per message, in input order
# ---------------------------------------------------------------------------
async def test_every_message_gets_exactly_one_delivery_in_order(
    push_on: None, upstream: UpstreamRouter
) -> None:
    """Including the failures, which is the whole point.

    Expo answers a three-message request with a 200 and three tickets, one of
    which failed. A caller that read the HTTP status would record all three as
    delivered; a caller that dropped the failure would leave its claim pending
    until the retry sweep found it.
    """
    messages = [message(0), message(1), message(2)]
    upstream.json(
        EXPO,
        {
            "data": [
                {"status": "ok", "id": "ticket-0"},
                error_ticket(push.DEVICE_NOT_REGISTERED),
                {"status": "ok", "id": "ticket-2"},
            ]
        },
    )

    deliveries = await push.send(messages)

    assert len(deliveries) == 3
    assert [delivery.message for delivery in deliveries] == messages
    assert [delivery.ok for delivery in deliveries] == [True, False, True]


async def test_a_retired_token_is_permanent_and_extracted_for_pruning(
    push_on: None, upstream: UpstreamRouter
) -> None:
    """``DeviceNotRegistered`` is the only error that means "delete this row".

    De-duplicated and order-preserving, so a prune that fails halfway is
    reproducible from the log line that preceded it.
    """
    dead = "ExponentPushToken[gone]"
    messages = [message(token=dead), message(1), message(2, token=dead)]
    upstream.json(
        EXPO,
        {
            "data": [
                error_ticket(push.DEVICE_NOT_REGISTERED),
                {"status": "ok"},
                error_ticket(push.DEVICE_NOT_REGISTERED),
            ]
        },
    )

    deliveries = await push.send(messages)

    assert deliveries[0].permanent is True
    assert deliveries[0].is_dead_token is True
    assert deliveries[1].is_dead_token is False
    assert push.dead_tokens(deliveries) == [dead]


async def test_a_malformed_token_is_rejected_before_it_costs_its_chunk(
    push_on: None, upstream: UpstreamRouter
) -> None:
    """One bad row must not fail the other ninety-nine messages beside it.

    Rows predating ``PushTokenRegister._expo_token_shape`` are still in the table,
    and Expo rejects a whole request over a single malformed ``to`` field. The
    local rejection is deliberately *not* ``DeviceNotRegistered``: the prune is
    keyed on that code, and deleting a row we never actually asked Expo about
    would silence a device on the strength of our own parsing.
    """
    messages = [message(token="not-an-expo-token"), message(1)]
    upstream.json(EXPO, ok_tickets(1))

    deliveries = await push.send(messages)

    assert sent_bodies(upstream) == [[messages[1].to_payload()]]
    assert deliveries[0].error_code == push.INVALID_PUSH_TOKEN
    assert deliveries[0].permanent is True
    assert deliveries[1].ok is True
    assert push.dead_tokens(deliveries) == []


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
async def test_chunks_at_expos_hard_limit_and_keeps_the_pairing(
    push_on: None, upstream: UpstreamRouter
) -> None:
    """250 messages are three requests of 100, 100 and 50 — not one of 250.

    The responder answers with as many tickets as it was sent, which is what makes
    the last assertion meaningful: if a chunk boundary were off by one, the ticket
    counts would still match per request while the deliveries came back paired to
    the wrong messages.
    """
    messages = [message(index) for index in range(250)]

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_tickets(len(json.loads(request.content.decode()))))

    upstream.add(EXPO, respond)

    deliveries = await push.send(messages)

    assert [len(body) for body in sent_bodies(upstream)] == [100, 100, 50]
    assert len(deliveries) == 250
    assert all(delivery.ok for delivery in deliveries)
    assert [delivery.message for delivery in deliveries] == messages


async def test_the_batch_limit_is_a_boundary_not_a_target(
    push_on: None, upstream: UpstreamRouter
) -> None:
    """Exactly 100 is one request; 101 is two. The off-by-one costs a whole batch."""
    upstream.add(
        EXPO,
        lambda request: httpx.Response(
            200, json=ok_tickets(len(json.loads(request.content.decode())))
        ),
    )

    await push.send([message(index) for index in range(push.MAX_MESSAGES_PER_REQUEST)])
    assert upstream.call_count(EXPO) == 1

    await push.send([message(index) for index in range(push.MAX_MESSAGES_PER_REQUEST + 1)])
    assert upstream.call_count(EXPO) == 3
    assert [len(body) for body in sent_bodies(upstream)] == [100, 100, 1]


# ---------------------------------------------------------------------------
# Reading the response
# ---------------------------------------------------------------------------
async def test_a_ticket_count_mismatch_fails_the_chunk_transiently(
    push_on: None, upstream: UpstreamRouter
) -> None:
    """Positional alignment is the only correlator Expo gives us.

    Two messages answered with one ticket cannot be attributed at all. Guessing
    would file one device's outcome against the other's claim, so the whole chunk
    is failed — and failed *transiently*, because the request may well have been
    queued and the retry sweep is bounded.
    """
    upstream.json(EXPO, ok_tickets(1))

    deliveries = await push.send([message(0), message(1)])

    assert [delivery.error_code for delivery in deliveries] == ["TicketCountMismatch"] * 2
    assert not any(delivery.permanent for delivery in deliveries)


async def test_a_request_level_rejection_arrives_with_a_200(
    push_on: None, upstream: UpstreamRouter
) -> None:
    """Expo reports some malformed requests as ``errors`` under a 200.

    The fault is ours to fix, not the device's, so nothing is marked permanent:
    the events stay in the feed for the whole lookback window and an operator can
    deploy a fix inside it.
    """
    upstream.json(
        EXPO,
        {"errors": [{"code": "PUSH_TOO_MANY_EXPERIENCE_IDS", "message": "too many projects"}]},
    )

    deliveries = await push.send([message(0), message(1)])

    assert [delivery.error_code for delivery in deliveries] == ["PUSH_TOO_MANY_EXPERIENCE_IDS"] * 2
    assert [delivery.detail for delivery in deliveries] == ["too many projects"] * 2
    assert not any(delivery.permanent for delivery in deliveries)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ([], "MalformedResponse"),
        ({"data": "nope"}, "MalformedResponse"),
        ({}, "MalformedResponse"),
    ],
    ids=["array-body", "ticket-list-is-a-string", "no-ticket-list"],
)
async def test_an_unreadable_body_fails_the_chunk(
    push_on: None, upstream: UpstreamRouter, payload: Any, expected: str
) -> None:
    """A 200 with a body we cannot parse is an unknown outcome, not a success."""
    upstream.json(EXPO, payload)

    deliveries = await push.send([message()])

    assert deliveries[0].error_code == expected
    assert deliveries[0].permanent is False


async def test_a_single_ticket_object_is_accepted(push_on: None, upstream: UpstreamRouter) -> None:
    """A proxy that unwraps a one-element array is not worth losing a batch over."""
    upstream.json(EXPO, {"data": {"status": "ok", "id": "ticket-0"}})

    deliveries = await push.send([message()])

    assert deliveries[0].ok is True


async def test_one_unreadable_ticket_does_not_take_down_its_neighbour(
    push_on: None, upstream: UpstreamRouter
) -> None:
    upstream.json(EXPO, {"data": ["not-a-ticket", {"status": "ok"}]})

    deliveries = await push.send([message(0), message(1)])

    assert deliveries[0].error_code == "MalformedTicket"
    assert deliveries[0].permanent is False
    assert deliveries[1].ok is True


@pytest.mark.parametrize(
    ("code", "permanent"),
    [
        (push.DEVICE_NOT_REGISTERED, True),
        ("MessageTooBig", True),
        ("MismatchSenderId", True),
        ("InvalidCredentials", True),
        ("MessageRateExceeded", False),
        ("ProviderError", False),
        # A code Expo has not shipped yet. Unknown-is-transient: a bounded retry of
        # something permanent wastes three requests, while calling something
        # transient permanent loses the alert for good.
        ("SomeCodeAddedAfterThisWasWritten", False),
    ],
)
async def test_permanence_is_classified_per_error_code(
    push_on: None, upstream: UpstreamRouter, code: str, permanent: bool
) -> None:
    upstream.json(EXPO, {"data": [error_ticket(code)]})

    deliveries = await push.send([message()])

    assert deliveries[0].ok is False
    assert deliveries[0].error_code == code
    assert deliveries[0].permanent is permanent


async def test_an_error_ticket_without_details_still_has_a_code(
    push_on: None, upstream: UpstreamRouter
) -> None:
    """``summary`` is what reaches ``sent_alerts.last_error``; it must never be empty."""
    upstream.json(EXPO, {"data": [{"status": "error", "message": "something went wrong"}]})

    deliveries = await push.send([message()])

    assert deliveries[0].error_code == "UnknownError"
    assert deliveries[0].summary == "something went wrong"


# ---------------------------------------------------------------------------
# Transport failures
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("timeout", "Timeout"),
        ("network", "NetworkError"),
        ("401", "HTTP 401"),
        ("500", "HTTP 500"),
    ],
)
async def test_a_transport_failure_is_never_permanent(
    push_on: None, upstream: UpstreamRouter, failure: str, expected: str
) -> None:
    """Including 401 and 403, which look like configuration mistakes — and are.

    An operator can fix a wrong access token in minutes, and the events stay in
    the feed for the whole lookback window. Marking these permanent would settle
    every claim in the pass as failed and guarantee those alerts never land.
    """
    if failure == "timeout":
        upstream.timeout(EXPO)
    elif failure == "network":
        upstream.network_error(EXPO)
    else:
        upstream.status(EXPO, int(failure))

    deliveries = await push.send([message(0), message(1)])

    assert len(deliveries) == 2
    assert [delivery.error_code for delivery in deliveries] == [expected] * 2
    assert not any(delivery.permanent for delivery in deliveries)
    assert not any(delivery.ok for delivery in deliveries)


@pytest.mark.parametrize(
    ("status", "attempts"),
    [(429, 2), (503, 2), (500, 1), (502, 1), (504, 1)],
)
async def test_only_a_refused_request_is_retried(
    push_on: None,
    upstream: UpstreamRouter,
    settings_override: Any,
    status: int,
    attempts: int,
) -> None:
    """Retrying a send is not free — it is a second notification.

    429 and 503 mean Expo refused the request outright, so nothing was queued and
    repeating it is safe. 500, 502 and 504 are ambiguous: Expo may have accepted
    and queued the batch before failing to answer, and a repeat is then a duplicate
    buzz for the same event. Those are left to fail and re-attempted once by the
    retry sweep, under a claim that bounds the total.

    ``Retry-After: 0.01`` keeps the honoured delay at ten milliseconds instead of a
    jittered quarter second.
    """
    settings_override(http_max_retries=1)
    upstream.json(EXPO, {"error": "refused"}, status=status, headers={"Retry-After": "0.01"})

    deliveries = await push.send([message()])

    assert upstream.call_count(EXPO) == attempts
    assert deliveries[0].error_code == f"HTTP {status}"
    assert deliveries[0].permanent is False


# ---------------------------------------------------------------------------
# The wire format
# ---------------------------------------------------------------------------
async def test_the_access_token_is_sent_as_a_bearer_header(
    push_on: None, upstream: UpstreamRouter, settings_override: Any
) -> None:
    """Without it, Expo accepts unauthenticated sends.

    Which means anyone who extracts a push token from the shipped app bundle can
    send notifications that appear to come from us — a hazard alert with our name
    on it. Optional in Expo's API; not optional here.
    """
    settings_override(expo_access_token=SecretStr("expo-access-token-value"))
    upstream.json(EXPO, ok_tickets(1))

    await push.send([message()])

    request = upstream.last_request(EXPO)
    assert request is not None
    assert request.headers["Authorization"] == "Bearer expo-access-token-value"


async def test_no_authorization_header_when_no_token_is_configured(
    push_on: None, upstream: UpstreamRouter, settings_override: Any
) -> None:
    # Set explicitly rather than relied on: `EXPO_ACCESS_TOKEN` is not one of the
    # variables `_TEST_ENVIRONMENT` pins, so a developer with one exported would
    # otherwise see this pass for the wrong reason on CI and fail locally.
    settings_override(expo_access_token=None)
    upstream.json(EXPO, ok_tickets(1))

    await push.send([message()])

    request = upstream.last_request(EXPO)
    assert request is not None
    assert "authorization" not in request.headers


async def test_the_payload_is_exactly_what_expo_expects(
    push_on: None, upstream: UpstreamRouter
) -> None:
    """Asserted as a whole dict, not field by field.

    An extra key is as much of a problem as a missing one: Expo rejects unknown
    fields on some paths, and this is the payload a phone acts on.
    """
    upstream.json(EXPO, ok_tickets(1))

    await push.send([message(7)])

    assert sent_bodies(upstream) == [
        [
            {
                "to": "ExponentPushToken[device-7]",
                "title": "Flood warning 7",
                "body": "Rising water reported near you (7).",
                # high, so Android delivers while the device is in Doze — a phone on
                # a bedside table at 3 a.m. is the case that matters.
                "priority": "high",
                "sound": "default",
                "channelId": push.ANDROID_CHANNEL_ID,
                "ttl": push.PUSH_TTL_SECONDS,
                "data": {"eventId": "event-7"},
            }
        ]
    ]


def test_the_channel_id_matches_the_one_the_app_creates() -> None:
    """Pins the string, because nothing else in the chain will complain about it.

    The client creates this channel at ``AndroidImportance.MAX`` in
    ``getPushToken()`` (frontend/app/_layout.tsx). Send an id the app never
    created and FCM falls back to the manifest channel at default importance:
    delivery still succeeds, the ticket still comes back ok, and the only symptom
    is that hazard alerts stop producing a heads-up notification. Renaming the
    channel on either side without the other is therefore a silent regression,
    and this assertion is the tripwire.
    """
    assert push.ANDROID_CHANNEL_ID == "default"
    assert push.PushMessage(token="t", title="t", body="b").channel_id == "default"


async def test_an_explicit_none_channel_drops_the_field(
    push_on: None, upstream: UpstreamRouter
) -> None:
    """The escape hatch exists for tests and for a future per-category channel;
    it is never what production should send."""
    upstream.json(EXPO, ok_tickets(1))

    await push.send(
        [
            push.PushMessage(
                token="ExponentPushToken[device-0]",
                title="No channel",
                body="Falls back to the manifest channel.",
                channel_id=None,
            )
        ]
    )

    assert "channelId" not in sent_bodies(upstream)[0][0]


async def test_long_text_is_truncated_before_it_reaches_the_lock_screen(
    push_on: None, upstream: UpstreamRouter
) -> None:
    """Truncated here rather than left to the OS, which elides mid-word.

    Feed titles are occasionally very long, and the first line is the only part a
    user reads from a lock screen. The cut lands on a word boundary and keeps the
    leading text intact — the end of a hazard title is never the important part.
    """
    long_title = " ".join(["Landslide"] * 40)
    long_body = " ".join(["Debris on the carriageway."] * 40)
    upstream.json(EXPO, ok_tickets(1))

    await push.send(
        [push.PushMessage(token="ExponentPushToken[device-0]", title=long_title, body=long_body)]
    )

    payload = sent_bodies(upstream)[0][0]
    assert len(payload["title"]) <= push.MAX_TITLE_LENGTH
    assert len(payload["body"]) <= push.MAX_BODY_LENGTH
    for field in ("title", "body"):
        text = payload[field]
        assert text.endswith("…")
        kept = text[:-1]
        assert not kept.endswith(" ")
        assert long_title.startswith(kept) if field == "title" else long_body.startswith(kept)


async def test_whitespace_is_collapsed_so_a_feed_title_reads_as_one_line(
    push_on: None, upstream: UpstreamRouter
) -> None:
    """USGS titles arrive with newlines in them; a notification has no second line."""
    upstream.json(EXPO, ok_tickets(1))

    await push.send(
        [
            push.PushMessage(
                token="ExponentPushToken[device-0]",
                title="M 5.2  -\n 74 km WNW of\tSomewhere",
                body="Line one.\n\nLine two.",
            )
        ]
    )

    payload = sent_bodies(upstream)[0][0]
    assert payload["title"] == "M 5.2 - 74 km WNW of Somewhere"
    assert payload["body"] == "Line one. Line two."


async def test_an_empty_payload_and_a_silent_message_omit_their_keys(
    push_on: None, upstream: UpstreamRouter
) -> None:
    """Expo treats a missing ``sound`` differently from ``null``, and ``data`` is
    what the tapped notification carries — an empty object is noise on the wire."""
    upstream.json(EXPO, ok_tickets(1))

    await push.send(
        [
            push.PushMessage(
                token="ExponentPushToken[device-0]",
                title="Quiet",
                body="No payload.",
                sound=None,
            )
        ]
    )

    payload = sent_bodies(upstream)[0][0]
    assert "sound" not in payload
    assert "data" not in payload


# ---------------------------------------------------------------------------
# The token must never reach a log sink
# ---------------------------------------------------------------------------
async def test_no_push_token_appears_in_any_log_record(
    push_on: None, upstream: UpstreamRouter, caplog: pytest.LogCaptureFixture
) -> None:
    """Expo puts the token in the *text* of this error, so redaction cannot catch it.

    ``_REDACT_KEYS`` guards structured fields by name; a token embedded in a
    message string sails past it. The rule this test defends is that log records
    from this module carry counts and error codes only.

    ``detail`` is asserted to *contain* the token on purpose: the value has to
    survive as far as the repository, which scrubs it on the way into
    ``sent_alerts.last_error``. That is where the scrubbing belongs — a delivery
    that lost the detail here could not report the failure at all.
    """
    token = "ExponentPushToken[secret-device-token]"
    upstream.json(
        EXPO,
        {
            "data": [
                error_ticket(
                    push.DEVICE_NOT_REGISTERED,
                    text=f'"{token}" is not a registered push notification recipient',
                )
            ]
        },
    )

    with caplog.at_level(logging.DEBUG):
        deliveries = await push.send([message(token=token)])

    assert any(record.getMessage() == "Push send complete" for record in caplog.records)
    assert token not in log_surface(caplog)
    assert "secret-device-token" not in log_surface(caplog)
    assert deliveries[0].detail is not None
    assert token in deliveries[0].detail
