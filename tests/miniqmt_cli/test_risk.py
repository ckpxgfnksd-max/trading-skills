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
        name="sim", account_id="55001234", account_type="STOCK",
    )
    return cfg


class _FakeTraderCtx:
    """Minimal stand-in for session.get_trader-equivalent; returns a FakeTrader."""

    def __init__(self):
        from tests.fakes.xtquant_stub import FakeStockAccount, FakeTrader
        self.trader = FakeTrader("/tmp", 42)
        self.acc = FakeStockAccount(account_id="55001234", account_type="STOCK")

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
