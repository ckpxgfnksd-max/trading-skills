"""CLI-side error helpers."""
from __future__ import annotations

import click

EXIT_GENERIC = 1
EXIT_BROKER = 2
EXIT_GUARD = 3


class GuardExit(click.ClickException):
    """Exits with code 3: a safety guard refused to proceed."""

    exit_code = EXIT_GUARD

    def __init__(self, message: str):
        super().__init__(message)


class BrokerReject(click.ClickException):
    exit_code = EXIT_BROKER

    def __init__(self, message: str):
        super().__init__(message)
