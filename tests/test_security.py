"""Security guarantees.

This file exists to make specific past defects permanently untestable-as-absent.
Each section names the thing it prevents:

* **Cross-device data access.** ``GET /api/favorites`` once selected the whole
  table with no filter, so every user saw every user's saved locations, and
  ``DELETE /api/favorites/{uid}`` deleted by *station* id with no ownership
  check. The tests below assert both the response and the query that produced it
  — a handler that returns the right rows because the table happens to hold only
  one device's data would pass a response-shape assertion and still be wrong.
* **Privilege via a spoofable header.** A device id is client-supplied. It
  separates users from one another; it must never open an admin route.
* **Host-header injection, oversized bodies, log injection, CORS drift, and
  secret leakage into responses.**

The device-id header is checked here rather than in each endpoint's own file
because it is one rule applied to every user-owned route, and a new route that
forgets it should fail *this* file.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.admin_auth import ALGORITHM, AUDIENCE, ISSUER
from app.core.config import settings
from app.core.security import DEVICE_ID_HEADER, MAX_BODY_BYTES, BodySizeLimitMiddleware
from app.main import create_app
from tests.conftest import assert_problem, favorite_row, report_row

# Routes that read or write data owned by one device. Table-driven so that
# adding a device-scoped endpoint without a device-id check fails here.
DEVICE_SCOPED_ROUTES: List[Any] = [
    ("GET", "/api/favorites", None),
    ("POST", "/api/favorites", {"name": "Home", "lat": 28.61, "lon": 77.21}),
    ("GET", "/api/favorites/alerts", None),
    ("DELETE", f"/api/favorites/{uuid.uuid4()}", None),
    (
        "POST",
        "/api/reports",
        {"category": "flood", "title": "Water on the road", "lat": 28.61, "lon": 77.21},
    ),
    ("POST", f"/api/reports/{uuid.uuid4()}/vote", None),
    ("DELETE", f"/api/reports/{uuid.uuid4()}", None),
    (
        "POST",
        "/api/notifications/register",
        {"token": "ExponentPushToken[abcdefghijklmnop]"},
    ),
    ("DELETE", "/api/notifications/register", None),
    ("GET", "/api/notifications/preferences", None),
    ("PATCH", "/api/notifications/preferences", {}),
]


# ---------------------------------------------------------------------------
# Device identity is required, and validated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), DEVICE_SCOPED_ROUTES)
def test_device_scoped_routes_reject_a_missing_device_id(
    client: TestClient, method: str, path: str, body: Any
) -> None:
    """No device id, no access to user-owned data — on every such route.

    422 rather than 401: nothing was rejected for lack of *authorisation*. The
    request is malformed, and the message says which header to send.
    """
    response = client.request(method, path, json=body)
    body_json = assert_problem(response, 422, code="validation_error")
    assert DEVICE_ID_HEADER in body_json["detail"]


@pytest.mark.parametrize(
    "value",
    [
        "",  # header present but empty
        "short",  # under 16 characters
        "x" * 65,  # over 64
        "has spaces in it",
        "semi;colon;injection",
        "../../etc/passwd",
        "null",
    ],
)
def test_malformed_device_ids_are_rejected(client: TestClient, value: str) -> None:
    """A device id is used as a database filter value, so its shape is validated
    before it reaches a query rather than after."""
    response = client.get("/api/favorites", headers={DEVICE_ID_HEADER: value})
    assert_problem(response, 422, code="validation_error")


def test_device_id_case_is_normalised(client: TestClient, db: Any) -> None:
    """The same install must not become two identities through header casing.

    ``normalise_device_id`` lowercases, so a client that upper-cases its stored
    UUID still sees its own rows instead of an empty list.
    """
    lower = uuid.uuid4().hex
    db.seed("favorites", favorite_row(lower, name="Home"))

    response = client.get("/api/favorites", headers={DEVICE_ID_HEADER: lower.upper()})

    assert response.status_code == 200
    assert [f["name"] for f in response.json()["favorites"]] == ["Home"]


# ---------------------------------------------------------------------------
# One device cannot read another's rows
# ---------------------------------------------------------------------------


def test_favorites_are_not_shared_between_devices(
    client: TestClient,
    db: Any,
    device_id: str,
    other_device_id: str,
    device_headers: Dict[str, str],
) -> None:
    """The original defect, asserted directly: two devices, two answers."""
    db.seed("favorites", favorite_row(device_id, name="Mine"))
    db.seed("favorites", favorite_row(other_device_id, name="Theirs", lat=19.07, lon=72.87))

    response = client.get("/api/favorites", headers=device_headers)

    assert response.status_code == 200
    payload = response.json()
    names = [f["name"] for f in payload["favorites"]]
    assert names == ["Mine"]
    assert payload["count"] == 1
    # And the query itself was scoped — not merely the result set.
    select = db.queries_for("favorites", "select")[-1]
    assert select.filtered_on("device_id")
    assert select.value_for("device_id") == device_id


def test_favorites_list_query_is_always_device_filtered(
    client: TestClient, db: Any, device_headers: Dict[str, str]
) -> None:
    """Passes even with an empty table, which is the point.

    A response-shape assertion cannot distinguish "filtered correctly" from
    "there was nothing else to return". This looks at the query.
    """
    response = client.get("/api/favorites", headers=device_headers)

    assert response.status_code == 200
    assert response.json()["favorites"] == []
    assert all(q.filtered_on("device_id") for q in db.queries_for("favorites"))


def test_a_device_cannot_delete_another_devices_favorite(
    client: TestClient,
    db: Any,
    device_headers: Dict[str, str],
    other_device_id: str,
) -> None:
    """404, the row survives, and the delete carried an owner filter.

    404 rather than 403 on purpose: 403 would confirm that the id exists, which
    turns a guessable id into an enumeration oracle.
    """
    theirs = db.seed("favorites", favorite_row(other_device_id, name="Theirs"))[0]

    response = client.delete(f"/api/favorites/{theirs['id']}", headers=device_headers)

    assert_problem(response, 404, code="not_found")
    assert db.count("favorites") == 1
    delete = db.queries_for("favorites", "delete")[-1]
    assert delete.filtered_on("device_id"), (
        "the delete was not scoped by device_id; it would have removed another "
        "device's row if the id had matched"
    )
    assert delete.filtered_on("id")


def test_a_device_cannot_delete_another_devices_report(
    client: TestClient,
    db: Any,
    device_headers: Dict[str, str],
    other_device_id: str,
) -> None:
    theirs = db.seed("community_reports", report_row(other_device_id))[0]

    response = client.delete(f"/api/reports/{theirs['id']}", headers=device_headers)

    assert_problem(response, 404, code="not_found")
    assert db.count("community_reports") == 1
    delete = db.queries_for("community_reports", "delete")[-1]
    assert delete.filtered_on("device_id")


def test_report_feed_never_serialises_a_device_id(
    client: TestClient,
    db: Any,
    device_id: str,
    other_device_id: str,
    device_headers: Dict[str, str],
) -> None:
    """Reports are a public feed, so ownership is exposed as a boolean only.

    A device id is the closest thing this app has to a user identifier. Echoing
    another device's id would let any client build a map of who reported what,
    and would let it delete their reports by replaying the id as its own header.
    """
    db.seed("community_reports", report_row(device_id, title="Mine"))
    db.seed("community_reports", report_row(other_device_id, title="Theirs"))

    response = client.get(
        "/api/reports", params={"lat": 28.6139, "lon": 77.2090}, headers=device_headers
    )

    assert response.status_code == 200
    reports = response.json()["reports"]
    assert len(reports) == 2
    assert other_device_id not in response.text
    assert device_id not in response.text
    by_title = {r["title"]: r for r in reports}
    assert by_title["Mine"]["is_mine"] is True
    assert by_title["Theirs"]["is_mine"] is False
    assert all("device_id" not in r for r in reports)


def test_favorite_payload_never_serialises_a_device_id(
    client: TestClient, db: Any, device_id: str, device_headers: Dict[str, str]
) -> None:
    db.seed("favorites", favorite_row(device_id))

    response = client.get("/api/favorites", headers=device_headers)

    assert response.status_code == 200
    assert device_id not in response.text
    assert all("device_id" not in f for f in response.json()["favorites"])


# ---------------------------------------------------------------------------
# A device id is not a credential
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/admin/me"),
        ("GET", "/api/admin/stats"),
        ("GET", "/api/admin/reports"),
        ("PATCH", f"/api/admin/reports/{uuid.uuid4()}"),
    ],
)
def test_admin_routes_reject_a_device_id_as_authentication(
    client: TestClient, device_headers: Dict[str, str], method: str, path: str
) -> None:
    """The header that identifies a user must not authorise a moderator.

    A client can send any device id it likes, so if this ever returned 200 the
    moderation surface would be open to everyone.
    """
    response = client.request(method, path, headers=device_headers, json={"status": "hidden"})
    assert_problem(response, 401, code="unauthorized")


@pytest.mark.parametrize(
    "authorization",
    [
        "",
        "Bearer ",
        "Bearer not-a-jwt",
        "Basic dGVzdC1hZG1pbjpwYXNzd29yZA==",
        "bearer eyJhbGciOiJub25lIn0.e30.",  # alg=none
    ],
)
def test_admin_rejects_unusable_authorization_headers(
    client: TestClient, authorization: str
) -> None:
    response = client.get("/api/admin/stats", headers={"Authorization": authorization})
    assert_problem(response, 401, code="unauthorized")


def test_admin_rejects_an_expired_token(client: TestClient) -> None:
    """Expiry is enforced server-side, not by the dashboard hiding a button."""
    past = datetime.now(UTC) - timedelta(minutes=5)
    token = jwt.encode(
        {
            "sub": "test-admin",
            "role": "admin",
            "iat": int((past - timedelta(minutes=1)).timestamp()),
            "exp": int(past.timestamp()),
            "aud": AUDIENCE,
            "iss": ISSUER,
        },
        settings.admin_jwt_secret.get_secret_value(),
        algorithm=ALGORITHM,
    )

    response = client.get("/api/admin/stats", headers={"Authorization": f"Bearer {token}"})

    body = assert_problem(response, 401, code="unauthorized")
    assert "expired" in body["detail"].lower()


def test_admin_rejects_a_token_signed_with_another_secret(client: TestClient) -> None:
    """Signature verification, asserted. A token this app did not issue is not
    accepted just because its claims look right."""
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "test-admin",
            "role": "admin",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=30)).timestamp()),
            "aud": AUDIENCE,
            "iss": ISSUER,
        },
        "an-attackers-own-signing-secret-32-chars",
        algorithm=ALGORITHM,
    )

    response = client.get("/api/admin/stats", headers={"Authorization": f"Bearer {token}"})
    assert_problem(response, 401, code="unauthorized")


def test_admin_rejects_a_correctly_signed_non_admin_token(client: TestClient) -> None:
    """``role`` is checked separately from the signature.

    There is only one role today, but the check is what keeps a future
    lower-privilege token from inheriting moderation rights by default.
    """
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "someone",
            "role": "viewer",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=30)).timestamp()),
            "aud": AUDIENCE,
            "iss": ISSUER,
        },
        settings.admin_jwt_secret.get_secret_value(),
        algorithm=ALGORITHM,
    )

    response = client.get("/api/admin/stats", headers={"Authorization": f"Bearer {token}"})
    assert_problem(response, 401, code="unauthorized")


def test_admin_rejects_a_token_minted_for_another_audience(client: TestClient) -> None:
    """The audience claim stops a token issued for a different service — signed
    with a shared secret — from being replayed here."""
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "test-admin",
            "role": "admin",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=30)).timestamp()),
            "aud": "some-other-service",
            "iss": ISSUER,
        },
        settings.admin_jwt_secret.get_secret_value(),
        algorithm=ALGORITHM,
    )

    response = client.get("/api/admin/stats", headers={"Authorization": f"Bearer {token}"})
    assert_problem(response, 401, code="unauthorized")


def test_unconfigured_admin_auth_reports_a_missing_feature_not_bad_credentials(
    no_admin_auth: None, client: TestClient, admin_headers: Dict[str, str]
) -> None:
    """503 ``feature_unavailable``, not 401.

    Telling an operator their credentials are wrong when the deployment has no
    admin credentials configured sends them to reset a password that would never
    have worked. Note this holds even with a validly signed token.
    """
    response = client.get("/api/admin/stats", headers=admin_headers)
    body = assert_problem(response, 503, code="feature_unavailable")
    assert "ADMIN_USERNAME" in body["detail"]


# ---------------------------------------------------------------------------
# Response headers
# ---------------------------------------------------------------------------


def test_hardening_headers_are_present_on_every_response(client: TestClient) -> None:
    response = client.get("/api")

    assert response.status_code == 200
    headers = response.headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert headers["X-Permitted-Cross-Domain-Policies"] == "none"
    assert "geolocation=()" in headers["Permissions-Policy"]
    # A JSON API needs no script, style or image sources at all.
    assert headers["Content-Security-Policy"].startswith("default-src 'none'")
    # Nothing here belongs in a shared cache; upstream data is cached
    # server-side with explicit TTLs instead.
    assert headers["Cache-Control"] == "no-store"


def test_hardening_headers_are_present_on_error_responses(client: TestClient) -> None:
    """An error response is the one most likely to be rendered by a browser, so
    it is the one that most needs the headers."""
    response = client.get("/api/favorites")

    assert response.status_code == 422
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"].startswith("default-src 'none'")


def test_docs_get_a_wider_csp_than_the_api(client: TestClient) -> None:
    """Swagger UI loads from a CDN, so exactly one page relaxes the policy — and
    the relaxation does not leak to any other path."""
    docs = client.get("/api/docs")
    assert docs.status_code == 200
    assert "cdn.jsdelivr.net" in docs.headers["Content-Security-Policy"]

    other = client.get("/api")
    assert "cdn.jsdelivr.net" not in other.headers["Content-Security-Policy"]


def test_hsts_is_off_by_default_and_opt_in(client: TestClient, settings_override: Any) -> None:
    """HSTS on a plain-HTTP local deployment locks a developer's browser out of
    ``localhost`` for a year, so it follows configuration rather than defaulting on."""
    assert "Strict-Transport-Security" not in client.get("/api").headers

    settings_override(enable_hsts=True)
    header = client.get("/api").headers["Strict-Transport-Security"]
    assert "max-age=31536000" in header
    assert "includeSubDomains" in header


def test_every_response_carries_a_request_id(client: TestClient) -> None:
    response = client.get("/api")
    assert len(response.headers["X-Request-ID"]) == 32
    assert "Server-Timing" in response.headers


def test_a_well_formed_client_request_id_is_preserved(client: TestClient) -> None:
    """Lets a mobile client correlate its own retry attempts with server logs."""
    response = client.get("/api", headers={"X-Request-ID": "mobile-42_abc.DEF"})
    assert response.headers["X-Request-ID"] == "mobile-42_abc.DEF"


@pytest.mark.parametrize(
    "supplied",
    [
        "id with spaces",
        "id\twith\ttabs",
        'injected" status=200 path="/admin',
        "x" * 65,
    ],
)
def test_a_hostile_client_request_id_is_replaced(client: TestClient, supplied: str) -> None:
    """The request id is interpolated into structured log lines, so an
    unvalidated one lets a client forge log fields."""
    response = client.get("/api", headers={"X-Request-ID": supplied})
    returned = response.headers["X-Request-ID"]
    assert returned != supplied
    assert len(returned) == 32


def test_error_bodies_are_correlatable_with_a_log_line(client: TestClient) -> None:
    """``assert_problem`` enforces this everywhere; stated once explicitly
    because it is the mechanism support uses to trace a user's screenshot."""
    response = client.get("/api/favorites")
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


# ---------------------------------------------------------------------------
# Request body limits
# ---------------------------------------------------------------------------


def test_an_oversized_body_is_rejected_before_it_is_parsed(
    client: TestClient, device_headers: Dict[str, str], db: Any
) -> None:
    """413 on Content-Length, so the server never buffers the payload.

    Asserting the database was untouched is the real check: it proves the
    rejection happened ahead of the handler rather than inside it.
    """
    oversized = b'{"title": "' + b"x" * (MAX_BODY_BYTES + 1) + b'"}'
    response = client.post(
        "/api/reports",
        content=oversized,
        headers={**device_headers, "Content-Type": "application/json"},
    )

    body = assert_problem(response, 413, code="payload_too_large")
    assert "256 KiB" in body["detail"]
    assert db.count("community_reports") == 0


async def test_a_malformed_content_length_is_rejected() -> None:
    """Exercised at the ASGI layer because an HTTP client will not send it.

    ``httpx`` computes ``Content-Length`` itself, so the only way to reach this
    branch is to hand the middleware the scope a hostile client would produce.
    """
    sent: List[Dict[str, Any]] = []

    async def receive() -> Dict[str, Any]:  # pragma: no cover - never awaited
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Dict[str, Any]) -> None:
        sent.append(message)

    async def never_called(scope: Any, receive: Any, send: Any) -> None:
        raise AssertionError("the request must not reach the application")

    middleware = BodySizeLimitMiddleware(never_called)
    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/reports",
            "headers": [(b"content-length", b"not-a-number")],
        },
        receive,
        send,
    )

    start = sent[0]
    assert start["type"] == "http.response.start"
    assert start["status"] == 400


async def test_non_http_scopes_pass_through_the_body_limit() -> None:
    """The lifespan scope has no headers; treating it as a request would break
    startup rather than reject a body."""
    seen: List[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    async def noop(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
        return None

    await BodySizeLimitMiddleware(inner)({"type": "lifespan"}, noop, noop)
    assert seen == ["lifespan"]


# ---------------------------------------------------------------------------
# Host and origin
# ---------------------------------------------------------------------------


def test_a_forged_host_header_is_rejected(client: TestClient) -> None:
    """Host-header poisoning: the app builds absolute URLs (the AQI tile proxy
    among them), and an unvalidated Host is reflected into them.

    Starlette's own middleware answers with a plain-text 400 rather than
    problem+json. That is acceptable here — it is refusing to interpret the
    request at all, so there is nothing for a client's error parser to act on —
    but it is the one non-conforming error response in the API.
    """
    response = client.get("/api", headers={"Host": "attacker.example.com"})
    assert response.status_code == 400


def test_configured_hosts_are_accepted(client: TestClient) -> None:
    for host in ("testserver", "localhost", "127.0.0.1"):
        assert client.get("/api", headers={"Host": host}).status_code == 200


def test_host_checking_is_skipped_when_hosts_are_a_wildcard(
    settings_override: Any, db: Any, upstream: Any
) -> None:
    """A wildcard is refused outright in production by ``_validate_production``;
    in development it must still serve, or local tooling that connects by IP
    breaks. The app is rebuilt because middleware is assembled from settings."""
    settings_override(trusted_hosts_raw="*")
    with TestClient(create_app()) as unrestricted:
        assert unrestricted.get("/api", headers={"Host": "anything.test"}).status_code == 200


def test_a_configured_origin_is_allowed_by_cors(client: TestClient) -> None:
    response = client.get("/api", headers={"Origin": "https://admin.example.test"})
    assert response.headers["access-control-allow-origin"] == "https://admin.example.test"
    # Credentials are only enabled once origins are enumerated; the spec forbids
    # the wildcard-plus-credentials combination and browsers reject it.
    assert response.headers["access-control-allow-credentials"] == "true"


def test_an_unknown_origin_is_not_reflected(client: TestClient) -> None:
    """Reflecting an arbitrary Origin is the same as having no CORS policy."""
    response = client.get("/api", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in response.headers


def test_preflight_advertises_only_the_headers_the_api_accepts(client: TestClient) -> None:
    response = client.options(
        "/api/reports",
        headers={
            "Origin": "https://admin.example.test",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": DEVICE_ID_HEADER,
        },
    )

    assert response.status_code == 200
    allowed = response.headers["access-control-allow-methods"]
    assert "POST" in allowed
    assert "PUT" not in allowed
    # The client must be able to read its own rate-limit budget and the request
    # id, which is only possible if they are exposed.
    exposed = response.headers["access-control-expose-headers"]
    assert "X-Request-ID" in exposed
    assert "X-RateLimit-Remaining" in exposed


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_reads_are_limited_and_the_budget_is_advertised(
    client: TestClient, rate_limits: Any
) -> None:
    rate_limits(read=2)

    first = client.get("/api/reports/categories")
    second = client.get("/api/reports/categories")
    third = client.get("/api/reports/categories")

    assert first.status_code == 200
    assert first.headers["X-RateLimit-Limit"] == "2"
    assert first.headers["X-RateLimit-Remaining"] == "1"
    assert second.headers["X-RateLimit-Remaining"] == "0"

    body = assert_problem(third, 429, code="rate_limited")
    assert body["limit"] == 2
    # Without Retry-After a client's only strategy is to keep hammering.
    assert int(third.headers["Retry-After"]) > 0


def test_write_and_read_budgets_are_separate(
    client: TestClient, rate_limits: Any, device_headers: Dict[str, str], db: Any
) -> None:
    """An AI call that costs money must not be able to exhaust the budget for
    cached reads, and vice versa — hence a bucket per cost class."""
    rate_limits(read=1, write=1)

    assert client.get("/api/reports/categories").status_code == 200
    assert client.get("/api/reports/categories").status_code == 429

    created = client.post(
        "/api/reports",
        json={"category": "fire", "title": "Smoke", "lat": 28.61, "lon": 77.21},
        headers=device_headers,
    )
    assert created.status_code == 201, created.text


def test_two_devices_have_independent_budgets(
    client: TestClient,
    rate_limits: Any,
    device_headers: Dict[str, str],
    other_device_headers: Dict[str, str],
) -> None:
    """Identity is the device id when present, so one heavy user on a carrier NAT
    cannot exhaust the budget for everyone behind it."""
    rate_limits(read=1)

    assert client.get("/api/reports/categories", headers=device_headers).status_code == 200
    assert client.get("/api/reports/categories", headers=device_headers).status_code == 429
    assert client.get("/api/reports/categories", headers=other_device_headers).status_code == 200


def test_liveness_and_readiness_are_never_rate_limited(
    client: TestClient, rate_limits: Any
) -> None:
    """An orchestrator polls these every few seconds from one address — exactly
    the traffic shape a limiter rejects. Throttling them would make the platform
    restart a healthy instance."""
    rate_limits(read=1)

    for _ in range(5):
        assert client.get("/api/health/live").status_code == 200
        assert client.get("/api/health/ready").status_code == 200


def test_login_attempts_are_limited(
    client: TestClient, rate_limits: Any, admin_credentials: Dict[str, str]
) -> None:
    """The ``auth`` bucket is what makes credential stuffing impractical, so it is
    asserted with wrong credentials — the path an attacker takes."""
    rate_limits(auth=2)
    wrong = {"username": admin_credentials["username"], "password": "not-the-password"}

    assert client.post("/api/admin/login", json=wrong).status_code == 401
    assert client.post("/api/admin/login", json=wrong).status_code == 401
    assert_problem(client.post("/api/admin/login", json=wrong), 429, code="rate_limited")


# ---------------------------------------------------------------------------
# Secrets stay server-side
# ---------------------------------------------------------------------------


SECRET_VALUES = (
    "test-waqi-token",
    "test-service-role-key",
    "test-jwt-secret-not-used-anywhere-else-0123456789",
    "correct-horse-battery-staple",
)


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/api",
        "/api/health",
        "/api/health/ready",
        "/api/meta/cache-stats",
        "/api/reports/categories",
        "/api/openapi.json",
    ],
)
def test_no_endpoint_echoes_a_configured_secret(client: TestClient, path: str) -> None:
    """Covers the diagnostic endpoints specifically.

    ``/api/health`` and ``/api/meta/cache-stats`` exist to describe the
    deployment, which is exactly the shape of endpoint that grows a field
    someone finds convenient and that turns out to contain a credential. The
    WAQI token matters most: it is quota'd per key, which is why map tiles are
    proxied through this API instead of being fetched by the client.
    """
    response = client.get(path)
    assert response.status_code == 200
    for secret in SECRET_VALUES:
        assert secret not in response.text, f"{path} leaked a configured secret"


def test_health_reports_capabilities_as_booleans_not_credentials(
    client: TestClient,
) -> None:
    """The client needs to know whether a feature works, which is a boolean. It
    never needs the key that makes it work."""
    features = client.get("/api/health").json()["features"]
    assert features["air_quality"] is True
    assert features["admin_dashboard"] is True
    assert all(isinstance(value, bool) for value in features.values())


def test_an_unhandled_error_does_not_leak_internals_in_production(
    client: TestClient, db: Any, settings_override: Any, device_headers: Dict[str, str]
) -> None:
    """A traceback names file paths, library versions and sometimes a URL with a
    token in it. Outside production the message is included deliberately, to
    save a trip to the logs during development."""
    db.failure = RuntimeError("connection string postgres://user:hunter2@db.internal")

    settings_override(environment="production")
    response = client.get("/api/favorites", headers=device_headers)

    assert response.status_code in (500, 503)
    assert "hunter2" not in response.text
    assert "db.internal" not in response.text
    assert "Traceback" not in response.text


def test_a_database_outage_is_a_503_not_a_500(
    client: TestClient, db: Any, device_headers: Dict[str, str]
) -> None:
    """ "Try again" and "we have a bug" are different messages to a user, and only
    one of them should page an on-call engineer."""
    db.failure = RuntimeError("connection reset by peer")

    response = client.get("/api/favorites", headers=device_headers)
    assert_problem(response, 503, code="service_unavailable")


def test_docs_and_schema_can_be_switched_off(
    settings_override: Any, db: Any, upstream: Any
) -> None:
    """In production the schema is a complete map of the attack surface.

    ``_validate_production`` refuses to boot with docs enabled; this asserts the
    switch it depends on actually removes both routes.
    """
    settings_override(enable_docs=False)
    with TestClient(create_app()) as locked_down:
        assert locked_down.get("/api/docs").status_code == 404
        assert locked_down.get("/api/openapi.json").status_code == 404
        assert locked_down.get("/api").status_code == 200
