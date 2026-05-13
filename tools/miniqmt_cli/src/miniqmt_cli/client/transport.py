"""HTTP transport used by CLI commands to talk to the daemon."""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator, Optional

import click
import httpx

from miniqmt_cli.client.errors import SubmitIndeterminate
from miniqmt_cli.client_config import ClientConfig

# Daemon returns 504 with a structured detail dict for state-changing
# operations that timed out. These error codes are the contract -- the
# transport translates them into a typed exception so the CLI exits with
# a distinct code instead of a generic "daemon error 504".
_INDETERMINATE_ERRORS = {"submit_indeterminate", "cancel_indeterminate"}


def _render_detail(detail: Any) -> str:
    """Pretty-print a daemon error detail. Dicts get the human-readable
    `message` extracted to the front; the rest stays as compact info."""
    if isinstance(detail, dict):
        msg = detail.get("message")
        if msg:
            rest = {k: v for k, v in detail.items() if k != "message"}
            if rest:
                return f"{msg} ({rest})"
            return str(msg)
    return str(detail)


class Transport:
    def __init__(self, cfg: ClientConfig):
        self.cfg = cfg
        self.base_url = cfg.resolve_url().rstrip("/")

    def _handle_status(self, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        detail: Any = None
        try:
            body = resp.json()
            if isinstance(body, dict):
                detail = body.get("detail") or body
            else:
                detail = body
        except Exception:
            detail = resp.text
        # 504 with a structured indeterminate-state response: surface as a
        # typed exit code so retrying scripts can tell "unknown state" from
        # "we know it failed" -- the difference matters on a real account.
        if (
            resp.status_code == 504
            and isinstance(detail, dict)
            and detail.get("error") in _INDETERMINATE_ERRORS
        ):
            raise SubmitIndeterminate(detail["error"], detail)
        if resp.status_code >= 500:
            raise click.ClickException(
                f"daemon error {resp.status_code}: {_render_detail(detail)}"
            )
        raise click.ClickException(_render_detail(detail))

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base_url + path
        try:
            resp = httpx.get(url, params=params, timeout=30.0)
        except httpx.HTTPError as e:
            raise click.ClickException(f"cannot reach daemon at {self.base_url}: {e}")
        self._handle_status(resp)
        return resp.json()

    def post(self, path: str, body: Dict[str, Any]) -> Any:
        url = self.base_url + path
        try:
            resp = httpx.post(url, json=body, timeout=60.0)
        except httpx.HTTPError as e:
            raise click.ClickException(f"cannot reach daemon at {self.base_url}: {e}")
        self._handle_status(resp)
        return resp.json()

    def stream(self, path: str, params: Optional[Dict[str, Any]] = None) -> Iterator[dict]:
        url = self.base_url + path
        try:
            with httpx.stream("GET", url, params=params, timeout=None) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    self._handle_status(resp)
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if not payload:
                        continue
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        continue
        except httpx.HTTPError as e:
            raise click.ClickException(f"cannot reach daemon at {self.base_url}: {e}")


def make_transport(ctx: click.Context) -> Transport:
    cfg: ClientConfig = ctx.obj["client_cfg"]
    return Transport(cfg)
