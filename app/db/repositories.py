"""Repositories: every query that touches user-owned data lives here.

The rule this layer exists to enforce: **no read or write of user-owned data
without a device id.** Previously ``GET /api/favorites`` ran
``table("favorites").select("*")`` with no filter and returned every row in the
database to every caller, and ``DELETE /api/favorites/{uid}`` deleted by station
id alone. Both are fixed by construction here — the scoped methods take
``device_id`` as a required first argument, so an unscoped query would have to
be written deliberately rather than forgotten accidentally.

Moderation model for community reports is **post-moderation**: a new report is
``visible`` immediately and an admin can later ``hide`` or ``remove`` it. The
alternative (nothing visible until approved) would leave the community feed
looking permanently empty, which is worse than the risk it mitigates for a
public-safety feed where timeliness is the point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.geoutils import bounding_box, haversine_km
from app.core.logging import get_logger
from app.db.supabase import require_client, run_db

logger = get_logger(__name__)

# Report visibility states.
STATUS_VISIBLE = "visible"
STATUS_HIDDEN = "hidden"
STATUS_REMOVED = "removed"
REPORT_STATUSES = (STATUS_VISIBLE, STATUS_HIDDEN, STATUS_REMOVED)

# Guard rails so one device cannot fill the tables.
MAX_FAVORITES_PER_DEVICE = 50
MAX_REPORTS_PER_DEVICE_PER_DAY = 20


def _rows(result: Any) -> List[Dict[str, Any]]:
    """Normalise a postgrest response into a list of dicts."""
    data = getattr(result, "data", None)
    if data is None:
        return []
    if isinstance(data, dict):
        return [data]
    return list(data)


def _first(result: Any) -> Optional[Dict[str, Any]]:
    rows = _rows(result)
    return rows[0] if rows else None


def _is_unique_violation(exc: BaseException) -> bool:
    """Detect a Postgres unique-constraint violation through postgrest."""
    code = getattr(exc, "code", None)
    if code in ("23505", 23505):
        return True
    message = f"{getattr(exc, 'message', '')} {exc}".lower()
    return "23505" in message or "duplicate key" in message


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


class FavoritesRepository:
    """Saved locations, scoped to one device."""

    TABLE = "favorites"

    async def list_for_device(
        self, device_id: str, *, limit: int = MAX_FAVORITES_PER_DEVICE
    ) -> List[Dict[str, Any]]:
        client = require_client()

        def _query() -> Any:
            return (
                client.table(self.TABLE)
                .select("*")
                .eq("device_id", device_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )

        return _rows(await run_db("favorites.list", _query))

    async def list_for_devices(self, device_ids: Sequence[str]) -> List[Dict[str, Any]]:
        """Every favourite belonging to any of ``device_ids``.

        The one read in this module that is not scoped to a single device, and it
        exists for exactly one caller: the push dispatcher, which has to ask
        "what is near each of these devices" for a page of devices at a time.
        Nothing reachable from an HTTP request may call it — a request handler
        knows one device id, and :meth:`list_for_device` is the method for that.

        The ids come from a page of ``push_tokens``, never from client input, so
        this cannot be steered into reading another device's rows.

        Note the limit: ``len(device_ids) * MAX_FAVORITES_PER_DEVICE``, not a
        flat page size. postgrest applies the limit to the joined result, so a
        fixed cap would silently drop the last devices in the batch — and a
        device whose favourites went missing would be told nothing is happening
        near it, which is the false all-clear this app exists to avoid.
        """
        if not device_ids:
            return []
        client = require_client()
        ids = list(device_ids)

        def _query() -> Any:
            return (
                client.table(self.TABLE)
                .select("*")
                .in_("device_id", ids)
                .limit(len(ids) * MAX_FAVORITES_PER_DEVICE)
                .execute()
            )

        return _rows(await run_db("favorites.list_for_devices", _query))

    async def count_for_device(self, device_id: str) -> int:
        client = require_client()

        def _query() -> Any:
            return (
                client.table(self.TABLE)
                .select("id", count="exact")
                .eq("device_id", device_id)
                .execute()
            )

        result = await run_db("favorites.count", _query)
        count = getattr(result, "count", None)
        if count is not None:
            return int(count)
        return len(_rows(result))

    async def create(
        self,
        device_id: str,
        *,
        name: str,
        lat: float,
        lon: float,
        station_uid: Optional[str] = None,
        alert_radius_km: float = 100.0,
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """Insert a favourite.

        Returns ``(row, created)``. ``created`` is ``False`` when the same
        location was already saved by this device, in which case the existing
        row is returned so the client sees a stable, idempotent result rather
        than a duplicate-key error.
        """
        client = require_client()
        payload = {
            "device_id": device_id,
            "name": name,
            "lat": lat,
            "lon": lon,
            "station_uid": station_uid,
            "alert_radius_km": alert_radius_km,
            "created_at": _utcnow_iso(),
        }

        def _insert() -> Any:
            try:
                return client.table(self.TABLE).insert(payload).execute()
            except Exception as exc:
                if _is_unique_violation(exc):
                    return "duplicate"
                raise

        result = await run_db("favorites.create", _insert)
        if result == "duplicate":
            existing = await self.find(device_id, station_uid=station_uid, lat=lat, lon=lon)
            return existing, False
        return _first(result), True

    async def find(
        self,
        device_id: str,
        *,
        station_uid: Optional[str] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Locate one of this device's favourites by station id or coordinates.

        Both are tried, station id first, because there are two unique indexes on
        this table — ``(device_id, station_uid)`` and ``(device_id, lat, lon)`` —
        and :meth:`create` cannot tell which one a duplicate-key error came from.
        Looking up only by station id would return nothing when it was the
        coordinate index that fired, and the caller would report "could not be
        saved" for a location that is in fact already saved.
        """
        client = require_client()

        def _by_station() -> Any:
            return (
                client.table(self.TABLE)
                .select("*")
                .eq("device_id", device_id)
                .eq("station_uid", station_uid)
                .limit(1)
                .execute()
            )

        def _by_coords() -> Any:
            return (
                client.table(self.TABLE)
                .select("*")
                .eq("device_id", device_id)
                .eq("lat", lat)
                .eq("lon", lon)
                .limit(1)
                .execute()
            )

        if station_uid:
            row = _first(await run_db("favorites.find_by_station", _by_station))
            if row:
                return row

        if lat is not None and lon is not None:
            return _first(await run_db("favorites.find_by_coords", _by_coords))

        return None

    async def delete(self, device_id: str, favorite_id: str) -> bool:
        """Delete one favourite. The ``device_id`` filter is the ownership check."""
        client = require_client()

        def _delete() -> Any:
            return (
                client.table(self.TABLE)
                .delete()
                .eq("id", favorite_id)
                .eq("device_id", device_id)
                .execute()
            )

        return bool(_rows(await run_db("favorites.delete", _delete)))


class ReportsRepository:
    """Community hazard reports."""

    TABLE = "community_reports"
    VOTES_TABLE = "report_votes"

    async def list_nearby(
        self,
        *,
        lat: float,
        lon: float,
        radius_km: float,
        limit: int = 100,
        max_age_hours: int = 72,
        category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Visible reports near a point, nearest first.

        A bounding box narrows the result set in the database; the exact
        haversine filter runs here. Doing the box push-down matters because the
        alternative is fetching the whole table and filtering in Python, which
        is what the previous implementation did.
        """
        client = require_client()
        min_lat, max_lat, min_lon, max_lon = bounding_box(lat, lon, radius_km)
        cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat()

        def _query() -> Any:
            query = (
                client.table(self.TABLE)
                .select("*")
                .eq("status", STATUS_VISIBLE)
                .gte("lat", min_lat)
                .lte("lat", max_lat)
                .gte("lon", min_lon)
                .lte("lon", max_lon)
                .gte("created_at", cutoff)
            )
            if category:
                query = query.eq("category", category)
            # Fetch a generous slice, then trim after the exact distance pass.
            return query.order("created_at", desc=True).limit(limit * 3).execute()

        rows = _rows(await run_db("reports.list_nearby", _query))

        enriched: List[Dict[str, Any]] = []
        for row in rows:
            try:
                distance = haversine_km(lat, lon, float(row["lat"]), float(row["lon"]))
            except (KeyError, TypeError, ValueError):
                continue
            if distance <= radius_km:
                row["distance_km"] = round(distance, 2)
                enriched.append(row)

        enriched.sort(key=lambda r: r["distance_km"])
        return enriched[:limit]

    async def count_recent_for_device(self, device_id: str, *, hours: int = 24) -> int:
        client = require_client()
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()

        def _query() -> Any:
            return (
                client.table(self.TABLE)
                .select("id", count="exact")
                .eq("device_id", device_id)
                .gte("created_at", cutoff)
                .execute()
            )

        result = await run_db("reports.count_recent", _query)
        count = getattr(result, "count", None)
        if count is not None:
            return int(count)
        return len(_rows(result))

    async def create(
        self,
        device_id: str,
        *,
        category: str,
        title: str,
        description: Optional[str],
        lat: float,
        lon: float,
        severity: str,
    ) -> Optional[Dict[str, Any]]:
        client = require_client()
        now = _utcnow_iso()
        payload = {
            "device_id": device_id,
            "category": category,
            "title": title,
            "description": description,
            "lat": lat,
            "lon": lon,
            "severity": severity,
            "status": STATUS_VISIBLE,
            "upvotes": 0,
            "created_at": now,
            "updated_at": now,
        }

        def _insert() -> Any:
            return client.table(self.TABLE).insert(payload).execute()

        return _first(await run_db("reports.create", _insert))

    async def get(self, report_id: str) -> Optional[Dict[str, Any]]:
        client = require_client()

        def _query() -> Any:
            return client.table(self.TABLE).select("*").eq("id", report_id).limit(1).execute()

        return _first(await run_db("reports.get", _query))

    async def add_vote(self, report_id: str, device_id: str) -> Tuple[bool, int]:
        """Record one upvote per device.

        Returns ``(accepted, upvotes)``. ``accepted`` is ``False`` when this
        device already voted — enforced by a composite primary key on
        ``(report_id, device_id)``, not by an application-level check, so
        concurrent duplicate requests cannot both succeed.
        """
        client = require_client()

        def _insert_vote() -> Any:
            try:
                return (
                    client.table(self.VOTES_TABLE)
                    .insert(
                        {
                            "report_id": report_id,
                            "device_id": device_id,
                            "created_at": _utcnow_iso(),
                        }
                    )
                    .execute()
                )
            except Exception as exc:
                if _is_unique_violation(exc):
                    return "duplicate"
                raise

        result = await run_db("reports.add_vote", _insert_vote)
        if result == "duplicate":
            current = await self.count_votes(report_id)
            return False, current

        # Recount rather than incrementing a cached column: two concurrent votes
        # doing read-modify-write on `upvotes` would lose one of them.
        total = await self.count_votes(report_id)
        await self._set_upvotes(report_id, total)
        return True, total

    async def count_votes(self, report_id: str) -> int:
        client = require_client()

        def _query() -> Any:
            return (
                client.table(self.VOTES_TABLE)
                .select("device_id", count="exact")
                .eq("report_id", report_id)
                .execute()
            )

        result = await run_db("reports.count_votes", _query)
        count = getattr(result, "count", None)
        if count is not None:
            return int(count)
        return len(_rows(result))

    async def _set_upvotes(self, report_id: str, total: int) -> None:
        client = require_client()

        def _update() -> Any:
            return (
                client.table(self.TABLE)
                .update({"upvotes": total, "updated_at": _utcnow_iso()})
                .eq("id", report_id)
                .execute()
            )

        await run_db("reports.set_upvotes", _update)

    async def voted_report_ids(self, device_id: str, report_ids: List[str]) -> List[str]:
        """Which of these reports has this device already voted on?

        Lets the client render the vote button in the correct state instead of
        letting the user tap it and get a rejection.
        """
        if not report_ids:
            return []
        client = require_client()

        def _query() -> Any:
            return (
                client.table(self.VOTES_TABLE)
                .select("report_id")
                .eq("device_id", device_id)
                .in_("report_id", report_ids)
                .execute()
            )

        rows = _rows(await run_db("reports.voted_ids", _query))
        return [str(row["report_id"]) for row in rows if row.get("report_id")]

    async def delete_own(self, device_id: str, report_id: str) -> bool:
        client = require_client()

        def _delete() -> Any:
            return (
                client.table(self.TABLE)
                .delete()
                .eq("id", report_id)
                .eq("device_id", device_id)
                .execute()
            )

        return bool(_rows(await run_db("reports.delete_own", _delete)))

    # -- Admin-only -------------------------------------------------------

    async def admin_list(
        self,
        *,
        status: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        client = require_client()

        def _query() -> Any:
            query = client.table(self.TABLE).select("*", count="exact")
            if status:
                query = query.eq("status", status)
            if category:
                query = query.eq("category", category)
            return query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()

        result = await run_db("reports.admin_list", _query)
        total = getattr(result, "count", None)
        rows = _rows(result)
        return rows, int(total) if total is not None else len(rows)

    async def admin_set_status(
        self, report_id: str, status: str, *, moderator: str
    ) -> Optional[Dict[str, Any]]:
        client = require_client()

        def _update() -> Any:
            return (
                client.table(self.TABLE)
                .update(
                    {
                        "status": status,
                        "moderated_by": moderator,
                        "moderated_at": _utcnow_iso(),
                        "updated_at": _utcnow_iso(),
                    }
                )
                .eq("id", report_id)
                .execute()
            )

        return _first(await run_db("reports.admin_set_status", _update))

    async def admin_stats(self) -> Dict[str, int]:
        """Counts per status, for the dashboard header."""
        client = require_client()

        async def _count(status: Optional[str]) -> int:
            def _query() -> Any:
                query = client.table(self.TABLE).select("id", count="exact")
                if status:
                    query = query.eq("status", status)
                return query.execute()

            result = await run_db("reports.admin_stats", _query)
            count = getattr(result, "count", None)
            return int(count) if count is not None else len(_rows(result))

        return {
            "total": await _count(None),
            "visible": await _count(STATUS_VISIBLE),
            "hidden": await _count(STATUS_HIDDEN),
            "removed": await _count(STATUS_REMOVED),
        }


class PushTokenRepository:
    """Expo push tokens, one row per device."""

    TABLE = "push_tokens"

    async def upsert(
        self,
        device_id: str,
        *,
        token: str,
        platform: Optional[str] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Register or refresh this device's push token.

        Upsert on ``device_id`` so a device that reinstalls or rotates its Expo
        token replaces its old row instead of accumulating stale tokens that
        will fail to deliver forever.

        The payload deliberately omits every preference column. postgrest builds
        the ``ON CONFLICT DO UPDATE SET`` list from the keys actually present, so
        a re-registration leaves ``alerts_enabled``, ``min_severity``, the quiet
        window and ``timezone`` exactly as the user set them. The app re-registers
        on almost every cold start; if that reset preferences, a user who had
        turned alerts off would find them back on the next morning.
        """
        client = require_client()
        payload: Dict[str, Any] = {
            "device_id": device_id,
            "token": token,
            "platform": platform,
            "updated_at": _utcnow_iso(),
        }
        if lat is not None and lon is not None:
            payload["lat"] = lat
            payload["lon"] = lon

        def _upsert() -> Any:
            return client.table(self.TABLE).upsert(payload, on_conflict="device_id").execute()

        return _first(await run_db("push_tokens.upsert", _upsert))

    async def delete(self, device_id: str) -> bool:
        client = require_client()

        def _delete() -> Any:
            return client.table(self.TABLE).delete().eq("device_id", device_id).execute()

        return bool(_rows(await run_db("push_tokens.delete", _delete)))

    async def get(self, device_id: str) -> Optional[Dict[str, Any]]:
        client = require_client()

        def _query() -> Any:
            return (
                client.table(self.TABLE).select("*").eq("device_id", device_id).limit(1).execute()
            )

        return _first(await run_db("push_tokens.get", _query))

    async def count(self) -> int:
        client = require_client()

        def _query() -> Any:
            return client.table(self.TABLE).select("device_id", count="exact").execute()

        result = await run_db("push_tokens.count", _query)
        count = getattr(result, "count", None)
        return int(count) if count is not None else len(_rows(result))

    # -- Alert preferences -------------------------------------------------

    async def update_preferences(
        self,
        device_id: str,
        *,
        alerts_enabled: Optional[bool] = None,
        min_severity: Optional[str] = None,
        quiet_hours_start: Optional[int] = None,
        quiet_hours_end: Optional[int] = None,
        timezone_name: Optional[str] = None,
        clear_quiet_hours: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Change one device's alert preferences. Returns the row, or ``None``.

        ``None`` back means the device has no token row, which is a 404 and not
        an error: preferences are stored on the token, so there is nothing to
        configure until the device has registered for notifications.

        Every argument defaulting to ``None`` means "leave this column alone",
        which is why clearing the quiet window needs its own flag rather than
        passing ``None`` for both ends — those two are indistinguishable
        otherwise, and the ``push_tokens_quiet_hours_paired`` CHECK rejects a
        half-specified window, so guessing would surface as a 503.

        Values are not validated here. The API schema constrains them before
        this is reached, and the CHECK constraints in
        ``0002_alert_delivery.sql`` are the real backstop; duplicating the
        vocabulary in a third place would only give it somewhere to drift.
        """
        client = require_client()
        payload: Dict[str, Any] = {"updated_at": _utcnow_iso()}
        if alerts_enabled is not None:
            payload["alerts_enabled"] = alerts_enabled
        if min_severity is not None:
            payload["min_severity"] = min_severity
        if timezone_name is not None:
            payload["timezone"] = timezone_name
        if clear_quiet_hours:
            payload["quiet_hours_start"] = None
            payload["quiet_hours_end"] = None
        elif quiet_hours_start is not None and quiet_hours_end is not None:
            payload["quiet_hours_start"] = quiet_hours_start
            payload["quiet_hours_end"] = quiet_hours_end

        def _update() -> Any:
            return client.table(self.TABLE).update(payload).eq("device_id", device_id).execute()

        return _first(await run_db("push_tokens.update_preferences", _update))

    async def dispatchable_page(
        self, *, limit: int, after_device_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """One page of devices that can currently be sent to, keyed for resumption.

        Device-unscoped by necessity: the dispatcher's whole job is to walk every
        device. Like :meth:`FavoritesRepository.list_for_devices`, nothing
        reachable from an HTTP request may call it.

        Paginated by ascending ``device_id`` rather than by offset or by
        ``updated_at``. The rows this walks are the same rows the dispatcher
        writes to during the walk — a successful send touches ``updated_at``, and
        the app re-registering mid-pass touches it too. Under an
        ``order by updated_at`` page window, a row that moves is visited twice or
        skipped entirely; skipped means a missed alert. ``device_id`` is unique
        (see the unique index in ``0001_initial_schema.sql``) and immutable, so a
        keyset on it is stable no matter what the pass writes.

        ``alerts_enabled`` is filtered in the database, not here: a device that
        has muted alerts should not even be fetched, and the partial index in
        ``0002_alert_delivery.sql`` exists for this query.
        """
        client = require_client()

        def _query() -> Any:
            query = client.table(self.TABLE).select("*").eq("alerts_enabled", True)
            if after_device_id is not None:
                query = query.gt("device_id", after_device_id)
            return query.order("device_id").limit(limit).execute()

        return _rows(await run_db("push_tokens.dispatchable_page", _query))

    async def list_for_devices(self, device_ids: Sequence[str]) -> List[Dict[str, Any]]:
        """Token rows for specific devices, whatever their ``alerts_enabled`` state.

        For the dispatcher's retry sweep, which starts from ``sent_alerts`` rows and
        works back to the devices they belong to — the reverse of
        :meth:`dispatchable_page`, which starts from devices.

        Unfiltered on ``alerts_enabled`` on purpose: the caller has to be able to
        tell "this device turned alerts off since the failed attempt" apart from
        "this device no longer has a token row", because the first must abandon the
        retry and the second must abandon it *and* stop looking. Returning nothing
        in both cases would make those indistinguishable.

        Device-unscoped like the two methods above, and reachable only from the
        dispatcher; the ids come from our own ledger, never from client input.
        """
        if not device_ids:
            return []
        client = require_client()
        ids = list(device_ids)

        def _query() -> Any:
            return (
                client.table(self.TABLE)
                .select("*")
                .in_("device_id", ids)
                .limit(len(ids))
                .execute()
            )

        return _rows(await run_db("push_tokens.list_for_devices", _query))

    async def delete_by_tokens(self, tokens: Sequence[str]) -> int:
        """Drop the rows holding these Expo tokens. Returns how many went.

        For ``DeviceNotRegistered``: the app was uninstalled, or the token was
        rotated and Expo has retired the old one. Every future send to it fails,
        so keeping the row costs a request per pass forever and eventually
        reputation with the push service.

        Deleting by token value rather than by device id is deliberate — the dead
        thing is the token, and the row that holds it is exactly the row to
        remove. This is unscoped by device for the same reason as the two methods
        above, and reachable only from the dispatcher.

        The tokens are not logged, here or by the caller. ``run_db``'s label is
        the only thing that reaches a log sink from this call.
        """
        if not tokens:
            return 0
        client = require_client()
        values = list(tokens)

        def _delete() -> Any:
            return client.table(self.TABLE).delete().in_("token", values).execute()

        return len(_rows(await run_db("push_tokens.delete_by_tokens", _delete)))


# Delivery states for `sent_alerts`. Mirrors the CHECK constraint in
# `0002_alert_delivery.sql`; a value not in this tuple is rejected by the
# database rather than quietly stored.
DELIVERY_PENDING = "pending"
DELIVERY_SENT = "sent"
DELIVERY_FAILED = "failed"

# `last_error` is the only free-text column anywhere in the delivery path, and
# Expo puts the push token *inside* some of its error messages — the
# DeviceNotRegistered body reads `"ExponentPushToken[xxx]" is not a registered
# push notification recipient`. Storing that verbatim would put a live token in a
# table, defeating the point of `_REDACT_KEYS` keeping it out of the logs. So the
# token shape is scrubbed on the way in, and the text is capped.
_TOKEN_PATTERN = re.compile(r"(?:Exponent|Expo)PushToken\[[^\]]*\]", re.IGNORECASE)
_MAX_ERROR_LENGTH = 300


def _scrub_error(error: str) -> str:
    """Strip anything token-shaped out of an error string, then bound its length."""
    return _TOKEN_PATTERN.sub("[token]", error).strip()[:_MAX_ERROR_LENGTH]


@dataclass(frozen=True)
class DeliveryFailure:
    """One (device, event) send that did not succeed.

    ``attempts`` is the count *including* this attempt, so the caller writes
    ``claim["attempts"] + 1``. The arithmetic is the caller's because postgrest
    cannot express ``attempts = attempts + 1``, and read-modify-write is safe
    here for the one reason that makes this whole design work: the row's
    composite primary key means exactly one dispatcher pass owns it.
    """

    device_id: str
    event_id: str
    attempts: int
    error: str


class SentAlertsRepository:
    """The delivery ledger: one row per (device, event) ever notified.

    This table is what stops a user being woken four times for one earthquake,
    and the guarantee is structural rather than procedural. :meth:`claim` inserts
    with ``ON CONFLICT DO NOTHING`` and returns only the rows it actually
    created, so two dispatcher passes running concurrently — a slow pass
    overlapping the next tick, two replicas, a manual run beside the scheduled
    one — cannot both come away believing they own the same send.

    Claim *before* sending, never after. A crash between claim and send costs at
    most one alert, and the retry sweep recovers it. A crash between send and
    claim re-sends on every pass forever, which is the failure this table exists
    to prevent.
    """

    TABLE = "sent_alerts"

    #: The composite primary key from `0002_alert_delivery.sql`, spelled once.
    #: Every write to this table conflicts on it; a typo in one of them would
    #: silently become an unconditional insert and a duplicate notification.
    ON_CONFLICT = "device_id,event_id"

    async def claim(self, pairs: Sequence[Tuple[str, str]]) -> List[Dict[str, Any]]:
        """Try to claim these ``(device_id, event_id)`` sends. Returns the wins.

        The returned rows are exactly the ones no previous pass had claimed —
        send to those and nothing else. Pairs already in the table come back
        absent, which is the deduplication.

        A returned row's ``attempts`` is ``0``: it has been claimed, not yet
        attempted. Follow with :meth:`mark_sent` or :meth:`mark_failed`.

        One round trip regardless of batch size. A per-pair
        select-then-insert would be both slower and racy — the gap between the
        two reads is precisely where a double send lives.
        """
        if not pairs:
            return []
        client = require_client()
        now = _utcnow_iso()
        rows = [
            {
                "device_id": device_id,
                "event_id": event_id,
                "status": DELIVERY_PENDING,
                "attempts": 0,
                "claimed_at": now,
            }
            for device_id, event_id in pairs
        ]

        def _claim() -> Any:
            return (
                client.table(self.TABLE)
                .upsert(rows, on_conflict=self.ON_CONFLICT, ignore_duplicates=True)
                .execute()
            )

        claimed = _rows(await run_db("sent_alerts.claim", _claim))
        if len(claimed) != len(rows):
            logger.debug(
                "Claimed %d of %d candidate alerts; the rest were already delivered",
                len(claimed),
                len(rows),
            )
        return claimed

    async def mark_sent(self, pairs: Sequence[Tuple[str, str]]) -> int:
        """Settle claims that Expo accepted. Returns how many rows were written.

        Batched into one statement because Expo takes 100 notifications per
        request, and settling those one row at a time would put 100 round trips
        behind every send. A merge-upsert is the only way postgrest can write
        different rows in one statement.

        Upsert rather than update, so a claim pruned mid-pass is re-created as
        ``sent`` instead of vanishing. Losing the record would let the next pass
        re-claim the event and send it again.
        """
        if not pairs:
            return 0
        client = require_client()
        now = _utcnow_iso()
        rows = [
            {
                "device_id": device_id,
                "event_id": event_id,
                "status": DELIVERY_SENT,
                "attempts": 1,
                "last_error": None,
                "sent_at": now,
            }
            for device_id, event_id in pairs
        ]

        def _update() -> Any:
            return client.table(self.TABLE).upsert(rows, on_conflict=self.ON_CONFLICT).execute()

        return len(_rows(await run_db("sent_alerts.mark_sent", _update)))

    async def mark_failed(self, failures: Sequence[DeliveryFailure]) -> int:
        """Record failed attempts so the retry sweep can pick them up.

        The row stays behind on purpose. Deleting it would let the next pass
        re-claim the event as if it were new, with no attempt count and therefore
        no bound on how many times a permanently broken send is retried.
        """
        if not failures:
            return 0
        client = require_client()
        rows = [
            {
                "device_id": failure.device_id,
                "event_id": failure.event_id,
                "status": DELIVERY_FAILED,
                "attempts": failure.attempts,
                "last_error": _scrub_error(failure.error),
                "sent_at": None,
            }
            for failure in failures
        ]

        def _update() -> Any:
            return client.table(self.TABLE).upsert(rows, on_conflict=self.ON_CONFLICT).execute()

        return len(_rows(await run_db("sent_alerts.mark_failed", _update)))

    async def retryable(
        self, *, max_attempts: int, stale_after_seconds: int, limit: int = 200
    ) -> List[Dict[str, Any]]:
        """Claims worth another attempt: failed sends, and pending ones abandoned mid-flight.

        ``stale_after_seconds`` is not optional, and it is the reason this method
        cannot double-send. A claim made moments ago is ``pending`` and would
        otherwise match here, so a pass that swept for retries after making its
        claims would immediately re-send everything it had just sent. Only a claim
        older than one dispatch interval can plausibly have been abandoned by a
        crashed pass.

        ``attempts < max_attempts`` is the bound. Without it a token that is
        broken rather than merely unlucky is retried on every pass until the row
        is pruned, and each retry is a request that will fail again.

        Oldest first: a stuck alert about an event from an hour ago matters more
        than one from a minute ago, and it is closer to being irrelevant.
        """
        client = require_client()
        cutoff = (datetime.now(UTC) - timedelta(seconds=stale_after_seconds)).isoformat()

        def _query() -> Any:
            return (
                client.table(self.TABLE)
                .select("*")
                .neq("status", DELIVERY_SENT)
                .lt("attempts", max_attempts)
                .lt("claimed_at", cutoff)
                .order("claimed_at")
                .limit(limit)
                .execute()
            )

        return _rows(await run_db("sent_alerts.retryable", _query))

    async def recent_count_for_device(self, device_id: str, *, hours: int = 1) -> int:
        """How many alerts this device has been claimed for recently.

        Backs the per-pass volume cap. A wide radius during an active earthquake
        sequence can match dozens of events that are each individually worth
        sending; delivered as dozens of notifications, the user mutes the app and
        then hears nothing about the one that matters.

        Counts claims, not successful sends, so a run of failures cannot become a
        way to exceed the cap.
        """
        client = require_client()
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()

        def _query() -> Any:
            return (
                client.table(self.TABLE)
                .select("event_id", count="exact")
                .eq("device_id", device_id)
                .gte("claimed_at", cutoff)
                .execute()
            )

        result = await run_db("sent_alerts.recent_count", _query)
        count = getattr(result, "count", None)
        if count is not None:
            return int(count)
        return len(_rows(result))

    async def purge_older_than(self, days: int) -> int:
        """Delete claims older than ``days``. Returns how many went.

        The same work as ``prune_old_data`` in ``0002_alert_delivery.sql``,
        callable from the worker for deployments without pg_cron. Safe because the
        dispatcher only ever looks at events from the last few days, so a claim
        past the retention window can no longer be suppressing a live alert.
        """
        client = require_client()
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()

        def _delete() -> Any:
            return client.table(self.TABLE).delete().lt("claimed_at", cutoff).execute()

        return len(_rows(await run_db("sent_alerts.purge", _delete)))


favorites_repo = FavoritesRepository()
reports_repo = ReportsRepository()
push_tokens_repo = PushTokenRepository()
sent_alerts_repo = SentAlertsRepository()

__all__ = [
    "DELIVERY_FAILED",
    "DELIVERY_PENDING",
    "DELIVERY_SENT",
    "MAX_FAVORITES_PER_DEVICE",
    "MAX_REPORTS_PER_DEVICE_PER_DAY",
    "REPORT_STATUSES",
    "STATUS_HIDDEN",
    "STATUS_REMOVED",
    "STATUS_VISIBLE",
    "DeliveryFailure",
    "FavoritesRepository",
    "PushTokenRepository",
    "ReportsRepository",
    "SentAlertsRepository",
    "favorites_repo",
    "push_tokens_repo",
    "reports_repo",
    "sent_alerts_repo",
]
