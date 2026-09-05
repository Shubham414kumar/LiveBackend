"""Expo Push API client.

The one place in this process that can make a phone buzz. Everything here is
shaped by that: the module sends what it was handed, reports precisely what
happened to each message, and decides nothing about *whether* an alert deserves
to exist — that judgement lives in :mod:`app.services.alert_dispatch`.

Three properties are load-bearing.

**A send is never silently partial.** Expo answers a 100-message request with a
``data`` array of 100 tickets, positionally aligned with the request, and any one
of them may have failed while the HTTP status was 200. :func:`send` therefore
returns one :class:`Delivery` per input message, in input order, always — so a
caller cannot mistake "Expo accepted the request" for "the notification was
accepted".

**Permanent failures are separated from transient ones.** A token Expo has
retired will fail identically on every future attempt, so retrying it burns the
attempt budget that a genuinely transient failure needs. Worse, it never gets
cleaned up, and a table full of dead tokens makes every pass slower forever.
:attr:`Delivery.permanent` is the signal; :func:`dead_tokens` extracts the subset
the caller should delete.

**No push token reaches a log record.** ``_REDACT_KEYS`` in
:mod:`app.core.logging` guards structured fields, but Expo also embeds the token
in the *text* of some error messages, so log lines here carry counts and error
codes only, and the messages handed back for storage are scrubbed by
``_scrub_error`` in :mod:`app.db.repositories` before they reach a column.

Deliberately not implemented: **receipts.** A ticket means Expo queued the
notification, not that FCM or APNs delivered it; the final word arrives from
``/push/getReceipts`` at least fifteen minutes later. Fetching them would need a
ticket id column on ``sent_alerts``, a second scheduled pass with its own timing
state, and a third failure mode to reason about — for one incremental benefit,
catching the dead tokens that tickets did not already report. Tickets report the
large majority of them, so this is a considered omission rather than an oversight,
and it is recorded as such in ``docs/PRD-v2.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Sequence

from app.core.config import settings
from app.core.http import UpstreamError, post_json
from app.core.logging import get_logger

logger = get_logger(__name__)

EXPO_SEND_URL = "https://exp.host/--/api/v2/push/send"

#: Provider label for :class:`~app.core.http.UpstreamError` and log records.
PROVIDER = "Expo Push"

#: Expo's documented maximum per request. Sending more is rejected outright, so
#: this is a hard chunk boundary rather than a tuning knob.
MAX_MESSAGES_PER_REQUEST = 100

#: How long Expo, and then FCM/APNs, should keep trying to deliver before giving
#: up. One hour, because a hazard notification that arrives six hours late is not
#: a late alert, it is a false one: the user reads it as current, and acts on
#: information about a situation that has already resolved. Better that it never
#: arrives than that it arrives stale.
PUSH_TTL_SECONDS = 3600

# Notification text is truncated here rather than left to the OS, which elides
# mid-word wherever the layout runs out. A feed title is occasionally very long
# ("M 5.2 - 74 km WNW of ..."), and the first line is the only part a user reads
# from the lock screen.
MAX_TITLE_LENGTH = 120
MAX_BODY_LENGTH = 400

# The Android notification channel every alert is filed under.
#
# This string is one half of a contract with the client: the app creates a channel
# with exactly this id in ``getPushToken()`` (frontend/src/lib/push.ts), at
# ``AndroidImportance.MAX`` with public lockscreen visibility. Naming it here is
# what makes the outgoing message land in *that* channel. Omit the field and
# Android files the notification under expo-notifications' own fallback channel
# instead, at default importance — no heads-up display, no sound while the screen
# is off — which is the wrong behaviour for a hazard alert and, worse, invisible:
# delivery still succeeds and the ticket still comes back ok.
#
# Renaming the channel on the client without changing this constant re-creates
# that failure, so the two must move together: an id the app has not created is
# not an error anywhere in the chain — FCM quietly falls back to the manifest
# channel — so the only symptom is alerts that stop buzzing. iOS ignores the field.
ANDROID_CHANNEL_ID = "default"

# Retried statuses, narrowed from ``_RETRY_STATUS`` in :mod:`app.core.http`.
#
# 429 and 503 mean Expo refused the request: nothing was queued, so repeating it
# is safe and correct. 500, 502 and 504 are ambiguous — Expo may have accepted
# and queued the batch before failing to answer — and repeating one of those is a
# second notification for the same event, which is precisely the 3 a.m. failure
# the whole delivery ledger exists to prevent. An ambiguous send is left to fail;
# the retry sweep re-attempts it once, under a claim that bounds the total.
_SEND_RETRY_STATUSES: FrozenSet[int] = frozenset({429, 503})

#: The two shapes Expo issues. Also enforced at registration by
#: ``PushTokenRegister._expo_token_shape``; checked again here because rows
#: predating that validator are still in the table, and one malformed token in a
#: chunk can cost the whole request rather than just its own ticket.
TOKEN_PREFIXES = ("ExponentPushToken[", "ExpoPushToken[")

#: Expo's code for a token that is no longer a valid recipient — the app was
#: uninstalled, or the OS rotated the token. The only error that means "delete
#: this row", and the caller's cue to do so.
DEVICE_NOT_REGISTERED = "DeviceNotRegistered"

# Errors that will recur identically on every future attempt, so a retry only
# consumes the attempt budget:
#
#   DeviceNotRegistered  the recipient is gone
#   MessageTooBig        the payload exceeds Expo's 4 KiB limit
#   MismatchSenderId     the token belongs to a different FCM sender
#   InvalidCredentials   our own FCM/APNs credentials are wrong
#
# Everything else Expo can return — MessageRateExceeded, ProviderError, and any
# code added after this was written — is treated as transient. Unknown-is-
# transient is the safe default here: a bounded retry of something permanent
# wastes three requests, while treating something transient as permanent loses
# the alert.
_PERMANENT_ERRORS: FrozenSet[str] = frozenset(
    {DEVICE_NOT_REGISTERED, "MessageTooBig", "MismatchSenderId", "InvalidCredentials"}
)

#: Our own code, for a token rejected before it was ever sent. Deliberately not
#: one of Expo's, so a reader of ``sent_alerts.last_error`` can tell a local
#: rejection from an upstream one.
INVALID_PUSH_TOKEN = "InvalidPushToken"


class PushDisabledError(RuntimeError):
    """Raised when :func:`send` is called with ``PUSH_ENABLED`` false.

    A hard guard rather than a quiet no-op, because this is the only function in
    the process with a side effect the user feels directly. The dispatcher checks
    the setting before it starts, so reaching this is a programming error — a new
    caller wired up without the gate — and it should be loud in staging rather
    than a surprise on someone's phone.
    """


def _truncate(value: str, limit: int) -> str:
    """Trim to ``limit`` characters on a word boundary where one is close by."""
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    clipped = text[: limit - 1].rstrip()
    cut = clipped.rfind(" ")
    if cut > limit // 2:
        clipped = clipped[:cut]
    return clipped + "…"


@dataclass(frozen=True)
class PushMessage:
    """One notification, addressed to one device.

    ``data`` is the payload the app receives, and it is the only part of a
    notification that survives being tapped: the mobile handler reads it to open
    the right screen. Keep it small — Expo rejects a message over 4 KiB with
    ``MessageTooBig``, which this module classifies as permanent, so an oversized
    payload silently costs the alert.
    """

    token: str
    title: str
    body: str
    data: Dict[str, Any] = field(default_factory=dict)

    #: ``high`` on purpose. On Android this maps to an FCM high-priority message,
    #: which is what allows delivery while the device is in Doze — a phone on a
    #: bedside table at 3 a.m. is exactly the case that matters. Volume is
    #: controlled by severity thresholds and quiet hours, not by delivering
    #: hazard alerts at low priority and hoping.
    priority: str = "high"
    sound: Optional[str] = "default"

    #: Android channel. See :data:`ANDROID_CHANNEL_ID` for why this is not
    #: optional in practice. ``None`` drops the field, which is only useful for a
    #: test asserting the fallback behaviour.
    channel_id: Optional[str] = ANDROID_CHANNEL_ID

    @property
    def has_valid_token(self) -> bool:
        return self.token.startswith(TOKEN_PREFIXES) and self.token.endswith("]")

    def to_payload(self) -> Dict[str, Any]:
        """The wire form Expo expects for one message."""
        payload: Dict[str, Any] = {
            "to": self.token,
            "title": _truncate(self.title, MAX_TITLE_LENGTH),
            "body": _truncate(self.body, MAX_BODY_LENGTH),
            "priority": self.priority,
            "ttl": PUSH_TTL_SECONDS,
        }
        if self.sound is not None:
            payload["sound"] = self.sound
        if self.channel_id is not None:
            payload["channelId"] = self.channel_id
        if self.data:
            payload["data"] = self.data
        return payload


@dataclass(frozen=True)
class Delivery:
    """What happened to one :class:`PushMessage`.

    Exactly one of these comes back per message handed to :func:`send`, in the
    same order, whether the request succeeded, failed per-ticket, or never left
    the process. The caller settles its ledger from this list, so a missing entry
    would leave a claim stuck at ``pending`` until the retry sweep found it.
    """

    message: PushMessage
    ok: bool
    #: Expo's ``details.error`` code, :data:`INVALID_PUSH_TOKEN`, or a transport
    #: label. ``None`` when :attr:`ok`.
    error_code: Optional[str] = None
    #: Human-readable detail for ``sent_alerts.last_error``. May contain the token
    #: verbatim when Expo put it there, which is why the repository scrubs it on
    #: the way into the column and nothing here logs it.
    detail: Optional[str] = None
    #: True when retrying is pointless. See :data:`_PERMANENT_ERRORS`.
    permanent: bool = False

    @property
    def is_dead_token(self) -> bool:
        return self.error_code == DEVICE_NOT_REGISTERED

    @property
    def summary(self) -> str:
        """A short, token-free description for storage."""
        if self.ok:
            return "sent"
        return self.detail or self.error_code or "send failed"


def dead_tokens(deliveries: Sequence[Delivery]) -> List[str]:
    """The tokens Expo says are no longer valid recipients, de-duplicated.

    Order-preserving so a caller's logs and its delete are in the same order,
    which makes a failed prune reproducible.
    """
    seen: Dict[str, None] = {}
    for delivery in deliveries:
        if delivery.is_dead_token:
            seen.setdefault(delivery.message.token, None)
    return list(seen)


def _auth_headers() -> Optional[Dict[str, str]]:
    """Bearer header when an access token is configured, else ``None``.

    Expo accepts unauthenticated sends for most projects, so this is optional —
    but without it anyone who extracts a push token from the shipped app bundle
    can send notifications that appear to come from us. Configure it.
    """
    secret = settings.expo_access_token
    if secret is None or not secret.get_secret_value():
        return None
    return {"Authorization": f"Bearer {secret.get_secret_value()}"}


def _transport_failure(message: PushMessage, exc: UpstreamError) -> Delivery:
    """The whole request failed, so nothing in this chunk was queued.

    Never permanent, including on 401 and 403. A wrong access token is a
    configuration error an operator can fix within minutes, and the events stay in
    the feed for the whole lookback window — so leaving these claims retryable
    costs a bounded handful of extra requests and buys a chance of the alert still
    landing. Marking them permanent would settle every claim in the pass as failed
    and guarantee those alerts are never delivered at all.
    """
    if exc.timeout:
        code = "Timeout"
    elif exc.status_code is not None:
        code = f"HTTP {exc.status_code}"
    else:
        code = "NetworkError"
    return Delivery(message=message, ok=False, error_code=code, detail=exc.message)


def _ticket_delivery(message: PushMessage, ticket: Any) -> Delivery:
    """Turn one entry of Expo's ``data`` array into a :class:`Delivery`."""
    if not isinstance(ticket, dict):
        return Delivery(
            message=message,
            ok=False,
            error_code="MalformedTicket",
            detail="upstream returned an unreadable ticket",
        )
    if ticket.get("status") == "ok":
        return Delivery(message=message, ok=True)

    details = ticket.get("details")
    raw_code = details.get("error") if isinstance(details, dict) else None
    code = raw_code if isinstance(raw_code, str) and raw_code else "UnknownError"
    detail = ticket.get("message")
    return Delivery(
        message=message,
        ok=False,
        error_code=code,
        detail=detail if isinstance(detail, str) else None,
        permanent=code in _PERMANENT_ERRORS,
    )


def _chunk_failure(chunk: Sequence[PushMessage], code: str, detail: str) -> List[Delivery]:
    return [
        Delivery(message=message, ok=False, error_code=code, detail=detail) for message in chunk
    ]


def _deliveries_from_response(chunk: Sequence[PushMessage], payload: Any) -> List[Delivery]:
    """Align Expo's response with the chunk that produced it.

    The alignment is positional — Expo documents ``data[i]`` as the ticket for
    request item ``i``, and there is no other correlator in the response. A length
    mismatch therefore means the response cannot be attributed at all, and the
    only safe reading is that this chunk's outcome is unknown: guessing would
    record one device's failure against another's claim, so every message is
    failed transiently instead and the retry sweep settles it.
    """
    if not isinstance(payload, dict):
        return _chunk_failure(chunk, "MalformedResponse", "upstream returned a non-object body")

    errors = payload.get("errors")
    if errors:
        # Request-level rejection with a 200, which Expo does for some malformed
        # bodies. The codes are ours to fix, not the device's, so keep them out of
        # the permanent set for the reason in :func:`_transport_failure`.
        first = errors[0] if isinstance(errors, list) and errors else {}
        code = first.get("code") if isinstance(first, dict) else None
        detail = first.get("message") if isinstance(first, dict) else None
        logger.error(
            "Expo rejected a push request",
            extra={"provider": PROVIDER, "code": code, "messages": len(chunk)},
        )
        return _chunk_failure(
            chunk,
            code if isinstance(code, str) and code else "RequestRejected",
            detail if isinstance(detail, str) else "upstream rejected the request",
        )

    tickets = payload.get("data")
    if isinstance(tickets, dict):
        # Expo mirrors the request shape; we always send a list, but a proxy that
        # unwraps a single-element array is not worth losing a batch over.
        tickets = [tickets]
    if not isinstance(tickets, list):
        return _chunk_failure(chunk, "MalformedResponse", "upstream response had no ticket list")

    if len(tickets) != len(chunk):
        logger.error(
            "Expo returned a ticket count that does not match the request",
            extra={"provider": PROVIDER, "tickets": len(tickets), "messages": len(chunk)},
        )
        return _chunk_failure(chunk, "TicketCountMismatch", "upstream tickets could not be paired")

    return [
        _ticket_delivery(message, ticket) for message, ticket in zip(chunk, tickets, strict=True)
    ]


async def _send_chunk(chunk: Sequence[PushMessage]) -> List[Delivery]:
    """One HTTP request for at most :data:`MAX_MESSAGES_PER_REQUEST` messages."""
    try:
        response = await post_json(
            EXPO_SEND_URL,
            provider=PROVIDER,
            json_body=[message.to_payload() for message in chunk],
            headers=_auth_headers(),
            retry_statuses=_SEND_RETRY_STATUSES,
        )
    except UpstreamError as exc:
        logger.warning(
            "Expo push request failed",
            extra={
                "provider": PROVIDER,
                "messages": len(chunk),
                "status": exc.status_code,
                "timeout": exc.timeout,
            },
        )
        return [_transport_failure(message, exc) for message in chunk]
    return _deliveries_from_response(chunk, response)


async def send(messages: Sequence[PushMessage]) -> List[Delivery]:
    """Deliver ``messages``, returning one :class:`Delivery` each, in input order.

    Chunked at Expo's per-request limit and sent sequentially. Sequential rather
    than concurrent because a dispatch pass is not latency-sensitive — it runs on
    a fifteen-minute timer — while a burst of parallel requests to a provider that
    rate-limits by account is a good way to earn a 429 for the pass that matters.

    Raises:
        PushDisabledError: if ``PUSH_ENABLED`` is false.
    """
    if not settings.push_enabled:
        raise PushDisabledError(
            "PUSH_ENABLED is false; refusing to send. This is a wiring bug: "
            "the caller should check the setting before building messages."
        )
    if not messages:
        return []

    results: List[Optional[Delivery]] = [None] * len(messages)
    sendable: List[int] = []
    for index, message in enumerate(messages):
        if message.has_valid_token:
            sendable.append(index)
        else:
            # Rejected locally so it cannot cost the rest of its chunk. Permanent:
            # a stored token that is not Expo-shaped will never become one, and the
            # caller's dead-token prune is keyed on DeviceNotRegistered, so this row
            # is left for `prune_old_data` to reap by age.
            results[index] = Delivery(
                message=message,
                ok=False,
                error_code=INVALID_PUSH_TOKEN,
                detail="stored token is not an Expo push token",
                permanent=True,
            )

    for start in range(0, len(sendable), MAX_MESSAGES_PER_REQUEST):
        indices = sendable[start : start + MAX_MESSAGES_PER_REQUEST]
        chunk = [messages[index] for index in indices]
        for index, delivery in zip(indices, await _send_chunk(chunk), strict=True):
            results[index] = delivery

    # Every slot was assigned above — the comprehension is what tells the type
    # checker so, since `_send_chunk` returning short would be a bug here rather
    # than something to paper over at runtime.
    deliveries = [delivery for delivery in results if delivery is not None]
    failed = [delivery for delivery in deliveries if not delivery.ok]
    logger.info(
        "Push send complete",
        extra={
            "provider": PROVIDER,
            "requested": len(messages),
            "sent": len(deliveries) - len(failed),
            "failed": len(failed),
            "permanent": sum(1 for delivery in failed if delivery.permanent),
            "dead_tokens": sum(1 for delivery in failed if delivery.is_dead_token),
        },
    )
    return deliveries


__all__ = [
    "DEVICE_NOT_REGISTERED",
    "EXPO_SEND_URL",
    "INVALID_PUSH_TOKEN",
    "MAX_BODY_LENGTH",
    "MAX_MESSAGES_PER_REQUEST",
    "MAX_TITLE_LENGTH",
    "PROVIDER",
    "PUSH_TTL_SECONDS",
    "TOKEN_PREFIXES",
    "Delivery",
    "PushDisabledError",
    "PushMessage",
    "dead_tokens",
    "send",
]
