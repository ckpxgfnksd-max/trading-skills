"""Risk status and reset endpoints."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

router = APIRouter(prefix="/risk", tags=["risk"])


def _session(request: Request):
    return request.app.state.session


class ResetRequest(BaseModel):
    account: str
    operator_note: str = Field(..., min_length=1, max_length=200)
    confirm_live_last4: Optional[str] = None


def _format_account_status(sess, account: str) -> dict:
    eff = sess.cfg.effective_risk(account)
    snapshot = sess.risk.snapshot_status(account)
    state = snapshot["state"]
    pending = snapshot["pending_orders"]
    orders_in_window = snapshot["orders_in_window"]

    current_asset = None
    try:
        snap = sess.risk.get_snapshot(account)
        current_asset = snap.total_asset
    except Exception as e:
        log.debug("snapshot unavailable for %s: %s", account, e)

    base = state["baseline_total_asset"] if state else None
    pnl = (current_asset - base) if (current_asset is not None and base is not None) else None

    reset_count_today = 0
    if state:
        td = state["trade_date"]
        if td:
            ymd = f"{td[0:4]}-{td[4:6]}-{td[6:8]}"
            reset_count_today = sum(
                1 for r in state["reset_history"]
                if isinstance(r.get("reset_at"), str) and r["reset_at"].startswith(ymd)
            )

    return {
        "trade_date": state["trade_date"] if state else None,
        "baseline_total_asset": base,
        "baseline_captured_at": state["baseline_captured_at"] if state else None,
        "baseline_imprecise": state["baseline_imprecise"] if state else None,
        "current_total_asset": current_asset,
        "daily_pnl": pnl,
        "breaker_tripped": bool(state and state["breaker_tripped"]),
        "breaker_reason": state["breaker_reason"] if state else None,
        "breaker_tripped_at": state["breaker_tripped_at"] if state else None,
        "effective_config": {
            "enabled": eff.enabled,
            "max_daily_loss": eff.max_daily_loss,
            "max_position_pct": eff.max_position_pct,
            "max_orders_per_minute": eff.max_orders_per_minute,
            "max_positions": eff.max_positions,
        },
        "orders_in_window": orders_in_window,
        "pending_orders": pending,
        "reset_count_today": reset_count_today,
        "reset_history": state["reset_history"] if state else [],
    }


@router.get("/status")
def status(request: Request, account: Optional[str] = Query(None)):
    sess = _session(request)
    sess.audit.append(
        phase="risk_status_query",
        account=account or "*",
    )
    if account:
        if account not in sess.cfg.accounts:
            raise HTTPException(status_code=404, detail=f"unknown account: {account}")
        data = _format_account_status(sess, account)
        data["account"] = account
        return data
    return {
        "accounts": {
            name: _format_account_status(sess, name)
            for name in sess.cfg.accounts
        },
    }


@router.post("/reset")
def reset(request: Request, body: ResetRequest):
    sess = _session(request)
    if body.account not in sess.cfg.accounts:
        raise HTTPException(status_code=404, detail=f"unknown account: {body.account}")
    acc = sess.cfg.accounts[body.account]
    if acc.requires_confirm_live:
        if not body.confirm_live_last4:
            raise HTTPException(
                status_code=400,
                detail="confirm_live_last4 required for live account",
            )
        if body.confirm_live_last4 != acc.last4:
            raise HTTPException(
                status_code=400,
                detail="confirm_live_last4 does not match",
            )
    try:
        result = sess.risk.reset_breaker(body.account, body.operator_note)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result
