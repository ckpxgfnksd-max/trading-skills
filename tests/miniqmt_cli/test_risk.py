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
