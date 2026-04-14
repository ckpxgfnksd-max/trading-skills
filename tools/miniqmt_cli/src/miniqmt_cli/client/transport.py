"""HTTP transport used by CLI commands to talk to the daemon."""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator, Optional

import click
import httpx

from miniqmt_cli.client_config import ClientConfig


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
        if resp.status_code >= 500:
            raise click.ClickException(
                f"daemon error {resp.status_code}: {detail}"
            )
        raise click.ClickException(f"{detail}")

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
