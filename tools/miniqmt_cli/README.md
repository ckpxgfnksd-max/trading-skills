# miniqmt-cli

Command-line tool for miniQMT / xtquant: market data, account queries, and live trading.

## Modes

- **local** (Windows): daemon + CLI on the same host. CLI talks HTTP to `127.0.0.1:8765`.
- **remote** (macOS): CLI on macOS talks HTTP to a daemon running on Windows. Network reachability (typically an SSH local port-forward) is the operator's responsibility.

## Install

```bash
pip install -e tools/miniqmt_cli
```

On Windows, xtquant is loaded via `sys.path` injection from `server.qmt_path` in `~/.miniqmt_cli/server.toml`. There is no pip package for xtquant.

## Quick start

```bash
# 1. Generate config templates
miniqmt-cli config client init
miniqmt-cli config server init     # Windows only

# 2. Edit ~/.miniqmt_cli/server.toml on Windows: qmt_path, accounts.*
# 3. Edit ~/.miniqmt_cli/client.toml on Mac: server_url

# 4. Start daemon on Windows
miniqmt-cli serve

# 5. From Mac, tunnel to Windows daemon
ssh -N -L 8765:127.0.0.1:8765 user@windows-host

# 6. Use CLI on Mac
miniqmt-cli health
miniqmt-cli tick --code 000001.SZ
miniqmt-cli order buy --account sim --code 000001.SZ --volume 100 --price 12.34
```

See `docs/superpowers/specs/2026-04-14-miniqmt-cli-design.md` for the full design.
