"""Health endpoint structured response + trader session lifecycle.

`/health` returns two independent top-level blocks: `daemon` (process-self:
xtquant module load, etc.) and `accounts` (per-account substates the daemon
can observe — trader session, risk breaker, baseline). There is no flat
`state` enum; callers compose decisions from the structured fields.
"""
from __future__ import annotations


def _body_order(account="sim", code="000001.SZ", side="buy", volume=100,
                price=12.0, client_req_id="req-h"):
    return {
        "account": account, "code": code, "side": side, "volume": volume,
        "price": price, "client_req_id": client_req_id, "type": "limit",
    }


# ---------- new structured shape ----------

def test_health_has_daemon_and_accounts_blocks(client, fake_xtquant):
    body = client.get("/health").json()
    assert "daemon" in body
    assert "accounts" in body
    assert body["daemon"]["state"] == "up"
    assert isinstance(body["daemon"]["xtquant_loaded"], bool)
    # accounts map is keyed by configured account name (from conftest)
    assert set(body["accounts"].keys()) == {"sim", "live"}


def test_health_account_block_fields(client, fake_xtquant):
    body = client.get("/health").json()
    sim = body["accounts"]["sim"]
    assert "trader" in sim
    assert "risk_breaker" in sim
    assert "baseline" in sim
    trader = sim["trader"]
    assert trader["state"] in {"never_connected", "alive", "lost"}
    assert "last_connect_at" in trader
    assert "last_disconnect_at" in trader


# ---------- trader.state lifecycle ----------

def test_trader_state_starts_never_connected(client, fake_xtquant):
    body = client.get("/health").json()
    assert body["accounts"]["sim"]["trader"]["state"] == "never_connected"
    assert body["accounts"]["sim"]["trader"]["last_connect_at"] is None
    assert body["accounts"]["sim"]["trader"]["last_disconnect_at"] is None


def test_trader_state_alive_after_first_account_call(client, fake_xtquant):
    """First call that triggers trader login flips state to alive."""
    r = client.get("/trade/asset", params={"account": "sim"})
    assert r.status_code == 200
    body = client.get("/health").json()
    sim = body["accounts"]["sim"]
    assert sim["trader"]["state"] == "alive"
    assert sim["trader"]["last_connect_at"] is not None
    # Other accounts that haven't been touched stay never_connected
    assert body["accounts"]["live"]["trader"]["state"] == "never_connected"


def test_trader_state_lost_after_disconnect_callback(client, fake_xtquant):
    """Simulate xtquant firing on_disconnected — daemon must surface it."""
    # Bring trader up first.
    client.get("/trade/asset", params={"account": "sim"})
    # Drive xtquant's disconnect callback (runs on the fake trader's callback
    # bridge, which in production is invoked from xtquant's C++ thread).
    trader = fake_xtquant.traders[-1]
    assert trader.callback is not None
    trader.callback.on_disconnected()

    body = client.get("/health").json()
    sim = body["accounts"]["sim"]
    assert sim["trader"]["state"] == "lost"
    assert sim["trader"]["last_disconnect_at"] is not None
    # last_connect_at survives so callers can compute "how long ago"
    assert sim["trader"]["last_connect_at"] is not None


# ---------- no flat `state` field ----------

def test_no_flat_state_field(client, fake_xtquant):
    """The flat `state` enum has been removed. Composite names like
    `daemon_up_no_trader` no longer leak into the response — callers must
    read the structured blocks."""
    body = client.get("/health").json()
    assert "state" not in body
    assert "tripped_accounts" not in body
    assert "accounts_pending" not in body
    assert "trader_lost_accounts" not in body


def test_concurrent_signals_all_visible_in_structured_block(client, fake_xtquant):
    """When trader is lost AND breaker is tripped, both surface independently
    on the account subblock — there's no priority-flattening anymore."""
    sess = client.app.state.session
    client.post("/trade/order", json=_body_order(client_req_id="req-multi"))
    trader = fake_xtquant.traders[-1]
    trader.callback.on_disconnected()
    sess.risk.trip_breaker("sim", reason="testing")
    sub = client.get("/health").json()["accounts"]["sim"]
    assert sub["trader"]["state"] == "lost"
    assert sub["risk_breaker"] == "tripped"
