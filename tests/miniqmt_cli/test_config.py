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
