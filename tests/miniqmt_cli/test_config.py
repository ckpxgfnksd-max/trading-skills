"""Client/server config loading and separation."""
from __future__ import annotations

from pathlib import Path

from miniqmt_cli.client_config import load_client_config
from miniqmt_cli.server_config import load_server_config


def test_client_config_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINIQMT_CLI_MODE", raising=False)
    monkeypatch.delenv("MINIQMT_CLI_SERVER_URL", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = load_client_config()
    assert cfg.mode == "auto"
    assert cfg.resolve_url() == "http://127.0.0.1:8765"


def test_client_config_from_file(tmp_path, monkeypatch):
    cfg_path = tmp_path / "client.toml"
    cfg_path.write_text(
        '[client]\nmode = "remote"\nserver_url = "http://example.com:1234"\n'
    )
    cfg = load_client_config(str(cfg_path))
    assert cfg.mode == "remote"
    assert cfg.resolve_url() == "http://example.com:1234"


def test_server_config_reads_accounts(tmp_path):
    cfg_path = tmp_path / "server.toml"
    cfg_path.write_text(
        '[server]\nhost = "0.0.0.0"\nport = 9000\nqmt_path = "/tmp/qmt"\n'
        "\n[accounts.sim]\n"
        'account_id = "55001234"\naccount_type = "STOCK"\n'
        "\n[accounts.live]\n"
        'account_id = "88881234"\nrequires_confirm_live = true\n'
    )
    cfg = load_server_config(str(cfg_path))
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9000
    assert set(cfg.accounts) == {"sim", "live"}
    assert cfg.accounts["live"].requires_confirm_live is True
    assert cfg.accounts["live"].last4 == "1234"


def test_client_does_not_touch_server_file(tmp_path, monkeypatch):
    """Loading client config never reads server.toml."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".miniqmt_cli").mkdir()
    (tmp_path / ".miniqmt_cli" / "server.toml").write_text("BROKEN TOML [[ ")
    # Should not raise — client loader must ignore server file
    load_client_config()


def test_session_id_defaults_to_pid(tmp_path):
    cfg = load_server_config(str(tmp_path / "nope.toml"))
    import os
    assert cfg.resolved_session_id() == os.getpid()


def test_account_masked_id():
    from miniqmt_cli.server_config import AccountConfig
    acc = AccountConfig(name="live", account_id="88881234")
    assert acc.masked_id().endswith("1234")
    assert "*" in acc.masked_id()


def test_risk_config_defaults():
    from miniqmt_cli.server_config import RiskConfig
    c = RiskConfig()
    assert c.enabled is True
    assert c.max_daily_loss == 50000.0
    assert c.max_position_pct == 30.0
    assert c.max_orders_per_minute == 10
    assert c.max_positions == 10


def test_effective_risk_uses_global_when_no_override(tmp_path):
    from miniqmt_cli.server_config import load_server_config
    p = tmp_path / "server.toml"
    p.write_text(
        '[server]\nqmt_path = "/tmp"\n'
        '[risk]\nmax_daily_loss = 12345\n'
        '[accounts.sim]\naccount_id = "55001234"\n',
        encoding="utf-8",
    )
    cfg = load_server_config(str(p))
    eff = cfg.effective_risk("sim")
    assert eff.max_daily_loss == 12345
    assert eff.max_positions == 10  # default retained


def test_effective_risk_field_level_override(tmp_path):
    from miniqmt_cli.server_config import load_server_config
    p = tmp_path / "server.toml"
    p.write_text(
        '[server]\nqmt_path = "/tmp"\n'
        '[risk]\nmax_daily_loss = 50000\nmax_position_pct = 30\n'
        '[accounts.live]\naccount_id = "88881234"\n'
        '[accounts.live.risk]\nmax_daily_loss = 10000\n',
        encoding="utf-8",
    )
    cfg = load_server_config(str(p))
    # Assert the override was materialized at LOAD time on the per-account dataclass
    assert cfg.accounts["live"].risk is not None
    assert cfg.accounts["live"].risk.max_daily_loss == 10000
    assert cfg.accounts["live"].risk.max_position_pct == 30.0
    # Stronger invariant: mutating global after load does not affect per-account
    cfg.risk.max_position_pct = 999.0
    assert cfg.effective_risk("live").max_position_pct == 30.0
    assert cfg.effective_risk("live").max_daily_loss == 10000


def test_risk_state_path_default():
    from miniqmt_cli.server_config import ServerConfig
    cfg = ServerConfig()
    assert cfg.risk_state_path == "~/.miniqmt_cli/risk_state.json"


def test_risk_disabled_flag(tmp_path):
    from miniqmt_cli.server_config import load_server_config
    p = tmp_path / "server.toml"
    p.write_text(
        '[server]\nqmt_path = "/tmp"\n'
        '[risk]\nenabled = false\n'
        '[accounts.sim]\naccount_id = "55001234"\n',
        encoding="utf-8",
    )
    cfg = load_server_config(str(p))
    assert cfg.effective_risk("sim").enabled is False


def test_risk_state_path_override(tmp_path):
    from miniqmt_cli.server_config import load_server_config
    p = tmp_path / "server.toml"
    p.write_text(
        '[server]\nqmt_path = "/tmp"\n'
        '[risk]\nstate_path = "/var/lib/miniqmt/state.json"\n',
        encoding="utf-8",
    )
    cfg = load_server_config(str(p))
    assert str(cfg.resolved_risk_state_path()) == "/var/lib/miniqmt/state.json"


def test_resolved_risk_state_path_expands_user(tmp_path):
    from pathlib import Path
    from miniqmt_cli.server_config import ServerConfig
    cfg = ServerConfig()
    expanded = cfg.resolved_risk_state_path()
    # Should be an absolute path (no leading ~)
    assert not str(expanded).startswith("~")
    assert expanded == Path("~/.miniqmt_cli/risk_state.json").expanduser()


def test_risk_config_snapshot_timings_configurable(tmp_path):
    from miniqmt_cli.server_config import load_server_config
    p = tmp_path / "server.toml"
    p.write_text(
        '[server]\nqmt_path = "/tmp"\n'
        '[risk]\nsnapshot_ttl_seconds = 10\nsnapshot_hard_expiry_seconds = 120\n'
        '[accounts.sim]\naccount_id = "55001234"\n',
        encoding="utf-8",
    )
    cfg = load_server_config(str(p))
    eff = cfg.effective_risk("sim")
    assert eff.snapshot_ttl_seconds == 10.0
    assert eff.snapshot_hard_expiry_seconds == 120.0
