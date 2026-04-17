"""Risk management: per-account breaker, baseline, pending tracking."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
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
