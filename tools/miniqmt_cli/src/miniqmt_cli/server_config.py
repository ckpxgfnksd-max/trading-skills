"""Server-side config for miniqmt-cli. Reads ~/.miniqmt_cli/server.toml."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional


@dataclass
class RiskConfig:
    enabled: bool = True
    max_daily_loss: float = 50000.0
    max_position_pct: float = 30.0
    max_orders_per_minute: int = 10
    max_positions: int = 10


@dataclass
class AccountConfig:
    name: str
    account_id: str
    account_type: str = "STOCK"
    requires_confirm_live: bool = False
    risk: Optional[RiskConfig] = None

    @property
    def last4(self) -> str:
        return self.account_id[-4:]

    def masked_id(self) -> str:
        if len(self.account_id) <= 4:
            return "*" * len(self.account_id)
        return "*" * (len(self.account_id) - 4) + self.account_id[-4:]


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    qmt_path: str = ""
    userdata_mini_path: str = ""  # optional; defaults to qmt_path/userdata_mini
    session_id: int = 0  # 0 means "use os.getpid()"
    accounts: Dict[str, AccountConfig] = field(default_factory=dict)
    audit_log_path: str = "~/.miniqmt_cli/orders.jsonl"
    idempotency_ttl_seconds: int = 300
    audit_warn_size_bytes: int = 100 * 1024 * 1024
    risk: RiskConfig = field(default_factory=RiskConfig)
    risk_state_path: str = "~/.miniqmt_cli/risk_state.json"

    def resolved_session_id(self) -> int:
        return self.session_id if self.session_id else os.getpid()

    def resolved_audit_log_path(self) -> Path:
        return Path(self.audit_log_path).expanduser()

    def resolved_userdata_mini_path(self) -> str:
        """Path xttrader.XtQuantTrader() needs: the userdata_mini dir.
        Defaults to qmt_path/userdata_mini (the standard miniQMT layout)."""
        if self.userdata_mini_path:
            return self.userdata_mini_path
        return os.path.join(self.qmt_path, "userdata_mini")

    def effective_risk(self, account_name: str) -> RiskConfig:
        acc = self.accounts.get(account_name)
        if acc is None or acc.risk is None:
            return self.risk
        return acc.risk

    def resolved_risk_state_path(self) -> Path:
        return Path(self.risk_state_path).expanduser()


def _config_path(override: Optional[str] = None) -> Path:
    if override:
        return Path(override).expanduser()
    local = Path.cwd() / "miniqmt_cli.server.toml"
    if local.exists():
        return local
    return Path.home() / ".miniqmt_cli" / "server.toml"


def _parse_risk(raw: dict, base: RiskConfig) -> RiskConfig:
    """Return a new RiskConfig with only fields present in `raw` overlaid on `base`.

    Absent fields inherit `base` values; this is how per-account overrides
    express field-level merge semantics.
    """
    return RiskConfig(
        enabled=bool(raw["enabled"]) if "enabled" in raw else base.enabled,
        max_daily_loss=float(raw["max_daily_loss"]) if "max_daily_loss" in raw else base.max_daily_loss,
        max_position_pct=float(raw["max_position_pct"]) if "max_position_pct" in raw else base.max_position_pct,
        max_orders_per_minute=int(raw["max_orders_per_minute"]) if "max_orders_per_minute" in raw else base.max_orders_per_minute,
        max_positions=int(raw["max_positions"]) if "max_positions" in raw else base.max_positions,
    )


def load_server_config(path_override: Optional[str] = None) -> ServerConfig:
    cfg = ServerConfig()
    path = _config_path(path_override)
    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
        server = data.get("server", {}) or {}
        cfg.host = server.get("host", cfg.host)
        cfg.port = int(server.get("port", cfg.port))
        cfg.qmt_path = server.get("qmt_path", cfg.qmt_path)
        cfg.userdata_mini_path = server.get("userdata_mini_path", cfg.userdata_mini_path)
        cfg.session_id = int(server.get("session_id", cfg.session_id))

        risk_raw = data.get("risk", {}) or {}
        cfg.risk = _parse_risk(risk_raw, RiskConfig())

        accounts_raw = data.get("accounts", {}) or {}
        for name, raw in accounts_raw.items():
            if not isinstance(raw, dict):
                continue
            acc_risk_raw = raw.get("risk")
            acc_risk = _parse_risk(acc_risk_raw, cfg.risk) if acc_risk_raw else None
            cfg.accounts[name] = AccountConfig(
                name=name,
                account_id=str(raw.get("account_id", "")),
                account_type=str(raw.get("account_type", "STOCK")),
                requires_confirm_live=bool(raw.get("requires_confirm_live", False)),
                risk=acc_risk,
            )

        audit = data.get("audit", {}) or {}
        cfg.audit_log_path = audit.get("log_path", cfg.audit_log_path)
        if "state_path" in risk_raw:
            cfg.risk_state_path = str(risk_raw["state_path"])

    # env overrides
    env_host = os.environ.get("MINIQMT_CLI_SERVER_HOST")
    if env_host:
        cfg.host = env_host
    env_port = os.environ.get("MINIQMT_CLI_SERVER_PORT")
    if env_port:
        cfg.port = int(env_port)
    env_qmt = os.environ.get("MINIQMT_CLI_SERVER_QMT_PATH")
    if env_qmt:
        cfg.qmt_path = env_qmt
    return cfg


TEMPLATE = """\
[server]
host = "127.0.0.1"
port = 8765
qmt_path = "C:/国金QMT交易端/userdata_mini"
# session_id = 123456  # omit to use os.getpid()

[accounts.sim]
account_id = "55001234"
account_type = "STOCK"

# [accounts.live]
# account_id = "88881234"
# account_type = "STOCK"
# requires_confirm_live = true

[audit]
log_path = "~/.miniqmt_cli/orders.jsonl"
"""


def write_template(path: Optional[Path] = None) -> Path:
    target = path or (Path.home() / ".miniqmt_cli" / "server.toml")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        # TOML spec requires UTF-8; be explicit so Windows doesn't fall back
        # to GBK / CP936 and break tomllib on subsequent reads.
        target.write_text(TEMPLATE, encoding="utf-8")
    return target
