"""Shared test fixtures.

Read this before adding a test; a few of the choices here are load-bearing.

**Environment before imports.** ``app.core.config`` builds its settings
singleton at *import* time and ``Settings._validate_production`` refuses to
construct on an unsafe configuration. The environment therefore has to be in
place before the first ``app.*`` import, which is why the ``os.environ`` block
sits above the imports with ``# noqa: E402``.

**Never rebuild the settings object.** Modules do ``from app.core.config import
settings``, so they each hold a reference to that one instance. Calling
``get_settings.cache_clear()`` and constructing a new ``Settings`` would produce
an object nothing looks at, and the test would appear to have no effect. To
change configuration for one test, mutate the singleton through
``monkeypatch.setattr(settings, "field", value)`` — the ``settings_override``
fixture wraps that.

**Nothing here talks to the network or to Supabase.** Outbound HTTP goes through
an ``httpx.MockTransport`` installed on the pooled client, and the database is an
in-memory fake injected with ``app.db.supabase.set_client()``. Both are asserted
at teardown: an outbound request that no test stubbed fails the test that made
it, so the suite cannot drift into depending on a live third-party API.

What the previous version of this file did, for the record, since each of these
was a real source of false confidence:

* ``import server`` and ``server._rate_buckets.clear()`` in an autouse fixture.
  ``server.py`` is now a deprecation shim with no such attribute, so every test
  errored in setup. Worse, importing the shim raises ``DeprecationWarning``,
  and ``filterwarnings = ["error", ...]`` in ``pyproject.toml`` makes that fatal.
* ``sys.modules["google"] = MagicMock()`` — a fake package left installed
  process-wide for the rest of the session, so any module that later imported
  ``google.*`` got a mock without knowing it.
* ``sys.modules["supabase"] = supabase_mock`` — patched the *import* rather than
  using the injection point ``app.db.supabase.set_client()`` exists for, and made
  every query return a mock that ignored its filters.
* ``@pytest.fixture(scope="session")`` on the client, so a report created by one
  test was visible to the next and the suite passed or failed on ordering.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Dict, Iterator, List, TypeVar

import bcrypt

# ---------------------------------------------------------------------------
# Environment. Must precede every `app.*` import — see the module docstring.
# ---------------------------------------------------------------------------

#: The plaintext used by the admin fixtures. Only ever exists in the test
#: process; what the app sees is the bcrypt hash below.
ADMIN_USERNAME = "test-admin"
ADMIN_PASSWORD = "correct-horse-battery-staple"

# Cost 4 instead of the production 12. bcrypt stores the cost factor inside the
# hash, so `checkpw` still verifies correctly — but the admin login path is
# exercised on many requests here and 12 rounds is ~250ms of pure CPU each time.
_ADMIN_PASSWORD_HASH = bcrypt.hashpw(
    ADMIN_PASSWORD.encode("utf-8"), bcrypt.gensalt(rounds=4)
).decode("utf-8")

_TEST_ENVIRONMENT: Dict[str, str] = {
    # `test` rather than `development`: it switches off the production rails
    # without pretending to be a developer's laptop, and `settings.is_test` is
    # readable in the code under test.
    "ENVIRONMENT": "test",
    "SERVICE_NAME": "sentinelai-api-test",
    "RELEASE": "test",
    "LOG_LEVEL": "WARNING",
    # JSON log lines in pytest output are unreadable; console format keeps a
    # failure's context legible.
    "LOG_FORMAT": "console",
    # Enumerated rather than "*", so `allow_credentials` is true and the CORS
    # tests exercise the configuration a real deployment uses.
    "ALLOWED_ORIGINS": "http://localhost:5173,https://admin.example.test",
    # TestClient sends `Host: testserver`. Without it here, TrustedHostMiddleware
    # answers every request with 400 and every test fails for the same reason.
    "TRUSTED_HOSTS": "testserver,localhost,127.0.0.1",
    "ENABLE_DOCS": "true",
    "ENABLE_HSTS": "false",
    # Present so `has_supabase` is true and the feature is considered configured.
    # No client is ever built from these — `set_client()` injects the fake first.
    "SUPABASE_URL": "https://project.supabase.test",
    "SUPABASE_KEY": "test-service-role-key",
    "WAQI_TOKEN": "test-waqi-token",
    # Absent by default, so the deterministic fallback is what tests see unless
    # a test opts into the Gemini path explicitly.
    "GEMINI_API_KEY": "",
    "CONTACT_EMAIL": "ops@example.test",
    "ADMIN_USERNAME": ADMIN_USERNAME,
    "ADMIN_PASSWORD_HASH": _ADMIN_PASSWORD_HASH,
    # 32+ characters: `_validate_production` rejects anything shorter, and a test
    # secret that would fail that check is a test secret that hides the rule.
    "ADMIN_JWT_SECRET": "test-jwt-secret-not-used-anywhere-else-0123456789",
    "ADMIN_TOKEN_TTL_MINUTES": "60",
    # Memory backends. A developer with REDIS_URL exported would otherwise have
    # tests reading and writing their real Redis.
    "REDIS_URL": "",
    # Off by default so a test that makes 200 requests doesn't 429 on request
    # 121 for reasons unrelated to what it is checking. The `rate_limits`
    # fixture turns it on for the tests that are about rate limiting.
    "RATE_LIMIT_ENABLED": "false",
    "RATE_LIMIT_READ": "120",
    "RATE_LIMIT_WRITE": "20",
    "RATE_LIMIT_AI": "10",
    "RATE_LIMIT_AUTH": "5",
    "RATE_LIMIT_WINDOW_SECONDS": "60",
    # No retries and no backoff sleeps: a test for "upstream is down" would
    # otherwise take three attempts and several seconds of jittered waiting.
    # The retry logic itself is tested directly in test_core_http.py.
    "HTTP_MAX_RETRIES": "0",
    "HTTP_TIMEOUT_SECONDS": "5",
    "TRUSTED_PROXY_HOPS": "0",
}

# Overwritten, not `setdefault`: a value already exported in the developer's
# shell (or a stale `backend/.env`) must not change what the suite tests. This
# is also why `SUPABASE_KEY` is set rather than defaulted — a real key in the
# ambient environment would otherwise be used to build a real client.
os.environ.update(_TEST_ENVIRONMENT)

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import http as http_module  # noqa: E402
from app.core.admin_auth import create_access_token  # noqa: E402
from app.core.cache import cache  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.rate_limit import limiter  # noqa: E402
from app.core.security import DEVICE_ID_HEADER  # noqa: E402
from app.db import supabase  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.fakes import FakeSupabaseClient, UpstreamRouter  # noqa: E402

# ---------------------------------------------------------------------------
# Process-wide hygiene
# ---------------------------------------------------------------------------


T = TypeVar("T")


def run_async(awaitable: Awaitable[T]) -> T:
    """Await a coroutine from synchronous test or fixture code.

    ``asyncio.run`` is safe for the handful of things called this way — clearing
    the in-memory cache, resetting the in-memory limiter, closing a mock-transport
    HTTP client — because none of them perform I/O or hold state bound to a
    particular event loop. It would not be safe for anything holding a real Redis
    or TCP connection, which is part of why ``REDIS_URL`` is forced empty above.
    """

    async def _run() -> T:
        return await awaitable

    return asyncio.run(_run())


@pytest.fixture(autouse=True)
def _reset_shared_state() -> Iterator[None]:
    """Return every process-wide singleton to a known state around each test.

    All of these outlive a single request by design, which is exactly why a test
    must not inherit them: a cached AQI response, a spent rate-limit budget, or a
    host's last-request timestamp would make the outcome depend on which tests
    ran first.

    Done both before and after, so a test is protected from its predecessors
    even when one of them fails partway through.
    """
    _reset_now()
    yield
    _reset_now()


def _reset_now() -> None:
    run_async(cache.clear())
    run_async(limiter.reset())
    # Per-host pacing (Nominatim's 1 req/s policy) is real behaviour we want in
    # production and pure latency in a test.
    http_module._throttle._last_request.clear()


@pytest.fixture(autouse=True)
def _no_host_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the deliberate inter-request delay for throttled hosts.

    ``_HOST_MIN_INTERVAL`` makes two consecutive Nominatim calls sleep 1.1s
    between them. Correct against the real service; a minute of dead time across
    a suite that never leaves the process.
    """
    monkeypatch.setattr(http_module, "_HOST_MIN_INTERVAL", {})


# ---------------------------------------------------------------------------
# Outbound HTTP
# ---------------------------------------------------------------------------


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> Iterator[UpstreamRouter]:
    """Install a mock transport on the pooled HTTP client.

    Patching at the transport layer rather than stubbing each service's
    ``get_json`` means the code under test keeps running for real: URL and query
    construction, the retry and status mapping in ``request_json``, per-host
    throttling, and the ``UpstreamError`` -> problem+json translation. Stubbing
    ``app.services.weather.get_json`` would skip all of it and assert only that
    the service reshapes a dict it was handed.
    """
    router = UpstreamRouter()
    client = httpx.AsyncClient(
        transport=router.transport(),
        headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
        follow_redirects=True,
    )
    # Assigned before the app's lifespan runs; `startup_http()` only builds a
    # client when the slot is empty, so it leaves this one alone.
    monkeypatch.setattr(http_module, "_client", client)
    yield router
    # The app's lifespan closes and clears `_client` on shutdown, which happens
    # before this teardown because `client` depends on `app` depends on this
    # fixture. If a test used the router without a TestClient, close it here.
    if http_module._client is client:
        run_async(client.aclose())
    # monkeypatch restores the previous value of `_client` after this returns.

    unmatched = router.unmatched
    assert not unmatched, (
        "outbound HTTP with no route registered in this test:\n  "
        + "\n  ".join(unmatched)
        + "\n\nRegister it on the `upstream` fixture. Every external call must be "
        "stubbed — otherwise this suite quietly starts depending on a third-party "
        "API being up."
    )


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> Iterator[FakeSupabaseClient]:
    """Inject the in-memory database double.

    ``set_client`` also sets the module's ``_init_attempted`` flag, so
    ``get_client()`` never tries to build a real client during the app's
    lifespan.
    """
    fake = FakeSupabaseClient()
    supabase.set_client(fake)
    yield fake
    supabase.reset_client()


@pytest.fixture
def no_db(db: FakeSupabaseClient) -> Iterator[None]:
    """Run with the database explicitly unavailable.

    This is a supported production state, not a broken one: the app serves every
    upstream read without Supabase and only persistence degrades. The endpoints
    that need it must answer 503 with a message that says so, rather than 500.

    Depends on ``db`` deliberately, even though it throws the fake away. Fixtures
    of the same scope are set up in request order, so without this a test written
    as ``def test_x(no_db, client)`` would have ``db`` — pulled in later via
    ``app`` — reinstall the fake and quietly assert nothing. Taking ``db`` as a
    parameter forces this fixture to run after it either way.
    """
    supabase.set_client(None)
    yield
    supabase.reset_client()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


@pytest.fixture
def app(db: FakeSupabaseClient, upstream: UpstreamRouter) -> FastAPI:
    """A freshly built app, so middleware reflects the current settings.

    Built per test rather than imported from ``app.main``: ``create_app()`` reads
    ``settings.trusted_hosts`` and ``settings.docs_enabled`` when it assembles the
    middleware stack, so a test that changes either has to construct the app
    afterwards for the change to mean anything.
    """
    return create_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """HTTP client for the app, with the lifespan run.

    ``raise_server_exceptions=False`` so that an unhandled exception is observed
    the way a caller observes it — a 500 with a problem+json body and a request
    id — instead of being re-raised into the test. The contract under test is the
    response, including the failure responses.

    Function-scoped. A session-scoped client shares the fake database and the
    cache across every test in the file.
    """
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def strict_client(app: FastAPI) -> Iterator[TestClient]:
    """Same app, but an unhandled exception is re-raised into the test.

    Useful while writing a test: a 500 tells you something broke, this tells you
    where.
    """
    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@pytest.fixture
def device_id() -> str:
    """A device id that satisfies ``_DEVICE_ID_RE`` (16-64 url-safe chars)."""
    return uuid.uuid4().hex


@pytest.fixture
def other_device_id() -> str:
    """A second device, for the tests that prove one device cannot see another's rows."""
    return uuid.uuid4().hex


@pytest.fixture
def device_headers(device_id: str) -> Dict[str, str]:
    return {DEVICE_ID_HEADER: device_id}


@pytest.fixture
def other_device_headers(other_device_id: str) -> Dict[str, str]:
    return {DEVICE_ID_HEADER: other_device_id}


@pytest.fixture
def admin_credentials() -> Dict[str, str]:
    return {"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}


@pytest.fixture
def admin_token() -> str:
    """A valid admin JWT, minted directly.

    Minting rather than posting to ``/admin/login`` keeps the ~200 tests that
    merely need to be authenticated from also depending on the login endpoint,
    and from spending a bcrypt verification each. ``test_admin_auth.py`` covers
    the login flow itself.
    """
    return str(create_access_token(ADMIN_USERNAME)["access_token"])


@pytest.fixture
def admin_headers(admin_token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {admin_token}"}


# ---------------------------------------------------------------------------
# Configuration overrides
# ---------------------------------------------------------------------------


@pytest.fixture
def settings_override(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Change settings for one test, with automatic restoration.

    Mutates the existing singleton on purpose. Every module holds a reference to
    that object, so replacing it would have no effect on the code under test.

        def test_x(settings_override, client):
            settings_override(rate_limit_enabled=True, rate_limit_read=2)
    """

    def apply(**values: Any) -> None:
        for field, value in values.items():
            if not hasattr(settings, field):
                raise AssertionError(
                    f"Settings has no field {field!r}; check the spelling against "
                    "app/core/config.py rather than adding an attribute that the "
                    "application never reads."
                )
            monkeypatch.setattr(settings, field, value)

    return apply


@pytest.fixture
def rate_limits(settings_override: Any) -> Any:
    """Turn rate limiting on with a small budget.

    rate_limits(read=2)   # third read in the window is a 429
    """

    def apply(*, read: int = 2, write: int = 2, ai: int = 2, auth: int = 2) -> None:
        settings_override(
            rate_limit_enabled=True,
            rate_limit_read=read,
            rate_limit_write=write,
            rate_limit_ai=ai,
            rate_limit_auth=auth,
            rate_limit_window_seconds=60,
        )

    return apply


@pytest.fixture
def no_admin_auth(settings_override: Any) -> None:
    """Deployment with the admin dashboard not configured.

    Every ``/api/admin/*`` route must then answer 503 with an explanation, not
    401 — the operator's password is not the problem, and telling them it is
    sends them to reset a credential that would not have helped.
    """
    settings_override(admin_username="")


# ---------------------------------------------------------------------------
# Assertions shared across files
# ---------------------------------------------------------------------------


def assert_problem(
    response: httpx.Response,
    status: int,
    *,
    code: str | None = None,
) -> Dict[str, Any]:
    """Assert an RFC 7807 error response and return its body.

    Checks the media type as well as the shape, because a handler that returns
    the right JSON with ``application/json`` has still broken the contract the
    mobile app's error parser relies on.
    """
    assert response.status_code == status, response.text
    content_type = response.headers.get("content-type", "")
    assert content_type.startswith("application/problem+json"), content_type
    body = response.json()
    for field in ("type", "title", "status", "detail", "code"):
        assert field in body, f"problem+json is missing {field!r}: {body}"
    assert body["status"] == status
    if code is not None:
        assert body["code"] == code, body
    # Every error must be correlatable with a log line, and the id in the body
    # must be the same one the header advertises — support reads one, the log
    # search uses the other.
    request_id = response.headers.get("X-Request-ID")
    assert request_id, "response carries no X-Request-ID"
    assert body.get("request_id") == request_id, body
    return dict(body)


def iso_ago(hours: float = 1.0) -> str:
    """A timestamp ``hours`` in the past, in the format the repositories write.

    Seed data must be *relative*, not a hardcoded date. ``list_nearby`` filters
    on ``created_at >= now - max_age_hours``, so a fixed literal would sit inside
    the window on the day it was written and outside it a week later — the suite
    would start failing on a calendar boundary with no code change.
    """
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def report_row(
    device_id: str,
    *,
    title: str = "Seeded report",
    category: str = "flood",
    severity: str = "Moderate",
    status: str = "visible",
    lat: float = 28.6139,
    lon: float = 77.2090,
    created_at: str | None = None,
    **extra: Any,
) -> Dict[str, Any]:
    """A community_reports row, for seeding. Defaults are all valid."""
    created_at = created_at or iso_ago(1)
    row: Dict[str, Any] = {
        "device_id": device_id,
        "category": category,
        "title": title,
        "description": None,
        "lat": lat,
        "lon": lon,
        "severity": severity,
        "status": status,
        "upvotes": 0,
        "created_at": created_at,
        "updated_at": created_at,
    }
    row.update(extra)
    return row


def favorite_row(
    device_id: str,
    *,
    name: str = "Home",
    lat: float = 28.6139,
    lon: float = 77.2090,
    station_uid: str | None = None,
    alert_radius_km: float = 100.0,
    created_at: str | None = None,
    **extra: Any,
) -> Dict[str, Any]:
    """A favorites row, for seeding."""
    created_at = created_at or iso_ago(1)
    row: Dict[str, Any] = {
        "device_id": device_id,
        "name": name,
        "lat": lat,
        "lon": lon,
        "station_uid": station_uid,
        "alert_radius_km": alert_radius_km,
        "created_at": created_at,
    }
    row.update(extra)
    return row


def ids(rows: List[Dict[str, Any]]) -> List[str]:
    return [str(row["id"]) for row in rows]
