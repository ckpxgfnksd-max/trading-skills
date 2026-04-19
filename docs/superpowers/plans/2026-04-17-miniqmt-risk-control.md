# miniqmt-cli Risk Control (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add daemon-side risk control layer to miniqmt-cli: daily loss breaker, single-name concentration, order frequency, max positions, with persistent breaker state and pending-order tracking.

**Architecture:** `RiskManager` composed into `SessionManager`; `/trade/order` invokes `check_order` before submission and `record_accepted` after; state persisted atomically to `~/.miniqmt_cli/risk_state.json`; snapshot cache invalidated by `on_trade_event`; pending map rebuilt on startup from `query_stock_orders`.

**Tech Stack:** Python 3.11, FastAPI, pydantic, dataclasses, pytest, TOML (tomllib), Click. Spec: `docs/superpowers/specs/2026-04-17-miniqmt-risk-control-design.md`.

**Working dir:** run all commands from repo root `/Users/oopslink/works/codes/oopslink/trading-skills`. Use `pytest tests/miniqmt_cli -v` to validate.

---

## File Inventory

**New files:**
- `tools/miniqmt_cli/src/miniqmt_cli/server/risk.py` — RiskManager + data classes
- `tools/miniqmt_cli/src/miniqmt_cli/server/routes_risk.py` — `/risk/status`, `/risk/reset`
- `tools/miniqmt_cli/src/miniqmt_cli/commands/risk.py` — CLI `risk status`, `risk reset`
- `tests/miniqmt_cli/test_risk.py` — RiskManager unit tests
- `tests/miniqmt_cli/test_routes_risk.py` — HTTP tests

**Modified files:**
- `tools/miniqmt_cli/src/miniqmt_cli/server_config.py` — add `RiskConfig`, `AccountConfig.risk`, `ServerConfig.risk`, `effective_risk()`, `risk_state_path`
- `tools/miniqmt_cli/src/miniqmt_cli/server/session.py` — own RiskManager, forward trade events, startup retry
- `tools/miniqmt_cli/src/miniqmt_cli/server/routes_trade.py` — insert `check_order` + `record_accepted`
- `tools/miniqmt_cli/src/miniqmt_cli/server/app.py` — mount risk routes, extend `/health`
- `tools/miniqmt_cli/src/miniqmt_cli/main.py` — register `risk` command group
- `tools/miniqmt_cli/src/miniqmt_cli/client/errors.py` — add `RiskReject` exception
- `tests/fakes/xtquant_stub.py` — extend FakeTrader for parameterizable asset/positions, injectable event triggers, pre-populated `query_stock_orders`
- `tests/miniqmt_cli/conftest.py` — fixture adjustments (minimal)

---

## Task 1: Add RiskConfig and per-account override to server_config

**Files:**
- Modify: `tools/miniqmt_cli/src/miniqmt_cli/server_config.py`
- Test: `tests/miniqmt_cli/test_config.py` (extend)

- [ ] **Step 1: Write failing tests**

Add to `tests/miniqmt_cli/test_config.py`:

```python
def test_risk_config_defaults():
    from miniqmt_cli.server_config import RiskConfig
    c = RiskConfig()
    assert c.enabled is True
    assert c.max_daily_loss == 50000.0
    assert c.max_position_pct == 30.0
    assert c.max_orders_per_minute == 10
    assert c.max_positions == 10


def test_effective_risk_uses_global_when_no_override(tmp_path):
    from miniqmt_cli.server_config import load_server_config
    p = tmp_path / "server.toml"
    p.write_text(
        '[server]\nqmt_path = "/tmp"\n'
        '[risk]\nmax_daily_loss = 12345\n'
        '[accounts.sim]\naccount_id = "1230001"\n',
        encoding="utf-8",
    )
    cfg = load_server_config(str(p))
    eff = cfg.effective_risk("sim")
    assert eff.max_daily_loss == 12345
    assert eff.max_positions == 10  # default retained


def test_effective_risk_field_level_override(tmp_path):
    from miniqmt_cli.server_config import load_server_config
    p = tmp_path / "server.toml"
    p.write_text(
        '[server]\nqmt_path = "/tmp"\n'
        '[risk]\nmax_daily_loss = 50000\nmax_position_pct = 30\n'
        '[accounts.live]\naccount_id = "1230002"\n'
        '[accounts.live.risk]\nmax_daily_loss = 10000\n',
        encoding="utf-8",
    )
    cfg = load_server_config(str(p))
    eff = cfg.effective_risk("live")
    assert eff.max_daily_loss == 10000  # override
    assert eff.max_position_pct == 30.0  # inherited from global


def test_risk_state_path_default():
    from miniqmt_cli.server_config import ServerConfig
    cfg = ServerConfig()
    assert cfg.risk_state_path == "~/.miniqmt_cli/risk_state.json"


def test_risk_disabled_flag(tmp_path):
    from miniqmt_cli.server_config import load_server_config
    p = tmp_path / "server.toml"
    p.write_text(
        '[server]\nqmt_path = "/tmp"\n'
        '[risk]\nenabled = false\n'
        '[accounts.sim]\naccount_id = "1230001"\n',
        encoding="utf-8",
    )
    cfg = load_server_config(str(p))
    assert cfg.effective_risk("sim").enabled is False
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `pytest tests/miniqmt_cli/test_config.py -v -k "risk"`
Expected: ImportError/AttributeError on `RiskConfig` or `effective_risk`.

- [ ] **Step 3: Implement RiskConfig and modifications**

Edit `tools/miniqmt_cli/src/miniqmt_cli/server_config.py`. Add `RiskConfig` dataclass above `AccountConfig`:

```python
@dataclass
class RiskConfig:
    enabled: bool = True
    max_daily_loss: float = 50000.0
    max_position_pct: float = 30.0
    max_orders_per_minute: int = 10
    max_positions: int = 10
```

Extend `AccountConfig`:

```python
@dataclass
class AccountConfig:
    name: str
    account_id: str
    account_type: str = "STOCK"
    requires_confirm_live: bool = False
    risk: Optional["RiskConfig"] = None

    @property
    def last4(self) -> str:
        return self.account_id[-4:]

    def masked_id(self) -> str:
        if len(self.account_id) <= 4:
            return "*" * len(self.account_id)
        return "*" * (len(self.account_id) - 4) + self.account_id[-4:]
```

Extend `ServerConfig`:

```python
@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    qmt_path: str = ""
    userdata_mini_path: str = ""
    session_id: int = 0
    accounts: Dict[str, AccountConfig] = field(default_factory=dict)
    audit_log_path: str = "~/.miniqmt_cli/orders.jsonl"
    idempotency_ttl_seconds: int = 300
    audit_warn_size_bytes: int = 100 * 1024 * 1024
    risk: RiskConfig = field(default_factory=RiskConfig)
    risk_state_path: str = "~/.miniqmt_cli/risk_state.json"

    # ... existing methods ...

    def effective_risk(self, account_name: str) -> RiskConfig:
        """Merge per-account risk override (if any) on top of global defaults."""
        acc = self.accounts.get(account_name)
        if acc is None or acc.risk is None:
            return self.risk
        # Field-level merge: start from global, overlay only fields set on per-account
        merged = RiskConfig(
            enabled=acc.risk.enabled if acc.risk.enabled is not None else self.risk.enabled,
            max_daily_loss=acc.risk.max_daily_loss,
            max_position_pct=acc.risk.max_position_pct,
            max_orders_per_minute=acc.risk.max_orders_per_minute,
            max_positions=acc.risk.max_positions,
        )
        return merged

    def resolved_risk_state_path(self) -> Path:
        return Path(self.risk_state_path).expanduser()
```

**Key subtlety on merge semantics:** TOML does not distinguish "absent" from "default" once parsed into a `RiskConfig` dataclass. We fix this by building `acc.risk` as a dict first (only fields that were actually specified in TOML), then merging. Do NOT construct a full `RiskConfig` from user's override.

Refactor the TOML parse in `load_server_config`:

```python
def _parse_risk(raw: dict, base: RiskConfig) -> RiskConfig:
    """Return new RiskConfig with only provided fields overlaid on base."""
    return RiskConfig(
        enabled=bool(raw["enabled"]) if "enabled" in raw else base.enabled,
        max_daily_loss=float(raw["max_daily_loss"]) if "max_daily_loss" in raw else base.max_daily_loss,
        max_position_pct=float(raw["max_position_pct"]) if "max_position_pct" in raw else base.max_position_pct,
        max_orders_per_minute=int(raw["max_orders_per_minute"]) if "max_orders_per_minute" in raw else base.max_orders_per_minute,
        max_positions=int(raw["max_positions"]) if "max_positions" in raw else base.max_positions,
    )


def load_server_config(path_override: Optional[str] = None) -> ServerConfig:
    cfg = ServerConfig()
    path = _config_path(path_override)
    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
        server = data.get("server", {}) or {}
        cfg.host = server.get("host", cfg.host)
        cfg.port = int(server.get("port", cfg.port))
        cfg.qmt_path = server.get("qmt_path", cfg.qmt_path)
        cfg.userdata_mini_path = server.get("userdata_mini_path", cfg.userdata_mini_path)
        cfg.session_id = int(server.get("session_id", cfg.session_id))

        # Global risk
        risk_raw = data.get("risk", {}) or {}
        cfg.risk = _parse_risk(risk_raw, RiskConfig())

        accounts_raw = data.get("accounts", {}) or {}
        for name, raw in accounts_raw.items():
            if not isinstance(raw, dict):
                continue
            acc_risk_raw = raw.get("risk")
            acc_risk = _parse_risk(acc_risk_raw, cfg.risk) if acc_risk_raw else None
            cfg.accounts[name] = AccountConfig(
                name=name,
                account_id=str(raw.get("account_id", "")),
                account_type=str(raw.get("account_type", "STOCK")),
                requires_confirm_live=bool(raw.get("requires_confirm_live", False)),
                risk=acc_risk,
            )

        audit = data.get("audit", {}) or {}
        cfg.audit_log_path = audit.get("log_path", cfg.audit_log_path)
        # Risk state path override (under [risk] for co-location)
        if "state_path" in risk_raw:
            cfg.risk_state_path = str(risk_raw["state_path"])

    # env overrides (unchanged)
    env_host = os.environ.get("MINIQMT_CLI_SERVER_HOST")
    if env_host:
        cfg.host = env_host
    env_port = os.environ.get("MINIQMT_CLI_SERVER_PORT")
    if env_port:
        cfg.port = int(env_port)
    env_qmt = os.environ.get("MINIQMT_CLI_SERVER_QMT_PATH")
    if env_qmt:
        cfg.qmt_path = env_qmt
    return cfg
```

Simplify `effective_risk` since `_parse_risk` already did the merge:

```python
def effective_risk(self, account_name: str) -> RiskConfig:
    acc = self.accounts.get(account_name)
    if acc is None or acc.risk is None:
        return self.risk
    return acc.risk   # already merged with global at load time
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `pytest tests/miniqmt_cli/test_config.py -v -k "risk"`
Expected: 5 tests pass.

- [ ] **Step 5: Full existing suite still green**

Run: `pytest tests/miniqmt_cli -v`
Expected: all tests pass (56 existing + 5 new = 61).

- [ ] **Step 6: Commit**

```bash
git add tools/miniqmt_cli/src/miniqmt_cli/server_config.py tests/miniqmt_cli/test_config.py
git commit -m "feat(miniqmt-cli): add RiskConfig and per-account risk override"
```

---

## Task 2: RiskState persistence (JSON atomic write / load)

**Files:**
- Create: `tools/miniqmt_cli/src/miniqmt_cli/server/risk.py`
- Create: `tests/miniqmt_cli/test_risk.py`

- [ ] **Step 1: Write failing tests**

Create `tests/miniqmt_cli/test_risk.py`:

```python
"""Unit tests for RiskManager."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_risk_state_file_load_missing_returns_empty(tmp_path):
    from miniqmt_cli.server.risk import RiskStateFile
    p = tmp_path / "rs.json"
    state = RiskStateFile.load(p)
    assert state.path == p
    assert state.accounts == {}


def test_risk_state_file_atomic_write_round_trip(tmp_path):
    from miniqmt_cli.server.risk import (
        AccountRiskState, RiskStateFile,
    )
    p = tmp_path / "rs.json"
    state = RiskStateFile.load(p)
    state.accounts["sim"] = AccountRiskState(
        trade_date="20260417",
        baseline_total_asset=1000000.0,
        baseline_captured_at="2026-04-17T01:15:30Z",
        baseline_imprecise=False,
    )
    state.save()
    # Round trip
    state2 = RiskStateFile.load(p)
    assert state2.accounts["sim"].baseline_total_asset == 1000000.0
    assert state2.accounts["sim"].trade_date == "20260417"
    assert state2.accounts["sim"].breaker_tripped is False


def test_risk_state_atomic_write_leaves_no_partial(tmp_path):
    """Interrupted save should leave original file intact."""
    from miniqmt_cli.server.risk import (
        AccountRiskState, RiskStateFile,
    )
    p = tmp_path / "rs.json"
    state = RiskStateFile.load(p)
    state.accounts["sim"] = AccountRiskState(
        trade_date="20260417",
        baseline_total_asset=100.0,
        baseline_captured_at="2026-04-17T00:00:00Z",
    )
    state.save()
    original = p.read_text()
    # Corrupt: simulate partial write via a raw file at path.tmp; atomic save should replace
    (p.with_suffix(p.suffix + ".tmp")).write_text("garbage")
    state.accounts["sim"].baseline_total_asset = 200.0
    state.save()
    # .tmp should have been os.replace'd away
    assert not p.with_suffix(p.suffix + ".tmp").exists()
    assert json.loads(p.read_text())["accounts"]["sim"]["baseline_total_asset"] == 200.0


def test_risk_state_version_field(tmp_path):
    from miniqmt_cli.server.risk import RiskStateFile
    p = tmp_path / "rs.json"
    state = RiskStateFile.load(p)
    state.save()
    data = json.loads(p.read_text())
    assert data["version"] == 1
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `pytest tests/miniqmt_cli/test_risk.py -v`
Expected: ImportError on `miniqmt_cli.server.risk`.

- [ ] **Step 3: Create risk.py with state persistence**

Create `tools/miniqmt_cli/src/miniqmt_cli/server/risk.py`:

```python
"""Risk management: per-account breaker, baseline, pending tracking."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Set

log = logging.getLogger(__name__)

STATE_VERSION = 1


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _today_str() -> str:
    # Trading date uses local time (Asia/Shanghai for A-share); daemon host is expected Asia/Shanghai
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
    def load(cls, path: Path) -> "RiskStateFile":
        state = cls(path=Path(path).expanduser(), accounts={})
        if not state.path.exists():
            return state
        try:
            with open(state.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log.warning("risk state file unreadable at %s: %s; starting empty", path, e)
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
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `pytest tests/miniqmt_cli/test_risk.py -v`
Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add tools/miniqmt_cli/src/miniqmt_cli/server/risk.py tests/miniqmt_cli/test_risk.py
git commit -m "feat(miniqmt-cli): add RiskState persistence layer"
```

---

## Task 3: Extend fake xtquant for risk tests

**Files:**
- Modify: `tests/fakes/xtquant_stub.py`

- [ ] **Step 1: Inspect current FakeTrader**

Read `tests/fakes/xtquant_stub.py:73-128`. Note: `query_stock_asset` returns static dict; `query_stock_positions` returns static list; no trade event injection.

- [ ] **Step 2: Add parameterizable returns and event injection**

Edit `tests/fakes/xtquant_stub.py`, replace `FakeTrader` with:

```python
class FakeTrader:
    def __init__(self, userdata_path, session_id):
        self.userdata_path = userdata_path
        self.session_id = session_id
        self.started = False
        self.connected = False
        self.subscribed_accounts: List[FakeStockAccount] = []
        self.orders_placed: List[dict] = []
        self.cancels: List[int] = []
        self._next_seq = 100
        self.should_fail_order = False
        self.callback = None
        # Overridable returns for risk tests
        self.asset_override: Optional[dict] = None
        self.positions_override: Optional[list] = None
        self.open_orders_override: Optional[list] = None   # used by query_stock_orders replay
        self.should_fail_asset_query = False

    def register_callback(self, callback):
        self.callback = callback

    def start(self):
        self.started = True

    def connect(self):
        self.connected = True
        return 0

    def subscribe(self, acc):
        self.subscribed_accounts.append(acc)
        return 0

    def order_stock(self, acc, code, direction, volume, price_type, price):
        if self.should_fail_order:
            raise RuntimeError("fake order failure")
        seq = self._next_seq
        self._next_seq += 1
        self.orders_placed.append({
            "seq": seq, "code": code, "direction": direction,
            "volume": volume, "price": price, "price_type": price_type,
            "account": acc.account_id,
        })
        return seq

    def cancel_order_stock(self, acc, order_id):
        self.cancels.append(int(order_id))
        return 0

    def query_stock_asset(self, acc):
        if self.should_fail_asset_query:
            raise RuntimeError("fake asset query failure")
        if self.asset_override is not None:
            d = dict(self.asset_override)
            d.setdefault("account_id", acc.account_id)
            return d
        return {"cash": 100000.0, "total_asset": 200000.0, "account_id": acc.account_id}

    def query_stock_positions(self, acc):
        if self.positions_override is not None:
            return [dict(p) for p in self.positions_override]
        return [
            {"stock_code": "000001.SZ", "volume": 100, "avg_price": 12.0,
             "market_value": 1234.0, "account": acc.account_id}
        ]

    def query_stock_orders(self, acc):
        if self.open_orders_override is not None:
            return [dict(o) for o in self.open_orders_override]
        return list(self.orders_placed)

    def query_stock_trades(self, acc):
        return []

    # Test helpers to drive xtquant callbacks
    def fire_order_event(self, **order_fields):
        class _FakeOrder:
            pass
        o = _FakeOrder()
        for k, v in order_fields.items():
            setattr(o, k, v)
        if self.callback and hasattr(self.callback, "on_order_event"):
            self.callback.on_order_event(o)

    def fire_trade_event(self, **trade_fields):
        class _FakeTrade:
            pass
        t = _FakeTrade()
        for k, v in trade_fields.items():
            setattr(t, k, v)
        if self.callback and hasattr(self.callback, "on_trade_event"):
            self.callback.on_trade_event(t)
```

Note the test helpers `fire_order_event` and `fire_trade_event` simulate the xtquant callback.

- [ ] **Step 3: Run existing tests to ensure no regression**

Run: `pytest tests/miniqmt_cli -v`
Expected: all existing tests still pass. (Note: `test_positions_known_account` asserts `code == "000001.SZ"`. We changed `code` key to `stock_code`. If this test fails, update it too.)

Check:

```python
# Edit: tests/miniqmt_cli/test_routes_trade.py::test_positions_known_account
def test_positions_known_account(client):
    resp = client.get("/trade/positions", params={"account": "sim"})
    assert resp.status_code == 200
    assert resp.json()[0]["stock_code"] == "000001.SZ"
```

Re-run and confirm green.

- [ ] **Step 4: Commit**

```bash
git add tests/fakes/xtquant_stub.py tests/miniqmt_cli/test_routes_trade.py
git commit -m "test(miniqmt-cli): extend fake xtquant for risk testing"
```

---

## Task 4: RiskManager skeleton + baseline capture

**Files:**
- Modify: `tools/miniqmt_cli/src/miniqmt_cli/server/risk.py`
- Modify: `tests/miniqmt_cli/test_risk.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/miniqmt_cli/test_risk.py`:

```python
from miniqmt_cli.server.audit import AuditLog
from miniqmt_cli.server_config import AccountConfig, RiskConfig, ServerConfig


def _make_cfg(tmp_path, **overrides) -> ServerConfig:
    cfg = ServerConfig(
        host="127.0.0.1", port=8765, qmt_path=str(tmp_path / "qmt"),
        audit_log_path=str(tmp_path / "orders.jsonl"),
        risk_state_path=str(tmp_path / "risk_state.json"),
        risk=RiskConfig(**overrides),
    )
    cfg.accounts["sim"] = AccountConfig(
        name="sim", account_id="1230001", account_type="STOCK",
    )
    return cfg


class _FakeTraderCtx:
    """Minimal stand-in for session.get_trader-equivalent; returns a FakeTrader."""

    def __init__(self):
        from tests.fakes.xtquant_stub import FakeStockAccount, FakeTrader
        self.trader = FakeTrader("/tmp", 42)
        self.acc = FakeStockAccount(account_id="1230001", account_type="STOCK")

    def __call__(self, account_name: str):
        return (self.trader, self.acc)


def test_baseline_capture_on_first_check(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000000.0, "cash": 500000.0}
    rm = RiskManager(cfg, audit, ctx)
    rm.ensure_baseline("sim")
    state = rm._state.accounts["sim"]
    assert state.baseline_total_asset == 1000000.0
    assert state.trade_date  # YYYYMMDD
    # Persisted
    saved = (tmp_path / "risk_state.json").read_text()
    assert "1000000" in saved


def test_baseline_reused_same_day(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000000.0}
    rm = RiskManager(cfg, audit, ctx)
    rm.ensure_baseline("sim")
    # Change asset to verify baseline is NOT refreshed second time
    ctx.trader.asset_override = {"total_asset": 999999.0}
    rm.ensure_baseline("sim")
    assert rm._state.accounts["sim"].baseline_total_asset == 1000000.0


def test_baseline_reset_on_new_trade_date(tmp_path, monkeypatch):
    from miniqmt_cli.server import risk as risk_mod
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 100.0}
    # Day 1
    monkeypatch.setattr(risk_mod, "_today_str", lambda: "20260416")
    rm = RiskManager(cfg, audit, ctx)
    rm.ensure_baseline("sim")
    assert rm._state.accounts["sim"].trade_date == "20260416"
    # Day 2
    monkeypatch.setattr(risk_mod, "_today_str", lambda: "20260417")
    ctx.trader.asset_override = {"total_asset": 200.0}
    rm.ensure_baseline("sim")
    assert rm._state.accounts["sim"].trade_date == "20260417"
    assert rm._state.accounts["sim"].baseline_total_asset == 200.0


def test_baseline_imprecise_flag_when_after_open(tmp_path, monkeypatch):
    """If capture happens after 09:30 local, set baseline_imprecise=True."""
    from miniqmt_cli.server import risk as risk_mod
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 100.0}
    monkeypatch.setattr(risk_mod, "_capture_is_imprecise", lambda: True)
    rm = RiskManager(cfg, audit, ctx)
    rm.ensure_baseline("sim")
    assert rm._state.accounts["sim"].baseline_imprecise is True


def test_baseline_capture_failure_raises(tmp_path):
    from miniqmt_cli.server.risk import BaselineUnavailable, RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.should_fail_asset_query = True
    rm = RiskManager(cfg, audit, ctx)
    with pytest.raises(BaselineUnavailable):
        rm.ensure_baseline("sim")
    # No state persisted
    assert "sim" not in rm._state.accounts


def test_baseline_audit_row_written(tmp_path):
    """Each baseline capture writes a risk_baseline_capture audit row."""
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000000.0}
    rm = RiskManager(cfg, audit, ctx)
    rm.ensure_baseline("sim")
    rows = [json.loads(l) for l in (tmp_path / "orders.jsonl").read_text().splitlines()]
    captures = [r for r in rows if r.get("phase") == "risk_baseline_capture"]
    assert len(captures) == 1
    assert captures[0]["account"] == "sim"
    assert captures[0]["baseline_total_asset"] == 1000000.0
```

- [ ] **Step 2: Run tests — expect failure**

Run: `pytest tests/miniqmt_cli/test_risk.py -v -k "baseline"`
Expected: attribute errors.

- [ ] **Step 3: Implement baseline capture in RiskManager**

Append to `tools/miniqmt_cli/src/miniqmt_cli/server/risk.py`:

```python
class BaselineUnavailable(RuntimeError):
    """Raised when baseline cannot be captured (fail closed)."""


def _capture_is_imprecise() -> bool:
    """True if current local time is past A-share market open (09:30)."""
    now = datetime.now().time()
    return (now.hour, now.minute) > (9, 30)


class RiskManager:
    def __init__(self, cfg, audit, xttrader_ctx: Callable[[str], tuple]):
        self._cfg = cfg
        self._audit = audit
        self._xttrader_ctx = xttrader_ctx
        self._state = RiskStateFile.load(cfg.resolved_risk_state_path())
        self._lock = threading.Lock()

    def ensure_baseline(self, account_name: str) -> None:
        """Capture baseline if missing or trade_date mismatches today.

        On failure raises BaselineUnavailable (fail closed).
        """
        today = _today_str()
        existing = self._state.accounts.get(account_name)
        if existing is not None and existing.trade_date == today:
            return  # already captured today

        trader, acc = self._xttrader_ctx(account_name)
        try:
            from miniqmt_cli.server import xttrader_adapter
            asset = xttrader_adapter.query_stock_asset(trader, acc)
        except Exception as e:
            log.warning("baseline capture failed for %s: %s", account_name, e)
            raise BaselineUnavailable(str(e)) from e

        total = float(asset.get("total_asset", 0))
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
            self._state.accounts[account_name] = new_state
            self._state.save()
        self._audit.append(
            phase="risk_baseline_capture",
            account=account_name,
            trade_date=today,
            baseline_total_asset=total,
            imprecise=imprecise,
        )
```

- [ ] **Step 4: Run tests — expect pass**

Run: `pytest tests/miniqmt_cli/test_risk.py -v -k "baseline"`
Expected: 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add tools/miniqmt_cli/src/miniqmt_cli/server/risk.py tests/miniqmt_cli/test_risk.py
git commit -m "feat(miniqmt-cli): baseline capture with imprecise flag and audit"
```

---

## Task 5: Snapshot cache (refresh + stale + hard expiry)

**Files:**
- Modify: `tools/miniqmt_cli/src/miniqmt_cli/server/risk.py`
- Modify: `tests/miniqmt_cli/test_risk.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/miniqmt_cli/test_risk.py`:

```python
def test_snapshot_refresh_populates_cache(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000000.0}
    ctx.trader.positions_override = [
        {"stock_code": "000001.SZ", "volume": 500, "market_value": 5000.0}
    ]
    rm = RiskManager(cfg, audit, ctx)
    snap = rm.get_snapshot("sim")
    assert snap.total_asset == 1000000.0
    assert snap.positions_by_code["000001.SZ"]["volume"] == 500


def test_snapshot_reused_within_ttl(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000000.0}
    rm = RiskManager(cfg, audit, ctx)
    s1 = rm.get_snapshot("sim")
    ctx.trader.asset_override = {"total_asset": 999999.0}
    s2 = rm.get_snapshot("sim")  # within 30s
    assert s2.total_asset == 1000000.0  # cached value
    assert s1 is s2


def test_snapshot_invalidated_by_stale_flag(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000000.0}
    rm = RiskManager(cfg, audit, ctx)
    s1 = rm.get_snapshot("sim")
    s1.stale = True
    ctx.trader.asset_override = {"total_asset": 999999.0}
    s2 = rm.get_snapshot("sim")
    assert s2.total_asset == 999999.0
    assert s2 is not s1


def test_snapshot_ttl_expiry(tmp_path, monkeypatch):
    from miniqmt_cli.server import risk as risk_mod
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 100.0}
    t = [1000.0]
    monkeypatch.setattr(risk_mod.time, "monotonic", lambda: t[0])
    rm = RiskManager(cfg, audit, ctx)
    rm.get_snapshot("sim")
    t[0] = 1031.0  # >30s later
    ctx.trader.asset_override = {"total_asset": 200.0}
    s2 = rm.get_snapshot("sim")
    assert s2.total_asset == 200.0


def test_snapshot_hard_expiry_raises_when_refresh_fails(tmp_path, monkeypatch):
    from miniqmt_cli.server import risk as risk_mod
    from miniqmt_cli.server.risk import RiskManager, SnapshotStale
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 100.0}
    t = [1000.0]
    monkeypatch.setattr(risk_mod.time, "monotonic", lambda: t[0])
    rm = RiskManager(cfg, audit, ctx)
    rm.get_snapshot("sim")
    # Fail future refreshes
    ctx.trader.should_fail_asset_query = True
    t[0] = 1035.0  # >30s, triggers refresh
    # Within tolerance (<300s): stale data returned with warning
    snap = rm.get_snapshot("sim")
    assert snap.total_asset == 100.0
    t[0] = 1400.0  # >5 min since last successful refresh
    with pytest.raises(SnapshotStale):
        rm.get_snapshot("sim")
```

- [ ] **Step 2: Run — expect failure**

Run: `pytest tests/miniqmt_cli/test_risk.py -v -k "snapshot"`

- [ ] **Step 3: Implement snapshot cache**

In `tools/miniqmt_cli/src/miniqmt_cli/server/risk.py`, add before `class RiskManager`:

```python
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
```

Extend `RiskManager.__init__`:

```python
    def __init__(self, cfg, audit, xttrader_ctx: Callable[[str], tuple]):
        self._cfg = cfg
        self._audit = audit
        self._xttrader_ctx = xttrader_ctx
        self._state = RiskStateFile.load(cfg.resolved_risk_state_path())
        self._snapshots: Dict[str, AccountSnapshot] = {}
        self._lock = threading.Lock()
```

Add `get_snapshot`:

```python
    def get_snapshot(self, account_name: str) -> AccountSnapshot:
        """Return a fresh-enough snapshot, refreshing if stale or past TTL.

        Locking discipline: NEVER hold self._lock during the xtquant queries
        (they block the caller thread for 50-400ms). Only hold lock for the
        dictionary assignment at the very end.
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

        # Attempt refresh outside lock
        try:
            new_snap = self._do_refresh(account_name)
        except Exception as e:
            # If we have old snap and it's still within hard expiry, return it
            if snap is not None and (now - snap.refreshed_at) <= SNAPSHOT_HARD_EXPIRY:
                log.warning(
                    "snapshot refresh failed for %s (%s); using stale snapshot age=%.1fs",
                    account_name, e, now - snap.refreshed_at,
                )
                return snap
            raise SnapshotStale(f"snapshot refresh failed and no usable cache: {e}") from e

        with self._lock:
            self._snapshots[account_name] = new_snap
        return new_snap

    def _do_refresh(self, account_name: str) -> AccountSnapshot:
        trader, acc = self._xttrader_ctx(account_name)
        from miniqmt_cli.server import xttrader_adapter
        asset = xttrader_adapter.query_stock_asset(trader, acc)
        positions = xttrader_adapter.query_stock_positions(trader, acc)
        return AccountSnapshot(
            total_asset=float(asset.get("total_asset", 0)),
            positions_by_code={
                p.get("stock_code", ""): p for p in positions if p.get("stock_code")
            },
            refreshed_at=time.monotonic(),
            stale=False,
        )
```

- [ ] **Step 4: Run tests — expect pass**

Run: `pytest tests/miniqmt_cli/test_risk.py -v -k "snapshot"`
Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add tools/miniqmt_cli/src/miniqmt_cli/server/risk.py tests/miniqmt_cli/test_risk.py
git commit -m "feat(miniqmt-cli): AccountSnapshot cache with TTL and hard expiry"
```

---

## Task 6: Pending tracking and event handlers

**Files:**
- Modify: `tools/miniqmt_cli/src/miniqmt_cli/server/risk.py`
- Modify: `tests/miniqmt_cli/test_risk.py`

- [ ] **Step 1: Write failing tests**

Append:

```python
def test_record_accepted_buy_adds_to_pending(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    rm = RiskManager(cfg, audit, _FakeTraderCtx())
    rm.record_accepted("sim", "buy", "000001.SZ", 500, 10.0, order_id=200)
    e = rm._pending["sim"]["000001.SZ"]
    assert e.buy_volume == 500
    assert e.buy_amount == 5000.0
    assert e.by_order_id[200] == {"volume": 500, "amount": 5000.0}


def test_record_accepted_sell_does_not_add_to_pending(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    rm = RiskManager(cfg, audit, _FakeTraderCtx())
    rm.record_accepted("sim", "sell", "000001.SZ", 100, 10.0, order_id=201)
    assert "sim" not in rm._pending or not rm._pending["sim"].get("000001.SZ")


def test_record_accepted_adds_frequency_window_entry(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    rm = RiskManager(cfg, audit, _FakeTraderCtx())
    rm.record_accepted("sim", "buy", "000001.SZ", 500, 10.0, order_id=200)
    assert len(rm._order_window["sim"]) == 1


def test_order_status_filled_removes_pending(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    rm = RiskManager(cfg, audit, _FakeTraderCtx())
    rm.record_accepted("sim", "buy", "000001.SZ", 500, 10.0, order_id=200)
    rm.on_trade_event({
        "type": "order_status", "account": "sim", "order_id": 200,
        "status": "filled", "code": "000001.SZ", "side": "buy",
        "volume": 500, "filled_volume": 500, "avg_price": 10.0,
    })
    assert "000001.SZ" not in rm._pending.get("sim", {})


def test_order_status_partial_fill_reduces_pending(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    rm = RiskManager(cfg, audit, _FakeTraderCtx())
    rm.record_accepted("sim", "buy", "000001.SZ", 500, 10.0, order_id=200)
    rm.on_trade_event({
        "type": "order_status", "account": "sim", "order_id": 200,
        "status": "partially_filled", "volume": 500, "filled_volume": 300,
    })
    e = rm._pending["sim"]["000001.SZ"]
    # 200 shares remaining
    assert e.buy_volume == 200
    assert e.buy_amount == 2000.0


def test_order_status_cancelled_removes_pending(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    rm = RiskManager(cfg, audit, _FakeTraderCtx())
    rm.record_accepted("sim", "buy", "000001.SZ", 500, 10.0, order_id=200)
    rm.on_trade_event({
        "type": "order_status", "account": "sim", "order_id": 200,
        "status": "cancelled", "volume": 500, "filled_volume": 0,
    })
    assert "000001.SZ" not in rm._pending.get("sim", {})


def test_trade_event_marks_snapshot_stale(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 100.0}
    rm = RiskManager(cfg, audit, ctx)
    snap = rm.get_snapshot("sim")
    rm.on_trade_event({"type": "trade", "account": "sim", "order_id": 200})
    assert snap.stale is True


def test_unknown_account_event_ignored(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    rm = RiskManager(cfg, audit, _FakeTraderCtx())
    # Should not raise
    rm.on_trade_event({"type": "trade", "account": "ghost", "order_id": 1})
```

- [ ] **Step 2: Run — expect failure**

Run: `pytest tests/miniqmt_cli/test_risk.py -v -k "pending or order_status or trade_event or record_accepted"`

- [ ] **Step 3: Implement pending tracking and events**

Add to `risk.py`:

```python
@dataclass
class PendingEntry:
    buy_volume: int = 0
    buy_amount: float = 0.0
    by_order_id: Dict[int, Dict[str, float]] = field(default_factory=dict)
```

Extend `RiskManager.__init__`:

```python
        self._pending: Dict[str, Dict[str, PendingEntry]] = {}
        self._order_window: Dict[str, Deque[float]] = {}
        self._pending_rebuilt: Set[str] = set()
```

Add methods:

```python
    def record_accepted(
        self, account: str, side: str, code: str, volume: int,
        price: float, order_id: int,
    ) -> None:
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

    def on_trade_event(self, event: dict) -> None:
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
                int(event.get("volume", 0)) - int(event.get("filled_volume", 0)), 0
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
                    # price-per-share constant
                    if old_vol <= 0:
                        return
                    price_per = old_amt / old_vol
                    new_amt = remaining_volume * price_per
                    # update
                    entry.by_order_id[order_id] = {
                        "volume": remaining_volume, "amount": new_amt,
                    }
                    entry.buy_volume += (remaining_volume - old_vol)
                    entry.buy_amount += (new_amt - old_amt)
                    if entry.buy_volume <= 0:
                        del codes[code]
                    break
```

- [ ] **Step 4: Run — expect pass**

Run: `pytest tests/miniqmt_cli/test_risk.py -v -k "pending or order_status or trade_event or record_accepted"`
Expected: 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add tools/miniqmt_cli/src/miniqmt_cli/server/risk.py tests/miniqmt_cli/test_risk.py
git commit -m "feat(miniqmt-cli): pending-order tracking and xtquant event handlers"
```

---

## Task 7: check_order — full decision flow

**Files:**
- Modify: `tools/miniqmt_cli/src/miniqmt_cli/server/risk.py`
- Modify: `tests/miniqmt_cli/test_risk.py`

- [ ] **Step 1: Write failing tests**

Append:

```python
def test_disabled_config_allows_all(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path, enabled=False, max_daily_loss=1)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 100.0}
    rm = RiskManager(cfg, audit, ctx)
    d = rm.check_order("sim", "buy", "000001.SZ", 100, 10.0)
    assert d.allow is True


def test_check_order_daily_loss_trips_breaker(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path, max_daily_loss=100.0)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000.0}
    rm = RiskManager(cfg, audit, ctx)
    rm.ensure_baseline("sim")
    # Simulate big loss: drop asset to 800 (lost 200, over 100 threshold)
    ctx.trader.asset_override = {"total_asset": 800.0}
    # Invalidate snapshot to force re-read
    if "sim" in rm._snapshots:
        rm._snapshots["sim"].stale = True
    d = rm.check_order("sim", "buy", "000001.SZ", 100, 10.0)
    assert d.allow is False
    assert d.reject_code == "BREAKER_TRIPPED"
    assert rm._state.accounts["sim"].breaker_tripped is True


def test_check_order_breaker_blocks_buy(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000000.0}
    rm = RiskManager(cfg, audit, ctx)
    rm.ensure_baseline("sim")
    rm.trip_breaker("sim", reason="manual")
    d = rm.check_order("sim", "buy", "000001.SZ", 100, 10.0)
    assert d.allow is False
    assert d.reject_code == "BREAKER_TRIPPED"


def test_check_order_breaker_allows_sell_within_position(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000000.0}
    ctx.trader.positions_override = [
        {"stock_code": "000001.SZ", "volume": 500, "market_value": 5000.0}
    ]
    rm = RiskManager(cfg, audit, ctx)
    rm.ensure_baseline("sim")
    rm.trip_breaker("sim", reason="manual")
    d = rm.check_order("sim", "sell", "000001.SZ", 400, 10.0)
    assert d.allow is True


def test_check_order_breaker_rejects_oversell(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000000.0}
    ctx.trader.positions_override = [
        {"stock_code": "000001.SZ", "volume": 500, "market_value": 5000.0}
    ]
    rm = RiskManager(cfg, audit, ctx)
    rm.ensure_baseline("sim")
    rm.trip_breaker("sim", reason="manual")
    d = rm.check_order("sim", "sell", "000001.SZ", 600, 10.0)
    assert d.allow is False
    assert d.reject_code == "BREAKER_TRIPPED"


def test_check_order_frequency_limit(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path, max_orders_per_minute=3)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000000.0}
    rm = RiskManager(cfg, audit, ctx)
    rm.ensure_baseline("sim")
    for i in range(3):
        d = rm.check_order("sim", "buy", "000001.SZ", 100, 10.0)
        assert d.allow is True
        rm.record_accepted("sim", "buy", "000001.SZ", 100, 10.0, order_id=300 + i)
    d4 = rm.check_order("sim", "buy", "000001.SZ", 100, 10.0)
    assert d4.allow is False
    assert d4.reject_code == "FREQUENCY"


def test_check_order_frequency_window_slides(tmp_path, monkeypatch):
    from miniqmt_cli.server import risk as risk_mod
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path, max_orders_per_minute=2)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000000.0}
    t = [1000.0]
    monkeypatch.setattr(risk_mod.time, "monotonic", lambda: t[0])
    rm = RiskManager(cfg, audit, ctx)
    rm.ensure_baseline("sim")
    rm.record_accepted("sim", "buy", "000001.SZ", 100, 10.0, order_id=1)
    rm.record_accepted("sim", "buy", "000001.SZ", 100, 10.0, order_id=2)
    # Third should fail
    d = rm.check_order("sim", "buy", "000001.SZ", 100, 10.0)
    assert d.reject_code == "FREQUENCY"
    # Advance past 60s; window slides; re-check passes
    t[0] = 1061.0
    d = rm.check_order("sim", "buy", "000001.SZ", 100, 10.0)
    assert d.allow is True


def test_check_order_max_positions_new_code(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path, max_positions=2)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000000.0}
    ctx.trader.positions_override = [
        {"stock_code": "000001.SZ", "volume": 100, "market_value": 1000},
        {"stock_code": "600000.SH", "volume": 100, "market_value": 1000},
    ]
    rm = RiskManager(cfg, audit, ctx)
    d = rm.check_order("sim", "buy", "000002.SZ", 100, 10.0)
    assert d.allow is False
    assert d.reject_code == "MAX_POSITIONS"
    # Adding to existing code OK
    d = rm.check_order("sim", "buy", "000001.SZ", 100, 10.0)
    assert d.allow is True


def test_check_order_position_pct_limit(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path, max_position_pct=10.0)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000.0}
    ctx.trader.positions_override = []  # start flat
    rm = RiskManager(cfg, audit, ctx)
    # 10% of 1000 = 100. Buy 11 shares at 10 = 110 -> over limit
    d = rm.check_order("sim", "buy", "000001.SZ", 11, 10.0)
    assert d.allow is False
    assert d.reject_code == "POSITION_PCT"
    # 10 shares * 10 = 100 exactly at limit
    d = rm.check_order("sim", "buy", "000001.SZ", 10, 10.0)
    assert d.allow is True


def test_check_order_position_pct_includes_pending(tmp_path):
    """Pending buys contribute to concentration estimate."""
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path, max_position_pct=10.0)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000.0}
    ctx.trader.positions_override = []
    rm = RiskManager(cfg, audit, ctx)
    # First order: 5 shares @ 10 = 50 — allow
    d1 = rm.check_order("sim", "buy", "000001.SZ", 5, 10.0)
    assert d1.allow is True
    rm.record_accepted("sim", "buy", "000001.SZ", 5, 10.0, order_id=1)
    # Second order: another 6 shares @ 10 = 60 more — combined 110 > limit 100
    d2 = rm.check_order("sim", "buy", "000001.SZ", 6, 10.0)
    assert d2.allow is False
    assert d2.reject_code == "POSITION_PCT"


def test_check_order_market_order_uses_last_price(tmp_path, monkeypatch):
    """Market orders estimate via get_full_tick last_price."""
    from miniqmt_cli.server import risk as risk_mod
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path, max_position_pct=10.0)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000.0}
    ctx.trader.positions_override = []
    rm = RiskManager(cfg, audit, ctx)
    # Patch get_full_tick to return last_price=10.0 for 000001
    monkeypatch.setattr(
        risk_mod, "_get_last_price",
        lambda code: 10.0 if code == "000001.SZ" else None,
    )
    # price=0 (market), last_price=10 => 11 shares => 110 > 100 limit
    d = rm.check_order("sim", "buy", "000001.SZ", 11, 0.0, order_type="market")
    assert d.allow is False
    assert d.reject_code == "POSITION_PCT"


def test_check_order_market_order_no_price_rejects(tmp_path, monkeypatch):
    from miniqmt_cli.server import risk as risk_mod
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000.0}
    ctx.trader.positions_override = []
    rm = RiskManager(cfg, audit, ctx)
    monkeypatch.setattr(risk_mod, "_get_last_price", lambda code: None)
    d = rm.check_order("sim", "buy", "000001.SZ", 10, 0.0, order_type="market")
    assert d.allow is False
    assert d.reject_code == "PRICE_UNAVAILABLE"
```

- [ ] **Step 2: Run — expect failure**

Run: `pytest tests/miniqmt_cli/test_risk.py -v -k "check_order or disabled or frequency or max_positions or position_pct or market_order"`

- [ ] **Step 3: Implement trip_breaker, reset_breaker, and check_order**

Add to `risk.py`:

```python
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
```

Add to `RiskManager`:

```python
    def trip_breaker(self, account: str, reason: str) -> None:
        with self._lock:
            state = self._state.accounts.get(account)
            if state is None:
                # Can't trip breaker without baseline; skip persistence
                log.error("cannot trip breaker for %s: no baseline state", account)
                return
            state.breaker_tripped = True
            state.breaker_reason = reason
            state.breaker_tripped_at = _iso_now()
            self._state.save()
        snap = self._snapshots.get(account)
        self._audit.append(
            phase="risk_breaker_trip",
            account=account,
            reason=reason,
            baseline_total_asset=state.baseline_total_asset,
            current_total_asset=snap.total_asset if snap else None,
            daily_pnl=(snap.total_asset - state.baseline_total_asset) if snap else None,
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

        # Step: breaker check (may be re-entered after step 6)
        def _breaker_check() -> RiskDecision:
            state = self._state.accounts.get(account)
            if state and state.breaker_tripped:
                if side == "sell":
                    # close-only exemption
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

        # Step: baseline
        try:
            self.ensure_baseline(account)
        except BaselineUnavailable as e:
            return RiskDecision(
                allow=False, reject_code="BASELINE_PENDING",
                reject_detail=str(e),
            )

        # Step: snapshot
        try:
            snap = self.get_snapshot(account)
        except SnapshotStale as e:
            return RiskDecision(
                allow=False, reject_code="SNAPSHOT_STALE",
                reject_detail=str(e),
            )

        # Step: pending rebuild (first-check per account)
        if account not in self._pending_rebuilt:
            self._rebuild_pending(account)
            self._pending_rebuilt.add(account)

        # Step: daily loss
        state = self._state.accounts[account]
        daily_pnl = snap.total_asset - state.baseline_total_asset
        if daily_pnl < -eff.max_daily_loss:
            self.trip_breaker(
                account, f"daily_loss {daily_pnl:.2f} < -{eff.max_daily_loss}",
            )
            return _breaker_check()  # re-evaluate with close-only exemption

        # Step: frequency window
        window = self._order_window.setdefault(account, deque())
        now = time.monotonic()
        while window and now - window[0] > 60.0:
            window.popleft()
        if len(window) >= eff.max_orders_per_minute:
            return RiskDecision(
                allow=False, reject_code="FREQUENCY",
                reject_detail=f"{len(window)} orders in last 60s ≥ {eff.max_orders_per_minute}",
            )

        if side == "buy":
            # Step: max_positions
            held = {
                c for c, p in snap.positions_by_code.items()
                if int(p.get("volume", 0)) > 0
            }
            pending_codes = set(self._pending.get(account, {}).keys())
            tracked = held | pending_codes
            if code not in tracked and len(tracked) >= eff.max_positions:
                return RiskDecision(
                    allow=False, reject_code="MAX_POSITIONS",
                    reject_detail=f"holding {len(tracked)} ≥ {eff.max_positions} (new: {code})",
                )

            # Step: position_pct
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
            existing_mv = float(snap.positions_by_code.get(code, {}).get("market_value", 0.0))
            pending_amount = self._pending.get(account, {}).get(code, PendingEntry()).buy_amount
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
        """Snapshot access that never raises (empty fallback)."""
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
            side = o.get("side") or ("buy" if o.get("direction") == 23 else None)
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
            self.record_accepted(account, "buy", code, volume, price, order_id)
            replayed += 1
        self._audit.append(
            phase="risk_pending_rebuild",
            account=account,
            rebuilt_orders_count=replayed,
        )
```

- [ ] **Step 4: Run — expect pass**

Run: `pytest tests/miniqmt_cli/test_risk.py -v`
Expected: all risk-unit tests pass (~26 tests).

- [ ] **Step 5: Commit**

```bash
git add tools/miniqmt_cli/src/miniqmt_cli/server/risk.py tests/miniqmt_cli/test_risk.py
git commit -m "feat(miniqmt-cli): check_order decision flow with breaker and limits"
```

---

## Task 8: SessionManager integration

**Files:**
- Modify: `tools/miniqmt_cli/src/miniqmt_cli/server/session.py`
- Modify: `tests/miniqmt_cli/test_routes_trade.py`

- [ ] **Step 1: Write failing test for dispatch forwarding**

Create `tests/miniqmt_cli/test_session_risk.py`:

```python
"""Session + Risk integration tests."""
from __future__ import annotations

import pytest


def test_session_has_risk_manager(app):
    assert hasattr(app.state.session, "risk")


def test_dispatch_order_event_forwards_to_risk(app, fake_xtquant):
    sess = app.state.session
    # Trigger trade event; risk should mark snapshot stale.
    # First populate pending via record_accepted
    sess.risk.record_accepted("sim", "buy", "000001.SZ", 100, 10.0, order_id=500)
    sess.dispatch_order_event({
        "type": "order_status", "account": "sim", "order_id": 500,
        "status": "filled", "volume": 100, "filled_volume": 100,
    })
    # pending should be gone
    assert "000001.SZ" not in sess.risk._pending.get("sim", {})
```

- [ ] **Step 2: Run — expect failure**

Run: `pytest tests/miniqmt_cli/test_session_risk.py -v`
Expected: AttributeError `sess.risk`.

- [ ] **Step 3: Wire RiskManager into SessionManager**

Edit `tools/miniqmt_cli/src/miniqmt_cli/server/session.py`.

Change the import block at top:

```python
from miniqmt_cli.server import xtdata_adapter, xttrader_adapter
from miniqmt_cli.server.audit import AuditLog
from miniqmt_cli.server.risk import RiskManager
from miniqmt_cli.server_config import AccountConfig, ServerConfig
```

Update `__init__`:

```python
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
        self._order_subscribers: list[asyncio.Queue] = []
        self._sub_lock = asyncio.Lock()
        self.risk = RiskManager(cfg, self.audit, self._xttrader_ctx_for_risk)

    def _xttrader_ctx_for_risk(self, account_name: str) -> tuple:
        """Synchronous adapter for RiskManager's xttrader_ctx.

        RiskManager runs from a variety of contexts (HTTP handler, xtquant
        callback thread); it needs a sync getter, so we poll the login state
        rather than awaiting. If trader not logged in yet, raises RuntimeError
        (RiskManager will propagate as BaselineUnavailable or similar).
        """
        if self.dry_run:
            raise RuntimeError("trader unavailable in dry_run")
        handle = self._traders.get(account_name)
        if handle is None:
            raise RuntimeError(f"trader for {account_name} not logged in")
        return (handle.trader, handle.acc)
```

Update `dispatch_order_event` to forward to risk:

```python
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
```

**Note:** RiskManager's `check_order` requires a logged-in trader to capture baseline. In the test fixture, `get_trader` is lazily invoked — there's no trader yet at `SessionManager.__init__`. So baseline capture is lazy (first `check_order` call triggers `get_trader` and retry). For tests, we'll force trader creation via a preliminary endpoint call OR accept BASELINE_PENDING on first order.

For tests in this task, we only need event forwarding, which doesn't need trader presence. But `risk.record_accepted` uses internal state only (no xttrader call), so it's fine to call directly in tests.

- [ ] **Step 4: Run tests**

Run: `pytest tests/miniqmt_cli/test_session_risk.py -v`
Expected: 2 pass.

Also: `pytest tests/miniqmt_cli -v`
Expected: all pass (some earlier tests may need the same `_xttrader_ctx_for_risk` path to work; adjust if needed).

- [ ] **Step 5: Commit**

```bash
git add tools/miniqmt_cli/src/miniqmt_cli/server/session.py tests/miniqmt_cli/test_session_risk.py
git commit -m "feat(miniqmt-cli): SessionManager owns RiskManager and forwards trade events"
```

---

## Task 9: Integrate check_order into /trade/order

**Files:**
- Modify: `tools/miniqmt_cli/src/miniqmt_cli/server/routes_trade.py`
- Modify: `tests/miniqmt_cli/test_routes_trade.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/miniqmt_cli/test_routes_trade.py`:

```python
def test_order_rejected_by_risk_check(client, server_cfg, fake_xtquant):
    """Risk check rejection should return 400 and audit a risk_check row."""
    # Shrink max_orders_per_minute so the 2nd order hits FREQUENCY
    session = client.app.state.session
    from miniqmt_cli.server_config import RiskConfig
    session.cfg.risk = RiskConfig(max_orders_per_minute=1)
    # Pump: prime trader login and baseline via first order
    r1 = client.post("/trade/order", json=_body(client_req_id="req-rc1"))
    assert r1.status_code == 200
    # Second order exceeds 1/min
    r2 = client.post("/trade/order", json=_body(client_req_id="req-rc2"))
    assert r2.status_code == 400
    detail = r2.json()["detail"]
    assert detail["error"] == "risk_reject"
    assert detail["code"] == "FREQUENCY"
    # audit contains a risk_check row with allow=False
    import json
    rows = [
        json.loads(l)
        for l in server_cfg.resolved_audit_log_path().read_text().splitlines()
    ]
    risk_rows = [r for r in rows if r.get("phase") == "risk_check"]
    assert any(r.get("allow") is False and r.get("reject_code") == "FREQUENCY" for r in risk_rows)


def test_order_risk_check_success_recorded(client, server_cfg, fake_xtquant):
    """Allowed risk check should also write an audit row (allow=True)."""
    r = client.post("/trade/order", json=_body(client_req_id="req-rc-ok"))
    assert r.status_code == 200
    import json
    rows = [
        json.loads(l)
        for l in server_cfg.resolved_audit_log_path().read_text().splitlines()
    ]
    assert any(
        r.get("phase") == "risk_check" and r.get("client_req_id") == "req-rc-ok"
        and r.get("allow") is True
        for r in rows
    )
```

- [ ] **Step 2: Run — expect failure**

Run: `pytest tests/miniqmt_cli/test_routes_trade.py::test_order_rejected_by_risk_check -v`

- [ ] **Step 3: Insert risk.check_order into routes_trade**

Edit `tools/miniqmt_cli/src/miniqmt_cli/server/routes_trade.py`.

In `place_order`, insert the risk check after audit_pre and before `xttrader_adapter.order_stock`:

```python
@router.post("/order")
async def place_order(request: Request, body: OrderRequest):
    sess = _session(request)
    acc = _require_account(sess, body.account)

    # Live gate (existing)
    if acc.requires_confirm_live:
        if not body.confirm_live_last4:
            raise HTTPException(status_code=400, detail=(
                "live account requires confirm_live_last4 matching last 4 digits of account_id"
            ))
        if body.confirm_live_last4 != acc.last4:
            raise HTTPException(status_code=400,
                                detail="confirm_live_last4 does not match account_id last 4")

    # Idempotency (existing)
    cached = await sess.idempotency_lookup(body.client_req_id)
    if cached is not None:
        return {**cached, "idempotent_hit": True}

    # Audit: pre (existing)
    sess.audit.append(
        phase="pre",
        client_req_id=body.client_req_id,
        account=body.account,
        account_id=acc.account_id,
        code=body.code,
        side=body.side,
        volume=body.volume,
        price=body.price,
        type=body.type,
        confirm_live_last4=body.confirm_live_last4,
    )

    # Ensure trader is logged in BEFORE risk check (so risk can query asset/positions)
    try:
        handle = await sess.get_trader(body.account)
    except Exception as e:
        sess.audit.append(
            phase="post", client_req_id=body.client_req_id,
            status="error", error=f"login failed: {e}",
        )
        raise HTTPException(status_code=500, detail=f"trader login failed: {e}")

    # Risk check (NEW)
    decision = sess.risk.check_order(
        body.account, body.side, body.code, body.volume, body.price,
        order_type=body.type,
    )
    sess.audit.append(
        phase="risk_check",
        client_req_id=body.client_req_id,
        account=body.account,
        side=body.side,
        code=body.code,
        volume=body.volume,
        price=body.price,
        type=body.type,
        allow=decision.allow,
        reject_code=decision.reject_code,
        reject_detail=decision.reject_detail,
    )
    if not decision.allow:
        raise HTTPException(status_code=400, detail={
            "error": "risk_reject",
            "code": decision.reject_code,
            "message": decision.reject_detail,
        })

    # Submit (existing)
    try:
        result = xttrader_adapter.order_stock(
            handle.trader, handle.acc, body.code, body.side,
            body.volume, body.price, order_type=body.type,
        )
    except Exception as e:
        sess.audit.append(
            phase="post", client_req_id=body.client_req_id,
            status="error", error=str(e),
        )
        raise HTTPException(status_code=500, detail=f"order_stock failed: {e}")

    seq = result.get("seq", 0)
    status = "ok" if seq > 0 else "rejected"
    response = {
        "client_req_id": body.client_req_id,
        "seq": seq,
        "status": status,
        "order_id": seq if seq > 0 else None,
    }
    # Update risk pending / frequency window (NEW)
    if seq > 0:
        sess.risk.record_accepted(
            body.account, body.side, body.code, body.volume, body.price, int(seq),
        )
    sess.audit.append(
        phase="post",
        client_req_id=body.client_req_id,
        status=status,
        seq=seq,
        order_id=response["order_id"],
    )
    await sess.idempotency_store(body.client_req_id, response)
    return response
```

- [ ] **Step 4: Run — expect pass**

Run: `pytest tests/miniqmt_cli/test_routes_trade.py -v`
Expected: all pass (new ones + existing).

- [ ] **Step 5: Commit**

```bash
git add tools/miniqmt_cli/src/miniqmt_cli/server/routes_trade.py tests/miniqmt_cli/test_routes_trade.py
git commit -m "feat(miniqmt-cli): integrate risk check_order into /trade/order"
```

---

## Task 10: /risk/status and /risk/reset endpoints

**Files:**
- Create: `tools/miniqmt_cli/src/miniqmt_cli/server/routes_risk.py`
- Modify: `tools/miniqmt_cli/src/miniqmt_cli/server/app.py`
- Create: `tests/miniqmt_cli/test_routes_risk.py`

- [ ] **Step 1: Write failing tests**

Create `tests/miniqmt_cli/test_routes_risk.py`:

```python
"""HTTP tests for /risk endpoints."""
from __future__ import annotations

import json
import pytest


def test_risk_status_all_accounts_before_baseline(client, server_cfg, fake_xtquant):
    """Before any order, status lists accounts with state=uninitialized."""
    resp = client.get("/risk/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "accounts" in body
    assert "sim" in body["accounts"]
    entry = body["accounts"]["sim"]
    assert entry.get("trade_date") is None
    assert entry.get("breaker_tripped") is False


def test_risk_status_one_account(client, server_cfg, fake_xtquant):
    # Trigger baseline capture by placing an order
    from tests.miniqmt_cli.test_routes_trade import _body
    client.post("/trade/order", json=_body(client_req_id="req-rs1"))
    resp = client.get("/risk/status", params={"account": "sim"})
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("account") == "sim"
    assert body.get("baseline_total_asset") is not None


def test_risk_status_audits(client, server_cfg, fake_xtquant):
    client.get("/risk/status")
    rows = [
        json.loads(l)
        for l in server_cfg.resolved_audit_log_path().read_text().splitlines()
    ]
    assert any(r.get("phase") == "risk_status_query" for r in rows)


def test_risk_reset_breaker_not_tripped(client, fake_xtquant):
    resp = client.post("/risk/reset", json={
        "account": "sim", "operator_note": "test",
    })
    assert resp.status_code == 400
    assert "not tripped" in resp.json()["detail"]


def test_risk_reset_missing_operator_note(client, fake_xtquant):
    resp = client.post("/risk/reset", json={"account": "sim"})
    assert resp.status_code == 422  # pydantic validation


def test_risk_reset_live_requires_confirm_last4(client, fake_xtquant):
    # First trip breaker for live
    sess = client.app.state.session
    from tests.miniqmt_cli.test_routes_trade import _body
    # Live requires confirm_live_last4=1234
    client.post("/trade/order", json=_body(
        account="live", confirm_live_last4="1234", client_req_id="req-prime-live",
    ))
    sess.risk.trip_breaker("live", reason="test")
    # Without confirm
    resp = client.post("/risk/reset", json={
        "account": "live", "operator_note": "ok",
    })
    assert resp.status_code == 400
    assert "confirm_live_last4" in resp.json()["detail"]
    # With wrong confirm
    resp = client.post("/risk/reset", json={
        "account": "live", "operator_note": "ok", "confirm_live_last4": "9999",
    })
    assert resp.status_code == 400
    # With correct confirm
    resp = client.post("/risk/reset", json={
        "account": "live", "operator_note": "ok", "confirm_live_last4": "1234",
    })
    assert resp.status_code == 200
    assert resp.json()["account"] == "live"


def test_risk_reset_audits(client, fake_xtquant):
    sess = client.app.state.session
    from tests.miniqmt_cli.test_routes_trade import _body
    client.post("/trade/order", json=_body(client_req_id="req-prime-sim"))
    sess.risk.trip_breaker("sim", reason="test_reset")
    client.post("/risk/reset", json={
        "account": "sim", "operator_note": "manual",
    })
    rows = [
        json.loads(l)
        for l in client.app.state.session.audit.path.read_text().splitlines()
    ]
    assert any(r.get("phase") == "risk_breaker_reset" for r in rows)
```

- [ ] **Step 2: Run — expect failure**

Run: `pytest tests/miniqmt_cli/test_routes_risk.py -v`
Expected: 404 on /risk/status (route not mounted).

- [ ] **Step 3: Create routes_risk.py**

```python
"""Risk status and reset endpoints."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

router = APIRouter(prefix="/risk", tags=["risk"])


def _session(request: Request):
    return request.app.state.session


class ResetRequest(BaseModel):
    account: str
    operator_note: str = Field(..., min_length=1, max_length=200)
    confirm_live_last4: Optional[str] = None


def _format_account_status(sess, account: str) -> dict:
    state = sess.risk._state.accounts.get(account)
    eff = sess.cfg.effective_risk(account)
    pending = sess.risk._pending.get(account, {})
    window = sess.risk._order_window.get(account, [])
    # Prune window to last 60s (non-mutating read; we need a snapshot-of-now)
    import time as _t
    now = _t.monotonic()
    in_window = sum(1 for ts in window if now - ts <= 60.0)
    # Try to query current asset snapshot (may fail if no trader)
    current_asset = None
    try:
        snap = sess.risk.get_snapshot(account)
        current_asset = snap.total_asset
    except Exception:
        pass
    base = state.baseline_total_asset if state else None
    pnl = (current_asset - base) if (current_asset is not None and base is not None) else None
    return {
        "trade_date": state.trade_date if state else None,
        "baseline_total_asset": base,
        "baseline_captured_at": state.baseline_captured_at if state else None,
        "baseline_imprecise": state.baseline_imprecise if state else None,
        "current_total_asset": current_asset,
        "daily_pnl": pnl,
        "breaker_tripped": bool(state and state.breaker_tripped),
        "breaker_reason": state.breaker_reason if state else None,
        "breaker_tripped_at": state.breaker_tripped_at if state else None,
        "effective_config": {
            "enabled": eff.enabled,
            "max_daily_loss": eff.max_daily_loss,
            "max_position_pct": eff.max_position_pct,
            "max_orders_per_minute": eff.max_orders_per_minute,
            "max_positions": eff.max_positions,
        },
        "orders_in_window": in_window,
        "pending_orders": {
            code: {"buy_volume": e.buy_volume, "buy_amount": e.buy_amount}
            for code, e in pending.items()
        },
        "reset_count_today": len(state.reset_history) if state else 0,
        "reset_history": state.reset_history if state else [],
    }


@router.get("/status")
def status(request: Request, account: Optional[str] = Query(None)):
    sess = _session(request)
    sess.audit.append(
        phase="risk_status_query",
        account=account or "*",
    )
    if account:
        if account not in sess.cfg.accounts:
            raise HTTPException(status_code=404, detail=f"unknown account: {account}")
        data = _format_account_status(sess, account)
        data["account"] = account
        return data
    return {
        "accounts": {
            name: _format_account_status(sess, name)
            for name in sess.cfg.accounts
        },
    }


@router.post("/reset")
def reset(request: Request, body: ResetRequest):
    sess = _session(request)
    if body.account not in sess.cfg.accounts:
        raise HTTPException(status_code=404, detail=f"unknown account: {body.account}")
    acc = sess.cfg.accounts[body.account]
    if acc.requires_confirm_live:
        if not body.confirm_live_last4:
            raise HTTPException(
                status_code=400,
                detail="confirm_live_last4 required for live account",
            )
        if body.confirm_live_last4 != acc.last4:
            raise HTTPException(
                status_code=400,
                detail="confirm_live_last4 does not match",
            )
    try:
        result = sess.risk.reset_breaker(body.account, body.operator_note)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result
```

- [ ] **Step 4: Mount in app.py**

Edit `tools/miniqmt_cli/src/miniqmt_cli/server/app.py`:

```python
from miniqmt_cli.server.routes_data import router as data_router
from miniqmt_cli.server.routes_risk import router as risk_router   # NEW
from miniqmt_cli.server.routes_stream import router as stream_router
from miniqmt_cli.server.routes_trade import router as trade_router
# ...

def create_app(cfg: ServerConfig, dry_run: bool = False) -> FastAPI:
    app = FastAPI(title="miniqmt-cli daemon", version="0.2.0")
    app.state.session = SessionManager(cfg, dry_run=dry_run)
    app.include_router(data_router)
    app.include_router(trade_router)
    app.include_router(stream_router)
    app.include_router(risk_router)   # NEW
    # ... /version /health unchanged for now ...
```

- [ ] **Step 5: Run tests — expect pass**

Run: `pytest tests/miniqmt_cli/test_routes_risk.py -v`
Expected: 7 pass.

- [ ] **Step 6: Commit**

```bash
git add tools/miniqmt_cli/src/miniqmt_cli/server/routes_risk.py \
        tools/miniqmt_cli/src/miniqmt_cli/server/app.py \
        tests/miniqmt_cli/test_routes_risk.py
git commit -m "feat(miniqmt-cli): /risk/status and /risk/reset endpoints"
```

---

## Task 11: /health reflects risk state

**Files:**
- Modify: `tools/miniqmt_cli/src/miniqmt_cli/server/app.py`
- Modify: `tests/miniqmt_cli/test_routes_risk.py`

- [ ] **Step 1: Write failing test**

Append to `tests/miniqmt_cli/test_routes_risk.py`:

```python
def test_health_reflects_breaker_tripped(client, fake_xtquant):
    sess = client.app.state.session
    from tests.miniqmt_cli.test_routes_trade import _body
    client.post("/trade/order", json=_body(client_req_id="req-h"))
    sess.risk.trip_breaker("sim", reason="testing")
    resp = client.get("/health")
    body = resp.json()
    assert body["state"] == "risk_breaker_tripped"
    assert "sim" in body["tripped_accounts"]
```

- [ ] **Step 2: Run — expect failure**

Run: `pytest tests/miniqmt_cli/test_routes_risk.py::test_health_reflects_breaker_tripped -v`

- [ ] **Step 3: Extend /health**

Edit `tools/miniqmt_cli/src/miniqmt_cli/server/app.py`:

```python
    @app.get("/health")
    async def health():
        sess = app.state.session
        if dry_run:
            return {"state": "ready", "dry_run": True}
        # Check breaker first (highest priority)
        tripped = [
            n for n, s in sess.risk._state.accounts.items()
            if s.breaker_tripped
        ]
        if tripped:
            return {"state": "risk_breaker_tripped", "tripped_accounts": tripped}
        try:
            await sess.ensure_xtquant()
        except Exception as e:
            return {"state": "daemon_up_xtquant_missing", "error": str(e)}
        if sess.trader_logged_in_count() == 0:
            return {"state": "daemon_up_no_trader"}
        return {"state": "ready"}
```

- [ ] **Step 4: Run — expect pass**

Run: `pytest tests/miniqmt_cli/test_routes_risk.py::test_health_reflects_breaker_tripped -v`
Expected: 1 pass.

- [ ] **Step 5: Commit**

```bash
git add tools/miniqmt_cli/src/miniqmt_cli/server/app.py tests/miniqmt_cli/test_routes_risk.py
git commit -m "feat(miniqmt-cli): /health reports risk_breaker_tripped priority"
```

---

## Task 12: CLI risk commands + RiskReject error class

**Files:**
- Modify: `tools/miniqmt_cli/src/miniqmt_cli/client/errors.py`
- Create: `tools/miniqmt_cli/src/miniqmt_cli/commands/risk.py`
- Modify: `tools/miniqmt_cli/src/miniqmt_cli/main.py`
- Modify: `tests/miniqmt_cli/test_cli_commands.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/miniqmt_cli/test_cli_commands.py`:

```python
def test_cli_risk_status(cli_runner, daemon_url, fake_xtquant):
    """Invokes `miniqmt-cli risk status` against the live daemon."""
    from miniqmt_cli.main import cli
    result = cli_runner.invoke(cli, [
        "--format", "json",
        "risk", "status",
    ], catch_exceptions=False)
    assert result.exit_code == 0
    # JSON-parseable output expected
    import json as _json
    body = _json.loads(result.output)
    assert "accounts" in body


def test_cli_risk_status_per_account(cli_runner, daemon_url, fake_xtquant):
    from miniqmt_cli.main import cli
    result = cli_runner.invoke(cli, [
        "--format", "json", "risk", "status", "--account", "sim",
    ])
    assert result.exit_code == 0
    import json as _json
    body = _json.loads(result.output)
    assert body.get("account") == "sim"


def test_cli_risk_reset_requires_note(cli_runner, daemon_url, fake_xtquant):
    """Missing --note -> Click usage error (exit 2)."""
    from miniqmt_cli.main import cli
    result = cli_runner.invoke(cli, ["risk", "reset", "--account", "sim", "--yes"])
    assert result.exit_code == 2
    assert "note" in (result.output + str(result.exception)).lower()


def test_cli_risk_reset_breaker_not_tripped(cli_runner, daemon_url, fake_xtquant):
    """Daemon 400 -> CLI exits non-zero with RiskReject message."""
    from miniqmt_cli.main import cli
    result = cli_runner.invoke(cli, [
        "risk", "reset", "--account", "sim", "--note", "x", "--yes",
    ])
    assert result.exit_code != 0
    assert "not tripped" in (result.output + (result.exception and str(result.exception) or "")).lower()
```

Note: These tests require a running daemon. Check `tests/miniqmt_cli/test_cli_commands.py` for the pattern (existing tests likely use pytest-httpserver or similar). If no existing pattern, see Step 3a first.

- [ ] **Step 3a: Inspect existing CLI test fixtures**

Run: `grep -n "daemon_url\|cli_runner" tests/miniqmt_cli/test_cli_commands.py tests/miniqmt_cli/conftest.py`

Adapt fixtures. If the existing test file uses a different pattern (e.g. mock `make_transport`), follow that pattern. For this task, write the tests to mirror whatever is already there.

- [ ] **Step 2: Run — expect failure**

Run: `pytest tests/miniqmt_cli/test_cli_commands.py -v -k "risk"`
Expected: ImportError or command-not-found.

- [ ] **Step 3: Add RiskReject exception**

Edit `tools/miniqmt_cli/src/miniqmt_cli/client/errors.py`:

```python
"""CLI-side error helpers."""
from __future__ import annotations

import click

EXIT_GENERIC = 1
EXIT_BROKER = 2
EXIT_GUARD = 3
EXIT_RISK = 4


class GuardExit(click.ClickException):
    exit_code = EXIT_GUARD

    def __init__(self, message: str):
        super().__init__(message)


class BrokerReject(click.ClickException):
    exit_code = EXIT_BROKER

    def __init__(self, message: str):
        super().__init__(message)


class RiskReject(click.ClickException):
    """Exits with code 4: daemon-side risk layer refused the action."""

    exit_code = EXIT_RISK

    def __init__(self, code: str, message: str):
        super().__init__(f"risk_reject [{code}] {message}")
        self.code = code
```

- [ ] **Step 4: Create commands/risk.py**

```python
"""CLI: miniqmt-cli risk status / reset."""
from __future__ import annotations

import json

import click

from miniqmt_cli.client.errors import GuardExit, RiskReject
from miniqmt_cli.client.transport import make_transport
from miniqmt_cli.output import format_output


@click.group()
def risk():
    """Risk control: view status, reset circuit breaker."""


@risk.command("status")
@click.option("--account", default=None, help="Specific account; otherwise list all.")
@click.pass_context
def status_cmd(ctx, account):
    t = make_transport(ctx)
    params = {"account": account} if account else None
    body = t.get("/risk/status", params=params)
    fmt = ctx.obj.get("fmt", "table")
    if fmt == "json":
        click.echo(json.dumps(body, ensure_ascii=False, indent=2))
        return
    # Table-like rendering
    if account:
        _render_account_status(account, body)
    else:
        for name, data in (body.get("accounts") or {}).items():
            _render_account_status(name, data)
            click.echo("")


def _render_account_status(name: str, data: dict) -> None:
    click.echo(f"Account: {name}")
    click.echo(f"  Trade date:      {data.get('trade_date') or '(not captured)'}")
    base = data.get("baseline_total_asset")
    cap = data.get("baseline_captured_at")
    imprecise = data.get("baseline_imprecise")
    if base is not None:
        flag = " (imprecise)" if imprecise else ""
        click.echo(f"  Baseline asset:  {base:,.2f}  (captured {cap}{flag})")
    curr = data.get("current_total_asset")
    if curr is not None:
        click.echo(f"  Current asset:   {curr:,.2f}")
    pnl = data.get("daily_pnl")
    if pnl is not None:
        click.echo(f"  Daily PnL:       {pnl:+,.2f}")
    tripped = data.get("breaker_tripped")
    if tripped:
        click.echo(
            f"  Breaker:         TRIPPED at {data.get('breaker_tripped_at')} "
            f"-- \"{data.get('breaker_reason')}\""
        )
    else:
        click.echo("  Breaker:         OK")
    eff = data.get("effective_config") or {}
    click.echo(
        f"  Config:          max_loss={eff.get('max_daily_loss')} "
        f"max_pos_pct={eff.get('max_position_pct')}% "
        f"max_freq={eff.get('max_orders_per_minute')}/min "
        f"max_positions={eff.get('max_positions')}"
    )
    click.echo(f"  Orders in 60s:   {data.get('orders_in_window')}")
    pending = data.get("pending_orders") or {}
    if pending:
        for code, e in pending.items():
            click.echo(f"  Pending:         {code}: +{e['buy_volume']} ({e['buy_amount']:,.2f})")
    click.echo(f"  Resets today:    {data.get('reset_count_today', 0)}")


@risk.command("reset")
@click.option("--account", required=True)
@click.option("--note", required=True, help="Operator justification (required for audit).")
@click.option("--confirm-live", default=None, help="Last 4 digits of live account_id")
@click.option("--yes", is_flag=True, default=False)
@click.pass_context
def reset_cmd(ctx, account, note, confirm_live, yes):
    t = make_transport(ctx)
    # Fetch current status to show tripped reason
    status_body = t.get("/risk/status", params={"account": account})
    if not status_body.get("breaker_tripped"):
        raise RiskReject("NOT_TRIPPED", f"breaker for {account} is not tripped")
    reason = status_body.get("breaker_reason")
    tripped_at = status_body.get("breaker_tripped_at")
    click.echo(f"Account:       {account}")
    click.echo(f"Tripped at:    {tripped_at}")
    click.echo(f"Reason:        {reason}")
    click.echo(f"Note:          {note}")
    click.echo("-" * 31)
    if not yes:
        confirmation = click.prompt('Type "yes" to confirm', default="", show_default=False)
        if confirmation.strip().lower() != "yes":
            raise GuardExit("reset declined by user")
    body = {"account": account, "operator_note": note}
    if confirm_live:
        body["confirm_live_last4"] = confirm_live
    try:
        resp = t.post("/risk/reset", body=body)
    except Exception as e:
        # transport wraps HTTP 400 as an exception with detail
        msg = str(e)
        if "risk_reject" in msg or "confirm_live" in msg or "not tripped" in msg:
            raise RiskReject("RESET_FAILED", msg)
        raise
    click.echo(f"Reset OK. Previous reason: {resp.get('previous_reason')}")
    click.echo("Baseline unchanged. Next order crossing threshold will re-trip.")
```

- [ ] **Step 5: Register in main.py**

Edit `tools/miniqmt_cli/src/miniqmt_cli/main.py`:

```python
from miniqmt_cli.commands.risk import risk
# ...
cli.add_command(risk)
```

- [ ] **Step 6: Run tests — expect pass**

Run: `pytest tests/miniqmt_cli/test_cli_commands.py -v -k "risk"`
Expected: pass.

Full suite: `pytest tests/miniqmt_cli -v`

- [ ] **Step 7: Commit**

```bash
git add tools/miniqmt_cli/src/miniqmt_cli/client/errors.py \
        tools/miniqmt_cli/src/miniqmt_cli/commands/risk.py \
        tools/miniqmt_cli/src/miniqmt_cli/main.py \
        tests/miniqmt_cli/test_cli_commands.py
git commit -m "feat(miniqmt-cli): CLI risk status and reset commands"
```

---

## Task 13: Update server.toml template + setup wizard docs

**Files:**
- Modify: `tools/miniqmt_cli/src/miniqmt_cli/server_config.py` (TEMPLATE constant)
- Modify: `tests/miniqmt_cli/test_setup_wizard.py` if it asserts on template

- [ ] **Step 1: Update TEMPLATE**

Edit `TEMPLATE` in `server_config.py`:

```python
TEMPLATE = """\
[server]
host = "127.0.0.1"
port = 8765
qmt_path = "C:/国金QMT交易端/userdata_mini"
# session_id = 123456  # omit to use os.getpid()

[accounts.sim]
account_id = "1230001"
account_type = "STOCK"

# [accounts.live]
# account_id = "1230002"
# account_type = "STOCK"
# requires_confirm_live = true

[audit]
log_path = "~/.miniqmt_cli/orders.jsonl"

[risk]
# Global risk defaults. Per-account override via [accounts.<name>.risk]
enabled = true
max_daily_loss = 50000        # yuan
max_position_pct = 30         # % of total asset per single name
max_orders_per_minute = 10
max_positions = 10
# state_path = "~/.miniqmt_cli/risk_state.json"   # breaker + baseline store

# [accounts.live.risk]
# max_daily_loss = 10000
# max_position_pct = 20
"""
```

- [ ] **Step 2: Verify setup wizard tests still pass**

Run: `pytest tests/miniqmt_cli/test_setup_wizard.py -v`
Expected: pass (template test may need adjustment if it matches exact content).

- [ ] **Step 3: Commit**

```bash
git add tools/miniqmt_cli/src/miniqmt_cli/server_config.py
git commit -m "chore(miniqmt-cli): server.toml template includes [risk] section"
```

---

## Task 14: Smoke test script + memory + roadmap update

**Files:**
- Create: `docs/superpowers/plans/2026-04-17-miniqmt-risk-control-smoke.md` (manual test checklist)
- Modify: `docs/roadmap-auto-trading.md`
- Modify: `/Users/oopslink/.claude/projects/-Users-oopslink-works-codes-oopslink-trading-skills/memory/project_miniqmt_cli.md`

- [ ] **Step 1: Write manual smoke test doc**

Create `docs/superpowers/plans/2026-04-17-miniqmt-risk-control-smoke.md`:

```markdown
# Risk Control Smoke Test

Run against a running daemon with fake_xtquant or in dry-run. Validates end-to-end flow.

## Setup

```
miniqmt-cli serve &
miniqmt-cli setup   # if first run
```

Confirm `~/.miniqmt_cli/server.toml` has `[risk]` section with conservative defaults.

## 1. Baseline capture

```
miniqmt-cli risk status --account sim
```

Expected: `Trade date: (not captured)`.

Place a small order:
```
miniqmt-cli order buy --account sim --code 000001.SZ --volume 100 --price 12 --yes
```

Re-check status — baseline now populated, daily_pnl ~ 0.

## 2. Trip the breaker

Edit server.toml: `max_daily_loss = 100`. Restart daemon.
Place order, then manipulate fake_xtquant to return lower total_asset.
Place another order — expect `FREQUENCY`, `BREAKER_TRIPPED`, or `DAILY_LOSS → BREAKER_TRIPPED`.

Verify `miniqmt-cli health` → `risk_breaker_tripped`.

## 3. Sell close-only

With breaker tripped, attempt a buy → reject.
Attempt sell within position → allow.

## 4. Reset

```
miniqmt-cli risk reset --account sim --note "smoke test" --yes
```

Verify daily_pnl still reflects negative value; next buy immediately re-trips (A semantics).

## 5. Audit trail

```
tail ~/.miniqmt_cli/orders.jsonl
```

Confirm phases present: `risk_baseline_capture`, `risk_check` (allow and reject), `risk_breaker_trip`, `risk_breaker_reset`, `risk_status_query`, `risk_pending_rebuild` (if any open orders on startup).
```

- [ ] **Step 2: Update roadmap**

Edit `docs/roadmap-auto-trading.md`. Change the Phase 2 status row in the "里程碑时间线" table:

```
| M3: 安全底线 | Phase 2 | 风控层 + 熔断 | **已完成** |
```

And mark the Phase 2 heading in the body to note completion date.

- [ ] **Step 3: Update project memory**

Edit `/Users/oopslink/.claude/projects/-Users-oopslink-works-codes-oopslink-trading-skills/memory/project_miniqmt_cli.md`. Append a short section summarizing:

```
## Phase 2 risk control architecture (added 2026-04-17)

- `server/risk.py`: RiskManager with breaker, baseline (JSON-persisted), snapshot cache, pending tracking
- Decision flow: enabled → breaker → baseline → snapshot → pending_rebuild → daily_loss → frequency → max_positions → position_pct
- State file: `~/.miniqmt_cli/risk_state.json` (atomic write)
- Audit phases: risk_check / risk_breaker_trip / risk_breaker_reset / risk_baseline_capture / risk_status_query / risk_pending_rebuild
- Close-only exemption: if breaker_tripped and side=sell within snapshot position, allow
- Reset semantics: only clears breaker flag, baseline unchanged (intentional — prevents accidental "zero-out")
```

(If `project_miniqmt_cli.md` does not exist, create it with this content.)

- [ ] **Step 4: Run full suite**

Run: `pytest tests/miniqmt_cli -v`
Expected: all green; estimated 110+ tests total.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/plans/2026-04-17-miniqmt-risk-control-smoke.md \
        docs/roadmap-auto-trading.md
git commit -m "docs(miniqmt-cli): Phase 2 complete -- smoke test + roadmap update"
```

Memory file is outside the repo; no git add needed.

---

## Completion Checklist

- [ ] All 14 tasks committed
- [ ] `pytest tests/miniqmt_cli -v` passes
- [ ] `docs/roadmap-auto-trading.md` Phase 2 marked 已完成
- [ ] `~/.miniqmt_cli/server.toml` template includes `[risk]`
- [ ] Memory note updated
- [ ] Smoke test checklist exists

---

## Self-Review Notes

**Spec coverage check:**
- §4 RiskConfig / per-account override → Task 1 ✓
- §4 RiskStateFile → Task 2 ✓
- §4 AccountSnapshot → Task 5 ✓
- §4 PendingEntry → Task 6 ✓
- §5 check_order flow (steps 1-10) → Task 7 ✓
- §5.2 record_accepted → Task 6 (impl) + Task 9 (wiring) ✓
- §6 event integration + locking → Task 6 + Task 8 ✓
- §6.3 startup retry → NOT explicit task; addressed implicitly (baseline is lazy on first check_order, so retry is essentially natural). The plan's baseline capture runs on every check_order when missing. If daemon starts before xtquant ready, first check returns BASELINE_PENDING, next one retries. Explicit background retry task deferred as optimization.
- §7 HTTP /risk/status + /risk/reset → Task 10 ✓
- §7.4 /health → Task 11 ✓
- §8 CLI commands → Task 12 ✓
- §9 audit all phases → present in tasks 4, 6, 7, 9, 10 ✓
- §10 server.toml template → Task 13 ✓
- §13 delivery criteria → Task 14 ✓

**Known deferrals (explicit, not silent gaps):**
- §6.3 background startup retry: current design relies on lazy retry at first check. An `asyncio.create_task` retry loop can be added in a follow-up if smoke test reveals a problem during daemon startup before first trade arrives.

**Placeholder scan:** no TBD/TODO/implement-later strings; every code step contains full code.

**Type consistency:** `RiskDecision.reject_code` uses string literals (`"BREAKER_TRIPPED"`, `"FREQUENCY"`, etc.) throughout; `AccountSnapshot.positions_by_code` uses stock_code key consistently; `PendingEntry.by_order_id` keys are `int`. Checked.
