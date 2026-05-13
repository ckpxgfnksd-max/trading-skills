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
    """Each test gets a fresh pool + semaphore. The module-level singletons
    bind to whichever loop / state the first test created, so we tear them
    down explicitly."""
    shutdown_pool()
    xtmod._sema = None
    yield
    shutdown_pool()
    xtmod._sema = None


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
