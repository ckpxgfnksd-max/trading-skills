"""Risk management: per-account breaker, baseline, pending tracking."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

STATE_VERSION = 1

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _today_str() -> str:
    """Trading date in Shanghai time (A-share market)."""
    return datetime.now(_SHANGHAI_TZ).strftime("%Y%m%d")


@dataclass
class AccountRiskState:
    trade_date: str
    baseline_total_asset: float
    baseline_captured_at: str
    baseline_imprecise: bool = False
    breaker_tripped: bool = False
    breaker_reason: Optional[str] = None
    breaker_tripped_at: Optional[str] = None
    reset_history: List[dict] = field(default_factory=list)


@dataclass
class RiskStateFile:
    path: Path
    accounts: Dict[str, AccountRiskState] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def load(cls, path) -> "RiskStateFile":
        state = cls(path=Path(path).expanduser(), accounts={})
        if not state.path.exists():
            return state
        try:
            with open(state.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("risk state root is not an object")
            for name, raw in (data.get("accounts") or {}).items():
                state.accounts[name] = AccountRiskState(**raw)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            log.error(
                "risk state file unreadable at %s: %s; quarantining and starting empty",
                state.path, e,
            )
            state.accounts.clear()
            try:
                quarantine = state.path.with_suffix(
                    state.path.suffix + f".corrupt-{int(time.time())}"
                )
                os.replace(state.path, quarantine)
                log.error("corrupt risk state quarantined to %s", quarantine)
            except OSError:
                pass
        return state

    def save(self) -> None:
        """Strict atomic write: builds payload under lock, os.replace on commit."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            payload = {
                "version": STATE_VERSION,
                "accounts": {n: asdict(s) for n, s in self.accounts.items()},
            }
            tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, allow_nan=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except (OSError, AttributeError):
                    pass
            os.replace(tmp_path, self.path)


class BaselineUnavailable(RuntimeError):
    """Raised when baseline cannot be captured (fail closed)."""


def _capture_is_imprecise() -> bool:
    """True if Shanghai time is at or past A-share market open (09:30).

    A daemon that starts after this time captures a baseline that does
    NOT reflect pre-open losses; callers should surface this warning.
    """
    from datetime import time as _time
    now = datetime.now(_SHANGHAI_TZ).time()
    return now >= _time(9, 30)


SNAPSHOT_TTL = 30.0
SNAPSHOT_HARD_EXPIRY = 300.0


class SnapshotStale(RuntimeError):
    """Raised when snapshot can't be refreshed and hard expiry is exceeded."""


@dataclass
class AccountSnapshot:
    total_asset: float
    positions_by_code: Dict[str, Dict[str, Any]]
    refreshed_at: float
    stale: bool = False


@dataclass
class PendingEntry:
    buy_volume: int = 0
    buy_amount: float = 0.0
    by_order_id: Dict[int, Dict[str, float]] = field(default_factory=dict)


@dataclass
class RiskDecision:
    allow: bool
    reject_code: Optional[str] = None
    reject_detail: Optional[str] = None


def _get_last_price(code: str) -> Optional[float]:
    """Fetch last_price via xtdata get_full_tick. Returns None if unavailable."""
    try:
        from miniqmt_cli.server import xtdata_adapter
        ticks = xtdata_adapter.get_full_tick([code])
        entry = ticks.get(code, {})
        lp = entry.get("lastPrice") or entry.get("last_price")
        return float(lp) if lp else None
    except Exception as e:
        log.warning("get_last_price for %s failed: %s", code, e)
        return None


def _xt_buy_direction() -> int:
    """Return xtconstant.STOCK_BUY, falling back to 23 if xtquant unavailable."""
    try:
        import xtquant.xtconstant as c  # type: ignore
        return int(getattr(c, "STOCK_BUY", 23))
    except Exception:
        return 23


class RiskManager:
    def __init__(
        self,
        cfg: "ServerConfig",
        audit: "AuditLog",
        xttrader_ctx: Callable[[str], tuple],
    ) -> None:
        self._cfg = cfg
        self._audit = audit
        self._xttrader_ctx = xttrader_ctx
        self._state = RiskStateFile.load(cfg.resolved_risk_state_path())
        self._snapshots: Dict[str, AccountSnapshot] = {}
        self._pending: Dict[str, Dict[str, PendingEntry]] = {}
        self._order_window: Dict[str, Deque[float]] = {}
        self._pending_rebuilt: Set[str] = set()
        self._lock = threading.Lock()

    def ensure_baseline(self, account_name: str) -> None:
        """Capture baseline if missing or trade_date mismatches today.

        Fail closed: on query error, raises BaselineUnavailable.
        """
        today = _today_str()
        existing = self._state.accounts.get(account_name)
        if existing is not None and existing.trade_date == today:
            return

        trader, acc = self._xttrader_ctx(account_name)
        try:
            from miniqmt_cli.server import xttrader_adapter  # deferred: avoid session->risk->xttrader cycle risk
            asset = xttrader_adapter.query_stock_asset(trader, acc)
        except Exception as e:
            log.warning("baseline capture failed for %s: %s", account_name, e)
            raise BaselineUnavailable(str(e)) from e

        total = float(asset.get("total_asset", 0.0))
        if total <= 0:
            raise BaselineUnavailable(f"total_asset is {total}")

        imprecise = _capture_is_imprecise()
        new_state = AccountRiskState(
            trade_date=today,
            baseline_total_asset=total,
            baseline_captured_at=_iso_now(),
            baseline_imprecise=imprecise,
        )
        with self._lock:
            current = self._state.accounts.get(account_name)
            if current is not None and current.trade_date == today:
                # Lost the race — another thread already captured for today
                return
            self._state.accounts[account_name] = new_state
            self._state.save()
        self._audit.append(
            phase="risk_baseline_capture",
            account=account_name,
            trade_date=today,
            baseline_total_asset=total,
            imprecise=imprecise,
        )

    def get_snapshot(self, account_name: str) -> AccountSnapshot:
        """Return a fresh-enough snapshot, refreshing if stale or past TTL.

        Locking discipline: NEVER hold self._lock during xtquant queries
        (they block for 50-400ms). Lock only for the dict assignment.
        """
        snap = self._snapshots.get(account_name)
        now = time.monotonic()
        needs_refresh = (
            snap is None
            or snap.stale
            or (now - snap.refreshed_at) > SNAPSHOT_TTL
        )
        if not needs_refresh:
            return snap

        try:
            new_snap = self._do_refresh(account_name)
        except Exception as e:
            if snap is not None and (now - snap.refreshed_at) <= SNAPSHOT_HARD_EXPIRY:
                log.warning(
                    "snapshot refresh failed for %s (%s); using stale snapshot age=%.1fs",
                    account_name, e, now - snap.refreshed_at,
                )
                return snap
            raise SnapshotStale(
                f"snapshot refresh failed and no usable cache: {e}"
            ) from e

        with self._lock:
            self._snapshots[account_name] = new_snap
        return new_snap

    def _do_refresh(self, account_name: str) -> AccountSnapshot:
        trader, acc = self._xttrader_ctx(account_name)
        from miniqmt_cli.server import xttrader_adapter
        asset = xttrader_adapter.query_stock_asset(trader, acc)
        positions = xttrader_adapter.query_stock_positions(trader, acc)
        return AccountSnapshot(
            total_asset=float(asset.get("total_asset", 0.0)),
            positions_by_code={
                p.get("stock_code", ""): p
                for p in positions
                if p.get("stock_code")
            },
            refreshed_at=time.monotonic(),
            stale=False,
        )

    def record_accepted(
        self, account: str, side: str, code: str, volume: int,
        price: float, order_id: int,
    ) -> None:
        """Call after an order is accepted by xttrader. Updates frequency
        window; for buys, adds to pending tracking by order_id.
        """
        with self._lock:
            self._order_window.setdefault(account, deque()).append(time.monotonic())
            if side == "buy":
                entries = self._pending.setdefault(account, {})
                entry = entries.setdefault(code, PendingEntry())
                entry.buy_volume += volume
                entry.buy_amount += volume * price
                entry.by_order_id[order_id] = {
                    "volume": volume, "amount": volume * price,
                }

    def _add_pending_only(
        self, account: str, code: str, volume: int, price: float, order_id: int,
    ) -> None:
        """Update pending tracking without touching the frequency window.

        Used by _rebuild_pending to replay open orders from xtquant without
        re-counting them against max_orders_per_minute.
        """
        with self._lock:
            entries = self._pending.setdefault(account, {})
            entry = entries.setdefault(code, PendingEntry())
            entry.buy_volume += volume
            entry.buy_amount += volume * price
            entry.by_order_id[order_id] = {
                "volume": volume, "amount": volume * price,
            }

    def on_trade_event(self, event: dict) -> None:
        """Called from xtquant callback thread. Fan-in point for order_status
        and trade events. Safe to call with events for unknown accounts.
        """
        account = event.get("account")
        if not account or account not in self._cfg.accounts:
            return
        evt_type = event.get("type")
        if evt_type == "order_status":
            self._handle_order_status(event)
        elif evt_type == "trade":
            with self._lock:
                snap = self._snapshots.get(account)
                if snap:
                    snap.stale = True

    def _handle_order_status(self, event: dict) -> None:
        status = event.get("status")
        order_id = event.get("order_id")
        account = event.get("account")
        if order_id is None:
            return
        if status in {"filled", "cancelled", "rejected", "expired"}:
            self._remove_pending_by_order_id(account, int(order_id))
        elif status == "partially_filled":
            remaining = max(
                int(event.get("volume", 0)) - int(event.get("filled_volume", 0)),
                0,
            )
            self._reduce_pending_to_remaining(account, int(order_id), remaining)

    def _remove_pending_by_order_id(self, account: str, order_id: int) -> None:
        with self._lock:
            codes = self._pending.get(account, {})
            for code, entry in list(codes.items()):
                if order_id in entry.by_order_id:
                    removed = entry.by_order_id.pop(order_id)
                    entry.buy_volume -= int(removed["volume"])
                    entry.buy_amount -= float(removed["amount"])
                    if entry.buy_volume <= 0:
                        del codes[code]
                    break

    def _reduce_pending_to_remaining(
        self, account: str, order_id: int, remaining_volume: int,
    ) -> None:
        with self._lock:
            codes = self._pending.get(account, {})
            for code, entry in list(codes.items()):
                if order_id in entry.by_order_id:
                    old = entry.by_order_id[order_id]
                    old_vol = int(old["volume"])
                    old_amt = float(old["amount"])
                    if old_vol <= 0:
                        return
                    price_per = old_amt / old_vol
                    new_amt = remaining_volume * price_per
                    entry.by_order_id[order_id] = {
                        "volume": remaining_volume, "amount": new_amt,
                    }
                    entry.buy_volume += (remaining_volume - old_vol)
                    entry.buy_amount += (new_amt - old_amt)
                    if entry.buy_volume <= 0:
                        del codes[code]
                    break

    def trip_breaker(self, account: str, reason: str) -> None:
        with self._lock:
            state = self._state.accounts.get(account)
            if state is None:
                log.error("cannot trip breaker for %s: no baseline state", account)
                return
            state.breaker_tripped = True
            state.breaker_reason = reason
            state.breaker_tripped_at = _iso_now()
            baseline_value = state.baseline_total_asset  # snapshot under lock
            self._state.save()
        snap = self._snapshots.get(account)
        self._audit.append(
            phase="risk_breaker_trip",
            account=account,
            reason=reason,
            baseline_total_asset=baseline_value,
            current_total_asset=snap.total_asset if snap else None,
            daily_pnl=(snap.total_asset - baseline_value) if snap else None,
        )
        log.warning("RISK BREAKER TRIPPED for %s: %s", account, reason)

    def reset_breaker(self, account: str, operator_note: str) -> dict:
        with self._lock:
            state = self._state.accounts.get(account)
            if state is None or not state.breaker_tripped:
                raise ValueError("breaker is not tripped")
            previous_reason = state.breaker_reason
            reset_at = _iso_now()
            state.reset_history.append({
                "reset_at": reset_at,
                "previous_reason": previous_reason,
                "operator_note": operator_note,
            })
            state.breaker_tripped = False
            state.breaker_reason = None
            state.breaker_tripped_at = None
            self._state.save()
        self._audit.append(
            phase="risk_breaker_reset",
            account=account,
            previous_reason=previous_reason,
            operator_note=operator_note,
        )
        return {"account": account, "previous_reason": previous_reason, "reset_at": reset_at}

    def check_order(
        self, account: str, side: str, code: str, volume: int,
        price: float, order_type: str = "limit",
    ) -> RiskDecision:
        eff = self._cfg.effective_risk(account)
        if not eff.enabled:
            return RiskDecision(allow=True)

        def _breaker_check() -> RiskDecision:
            state = self._state.accounts.get(account)
            if state and state.breaker_tripped:
                if side == "sell":
                    pos = self._safe_snapshot(account).positions_by_code.get(code, {})
                    if int(pos.get("volume", 0)) >= volume:
                        return RiskDecision(allow=True)
                return RiskDecision(
                    allow=False, reject_code="BREAKER_TRIPPED",
                    reject_detail=f"breaker tripped: {state.breaker_reason}",
                )
            return RiskDecision(allow=True)

        decision = _breaker_check()
        if not decision.allow:
            return decision

        try:
            self.ensure_baseline(account)
        except BaselineUnavailable as e:
            return RiskDecision(
                allow=False, reject_code="BASELINE_PENDING",
                reject_detail=str(e),
            )

        try:
            snap = self.get_snapshot(account)
        except SnapshotStale as e:
            return RiskDecision(
                allow=False, reject_code="SNAPSHOT_STALE",
                reject_detail=str(e),
            )

        if account not in self._pending_rebuilt:
            self._rebuild_pending(account)
            self._pending_rebuilt.add(account)

        state = self._state.accounts[account]
        daily_pnl = snap.total_asset - state.baseline_total_asset
        if daily_pnl < -eff.max_daily_loss:
            self.trip_breaker(
                account, f"daily_loss {daily_pnl:.2f} < -{eff.max_daily_loss}",
            )
            return _breaker_check()

        window = self._order_window.setdefault(account, deque())
        now = time.monotonic()
        while window and now - window[0] > 60.0:
            window.popleft()
        if len(window) >= eff.max_orders_per_minute:
            return RiskDecision(
                allow=False, reject_code="FREQUENCY",
                reject_detail=f"{len(window)} orders in last 60s >= {eff.max_orders_per_minute}",
            )

        if side == "buy":
            held = {
                c for c, p in snap.positions_by_code.items()
                if int(p.get("volume", 0)) > 0
            }
            pending_codes = set(self._pending.get(account, {}).keys())
            tracked = held | pending_codes
            if code not in tracked and len(tracked) >= eff.max_positions:
                return RiskDecision(
                    allow=False, reject_code="MAX_POSITIONS",
                    reject_detail=f"holding {len(tracked)} >= {eff.max_positions} (new: {code})",
                )

            if order_type == "limit":
                est_price = float(price)
            else:
                last = _get_last_price(code)
                if (not price or price <= 0) and (last is None or last <= 0):
                    return RiskDecision(
                        allow=False, reject_code="PRICE_UNAVAILABLE",
                        reject_detail=f"no price for market order on {code}",
                    )
                est_price = max(float(price or 0), float(last or 0))
            existing_mv = float(
                snap.positions_by_code.get(code, {}).get("market_value", 0.0)
            )
            pending_amount = self._pending.get(account, {}).get(
                code, PendingEntry()
            ).buy_amount
            est_new = existing_mv + pending_amount + volume * est_price
            ratio = est_new / snap.total_asset if snap.total_asset else float("inf")
            if ratio > eff.max_position_pct / 100.0:
                return RiskDecision(
                    allow=False, reject_code="POSITION_PCT",
                    reject_detail=(
                        f"est MV {est_new:.2f} / {snap.total_asset:.2f} = "
                        f"{ratio*100:.2f}% > {eff.max_position_pct}%"
                    ),
                )

        return RiskDecision(allow=True)

    def _safe_snapshot(self, account: str) -> AccountSnapshot:
        try:
            return self.get_snapshot(account)
        except Exception:
            return AccountSnapshot(
                total_asset=0.0, positions_by_code={},
                refreshed_at=time.monotonic(), stale=True,
            )

    def _rebuild_pending(self, account: str) -> None:
        """On first check per account, replay open buys from xttrader."""
        try:
            trader, acc = self._xttrader_ctx(account)
            from miniqmt_cli.server import xttrader_adapter
            orders = xttrader_adapter.query_stock_orders(trader, acc)
        except Exception as e:
            log.warning("pending rebuild for %s failed: %s", account, e)
            self._audit.append(
                phase="risk_pending_rebuild",
                account=account,
                rebuilt_orders_count=0,
                error=str(e),
            )
            return

        replayed = 0
        open_statuses = {"submitted", "confirmed", "partially_filled"}
        for o in orders or []:
            status = str(o.get("order_status_str") or o.get("status") or "").lower()
            if status and status not in open_statuses:
                continue
            side = o.get("side") or ("buy" if o.get("direction") == _xt_buy_direction() else None)
            if side != "buy":
                continue
            code = o.get("stock_code") or o.get("code")
            order_id = int(o.get("order_id") or o.get("seq") or 0)
            volume = int(o.get("order_volume") or o.get("volume") or 0) - int(
                o.get("traded_volume") or 0
            )
            price = float(o.get("price") or o.get("order_price") or 0.0)
            if volume <= 0 or not code or order_id == 0:
                continue
            self._add_pending_only(account, code, volume, price, order_id)
            replayed += 1
        self._audit.append(
            phase="risk_pending_rebuild",
            account=account,
            rebuilt_orders_count=replayed,
        )
