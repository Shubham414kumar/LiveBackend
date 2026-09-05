"""The push dispatch worker: refusals, exit codes, and crash containment.

Three properties are load-bearing here, and none of them are about dispatch —
:mod:`tests.test_alert_dispatch` owns that. This file owns the process.

**A worker that cannot send must not start.** With ``PUSH_ENABLED`` false or no
datastore, every pass would abort forever while the container sat there looking
healthy. An operator seeing a running worker is entitled to conclude that
notifications are going out, so the honest failure is at startup, loudly, with a
non-zero exit code.

**``--once`` tells the truth to a scheduler.** It exits non-zero when the pass
aborted and zero when there was simply nothing to send — a cron job that cannot
tell "the upstream feed is down" from "a quiet afternoon" is not monitoring
anything.

**A surprise must not end the process.** ``run_pass`` is written not to raise, but
the worker guards it anyway, because a dispatcher that dies on the first unhandled
exception stops notifying anyone until a human notices. The pass is logged and the
timer continues.

``_startup`` and ``_shutdown`` are replaced throughout by the ``lifecycle``
fixture. What they do — build the pooled HTTP client, connect the cache, construct
the Supabase client — belongs to those modules' own tests; running them here would
tear down the very fixtures the rest of the suite shares. What this file does check
is that they are *called*, and that release happens even when the pass blows up.
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Optional

import pytest

from app import worker
from app.services import alert_dispatch


class Lifecycle:
    """Records the worker's resource acquisition instead of performing it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.started = 0
        self.stopped = 0

        async def startup() -> None:
            self.started += 1

        async def shutdown() -> None:
            self.stopped += 1

        monkeypatch.setattr(worker, "_startup", startup)
        monkeypatch.setattr(worker, "_shutdown", shutdown)


class Dispatcher:
    """Stands in for the dispatcher, so this file tests only the process around it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.passes = 0
        self.prunes = 0
        self.stats = alert_dispatch.DispatchStats()
        #: Raised by ``run_pass`` when set, to exercise the containment guard.
        self.pass_error: Optional[BaseException] = None
        self.prune_error: Optional[BaseException] = None
        #: Called after each pass, for tests that need to stop the loop.
        self.after_pass: Any = None

        async def run_pass() -> alert_dispatch.DispatchStats:
            self.passes += 1
            if self.after_pass is not None:
                self.after_pass()
            if self.pass_error is not None:
                raise self.pass_error
            return self.stats

        async def prune_ledger() -> int:
            self.prunes += 1
            if self.prune_error is not None:
                raise self.prune_error
            return 0

        monkeypatch.setattr(alert_dispatch, "run_pass", run_pass)
        monkeypatch.setattr(alert_dispatch, "prune_ledger", prune_ledger)


@pytest.fixture
def lifecycle(monkeypatch: pytest.MonkeyPatch) -> Lifecycle:
    return Lifecycle(monkeypatch)


@pytest.fixture
def dispatcher(monkeypatch: pytest.MonkeyPatch) -> Dispatcher:
    return Dispatcher(monkeypatch)


@pytest.fixture
def runnable(settings_override: Any) -> None:
    """Configuration the worker will agree to start under."""
    settings_override(push_enabled=True)


# ---------------------------------------------------------------------------
# Refusing to start
# ---------------------------------------------------------------------------


async def test_a_worker_that_cannot_send_refuses_to_start(
    lifecycle: Lifecycle, dispatcher: Dispatcher
) -> None:
    """``PUSH_ENABLED`` false is a deployment mistake, not a runtime mode."""
    code = await worker._run("once")

    assert code == 2
    assert dispatcher.passes == 0
    assert lifecycle.started == 0, "refusal happens before any resource is acquired"


async def test_a_worker_without_a_datastore_refuses_to_start(
    runnable: None, settings_override: Any, lifecycle: Lifecycle, dispatcher: Dispatcher
) -> None:
    """Tokens, preferences and the ledger all live in Supabase; without it there is
    nothing to read and nowhere to record a claim."""
    settings_override(supabase_url=None)

    code = await worker._run("once")

    assert code == 2
    assert dispatcher.passes == 0


def test_each_refusal_names_the_setting_an_operator_has_to_change(
    settings_override: Any,
) -> None:
    """A refusal an operator cannot act on is just a crash with better manners."""
    assert "PUSH_ENABLED" in worker._refuse_to_start()

    settings_override(push_enabled=True)
    assert worker._refuse_to_start() == ""

    settings_override(supabase_url=None)
    assert "SUPABASE_URL" in worker._refuse_to_start()


# ---------------------------------------------------------------------------
# --once, and what it tells a scheduler
# ---------------------------------------------------------------------------


async def test_once_runs_exactly_one_pass_and_exits_zero(
    runnable: None, lifecycle: Lifecycle, dispatcher: Dispatcher
) -> None:
    dispatcher.stats = alert_dispatch.DispatchStats(devices=3, sent=2)

    code = await worker._run("once")

    assert code == 0
    assert dispatcher.passes == 1
    assert dispatcher.prunes == 0, "retention is the loop's job, on its own cadence"
    assert (lifecycle.started, lifecycle.stopped) == (1, 1)


async def test_a_pass_with_nothing_to_send_is_a_success(
    runnable: None, lifecycle: Lifecycle, dispatcher: Dispatcher
) -> None:
    """A quiet afternoon is not a failure, and a cron job that treats it as one
    trains its owner to ignore the alerts."""
    dispatcher.stats = alert_dispatch.DispatchStats(devices=3, sent=0)

    assert await worker._run("once") == 0


async def test_an_aborted_pass_exits_non_zero(
    runnable: None, lifecycle: Lifecycle, dispatcher: Dispatcher
) -> None:
    """This is the entire reason ``DispatchStats.aborted`` exists."""
    dispatcher.stats = alert_dispatch.DispatchStats(aborted="feed_unavailable")

    assert await worker._run("once") == 1


async def test_resources_are_released_even_when_the_pass_explodes(
    runnable: None, lifecycle: Lifecycle, dispatcher: Dispatcher
) -> None:
    """``--once`` calls ``run_pass`` directly, so an unexpected exception does
    propagate — but it must not leak the HTTP pool or the cache connection on
    the way out."""
    dispatcher.pass_error = RuntimeError("upstream client exploded")

    with pytest.raises(RuntimeError, match="exploded"):
        await worker._run("once")

    assert lifecycle.stopped == 1


# ---------------------------------------------------------------------------
# --prune
# ---------------------------------------------------------------------------


async def test_prune_mode_sweeps_the_ledger_without_dispatching(
    runnable: None, lifecycle: Lifecycle, dispatcher: Dispatcher
) -> None:
    """For deployments that would rather schedule retention themselves."""
    code = await worker._run("prune")

    assert code == 0
    assert dispatcher.prunes == 1
    assert dispatcher.passes == 0


# ---------------------------------------------------------------------------
# Containment: one bad pass must not end the process
# ---------------------------------------------------------------------------


async def test_an_unexpected_exception_becomes_an_aborted_pass(
    dispatcher: Dispatcher,
) -> None:
    """``run_pass`` is written not to raise. The guard is for the day it does."""
    dispatcher.pass_error = ValueError("a shape nobody predicted")

    stats = await worker._one_pass(1)

    assert stats.aborted == "unhandled_exception"


async def test_retention_runs_on_the_pruning_pass_only(dispatcher: Dispatcher) -> None:
    """Counted in passes rather than clock time so the worker needs no state:
    a restart just means the first prune happens a little later."""
    await worker._one_pass(1)
    assert dispatcher.prunes == 0

    await worker._one_pass(worker.PRUNE_EVERY_PASSES)
    assert dispatcher.prunes == 1


async def test_a_failed_prune_does_not_discard_the_pass_it_followed(
    dispatcher: Dispatcher,
) -> None:
    """Retention is housekeeping; the database's own ``prune_old_data`` is the
    backstop. A pass that failed to prune must still have delivered."""
    dispatcher.stats = alert_dispatch.DispatchStats(sent=2)
    dispatcher.prune_error = RuntimeError("delete timed out")

    stats = await worker._one_pass(worker.PRUNE_EVERY_PASSES)

    assert stats.sent == 2
    assert stats.aborted is None


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------


async def test_a_pending_shutdown_short_circuits_the_interval() -> None:
    """A plain ``asyncio.sleep`` would hold the container for up to fifteen minutes
    after SIGTERM, which orchestrators answer with SIGKILL."""
    stop = asyncio.Event()
    stop.set()

    assert await worker._sleep_or_stop(stop, 900.0) is True


async def test_the_interval_is_waited_out_when_nothing_asks_to_stop() -> None:
    stop = asyncio.Event()

    assert await worker._sleep_or_stop(stop, 0.02) is False


async def test_an_overrunning_pass_starts_the_next_one_immediately() -> None:
    """``interval - elapsed`` can go negative, and that is the right answer: it
    means there is more work than the cadence allows."""
    stop = asyncio.Event()

    assert await worker._sleep_or_stop(stop, -30.0) is False


async def test_the_loop_finishes_the_pass_in_flight_before_stopping(
    monkeypatch: pytest.MonkeyPatch, dispatcher: Dispatcher
) -> None:
    """Killing a pass mid-send is the one thing that strands claims at ``pending``.
    The signal stops the loop, not the pass."""
    captured: List[asyncio.Event] = []
    monkeypatch.setattr(worker, "_install_signal_handlers", captured.append)
    dispatcher.after_pass = lambda: captured[0].set()

    code = await worker._loop()

    assert code == 0
    assert dispatcher.passes == 1


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def test_the_flags_map_to_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    modes: List[str] = []

    async def run(mode: str) -> int:
        modes.append(mode)
        return 0

    # `main` configures logging process-wide, which would strip the handler
    # `caplog` installs and quietly break the log assertions in other files.
    monkeypatch.setattr(worker, "configure_logging", lambda: None)
    monkeypatch.setattr(worker, "_run", run)

    assert worker.main([]) == 0
    assert worker.main(["--once"]) == 0
    assert worker.main(["--prune"]) == 0
    assert modes == ["loop", "once", "prune"]


def test_the_exit_code_survives_the_trip_to_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Everything above is worthless if ``main`` swallows the code."""

    async def run(mode: str) -> int:
        return 2

    monkeypatch.setattr(worker, "configure_logging", lambda: None)
    monkeypatch.setattr(worker, "_run", run)

    assert worker.main(["--once"]) == 2


def test_once_and_prune_cannot_be_combined(monkeypatch: pytest.MonkeyPatch) -> None:
    """They are different jobs with different exit-code meanings."""
    monkeypatch.setattr(worker, "configure_logging", lambda: None)

    with pytest.raises(SystemExit) as exit_info:
        worker.main(["--once", "--prune"])

    assert exit_info.value.code == 2
