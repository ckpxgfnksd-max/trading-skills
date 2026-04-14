"""Per-account login lock prevents double-login races."""
from __future__ import annotations

import asyncio

import pytest

from miniqmt_cli.server.session import SessionManager


@pytest.mark.asyncio
async def test_concurrent_get_trader_single_login(server_cfg, fake_xtquant):
    sess = SessionManager(server_cfg, dry_run=False)
    results = await asyncio.gather(
        sess.get_trader("sim"),
        sess.get_trader("sim"),
        sess.get_trader("sim"),
    )
    # All calls return the same handle.
    first = results[0]
    for r in results[1:]:
        assert r is first
    # Only one trader factory call happened.
    assert fake_xtquant.trader_factory_calls == 1
    assert len(fake_xtquant.traders) == 1
    assert len(fake_xtquant.traders[0].subscribed_accounts) == 1
