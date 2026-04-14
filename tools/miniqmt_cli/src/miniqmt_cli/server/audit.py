"""Append-only audit log for order requests."""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger(__name__)


class AuditLog:
    def __init__(self, path: Path, warn_size_bytes: int = 100 * 1024 * 1024):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.warn_size_bytes = warn_size_bytes
        self._lock = threading.Lock()

    @staticmethod
    def _ts() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def append(self, **record: Any) -> None:
        record.setdefault("ts", self._ts())
        line = json.dumps(record, ensure_ascii=False, sort_keys=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except (OSError, AttributeError):
                    pass
            try:
                size = self.path.stat().st_size
                if size > self.warn_size_bytes:
                    log.warning(
                        "audit log %s size %d exceeds %d bytes; consider rotating",
                        self.path, size, self.warn_size_bytes,
                    )
            except OSError:
                pass
