"""Unit tests for RiskManager."""
from __future__ import annotations

import json

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


def test_save_replaces_existing_file_atomically(tmp_path, monkeypatch):
    """If os.replace fails, the existing file must remain intact."""
    import os as _os
    from miniqmt_cli.server.risk import AccountRiskState, RiskStateFile
    p = tmp_path / "rs.json"
    state = RiskStateFile.load(p)
    state.accounts["sim"] = AccountRiskState(
        trade_date="20260417",
        baseline_total_asset=100.0,
        baseline_captured_at="2026-04-17T00:00:00Z",
    )
    state.save()
    original_bytes = p.read_bytes()

    def boom(src, dst):
        raise OSError("simulated failure")

    monkeypatch.setattr(_os, "replace", boom)
    state.accounts["sim"].baseline_total_asset = 999.0
    with pytest.raises(OSError):
        state.save()
    # Original file must be unchanged
    assert p.read_bytes() == original_bytes


def test_load_quarantines_corrupt_file(tmp_path):
    """A corrupt state file gets renamed and load returns empty."""
    from miniqmt_cli.server.risk import RiskStateFile
    p = tmp_path / "rs.json"
    p.write_text("not valid json {{{", encoding="utf-8")
    state = RiskStateFile.load(p)
    assert state.accounts == {}
    # Original file should have been moved; look for a quarantine sibling
    siblings = list(tmp_path.iterdir())
    assert not p.exists() or p.read_text() != "not valid json {{{"
    assert any(".corrupt-" in s.name for s in siblings)


def test_risk_state_version_field(tmp_path):
    from miniqmt_cli.server.risk import RiskStateFile
    p = tmp_path / "rs.json"
    state = RiskStateFile.load(p)
    state.save()
    data = json.loads(p.read_text())
    assert data["version"] == 1


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
    monkeypatch.setattr(risk_mod, "_today_str", lambda: "20260416")
    rm = RiskManager(cfg, audit, ctx)
    rm.ensure_baseline("sim")
    assert rm._state.accounts["sim"].trade_date == "20260416"
    monkeypatch.setattr(risk_mod, "_today_str", lambda: "20260417")
    ctx.trader.asset_override = {"total_asset": 200.0}
    rm.ensure_baseline("sim")
    assert rm._state.accounts["sim"].trade_date == "20260417"
    assert rm._state.accounts["sim"].baseline_total_asset == 200.0


def test_baseline_imprecise_flag_when_after_open(tmp_path, monkeypatch):
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
    assert "sim" not in rm._state.accounts
    # Fail-closed: no audit row written when baseline fails
    audit_path = tmp_path / "orders.jsonl"
    if audit_path.exists():
        assert "risk_baseline_capture" not in audit_path.read_text()


def test_baseline_audit_row_written(tmp_path):
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
    assert s2.total_asset == 1000000.0
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
    ctx.trader.should_fail_asset_query = True
    t[0] = 1035.0  # >30s, triggers refresh; refresh fails -> stale fallback
    snap = rm.get_snapshot("sim")
    assert snap.total_asset == 100.0   # stale-but-within-hard-expiry
    t[0] = 1400.0  # >5 min since last successful refresh
    with pytest.raises(SnapshotStale):
        rm.get_snapshot("sim")


def test_record_accepted_buy_adds_to_pending(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    rm = RiskManager(cfg, audit, _FakeTraderCtx())
    rm.record_accepted("sim", "buy", "000001.SZ", 500, 10.0, order_id=200)
    e = rm._pending["sim"]["000001.SZ"]
    assert e.buy_volume == 500
    assert e.buy_amount == 5000.0
    assert e.by_order_id[200] == {"side": "buy", "volume": 500, "amount": 5000.0}


def test_record_accepted_sell_tracks_pending(tmp_path):
    """Sell orders now update sell_volume/sell_amount in PendingEntry."""
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    rm = RiskManager(cfg, audit, _FakeTraderCtx())
    rm.record_accepted("sim", "sell", "000001.SZ", 300, 10.0, order_id=700)
    e = rm._pending["sim"]["000001.SZ"]
    assert e.sell_volume == 300
    assert e.sell_amount == 3000.0
    assert e.buy_volume == 0
    assert e.by_order_id[700]["side"] == "sell"


def test_record_accepted_mixed_buy_and_sell_same_code(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    rm = RiskManager(cfg, audit, _FakeTraderCtx())
    rm.record_accepted("sim", "buy", "000001.SZ", 500, 10.0, order_id=701)
    rm.record_accepted("sim", "sell", "000001.SZ", 200, 10.0, order_id=702)
    e = rm._pending["sim"]["000001.SZ"]
    assert e.buy_volume == 500
    assert e.sell_volume == 200


def test_order_status_filled_removes_sell_pending(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    rm = RiskManager(cfg, audit, _FakeTraderCtx())
    rm.record_accepted("sim", "sell", "000001.SZ", 300, 10.0, order_id=800)
    rm.on_trade_event({
        "type": "order_status", "account": "sim", "order_id": 800,
        "status": "filled", "volume": 300, "filled_volume": 300,
    })
    assert "000001.SZ" not in rm._pending.get("sim", {})


def test_order_status_partial_fill_reduces_sell_pending(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    rm = RiskManager(cfg, audit, _FakeTraderCtx())
    rm.record_accepted("sim", "sell", "000001.SZ", 300, 10.0, order_id=801)
    rm.on_trade_event({
        "type": "order_status", "account": "sim", "order_id": 801,
        "status": "partially_filled", "volume": 300, "filled_volume": 100,
    })
    e = rm._pending["sim"]["000001.SZ"]
    assert e.sell_volume == 200
    assert e.sell_amount == 2000.0


def test_check_order_position_pct_unaffected_by_sell_pending(tmp_path):
    """Conservative semantics: sell pending does NOT relax position_pct.

    A pending sell does not reduce existing_mv — if user wanted to rotate
    by selling then buying, the buy check still uses full position as
    denominator. Protects against race where sell doesn't fill.
    """
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path, max_position_pct=10.0)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000.0}
    # 100 shares held at 10.0 (MV=100, = 10% already)
    ctx.trader.positions_override = [
        {"stock_code": "000001.SZ", "volume": 10, "market_value": 100.0}
    ]
    rm = RiskManager(cfg, audit, ctx)
    # Record a sell of 5 shares — does NOT relax the limit
    rm.record_accepted("sim", "sell", "000001.SZ", 5, 10.0, order_id=802)
    # New buy of 1 share would push MV to 100 + 10 = 110 > 100 limit
    d = rm.check_order("sim", "buy", "000001.SZ", 1, 10.0)
    assert d.allow is False
    assert d.reject_code == "POSITION_PCT"


def test_snapshot_status_exposes_sell_pending(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    rm = RiskManager(cfg, audit, _FakeTraderCtx())
    rm.record_accepted("sim", "sell", "000001.SZ", 300, 10.0, order_id=803)
    status = rm.snapshot_status("sim")
    entry = status["pending_orders"]["000001.SZ"]
    assert entry["sell_volume"] == 300
    assert entry["sell_amount"] == 3000.0
    assert entry.get("buy_volume", 0) == 0


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
    rm.on_trade_event({"type": "trade", "account": "ghost", "order_id": 1})
    # Should not raise


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
    ctx.trader.asset_override = {"total_asset": 800.0}
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
    d = rm.check_order("sim", "buy", "000001.SZ", 100, 10.0)
    assert d.reject_code == "FREQUENCY"
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
    d = rm.check_order("sim", "buy", "000001.SZ", 100, 10.0)
    assert d.allow is True


def test_check_order_position_pct_limit(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path, max_position_pct=10.0)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000.0}
    ctx.trader.positions_override = []
    rm = RiskManager(cfg, audit, ctx)
    d = rm.check_order("sim", "buy", "000001.SZ", 11, 10.0)
    assert d.allow is False
    assert d.reject_code == "POSITION_PCT"
    d = rm.check_order("sim", "buy", "000001.SZ", 10, 10.0)
    assert d.allow is True


def test_check_order_position_pct_includes_pending(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path, max_position_pct=10.0)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000.0}
    ctx.trader.positions_override = []
    rm = RiskManager(cfg, audit, ctx)
    d1 = rm.check_order("sim", "buy", "000001.SZ", 5, 10.0)
    assert d1.allow is True
    rm.record_accepted("sim", "buy", "000001.SZ", 5, 10.0, order_id=1)
    d2 = rm.check_order("sim", "buy", "000001.SZ", 6, 10.0)
    assert d2.allow is False
    assert d2.reject_code == "POSITION_PCT"


def test_check_order_market_order_uses_last_price(tmp_path, monkeypatch):
    from miniqmt_cli.server import risk as risk_mod
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path, max_position_pct=10.0)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000.0}
    ctx.trader.positions_override = []
    rm = RiskManager(cfg, audit, ctx)
    monkeypatch.setattr(
        risk_mod, "_get_last_price",
        lambda code: 10.0 if code == "000001.SZ" else None,
    )
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


def test_check_order_baseline_pending_when_query_fails(tmp_path):
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.should_fail_asset_query = True
    rm = RiskManager(cfg, audit, ctx)
    d = rm.check_order("sim", "buy", "000001.SZ", 100, 10.0)
    assert d.allow is False
    assert d.reject_code == "BASELINE_PENDING"


def test_check_order_snapshot_stale(tmp_path, monkeypatch):
    from miniqmt_cli.server import risk as risk_mod
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000.0}
    t = [1000.0]
    monkeypatch.setattr(risk_mod.time, "monotonic", lambda: t[0])
    rm = RiskManager(cfg, audit, ctx)
    rm.ensure_baseline("sim")
    # Populate initial snapshot
    rm.get_snapshot("sim")
    # Now fail queries and advance past hard expiry
    ctx.trader.should_fail_asset_query = True
    t[0] = 1400.0  # >5 min
    d = rm.check_order("sim", "buy", "000001.SZ", 100, 10.0)
    assert d.allow is False
    assert d.reject_code == "SNAPSHOT_STALE"


def test_pending_rebuild_does_not_consume_frequency_window(tmp_path):
    """Regression: daemon restart with N open buys must not eat N/min slots."""
    from miniqmt_cli.server.risk import RiskManager
    cfg = _make_cfg(tmp_path, max_orders_per_minute=3)
    audit = AuditLog(tmp_path / "orders.jsonl")
    ctx = _FakeTraderCtx()
    ctx.trader.asset_override = {"total_asset": 1000000.0}
    # Simulate 3 open buy orders present in xttrader
    ctx.trader.open_orders_override = [
        {"stock_code": "000001.SZ", "order_id": i, "side": "buy",
         "status": "submitted", "order_volume": 100, "traded_volume": 0,
         "price": 10.0}
        for i in range(501, 504)
    ]
    rm = RiskManager(cfg, audit, ctx)
    # First check_order triggers rebuild + would fail if rebuild ate the window
    d = rm.check_order("sim", "buy", "000002.SZ", 100, 10.0)
    assert d.allow is True
    # Window should be empty (rebuild populated pending only)
    assert len(rm._order_window.get("sim", [])) == 0
