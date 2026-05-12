"""Trade and account endpoints including the order guard pipeline."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from miniqmt_cli.server import xttrader_adapter
from miniqmt_cli.server_config import AccountConfig

log = logging.getLogger(__name__)

router = APIRouter(prefix="/trade", tags=["trade"])

# xtquant trader calls block the C extension and have wedged the event loop
# in production. Always dispatch through this helper with a hard timeout.
XT_TIMEOUT_QUERY = 10.0
XT_TIMEOUT_SUBMIT = 30.0


async def _xt_call(fn: Callable[..., Any], *args, timeout: float, label: str) -> Any:
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=timeout)
    except asyncio.TimeoutError as e:
        log.warning("xttrader call %s timed out after %.1fs", label, timeout)
        raise HTTPException(
            status_code=503,
            detail=f"xttrader {label} timed out after {timeout}s",
        ) from e


def _session(request: Request):
    return request.app.state.session


@router.get("/accounts")
def list_accounts(request: Request):
    cfg = _session(request).cfg
    return {
        "accounts": [
            {
                "name": name,
                "account_id_masked": acc.masked_id(),
                "account_type": acc.account_type,
                "requires_confirm_live": acc.requires_confirm_live,
            }
            for name, acc in cfg.accounts.items()
        ]
    }


@router.get("/account/meta")
def account_meta(request: Request, name: str = Query(...)):
    try:
        acc = _session(request).get_account(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown account: {name}")
    return {
        "name": acc.name,
        "account_id_masked": acc.masked_id(),
        "account_type": acc.account_type,
        "requires_confirm_live": acc.requires_confirm_live,
    }


@router.get("/asset")
async def asset(request: Request, account: str = Query(...)):
    sess = _session(request)
    _require_account(sess, account)
    handle = await sess.get_trader(account)
    return await _xt_call(
        xttrader_adapter.query_stock_asset, handle.trader, handle.acc,
        timeout=XT_TIMEOUT_QUERY, label="query_stock_asset",
    )


@router.get("/positions")
async def positions(request: Request, account: str = Query(...)):
    sess = _session(request)
    _require_account(sess, account)
    handle = await sess.get_trader(account)
    return await _xt_call(
        xttrader_adapter.query_stock_positions, handle.trader, handle.acc,
        timeout=XT_TIMEOUT_QUERY, label="query_stock_positions",
    )


@router.get("/orders")
async def orders(request: Request, account: str = Query(...)):
    sess = _session(request)
    _require_account(sess, account)
    handle = await sess.get_trader(account)
    return await _xt_call(
        xttrader_adapter.query_stock_orders, handle.trader, handle.acc,
        timeout=XT_TIMEOUT_QUERY, label="query_stock_orders",
    )


@router.get("/trades")
async def trades(request: Request, account: str = Query(...)):
    sess = _session(request)
    _require_account(sess, account)
    handle = await sess.get_trader(account)
    return await _xt_call(
        xttrader_adapter.query_stock_trades, handle.trader, handle.acc,
        timeout=XT_TIMEOUT_QUERY, label="query_stock_trades",
    )


@router.get("/preview")
async def preview(
    request: Request,
    account: str = Query(...),
    code: str = Query(...),
    side: str = Query(...),
    volume: int = Query(...),
    price: float = Query(...),
):
    sess = _session(request)
    acc = _require_account(sess, account)
    from miniqmt_cli.server import xtdata_adapter
    last_price = None
    try:
        await sess.ensure_xtquant()
        ticks = await _xt_call(
            xtdata_adapter.get_full_tick, [code],
            timeout=XT_TIMEOUT_QUERY, label="get_full_tick(preview)",
        )
        entry = ticks.get(code, {})
        last_price = entry.get("lastPrice") or entry.get("last_price")
    except HTTPException:
        # timeout already logged; preview is best-effort, swallow
        log.warning("preview: last price unavailable for %s (timeout)", code)
    except Exception as e:
        log.warning("preview: could not fetch last price for %s: %s", code, e)
    est_cost = float(volume) * float(price)
    return {
        "account": account,
        "account_id_masked": acc.masked_id(),
        "requires_confirm_live": acc.requires_confirm_live,
        "code": code,
        "side": side,
        "volume": volume,
        "price": price,
        "last_price": last_price,
        "est_cost": est_cost,
    }


class OrderRequest(BaseModel):
    account: str
    code: str
    side: str = Field(..., pattern="^(buy|sell)$")
    volume: int
    price: float
    type: str = Field("limit", pattern="^(limit|market)$")
    client_req_id: str
    confirm_live_last4: Optional[str] = None


class CancelRequest(BaseModel):
    account: str
    order_id: int
    client_req_id: str


@router.post("/order")
async def place_order(request: Request, body: OrderRequest):
    sess = _session(request)
    acc = _require_account(sess, body.account)

    # Live gate — authoritative check, independent of CLI.
    if acc.requires_confirm_live:
        if not body.confirm_live_last4:
            raise HTTPException(
                status_code=400,
                detail=(
                    "live account requires confirm_live_last4 matching last "
                    "4 digits of account_id"
                ),
            )
        if body.confirm_live_last4 != acc.last4:
            raise HTTPException(
                status_code=400,
                detail="confirm_live_last4 does not match account_id last 4",
            )

    # Idempotency.
    cached = await sess.idempotency_lookup(body.client_req_id)
    if cached is not None:
        return {**cached, "idempotent_hit": True}

    # Audit: pre
    sess.audit.append(
        phase="pre",
        client_req_id=body.client_req_id,
        account=body.account,
        account_id=acc.account_id,
        code=body.code,
        side=body.side,
        volume=body.volume,
        price=body.price,
        type=body.type,
        confirm_live_last4=body.confirm_live_last4,
    )

    # Ensure trader is logged in BEFORE risk check (so risk can query asset/positions).
    try:
        handle = await sess.get_trader(body.account)
    except Exception as e:
        sess.audit.append(
            phase="post",
            client_req_id=body.client_req_id,
            status="error",
            error=f"login failed: {e}",
        )
        raise HTTPException(status_code=500, detail=f"trader login failed: {e}")

    # Risk check (Phase 2). check_order may issue blocking xttrader/xtdata
    # calls for snapshot refresh and last-price lookup, so dispatch to a
    # worker thread to keep the event loop responsive.
    decision = await _xt_call(
        sess.risk.check_order,
        body.account, body.side, body.code, body.volume, body.price, body.type,
        timeout=XT_TIMEOUT_QUERY, label="risk.check_order",
    )
    sess.audit.append(
        phase="risk_check",
        client_req_id=body.client_req_id,
        account=body.account,
        side=body.side,
        code=body.code,
        volume=body.volume,
        price=body.price,
        type=body.type,
        allow=decision.allow,
        reject_code=decision.reject_code,
        reject_detail=decision.reject_detail,
    )
    if not decision.allow:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "risk_reject",
                "code": decision.reject_code,
                "message": decision.reject_detail,
            },
        )

    # Submit
    try:
        result = await _xt_call(
            xttrader_adapter.order_stock,
            handle.trader, handle.acc, body.code, body.side,
            body.volume, body.price, body.type,
            timeout=XT_TIMEOUT_SUBMIT, label="order_stock",
        )
    except HTTPException:
        # Timeout: audit and re-raise as 503 so caller knows the submit is
        # in limbo (xtquant may still have accepted it). Status-watching
        # subscribers should reconcile via /trade/orders.
        sess.audit.append(
            phase="post",
            client_req_id=body.client_req_id,
            status="error",
            error="order_stock timed out",
        )
        raise
    except Exception as e:
        sess.audit.append(
            phase="post",
            client_req_id=body.client_req_id,
            status="error",
            error=str(e),
        )
        raise HTTPException(status_code=500, detail=f"order_stock failed: {e}")

    seq = result.get("seq", 0)
    status = "ok" if seq > 0 else "rejected"
    response = {
        "client_req_id": body.client_req_id,
        "seq": seq,
        "status": status,
        "order_id": seq if seq > 0 else None,
    }
    # Update risk pending / frequency window (Phase 2)
    if seq > 0:
        sess.risk.record_accepted(
            body.account, body.side, body.code, body.volume, body.price, int(seq),
        )
    sess.audit.append(
        phase="post",
        client_req_id=body.client_req_id,
        status=status,
        seq=seq,
        order_id=response["order_id"],
    )
    await sess.idempotency_store(body.client_req_id, response)
    return response


@router.post("/cancel")
async def cancel_order(request: Request, body: CancelRequest):
    sess = _session(request)
    _require_account(sess, body.account)

    cached = await sess.idempotency_lookup(body.client_req_id)
    if cached is not None:
        return {**cached, "idempotent_hit": True}

    sess.audit.append(
        phase="pre",
        client_req_id=body.client_req_id,
        action="cancel",
        account=body.account,
        order_id=body.order_id,
    )
    try:
        handle = await sess.get_trader(body.account)
        result = await _xt_call(
            xttrader_adapter.cancel_order_stock,
            handle.trader, handle.acc, body.order_id,
            timeout=XT_TIMEOUT_SUBMIT, label="cancel_order_stock",
        )
    except HTTPException:
        sess.audit.append(
            phase="post",
            client_req_id=body.client_req_id,
            action="cancel",
            status="error",
            error="cancel_order_stock timed out",
        )
        raise
    except Exception as e:
        sess.audit.append(
            phase="post",
            client_req_id=body.client_req_id,
            action="cancel",
            status="error",
            error=str(e),
        )
        raise HTTPException(status_code=500, detail=f"cancel_order_stock failed: {e}")

    seq = result.get("seq", 0)
    status = "ok" if seq >= 0 else "rejected"
    response = {
        "client_req_id": body.client_req_id,
        "seq": seq,
        "status": status,
    }
    sess.audit.append(
        phase="post",
        client_req_id=body.client_req_id,
        action="cancel",
        status=status,
        seq=seq,
    )
    await sess.idempotency_store(body.client_req_id, response)
    return response


def _require_account(sess, name: str) -> AccountConfig:
    try:
        return sess.get_account(name)
    except KeyError:
        raise HTTPException(
            status_code=400, detail=f"unknown account: {name!r} (not in whitelist)"
        )
