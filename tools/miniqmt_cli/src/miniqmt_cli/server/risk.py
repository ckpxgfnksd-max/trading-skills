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
from typing import Dict, List, Optional
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
