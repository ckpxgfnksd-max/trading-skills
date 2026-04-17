# Risk Control Smoke Test

Run against a running daemon (real xtquant or dry-run + TestClient). Validates end-to-end flow.

## Setup

```
miniqmt-cli serve &
miniqmt-cli setup   # first run only
```

Confirm `~/.miniqmt_cli/server.toml` has a `[risk]` section with conservative defaults (max_daily_loss=50000, max_position_pct=30, max_orders_per_minute=10, max_positions=10).

## 1. Baseline capture

```
miniqmt-cli risk status --account sim
```

Expected on first run: `Trade date: (not captured)`.

Place a small order to trigger baseline capture:
```
miniqmt-cli order buy --account sim --code 000001.SZ --volume 100 --price 12 --yes
```

Re-check status: `Trade date: <today>`, `Baseline asset: <current total>`, `Daily PnL: 0.00`.

## 2. Trip the breaker

Edit `~/.miniqmt_cli/server.toml`: set `max_daily_loss = 100`. Restart daemon. Place one order to re-capture baseline. Then manipulate fake xtquant or wait for real market MTM to drop the total_asset so `current < baseline - 100`. The next order should be rejected with `BREAKER_TRIPPED`.

Verify:
```
miniqmt-cli health
# -> {"state": "risk_breaker_tripped", "tripped_accounts": ["sim"]}
```

## 3. Sell close-only

With breaker tripped:
- Buy attempt should be rejected: `risk_reject [BREAKER_TRIPPED]`
- Sell of existing position should be allowed.

## 4. Reset

```
miniqmt-cli risk reset --account sim --note "smoke test" --yes
```

Verify output: `Reset OK. Previous reason: <...>`. Daily PnL still reflects the negative; next buy that crosses the threshold re-trips immediately.

## 5. Audit trail

```
tail ~/.miniqmt_cli/orders.jsonl
```

Confirm phases present: `risk_baseline_capture`, `risk_check` (allow and reject cases), `risk_breaker_trip`, `risk_breaker_reset`, `risk_status_query`, and `risk_pending_rebuild` (on daemon restart with open orders).

## 6. Per-account override

Edit server.toml:
```toml
[accounts.live.risk]
max_daily_loss = 10000
max_position_pct = 20
```

Restart daemon. `miniqmt-cli risk status --account live` should show `Config: max_loss=10000 max_pos_pct=20% ...` while `--account sim` retains the 50000 default.
