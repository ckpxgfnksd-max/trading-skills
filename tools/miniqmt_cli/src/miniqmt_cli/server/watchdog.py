"""Event-loop watchdog.

Past production incidents: the daemon hung for hours while TCP listening but
the asyncio event loop was wedged (a sync xtquant call blocked the loop,
or an xtquant C-extension wedged the GIL). No exceptions were raised; the
process looked healthy from outside but served no requests. By the time
anyone noticed, the only forensic evidence was lost.

This watchdog runs in a dedicated daemon thread. It periodically schedules
a heartbeat callback onto the event loop via call_soon_threadsafe. If the
loop is healthy the callback runs and updates a timestamp. If the loop has
not advanced its timestamp for `hang_timeout_seconds`, we conclude the loop
is wedged: dump every thread's Python stack to a hang-{ts}.log file beside
the daemon log, then os._exit(1) so an external supervisor can restart.

Why os._exit and not sys.exit: the loop is wedged, so a clean shutdown
would itself hang. os._exit bypasses Python's atexit/finalize machinery.
"""
from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class LoopWatchdog:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        hang_dump_dir: Path,
        heartbeat_interval_seconds: float = 10.0,
        hang_timeout_seconds: float = 60.0,
    ) -> None:
        self._loop = loop
        self._hang_dump_dir = hang_dump_dir
        self._heartbeat_interval = heartbeat_interval_seconds
        self._hang_timeout = hang_timeout_seconds
        self._last_heartbeat = time.monotonic()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._heartbeat_lock = threading.Lock()

    def _on_heartbeat(self) -> None:
        # Runs on the event loop thread.
        with self._heartbeat_lock:
            self._last_heartbeat = time.monotonic()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._loop.call_soon_threadsafe(self._on_heartbeat)
            except RuntimeError:
                # Loop closed — daemon shutting down.
                return
            # Wait one heartbeat interval, then check.
            if self._stop.wait(self._heartbeat_interval):
                return
            with self._heartbeat_lock:
                age = time.monotonic() - self._last_heartbeat
            if age > self._hang_timeout:
                self._on_hang(age)
                return

    def _on_hang(self, age: float) -> None:
        ts = time.strftime("%Y%m%dT%H%M%S")
        dump_path = self._hang_dump_dir / f"daemon-hang-{ts}.log"
        try:
            self._hang_dump_dir.mkdir(parents=True, exist_ok=True)
            with open(dump_path, "w", encoding="utf-8") as f:
                f.write(
                    f"event-loop unresponsive for {age:.1f}s "
                    f"(threshold {self._hang_timeout:.1f}s)\n"
                )
                f.write(f"timestamp: {ts}\n")
                f.write(f"pid: {os.getpid()}\n\n")
                # faulthandler gives a clean per-thread Python stack trace,
                # including the GIL-holding thread if it's in Python.
                faulthandler.dump_traceback(file=f, all_threads=True)
                f.write("\n--- traceback module dump ---\n")
                for thread_id, frame in sys._current_frames().items():
                    f.write(f"\nThread {thread_id}:\n")
                    traceback.print_stack(frame, file=f)
            log.critical(
                "event loop hung for %.1fs; stack dumped to %s; exiting",
                age, dump_path,
            )
        except Exception as e:
            # Even logging may be wedged; best effort.
            try:
                sys.stderr.write(f"watchdog: hang dump failed: {e}\n")
            except Exception:
                pass
        # os._exit so supervisor can restart; sys.exit would itself hang.
        os._exit(1)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="loop-watchdog", daemon=True,
        )
        self._thread.start()
        log.info(
            "watchdog started: heartbeat=%.1fs hang_timeout=%.1fs dump_dir=%s",
            self._heartbeat_interval, self._hang_timeout, self._hang_dump_dir,
        )

    def stop(self) -> None:
        self._stop.set()
