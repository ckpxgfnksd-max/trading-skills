"""Session + Risk integration tests."""
from __future__ import annotations

import pytest

from miniqmt_cli.server.session import SessionManager


def test_session_has_risk_manager(app):
    assert hasattr(app.state.session, "risk")


def test_dispatch_order_event_forwards_to_risk(app, fake_xtquant):
    sess = app.state.session
    # Populate pending via record_accepted so we can verify removal
    sess.risk.record_accepted("sim", "buy", "000001.SZ", 100, 10.0, order_id=500)
    sess.dispatch_order_event({
        "type": "order_status", "account": "sim", "order_id": 500,
        "status": "filled", "volume": 100, "filled_volume": 100,
    })
    assert "000001.SZ" not in sess.risk._pending.get("sim", {})


@pytest.mark.asyncio
async def test_get_trader_captures_baseline_eagerly(server_cfg, fake_xtquant):
    """Baseline should be captured on trader login so /health can reach
    'ready' without requiring an order placement."""
    sess = SessionManager(server_cfg, dry_run=False)
    assert sess.risk.baseline_pending_accounts(["sim"]) == ["sim"]
    await sess.get_trader("sim")
    # After login, today's baseline is captured and baseline_pending clears.
    assert sess.risk.baseline_pending_accounts(["sim"]) == []
    state = sess.risk._state.accounts["sim"]
    assert state.baseline_total_asset == 200000.0  # from fake asset


@pytest.mark.asyncio
async def test_get_trader_survives_baseline_capture_failure(
    server_cfg, fake_xtquant, monkeypatch,
):
    """If baseline capture fails at login (e.g., transient broker blip),
    get_trader must still return the handle — the first order will retry."""
    from miniqmt_cli.server import xttrader_adapter

    def _failing_asset(trader, acc):
        raise RuntimeError("transient broker error")

    monkeypatch.setattr(xttrader_adapter, "query_stock_asset", _failing_asset)
    sess = SessionManager(server_cfg, dry_run=False)
    handle = await sess.get_trader("sim")
    assert handle is not None
    assert sess.risk.baseline_pending_accounts(["sim"]) == ["sim"]
    assert sess.trader_logged_in_count() == 1


@pytest.mark.asyncio
async def test_get_trader_retries_baseline_after_transient_failure(
    server_cfg, fake_xtquant, monkeypatch,
):
    """If the first login-time baseline capture fails, a subsequent
    get_trader call (e.g. the next `account asset` probe) must re-attempt
    and succeed once the underlying issue clears — no restart required."""
    from miniqmt_cli.server import xttrader_adapter
    orig = xttrader_adapter.query_stock_asset
    calls = {"n": 0}

    def _flaky_asset(trader, acc):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient broker error")
        return orig(trader, acc)

    monkeypatch.setattr(xttrader_adapter, "query_stock_asset", _flaky_asset)
    sess = SessionManager(server_cfg, dry_run=False)
    await sess.get_trader("sim")  # first call: baseline fails
    assert sess.risk.baseline_pending_accounts(["sim"]) == ["sim"]
    await sess.get_trader("sim")  # second call: baseline succeeds
    assert sess.risk.baseline_pending_accounts(["sim"]) == []
    assert sess.trader_logged_in_count() == 1  # still one trader
