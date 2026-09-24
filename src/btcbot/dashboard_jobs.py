"""Bounded, offline background backtests for the monitoring dashboard.

Progress counts completed sensitivity scenarios, not guessed replay percentages. Every
scenario in a job reads the same SQLite transaction. Workers never write to recordings,
access a network, or place an order. Completed jobs are reused only while the recording,
its WAL, and the configuration still match their submission-time file signatures.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from btcbot.backtest import run_backtest
from btcbot.config import load_config
from btcbot.paper_broker import QueueAssumption


class BacktestQueueFull(Exception):
    """The bounded worker pool has no room for another distinct run."""


def _signature(path: Path, *, optional: bool = False) -> tuple[Any, ...]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        if optional:
            return (str(path), None)
        raise
    # ctime is deliberately excluded: opening a WAL-mode database read-only can touch a
    # sidecar file's ctime alone (e.g. creating/locking "-shm") without changing its
    # content, which would falsely invalidate an unchanged completed job.
    return (str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _inputs(db_path: Path, config_path: Path) -> tuple[Any, ...]:
    # The main database mtime alone misses commits while a recorder has an open WAL.
    return (
        _signature(db_path), _signature(Path(str(db_path) + "-wal"), optional=True),
        _signature(config_path),
    )


def _scenarios(queue: str, maker_fee_multiplier: str) -> tuple[tuple[QueueAssumption, Decimal], ...]:
    if queue not in ("both", "optimistic", "pessimistic"):
        raise ValueError("queue must be 'optimistic', 'pessimistic', or 'both'")
    queues = tuple(QueueAssumption) if queue == "both" else (QueueAssumption(queue),)
    if maker_fee_multiplier == "both":
        fees = (Decimal("0"), Decimal("0.25"))
    else:
        try:
            fee = Decimal(str(maker_fee_multiplier))
        except (InvalidOperation, ValueError):
            raise ValueError("maker_fee_multiplier must be 'both' or a finite non-negative number") from None
        if not fee.is_finite() or fee < 0:
            raise ValueError("maker_fee_multiplier must be 'both' or a finite non-negative number")
        fees = (fee,)
    return tuple((q, fee) for q in queues for fee in fees)


@dataclass
class _Job:
    db_path: Path
    config_path: Path
    scenarios: tuple[tuple[QueueAssumption, Decimal], ...]
    input_signature: tuple[Any, ...]
    key: tuple[Any, ...]
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = "queued"
    label: str = "Waiting for a backtest worker"
    created: float = field(default_factory=time.monotonic)
    started: float | None = None
    finished: float | None = None
    reports: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def snapshot(self, *, cached: bool = False) -> dict[str, Any]:
        now = self.finished if self.finished is not None else time.monotonic()
        return {
            "id": self.id, "state": self.state, "db": self.db_path.name,
            "done": len(self.reports), "total": len(self.scenarios), "label": self.label,
            "elapsed_sec": max(0.0, now - self.created),
            "run_elapsed_sec": max(0.0, now - self.started) if self.started is not None else 0.0,
            "queued_sec": max(0.0, (self.started if self.started is not None else now) - self.created),
            "reports": [dict(report) for report in self.reports], "error": self.error, "cached": cached,
        }


class BacktestJobs:
    """A small daemon worker pool with bounded pending jobs and completed history.

    ``submit`` and ``get`` return detached dictionaries compatible with webui's JSON
    encoder. Invalid assumptions raise ``ValueError`` before consuming a worker slot.
    Repeated submissions share an in-flight job or an unchanged completed result;
    ``cached`` is true on the submission response for the latter case only.

    ``close`` rejects new work and lets accepted work finish. Daemon workers avoid
    holding up dashboard process exit; ``close(wait=True)`` joins them for tests.
    """

    def __init__(self, *, max_workers: int = 1, max_pending: int = 2, max_history: int = 12) -> None:
        if max_workers < 1 or max_pending < 0 or max_history < 1:
            raise ValueError("max_workers/max_history must be positive and max_pending non-negative")
        self._max_workers = max_workers
        self._capacity = max_workers + max_pending
        self._max_history = max_history
        self._condition = threading.Condition()
        self._jobs: OrderedDict[str, _Job] = OrderedDict()
        self._by_key: dict[tuple[Any, ...], str] = {}
        self._pending: deque[_Job] = deque()
        self._workers: list[threading.Thread] = []
        self._closed = False

    def submit(
        self, db_path: Path, config_path: Path, *, queue: str = "both", maker_fee_multiplier: str = "both",
    ) -> dict[str, Any]:
        scenarios = _scenarios(queue, maker_fee_multiplier)
        db_path, config_path = Path(db_path).resolve(), Path(config_path).resolve()
        signature = _inputs(db_path, config_path)
        key = (signature, scenarios)
        with self._condition:
            if self._closed:
                raise ValueError("the backtest worker pool is closed")
            existing = self._jobs.get(self._by_key.get(key, ""))
            if existing is not None and existing.state in ("queued", "running", "completed"):
                return existing.snapshot(cached=existing.state == "completed")
            if sum(job.state in ("queued", "running") for job in self._jobs.values()) >= self._capacity:
                raise BacktestQueueFull("Backtest queue is full; wait for a running job to finish.")
            job = _Job(db_path, config_path, scenarios, signature, key)
            self._jobs[job.id] = job
            self._by_key[key] = job.id
            self._pending.append(job)
            if not self._workers:
                for index in range(self._max_workers):
                    worker = threading.Thread(target=self._worker, daemon=True, name=f"dashboard-backtest-{index}")
                    self._workers.append(worker)
                    worker.start()
            self._condition.notify_all()
            return job.snapshot()

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._condition:
            job = self._jobs.get(job_id)
            return job.snapshot() if job is not None else None

    def close(self, *, wait: bool = False) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if wait:
            for worker in self._workers:
                worker.join()

    def _worker(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: bool(self._pending) or self._closed)
                if not self._pending:
                    return
                job = self._pending.popleft()
                job.state, job.started, job.label = "running", time.monotonic(), "Loading configuration and recorded data"
            self._run(job)

    def _run(self, job: _Job) -> None:
        cacheable = False
        error = None
        try:
            config = load_config(job.config_path)
            conn = sqlite3.connect(job.db_path.as_uri() + "?mode=ro", uri=True, timeout=5)
            try:
                conn.execute("BEGIN")
                for index, (queue, fee) in enumerate(job.scenarios, 1):
                    with self._condition:
                        job.label = f"Scenario {index}/{len(job.scenarios)}: {queue.value}, maker fee multiplier {fee}"
                    report = run_backtest(conn, config, queue_assumption=queue, maker_fee_multiplier=fee)
                    with self._condition:
                        job.reports.append(asdict(report))
            finally:
                conn.close()
            try:
                cacheable = _inputs(job.db_path, job.config_path) == job.input_signature
            except OSError:
                pass  # Results remain usable even if a source was moved while the job ran.
        except Exception as exc:  # A background failure must become visible and release capacity.
            error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._condition:
                job.state = "error" if error is not None else "completed"
                job.label = "Backtest failed" if error is not None else "All sensitivity scenarios completed"
                job.error = error
                job.finished = time.monotonic()
                if not cacheable and self._by_key.get(job.key) == job.id:
                    self._by_key.pop(job.key, None)
                self._prune()

    def _prune(self) -> None:
        completed = [job for job in self._jobs.values() if job.state in ("completed", "error")]
        for old in completed[:max(0, len(completed) - self._max_history)]:
            self._jobs.pop(old.id)
            if self._by_key.get(old.key) == old.id:
                self._by_key.pop(old.key)
