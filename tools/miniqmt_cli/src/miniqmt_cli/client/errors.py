"""CLI-side error helpers."""
from __future__ import annotations

import click

EXIT_GENERIC = 1
EXIT_BROKER = 2
EXIT_GUARD = 3
EXIT_RISK = 4
EXIT_INDETERMINATE = 5


class GuardExit(click.ClickException):
    """Exits with code 3: a safety guard refused to proceed."""

    exit_code = EXIT_GUARD

    def __init__(self, message: str):
        super().__init__(message)


class BrokerReject(click.ClickException):
    exit_code = EXIT_BROKER

    def __init__(self, message: str):
        super().__init__(message)


class RiskReject(click.ClickException):
    """Exits with code 4: daemon-side risk layer refused the action."""

    exit_code = EXIT_RISK

    def __init__(self, code: str, message: str):
        super().__init__(f"risk_reject [{code}] {message}")
        self.code = code


class SubmitIndeterminate(click.ClickException):
    """Exits with code 5: order or cancel submission timed out and the
    broker state is unknown. Retrying is unsafe -- the caller MUST
    reconcile against /trade/orders before deciding next action.

    A separate exit code (not just 1) lets scripts and supervising agents
    distinguish 'we know it failed' from 'we don't know whether it
    succeeded' -- the difference matters when the action is state-changing
    on a real money account.
    """

    exit_code = EXIT_INDETERMINATE

    def __init__(self, action: str, detail: dict):
        self.action = action
        self.detail = detail
        msg = (
            f"{action} INDETERMINATE -- DO NOT RETRY. "
            f"client_req_id={detail.get('client_req_id')}. "
            f"Reconcile via {detail.get('reconcile_via', '/trade/orders')}. "
            f"Daemon said: {detail.get('message', '')}"
        )
        super().__init__(msg)
