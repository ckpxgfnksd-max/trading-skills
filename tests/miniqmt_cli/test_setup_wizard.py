"""Unit tests for the setup wizard state layer and command wiring.

We don't exercise the interactive steps themselves (they shell out to
ssh/scp and require a live Windows host); we cover the pure functions
that make re-runs idempotent.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from miniqmt_cli.commands import setup as wizard
from miniqmt_cli.main import cli


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        wizard, "WIZARD_STATE_PATH", tmp_path / ".miniqmt_cli" / "wizard.json"
    )
    monkeypatch.setattr(
        wizard, "CLIENT_CONFIG_PATH", tmp_path / ".miniqmt_cli" / "client.toml"
    )
    return tmp_path


def test_state_roundtrip(fake_home):
    s = wizard.WizardState(win_host="my-win", win_repo="D:/app")
    s.mark("params")
    s.mark("ssh_ok")
    loaded = wizard.WizardState.load()
    assert loaded.win_host == "my-win"
    assert loaded.win_repo == "D:/app"
    assert loaded.is_done("params")
    assert loaded.is_done("ssh_ok")
    assert not loaded.is_done("deploy")


def test_state_file_location(fake_home):
    assert not wizard.WIZARD_STATE_PATH.exists()
    s = wizard.WizardState(win_host="h")
    s.mark("params")
    assert wizard.WIZARD_STATE_PATH.exists()
    data = json.loads(wizard.WIZARD_STATE_PATH.read_text())
    assert data["win_host"] == "h"
    assert "params" in data["completed"]


def test_state_load_missing_file(fake_home):
    s = wizard.WizardState.load()
    assert s.win_host is None
    assert s.win_repo == "C:/apps/trading-skills"
    assert s.completed == []


def test_state_load_corrupt_file(fake_home):
    wizard.WIZARD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    wizard.WIZARD_STATE_PATH.write_text("not json {{{")
    s = wizard.WizardState.load()
    assert s.win_host is None
    assert s.completed == []


def test_state_load_ignores_unknown_keys(fake_home):
    wizard.WIZARD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    wizard.WIZARD_STATE_PATH.write_text(
        json.dumps({"win_host": "h", "future_field": "nope", "completed": []})
    )
    s = wizard.WizardState.load()
    assert s.win_host == "h"


def test_state_mark_is_idempotent(fake_home):
    s = wizard.WizardState(win_host="h")
    s.mark("params")
    s.mark("params")
    s.mark("params")
    assert s.completed == ["params"]


def test_reset_flag_removes_state(fake_home):
    s = wizard.WizardState(win_host="h")
    s.mark("params")
    assert wizard.WIZARD_STATE_PATH.exists()

    runner = CliRunner()
    result = runner.invoke(cli, ["setup", "--reset"])
    assert result.exit_code == 0
    assert "removed" in result.output
    assert not wizard.WIZARD_STATE_PATH.exists()


def test_reset_flag_on_empty_state(fake_home):
    runner = CliRunner()
    result = runner.invoke(cli, ["setup", "--reset"])
    assert result.exit_code == 0


def test_step_number_bounds(fake_home):
    runner = CliRunner()
    result = runner.invoke(cli, ["setup", "--step", "0"])
    assert result.exit_code != 0
    assert "1..9" in result.output or "must be" in result.output


def test_step_number_out_of_range(fake_home):
    runner = CliRunner()
    result = runner.invoke(cli, ["setup", "--step", "99"])
    assert result.exit_code != 0


def test_steps_registry_matches_count():
    assert len(wizard.STEPS) == 9
    names = [name for name, _ in wizard.STEPS]
    assert names == [
        "params", "local_config", "ssh_ok", "remote_python",
        "bootstrap", "server_config", "deploy", "tunnel", "smoke_test",
    ]


def test_setup_help_includes_wizard(fake_home):
    runner = CliRunner()
    result = runner.invoke(cli, ["setup", "--help"])
    assert result.exit_code == 0
    assert "wizard" in result.output.lower()
    assert "--step" in result.output
    assert "--reset" in result.output


def test_setup_appears_in_root_help(fake_home):
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "setup" in result.output
