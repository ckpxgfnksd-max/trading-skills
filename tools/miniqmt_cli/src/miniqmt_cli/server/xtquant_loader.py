"""Lazy xtquant loader: injects qmt_path into sys.path before import."""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional

log = logging.getLogger(__name__)

_loaded = False


def load_xtquant(qmt_path: str) -> None:
    """Inject xtquant's bundled site-packages into sys.path and import it.

    Idempotent: subsequent calls are no-ops.
    """
    global _loaded
    if _loaded:
        return
    if not qmt_path:
        raise RuntimeError(
            "server.qmt_path is empty; set it in server.toml to the miniQMT "
            "client install directory"
        )
    site_packages = os.path.join(qmt_path, "bin.x64", "Lib", "site-packages")
    if not os.path.isdir(site_packages):
        raise RuntimeError(
            f"xtquant not found under {site_packages!r}. Check "
            f"[server].qmt_path in server.toml — it must point at the "
            f"miniQMT client install directory that contains "
            f"bin.x64/Lib/site-packages/xtquant."
        )
    if site_packages not in sys.path:
        sys.path.insert(0, site_packages)
    import xtquant.xtdata  # noqa: F401
    import xtquant.xttrader  # noqa: F401
    _loaded = True
    log.info("xtquant loaded from %s", site_packages)


def reset_for_tests() -> None:
    """Only for tests: forget the loaded flag so re-import can be exercised."""
    global _loaded
    _loaded = False
