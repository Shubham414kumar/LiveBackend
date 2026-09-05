"""Response cache for upstream data.

Why this exists: every disaster/AQI/geocode endpoint fans out to a free public
API. Nominatim and Overpass will ban the deployment IP outright under load,
and WAQI is token-quota'd. Caching is a correctness requirement here, not an
optimisation.

Two backends:

* **Redis** when ``REDIS_URL`` is set — shared across workers and restarts.
* **In-memory TTL + LRU** otherwise — correct but per-worker, and bounded so
  it can never grow into a memory leak (the original code used unbounded
  module-level dicts).

Both paths go through :meth:`Cache.get_or_set`, which collapses concurrent
misses for the same key into a single upstream call ("single flight"). Without
that, a cold cache plus a traffic spike produces one upstream request per
inbound request, which is exactly how you get banned.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Dict, Tuple

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_MISS = object()


class _MemoryStore:
    """Bounded TTL cache with LRU eviction."""

    def __init__(self, max_entries: int) -> None:
        self._max_entries = max_entries
        # key -> (expires_at_monotonic, value)
        self._data: OrderedDict[str, Tuple[float, Any]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return _MISS
            expires_at, value = entry
            if expires_at <= time.monotonic():
                del self._data[key]
                return _MISS
            self._data.move_to_end(key)
            return value

    async def set(self, key: str, value: Any, ttl: int) -> None:
        if ttl <= 0:
            return
        async with self._lock:
            self._data[key] = (time.monotonic() + ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max_entries:
                self._data.popitem(last=False)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._data.clear()

    def stats(self) -> Dict[str, Any]:
        return {"backend": "memory", "entries": len(self._data), "max_entries": self._max_entries}


class Cache:
    """Namespaced async cache with single-flight fetching."""

    def __init__(self, namespace: str = "sentinelai") -> None:
        self._namespace = namespace
        self._memory = _MemoryStore(settings.cache_max_entries)
        self._redis: Any = None
        self._redis_available = False
        # Guards against duplicate concurrent upstream calls per key.
        self._inflight: Dict[str, asyncio.Future] = {}
        self._inflight_lock = asyncio.Lock()

    # ---------- lifecycle ----------
    async def connect(self) -> None:
        if not settings.redis_url:
            logger.info(
                "Cache using in-memory backend; set REDIS_URL to share cache across workers.",
                extra={"cache_backend": "memory"},
            )
            return
        try:
            import redis.asyncio as aioredis  # imported lazily: optional dependency

            self._redis = aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
                health_check_interval=30,
            )
            await self._redis.ping()
            self._redis_available = True
            logger.info("Cache connected to Redis", extra={"cache_backend": "redis"})
        except Exception as exc:
            # A cache outage must not take the API down; degrade to memory.
            self._redis = None
            self._redis_available = False
            logger.warning(
                "Redis unavailable, falling back to in-memory cache: %s",
                exc,
                extra={"cache_backend": "memory"},
            )

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # pragma: no cover - best-effort teardown
                pass
            self._redis = None
            self._redis_available = False
        await self._memory.clear()

    # ---------- primitives ----------
    def _full_key(self, key: str) -> str:
        return f"{self._namespace}:{key}"

    async def get(self, key: str) -> Any:
        """Return the cached value, or the sentinel ``_MISS``."""
        if self._redis_available and self._redis is not None:
            try:
                raw = await self._redis.get(self._full_key(key))
                if raw is not None:
                    return json.loads(raw)
                return _MISS
            except Exception as exc:
                logger.warning("Redis GET failed, using memory: %s", exc)
                self._redis_available = False
        return await self._memory.get(key)

    async def set(self, key: str, value: Any, ttl: int) -> None:
        if ttl <= 0:
            return
        if self._redis_available and self._redis is not None:
            try:
                await self._redis.set(self._full_key(key), json.dumps(value, default=str), ex=ttl)
                return
            except Exception as exc:
                logger.warning("Redis SET failed, using memory: %s", exc)
                self._redis_available = False
        await self._memory.set(key, value, ttl)

    async def delete(self, key: str) -> None:
        if self._redis_available and self._redis is not None:
            try:
                await self._redis.delete(self._full_key(key))
            except Exception:
                self._redis_available = False
        await self._memory.delete(key)

    async def clear(self) -> None:
        """Drop every entry in this namespace.

        Used by the test suite between cases: a response cached by one test
        would otherwise be served to the next, so an assertion about a
        provider failure would pass or fail depending on which test ran first.

        Redis is cleared by scanning this namespace's key prefix rather than
        with ``FLUSHDB``, because the Redis instance may be shared with other
        applications — and in CI it is shared with the rate limiter, whose
        counters live under their own prefix and are reset separately.
        """
        async with self._inflight_lock:
            self._inflight.clear()
        await self._memory.clear()
        if self._redis is None:
            return
        try:
            async for key in self._redis.scan_iter(match=self._full_key("*"), count=500):
                await self._redis.delete(key)
        except Exception as exc:  # pragma: no cover - best-effort
            logger.warning("Redis namespace clear failed: %s", exc)

    # ---------- single-flight ----------
    async def get_or_set(
        self,
        key: str,
        ttl: int,
        factory: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Return the cached value for ``key``, computing it at most once.

        Concurrent callers that miss on the same key await a single execution
        of ``factory`` instead of each issuing their own upstream request.
        """
        cached = await self.get(key)
        if cached is not _MISS:
            return cached

        async with self._inflight_lock:
            existing = self._inflight.get(key)
            if existing is not None:
                leader = False
                future = existing
            else:
                leader = True
                future = asyncio.get_running_loop().create_future()
                self._inflight[key] = future

        if not leader:
            # Someone else is already fetching; ride along on their result.
            return await asyncio.shield(future)

        try:
            value = await factory()
        except BaseException as exc:
            async with self._inflight_lock:
                self._inflight.pop(key, None)
            if not future.done():
                future.set_exception(exc)
                # Retrieve it so asyncio doesn't warn about a never-consumed
                # exception when no follower happened to be waiting.
                try:
                    future.exception()
                except asyncio.CancelledError:
                    pass
            raise
        else:
            await self.set(key, value, ttl)
            async with self._inflight_lock:
                self._inflight.pop(key, None)
            if not future.done():
                future.set_result(value)
            return value

    def stats(self) -> Dict[str, Any]:
        if self._redis_available:
            return {"backend": "redis", "inflight": len(self._inflight)}
        return {**self._memory.stats(), "inflight": len(self._inflight)}


# Process-wide instance, wired up in the application lifespan.
cache = Cache()


def cache_key(prefix: str, **parts: Any) -> str:
    """Build a stable cache key.

    Sorted so that ``cache_key("aqi", lat=1, lon=2)`` and
    ``cache_key("aqi", lon=2, lat=1)`` collapse to the same key.
    """
    encoded = ":".join(f"{k}={parts[k]}" for k in sorted(parts) if parts[k] is not None)
    return f"{prefix}:{encoded}" if encoded else prefix


def round_coord(value: float, places: int = 2) -> float:
    """Quantise a coordinate so nearby requests share a cache entry.

    Two decimal places is roughly a 1 km grid — well inside the resolution of
    every upstream we query, and it turns a continuous stream of unique
    coordinates into a bounded, cacheable keyspace.
    """
    return round(value, places)
