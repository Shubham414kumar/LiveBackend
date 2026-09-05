"""Push notification registration and per-device alert preferences."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends

from app.api.deps import read_limit, write_limit
from app.core.config import settings
from app.core.errors import NotFoundError, ValidationError
from app.core.identity import require_device_id
from app.db.repositories import push_tokens_repo
from app.models.schemas import (
    AlertPreferences,
    AlertPreferencesUpdate,
    PushTokenRegister,
    SimpleStatus,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])

#: Answered by both PATCH branches, so it is written once. Phrased as an
#: instruction because the client's next call is the fix: register a token, then
#: retry the update.
_NOT_REGISTERED = (
    "This device has not registered for alerts yet, so there are no preferences "
    "to update. Register a push token first."
)


def _preferences(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Project a token row onto the preferences response.

    ``None`` is answered with the server defaults and ``registered: false`` rather
    than a 404. The settings screen is reachable before notifications have ever
    been enabled, and it needs something to render; a 404 would push that decision
    into every client.

    The token itself is never included — see :class:`AlertPreferences`.
    """
    row = row or {}
    return {
        "registered": bool(row),
        # Absent means true: the column defaults to true, and a device that has a
        # token but no explicit choice is opted in.
        "alerts_enabled": bool(row.get("alerts_enabled", True)),
        "min_severity": row.get("min_severity") or settings.push_default_min_severity,
        "quiet_hours_start": row.get("quiet_hours_start"),
        "quiet_hours_end": row.get("quiet_hours_end"),
        "timezone": row.get("timezone"),
        "quiet_hours_breakthrough": settings.push_quiet_hours_breakthrough,
        "delivery_enabled": settings.push_enabled,
    }


@router.post(
    "/register",
    response_model=SimpleStatus,
    dependencies=[Depends(write_limit)],
    summary="Register or refresh this device's push token",
)
async def register(
    payload: PushTokenRegister,
    device_id: str = Depends(require_device_id),
) -> Dict[str, Any]:
    """Upsert on ``device_id``, not on the token.

    A device's Expo token rotates on reinstall and on some OS updates. Keying on
    the token accumulates dead rows that fail delivery forever; keying on the
    device replaces the old token in place. The token's shape is validated in the
    schema, because an invalid token fails silently at send time — the worst kind
    of failure for an alerting system.
    """
    row = await push_tokens_repo.upsert(
        device_id,
        token=payload.token,
        platform=payload.platform,
        lat=payload.lat,
        lon=payload.lon,
    )
    if not row:
        raise ValidationError("The push token could not be saved. Please try again.")
    return {"status": "registered", "detail": "Alerts are enabled for this device."}


@router.delete(
    "/register",
    response_model=SimpleStatus,
    dependencies=[Depends(write_limit)],
    summary="Stop alerts for this device",
)
async def unregister(device_id: str = Depends(require_device_id)) -> Dict[str, Any]:
    """Remove the token. Idempotent — unsubscribing twice is not an error."""
    await push_tokens_repo.delete(device_id)
    return {"status": "unregistered", "detail": "Alerts are disabled for this device."}


@router.get(
    "/preferences",
    response_model=AlertPreferences,
    dependencies=[Depends(read_limit)],
    summary="Read this device's alert preferences",
)
async def read_preferences(device_id: str = Depends(require_device_id)) -> Dict[str, Any]:
    """What this device will and will not be woken for.

    Includes the two server-side policy values the screen needs to describe
    itself honestly: which severity overrides quiet hours, and whether this
    deployment dispatches at all.
    """
    return _preferences(await push_tokens_repo.get(device_id))


@router.patch(
    "/preferences",
    response_model=AlertPreferences,
    dependencies=[Depends(write_limit)],
    summary="Update this device's alert preferences",
)
async def update_preferences(
    payload: AlertPreferencesUpdate,
    device_id: str = Depends(require_device_id),
) -> Dict[str, Any]:
    """Change some preferences, leaving the rest alone.

    PATCH rather than PUT, and partial rather than whole-object, because the
    preferences live on the same row as the push token: a full replacement sent by
    a screen holding stale state would silently undo a change made moments
    earlier, and there is no way for the user to tell that happened.

    A 404 here — unlike on the GET — is correct rather than unhelpful. There is no
    row to update until the device has registered a token, and quietly creating
    one would mean storing preferences for a device that cannot receive anything.
    """
    quiet_window = payload.quiet_hours_start is not None
    if quiet_window and not payload.timezone:
        # A quiet window without a zone is not a partial setting, it is an
        # inert one: `alert_dispatch.in_quiet_hours` cannot evaluate "22:00 local"
        # without knowing whose local, so it delivers. Rejecting the pair is the
        # only way the user learns that now rather than at 3 a.m.
        current = await push_tokens_repo.get(device_id)
        if not current:
            raise NotFoundError(_NOT_REGISTERED)
        if not current.get("timezone"):
            raise ValidationError(
                "Quiet hours need a timezone. Send `timezone` with an IANA name "
                "such as Asia/Kolkata alongside the quiet window."
            )

    row = await push_tokens_repo.update_preferences(
        device_id,
        alerts_enabled=payload.alerts_enabled,
        min_severity=payload.min_severity,
        quiet_hours_start=payload.quiet_hours_start,
        quiet_hours_end=payload.quiet_hours_end,
        timezone_name=payload.timezone,
        clear_quiet_hours=payload.clears_quiet_hours,
    )
    if not row:
        raise NotFoundError(_NOT_REGISTERED)
    return _preferences(row)
