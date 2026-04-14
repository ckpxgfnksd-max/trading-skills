"""Shared fixtures for miniqmt-cli tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))  # make `tests.fakes` importable

from tests.fakes import xtquant_stub  # noqa: E402

from miniqmt_cli.server_config import AccountConfig, ServerConfig


@pytest.fixture
def fake_xtquant():
    state = xtquant_stub.install()
    yield state
    xtquant_stub.uninstall()


@pytest.fixture
def server_cfg(tmp_path) -> ServerConfig:
    cfg = ServerConfig(
        host="127.0.0.1",
        port=8765,
        qmt_path=str(tmp_path / "qmt"),
        session_id=42,
        audit_log_path=str(tmp_path / "orders.jsonl"),
        idempotency_ttl_seconds=60,
    )
    cfg.accounts["sim"] = AccountConfig(
        name="sim", account_id="55001234", account_type="STOCK",
    )
    cfg.accounts["live"] = AccountConfig(
        name="live", account_id="88881234", account_type="STOCK",
        requires_confirm_live=True,
    )
    return cfg


@pytest.fixture
def app(server_cfg, fake_xtquant):
    from miniqmt_cli.server.app import create_app
    return create_app(server_cfg, dry_run=False)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    return TestClient(app)
