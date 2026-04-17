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
