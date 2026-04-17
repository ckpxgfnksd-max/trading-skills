"""Daemon-level state: trader session pool, idempotency cache, audit hook."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
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
        self._login_locks: Dict[str, asyncio.Lock] = {}
        self._idem: Dict[str, IdempotencyEntry] = {}
        self._idem_lock = asyncio.Lock()
        self._xtquant_loaded = False
        self._xtquant_load_lock = asyncio.Lock()
        # Order event subscriber management
        self._order_subscribers: list[asyncio.Queue] = []
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

        Thread-safe: asyncio.Queue.put_nowait is safe from any thread;
        RiskManager uses its own threading.Lock.
        """
        for q in self._order_subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("order subscriber queue full, dropping event")
        try:
            self.risk.on_trade_event(event)
        except Exception:
            log.exception("risk.on_trade_event failed")

    async def subscribe_orders(self) -> asyncio.Queue:
        """Register a new order event subscriber. Returns its queue."""
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        async with self._sub_lock:
            self._order_subscribers.append(q)
        return q

    async def unsubscribe_orders(self, q: asyncio.Queue) -> None:
        """Remove an order event subscriber."""
        async with self._sub_lock:
            try:
                self._order_subscribers.remove(q)
            except ValueError:
                pass

    async def get_trader(self, account_name: str) -> TraderHandle:
        if self.dry_run:
            raise RuntimeError("trader is unavailable in --dry-run daemon mode")
        await self.ensure_xtquant()
        handle = self._traders.get(account_name)
        if handle is not None:
            return handle
        lock = self._lock_for(account_name)
        async with lock:
            handle = self._traders.get(account_name)
            if handle is not None:
                return handle
            acc_cfg = self.get_account(account_name)
            trader = xttrader_adapter.create_trader(
                self.cfg.resolved_session_id(),
                self.cfg.resolved_userdata_mini_path(),
                dispatcher=self.dispatch_order_event,
                account_name=account_name,
            )
            acc = xttrader_adapter.subscribe_account(trader, acc_cfg)
            handle = TraderHandle(trader=trader, acc=acc)
            self._traders[account_name] = handle
            log.info("trader logged in: %s", account_name)
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
