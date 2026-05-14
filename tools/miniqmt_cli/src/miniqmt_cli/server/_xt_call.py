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

Per-label circuit breaker:
- The pool alone is not enough. If a single xtquant API hangs (e.g.
  query_stock_trades after some order lifecycle events) and a poller keeps
  hitting it, the same wedged label burns through all 8 slots in minutes,
  taking down healthy labels too. The 2026-05-14 incident played out
  exactly this way.
- The breaker tracks consecutive XtCallTimeouts per label. After
  BREAKER_THRESHOLD on the same label, it opens: subsequent calls of that
  label fast-fail with XtCallSaturated WITHOUT consuming a slot. Other
  labels are unaffected. After BREAKER_COOLDOWN_SECONDS the breaker goes
  half_open and lets exactly one probe through; success closes it, another
  timeout reopens with a fresh cooldown. fn-level exceptions (non-timeout)
  count as success because the SDK clearly responded.
"""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

log = logging.getLogger(__name__)

XT_POOL_MAX_WORKERS = 8
SEM_ACQUIRE_TIMEOUT_SECONDS = 0.5
BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN_SECONDS = 30.0


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


@dataclass
class _BreakerState:
    consecutive_timeouts: int = 0
    state: str = "closed"  # closed | open | half_open
    open_until: float = 0.0
    probe_in_flight: bool = False


_breakers: Dict[str, _BreakerState] = {}


def _breaker_check(label: str) -> None:
    """Pre-flight check before acquiring a pool slot. Raises XtCallSaturated
    if the breaker for this label is open (or half_open with a probe
    already in flight). Mutates state on the open->half_open transition.

    Single event-loop assumption: no lock needed."""
    st = _breakers.get(label)
    if st is None:
        return
    if st.state == "open":
        if time.monotonic() < st.open_until:
            raise XtCallSaturated(XT_POOL_MAX_WORKERS)
        # Cooldown elapsed: enter half_open. Caller becomes the probe.
        st.state = "half_open"
        st.probe_in_flight = False
    if st.state == "half_open":
        if st.probe_in_flight:
            raise XtCallSaturated(XT_POOL_MAX_WORKERS)
        st.probe_in_flight = True


def _breaker_record_success(label: str) -> None:
    """Any non-timeout outcome (result or fn exception) resets the breaker:
    the SDK responded, so it's not wedged."""
    st = _breakers.get(label)
    if st is None:
        return
    st.consecutive_timeouts = 0
    st.state = "closed"
    st.probe_in_flight = False


def _breaker_record_timeout(label: str) -> None:
    """An XtCallTimeout increments the counter. If the call was a half_open
    probe, immediately reopen with a fresh cooldown (the wedge persists).
    Otherwise, open once the threshold is reached."""
    st = _breakers.setdefault(label, _BreakerState())
    st.consecutive_timeouts += 1
    if st.state == "half_open":
        st.state = "open"
        st.open_until = time.monotonic() + BREAKER_COOLDOWN_SECONDS
        st.probe_in_flight = False
        log.warning(
            "xt_call breaker %s: half_open probe timed out, reopening for %.0fs",
            label, BREAKER_COOLDOWN_SECONDS,
        )
    elif st.state == "closed" and st.consecutive_timeouts >= BREAKER_THRESHOLD:
        st.state = "open"
        st.open_until = time.monotonic() + BREAKER_COOLDOWN_SECONDS
        log.warning(
            "xt_call breaker %s: opened after %d consecutive timeouts, "
            "rejecting for %.0fs",
            label, st.consecutive_timeouts, BREAKER_COOLDOWN_SECONDS,
        )


def _breaker_reset_all() -> None:
    """Clear all breaker state. Used in tests; not exposed via routes."""
    _breakers.clear()


def breaker_snapshot() -> Dict[str, Dict[str, Any]]:
    """Observability: snapshot of breaker states for /health-style endpoints.
    Returns {label: {state, consecutive_timeouts, cooldown_remaining}}.
    Only includes labels with non-default state."""
    now = time.monotonic()
    out: Dict[str, Dict[str, Any]] = {}
    for label, st in _breakers.items():
        if st.state == "closed" and st.consecutive_timeouts == 0:
            continue
        cooldown_remaining = max(0.0, st.open_until - now) if st.state == "open" else 0.0
        out[label] = {
            "state": st.state,
            "consecutive_timeouts": st.consecutive_timeouts,
            "cooldown_remaining": round(cooldown_remaining, 1),
        }
    return out


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
        XtCallSaturated: if the breaker for ``label`` is open, or if no
            permit was available within SEM_ACQUIRE_TIMEOUT_SECONDS.
        XtCallTimeout: if the worker did not finish within ``timeout``.
        Anything raised by ``fn``: propagated unchanged.
    """
    # Breaker check first -- a rejection here must NOT consume a slot,
    # otherwise the breaker can't slow pool burn.
    _breaker_check(label)

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
        result = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning(
            "xt_call %s timed out after %.1fs (worker still running in pool)",
            label, timeout,
        )
        _breaker_record_timeout(label)
        raise XtCallTimeout(label, timeout) from None
    except BaseException:
        # fn raised; SDK is responsive, so this is not a wedge signal.
        _breaker_record_success(label)
        raise
    _breaker_record_success(label)
    return result


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
