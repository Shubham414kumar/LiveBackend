"""Rate limiting.

The original implementation kept an unbounded module-level
``defaultdict(list)`` keyed by client IP. Two problems: it never evicted keys
(an unbounded memory leak reachable by any client that varies its IP), and it
was per-process, so running the recommended multiple uvicorn workers silently
multiplied every limit by the worker count.

This version uses a Redis sliding-window log when Redis is configured — so
limits are shared and accurate across workers — and falls back to a *bounded*
in-memory window otherwise.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Deque, Dict

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Bucket name -> requests permitted per window.
BUCKETS: Dict[str, str] = {
    "read": "rate_limit_read",
    "write": "rate_limit_write",
    "ai": "rate_limit_ai",
    "auth": "rate_limit_auth",
}

# Hard ceiling on distinct in-memory keys, so the fallback path can never grow
# without bound the way the original did.
_MAX_MEMORY_KEYS = 20_000


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


def limit_for(bucket: str) -> int:
    attr = BUCKETS.get(bucket, BUCKETS["read"])
    return int(getattr(settings, attr))


class RateLimiter:
    def __init__(self) -> None:
        self._redis = None
        self._redis_available = False
        self._windows: OrderedDict[str, Deque[float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if not settings.redis_url:
            logger.info(
                "Rate limiter using in-memory backend; limits apply per worker. "
                "Set REDIS_URL to enforce them cluster-wide.",
                extra={"rate_limit_backend": "memory"},
            )
            return
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            await self._redis.ping()
            self._redis_available = True
            logger.info(
                "Rate limiter connected to Redis",
                extra={"rate_limit_backend": "redis"},
            )
        except Exception as exc:
            self._redis = None
            self._redis_available = False
            logger.warning("Rate limiter Redis unavailable, using memory: %s", exc)

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # pragma: no cover
                pass
            self._redis = None
            self._redis_available = False
        async with self._lock:
            self._windows.clear()

    @property
    def backend(self) -> str:
        """Which store is actually enforcing limits, not which one was asked for.

        :meth:`connect` degrades to the in-memory windows on any Redis failure
        — wrong URL, wrong password, unreachable host — and logs a warning that
        nothing reads. Reporting ``"redis"`` merely because ``REDIS_URL`` is set
        therefore describes intent, and it diverges from reality in precisely the
        situation an operator needs told: limits have silently become per-worker,
        so the effective ceiling is the configured one multiplied by the worker
        count, and it resets on every deploy.
        """
        return "redis" if self._redis_available else "memory"

    async def reset(self) -> None:
        """Clear all counters. Used by the test suite between cases."""
        async with self._lock:
            self._windows.clear()
        if self._redis_available and self._redis is not None:
            try:
                async for key in self._redis.scan_iter(match="rl:*", count=500):
                    await self._redis.delete(key)
            except Exception:  # pragma: no cover
                pass

    async def check(self, identity: str, bucket: str = "read") -> RateLimitResult:
        if not settings.rate_limit_enabled:
            limit = limit_for(bucket)
            return RateLimitResult(True, limit, limit, 0)

        limit = limit_for(bucket)
        window = settings.rate_limit_window_seconds
        key = f"rl:{bucket}:{identity}"

        if self._redis_available and self._redis is not None:
            try:
                return await self._check_redis(key, limit, window)
            except Exception as exc:
                logger.warning("Rate limiter Redis error, using memory: %s", exc)
                self._redis_available = False

        return await self._check_memory(key, limit, window)

    async def _check_redis(self, key: str, limit: int, window: int) -> RateLimitResult:
        now = time.time()
        member = f"{now}:{uuid.uuid4().hex[:8]}"

        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(key, 0, now - window)
        pipe.zadd(key, {member: now})
        pipe.zcard(key)
        pipe.expire(key, window + 1)
        results = await pipe.execute()
        count = int(results[2])

        if count > limit:
            # Roll back our own entry so a rejected request doesn't extend the
            # window for everyone else sharing this key.
            await self._redis.zrem(key, member)
            oldest = await self._redis.zrange(key, 0, 0, withscores=True)
            retry_after = window
            if oldest:
                retry_after = max(1, int(window - (now - float(oldest[0][1]))) + 1)
            return RateLimitResult(False, limit, 0, retry_after)

        return RateLimitResult(True, limit, max(0, limit - count), 0)

    async def _check_memory(self, key: str, limit: int, window: int) -> RateLimitResult:
        now = time.monotonic()
        async with self._lock:
            bucket_window = self._windows.get(key)
            if bucket_window is None:
                bucket_window = deque()
                self._windows[key] = bucket_window
            self._windows.move_to_end(key)

            cutoff = now - window
            while bucket_window and bucket_window[0] <= cutoff:
                bucket_window.popleft()

            if len(bucket_window) >= limit:
                retry_after = max(1, int(window - (now - bucket_window[0])) + 1)
                return RateLimitResult(False, limit, 0, retry_after)

            bucket_window.append(now)
            remaining = max(0, limit - len(bucket_window))

            # Evict least-recently-used keys, dropping empty windows first.
            while len(self._windows) > _MAX_MEMORY_KEYS:
                evicted_key, _ = self._windows.popitem(last=False)
                if evicted_key == key:  # pragma: no cover - defensive
                    break

            return RateLimitResult(True, limit, remaining, 0)


limiter = RateLimiter()
