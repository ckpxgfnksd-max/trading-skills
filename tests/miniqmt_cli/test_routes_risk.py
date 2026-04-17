"""HTTP tests for /risk endpoints."""
from __future__ import annotations

import json

import pytest


def _body_order(account="sim", code="000001.SZ", side="buy", volume=100, price=12.0,
                client_req_id="req-1", confirm_live_last4=None, type="limit"):
    return {
        "account": account, "code": code, "side": side, "volume": volume,
        "price": price, "type": type, "client_req_id": client_req_id,
        "confirm_live_last4": confirm_live_last4,
    }


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
    client.post("/trade/order", json=_body_order(client_req_id="req-rs1"))
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
    assert resp.status_code == 422


def test_risk_reset_live_requires_confirm_last4(client, fake_xtquant):
    sess = client.app.state.session
    # Prime live trader and baseline
    client.post("/trade/order", json=_body_order(
        account="live", confirm_live_last4="1234", client_req_id="req-prime-live",
    ))
    sess.risk.trip_breaker("live", reason="test")
    # Without confirm
    resp = client.post("/risk/reset", json={
        "account": "live", "operator_note": "ok",
    })
    assert resp.status_code == 400
    assert "confirm_live_last4" in resp.json()["detail"]
    # Wrong confirm
    resp = client.post("/risk/reset", json={
        "account": "live", "operator_note": "ok", "confirm_live_last4": "9999",
    })
    assert resp.status_code == 400
    # Correct confirm
    resp = client.post("/risk/reset", json={
        "account": "live", "operator_note": "ok", "confirm_live_last4": "1234",
    })
    assert resp.status_code == 200
    assert resp.json()["account"] == "live"


def test_risk_reset_audits(client, fake_xtquant):
    sess = client.app.state.session
    client.post("/trade/order", json=_body_order(client_req_id="req-prime-sim"))
    sess.risk.trip_breaker("sim", reason="test_reset")
    client.post("/risk/reset", json={
        "account": "sim", "operator_note": "manual",
    })
    rows = [
        json.loads(l)
        for l in client.app.state.session.audit.path.read_text().splitlines()
    ]
    assert any(r.get("phase") == "risk_breaker_reset" for r in rows)


def test_reset_count_today_filters_by_trade_date(client, fake_xtquant):
    """reset_count_today should count only entries matching today's trade_date."""
    sess = client.app.state.session
    # Prime baseline
    client.post("/trade/order", json=_body_order(client_req_id="req-prime-rc"))
    # Inject history rows: one matching today, one older
    state = sess.risk._state.accounts["sim"]
    td = state.trade_date
    ymd = f"{td[0:4]}-{td[4:6]}-{td[6:8]}"
    state.reset_history = [
        {"reset_at": f"{ymd}T01:00:00Z", "previous_reason": "a", "operator_note": "x"},
        {"reset_at": "2020-01-01T00:00:00Z", "previous_reason": "b", "operator_note": "y"},
    ]
    resp = client.get("/risk/status", params={"account": "sim"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["reset_count_today"] == 1
    assert len(body["reset_history"]) == 2


def test_snapshot_status_returns_consistent_state(client, fake_xtquant):
    """snapshot_status should return a dict with state/pending/orders_in_window."""
    sess = client.app.state.session
    client.post("/trade/order", json=_body_order(client_req_id="req-prime-ss"))
    snap = sess.risk.snapshot_status("sim")
    assert snap["state"] is not None
    assert "pending_orders" in snap
    assert "orders_in_window" in snap
    # Unknown account — state is None but keys present
    snap2 = sess.risk.snapshot_status("ghost")
    assert snap2["state"] is None
    assert snap2["pending_orders"] == {}
    assert snap2["orders_in_window"] == 0
