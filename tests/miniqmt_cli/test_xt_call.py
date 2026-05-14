"""Tests for the shared xt_call helper: bounded pool, saturation, timeout."""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from miniqmt_cli.server import _xt_call as xtmod
from miniqmt_cli.server._xt_call import (
    XT_POOL_MAX_WORKERS,
    XtCallSaturated,
    XtCallTimeout,
    shutdown_pool,
    xt_call,
)


@pytest.fixture(autouse=True)
def _reset_pool_between_tests():
    """Each test gets a fresh pool + semaphore + breaker map. The module-level
    singletons bind to whichever loop / state the first test created, so we
    tear them down explicitly."""
    shutdown_pool()
    xtmod._sema = None
    xtmod._breaker_reset_all()
    yield
    shutdown_pool()
    xtmod._sema = None
    xtmod._breaker_reset_all()


@pytest.mark.asyncio
async def test_happy_path_returns_result():
    def work(x, y):
        return x + y
    result = await xt_call(work, 2, 3, timeout=1.0, label="add")
    assert result == 5


@pytest.mark.asyncio
async def test_propagates_exceptions_unchanged():
    def boom():
        raise ValueError("kaboom")
    with pytest.raises(ValueError, match="kaboom"):
        await xt_call(boom, timeout=1.0, label="boom")


@pytest.mark.asyncio
async def test_timeout_raises_xt_call_timeout():
    def slow():
        time.sleep(2.0)
        return "late"
    with pytest.raises(XtCallTimeout) as excinfo:
        await xt_call(slow, timeout=0.2, label="slow")
    assert excinfo.value.label == "slow"
    assert excinfo.value.timeout_seconds == 0.2


@pytest.mark.asyncio
async def test_saturation_raises_quickly():
    """Fill every worker slot with a stuck call, then verify the next call
    fails fast at the semaphore with XtCallSaturated -- not at the
    asyncio.to_thread queue and not with the work-level timeout."""
    blockers_done = threading.Event()

    def block_forever():
        # Hold the worker until the test releases blockers_done. This is
        # the production failure mode we're modelling: xtquant network
        # call that never returns.
        blockers_done.wait(timeout=5.0)

    # Saturate the pool. Fire each call with a long work timeout so they
    # don't unwedge themselves -- if they did, the saturation check would
    # be racy.
    blocker_tasks = [
        asyncio.create_task(xt_call(block_forever, timeout=10.0, label=f"block{i}"))
        for i in range(XT_POOL_MAX_WORKERS)
    ]
    # Give the event loop a beat to dispatch all blockers into the pool.
    for _ in range(20):
        await asyncio.sleep(0.01)
        if xtmod.inflight_count() == XT_POOL_MAX_WORKERS:
            break
    assert xtmod.inflight_count() == XT_POOL_MAX_WORKERS

    # The (N+1)th call must fail fast with XtCallSaturated, not wait on
    # the work timeout (10s) or the semaphore default (no timeout).
    start = time.monotonic()
    with pytest.raises(XtCallSaturated) as excinfo:
        await xt_call(lambda: 1, timeout=10.0, label="extra")
    elapsed = time.monotonic() - start
    # Sem acquire timeout is 0.5s; we should fail under ~1s comfortably.
    assert elapsed < 1.5, f"saturation took {elapsed:.2f}s, expected <1.5s"
    assert excinfo.value.max_workers == XT_POOL_MAX_WORKERS

    # Release blockers; the in-flight count must drain back to 0 so other
    # tests aren't poisoned.
    blockers_done.set()
    for t in blocker_tasks:
        try:
            await asyncio.wait_for(t, timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            t.cancel()


@pytest.mark.asyncio
async def test_timeout_does_not_release_semaphore_prematurely():
    """If wait_for cancelled the future and released the semaphore early,
    we'd be able to schedule more calls than the pool can run -- the
    semaphore would lie about capacity. Verify the permit stays held
    until the underlying worker really finishes."""
    work_done = threading.Event()

    def slow_then_done():
        work_done.wait(timeout=5.0)
        return "ok"

    # Saturate with N-1 stuck workers so we have one free slot.
    stuck_done = threading.Event()
    stuck = [
        asyncio.create_task(xt_call(stuck_done.wait, timeout=10.0, label=f"s{i}"))
        for i in range(XT_POOL_MAX_WORKERS - 1)
    ]
    # Wait for them all to acquire permits.
    for _ in range(20):
        await asyncio.sleep(0.01)
        if xtmod.inflight_count() == XT_POOL_MAX_WORKERS - 1:
            break

    # Submit the work-we'll-time-out; expect XtCallTimeout.
    with pytest.raises(XtCallTimeout):
        await xt_call(slow_then_done, timeout=0.2, label="will-timeout")

    # The worker for slow_then_done is still blocked on work_done.wait;
    # its permit should still be held. inflight_count must still be N.
    assert xtmod.inflight_count() == XT_POOL_MAX_WORKERS

    # A new call should hit saturation, NOT succeed past the semaphore.
    with pytest.raises(XtCallSaturated):
        await xt_call(lambda: 1, timeout=5.0, label="should-saturate")

    # Cleanup: unblock everyone.
    work_done.set()
    stuck_done.set()
    for t in stuck:
        try:
            await asyncio.wait_for(t, timeout=2.0)
        except Exception:
            t.cancel()
    # Let the timed-out worker finish so its permit gets released.
    for _ in range(20):
        await asyncio.sleep(0.05)
        if xtmod.inflight_count() == 0:
            break
    assert xtmod.inflight_count() == 0, (
        f"semaphore leaked: inflight={xtmod.inflight_count()} after cleanup"
    )


# ---------------------------------------------------------------------------
# Per-label circuit breaker
#
# Without this, a single API that hangs in xtquant (e.g. query_stock_trades)
# fills the pool with stuck workers because a polling caller keeps calling the
# same broken API. Each stuck thread permanently holds a slot (C ext, no
# cancel possible), so after enough polls the pool saturates and ALL labels
# 503 -- including healthy ones. The breaker isolates a wedged label so other
# labels keep flowing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_breaker_opens_after_threshold_timeouts(monkeypatch):
    """After N consecutive timeouts on the same label, the next call fails
    fast with XtCallSaturated WITHOUT acquiring a pool slot. This is the
    critical property: breaker rejections must not consume slots, otherwise
    the breaker doesn't slow down pool burn at all."""
    monkeypatch.setattr(xtmod, "BREAKER_THRESHOLD", 3)
    monkeypatch.setattr(xtmod, "BREAKER_COOLDOWN_SECONDS", 30.0)

    def slow():
        time.sleep(2.0)

    # Drive 3 timeouts on label "wedged".
    for _ in range(3):
        with pytest.raises(XtCallTimeout):
            await xt_call(slow, timeout=0.05, label="wedged")

    # Pool slots are still held by the 3 stuck workers.
    assert xtmod.inflight_count() == 3

    # The 4th call must be rejected by the BREAKER, not by sem timeout.
    # Distinguishing signal: breaker rejection is instant (<<0.1s); sem
    # timeout would take SEM_ACQUIRE_TIMEOUT_SECONDS (0.5s).
    start = time.monotonic()
    with pytest.raises(XtCallSaturated):
        await xt_call(lambda: 1, timeout=5.0, label="wedged")
    elapsed = time.monotonic() - start
    assert elapsed < 0.1, f"breaker should fail-fast, took {elapsed:.3f}s"

    # And critically: no new slot was consumed.
    assert xtmod.inflight_count() == 3


@pytest.mark.asyncio
async def test_breaker_is_per_label(monkeypatch):
    """A wedge on label A must not affect label B. Pool capacity stays
    available for healthy labels."""
    monkeypatch.setattr(xtmod, "BREAKER_THRESHOLD", 3)

    def slow():
        time.sleep(2.0)

    for _ in range(3):
        with pytest.raises(XtCallTimeout):
            await xt_call(slow, timeout=0.05, label="wedged")

    # "wedged" is now open. But "healthy" label still works.
    result = await xt_call(lambda x: x * 2, 21, timeout=1.0, label="healthy")
    assert result == 42

    # Confirm: "wedged" still rejects.
    with pytest.raises(XtCallSaturated):
        await xt_call(lambda: 1, timeout=1.0, label="wedged")


@pytest.mark.asyncio
async def test_breaker_success_resets_counter(monkeypatch):
    """Two timeouts followed by a success must reset the counter, so the
    next two timeouts shouldn't open the breaker."""
    monkeypatch.setattr(xtmod, "BREAKER_THRESHOLD", 3)

    def slow():
        time.sleep(2.0)

    for _ in range(2):
        with pytest.raises(XtCallTimeout):
            await xt_call(slow, timeout=0.05, label="flap")

    # Healthy call -- counter resets.
    assert await xt_call(lambda: "ok", timeout=1.0, label="flap") == "ok"

    # Two more timeouts -- breaker still closed (counter is at 2, not 4).
    for _ in range(2):
        with pytest.raises(XtCallTimeout):
            await xt_call(slow, timeout=0.05, label="flap")

    # The next call goes through the pool normally (returns immediately
    # because lambda is instant). If breaker were open, it'd raise
    # XtCallSaturated.
    assert await xt_call(lambda: "still-ok", timeout=1.0, label="flap") == "still-ok"


@pytest.mark.asyncio
async def test_breaker_half_open_probe_success_closes(monkeypatch):
    """After cooldown, the breaker enters half_open and lets ONE probe call
    through. If it succeeds, the breaker fully closes (counter reset)."""
    monkeypatch.setattr(xtmod, "BREAKER_THRESHOLD", 3)
    monkeypatch.setattr(xtmod, "BREAKER_COOLDOWN_SECONDS", 0.2)

    def slow():
        time.sleep(2.0)

    for _ in range(3):
        with pytest.raises(XtCallTimeout):
            await xt_call(slow, timeout=0.05, label="recovering")

    # Open: immediate rejection.
    with pytest.raises(XtCallSaturated):
        await xt_call(lambda: 1, timeout=1.0, label="recovering")

    # Wait past cooldown.
    await asyncio.sleep(0.3)

    # Probe call succeeds -- breaker closes.
    assert await xt_call(lambda: "alive", timeout=1.0, label="recovering") == "alive"

    # Subsequent call also succeeds (state is fully closed now).
    assert await xt_call(lambda: "still-alive", timeout=1.0, label="recovering") == "still-alive"


@pytest.mark.asyncio
async def test_breaker_half_open_probe_timeout_reopens(monkeypatch):
    """If the half_open probe times out, the breaker goes back to open with
    a fresh cooldown. We must NOT reset to closed prematurely."""
    monkeypatch.setattr(xtmod, "BREAKER_THRESHOLD", 3)
    monkeypatch.setattr(xtmod, "BREAKER_COOLDOWN_SECONDS", 0.2)

    def slow():
        time.sleep(2.0)

    for _ in range(3):
        with pytest.raises(XtCallTimeout):
            await xt_call(slow, timeout=0.05, label="still-wedged")

    await asyncio.sleep(0.3)

    # Probe -- still hung.
    with pytest.raises(XtCallTimeout):
        await xt_call(slow, timeout=0.05, label="still-wedged")

    # Breaker should be open again. Immediate rejection.
    start = time.monotonic()
    with pytest.raises(XtCallSaturated):
        await xt_call(lambda: 1, timeout=1.0, label="still-wedged")
    assert time.monotonic() - start < 0.1


@pytest.mark.asyncio
async def test_breaker_half_open_serializes_probes(monkeypatch):
    """Only ONE probe may run during half_open. A second concurrent caller
    must be rejected, otherwise a flood at the cooldown boundary could
    consume multiple slots before we know whether the API recovered."""
    monkeypatch.setattr(xtmod, "BREAKER_THRESHOLD", 3)
    monkeypatch.setattr(xtmod, "BREAKER_COOLDOWN_SECONDS", 0.2)

    for _ in range(3):
        with pytest.raises(XtCallTimeout):
            await xt_call(lambda: time.sleep(2.0), timeout=0.05, label="probe-race")

    await asyncio.sleep(0.3)

    # Start a probe that takes long enough that we can race a second caller.
    blocker = threading.Event()

    def slow_probe():
        blocker.wait(timeout=5.0)
        return "probe-done"

    probe = asyncio.create_task(
        xt_call(slow_probe, timeout=10.0, label="probe-race")
    )
    # Yield so the probe registers and grabs probe_in_flight.
    await asyncio.sleep(0.05)

    # Second caller during half_open MUST be rejected -- not start another
    # concurrent xt call.
    with pytest.raises(XtCallSaturated):
        await xt_call(lambda: "second", timeout=1.0, label="probe-race")

    # Release the probe and verify it completes normally.
    blocker.set()
    assert await asyncio.wait_for(probe, timeout=2.0) == "probe-done"


@pytest.mark.asyncio
async def test_breaker_does_not_count_fn_exceptions_as_timeout(monkeypatch):
    """If fn() raises a normal exception, the SDK is responsive -- this is
    NOT a wedge signal. The breaker must not count it."""
    monkeypatch.setattr(xtmod, "BREAKER_THRESHOLD", 3)

    def err():
        raise ValueError("expected")

    # Three fn exceptions -- breaker stays closed.
    for _ in range(3):
        with pytest.raises(ValueError):
            await xt_call(err, timeout=1.0, label="erroring")

    # Next call still allowed through.
    assert await xt_call(lambda: "fine", timeout=1.0, label="erroring") == "fine"
