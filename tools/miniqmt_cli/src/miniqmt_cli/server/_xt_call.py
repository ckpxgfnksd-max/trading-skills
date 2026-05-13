"""Bounded executor for blocking xtquant/xttrader calls.

Why this exists:
- xtquant/xttrader calls are synchronous C-extension calls that can block
  for an arbitrarily long time when the broker network path is wedged.
- They cannot be safely run inline in an async def handler (event-loop
  deadlock; see commit 66306e5).
- They cannot be run on the default asyncio.to_thread executor either:
  that pool is shared with other code (e.g. risk.ensure_baseline), and a
  sustained xtquant wedge would silently fill the pool while the event
  loop kept advancing -- exactly the daemon-is-up-but-nothing-works
  failure mode the watchdog cannot detect.

This module owns a dedicated ThreadPoolExecutor and a same-sized
asyncio.Semaphore. Each xt_call() takes one semaphore permit before
dispatching to the pool; the permit is released by an add_done_callback
that fires only when the underlying worker thread truly finishes. We use
asyncio.shield() so the caller's wait_for timeout does not cancel the
asyncio.Future and so the semaphore bookkeeping stays consistent with
real pool occupancy. The result: when xtquant is wedged, the first 8
in-flight calls hold their slots forever, and the 9th call fast-fails
with XtCallSaturated within SEM_ACQUIRE_TIMEOUT_SECONDS -- giving
callers a clear "backend stuck" signal rather than a silent stall.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

XT_POOL_MAX_WORKERS = 8
SEM_ACQUIRE_TIMEOUT_SECONDS = 0.5


class XtCallError(Exception):
    """Base class for xt_call signalling errors."""


class XtCallTimeout(XtCallError):
    """The worker did not finish within the requested timeout. The thread
    is still running in the pool; the slot is not yet free. Callers that
    submitted state-changing operations (order/cancel) MUST reconcile."""
    def __init__(self, label: str, timeout_seconds: float) -> None:
        self.label = label
        self.timeout_seconds = timeout_seconds
        super().__init__(f"xt {label} timed out after {timeout_seconds:.1f}s")


class XtCallSaturated(XtCallError):
    """All worker slots are occupied by calls that have not yet completed,
    indicating the xtquant backend is likely wedged. The submitted call
    was rejected before reaching the broker."""
    def __init__(self, max_workers: int) -> None:
        self.max_workers = max_workers
        super().__init__(
            f"xtquant call pool saturated ({max_workers} stuck in flight)"
        )


_pool: Optional[ThreadPoolExecutor] = None
_sema: Optional[asyncio.Semaphore] = None


def _ensure_pool() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(
            max_workers=XT_POOL_MAX_WORKERS, thread_name_prefix="xt",
        )
    return _pool


def _ensure_sema() -> asyncio.Semaphore:
    global _sema
    if _sema is None:
        _sema = asyncio.Semaphore(XT_POOL_MAX_WORKERS)
    return _sema


async def xt_call(
    fn: Callable[..., Any], *args, timeout: float, label: str,
) -> Any:
    """Run a blocking xtquant/xttrader function on the dedicated pool.

    Raises:
        XtCallSaturated: if no permit was available within
            SEM_ACQUIRE_TIMEOUT_SECONDS.
        XtCallTimeout: if the worker did not finish within ``timeout``.
        Anything raised by ``fn``: propagated unchanged.
    """
    pool = _ensure_pool()
    sema = _ensure_sema()

    try:
        await asyncio.wait_for(sema.acquire(), timeout=SEM_ACQUIRE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        log.warning(
            "xt_call %s rejected: pool saturated (cap=%d)",
            label, XT_POOL_MAX_WORKERS,
        )
        raise XtCallSaturated(XT_POOL_MAX_WORKERS) from None

    try:
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(pool, fn, *args)
    except Exception:
        # Submission itself failed (e.g. pool shut down). Return the permit.
        sema.release()
        raise

    # Release the permit only when the worker truly finishes, not when our
    # caller's wait_for gives up. Otherwise the semaphore would say "free"
    # while the pool slot was still occupied by a stuck thread.
    fut.add_done_callback(lambda _f: sema.release())

    try:
        # shield: don't let wait_for cancel fut. Cancelling an already-running
        # run_in_executor future is a no-op anyway (the worker thread keeps
        # going), but the asyncio.Future would transition to CANCELLED and
        # the done_callback would fire prematurely, releasing the semaphore
        # while the thread is still busy.
        return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning(
            "xt_call %s timed out after %.1fs (worker still running in pool)",
            label, timeout,
        )
        raise XtCallTimeout(label, timeout) from None


def shutdown_pool() -> None:
    """Tear down the executor on daemon shutdown. Does NOT wait for stuck
    workers — they're already lost. cancel_futures cancels only the work
    that has not started yet."""
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None


def inflight_count() -> int:
    """Approximate count of permits in use. Best-effort, for metrics/log."""
    if _sema is None:
        return 0
    # asyncio.Semaphore exposes ._value (private but stable). On Python 3.11+
    # there's no public API for this; we treat it as observational only.
    try:
        return XT_POOL_MAX_WORKERS - _sema._value  # type: ignore[attr-defined]
    except AttributeError:
        return -1
