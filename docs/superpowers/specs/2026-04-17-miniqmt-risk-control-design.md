# miniqmt-cli Risk Control (Phase 2) Design

**Status:** Draft (awaiting review)
**Date:** 2026-04-17
**Target version:** v0.2.0
**Roadmap:** docs/roadmap-auto-trading.md — Phase 2 / Milestone M3

## 1. Goal

Insert a daemon-side risk control layer into miniqmt-cli so that strategy bugs, fat-finger orders, or runaway loops cannot cause unbounded losses. The layer is authoritative: it runs on the Windows daemon (not the Mac CLI), so even if Mac-side guards are bypassed — or if the CLI is replaced by a raw HTTP client — the daemon still enforces the same limits.

Four hard limits are enforced at order-submission time:

1. **Daily loss** — reject new opening trades when today's P&L falls below a threshold.
2. **Single-name concentration** — reject buys that would push one ticker above X% of total asset.
3. **Order frequency** — reject more than N orders per 60-second sliding window.
4. **Position count** — reject opening a position in a new ticker if the account already holds ≥ N names.

Plus a breaker: once tripped, new buys are blocked account-wide until explicitly reset. Sells of existing positions remain allowed so the operator can reduce exposure.

## 2. Non-goals

- Portfolio-level risk (cross-account VaR, sector exposure)
- Intraday margin / leverage checks (not relevant for 现金账户)
- Dynamic thresholds (time-of-day or volatility-linked) — thresholds are static from config
- Alerting / notification of breaker trips (Phase 4 will own webhook pushes)
- Automatic breaker reset (manual only, by design)
- Conditional orders / stop-loss automation (Phase 3)

## 3. Architecture

### 3.1 Placement

```
  Mac CLI / client ──HTTP──> Windows daemon
                                  │
                                  ▼
         routes_trade.POST /trade/order
         ┌──────────────────────────────────────────┐
         │ whitelist → live_gate → idempotency →    │
         │ audit_pre →                              │
         │   sess.risk.check_order(...)  ←── Phase 2│
         │ → xttrader.order_stock → audit_post →    │
         │   sess.risk.record_accepted(...)         │
         └──────────────────────────────────────────┘
                                  │
                                  │  owns
                                  ▼
                        RiskManager (server/risk.py)
                        - RiskConfig (TOML, per-account override)
                        - RiskState (JSON on disk)
                        - AccountSnapshot cache (in-memory, ~30s TTL)
                        - Pending-order map (rebuilt on startup)
                        - Sliding-window frequency counter
                        - Breaker state (persistent across restart)
                        Event inputs (from SessionManager.dispatch_order_event):
                        - on_order_event → update pending
                        - on_trade_event → mark snapshot stale
```

RiskManager is a composition member of `SessionManager`. It does not own a trader handle directly; instead it receives a callable `xttrader_ctx(account_name) -> (trader, acc)` from SessionManager, preserving single-direction dependency (session → risk, never reverse).

### 3.2 Order flow with risk check

```
client POST /trade/order
  │
  ▼
whitelist (existing)
  │
  ▼
live_gate (existing)
  │
  ▼
idempotency lookup (existing; idempotent hits skip risk check)
  │
  ▼
audit phase="pre" (existing)
  │
  ▼
risk.check_order(account, side, code, volume, price)   ◄── NEW
  │
  ├── allow=False → audit phase="risk_check" (result logged) →
  │                  HTTPException 400 {"error":"risk_reject","code":<REJECT_CODE>}
  │
  └── allow=True → audit phase="risk_check" (result logged) →
                    xttrader.order_stock(...)
                      │
                      ├── exception → audit phase="post" status="error" → 500
                      │
                      └── ok → risk.record_accepted(...) →   ◄── NEW
                               audit phase="post" status="ok" → response
```

## 4. Data Structures

### 4.1 RiskConfig (server_config.py)

```python
@dataclass
class RiskConfig:
    enabled: bool = True
    max_daily_loss: float = 50000.0        # yuan
    max_position_pct: float = 30.0         # single-name MV / total_asset, percent
    max_orders_per_minute: int = 10
    max_positions: int = 10
```

`AccountConfig` gains `risk: Optional[RiskConfig] = None`.

`ServerConfig` gains:
- `risk: RiskConfig = field(default_factory=RiskConfig)` — global default
- `risk_state_path: str = "~/.miniqmt_cli/risk_state.json"`
- `effective_risk(account_name: str) -> RiskConfig` — field-level merge of global + per-account override

### 4.2 RiskState (server/risk.py)

```python
@dataclass
class AccountRiskState:
    trade_date: str                           # "YYYYMMDD"
    baseline_total_asset: float
    baseline_captured_at: str                 # ISO 8601 UTC
    baseline_imprecise: bool = False          # True if daemon started after 09:30
    breaker_tripped: bool = False
    breaker_reason: Optional[str] = None
    breaker_tripped_at: Optional[str] = None
    reset_history: list[dict] = field(default_factory=list)
```

Persisted as `~/.miniqmt_cli/risk_state.json`:

```json
{
  "version": 1,
  "accounts": {
    "sim": {
      "trade_date": "20260417",
      "baseline_total_asset": 1000000.0,
      "baseline_captured_at": "2026-04-17T01:15:30Z",
      "baseline_imprecise": false,
      "breaker_tripped": false,
      "breaker_reason": null,
      "breaker_tripped_at": null,
      "reset_history": []
    }
  }
}
```

Write strategy: atomic (tmp file + `os.replace`). Single file covers all accounts, guarded by `threading.Lock`.

### 4.3 In-memory state (RiskManager)

```python
class RiskManager:
    def __init__(self, cfg: ServerConfig, audit: AuditLog, xttrader_ctx):
        self._cfg = cfg
        self._audit = audit
        self._xttrader_ctx = xttrader_ctx
        self._state: RiskStateFile                      # loaded from disk
        self._snapshots: Dict[str, AccountSnapshot] = {}
        self._pending: Dict[str, Dict[str, PendingEntry]] = {}  # account → code → entry
        self._order_window: Dict[str, deque[float]] = {}  # account → deque of monotonic timestamps
        self._lock = threading.Lock()
        self._persist_lock = threading.Lock()
        self._pending_rebuilt: Set[str] = set()          # accounts whose pending has been reconciled from xttrader
```

```python
@dataclass
class AccountSnapshot:
    total_asset: float
    positions_by_code: Dict[str, Dict[str, Any]]    # stock_code → raw position dict
    refreshed_at: float                               # time.monotonic()
    stale: bool = False

@dataclass
class PendingEntry:
    buy_volume: int
    buy_amount: float
    by_order_id: Dict[int, Dict[str, float]]         # order_id → {"volume", "amount"}

@dataclass
class RiskDecision:
    allow: bool
    reject_code: Optional[str] = None
    reject_detail: Optional[str] = None
```

## 5. Decision Flow

`RiskManager.check_order(account, side, code, volume, price) -> RiskDecision` executes short-circuit:

1. **enabled gate** — if `effective_risk(account).enabled is False`, return allow immediately.
2. **breaker check** — if `state.breaker_tripped`:
   - If `side == "sell"` and `snapshot.positions_by_code[code].volume >= volume`: allow (close-only exception).
   - Otherwise: reject `BREAKER_TRIPPED`.
3. **baseline capture** — if missing or `trade_date != today`:
   - Call `query_stock_asset`. On success: write baseline, set `baseline_imprecise = (now > 09:30 local)`, persist state.
   - On failure: reject `BASELINE_PENDING`. Startup retry loop (see §6.3) ensures this is transient.
4. **snapshot freshness** — if `snapshot is None or stale or (now - refreshed_at > 30s)`: refresh.
   - Refresh failure: tolerate up to 5 min using stale data (log warning); beyond 5 min reject `SNAPSHOT_STALE`.
5. **pending rebuild** — if `account not in _pending_rebuilt`: call `query_stock_orders`, rebuild `_pending` from open buy orders with status ∈ {submitted, confirmed, partially_filled}, mark rebuilt. (First check per account only.)
6. **daily loss** — `daily_pnl = snapshot.total_asset - state.baseline_total_asset`. If `daily_pnl < -max_daily_loss`: call `trip_breaker`, then re-run step 2 (sell close-only exemption may still allow).
7. **frequency** — prune window to `now - 60s`. If `len(window) >= max_orders_per_minute`: reject `FREQUENCY`. (Do NOT push here; push happens in `record_accepted` after the check passes AND the order is accepted by xttrader.)
8. **max_positions** — `buy` only. Count = `{c for c, p in snapshot.positions_by_code.items() if p.volume > 0} ∪ {c for c in pending[account]}`. If `code not in count` and `len(count) >= max_positions`: reject `MAX_POSITIONS`.
9. **position_pct** — `buy` only.
   - `existing_mv = snapshot.positions_by_code.get(code, {}).get("market_value", 0.0)`
   - `pending_amount = pending[account].get(code, PendingEntry()).buy_amount`
   - Determine `est_price`:
     - If `order_type == "limit"`: `est_price = price` (limit caps max fill price; self-consistent)
     - If `order_type == "market"`: call `xtdata_adapter.get_full_tick([code])` to fetch `last_price`; `est_price = max(price, last_price)`. If both `price` and `last_price` are 0/unavailable: reject `PRICE_UNAVAILABLE`.
   - `est_new = existing_mv + pending_amount + volume * est_price`
   - If `est_new / snapshot.total_asset > max_position_pct / 100`: reject `POSITION_PCT`.
10. Allow.

### 5.1 Reject codes

| Code | Meaning | HTTP |
|------|---------|------|
| `BREAKER_TRIPPED` | Breaker active; buys or oversell blocked | 400 |
| `BASELINE_PENDING` | Daemon could not capture baseline yet | 400 |
| `SNAPSHOT_STALE` | xtquant unreachable for >5 min | 400 |
| `FREQUENCY` | Orders/minute exceeded | 400 |
| `MAX_POSITIONS` | Opening a new name beyond limit | 400 |
| `POSITION_PCT` | Would exceed single-name concentration limit | 400 |
| `PRICE_UNAVAILABLE` | Market order without last_price fallback | 400 |

### 5.2 record_accepted

Called from routes_trade AFTER `order_stock` returns `seq > 0`:

```python
def record_accepted(self, account, side, code, volume, price, order_id):
    with self._lock:
        self._order_window.setdefault(account, deque()).append(time.monotonic())
        if side == "buy":
            entries = self._pending.setdefault(account, {})
            entry = entries.setdefault(code, PendingEntry(0, 0.0, {}))
            entry.buy_volume += volume
            entry.buy_amount += volume * price
            entry.by_order_id[order_id] = {"volume": volume, "amount": volume * price}
```

## 6. Event Integration and Lifecycle

### 6.1 Trade event handling

`SessionManager.dispatch_order_event` (called from xtquant callback thread) forwards events to `RiskManager.on_trade_event`:

```python
def on_trade_event(self, event):
    account = event.get("account")
    if account not in self._cfg.accounts:
        return
    evt_type = event.get("type")
    if evt_type == "order_status":
        self._handle_order_status(event)
    elif evt_type == "trade":
        # mark snapshot stale; next check_order will re-query
        snap = self._snapshots.get(account)
        if snap:
            snap.stale = True

def _handle_order_status(self, event):
    status = event["status"]
    order_id = event["order_id"]
    account = event["account"]
    if status in {"filled", "cancelled", "rejected", "expired"}:
        self._remove_pending_by_order_id(account, order_id)
    elif status == "partially_filled":
        remaining = max(event["volume"] - event["filled_volume"], 0)
        self._update_pending_remaining(account, order_id, remaining)
```

Removal logic iterates `pending[account]` to find the entry containing `order_id` and decrements, removing the code-level entry when `buy_volume <= 0`.

### 6.2 Locking discipline

- `_refresh_snapshot` must NOT hold `self._lock` while calling `xttrader_adapter.query_*` (those calls take 50–400 ms and block the xtquant callback thread if the lock is held across them).
- Lock is held only for dictionary assignment (`self._snapshots[account] = snap`).
- `on_trade_event` handlers use `self._lock` for their internal dict mutations; they are O(pending_orders) and complete in microseconds.

### 6.3 Startup

On `SessionManager.__init__`:
1. `RiskManager.__init__` loads `risk_state.json` (creating empty if absent).
2. SessionManager schedules `asyncio.create_task(risk.startup_baseline_retry())` — attempts baseline capture for each configured account with exponential backoff: base 5 s, doubling on failure, cap at 60 s, until success (or daemon shutdown). Per-account failures tracked independently so one account's xtquant hiccup does not block others.
3. Pending rebuild is lazy: on first `check_order` for an account, `query_stock_orders` is called and open buys are replayed into `_pending`.

On daemon shutdown: no special flush needed; state is persisted eagerly on every mutation.

## 7. HTTP Surface

### 7.1 Routes

```
GET  /risk/status[?account=NAME]
POST /risk/reset
```

### 7.2 GET /risk/status response shape

```json
{
  "accounts": {
    "sim": {
      "trade_date": "20260417",
      "baseline_total_asset": 1000000.0,
      "baseline_captured_at": "2026-04-17T01:15:30Z",
      "baseline_imprecise": false,
      "current_total_asset": 998500.0,
      "daily_pnl": -1500.0,
      "breaker_tripped": false,
      "breaker_reason": null,
      "breaker_tripped_at": null,
      "effective_config": {
        "enabled": true, "max_daily_loss": 50000.0, "max_position_pct": 30.0,
        "max_orders_per_minute": 10, "max_positions": 10
      },
      "orders_in_window": 3,
      "pending_orders": {"000001.SZ": {"buy_volume": 500, "buy_amount": 6000.0}},
      "reset_count_today": 0,
      "reset_history": []
    }
  }
}
```

Single-account query returns just that account's object under `account`.

### 7.3 POST /risk/reset

```json
// Request
{"account": "sim", "operator_note": "verified positions safe", "confirm_live_last4": "1234"}

// Response 200
{"account": "sim", "previous_reason": "daily_loss -51200 < -50000", "reset_at": "2026-04-17T02:45:00Z"}

// Errors
// 400 "breaker is not tripped"
// 400 "operator_note required"
// 400 "confirm_live_last4 required for live account"
// 400 "confirm_live_last4 does not match"
// 404 "unknown account"
```

### 7.4 /health extension

Priority order: breaker_tripped > xtquant_missing > baseline_pending > no_trader > ready.

```json
{"state": "ready"}
{"state": "risk_breaker_tripped", "tripped_accounts": ["live"]}
{"state": "daemon_up_baseline_pending", "accounts_pending": ["sim"]}
```

## 8. CLI Surface

### 8.1 Commands

```
miniqmt-cli risk status [--account NAME] [--output json|table]
miniqmt-cli risk reset  --account NAME --note "reason" [--confirm-live XXXX] [--yes]
```

### 8.2 Safety gates (mirrored on daemon)

- `risk reset` requires `--confirm-live <last4>` for accounts with `requires_confirm_live = true`.
- `risk reset` without `--yes` prompts interactively (same pattern as `order buy/sell`).
- `--note` is mandatory; passes through to `operator_note`.
- Exit codes: `0` success, `3` RiskReject (new, distinct from `BrokerReject=2`), `1` other error.

### 8.3 Table output example

```
Account: live
  Trade date:      20260417
  Baseline asset:  500,000.00  (captured 2026-04-17T01:15Z, imprecise: true)
  Current asset:   489,500.00
  Daily PnL:       -10,500.00
  Breaker:         TRIPPED at 02:30Z -- "daily_loss -10500 < -10000"
  Config:          max_loss=10000 max_pos_pct=20% max_freq=10/min max_positions=10
  Orders in 60s:   0
  Pending:         000001.SZ: +500 (6,000.00)
  Resets today:    0
```

## 9. Audit

Per user requirement, every risk operation is audited. AuditLog `phase` values added:

| phase | When | Fields |
|-------|------|--------|
| `risk_check` | Every check_order (allow or reject) | client_req_id, account, side, code, volume, price, allow, reject_code, reject_detail |
| `risk_breaker_trip` | Breaker trips | account, reason, baseline_total_asset, current_total_asset, daily_pnl |
| `risk_breaker_reset` | Manual reset succeeds | account, previous_reason, operator_note |
| `risk_baseline_capture` | Baseline captured (first of day or imprecise) | account, trade_date, baseline_total_asset, imprecise |
| `risk_status_query` | GET /risk/status | account (or "*"), caller_ip (if available) |
| `risk_pending_rebuild` | Startup pending rebuild | account, rebuilt_orders_count |

All write to `~/.miniqmt_cli/orders.jsonl` (shared file with Phase 1 order audit).

## 10. Configuration

`~/.miniqmt_cli/server.toml` additions:

```toml
[risk]
enabled = true
max_daily_loss = 50000
max_position_pct = 30
max_orders_per_minute = 10
max_positions = 10

# Per-account override (optional, field-level merge over [risk])
[accounts.live.risk]
max_daily_loss = 10000
max_position_pct = 20
```

`write_template` in server_config.py is updated to emit these with comments.

## 11. Accepted Compromises and Worst Cases

| Compromise | Worst case | Mitigation |
|---|---|---|
| Snapshot may be up to 30s stale | External concurrent selling (QMT GUI) could let an oversize buy through; broker rejects downstream | xtquant callback covers GUI-originated trades (assumption, to be verified in smoke test); marked as documented dependency |
| Baseline captured at daemon start, not market open, if daemon late | Early-session losses (before daemon up) don't count toward `max_daily_loss` | `baseline_imprecise` flag surfaced in `/risk/status` and `/health`; logged warning on capture |
| `fail closed` on baseline capture failure | Xtquant outage at startup blocks all orders until capture succeeds | Accepted — risk layer must not run uncalibrated; startup retry loop minimizes window |
| Not tracking sell-side pending | None — omission is conservative (inflates existing_mv denominator relative to true future state), never under-reports | No fix needed |
| Pending not persisted across daemon restart | After restart, in-flight buys not counted → rapid re-orders could exceed concentration limit | Lazy pending rebuild via `query_stock_orders` on first check per account |
| market order without `last_price` fallback | Zero estimated MV slips through concentration check | Reject `PRICE_UNAVAILABLE`; `last_price` sourced from snapshot or tick fetch |
| RiskManager callbacks share xtquant thread | Thread stall could delay event processing | `_refresh_snapshot` does not hold `_lock` during I/O; event handlers are O(pending) |
| Breaker reset does not re-baseline | Next order may instantly re-trip if PnL still below threshold | By design — prevents accidental "zero-out" of the daily guard |
| Reset history only in state file + audit log | State file corruption loses history | Audit log is append-only; acceptable |

## 12. Testing Strategy

Unit tests (`tests/miniqmt_cli/test_risk.py`):
- Baseline lifecycle (capture, reuse same day, reset new day, imprecise flag, capture failure)
- Breaker (trip on daily loss, block buy, allow close-sell, reject oversell)
- Limits (frequency window sliding, max_positions, position_pct, pending contribution)
- Pending tracking (fill/partial/cancel/reject/expire, startup rebuild)
- Market order price handling
- Snapshot invalidation, hard expiry
- State atomic write, per-account override, disabled mode

Integration (`tests/miniqmt_cli/test_routes_risk.py`):
- Order rejection returns 400 with structured detail
- Reset endpoint (happy path, live confirm, missing note, not-tripped)
- Status endpoint shape (single, all)
- /health priority ordering
- All six audit phases written

CLI (`tests/miniqmt_cli/test_cli_commands.py` additions):
- `risk status` table and JSON outputs
- `risk reset` with/without --confirm-live, --yes, --note

Fixture updates (`tests/miniqmt_cli/conftest.py`):
- fake_xtquant: parameterizable `query_stock_asset` returns, injectable trade/order event triggers, pre-populated `query_stock_orders` for rebuild tests

## 13. Delivery Criteria

Phase 2 is complete when:

1. All tests in §12 pass (estimated +60 tests).
2. `server.toml` template updated with commented `[risk]` + `[accounts.<name>.risk]` sections.
3. `docs/roadmap-auto-trading.md` Phase 2 row marked "完成".
4. Memory note `project_miniqmt_cli.md` updated with RiskManager architecture summary.
5. Manual smoke test (dry_run + fake xtquant): baseline capture → intentional loss → breaker trip → reset → buy blocked → sell close-only allowed → new trading day → baseline reset.

## 14. File Inventory

New files:
- `tools/miniqmt_cli/src/miniqmt_cli/server/risk.py` (~400 lines)
- `tools/miniqmt_cli/src/miniqmt_cli/server/routes_risk.py` (~80 lines)
- `tools/miniqmt_cli/src/miniqmt_cli/commands/risk.py` (~120 lines)
- `tests/miniqmt_cli/test_risk.py` (~600 lines)
- `tests/miniqmt_cli/test_routes_risk.py` (~200 lines)

Changed files:
- `tools/miniqmt_cli/src/miniqmt_cli/server_config.py` — RiskConfig, AccountConfig.risk, ServerConfig.risk, effective_risk()
- `tools/miniqmt_cli/src/miniqmt_cli/server/session.py` — instantiate RiskManager, forward trade events, startup retry task
- `tools/miniqmt_cli/src/miniqmt_cli/server/routes_trade.py` — insert check_order + record_accepted calls
- `tools/miniqmt_cli/src/miniqmt_cli/server/app.py` — mount routes_risk, /health extension
- `tools/miniqmt_cli/src/miniqmt_cli/main.py` — register `risk` command group
- `tools/miniqmt_cli/src/miniqmt_cli/client/errors.py` — add `RiskReject` exception (exit_code=3)
- `tests/miniqmt_cli/conftest.py` — fake_xtquant extensions

Estimated diff: +~1400 lines (code+tests), ~150 lines changed.
