"""Daemon-level state: trader session pool, idempotency cache, audit hook."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from miniqmt_cli.server import xtdata_adapter, xttrader_adapter
from miniqmt_cli.server.audit import AuditLog
from miniqmt_cli.server.risk import RiskManager
from miniqmt_cli.server_config import AccountConfig, ServerConfig

log = logging.getLogger(__name__)


@dataclass
class TraderHandle:
    trader: Any
    acc: Any


@dataclass
class TraderState:
    """Per-account connection state, observable via /health.

    `state` reflects what the daemon last heard from xtquant on this SDK
    channel — not what miniQMT or the broker know. Daemon cannot probe
    miniQMT's GUI / broker independently; this is the closest signal it has.
    """
    state: str = "never_connected"  # never_connected | alive | lost
    last_connect_at: Optional[str] = None  # ISO 8601 UTC
    last_disconnect_at: Optional[str] = None


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


@dataclass
class IdempotencyEntry:
    result: Dict[str, Any]
    created_at: float


class SessionManager:
    def __init__(self, cfg: ServerConfig, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.audit = AuditLog(
            cfg.resolved_audit_log_path(),
            warn_size_bytes=cfg.audit_warn_size_bytes,
        )
        self._traders: Dict[str, TraderHandle] = {}
        # Per-account TraderState. Mutated from both asyncio handlers and the
        # xtquant callback thread (on_disconnected), so guard with a plain
        # threading.Lock — short critical section, no awaits inside.
        self._trader_states: Dict[str, TraderState] = {}
        self._trader_states_lock = threading.Lock()
        self._login_locks: Dict[str, asyncio.Lock] = {}
        self._idem: Dict[str, IdempotencyEntry] = {}
        self._idem_lock = asyncio.Lock()
        self._xtquant_loaded = False
        self._xtquant_load_lock = asyncio.Lock()
        # Order event subscriber management. Each entry is (queue, loop) so
        # that dispatch_order_event — called from xtquant's callback thread —
        # can schedule the put via loop.call_soon_threadsafe instead of the
        # cross-thread-unsafe asyncio.Queue.put_nowait.
        self._order_subscribers: list[tuple[asyncio.Queue, asyncio.AbstractEventLoop]] = []
        self._sub_lock = asyncio.Lock()
        # Phase 2: risk manager is composed into session (single-direction dep).
        self.risk = RiskManager(cfg, self.audit, self._xttrader_ctx_for_risk)

    def get_account(self, name: str) -> AccountConfig:
        acc = self.cfg.accounts.get(name)
        if acc is None:
            raise KeyError(name)
        return acc

    async def ensure_xtquant(self) -> None:
        if self._xtquant_loaded or self.dry_run:
            return
        async with self._xtquant_load_lock:
            if self._xtquant_loaded:
                return
            xtdata_adapter.ensure_loaded(self.cfg.qmt_path)
            self._xtquant_loaded = True

    def _lock_for(self, account_name: str) -> asyncio.Lock:
        lock = self._login_locks.get(account_name)
        if lock is None:
            lock = asyncio.Lock()
            self._login_locks[account_name] = lock
        return lock

    def dispatch_order_event(self, event: dict) -> None:
        """Called from xtquant callback thread. Fan-out to SSE subscribers
        AND forward to RiskManager for pending/snapshot updates.

        asyncio.Queue is NOT thread-safe; we must schedule the put on each
        subscriber's owning event loop via call_soon_threadsafe.
        RiskManager.on_trade_event uses its own threading.Lock and is safe
        to invoke directly from this thread.
        """
        # Snapshot the subscriber list so a concurrent subscribe/unsubscribe
        # can't mutate it under us mid-iteration.
        for q, loop in list(self._order_subscribers):
            if loop.is_closed():
                continue
            try:
                loop.call_soon_threadsafe(self._enqueue_order_event, q, event)
            except RuntimeError:
                # Loop shut down between is_closed() check and the call.
                continue
        try:
            self.risk.on_trade_event(event)
        except Exception:
            log.exception("risk.on_trade_event failed")

    @staticmethod
    def _enqueue_order_event(q: asyncio.Queue, event: dict) -> None:
        """Runs on the subscriber's event loop; safe to use put_nowait here."""
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("order subscriber queue full, dropping event")

    async def subscribe_orders(self) -> asyncio.Queue:
        """Register a new order event subscriber. Returns its queue, bound to
        the calling coroutine's running event loop."""
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        async with self._sub_lock:
            self._order_subscribers.append((q, loop))
        return q

    async def unsubscribe_orders(self, q: asyncio.Queue) -> None:
        """Remove an order event subscriber."""
        async with self._sub_lock:
            for i, (entry_q, _loop) in enumerate(self._order_subscribers):
                if entry_q is q:
                    self._order_subscribers.pop(i)
                    return

    async def get_trader(self, account_name: str) -> TraderHandle:
        if self.dry_run:
            raise RuntimeError("trader is unavailable in --dry-run daemon mode")
        await self.ensure_xtquant()
        handle = self._traders.get(account_name)
        if handle is None:
            lock = self._lock_for(account_name)
            async with lock:
                handle = self._traders.get(account_name)
                if handle is None:
                    acc_cfg = self.get_account(account_name)
                    trader = xttrader_adapter.create_trader(
                        self.cfg.resolved_session_id(),
                        self.cfg.resolved_userdata_mini_path(),
                        dispatcher=self.dispatch_order_event,
                        account_name=account_name,
                        on_disconnect=self._on_trader_disconnected,
                    )
                    acc = xttrader_adapter.subscribe_account(trader, acc_cfg)
                    handle = TraderHandle(trader=trader, acc=acc)
                    self._traders[account_name] = handle
                    self._mark_trader_alive(account_name)
                    log.info("trader logged in: %s", account_name)
        # Always ensure today's risk baseline. ensure_baseline fast-paths to
        # a dict lookup when already captured for today; on the slow path
        # (first login / midnight rollover) it blocks on a broker asset
        # query for tens to hundreds of ms, so dispatch to a worker thread
        # to keep the event loop responsive. ensure_baseline already logs
        # its own WARNING on capture failure — we just swallow here so a
        # transient broker blip doesn't fail the trader handle itself.
        try:
            await asyncio.to_thread(self.risk.ensure_baseline, account_name)
        except Exception:
            log.debug(
                "baseline capture for %s failed at get_trader; "
                "will retry on next access or first order",
                account_name,
            )
        return handle

    def _xttrader_ctx_for_risk(self, account_name: str) -> tuple:
        """Synchronous adapter for RiskManager's xttrader_ctx callable.

        RiskManager may be invoked from various contexts (HTTP handlers,
        xtquant callback threads); it needs a synchronous getter. We look
        up the already-logged-in trader; if none, raise RuntimeError so
        RiskManager can surface BaselineUnavailable.
        """
        if self.dry_run:
            raise RuntimeError("trader unavailable in dry_run")
        handle = self._traders.get(account_name)
        if handle is None:
            raise RuntimeError(f"trader for {account_name} not logged in")
        return (handle.trader, handle.acc)

    def trader_logged_in_count(self) -> int:
        return len(self._traders)

    def _mark_trader_alive(self, account_name: str) -> None:
        with self._trader_states_lock:
            ts = self._trader_states.setdefault(account_name, TraderState())
            ts.state = "alive"
            ts.last_connect_at = _utcnow_iso()

    def _on_trader_disconnected(self, account_name: str) -> None:
        """Invoked from xtquant's callback thread when the SDK channel drops.

        Note we only flip the state flag here — we do NOT pop self._traders.
        The existing pool semantics are preserved so in-flight handlers
        holding a TraderHandle don't crash; future calls into a `lost` trader
        will surface the broker error naturally. Self-healing (recreate on
        next get_trader) is a separate change.
        """
        with self._trader_states_lock:
            ts = self._trader_states.setdefault(account_name, TraderState())
            ts.state = "lost"
            ts.last_disconnect_at = _utcnow_iso()

    def trader_state_view(self, account_name: str) -> Dict[str, Any]:
        with self._trader_states_lock:
            ts = self._trader_states.get(account_name)
            if ts is None:
                return {
                    "state": "never_connected",
                    "last_connect_at": None,
                    "last_disconnect_at": None,
                }
            return {
                "state": ts.state,
                "last_connect_at": ts.last_connect_at,
                "last_disconnect_at": ts.last_disconnect_at,
            }

    async def idempotency_lookup(self, client_req_id: str) -> Optional[Dict[str, Any]]:
        async with self._idem_lock:
            entry = self._idem.get(client_req_id)
            if entry is None:
                return None
            age = time.time() - entry.created_at
            if age > self.cfg.idempotency_ttl_seconds:
                self._idem.pop(client_req_id, None)
                return None
            return entry.result

    async def idempotency_store(self, client_req_id: str, result: Dict[str, Any]) -> None:
        async with self._idem_lock:
            self._idem[client_req_id] = IdempotencyEntry(
                result=result, created_at=time.time()
            )

    def xtquant_state(self) -> str:
        if not self._xtquant_loaded:
            return "xtquant_missing"
        if not self._traders:
            return "no_trader"
        return "ready"
