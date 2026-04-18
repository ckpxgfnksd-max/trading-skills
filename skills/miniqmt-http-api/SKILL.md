---
name: miniqmt-http-api
description: Reference for the miniqmt-cli daemon's HTTP/SSE API, for external agents (Claude agents, MCP servers, scripts) that want to drive A-share trading programmatically instead of shelling out to the CLI. Use this skill when the user mentions calling the daemon directly, integrating with an agent framework, building an MCP server around miniqmt, or needs JSON payload shapes, SSE event formats, or error codes.
---

# miniqmt-http-api: Programmatic Interface to the Trading Daemon

All endpoints are served by the FastAPI daemon at `http://127.0.0.1:8765` (default), typically reached through an SSH tunnel. FastAPI's built-in `/docs` page serves live OpenAPI if you need schema introspection.

## Base URL

```
http://127.0.0.1:8765
```

All responses are JSON unless the endpoint is SSE (noted below). All requests that take parameters accept them as query strings for `GET` or a JSON body for `POST`.

## Endpoints

### Health & Version

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/version` | Daemon build version |
| GET | `/health` | `{"state": "ready" \| "daemon_up_no_trader" \| "daemon_up_xtquant_missing" \| "daemon_up_baseline_pending", "risk_breaker_tripped": bool}` |

Agents should verify `/health` returns `state == "ready"` **and** `risk_breaker_tripped == false` before trading.

### Market Data (`/data/*`)

| Method | Path | Query | Returns |
|--------|------|-------|---------|
| GET | `/data/sectors` | — | List of sector names |
| GET | `/data/instruments` | `sector`, `limit` | List of `{code, name}` |
| GET | `/data/instrument` | `code` | Full metadata for one code |
| GET | `/data/tick` | `code` (repeatable) | Latest snapshot per code |
| GET | `/data/kline` | `code`, `period` (`1d`/`1m`/`5m`), `start`, `end` | OHLCV bars |
| GET | `/data/ticks` | `code`, `start`, `end` (both `YYYYMMDDHHMMSS`) | Tick-by-tick snapshots |

### Trade (`/trade/*`)

| Method | Path | Query / Body | Returns |
|--------|------|--------------|---------|
| GET | `/trade/accounts` | — | `[{name, account_id_masked, account_type, requires_confirm_live}]` |
| GET | `/trade/account/meta` | `name` | Masked id + type + live flag |
| GET | `/trade/asset` | `account` | `{cash, frozen, total, market_value}` |
| GET | `/trade/positions` | `account` | List of position dicts |
| GET | `/trade/orders` | `account` | Today's orders |
| GET | `/trade/trades` | `account` | Today's fills |
| GET | `/trade/preview` | `account`, `code`, `side`, `volume`, `price` | `{account_id_masked, requires_confirm_live, last_price, est_cost}` |
| POST | `/trade/order` | (see below) | `{seq, order_id, status}` |
| POST | `/trade/cancel` | `{account, order_id, client_req_id}` | `{ok, status}` |

`POST /trade/order` body:

```json
{
  "account": "sim",
  "code": "000001.SZ",
  "side": "buy",
  "volume": 100,
  "price": 10.50,
  "type": "limit",
  "client_req_id": "agent-uuid-1234",
  "confirm_live_last4": null
}
```

- `client_req_id` is **required**; it's the idempotency key — retrying the same id returns the original result.
- `confirm_live_last4` is required when `requires_confirm_live=true` on the account.
- `type` is `limit` or `market`.

### Risk (`/risk/*`)

| Method | Path | Query / Body | Returns |
|--------|------|--------------|---------|
| GET | `/risk/status` | `account` (optional) | Per-account state (see `miniqmt-cli` skill → Risk Control for field meanings) |
| POST | `/risk/reset` | `{account, note, confirm_live_last4?}` | `{ok, reset_count_today}` |

### Streaming / SSE (`/stream/*`)

All three endpoints are **Server-Sent Events**. Each message is `data: <json>\n\n`. Consumers should keep the connection open and parse line-by-line.

| Path | Query | Event shape |
|------|-------|-------------|
| `/stream/tick` | `code` (repeatable) | `{"event": "tick", "code": ..., "lastPrice": ..., ...}` |
| `/stream/kline` | `code` (repeatable), `period` | `{"event": "kline", "code": ..., "open": ..., "close": ..., ...}` |
| `/stream/order` | `account` (optional) | First line is an envelope `{"event": "subscribed", "filter_account": ...}`; subsequent messages are `{"type": "order_status", "account": ..., "order_id": ..., "status": ..., "code": ..., "side": ..., "volume": ..., "filled_volume": ..., "avg_price": ..., "frozen": ...}`. Other types: `order_response`, `trade`. |

`/stream/order` `status` values: `submitted`, `confirmed`, `partially_filled`, `filled`, `cancelled`, `rejected`, `expired`, `pending_cancel`, `unknown`, `unknown_<n>`.

## Error Responses

| HTTP | Body `detail` | Meaning |
|--------|----------------|---------|
| 400  | `"unknown account"` | `account` not in whitelist |
| 400  | `"confirm_live_last4 required"` | Live account without confirmation |
| 400  | `"confirm_live_last4 mismatch"` | Confirmation digits wrong |
| 409  | `"risk: <reason>"` | Risk check rejected (e.g. `breaker_tripped`, `position_pct_exceeded`, `daily_loss_exceeded`, `frequency_exceeded`, `max_positions_exceeded`) |
| 502  | `"broker reject: <reason>"` | xtquant-level rejection (market closed, invalid code, insufficient funds) |
| 503  | `"daemon not ready: <state>"` | Health state is not `ready` |

**Risk rejections are the important ones for agents.** The `reason` string is stable and can be matched against to decide whether to retry, back off, or surface to the user.

## Idempotency & Reconciliation

- Every `POST /trade/order` must carry a unique `client_req_id` (UUID recommended). Re-issuing the same id is safe — the daemon returns the cached result.
- After an agent crash, reconcile by calling `GET /trade/orders?account=...` — every order has its `client_req_id` echoed back. Compare against the agent's local log to identify unknown / orphaned orders before making new decisions.

## Authentication

The daemon has no auth of its own — it relies on the SSH tunnel binding it to `127.0.0.1` on the Mac side. **Do not expose port 8765 publicly.**

## Minimal Agent Example (Python)

```python
import httpx, uuid

BASE = "http://127.0.0.1:8765"

def health():
    return httpx.get(f"{BASE}/health").json()

def place_order(account, code, side, volume, price):
    return httpx.post(f"{BASE}/trade/order", json={
        "account": account, "code": code, "side": side,
        "volume": volume, "price": price, "type": "limit",
        "client_req_id": f"agent-{uuid.uuid4()}",
    }).json()

def stream_orders(account):
    with httpx.stream("GET", f"{BASE}/stream/order",
                      params={"account": account}, timeout=None) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                yield line[5:].strip()
```

## Related Skills

- `miniqmt-cli` — CLI parameter reference and deployment
- `trading-analysis` — Money flow + signals built on top of `/data/ticks`
- `auto-trading-loop` — How to compose these endpoints into a full trading loop
