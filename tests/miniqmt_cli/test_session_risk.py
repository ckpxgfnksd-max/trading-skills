"""Session + Risk integration tests."""
from __future__ import annotations

import pytest


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
