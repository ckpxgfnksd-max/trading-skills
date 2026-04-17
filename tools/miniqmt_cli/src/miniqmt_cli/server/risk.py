"""Risk management: per-account breaker, baseline, pending tracking."""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

STATE_VERSION = 1


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _today_str() -> str:
    """Trading date in local time (daemon host expected to be Asia/Shanghai)."""
    return datetime.now().strftime("%Y%m%d")


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
        except (OSError, json.JSONDecodeError) as e:
            log.warning(
                "risk state file unreadable at %s: %s; starting empty", path, e
            )
            return state
        for name, raw in (data.get("accounts") or {}).items():
            state.accounts[name] = AccountRiskState(**raw)
        return state

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "accounts": {n: asdict(s) for n, s in self.accounts.items()},
        }
        with self._lock:
            tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except (OSError, AttributeError):
                    pass
            os.replace(tmp_path, self.path)
