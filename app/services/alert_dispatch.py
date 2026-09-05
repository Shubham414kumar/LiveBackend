"""The push dispatch pass.

Answers "what is happening near this device's saved locations" on a schedule
instead of waiting for the app to be opened, and turns the answer into
notifications. Matching is not reimplemented here — :mod:`app.services.alert_match`
owns it, shared with ``GET /api/favorites/alerts``, so a notification and the
screen it opens can never disagree.

Everything below is arranged around one requirement: **do not wake someone at
3 a.m. four times for one earthquake.** Four independent mechanisms enforce it,
and they are applied in this order for reasons that matter:

1. **Severity threshold**, per device. A user near an active fault who is told
   about every magnitude 4.5 mutes the app inside a month, and then hears nothing
   about the one that mattered.
2. **One notification per event, not per favourite.** Home and Office 5 km apart
   match the same quake; that is one thing happening, so it is one notification.
3. **Quiet hours**, checked *before* the claim. A suppressed alert must remain
   unclaimed so it can still be delivered when the window ends — claiming it
   first would consume the event silently and the user would never hear about it.
   Severity at or above ``PUSH_QUIET_HOURS_BREAKTHROUGH`` ignores the window,
   because a tsunami at 3 a.m. is exactly what the user installed this for.
4. **A volume cap**, counting what the device was already sent in the last hour,
   not just this pass. Without the lookback, a three-per-pass cap on a
   fifteen-minute timer is twelve notifications an hour.

Then, and only then, :meth:`SentAlertsRepository.claim` runs — an
``ON CONFLICT DO NOTHING`` whose returned rows are the sends this pass owns.
Claim before sending, never after: a crash between the two costs at most one
alert, which the retry sweep recovers, while the reverse re-sends forever.

A pass is a pure function of the feed plus the database, holds no state between
runs, and is safe to run concurrently with another copy of itself — the claim is
what makes that true. It is therefore also safe to run by hand against
production while the worker is running, which is how it should be tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.repositories import (
    DeliveryFailure,
    favorites_repo,
    push_tokens_repo,
    sent_alerts_repo,
)
from app.services import disasters, push
from app.services.alert_match import (
    EventMatch,
    alert_radius_km,
    favorite_coords,
    matches_for_point,
    severity_rank,
)

logger = get_logger(__name__)

#: Ceiling on pages walked in one pass, so a pathological table cannot turn a
#: fifteen-minute timer into an hour-long pass that overlaps the next one.
#: 200 pages x 100 devices is 20 000 devices; well past that, the design needs a
#: queue rather than a larger number here.
MAX_PAGES_PER_PASS = 200

#: Window the volume cap looks back over. One hour, matched to the cap's intent:
#: "how many times have I been interrupted recently", not "how many times in this
#: particular pass".
VOLUME_WINDOW_HOURS = 1


@dataclass
class DispatchStats:
    """Counters for one pass. Mutable on purpose — this is an accumulator."""

    devices: int = 0
    #: Distinct (device, event) pairs that survived every filter.
    candidates: int = 0
    #: Pairs this pass won the claim for; the rest were already delivered.
    claimed: int = 0
    sent: int = 0
    failed: int = 0
    suppressed_quiet: int = 0
    suppressed_cap: int = 0
    dead_tokens_pruned: int = 0
    events: int = 0
    #: Providers that failed during the feed fetch. Surfaced because an empty feed
    #: caused by four dead upstreams looks exactly like a quiet day from here.
    sources_failed: List[str] = field(default_factory=list)
    #: Set when the pass aborted early. A pass that sent nothing because it broke
    #: must not be indistinguishable in the logs from one that had nothing to send.
    aborted: Optional[str] = None

    def as_log_fields(self) -> Dict[str, Any]:
        return {
            "devices": self.devices,
            "events": self.events,
            "candidates": self.candidates,
            "claimed": self.claimed,
            "sent": self.sent,
            "failed": self.failed,
            "suppressed_quiet": self.suppressed_quiet,
            "suppressed_cap": self.suppressed_cap,
            "dead_tokens_pruned": self.dead_tokens_pruned,
            "sources_failed": ",".join(self.sources_failed) or None,
            "aborted": self.aborted,
        }


def in_quiet_hours(token_row: Dict[str, Any], now: datetime) -> bool:
    """Is ``now`` inside this device's quiet window?

    False whenever the answer is not knowable. A missing timezone, an IANA name
    the runtime cannot resolve (the container image has no ``tzdata``), a
    half-specified window: every one of those means deliver. Guessing UTC would
    silence a user in IST for the wrong nine hours of their day, and suppression
    the user cannot predict is worse than none at all.

    ``start == end`` is a zero-width window, and it is read as "no quiet hours"
    rather than "quiet all day". Both readings are defensible from the value
    alone; only one of them can silence a device completely by accident.
    """
    start = token_row.get("quiet_hours_start")
    end = token_row.get("quiet_hours_end")
    zone_name = token_row.get("timezone")
    if not isinstance(start, int) or not isinstance(end, int) or not zone_name:
        return False
    if start == end:
        return False

    try:
        local_hour = now.astimezone(ZoneInfo(str(zone_name))).hour
    except (ZoneInfoNotFoundError, KeyError, TypeError, ValueError):
        logger.warning(
            "Unresolvable timezone on a push token; delivering rather than guessing",
            extra={"timezone": str(zone_name)[:64]},
        )
        return False

    if start < end:
        return start <= local_hour < end
    # Wraps midnight: start 22, end 7 is 22:00-07:00, and it is the common case.
    return local_hour >= start or local_hour < end


def breaks_through_quiet_hours(event: Dict[str, Any]) -> bool:
    """Is this event severe enough to ignore a quiet window?

    An unrecognised severity does *not* break through. This is the one place in
    the alerting path where unknown-means-no, and deliberately so: everywhere else
    an unknown severity errs towards delivering, but "wake the user at 3 a.m." is
    not a default anything should fall into by accident.
    """
    threshold = severity_rank(settings.push_quiet_hours_breakthrough)
    rank = severity_rank(event.get("severity"))
    return rank >= threshold > 0


@dataclass(frozen=True)
class Candidate:
    """One event worth telling one device about, and the favourite that caught it."""

    event_id: str
    match: EventMatch
    favorite_name: str


def candidates_for_device(
    favorites: Sequence[Dict[str, Any]],
    events: Sequence[Dict[str, Any]],
    *,
    min_severity: Optional[str],
) -> List[Candidate]:
    """Deduplicated, ranked candidates for one device.

    Deduplicated **by event**, not by favourite. Home and Office five kilometres
    apart match the same earthquake, and that is one thing happening in the world,
    so it is one notification — attributed to whichever of them is closer, since
    "12 km from Home" is more useful than "17 km from Office" for the same quake.
    The dedupe is also what makes ``sent_alerts``' composite key sufficient: two
    rows for one event and one device cannot exist, so they cannot race.

    Ranked most severe first, nearest as the tie-break, because the volume cap
    truncates this list and the alerts that survive should be the ones that matter.

    An event with no id is dropped. It cannot be claimed — ``sent_alerts`` requires
    a non-blank ``event_id`` — and an unclaimable alert would be re-sent on every
    pass for as long as it stayed in the feed.
    """
    best: Dict[str, Candidate] = {}
    for favorite in favorites:
        coords = favorite_coords(favorite)
        if coords is None:
            continue
        name = str(favorite.get("name") or "your saved location").strip()
        matches = matches_for_point(
            coords[0],
            coords[1],
            alert_radius_km(favorite),
            events,
            min_severity=min_severity,
        )
        for match in matches:
            event_id = match.event_id
            if not event_id:
                continue
            existing = best.get(event_id)
            if existing is None or match.distance_km < existing.match.distance_km:
                best[event_id] = Candidate(
                    event_id=event_id,
                    match=match,
                    favorite_name=name or "your saved location",
                )

    return sorted(
        best.values(),
        key=lambda candidate: (
            severity_rank(candidate.match.event.get("severity")),
            -candidate.match.distance_km,
        ),
        reverse=True,
    )


def _distance_phrase(km: float) -> str:
    if km < 1:
        return "under 1 km"
    return f"{round(km)} km"


def build_message(token: str, candidate: Candidate) -> push.PushMessage:
    """Compose the notification for one candidate.

    The title is the event as the feed described it, and the body answers the only
    question a notification has to answer on a lock screen: how bad, how far, from
    where. Neither is templated with an emoji or an exclamation mark — this is the
    same text a user might read while deciding whether to leave a building.

    ``data`` is the tap payload. ``eventId`` is the contract: the app looks the
    event up in the feed it already fetches rather than trusting notification
    contents that may be hours old by the time the notification is opened.
    """
    event = candidate.match.event
    severity = candidate.match.severity
    body = (
        f"{severity} · {_distance_phrase(candidate.match.distance_km)} "
        f"from {candidate.favorite_name}"
    )
    data: Dict[str, Any] = {
        "eventId": candidate.event_id,
        "category": event.get("category"),
        "severity": severity,
        "distanceKm": candidate.match.distance_km,
        "favoriteName": candidate.favorite_name,
        "lat": event.get("lat"),
        "lon": event.get("lon"),
    }
    return push.PushMessage(
        token=token,
        title=candidate.match.title,
        body=body,
        data=data,
    )


@dataclass(frozen=True)
class PendingSend:
    """A claim this pass owns, carrying the attempt count it inherited."""

    device_id: str
    event_id: str
    #: Attempts already recorded against this claim, so a failure can write
    #: ``attempts + 1``. Zero for a fresh claim, non-zero for a retry.
    attempts: int


async def _plan_for_device(
    token_row: Dict[str, Any],
    favorites: Sequence[Dict[str, Any]],
    events: Sequence[Dict[str, Any]],
    now: datetime,
    stats: DispatchStats,
) -> List[Candidate]:
    """Everything this device should hear about in this pass, after every filter.

    Suppressed candidates are dropped, not claimed. That is the whole point of
    running the filters before the claim: an alert held back by quiet hours or by
    the volume cap stays unclaimed, so the next pass can deliver it once the window
    ends or the budget frees up. Claiming first would consume the event silently
    and the user would never hear about it at all.
    """
    device_id = str(token_row.get("device_id") or "")
    if not device_id:
        return []

    # A device that never chose a threshold gets the configured default rather
    # than no filtering, matching the column default in `0002_alert_delivery.sql`.
    min_severity = token_row.get("min_severity") or settings.push_default_min_severity
    candidates = candidates_for_device(favorites, events, min_severity=str(min_severity))
    if not candidates:
        return []

    if in_quiet_hours(token_row, now):
        through = [c for c in candidates if breaks_through_quiet_hours(c.match.event)]
        stats.suppressed_quiet += len(candidates) - len(through)
        candidates = through
        if not candidates:
            return []

    recent = await sent_alerts_repo.recent_count_for_device(device_id, hours=VOLUME_WINDOW_HOURS)
    budget = max(0, settings.push_max_per_device_per_pass - recent)
    if budget < len(candidates):
        stats.suppressed_cap += len(candidates) - budget
        candidates = candidates[:budget]
    return candidates


async def _settle(
    pending: Sequence[PendingSend],
    deliveries: Sequence[push.Delivery],
    stats: DispatchStats,
) -> None:
    """Write the outcome of every claim this pass sent for.

    Settling is the step that must not be skipped, which is why it takes the whole
    batch and writes it in at most three statements: a claim left ``pending``
    because settlement was interrupted looks to the next pass like an abandoned
    send and is retried, so a partial settle is safe but wasteful.

    A permanent failure is recorded with ``attempts`` set to the maximum. That is
    not a lie about how many times we tried; it is the honest encoding of "there is
    nothing left to try", and it is what keeps the retry sweep — which filters
    ``attempts < max_attempts`` — from picking the row up forever.
    """
    sent_pairs: List[Tuple[str, str]] = []
    failures: List[DeliveryFailure] = []
    for item, delivery in zip(pending, deliveries, strict=True):
        if delivery.ok:
            sent_pairs.append((item.device_id, item.event_id))
            continue
        failures.append(
            DeliveryFailure(
                device_id=item.device_id,
                event_id=item.event_id,
                attempts=settings.push_max_attempts if delivery.permanent else item.attempts + 1,
                error=delivery.summary,
            )
        )

    if sent_pairs:
        stats.sent += await sent_alerts_repo.mark_sent(sent_pairs)
    if failures:
        stats.failed += await sent_alerts_repo.mark_failed(failures)

    dead = push.dead_tokens(deliveries)
    if dead:
        # Deleted by token value, not by device: a device that has since registered
        # a different token keeps its row, and one whose app is still installed
        # re-registers on the next launch. Keeping a retired token instead would
        # cost a wasted request on every pass, forever.
        stats.dead_tokens_pruned += await push_tokens_repo.delete_by_tokens(dead)


def _group_by_device(rows: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        device_id = str(row.get("device_id") or "")
        if device_id:
            grouped.setdefault(device_id, []).append(row)
    return grouped


async def _dispatch_page(
    token_rows: Sequence[Dict[str, Any]],
    events: Sequence[Dict[str, Any]],
    now: datetime,
    stats: DispatchStats,
) -> None:
    """Plan, claim, send and settle one page of devices.

    One favourites read, one claim and one settle per page rather than per device.
    A page is 100 devices by default and the claim is a single ``ON CONFLICT DO
    NOTHING`` over all of that page's pairs, so a pass costs a handful of round
    trips rather than one per device — and the claim stays a single statement,
    which is what makes it atomic against a concurrent pass.
    """
    device_ids = [str(row.get("device_id")) for row in token_rows if row.get("device_id")]
    if not device_ids:
        return

    favorites_by_device = _group_by_device(await favorites_repo.list_for_devices(device_ids))
    if not favorites_by_device:
        return

    plans: List[Tuple[Dict[str, Any], Candidate]] = []
    for row in token_rows:
        device_id = str(row.get("device_id") or "")
        favorites = favorites_by_device.get(device_id)
        if not favorites:
            # No saved locations means nothing to match against. Not an error and
            # not worth a log line: most installs sit here until the user saves
            # their first place.
            continue
        for candidate in await _plan_for_device(row, favorites, events, now, stats):
            plans.append((row, candidate))

    if not plans:
        return
    stats.candidates += len(plans)

    claimed = await sent_alerts_repo.claim(
        [(str(row["device_id"]), candidate.event_id) for row, candidate in plans]
    )
    if not claimed:
        # Every pair was already delivered. The normal case for a feed that has not
        # changed since the last pass, and the reason this runs every 15 minutes
        # without notifying anyone twice.
        return
    stats.claimed += len(claimed)

    won = {(str(row.get("device_id")), str(row.get("event_id"))) for row in claimed}
    pending: List[PendingSend] = []
    messages: List[push.PushMessage] = []
    for row, candidate in plans:
        device_id = str(row["device_id"])
        if (device_id, candidate.event_id) not in won:
            continue
        token = str(row.get("token") or "")
        pending.append(PendingSend(device_id=device_id, event_id=candidate.event_id, attempts=0))
        messages.append(build_message(token, candidate))

    if messages:
        await _settle(pending, await push.send(messages), stats)


def _retryable_by_device(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """``{device_id: {event_id: attempts_so_far}}`` from raw ledger rows."""
    grouped: Dict[str, Dict[str, int]] = {}
    for row in rows:
        device_id = str(row.get("device_id") or "")
        event_id = str(row.get("event_id") or "")
        if not device_id or not event_id:
            continue
        try:
            attempts = int(row.get("attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0
        grouped.setdefault(device_id, {})[event_id] = attempts
    return grouped


async def _sweep_retries(
    events: Sequence[Dict[str, Any]],
    now: datetime,
    stats: DispatchStats,
) -> None:
    """Re-attempt claims that failed or were abandoned mid-flight.

    This is what makes a crashed pass, a deploy in the middle of a send, or a
    thirty-second Expo outage cost nothing. ``stale_after_seconds`` is the dispatch
    interval, so a claim made moments ago by *this* pass cannot match — without
    that, the sweep would immediately re-send everything just sent.

    Retries are rebuilt from the current feed rather than replayed from a stored
    payload, and that is the interesting decision here. The notification text is
    not persisted anywhere, so a retry has to be re-derived — which means a retry
    is automatically re-filtered against the device's *current* preferences. A user
    who raised their severity floor after the failure does not receive the old
    alert. An event that has aged out of the feed is abandoned rather than sent
    late, for the reason ``PUSH_TTL_SECONDS`` exists: a stale hazard notification
    reads as a current one.

    The volume cap is deliberately not re-applied. A retry is an alert the device
    was already cleared for, its own claim is already inside the counting window,
    and ``attempts`` bounds it — capping it again would starve exactly the alerts
    this method exists to rescue.
    """
    rows = await sent_alerts_repo.retryable(
        max_attempts=settings.push_max_attempts,
        stale_after_seconds=settings.push_dispatch_interval_seconds,
    )
    by_device = _retryable_by_device(rows)
    if not by_device:
        return

    device_ids = sorted(by_device)
    rows_by_id = await push_tokens_repo.list_for_devices(device_ids)
    token_rows = {str(row.get("device_id")): row for row in rows_by_id}
    favorites_by_device = _group_by_device(await favorites_repo.list_for_devices(device_ids))

    pending: List[PendingSend] = []
    messages: List[push.PushMessage] = []
    abandoned: List[DeliveryFailure] = []

    for device_id, wanted in by_device.items():
        token_row = token_rows.get(device_id)
        token = str(token_row.get("token") or "") if token_row else ""
        if token_row is None or not token_row.get("alerts_enabled") or not token:
            abandoned.extend(
                DeliveryFailure(
                    device_id=device_id,
                    event_id=event_id,
                    attempts=settings.push_max_attempts,
                    error="device no longer accepts alerts",
                )
                for event_id in wanted
            )
            continue

        min_severity = token_row.get("min_severity") or settings.push_default_min_severity
        candidates = candidates_for_device(
            favorites_by_device.get(device_id) or [],
            events,
            min_severity=str(min_severity),
        )
        available = {candidate.event_id: candidate for candidate in candidates}
        quiet = in_quiet_hours(token_row, now)

        for event_id, attempts in wanted.items():
            candidate = available.get(event_id)
            if candidate is None:
                abandoned.append(
                    DeliveryFailure(
                        device_id=device_id,
                        event_id=event_id,
                        attempts=settings.push_max_attempts,
                        error="event no longer matches this device",
                    )
                )
                continue
            if quiet and not breaks_through_quiet_hours(candidate.match.event):
                # Left unsettled on purpose: the claim stays stale and retryable, so
                # the first pass after the window ends picks it up.
                stats.suppressed_quiet += 1
                continue
            pending.append(PendingSend(device_id=device_id, event_id=event_id, attempts=attempts))
            messages.append(build_message(token, candidate))

    if abandoned:
        stats.failed += await sent_alerts_repo.mark_failed(abandoned)
    if messages:
        await _settle(pending, await push.send(messages), stats)


async def _walk_devices(
    events: Sequence[Dict[str, Any]],
    now: datetime,
    stats: DispatchStats,
) -> None:
    """Page through every dispatchable device, keyset-paginated on ``device_id``."""
    after: Optional[str] = None
    page_size = settings.push_batch_size
    for _ in range(MAX_PAGES_PER_PASS):
        page = await push_tokens_repo.dispatchable_page(limit=page_size, after_device_id=after)
        if not page:
            return
        stats.devices += len(page)
        await _dispatch_page(page, events, now, stats)

        after = str(page[-1].get("device_id") or "")
        if not after or len(page) < page_size:
            return
    logger.warning(
        "Dispatch pass stopped at the page ceiling; some devices were not visited",
        extra={"pages": MAX_PAGES_PER_PASS, "page_size": page_size},
    )


async def run_pass() -> DispatchStats:
    """One complete dispatch pass. Never raises; the outcome is in the return value.

    Safe to run concurrently with another copy of itself and safe to run by hand
    against production — the claim is what guarantees both, and it is the only
    guarantee that matters here.
    """
    stats = DispatchStats()
    if not settings.push_enabled:
        # Not a warning. Off is the default and a deliberate deployment choice.
        stats.aborted = "push_disabled"
        return stats

    now = datetime.now(UTC)
    try:
        events, failed_sources = await disasters.aggregate(hours=settings.push_lookback_hours)
        stats.events = len(events)
        stats.sources_failed = list(failed_sources)

        if events:
            await _walk_devices(events, now, stats)
            await _sweep_retries(events, now, stats)
        elif failed_sources:
            # An empty feed with failed providers is not a quiet planet, it is an
            # outage — and the retry sweep abandons claims whose event is absent
            # from the feed, so running it here would discard live alerts as
            # "no longer matching". Skip the whole pass and try again in fifteen
            # minutes.
            stats.aborted = "feed_unavailable"
    except AppError as exc:
        # Datastore or upstream failure the pass cannot work around. Recorded
        # rather than raised so the worker's loop stays a plain timer and the
        # counters gathered before the failure are not lost.
        stats.aborted = type(exc).__name__
        logger.error("Dispatch pass aborted", extra={"error": str(exc)})

    log = logger.warning if stats.aborted else logger.info
    log("Dispatch pass complete", extra=stats.as_log_fields())
    return stats


async def prune_ledger() -> int:
    """Drop delivery records past the retention window. Returns rows removed.

    Duplicates what ``prune_old_data`` in ``0002_alert_delivery.sql`` does, for
    deployments without pg_cron — which is most of them. Idempotent and safe to run
    alongside a dispatch pass: the dispatcher only reads events from the last few
    days, so a claim old enough to be pruned can no longer be suppressing anything.
    """
    removed = await sent_alerts_repo.purge_older_than(settings.sent_alerts_retention_days)
    if removed:
        logger.info(
            "Pruned delivery ledger",
            extra={"removed": removed, "retain_days": settings.sent_alerts_retention_days},
        )
    return removed


__all__ = [
    "MAX_PAGES_PER_PASS",
    "VOLUME_WINDOW_HOURS",
    "Candidate",
    "DispatchStats",
    "PendingSend",
    "breaks_through_quiet_hours",
    "build_message",
    "candidates_for_device",
    "in_quiet_hours",
    "prune_ledger",
    "run_pass",
]
