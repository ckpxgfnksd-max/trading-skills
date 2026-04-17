"""FastAPI app factory for miniqmt-cli daemon."""
from __future__ import annotations

from fastapi import FastAPI

from miniqmt_cli.server.routes_data import router as data_router
from miniqmt_cli.server.routes_risk import router as risk_router
from miniqmt_cli.server.routes_stream import router as stream_router
from miniqmt_cli.server.routes_trade import router as trade_router
from miniqmt_cli.server.session import SessionManager
from miniqmt_cli.server_config import ServerConfig


def create_app(cfg: ServerConfig, dry_run: bool = False) -> FastAPI:
    app = FastAPI(title="miniqmt-cli daemon", version="0.2.0")
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
