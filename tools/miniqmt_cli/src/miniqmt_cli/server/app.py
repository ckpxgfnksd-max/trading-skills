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

    app = FastAPI(title="miniqmt-cli daemon", version="0.2.0", lifespan=lifespan)
    app.state.session = SessionManager(cfg, dry_run=dry_run)
    app.include_router(data_router)
    app.include_router(trade_router)
    app.include_router(stream_router)
    app.include_router(risk_router)

    @app.get("/version")
    def version():
        return {"tag": "sp3", "version": "1.0"}

    @app.get("/health")
    async def health():
        sess = app.state.session
        if dry_run:
            return {"state": "ready", "dry_run": True}
        # Risk breaker has highest priority — surfaces even if xtquant later fails
        tripped = sess.risk.tripped_accounts()
        if tripped:
            return {"state": "risk_breaker_tripped", "tripped_accounts": tripped}
        try:
            await sess.ensure_xtquant()
        except Exception as e:
            return {"state": "daemon_up_xtquant_missing", "error": str(e)}
        if sess.trader_logged_in_count() == 0:
            return {"state": "daemon_up_no_trader"}
        # Baseline pending: trader is up but some configured account hasn't captured baseline
        pending_accounts = sess.risk.baseline_pending_accounts(list(sess.cfg.accounts.keys()))
        if pending_accounts:
            return {
                "state": "daemon_up_baseline_pending",
                "accounts_pending": pending_accounts,
            }
        return {"state": "ready"}

    return app
