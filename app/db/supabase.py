"""Supabase client and blocking-call bridge.

``supabase-py`` is a synchronous library. Calling it directly from an ``async
def`` handler blocks the event loop for the duration of the round trip, which
stalls every other in-flight request on that worker — the kind of problem that
only shows up under concurrency. Every query here is therefore dispatched to a
worker thread via :func:`run_db`.

:func:`run_db` also converts driver exceptions into an application error, so a
database outage returns a clean 503 instead of a 500 with a stack trace.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, TypeVar

import anyio.to_thread

from app.core.config import settings
from app.core.errors import ServiceUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

_client: Optional[Any] = None
_init_attempted = False


def get_client() -> Optional[Any]:
    """Return the Supabase client, or ``None`` when it isn't configured.

    ``None`` is a supported state: the app still serves every read-only
    upstream endpoint (AQI, disasters, weather) without a database. Only
    persistence features degrade.
    """
    global _client, _init_attempted
    if _client is not None or _init_attempted:
        return _client

    _init_attempted = True
    if not settings.has_supabase:
        logger.warning(
            "Supabase is not configured; favourites, reports and push tokens "
            "will be unavailable. Set SUPABASE_URL and SUPABASE_KEY to enable them."
        )
        return None

    try:
        from supabase import create_client

        _client = create_client(settings.supabase_url, settings.supabase_key.get_secret_value())
        logger.info("Supabase client initialised")
    except Exception as exc:
        logger.error("Failed to initialise Supabase client: %s", exc)
        _client = None
    return _client


def reset_client() -> None:
    """Drop the cached client. Used by tests to inject a fake."""
    global _client, _init_attempted
    _client = None
    _init_attempted = False


def set_client(client: Optional[Any]) -> None:
    """Inject a client directly. Used by tests."""
    global _client, _init_attempted
    _client = client
    _init_attempted = True


def is_available() -> bool:
    return get_client() is not None


def require_client() -> Any:
    client = get_client()
    if client is None:
        raise ServiceUnavailableError(
            "This feature requires database storage, which is not configured on this deployment."
        )
    return client


async def run_db(label: str, fn: Callable[[], T]) -> T:
    """Run a blocking Supabase call in a worker thread.

    Args:
        label: Short operation name, used in logs.
        fn: Zero-argument callable performing the query.
    """
    try:
        return await anyio.to_thread.run_sync(fn)
    except Exception as exc:
        logger.error(
            "Database operation failed",
            extra={"operation": label, "error": str(exc)},
        )
        raise ServiceUnavailableError(
            "The database is temporarily unavailable. Please try again."
        ) from exc


async def ping() -> bool:
    """Lightweight connectivity probe for the readiness endpoint."""
    client = get_client()
    if client is None:
        return False

    def _probe() -> bool:
        client.table("favorites").select("id").limit(1).execute()
        return True

    try:
        return await anyio.to_thread.run_sync(_probe)
    except Exception as exc:
        logger.warning("Database ping failed: %s", exc)
        return False


__all__ = [
    "get_client",
    "is_available",
    "ping",
    "require_client",
    "reset_client",
    "run_db",
    "set_client",
]
