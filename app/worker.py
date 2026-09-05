"""Push dispatch worker.

A second process that runs the same code as the API, on a timer. Deliberately not
a background task inside the API process, for three reasons that all came from
the same place — the API is horizontally scaled:

* **Scaling the API would multiply the dispatcher.** Four uvicorn workers each
  running their own timer is four passes, and while the claim makes that safe, it
  is four times the upstream fan-out for nothing.
* **A pass is minutes long.** Long work inside a request-serving process competes
  with request latency for the same event loop.
* **Notifications should be deployable and stoppable on their own.** Stopping the
  dispatcher must never mean stopping the API, and vice versa.

Run it with the same image and environment as the API::

    python -m app.worker              # loop on PUSH_DISPATCH_INTERVAL_SECONDS
    python -m app.worker --once       # a single pass, then exit
    python -m app.worker --prune      # retention sweep only

``--once`` exists for deployments that already have a scheduler — a Kubernetes
CronJob, or plain cron — and for verifying a configuration change by hand. It
exits non-zero when the pass aborted, so a scheduler notices.

Shutdown is graceful: SIGTERM and SIGINT stop the loop, but the pass in flight
runs to completion. Killing a pass mid-send is the one thing that leaves claims
stranded at ``pending``, and although the retry sweep recovers them, taking the
recoverable path on every deploy is a bad habit to build in.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
import time
from typing import List, Optional

from app.core.cache import cache
from app.core.config import settings
from app.core.http import shutdown_http, startup_http
from app.core.logging import configure_logging, get_logger
from app.db import supabase
from app.services import alert_dispatch

logger = get_logger(__name__)

#: How often the retention sweep runs, in passes. Once every 96 passes is daily at
#: the default fifteen-minute interval. Counted in passes rather than scheduled by
#: clock time so the worker needs no persistent state: a restart simply means the
#: first prune happens a little later, which nothing depends on.
PRUNE_EVERY_PASSES = 96


def _refuse_to_start() -> str:
    """Why this process must not run, or an empty string if it may.

    Both checks are refusals rather than warnings. A worker that starts and then
    aborts every pass logs an error every fifteen minutes forever, and an operator
    who sees a running container reasonably assumes notifications are being sent.
    Failing at startup is the only version of this that is honest.
    """
    if not settings.push_enabled:
        return (
            "PUSH_ENABLED is false, so this worker would never send anything. "
            "Set PUSH_ENABLED=true to enable delivery, or do not start the worker."
        )
    if not settings.has_supabase:
        return (
            "SUPABASE_URL and SUPABASE_KEY are required: push tokens, alert "
            "preferences and the delivery ledger all live in the database."
        )
    return ""


async def _startup() -> None:
    """Acquire the same process-wide resources the API's lifespan does.

    The rate limiter is not among them: it exists to bound inbound HTTP, and this
    process serves none. Everything else is shared code that expects to be
    initialised — ``disasters.aggregate`` reads through the cache, and every
    repository call needs the Supabase client.
    """
    await startup_http()
    await cache.connect()
    supabase.get_client()
    logger.info(
        "Push dispatch worker started",
        extra={
            "environment": settings.environment,
            "interval_seconds": settings.push_dispatch_interval_seconds,
            "lookback_hours": settings.push_lookback_hours,
            "batch_size": settings.push_batch_size,
            "max_per_device": settings.push_max_per_device_per_pass,
            "expo_authenticated": settings.has_expo_access_token,
        },
    )
    if not settings.has_expo_access_token:
        logger.warning(
            "EXPO_ACCESS_TOKEN not set; sends are unauthenticated. Anyone who "
            "extracts a push token from the app bundle can notify our users."
        )


async def _shutdown() -> None:
    """Release in reverse order, each step independent of the others."""
    for label, closer in (("cache", cache.close), ("http client", shutdown_http)):
        try:
            await closer()
        except Exception as exc:
            logger.warning("Failed to close %s cleanly: %s", label, exc)


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """Ask the loop to stop after the current pass on SIGTERM or SIGINT.

    ``add_signal_handler`` is POSIX-only, and the fallback matters: without it,
    running the worker on Windows for a local test would fail at startup rather
    than merely losing graceful shutdown.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, AttributeError, ValueError):
            loop.add_signal_handler(sig, stop.set)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Wait out the interval. Returns True if a shutdown signal arrived first.

    Interruptible on purpose: a plain ``asyncio.sleep`` would keep a container
    alive for up to a full interval after SIGTERM, which orchestrators answer with
    SIGKILL.
    """
    if seconds <= 0:
        return stop.is_set()
    waiter = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait({waiter}, timeout=seconds)
    if waiter not in done:
        waiter.cancel()
    return stop.is_set()


async def _one_pass(pass_number: int) -> alert_dispatch.DispatchStats:
    """Run a pass, and the retention sweep when this is a pruning pass.

    ``run_pass`` never raises, but the broad guard stays: an unexpected exception
    here would end the worker, and a dispatcher that dies on the first surprise is
    worse than one that logs it and tries again in fifteen minutes. The traceback is
    logged in full — this is the branch that hides bugs otherwise.
    """
    try:
        stats = await alert_dispatch.run_pass()
    except Exception:
        logger.exception("Dispatch pass raised unexpectedly")
        stats = alert_dispatch.DispatchStats(aborted="unhandled_exception")

    if pass_number % PRUNE_EVERY_PASSES == 0:
        try:
            await alert_dispatch.prune_ledger()
        except Exception:
            # Retention is housekeeping. The database's own `prune_old_data` is the
            # backstop, and a pass that failed to prune must still deliver.
            logger.exception("Ledger prune failed")

    return stats


async def _loop() -> int:
    """Dispatch on a fixed cadence until asked to stop. Returns a process exit code."""
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    interval = float(settings.push_dispatch_interval_seconds)
    pass_number = 0

    while not stop.is_set():
        pass_number += 1
        started = time.monotonic()
        await _one_pass(pass_number)
        elapsed = time.monotonic() - started

        # Sleep the remainder of the interval rather than the whole of it, so a slow
        # pass does not push every later pass later still. A pass that overran the
        # interval starts the next one immediately, which is the correct response:
        # it means there is more work than the cadence allows.
        if await _sleep_or_stop(stop, interval - elapsed):
            break

    logger.info("Push dispatch worker stopping", extra={"passes": pass_number})
    return 0


async def _run(mode: str) -> int:
    refusal = _refuse_to_start()
    if refusal:
        logger.error("Refusing to start the push dispatch worker: %s", refusal)
        return 2

    await _startup()
    try:
        if mode == "prune":
            await alert_dispatch.prune_ledger()
            return 0
        if mode == "once":
            stats = await alert_dispatch.run_pass()
            # Non-zero on an aborted pass so a cron or CronJob surfaces it. A pass
            # that simply had nothing to send is a success and exits 0.
            return 1 if stats.aborted else 0
        return await _loop()
    finally:
        await _shutdown()


def main(argv: Optional[List[str]] = None) -> int:
    configure_logging()

    parser = argparse.ArgumentParser(
        prog="python -m app.worker",
        description="SentinelAI push dispatch worker.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--once",
        action="store_const",
        const="once",
        dest="mode",
        help="run a single dispatch pass, then exit non-zero if it aborted",
    )
    group.add_argument(
        "--prune",
        action="store_const",
        const="prune",
        dest="mode",
        help="run the delivery-ledger retention sweep only",
    )
    parser.set_defaults(mode="loop")
    args = parser.parse_args(argv)

    return asyncio.run(_run(args.mode))


if __name__ == "__main__":
    sys.exit(main())
