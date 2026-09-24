"""Dashboard support for the BTC perpetual PAPER backtest (docs/research/perps-paper.md, ``btcbot
perp-backtest``). Read-only over data already on disk, plus a small bounded worker pool that runs a fresh
backtest from the dashboard the same way the CLI does: offline, no network, no key, and no perps order code
exists anywhere in this repo (see :mod:`btcbot.perp_paper`/:mod:`btcbot.perp_backtest`). A completed run is
saved under the same ``perp-backtest-<timestamp>.json`` name the CLI itself writes, so it joins
:func:`list_perp_reports` for next time -- the dashboard and the CLI share one on-disk report format.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from btcbot.perp_backtest import (
    MAX_PAPER_LEVERAGE, STRATEGIES, PerpBacktestError, describe_source, load_bars, run_backtest,
)
from btcbot.perp_paper import PerpPaperError, PerpSpec

REPORT_PREFIX = "perp-backtest-"


class PerpQueueFull(Exception):
    """The bounded worker pool has no room for another run."""


# --------------------------------------------------------------------------- read-only views


def list_perp_databases(data_dir: Path) -> list[dict[str, Any]]:
    """Local databases with BTC bars a perp backtest could run on: a ``download-history`` database's
    Coinbase ``spot_candles``, or a ``download-polymarket-history`` database's Binance ``btc_klines_1s``.
    Cheap -- checks table presence and a single row, never loads the bars themselves."""
    if not data_dir.is_dir():
        return []
    entries = []
    for path in sorted(data_dir.glob("*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
        except sqlite3.OperationalError:
            continue
        try:
            source = describe_source(conn)
        except sqlite3.DatabaseError:
            source = None
        finally:
            conn.close()
        if source is not None:
            stat = path.stat()
            entries.append({"name": path.name, "source": source, "size_bytes": stat.st_size, "modified_ts": stat.st_mtime})
    return entries


def _resolve_report(data_dir: Path, name: str) -> Path | None:
    """Only a bare filename that is one of ``data_dir/research``'s own ``perp-backtest-*.json`` reports is
    accepted -- the same traversal guard :func:`btcbot.webui._resolve_db` uses for databases -- so a
    ``name`` query parameter can't reach another file in that directory (e.g. a Polymarket reaction
    report)."""
    if name != Path(name).name or not name.startswith(REPORT_PREFIX) or not name.endswith(".json"):
        return None
    candidate = data_dir / "research" / name
    return candidate if candidate.is_file() else None


def list_perp_reports(data_dir: Path) -> list[dict[str, Any]]:
    research = data_dir / "research"
    if not research.is_dir():
        return []
    entries = []
    for path in sorted(research.glob(f"{REPORT_PREFIX}*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # a partially written or unrelated file: skip rather than fail the whole listing
        entries.append({
            "name": path.name, "modified_ts": path.stat().st_mtime,
            "source": report.get("source", ""), "bars": report.get("bars", 0),
            "train_end": report.get("train_end", ""),
        })
    return entries


def read_perp_report(data_dir: Path, name: str) -> dict[str, Any]:
    path = _resolve_report(data_dir, name)
    if path is None:
        raise FileNotFoundError(name)
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- background runs


def _decimal_list(payload: dict[str, Any], key: str, default: list[str]) -> list[Decimal]:
    raw = payload.get(key) or default
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{key} must be a non-empty list")
    try:
        values = [Decimal(str(v)) for v in raw]
    except InvalidOperation:
        raise ValueError(f"{key} must be numbers") from None
    if not all(v.is_finite() for v in values):
        raise ValueError(f"{key} must be finite numbers")
    return values


def _params_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    strategies = payload.get("strategies") or list(STRATEGIES)
    if not isinstance(strategies, list) or not strategies:
        raise ValueError("choose at least one strategy")
    unknown = set(strategies) - set(STRATEGIES)
    if unknown:
        raise ValueError(f"unknown strategy: {', '.join(sorted(unknown))}")
    leverages = _decimal_list(payload, "leverages", ["1", "2"])
    for lev in leverages:
        if not Decimal(0) < lev <= MAX_PAPER_LEVERAGE:
            raise ValueError(f"leverage {lev} outside (0, {MAX_PAPER_LEVERAGE}]")
    fundings = _decimal_list(payload, "fundings", ["0", "0.0001"])
    try:
        account = Decimal(str(payload.get("account", "500")))
        split = float(payload.get("split", 0.7))
        fee_bps = Decimal(str(payload.get("fee_bps", "12")))
        slippage_bps = Decimal(str(payload.get("slippage_bps", "1")))
        maintenance_frac = Decimal(str(payload.get("maintenance_frac", "0.9")))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("account/split/fee_bps/slippage_bps/maintenance_frac must be numbers") from None
    if not account.is_finite() or account <= 0:
        raise ValueError("account must be a positive number")
    if not 0.2 <= split <= 0.9:
        raise ValueError("split must be between 0.2 and 0.9")
    spec = PerpSpec(taker_fee_bps=fee_bps, half_spread_bps=slippage_bps, maintenance_frac=maintenance_frac)
    return {
        "account_usd": account, "leverages": leverages, "fundings": fundings, "strategies": strategies,
        "train_fraction": split, "spec": spec,
    }


@dataclass
class _PerpJob:
    db_path: Path
    data_dir: Path
    params: dict[str, Any]
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = "queued"
    label: str = "Waiting for a worker"
    error: str | None = None
    report: dict[str, Any] | None = None
    report_path: str | None = None
    created: float = field(default_factory=time.monotonic)
    started: float | None = None
    finished: float | None = None

    def snapshot(self) -> dict[str, Any]:
        now = self.finished if self.finished is not None else time.monotonic()
        return {
            "id": self.id, "state": self.state, "db": self.db_path.name, "label": self.label,
            "error": self.error, "report": self.report, "report_path": self.report_path,
            "elapsed_sec": max(0.0, now - self.created),
        }


class PerpJobs:
    """One background ``btcbot perp-backtest`` run at a time, triggered from the dashboard instead of a
    terminal. Same offline guarantees as the CLI: no network, no key, no perps order code. A completed run
    is written to ``data_dir/research`` under the same name the CLI itself would use, so it shows up in
    :func:`list_perp_reports` too.

    ``close`` rejects new work and lets accepted work finish. Daemon workers avoid holding up dashboard
    process exit; ``close(wait=True)`` joins the worker for tests.
    """

    def __init__(self, *, max_pending: int = 1, max_history: int = 8) -> None:
        self._capacity = 1 + max_pending
        self._max_history = max_history
        self._condition = threading.Condition()
        self._jobs: OrderedDict[str, _PerpJob] = OrderedDict()
        self._pending: deque[_PerpJob] = deque()
        self._worker: threading.Thread | None = None
        self._closed = False

    def submit(self, db_path: Path, data_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
        params = _params_from_payload(payload)  # raises ValueError before any worker slot is consumed
        with self._condition:
            if self._closed:
                raise ValueError("the perp backtest worker pool is closed")
            if sum(job.state in ("queued", "running") for job in self._jobs.values()) >= self._capacity:
                raise PerpQueueFull("A perp backtest is already running; wait for it to finish.")
            job = _PerpJob(Path(db_path), Path(data_dir), params)
            self._jobs[job.id] = job
            self._pending.append(job)
            if self._worker is None:
                self._worker = threading.Thread(target=self._loop, daemon=True, name="dashboard-perp-backtest")
                self._worker.start()
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
        if wait and self._worker is not None:
            self._worker.join()

    def _loop(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: bool(self._pending) or self._closed)
                if not self._pending:
                    return
                job = self._pending.popleft()
                job.state, job.started, job.label = "running", time.monotonic(), "Loading bars"
            self._run(job)

    def _run(self, job: _PerpJob) -> None:
        error = None
        try:
            conn = sqlite3.connect(f"{job.db_path.as_uri()}?mode=ro", uri=True, timeout=5)
            try:
                bars, source = load_bars(conn)
            finally:
                conn.close()
            params = job.params
            with self._condition:
                job.label = (
                    f"Running {len(params['strategies'])} strategies x {len(params['leverages'])} leverages "
                    f"x {len(params['fundings'])} fundings, train+test"
                )
            report = run_backtest(bars, source=source, **params)
            payload = {
                "source": report.source, "bars": report.bars, "train_end": report.train_end,
                "spec": report.spec, "rows": report.rows, "verdicts": report.verdicts,
            }
            out = job.data_dir / "research" / f"{REPORT_PREFIX}{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(payload, indent=2), encoding="ascii")
            with self._condition:
                job.report, job.report_path = payload, str(out)
        except (PerpBacktestError, PerpPaperError, sqlite3.DatabaseError, InvalidOperation, OSError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # a background failure must become visible and release capacity
            error = f"unexpected {type(exc).__name__}: {exc}"
        finally:
            with self._condition:
                job.state = "error" if error is not None else "completed"
                job.label = "Backtest failed" if error is not None else "Done"
                job.error = error
                job.finished = time.monotonic()
                self._prune()

    def _prune(self) -> None:
        completed = [job for job in self._jobs.values() if job.state in ("completed", "error")]
        for old in completed[:max(0, len(completed) - self._max_history)]:
            self._jobs.pop(old.id)
