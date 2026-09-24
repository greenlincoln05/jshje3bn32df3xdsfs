"""A PID-file lock so two instances of the same live command (``btcbot paper``/``btcbot demo``) never run
against the same data directory at once.

Confirmed in production (2026-09-23): two ``btcbot demo`` processes running concurrently against the same
Kalshi DEMO account independently decided to enter the same window (``KXBTC15M-26SEP231430-30``), each
placed its own real order (different order ids, prices and sizes), and each got partially filled -- neither
process's local ``RiskManager``/bankroll ever learned about the other's order, so each one's own PnL/exposure
bookkeeping silently diverged from the real account state. Two ``btcbot paper`` processes don't share an
external account, but running two at once still produces two independent, interleaved-looking ledgers in the
same data directory -- confusing, and exactly what "the PnL looks weird" turned out to mean here too. This
lock stops the mistake at the source instead of relying on nobody ever starting a second instance by hand.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


class AlreadyRunningError(Exception):
    """Another live process already holds this lock."""


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_lock(lock_path: Path, *, label: str) -> None:
    """Claim ``lock_path`` for this process, or raise :class:`AlreadyRunningError` if a live process
    already holds it. A lock file left behind by a process that crashed or was killed (so its recorded PID
    is no longer running) is stale and gets silently reclaimed -- it never blocks a real restart."""
    if lock_path.exists():
        pid = None
        try:
            held = json.loads(lock_path.read_text())
            pid, started_at = held["pid"], held["started_at"]
        except (OSError, ValueError, KeyError):
            started_at = "unknown"
        if pid is not None and _pid_is_alive(pid):
            raise AlreadyRunningError(
                f"{label} is already running (PID {pid}, started {started_at}) using {lock_path} -- "
                f"stop it first, or delete {lock_path} if you're sure it isn't actually running."
            )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()}))


def release_lock(lock_path: Path) -> None:
    """Best-effort: never raises (so calling this from a ``finally`` block can never mask whatever error
    actually triggered shutdown), and only removes the lock if THIS process is still the one holding it --
    a lock already reclaimed by a newer process (or never actually acquired by this one) is left alone."""
    try:
        if lock_path.exists() and json.loads(lock_path.read_text()).get("pid") == os.getpid():
            lock_path.unlink()
    except (OSError, ValueError):
        pass
