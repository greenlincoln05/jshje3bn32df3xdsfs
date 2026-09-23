"""Polymarket order-book recorder (``btcbot record-polymarket``): READ-ONLY, a separate venue from Kalshi.

Polls the public Gamma + CLOB APIs (``polymarket_client.py``) for the rolling "Bitcoin Up or Down" series and
writes book snapshots, market metadata and settlements to their own SQLite tables (prefixed ``pm_`` so they can
never be confused with -- or accidentally read by -- any Kalshi-schema code such as ``btcbot.features``, whose
``build_rows`` looks specifically for the Kalshi ``orderbook_snapshots`` table and would correctly skip a
Polymarket database as the wrong schema).

There is no order, cancel or wallet code anywhere in this module, on either the "Up" or "Down" token: only
``PolymarketClient.get_order_book``/``list_recent_updown_events``/``get_event``, all public GETs. This is
research infrastructure only, mirroring ``recorder.py``'s own "record public data, decide what to do with it
later" shape -- nothing here places a bet, and nothing here should be read as a step toward Polymarket order
placement, which is a separate, much larger decision (see the module docstring in ``polymarket_client.py``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from btcbot.models import ParseError
from btcbot.polymarket_client import PolymarketAPIError, PolymarketClient, PolymarketConnectionError, UpdownEvent

log = logging.getLogger("btcbot.polymarket_recorder")

DEFAULT_POLL_INTERVAL_SEC = 2.0
DEFAULT_MAX_CONSECUTIVE_FAILURES = 10
DEFAULT_MAX_DB_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
DEFAULT_MIN_FREE_BYTES = 500 * 1024 * 1024  # 500 MiB

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pm_orderbook_snapshots (
    id INTEGER PRIMARY KEY,
    event_slug TEXT NOT NULL,
    outcome TEXT NOT NULL,          -- "up" | "down"
    token_id TEXT NOT NULL,
    poll_ts TEXT NOT NULL,
    book_json TEXT NOT NULL         -- {"bids": [[price, size], ...], "asks": [...]}, same shape as btcbot.models
);
CREATE INDEX IF NOT EXISTS idx_pm_orderbook_slug_ts ON pm_orderbook_snapshots (event_slug, poll_ts);

CREATE TABLE IF NOT EXISTS pm_market_state (
    id INTEGER PRIMARY KEY,
    event_slug TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    question TEXT NOT NULL,
    up_token_id TEXT NOT NULL,
    down_token_id TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    poll_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pm_settlements (
    id INTEGER PRIMARY KEY,
    event_slug TEXT NOT NULL UNIQUE,
    condition_id TEXT NOT NULL,
    result_up INTEGER NOT NULL,     -- 1 if "Up" won, 0 if "Down" won
    end_time TEXT NOT NULL,
    finalized_poll_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class PolymarketRecorderSummary:
    started_at: datetime
    stopped_at: datetime
    stop_reason: str  # "time_limit" | "kill_file" | "disk_floor" | "db_size_cap" | "repeated_failures" | "cancelled"
    stop_detail: str
    book_polls: int = 0
    market_state_changes: int = 0
    settlements: int = 0
    rollover_gaps: int = 0
    errors: int = 0
    unresolved_settlements: tuple[str, ...] = ()


@dataclass
class _Counters:
    book_polls: int = 0
    market_state_changes: int = 0
    settlements: int = 0
    rollover_gaps: int = 0
    errors: int = 0


class PolymarketRecorder:
    """Polls the public Polymarket "Bitcoin Up or Down" series at ``horizon`` and writes everything to one
    SQLite file. Same stop-condition shape as ``btcbot.recorder.Recorder``: a time limit, a kill-file marker,
    free disk space below a floor, the database reaching a size cap, or too many consecutive request failures."""

    def __init__(
        self,
        client: PolymarketClient,
        *,
        horizon: str = "15m",
        db_path: str | Path,
        kill_file: str | Path = "KILL_PM",
        max_db_bytes: int = DEFAULT_MAX_DB_BYTES,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        disk_free_bytes: Callable[[str], int] = lambda path: shutil.disk_usage(path).free,
    ) -> None:
        if poll_interval_sec <= 0:
            raise ValueError("poll_interval_sec must be positive")
        self._client = client
        self._horizon = horizon
        self._db_path = Path(db_path)
        self._kill_file = Path(kill_file)
        self._max_db_bytes = max_db_bytes
        self._min_free_bytes = min_free_bytes
        self._poll_interval_sec = poll_interval_sec
        self._max_consecutive_failures = max_consecutive_failures
        self._clock = clock
        self._sleep = sleep
        self._disk_free_bytes = disk_free_bytes

        self._db = sqlite3.connect(str(self._db_path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.commit()
        self._db.close()

    # ---- recording

    def _log(self, level: str, event: str, detail: str) -> None:
        getattr(log, level, log.info)("%s: %s", event, detail)
        self._db.execute(
            "INSERT INTO run_log (ts, level, event, detail) VALUES (?, ?, ?, ?)",
            (_iso(self._clock()), level, event, detail),
        )
        self._db.commit()

    def _record_market_state(self, ev: UpdownEvent, poll_ts: datetime) -> None:
        self._db.execute(
            "INSERT INTO pm_market_state (event_slug, condition_id, question, up_token_id, down_token_id,"
            " start_time, end_time, poll_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ev.slug, ev.condition_id, ev.question, ev.up_token_id, ev.down_token_id,
             _iso(ev.start_time), _iso(ev.end_time), _iso(poll_ts)),
        )
        self._db.commit()

    def _record_book(self, ev_slug: str, outcome: str, token_id: str, book, poll_ts: datetime) -> None:
        payload = json.dumps({
            "bids": [[str(l.price), str(l.size)] for l in book.bids],
            "asks": [[str(l.price), str(l.size)] for l in book.asks],
        })
        self._db.execute(
            "INSERT INTO pm_orderbook_snapshots (event_slug, outcome, token_id, poll_ts, book_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (ev_slug, outcome, token_id, _iso(poll_ts), payload),
        )
        self._db.commit()

    def _record_settlement(self, ev: UpdownEvent, poll_ts: datetime) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO pm_settlements (event_slug, condition_id, result_up, end_time, finalized_poll_ts)"
            " VALUES (?, ?, ?, ?, ?)",
            (ev.slug, ev.condition_id, 1 if ev.result_up else 0, _iso(ev.end_time), _iso(poll_ts)),
        )
        self._db.commit()

    # ---- the poll loop

    def _disk_ok(self) -> str | None:
        try:
            free = self._disk_free_bytes(str(self._db_path.parent) or ".")
        except OSError:
            return None
        return None if free >= self._min_free_bytes else f"{free} bytes free, floor is {self._min_free_bytes}"

    def _db_size_ok(self) -> str | None:
        try:
            size = self._db_path.stat().st_size
        except FileNotFoundError:
            return None
        return None if size < self._max_db_bytes else f"{size} bytes, cap is {self._max_db_bytes}"

    async def _finalize_pending(self, pending: set[str], stats: _Counters) -> None:
        for slug in list(pending):
            try:
                ev = await self._client.get_event(slug)
            except (PolymarketAPIError, PolymarketConnectionError, ParseError) as exc:
                stats.errors += 1
                self._log("warning", "poll_error", f"settlement check {slug}: {exc}")
                continue
            if ev.closed and ev.result_up is not None:
                self._record_settlement(ev, poll_ts=self._clock())
                stats.settlements += 1
                pending.discard(slug)

    async def run(self, *, duration_sec: float | None = None, deadline: datetime | None = None) -> PolymarketRecorderSummary:
        if duration_sec is not None and deadline is not None:
            raise ValueError("pass duration_sec or deadline, not both")
        started = self._clock()
        end = deadline if deadline is not None else (started + timedelta(seconds=duration_sec) if duration_sec else None)
        self._log("info", "start", f"recording Polymarket btc-updown-{self._horizon} -> {self._db_path}")

        stats = _Counters()
        pending_settlement: set[str] = set()
        current_slug: str | None = None

        def stop(reason: str, detail: str) -> PolymarketRecorderSummary:
            stopped = self._clock()
            self._log("info", "stop", f"{reason}: {detail}")
            return PolymarketRecorderSummary(
                started_at=started, stopped_at=stopped, stop_reason=reason, stop_detail=detail,
                book_polls=stats.book_polls, market_state_changes=stats.market_state_changes,
                settlements=stats.settlements, rollover_gaps=stats.rollover_gaps, errors=stats.errors,
                unresolved_settlements=tuple(sorted(pending_settlement)),
            )

        try:
            while True:
                now = self._clock()
                if end is not None and now >= end:
                    return stop("time_limit", f"reached the recording deadline of {_iso(end)}")
                if self._kill_file.exists():
                    return stop("kill_file", str(self._kill_file))
                disk_problem = self._disk_ok()
                if disk_problem is not None:
                    return stop("disk_floor", disk_problem)
                size_problem = self._db_size_ok()
                if size_problem is not None:
                    return stop("db_size_cap", size_problem)

                try:
                    events = await self._client.list_recent_updown_events(horizon=self._horizon, limit=20)
                except (PolymarketAPIError, PolymarketConnectionError, ParseError) as exc:
                    stats.errors += 1
                    self._log("warning", "poll_error", f"discovery: {exc}")
                    if stats.errors >= self._max_consecutive_failures:
                        return stop("repeated_failures", f"{stats.errors} consecutive failures")
                    await self._sleep(self._poll_interval_sec)
                    continue

                live = next((e for e in events if not e.closed and e.start_time <= now <= e.end_time), None)
                if live is None:
                    stats.rollover_gaps += 1
                    if current_slug is not None:
                        self._log("info", "rollover_gap", f"no open btc-updown-{self._horizon} event")
                        pending_settlement.add(current_slug)
                    current_slug = None
                    await self._finalize_pending(pending_settlement, stats)
                    await self._sleep(self._poll_interval_sec)
                    continue

                if live.slug != current_slug:
                    if current_slug is not None:
                        pending_settlement.add(current_slug)
                    self._record_market_state(live, now)
                    stats.market_state_changes += 1
                    current_slug = live.slug

                try:
                    poll_ts = self._clock()
                    up_book = await self._client.get_order_book(live.up_token_id)
                    down_book = await self._client.get_order_book(live.down_token_id)
                    self._record_book(live.slug, "up", live.up_token_id, up_book, poll_ts)
                    self._record_book(live.slug, "down", live.down_token_id, down_book, poll_ts)
                    stats.book_polls += 1
                    stats.errors = 0
                except (PolymarketAPIError, PolymarketConnectionError, ParseError) as exc:
                    stats.errors += 1
                    self._log("warning", "poll_error", f"book {live.slug}: {exc}")
                    if stats.errors >= self._max_consecutive_failures:
                        return stop("repeated_failures", f"{stats.errors} consecutive failures")

                await self._finalize_pending(pending_settlement, stats)
                elapsed = (self._clock() - now).total_seconds()
                delay = self._poll_interval_sec - elapsed
                await self._sleep(delay if delay > 0 else self._poll_interval_sec)
        except asyncio.CancelledError:
            self._log("info", "stop", "cancelled: task cancelled")
            raise
