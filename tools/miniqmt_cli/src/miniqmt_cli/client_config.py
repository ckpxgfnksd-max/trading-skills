"""Client-side config for miniqmt-cli. Reads ~/.miniqmt_cli/client.toml."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_LOCAL_URL = "http://127.0.0.1:8765"


@dataclass
class ClientConfig:
    mode: str = "auto"  # auto | local | remote
    server_url: Optional[str] = None
    output_format: str = "table"

    def resolve_url(self) -> str:
        if self.mode == "local":
            return DEFAULT_LOCAL_URL
        if self.mode == "remote":
            if not self.server_url:
                raise RuntimeError(
                    "client.mode = 'remote' but client.server_url is not set"
                )
            return self.server_url
        # auto
        return self.server_url or DEFAULT_LOCAL_URL


def _config_path(override: Optional[str] = None) -> Path:
    if override:
        return Path(override).expanduser()
    local = Path.cwd() / "miniqmt_cli.client.toml"
    if local.exists():
        return local
    return Path.home() / ".miniqmt_cli" / "client.toml"


def load_client_config(path_override: Optional[str] = None) -> ClientConfig:
    cfg = ClientConfig()
    path = _config_path(path_override)
    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
        client = data.get("client", {}) or {}
        cfg.mode = client.get("mode", cfg.mode)
        cfg.server_url = client.get("server_url", cfg.server_url)
        output = client.get("output", {}) or {}
        cfg.output_format = output.get("format", cfg.output_format)

    # env overrides
    env_mode = os.environ.get("MINIQMT_CLI_MODE")
    if env_mode:
        cfg.mode = env_mode
    env_url = os.environ.get("MINIQMT_CLI_SERVER_URL")
    if env_url:
        cfg.server_url = env_url
    env_fmt = os.environ.get("MINIQMT_CLI_FORMAT")
    if env_fmt:
        cfg.output_format = env_fmt
    return cfg


TEMPLATE = """\
[client]
mode = "auto"                         # auto | local | remote
server_url = "http://127.0.0.1:8765"  # required when mode = remote

[client.output]
format = "table"
"""


def write_template(path: Optional[Path] = None) -> Path:
    target = path or (Path.home() / ".miniqmt_cli" / "client.toml")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(TEMPLATE)
    return target
