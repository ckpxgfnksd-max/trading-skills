"""FastAPI app factory for miniqmt-cli daemon."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from miniqmt_cli.server._xt_call import shutdown_pool as _xt_shutdown
from miniqmt_cli.server.routes_data import router as data_router
from miniqmt_cli.server.routes_risk import router as risk_router
from miniqmt_cli.server.routes_stream import router as stream_router
from miniqmt_cli.server.routes_trade import router as trade_router
from miniqmt_cli.server.session import SessionManager
from miniqmt_cli.server.watchdog import LoopWatchdog
from miniqmt_cli.server_config import ServerConfig

log = logging.getLogger(__name__)

WATCHDOG_HEARTBEAT_SECONDS = 10.0
WATCHDOG_HANG_TIMEOUT_SECONDS = 60.0
WATCHDOG_DUMP_DIR = Path("~/.miniqmt_cli").expanduser()
WATCHDOG_DISABLE_ENV = "MINIQMT_DISABLE_WATCHDOG"


def create_app(cfg: ServerConfig, dry_run: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        watchdog = None
        if not os.environ.get(WATCHDOG_DISABLE_ENV):
            loop = asyncio.get_running_loop()
            watchdog = LoopWatchdog(
                loop=loop,
                hang_dump_dir=WATCHDOG_DUMP_DIR,
                heartbeat_interval_seconds=WATCHDOG_HEARTBEAT_SECONDS,
                hang_timeout_seconds=WATCHDOG_HANG_TIMEOUT_SECONDS,
            )
            watchdog.start()
            app.state.watchdog = watchdog
        try:
            yield
        finally:
            if watchdog is not None:
                watchdog.stop()
            _xt_shutdown()

    app = FastAPI(title="miniqmt-cli daemon", version="0.3.0", lifespan=lifespan)
    app.state.session = SessionManager(cfg, dry_run=dry_run)
    app.include_router(data_router)
    app.include_router(trade_router)
    app.include_router(stream_router)
    app.include_router(risk_router)

    @app.get("/version")
    def version():
        return {"tag": "sp4", "version": "1.1"}

    @app.get("/health")
    async def health():
        sess = app.state.session

        # daemon-self block: does NOT reflect broker / miniQMT — only this
        # python process + xtquant module load.
        daemon_block: dict = {"state": "up"}
        if dry_run:
            daemon_block["dry_run"] = True
            daemon_block["xtquant_loaded"] = False
        else:
            try:
                await sess.ensure_xtquant()
                daemon_block["xtquant_loaded"] = True
            except Exception as e:
                daemon_block["state"] = "degraded"
                daemon_block["xtquant_loaded"] = False
                daemon_block["xtquant_error"] = str(e)

        # accounts block: per-account substates the daemon can observe.
        # trader.state reflects what the daemon last heard on the SDK channel
        # (xtquant connect / on_disconnected). It is not a probe of miniQMT
        # or the broker — only an account command can definitively prove
        # broker reachability.
        tripped = set(sess.risk.tripped_accounts())
        pending = set(sess.risk.baseline_pending_accounts(
            list(sess.cfg.accounts.keys())
        )) if daemon_block.get("xtquant_loaded") else set()
        accounts_block: dict = {}
        for name in sess.cfg.accounts:
            accounts_block[name] = {
                "trader": sess.trader_state_view(name),
                "risk_breaker": "tripped" if name in tripped else "ok",
                "baseline": "pending" if name in pending else "captured",
            }

        return {"daemon": daemon_block, "accounts": accounts_block}

    return app
