"""End-to-end CLI command tests using click.testing + FastAPI TestClient as
the transport target. We monkeypatch httpx to route requests through the
in-process TestClient.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from miniqmt_cli.main import cli


@pytest.fixture
def cli_env(monkeypatch, tmp_path, client, app):
    # Make client.toml point to a bogus URL; we'll intercept httpx calls.
    cfg_home = tmp_path / ".miniqmt_cli"
    cfg_home.mkdir()
    (cfg_home / "client.toml").write_text(
        '[client]\nmode = "remote"\nserver_url = "http://testserver"\n'
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)

    # Redirect httpx calls to FastAPI TestClient for the duration of the test.
    import httpx

    def _strip(url: str) -> str:
        return url.replace("http://testserver", "")

    def fake_get(url, params=None, timeout=None, **kwargs):
        path = _strip(url)
        return _FakeResp(client.get(path, params=params))

    def fake_post(url, json=None, timeout=None, **kwargs):
        path = _strip(url)
        return _FakeResp(client.post(path, json=json))

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    return None


class _FakeResp:
    def __init__(self, real):
        self.real = real
        self.status_code = real.status_code
        self.text = real.text

    def json(self):
        return self.real.json()


def test_config_client_show(cli_env):
    runner = CliRunner()
    result = runner.invoke(cli, ["config", "client", "show"])
    assert result.exit_code == 0
    assert "testserver" in result.output


def test_sector_list(cli_env):
    runner = CliRunner()
    result = runner.invoke(cli, ["--format", "json", "sector", "list"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    names = {r["sector"] for r in data}
    assert "沪深A股" in names


def test_instrument_list_requires_sector_or_limit(cli_env):
    runner = CliRunner()
    result = runner.invoke(cli, ["instrument", "list"])
    # GuardExit => exit code 3
    assert result.exit_code == 3
    assert "--sector" in result.output and "--limit" in result.output


def test_tick_snapshot(cli_env):
    runner = CliRunner()
    result = runner.invoke(cli, ["--format", "json", "tick", "--code", "000001.SZ"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data[0]["code"] == "000001.SZ"


def test_account_list(cli_env):
    runner = CliRunner()
    result = runner.invoke(cli, ["--format", "json", "account", "list"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert {a["name"] for a in data} == {"sim", "live"}


def test_account_position(cli_env, fake_xtquant):
    runner = CliRunner()
    result = runner.invoke(
        cli, ["--format", "json", "account", "position", "--account", "sim"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data[0]["stock_code"] == "000001.SZ"


def test_order_buy_dry_run_exits_3(cli_env, server_cfg, fake_xtquant):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "order", "buy",
            "--account", "sim",
            "--code", "000001.SZ",
            "--volume", "100",
            "--price", "12.34",
            "--dry-run",
        ],
    )
    assert result.exit_code == 3
    assert "dry-run" in result.output
    # No order_stock invocation
    assert fake_xtquant.trader_factory_calls == 0
    # No audit row
    audit = server_cfg.resolved_audit_log_path()
    assert not audit.exists() or audit.read_text() == ""


def test_order_buy_with_yes(cli_env, server_cfg, fake_xtquant):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "order", "buy",
            "--account", "sim",
            "--code", "000001.SZ",
            "--volume", "100",
            "--price", "12.34",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(fake_xtquant.traders[0].orders_placed) == 1


def test_order_buy_live_without_confirm_exits_3(cli_env, fake_xtquant):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "order", "buy",
            "--account", "live",
            "--code", "000001.SZ",
            "--volume", "100",
            "--price", "12.34",
            "--yes",
        ],
    )
    assert result.exit_code == 3
    assert "confirm-live" in result.output
    assert fake_xtquant.trader_factory_calls == 0


def test_order_buy_live_with_confirm(cli_env, fake_xtquant):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "order", "buy",
            "--account", "live",
            "--code", "000001.SZ",
            "--volume", "100",
            "--price", "12.34",
            "--yes",
            "--confirm-live", "0002",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(fake_xtquant.traders[0].orders_placed) == 1


def test_order_buy_live_with_wrong_confirm(cli_env, fake_xtquant):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "order", "buy",
            "--account", "live",
            "--code", "000001.SZ",
            "--volume", "100",
            "--price", "12.34",
            "--yes",
            "--confirm-live", "9999",
        ],
    )
    # CLI-side last4 format check passes (4 digits), daemon rejects => exit 1
    assert result.exit_code == 1
    assert "last 4" in result.output or "confirm_live_last4" in result.output
    # order_stock was NOT invoked (daemon rejected before placing)
    if fake_xtquant.traders:
        assert len(fake_xtquant.traders[0].orders_placed) == 0


def test_order_buy_confirm_live_bad_length(cli_env):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "order", "buy",
            "--account", "live",
            "--code", "000001.SZ",
            "--volume", "100",
            "--price", "12.34",
            "--yes",
            "--confirm-live", "12",
        ],
    )
    assert result.exit_code == 3
    assert "4 digits" in result.output


# ---------------------------------------------------------------------------
# risk status / reset CLI
# ---------------------------------------------------------------------------

def test_cli_risk_status_json(cli_env, fake_xtquant):
    """`miniqmt-cli --format json risk status` returns structured JSON."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--format", "json", "risk", "status"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert "accounts" in body
    assert "sim" in body["accounts"]


def test_cli_risk_status_per_account_json(cli_env, fake_xtquant):
    runner = CliRunner()
    result = runner.invoke(
        cli, ["--format", "json", "risk", "status", "--account", "sim"]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body.get("account") == "sim"


def test_cli_risk_status_table(cli_env, fake_xtquant):
    """Table mode prints per-account summary with key labels."""
    runner = CliRunner()
    result = runner.invoke(cli, ["risk", "status", "--account", "sim"])
    assert result.exit_code == 0, result.output
    assert "Account: sim" in result.output
    assert "Breaker:" in result.output
    assert "Config:" in result.output


def test_cli_risk_reset_requires_note(cli_env, fake_xtquant):
    """Missing --note -> Click usage error exit code 2."""
    runner = CliRunner()
    result = runner.invoke(
        cli, ["risk", "reset", "--account", "sim", "--yes"]
    )
    assert result.exit_code == 2
    assert "note" in result.output.lower()


def test_cli_risk_reset_requires_account(cli_env, fake_xtquant):
    runner = CliRunner()
    result = runner.invoke(
        cli, ["risk", "reset", "--note", "x", "--yes"]
    )
    assert result.exit_code == 2
    assert "account" in result.output.lower()


def test_cli_risk_reset_breaker_not_tripped(cli_env, fake_xtquant):
    """Daemon would 400, CLI short-circuits via status check -> RiskReject (code 4)."""
    runner = CliRunner()
    result = runner.invoke(
        cli, ["risk", "reset", "--account", "sim", "--note", "x", "--yes"]
    )
    assert result.exit_code == 4, result.output
    combined = (result.output or "").lower()
    assert "not tripped" in combined or "not_tripped" in combined


def test_cli_risk_reset_tripped_success(cli_env, fake_xtquant, client):
    """When breaker is tripped, CLI reset posts and exits 0."""
    # Prime baseline
    resp = client.post("/trade/order", json={
        "account": "sim", "code": "000001.SZ", "side": "buy",
        "volume": 100, "price": 12.0, "type": "limit",
        "client_req_id": "req-cli-reset",
    })
    assert resp.status_code == 200, resp.text
    # Trip the breaker
    client.app.state.session.risk.trip_breaker("sim", reason="cli-test")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["risk", "reset", "--account", "sim", "--note", "manual-cli", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert "Reset OK" in result.output
    # Subsequent status should not be tripped
    resp2 = client.get("/risk/status", params={"account": "sim"})
    assert resp2.json().get("breaker_tripped") is False


def test_cli_risk_reset_declined_without_yes(cli_env, fake_xtquant, client):
    """Tripped breaker + no --yes + prompt input 'no' -> GuardExit (code 3)."""
    resp = client.post("/trade/order", json={
        "account": "sim", "code": "000001.SZ", "side": "buy",
        "volume": 100, "price": 12.0, "type": "limit",
        "client_req_id": "req-cli-decline",
    })
    assert resp.status_code == 200, resp.text
    client.app.state.session.risk.trip_breaker("sim", reason="cli-decline")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["risk", "reset", "--account", "sim", "--note", "maybe"],
        input="no\n",
    )
    assert result.exit_code == 3, result.output
    assert "declined" in result.output.lower()


def test_cli_risk_reset_live_without_confirm(cli_env, fake_xtquant, client):
    """Live account tripped breaker, no --confirm-live -> daemon 400 surfaces as error."""
    # Prime live baseline
    resp = client.post("/trade/order", json={
        "account": "live", "code": "000001.SZ", "side": "buy",
        "volume": 100, "price": 12.0, "type": "limit",
        "client_req_id": "req-cli-live",
        "confirm_live_last4": "0002",
    })
    assert resp.status_code == 200, resp.text
    client.app.state.session.risk.trip_breaker("live", reason="cli-live")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["risk", "reset", "--account", "live", "--note", "no-confirm", "--yes"],
    )
    # ClickException default exit code (1) from transport error-surface path.
    assert result.exit_code != 0
    assert "confirm_live_last4" in result.output
