"""Shared outbound HTTP client.

The original code created a fresh ``httpx.AsyncClient`` inside every handler,
which throws away connection pooling and TLS session reuse — measurably slower
and much harder on upstreams. This module owns one pooled client for the
process lifetime and adds the three things every upstream call needs:

* **Retry with exponential backoff and jitter** on transient failures, honouring
  ``Retry-After`` when the upstream sends it.
* **Per-host politeness throttling** for APIs whose usage policy caps request
  rate (Nominatim: 1 req/s, Overpass: low concurrency). Exceeding these gets
  the deployment IP banned, not throttled.
* **A typed error** so routers can distinguish "upstream is down" (502) from
  "upstream timed out" (504) without leaking upstream detail to clients.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Dict, FrozenSet, Mapping, Optional
from urllib.parse import urlsplit

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Status codes worth retrying: rate limit plus transient server-side faults.
#
# The default is right for the read-only providers this module was written for,
# where a repeated GET costs nothing. It is *not* right for every POST — see
# ``retry_statuses`` on :func:`request_json`, which the push sender narrows,
# because re-sending a notification Expo may already have accepted is a
# duplicate notification rather than a wasted request.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

# Minimum seconds between requests to hosts with a published rate policy.
# https://operations.osmfoundation.org/policies/nominatim/
# https://dev.overpass-api.de/overpass-doc/en/preface/commons.html
_HOST_MIN_INTERVAL: Dict[str, float] = {
    "nominatim.openstreetmap.org": 1.1,
    "overpass-api.de": 1.0,
}

# Cap simultaneous in-flight requests per host so one slow upstream cannot
# exhaust the connection pool for the others.
_HOST_MAX_CONCURRENCY: Dict[str, int] = {
    "nominatim.openstreetmap.org": 1,
    "overpass-api.de": 2,
}
_DEFAULT_HOST_CONCURRENCY = 10


class UpstreamError(Exception):
    """An upstream data provider failed.

    Carries enough context for structured logging while keeping the
    client-facing message free of upstream URLs and credentials.
    """

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: Optional[int] = None,
        timeout: bool = False,
    ) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message
        self.status_code = status_code
        self.timeout = timeout


def _error_reason(response: httpx.Response) -> Optional[str]:
    """Extract a provider-supplied explanation from an error response body.

    Opt-in per call site (``explain_errors=True``), because a body is only safe
    to record when the provider takes no credential in the request — otherwise
    the explanation can echo a query string carrying an API token. Open-Meteo is
    keyless and answers ``{"error": true, "reason": "..."}``, and that reason is
    the whole difference between "your parameters are wrong" and "you are rate
    limited", which is otherwise indistinguishable from a bare HTTP 400.
    """
    if len(response.content) > 4096:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    reason = body.get("reason") or body.get("error_description") or body.get("message")
    return reason[:300] if isinstance(reason, str) else None


def error_fields(exc: Optional[BaseException]) -> Dict[str, Any]:
    """Structured log fields describing *why* an upstream call failed.

    Aggregating endpoints gather their legs with ``return_exceptions=True`` and
    then log whichever came back as an exception. Recording only
    ``type(exc).__name__`` yields the string ``"UpstreamError"``, which does not
    distinguish a timeout from an HTTP 429 from an undecodable body — and those
    three have completely different fixes. Diagnosing a failing leg from
    production logs was impossible for exactly this reason.

    Only :class:`UpstreamError` is described in detail, and that is a security
    decision rather than laziness. Its ``message`` is constructed by this module
    without the request URL or any credential, so it is safe to record. An
    arbitrary exception's string carries no such guarantee: an httpx error can
    embed the request URL, and some provider URLs carry the API token in their
    query string. For anything else, the type alone is recorded.
    """
    if exc is None:
        return {"error": None}

    fields: Dict[str, Any] = {"error": type(exc).__name__}
    if isinstance(exc, UpstreamError):
        fields["provider"] = exc.provider
        fields["reason"] = exc.message
        fields["timeout"] = exc.timeout
        if exc.status_code is not None:
            fields["upstream_status"] = exc.status_code
    return fields


class _HostThrottle:
    """Serialises and paces requests per host."""

    def __init__(self) -> None:
        self._semaphores: Dict[str, asyncio.Semaphore] = {}
        self._pace_locks: Dict[str, asyncio.Lock] = {}
        self._last_request: Dict[str, float] = {}
        self._guard = asyncio.Lock()

    async def _resources(self, host: str) -> tuple[asyncio.Semaphore, asyncio.Lock]:
        async with self._guard:
            if host not in self._semaphores:
                limit = _HOST_MAX_CONCURRENCY.get(host, _DEFAULT_HOST_CONCURRENCY)
                self._semaphores[host] = asyncio.Semaphore(limit)
                self._pace_locks[host] = asyncio.Lock()
            return self._semaphores[host], self._pace_locks[host]

    async def acquire(self, host: str) -> asyncio.Semaphore:
        semaphore, pace_lock = await self._resources(host)
        await semaphore.acquire()
        min_interval = _HOST_MIN_INTERVAL.get(host)
        if min_interval:
            async with pace_lock:
                last = self._last_request.get(host, 0.0)
                wait = min_interval - (time.monotonic() - last)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_request[host] = time.monotonic()
        return semaphore


_throttle = _HostThrottle()
_client: Optional[httpx.AsyncClient] = None


def _build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.http_timeout_seconds,
            connect=min(5.0, settings.http_timeout_seconds),
        ),
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_keepalive,
        ),
        follow_redirects=True,
        headers={
            # Nominatim and Overpass require an identifying, contactable UA.
            "User-Agent": settings.user_agent,
            "Accept": "application/json",
        },
    )


async def startup_http() -> None:
    global _client
    if _client is None:
        _client = _build_client()
        logger.info("HTTP client pool initialised")


async def shutdown_http() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
        logger.info("HTTP client pool closed")


def get_client() -> httpx.AsyncClient:
    """Return the pooled client, creating it on demand.

    The lazy path matters for tests and scripts that import a service without
    running the app's lifespan.
    """
    global _client
    if _client is None:
        _client = _build_client()
    return _client


def _retry_after_seconds(response: httpx.Response) -> Optional[float]:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        # Only the delta-seconds form is worth honouring automatically; an
        # HTTP-date could be far in the future and would stall the request.
        seconds = float(raw.strip())
    except ValueError:
        return None
    return max(0.0, min(seconds, 10.0))


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with full jitter, capped."""
    base = min(2.0**attempt * 0.25, 4.0)
    return base * (0.5 + random.random() / 2)


async def request_json(
    method: str,
    url: str,
    *,
    provider: str,
    params: Optional[Mapping[str, Any]] = None,
    data: Optional[Mapping[str, Any]] = None,
    json_body: Optional[Any] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: Optional[float] = None,
    retries: Optional[int] = None,
    retry_statuses: Optional[FrozenSet[int]] = None,
    explain_errors: bool = False,
) -> Any:
    """Perform an HTTP request and decode JSON, with retries and throttling.

    ``data`` is form-encoded; ``json_body`` is sent as a JSON document with the
    matching Content-Type. They are mutually exclusive, and passing both raises
    rather than picking one: httpx silently prefers ``data``, so a caller that
    meant to send JSON would get a form body and an upstream 400 that points
    nowhere near the mistake.

    ``retry_statuses`` overrides which response codes are retried. Narrow it for
    any request that is not safe to repeat.

    ``explain_errors`` folds the provider's own explanation of a 4xx/5xx into the
    error message and the log record. Enable it only for providers that take no
    credential in the request — see :func:`_error_reason`.

    Raises:
        UpstreamError: on timeout, network failure, non-2xx after retries, or
            an undecodable body.
        ValueError: if both ``data`` and ``json_body`` are given.
    """
    if data is not None and json_body is not None:
        raise ValueError("pass either data= or json_body=, not both")

    client = get_client()
    host = urlsplit(url).hostname or ""
    max_attempts = (settings.http_max_retries if retries is None else retries) + 1
    request_timeout = timeout if timeout is not None else settings.http_timeout_seconds
    retryable = _RETRY_STATUS if retry_statuses is None else retry_statuses

    last_error: Optional[UpstreamError] = None

    for attempt in range(max_attempts):
        semaphore = await _throttle.acquire(host)
        retry_delay: Optional[float] = None
        try:
            try:
                response = await client.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    json=json_body,
                    headers=dict(headers) if headers else None,
                    timeout=request_timeout,
                )
            except httpx.TimeoutException as exc:
                last_error = UpstreamError(provider, "request timed out", timeout=True)
                retry_delay = _backoff_delay(attempt)
                logger.warning(
                    "Upstream timeout",
                    extra={
                        "provider": provider,
                        "host": host,
                        "attempt": attempt + 1,
                        "error": str(exc),
                    },
                )
            except httpx.HTTPError as exc:
                last_error = UpstreamError(provider, "network error")
                retry_delay = _backoff_delay(attempt)
                logger.warning(
                    "Upstream network error",
                    extra={
                        "provider": provider,
                        "host": host,
                        "attempt": attempt + 1,
                        "error": str(exc),
                    },
                )
            else:
                status = response.status_code
                detail = _error_reason(response) if explain_errors and status >= 400 else None
                if status in retryable and attempt < max_attempts - 1:
                    retry_delay = _retry_after_seconds(response) or _backoff_delay(attempt)
                    last_error = UpstreamError(
                        provider,
                        f"returned HTTP {status}" + (f": {detail}" if detail else ""),
                        status_code=status,
                    )
                    logger.warning(
                        "Upstream returned retryable status",
                        extra={
                            "provider": provider,
                            "host": host,
                            "status": status,
                            "attempt": attempt + 1,
                            "retry_in": round(retry_delay, 2),
                            "upstream_reason": detail,
                        },
                    )
                elif status >= 400:
                    logger.warning(
                        "Upstream returned error status",
                        extra={
                            "provider": provider,
                            "host": host,
                            "status": status,
                            "upstream_reason": detail,
                        },
                    )
                    raise UpstreamError(
                        provider,
                        f"returned HTTP {status}" + (f": {detail}" if detail else ""),
                        status_code=status,
                    )
                else:
                    try:
                        return response.json()
                    except ValueError:
                        raise UpstreamError(provider, "returned a non-JSON body") from None
        finally:
            semaphore.release()

        if retry_delay is not None and attempt < max_attempts - 1:
            await asyncio.sleep(retry_delay)

    raise last_error or UpstreamError(provider, "request failed")


async def get_json(url: str, *, provider: str, **kwargs: Any) -> Any:
    return await request_json("GET", url, provider=provider, **kwargs)


async def post_json(url: str, *, provider: str, **kwargs: Any) -> Any:
    return await request_json("POST", url, provider=provider, **kwargs)
