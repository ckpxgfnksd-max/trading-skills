"""Exhaustive trade endpoint tests, including CLI-bypass attacks."""
from __future__ import annotations

import json

import pytest


def _body(account="sim", code="000001.SZ", side="buy", volume=100, price=12.0,
          client_req_id="req-1", confirm_live_last4=None, type="limit"):
    return {
        "account": account,
        "code": code,
        "side": side,
        "volume": volume,
        "price": price,
        "type": type,
        "client_req_id": client_req_id,
        "confirm_live_last4": confirm_live_last4,
    }


def test_list_accounts_masks_id(client):
    resp = client.get("/trade/accounts")
    accounts = resp.json()["accounts"]
    names = {a["name"] for a in accounts}
    assert names == {"sim", "live"}
    for a in accounts:
        assert "*" in a["account_id_masked"]


def test_account_meta(client):
    resp = client.get("/trade/account/meta", params={"name": "live"})
    assert resp.status_code == 200
    assert resp.json()["requires_confirm_live"] is True


def test_account_meta_unknown(client):
    resp = client.get("/trade/account/meta", params={"name": "ghost"})
    assert resp.status_code == 404


def test_positions_known_account(client):
    resp = client.get("/trade/positions", params={"account": "sim"})
    assert resp.status_code == 200
    assert resp.json()[0]["stock_code"] == "000001.SZ"


def test_positions_whitelist_bypass(client):
    """Direct POST with unknown account must be rejected at daemon."""
    resp = client.get("/trade/positions", params={"account": "ghost"})
    assert resp.status_code == 400
    assert "whitelist" in resp.json()["detail"]


def test_preview(client):
    resp = client.get(
        "/trade/preview",
        params={"account": "sim", "code": "000001.SZ", "side": "buy", "volume": 100, "price": 12.0},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["est_cost"] == 1200.0
    assert body["last_price"] == 12.34


def test_order_sim_happy_path(client, server_cfg, fake_xtquant):
    resp = client.post("/trade/order", json=_body(client_req_id="req-happy"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    # Audit rows
    audit_path = server_cfg.resolved_audit_log_path()
    lines = [json.loads(l) for l in audit_path.read_text().splitlines()]
    assert any(r["phase"] == "pre" for r in lines)
    assert any(r["phase"] == "post" and r["status"] == "ok" for r in lines)


def test_order_whitelist_bypass_no_audit(client, server_cfg, fake_xtquant):
    resp = client.post("/trade/order", json=_body(account="ghost", client_req_id="req-ghost"))
    assert resp.status_code == 400
    # No audit row for rejected whitelist
    audit_path = server_cfg.resolved_audit_log_path()
    text = audit_path.read_text() if audit_path.exists() else ""
    assert "req-ghost" not in text
    # No order_stock invocation on the fake
    assert fake_xtquant.trader_factory_calls == 0


def test_live_gate_no_field(client, server_cfg, fake_xtquant):
    resp = client.post("/trade/order", json=_body(account="live", client_req_id="req-lg1"))
    assert resp.status_code == 400
    assert "confirm_live_last4" in resp.json()["detail"]
    audit_path = server_cfg.resolved_audit_log_path()
    assert not audit_path.exists() or "req-lg1" not in audit_path.read_text()
    assert fake_xtquant.trader_factory_calls == 0


def test_live_gate_wrong_last4(client, server_cfg, fake_xtquant):
    resp = client.post(
        "/trade/order",
        json=_body(account="live", confirm_live_last4="9999", client_req_id="req-lg2"),
    )
    assert resp.status_code == 400
    assert "last 4" in resp.json()["detail"]
    audit_path = server_cfg.resolved_audit_log_path()
    assert not audit_path.exists() or "req-lg2" not in audit_path.read_text()
    assert fake_xtquant.trader_factory_calls == 0


def test_live_gate_correct_last4(client, server_cfg, fake_xtquant):
    # server_cfg.accounts["live"] has account_id 1230002 -> last4 == "0002"
    resp = client.post(
        "/trade/order",
        json=_body(account="live", confirm_live_last4="0002", client_req_id="req-lg3"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"
    assert fake_xtquant.trader_factory_calls == 1


def test_idempotency_hit(client, fake_xtquant):
    body = _body(client_req_id="req-idem-1")
    r1 = client.post("/trade/order", json=body)
    r2 = client.post("/trade/order", json=body)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json().get("idempotent_hit") is True
    # Fake order_stock should have been invoked only once
    orders = fake_xtquant.traders[0].orders_placed
    assert len(orders) == 1


def test_idempotency_ttl_expires(client, server_cfg, fake_xtquant):
    """After TTL, same id re-enters the flow."""
    # Force a tiny TTL
    client.app.state.session.cfg.idempotency_ttl_seconds = 0
    body = _body(client_req_id="req-idem-ttl")
    client.post("/trade/order", json=body)
    # time.time will differ on second call; entry age > 0 > ttl => purged
    client.post("/trade/order", json=body)
    orders = fake_xtquant.traders[0].orders_placed
    assert len(orders) == 2


def test_order_exit_on_xttrader_error(client, server_cfg, fake_xtquant):
    """If order_stock raises, post-audit is still written with status=error."""
    # Prime the session so trader exists
    r0 = client.post("/trade/order", json=_body(client_req_id="prime"))
    assert r0.status_code == 200
    fake_xtquant.traders[0].should_fail_order = True
    r1 = client.post("/trade/order", json=_body(client_req_id="boom"))
    assert r1.status_code == 500
    audit_lines = [
        json.loads(l)
        for l in server_cfg.resolved_audit_log_path().read_text().splitlines()
    ]
    boom_rows = [r for r in audit_lines if r.get("client_req_id") == "boom"]
    assert any(r["phase"] == "pre" for r in boom_rows)
    assert any(r["phase"] == "post" and r["status"] == "error" for r in boom_rows)


def test_cancel_flow(client, fake_xtquant):
    r1 = client.post("/trade/order", json=_body(client_req_id="place1"))
    assert r1.status_code == 200
    order_id = r1.json()["order_id"]
    r2 = client.post(
        "/trade/cancel",
        json={"account": "sim", "order_id": order_id, "client_req_id": "cancel1"},
    )
    assert r2.status_code == 200
    assert fake_xtquant.traders[0].cancels == [order_id]


def test_order_submit_timeout_returns_504_submit_indeterminate(
    client, server_cfg, fake_xtquant, monkeypatch,
):
    """When order_stock exceeds the submit timeout, return 504 (not 503)
    with structured submit_indeterminate detail and audit status
    'indeterminate' (not 'error'). Retrying blindly on this response would
    risk a double position; the contract has to be loud."""
    import time

    from miniqmt_cli.server import routes_trade, xttrader_adapter

    # Tighten the submit timeout so the test is fast.
    monkeypatch.setattr(routes_trade, "XT_TIMEOUT_SUBMIT", 0.2)

    # Make order_stock block long enough to time out.
    def slow_order(*args, **kwargs):
        time.sleep(0.6)
        return {"seq": 999}
    monkeypatch.setattr(xttrader_adapter, "order_stock", slow_order)

    resp = client.post("/trade/order", json=_body(client_req_id="req-to1"))
    assert resp.status_code == 504, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "submit_indeterminate"
    assert detail["client_req_id"] == "req-to1"
    assert detail["reconcile_via"] == "/trade/orders"
    assert "DO NOT RETRY" in detail["message"]
    # Audit must NOT say "error" -- it says indeterminate so reconcilers
    # can distinguish a known failure from an unknown state.
    rows = [
        json.loads(l)
        for l in server_cfg.resolved_audit_log_path().read_text().splitlines()
    ]
    post_rows = [
        r for r in rows
        if r.get("phase") == "post" and r.get("client_req_id") == "req-to1"
    ]
    assert len(post_rows) == 1
    assert post_rows[0]["status"] == "indeterminate"
    assert "broker state unknown" in post_rows[0]["error"]


def test_cancel_submit_timeout_returns_504_cancel_indeterminate(
    client, server_cfg, fake_xtquant, monkeypatch,
):
    """Cancel-side mirror of the above. Cancel is also state-changing on
    the broker, so timeout is INDETERMINATE not just an error."""
    import time

    from miniqmt_cli.server import routes_trade, xttrader_adapter

    # Place an order first to have something to cancel.
    r1 = client.post("/trade/order", json=_body(client_req_id="place-cancel-to"))
    assert r1.status_code == 200
    order_id = r1.json()["order_id"]

    monkeypatch.setattr(routes_trade, "XT_TIMEOUT_SUBMIT", 0.2)

    def slow_cancel(*args, **kwargs):
        time.sleep(0.6)
        return {"seq": 0}
    monkeypatch.setattr(xttrader_adapter, "cancel_order_stock", slow_cancel)

    resp = client.post(
        "/trade/cancel",
        json={"account": "sim", "order_id": order_id, "client_req_id": "cancel-to1"},
    )
    assert resp.status_code == 504, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "cancel_indeterminate"
    assert detail["order_id"] == order_id
    assert detail["reconcile_via"] == "/trade/orders"
    rows = [
        json.loads(l)
        for l in server_cfg.resolved_audit_log_path().read_text().splitlines()
    ]
    post_rows = [
        r for r in rows
        if r.get("phase") == "post" and r.get("client_req_id") == "cancel-to1"
    ]
    assert len(post_rows) == 1
    assert post_rows[0]["status"] == "indeterminate"


def test_order_rejected_by_risk_check(client, server_cfg, fake_xtquant):
    """Risk check rejection should return 400 and audit a risk_check row."""
    # Shrink max_orders_per_minute so the 2nd order hits FREQUENCY
    from miniqmt_cli.server_config import RiskConfig
    sess = client.app.state.session
    sess.cfg.risk = RiskConfig(max_orders_per_minute=1)
    # Prime trader login and baseline via first order
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
    assert any(
        r.get("allow") is False and r.get("reject_code") == "FREQUENCY"
        for r in risk_rows
    )


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
        r.get("phase") == "risk_check"
        and r.get("client_req_id") == "req-rc-ok"
        and r.get("allow") is True
        for r in rows
    )
