"""Shared route dependencies.

Rate limiting is expressed as a dependency per bucket rather than as a single
global middleware, so each route declares its own cost class. An AI call that
costs money and takes seconds should not share a budget with a cached AQI read.

Identity for limiting purposes is the device id when the client sent one, and
the client IP otherwise. Preferring the device id means one device behind a
carrier NAT is limited on its own, instead of thousands of unrelated users on
that NAT sharing a single bucket. It is trivially spoofable, which is why the IP
is still used as the fallback and why nothing security-sensitive depends on it.
"""

from __future__ import annotations

from typing import Callable, Optional

from fastapi import Request, Response

from app.core.errors import RateLimitedError
from app.core.identity import normalise_device_id
from app.core.rate_limit import limiter
from app.core.security import DEVICE_ID_HEADER, client_ip


def _identity(request: Request) -> str:
    device_id = normalise_device_id(request.headers.get(DEVICE_ID_HEADER))
    if device_id:
        return f"dev:{device_id}"
    return f"ip:{client_ip(request)}"


def rate_limit(bucket: str) -> Callable[[Request, Response], object]:
    """Build a dependency enforcing the named bucket's limit.

    Standard ``X-RateLimit-*`` headers are set on success as well as failure, so
    a well-behaved client can back off before it gets a 429.
    """

    async def _dependency(request: Request, response: Response) -> None:
        result = await limiter.check(_identity(request), bucket)

        response.headers["X-RateLimit-Limit"] = str(result.limit)
        response.headers["X-RateLimit-Remaining"] = str(result.remaining)

        if not result.allowed:
            raise RateLimitedError(
                "Too many requests. Please slow down and try again shortly.",
                retry_after=result.retry_after,
                limit=result.limit,
            )

    return _dependency


# Pre-built dependencies, so routes read as `dependencies=[Depends(read_limit)]`.
read_limit = rate_limit("read")
write_limit = rate_limit("write")
ai_limit = rate_limit("ai")
auth_limit = rate_limit("auth")


def maybe_device_id(request: Request) -> Optional[str]:
    """Non-async accessor for the normalised device id, for use inside handlers."""
    return normalise_device_id(request.headers.get(DEVICE_ID_HEADER))


__all__ = [
    "ai_limit",
    "auth_limit",
    "maybe_device_id",
    "rate_limit",
    "read_limit",
    "write_limit",
]
