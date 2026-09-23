"""Data recorder: order books, market state, spot ticks and settlements into a SQLite database.

Phase 2 uses public, unauthenticated endpoints only (per CLAUDE.md and docs/review-and-overnight-plan.md:
"a fresh key must be provisioned securely before authenticated capture", and no key has been). That rules
out the WebSocket order-book-delta channel, which requires auth even for public data (see README, "Verified
Kalshi API facts"), so this recorder polls ``GET /markets`` and ``GET /markets/{ticker}/orderbook`` on a
short interval instead. That is a real limitation, not a placeholder detail: label anything built on this
data as coming from coarse REST polling, not full order-flow, until an authenticated capture exists.

One poll loop drives both: market discovery (also serves as the every-iteration check for a rollover or a
close), the order book, and settlement follow-up for markets that closed but have not yet finalized. Each
iteration costs at most three requests, comfortably inside the "at most 2 req/s average" budget most of the
time (pending settlements are the transient exception). Spot ticks arrive separately, pushed in by whatever
drives a :class:`btcbot.spot_feed.CoinbaseSpotFeed` (``record_spot_tick`` is the callback to hand it).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from btcbot.kalshi_client import KalshiError
from btcbot.market_discovery import find_current_market
from btcbot.models import Market, OrderBook, ParseError
from btcbot.trade_tape import init_trade_tape_schema, upsert_trades

if TYPE_CHECKING:
    from btcbot.spot_feed import SpotTick

log = logging.getLogger("btcbot.recorder")

DEFAULT_MAX_DB_BYTES = 1_000_000_000  # 1 GB, per the overnight plan's data cap
DEFAULT_MIN_FREE_BYTES = 2_000_000_000  # 2 GB free-disk floor
DEFAULT_POLL_INTERVAL_SEC = 1.0
DEFAULT_MAX_CONSECUTIVE_FAILURES = 20


class RecorderError(Exception):
    """Recorder setup failed (bad path, schema mismatch)."""


class KalshiSource(Protocol):
    async def list_markets(self, *, series_ticker: str, status: str | None = None) -> list[Market]: ...
    async def get_orderbook(self, ticker: str, *, depth: int = 0) -> OrderBook: ...
    async def get_market(self, ticker: str) -> Market: ...


# --------------------------------------------------------------------------- schema

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    request_started_ts TEXT NOT NULL,
    poll_ts TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    yes_bid_price TEXT, yes_bid_size TEXT, yes_ask_price TEXT, yes_ask_size TEXT,
    no_bid_price TEXT, no_bid_size TEXT, no_ask_price TEXT, no_ask_size TEXT,
    book_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orderbook_ticker_ts ON orderbook_snapshots (ticker, poll_ts);

CREATE TABLE IF NOT EXISTS market_state (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    poll_ts TEXT NOT NULL,
    status TEXT NOT NULL,
    strike TEXT,
    open_time TEXT NOT NULL,
    close_time TEXT NOT NULL,
    volume TEXT,
    open_interest TEXT
);
CREATE INDEX IF NOT EXISTS idx_market_state_ticker_ts ON market_state (ticker, poll_ts);

CREATE TABLE IF NOT EXISTS spot_ticks (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    price TEXT NOT NULL,
    source_ts TEXT,
    receive_ts TEXT NOT NULL,
    monotonic_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_ticks_receive_ts ON spot_ticks (receive_ts);

CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL UNIQUE,
    event_ticker TEXT NOT NULL,
    result TEXT,
    settled_avg TEXT,
    strike TEXT,
    close_time TEXT NOT NULL,
    finalized_poll_ts TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 1
);

-- trade_tape lives in btcbot.trade_tape now (shared with the historical backfill); its schema is applied
-- separately below via init_trade_tape_schema, not duplicated in this string.

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


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _book_json(book: OrderBook) -> str:
    return json.dumps(
        {
            "yes": [[str(level.price), str(level.size)] for level in book.yes_bids],
            "no": [[str(level.price), str(level.size)] for level in book.no_bids],
        },
        separators=(",", ":"),
    )


# --------------------------------------------------------------------------- summary


@dataclass(frozen=True, slots=True)
class RecorderSummary:
    started_at: datetime
    stopped_at: datetime
    stop_reason: str  # "time_limit" | "kill_file" | "disk_floor" | "db_size_cap" | "repeated_failures" | "cancelled"
    stop_detail: str
    orderbook_polls: int = 0
    market_state_changes: int = 0
    spot_ticks: int = 0
    settlements: int = 0
    rollover_gaps: int = 0
    errors: int = 0
    unresolved_settlements: tuple[str, ...] = ()


@dataclass
class _Counters:
    orderbook_polls: int = 0
    market_state_changes: int = 0
    settlements: int = 0
    rollover_gaps: int = 0
    errors: int = 0


# --------------------------------------------------------------------------- recorder


class Recorder:
    """Polls public Kalshi endpoints and writes everything to one SQLite file.

    Stop conditions (checked once per loop iteration, before that iteration's requests): a time limit, a
    ``KILL`` marker file, free disk space below a floor, the database file reaching a size cap, or too many
    consecutive request failures. None of these restart themselves; the caller decides whether to run again.

    ``on_orderbook`` and ``on_settlement`` are optional hooks for something that wants to react to live data
    as it arrives (Phase 5's live paper loop) without polling a second time: ``on_orderbook`` fires right
    after each order-book snapshot is written, with the market whose book it is; ``on_settlement`` fires
    right after a settlement is written. Both are awaited from inside the poll loop, so a slow hook slows
    recording -- keep them fast. Neither hook can affect what gets recorded; they observe, not filter.
    """

    def __init__(
        self,
        client: KalshiSource,
        *,
        series_ticker: str,
        db_path: str | Path,
        kill_file: str | Path = "KILL",
        max_db_bytes: int = DEFAULT_MAX_DB_BYTES,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        disk_free_bytes: Callable[[str], int] = lambda path: shutil.disk_usage(path).free,
        on_orderbook: Callable[[Market, OrderBook, datetime], Awaitable[None]] | None = None,
        on_settlement: Callable[[Market], Awaitable[None]] | None = None,
        settle_on_determined: bool = False,
        tape_poll_interval_sec: float = 4.0,
    ) -> None:
        if poll_interval_sec <= 0:
            raise ValueError("poll_interval_sec must be positive")
        self._client = client
        # Kalshi's DEMO exchange leaves BTC markets in status "determined" (result and expiration_value already
        # set) and never moves them to "finalized" (observed 2026-09-20: nothing after 12:15 ET finalized). Prod
        # finalizes within seconds. Only `btcbot demo` opts in; everywhere else waits for "finalized".
        self._settle_on_determined = settle_on_determined
        # Public trade tape (who actually crossed, at what price): what a fill model needs and books alone cannot show.
        self._tape_interval = timedelta(seconds=tape_poll_interval_sec)
        self._tape_due: datetime | None = None
        self._tape_last: dict[str, datetime] = {}
        self._tape_seen: dict[str, set[str]] = {}  # trade ids already folded into an aggregate row, per market
        self._tape_errors = 0
        self._series_ticker = series_ticker
        self._db_path = Path(db_path)
        self._kill_file = Path(kill_file)
        self._max_db_bytes = max_db_bytes
        self._min_free_bytes = min_free_bytes
        self._poll_interval_sec = poll_interval_sec
        self._max_consecutive_failures = max_consecutive_failures
        self._clock = clock
        self._sleep = sleep
        self._disk_free_bytes = disk_free_bytes
        self.on_orderbook = on_orderbook
        self.on_settlement = on_settlement

        self._spot_tick_count = 0

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._db_path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        init_trade_tape_schema(self._db)
        self._db.commit()

    def close(self) -> None:
        self._db.commit()
        self._db.close()

    # ---- writers (synchronous; each call is one small local-disk write)

    def _log(self, level: str, event: str, detail: str) -> None:
        getattr(log, level, log.info)("%s: %s", event, detail)
        self._db.execute(
            "INSERT INTO run_log (ts, level, event, detail) VALUES (?, ?, ?, ?)",
            (_iso(self._clock()), level, event, detail),
        )
        self._db.commit()

    async def _poll_tape(self, ticker: str, *, force: bool = False) -> None:
        """Store new public trades for ``ticker`` (deduped by trade id). Never raises and never counts toward the
        recorder's failure stop: a missing tape must not end an order-book recording."""
        get_trades = getattr(self._client, "get_trades", None)
        if get_trades is None:
            return
        now = self._clock()
        if not force and self._tape_due is not None and now < self._tape_due:
            return
        self._tape_due = now + self._tape_interval
        since = self._tape_last.get(ticker)
        try:
            # A generous overlap (the id set makes it safe) so a stalled poll cannot leave a gap; a hard timeout so a slow
            # /trades call cannot hold up the order-book poll that follows.
            trades = await asyncio.wait_for(
                get_trades(ticker, min_ts=None if since is None else since - timedelta(seconds=15)), timeout=8.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # any tape failure, never a reason to stop the recording
            self._tape_errors += 1
            if self._tape_errors <= 3 or self._tape_errors % 50 == 0:
                self._log("warning", "tape_error", f"trades {ticker}: {exc}")
            return
        seen = self._tape_seen.setdefault(ticker, set())
        fresh = []
        for t in trades:
            if t.trade_id in seen:
                continue  # the overlap between polls re-returns recent prints; count each once
            seen.add(t.trade_id)
            fresh.append(t)
            if since is None or t.created_time > since:
                since = t.created_time
        if fresh:
            upsert_trades(self._db, fresh)  # each trade passed in exactly once here, so additive is safe
            self._tape_last[ticker] = since
        if force:  # a forced poll is the final sweep of a market that just closed: free its dedupe set
            self._tape_seen.pop(ticker, None)
            self._tape_last.pop(ticker, None)

    def log_event(self, event: str, detail: str, *, level: str = "info") -> None:
        """Write one row to ``run_log`` (public wrapper, e.g. for ``account_start``)."""
        self._log(level, event, detail)

    def record_spot_tick(self, tick: SpotTick) -> None:
        self._db.execute(
            "INSERT INTO spot_ticks (source, price, source_ts, receive_ts, monotonic_ts) VALUES (?, ?, ?, ?, ?)",
            (
                tick.source,
                str(tick.price),
                None if tick.source_ts is None else _iso(tick.source_ts),
                _iso(tick.receive_ts),
                tick.monotonic_ts,
            ),
        )
        self._db.commit()
        self._spot_tick_count += 1

    def _record_market_state(self, market: Market, poll_ts: datetime) -> None:
        self._db.execute(
            """INSERT INTO market_state
               (ticker, event_ticker, poll_ts, status, strike, open_time, close_time, volume, open_interest)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                market.ticker,
                market.event_ticker,
                _iso(poll_ts),
                market.status,
                _dec(market.strike),
                _iso(market.open_time),
                _iso(market.close_time),
                _dec(market.volume),
                _dec(market.open_interest),
            ),
        )
        self._db.commit()

    def _record_orderbook(
        self, ticker: str, book: OrderBook, *, request_started: datetime, poll_ts: datetime
    ) -> None:
        yes_bid, yes_ask = book.best_bid("yes"), book.best_ask("yes")
        no_bid, no_ask = book.best_bid("no"), book.best_ask("no")
        self._db.execute(
            """INSERT INTO orderbook_snapshots
               (ticker, request_started_ts, poll_ts, latency_ms,
                yes_bid_price, yes_bid_size, yes_ask_price, yes_ask_size,
                no_bid_price, no_bid_size, no_ask_price, no_ask_size, book_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ticker,
                _iso(request_started),
                _iso(poll_ts),
                (poll_ts - request_started).total_seconds() * 1000,
                _dec(yes_bid.price if yes_bid else None),
                _dec(yes_bid.size if yes_bid else None),
                _dec(yes_ask.price if yes_ask else None),
                _dec(yes_ask.size if yes_ask else None),
                _dec(no_bid.price if no_bid else None),
                _dec(no_bid.size if no_bid else None),
                _dec(no_ask.price if no_ask else None),
                _dec(no_ask.size if no_ask else None),
                _book_json(book),
            ),
        )
        self._db.commit()

    def _record_settlement(self, market: Market, *, poll_ts: datetime, resolved: bool) -> None:
        result = market.raw.get("result") or None
        settled_avg = market.raw.get("expiration_value")
        self._db.execute(
            """INSERT OR REPLACE INTO settlements
               (ticker, event_ticker, result, settled_avg, strike, close_time, finalized_poll_ts, resolved)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                market.ticker,
                market.event_ticker,
                result,
                None if settled_avg is None else str(settled_avg),
                _dec(market.strike),
                _iso(market.close_time),
                _iso(poll_ts),
                1 if resolved else 0,
            ),
        )
        self._db.commit()

    # ---- the poll loop

    def _disk_ok(self) -> str | None:
        try:
            free = self._disk_free_bytes(str(self._db_path.parent))
        except OSError:
            return None  # cannot check free space (e.g. an unsupported filesystem); do not stop over it
        return None if free >= self._min_free_bytes else f"{free} bytes free, floor is {self._min_free_bytes}"

    def _db_size_ok(self) -> str | None:
        try:
            size = self._db_path.stat().st_size
        except FileNotFoundError:
            return None
        return None if size < self._max_db_bytes else f"{size} bytes, cap is {self._max_db_bytes}"

    async def _finalize_pending(self, pending: set[str], stats: _Counters) -> None:
        for ticker in list(pending):
            try:
                market = await self._client.get_market(ticker)
            except (KalshiError, ParseError) as exc:
                stats.errors += 1
                self._log("warning", "poll_error", f"settlement check {ticker}: {exc}")
                continue
            determined = (
                self._settle_on_determined and market.status == "determined"
                and market.raw.get("result") in ("yes", "no")
            )
            if market.status == "finalized" or determined:
                self._record_settlement(market, poll_ts=self._clock(), resolved=True)
                stats.settlements += 1
                pending.discard(ticker)
                if self.on_settlement is not None:
                    await self.on_settlement(market)

    async def run(
        self,
        *,
        duration_sec: float | None = None,
        deadline: datetime | None = None,
    ) -> RecorderSummary:
        if duration_sec is not None and deadline is not None:
            raise ValueError("pass duration_sec or deadline, not both")
        started = self._clock()
        end = deadline if deadline is not None else (started + timedelta(seconds=duration_sec) if duration_sec else None)
        self._log("info", "start", f"recording {self._series_ticker} -> {self._db_path}")

        stats = _Counters()
        pending_settlement: set[str] = set()
        last_state_key: tuple[str, str, str | None] | None = None

        def stop(reason: str, detail: str) -> RecorderSummary:
            self._log("info", "stop", f"{reason}: {detail}")
            stopped = self._clock()
            return RecorderSummary(
                started_at=started,
                stopped_at=stopped,
                stop_reason=reason,
                stop_detail=detail,
                orderbook_polls=stats.orderbook_polls,
                market_state_changes=stats.market_state_changes,
                spot_ticks=self._spot_tick_count,
                settlements=stats.settlements,
                rollover_gaps=stats.rollover_gaps,
                errors=stats.errors,
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
                    market = await find_current_market(self._client, self._series_ticker, now=now)
                except (KalshiError, ParseError) as exc:
                    stats.errors += 1
                    self._log("warning", "poll_error", f"discovery: {exc}")
                    if stats.errors >= self._max_consecutive_failures:
                        return stop("repeated_failures", f"{stats.errors} consecutive failures")
                    await self._sleep(self._poll_interval_sec)
                    continue

                if market is None:
                    stats.rollover_gaps += 1
                    if last_state_key is not None:
                        self._log("info", "rollover_gap", f"no open {self._series_ticker} market")
                        pending_settlement.add(last_state_key[0])
                    last_state_key = None
                    await self._finalize_pending(pending_settlement, stats)
                    await self._sleep(self._poll_interval_sec)
                    continue

                state_key = (market.ticker, market.status, _dec(market.strike))
                if state_key != last_state_key:
                    if last_state_key is not None and last_state_key[0] != market.ticker:
                        pending_settlement.add(last_state_key[0])
                        await self._poll_tape(last_state_key[0], force=True)  # last trades of the window that just closed
                    self._record_market_state(market, now)
                    stats.market_state_changes += 1
                    last_state_key = state_key

                try:
                    request_started = self._clock()
                    book = await self._client.get_orderbook(market.ticker)
                    poll_ts = self._clock()
                    if not market.is_open_at(poll_ts):
                        pending_settlement.add(market.ticker)
                        self._log("info", "expired_book", f"discarded post-close book for {market.ticker}")
                        await self._finalize_pending(pending_settlement, stats)
                        await self._sleep(self._poll_interval_sec)
                        continue
                    self._record_orderbook(market.ticker, book, request_started=request_started, poll_ts=poll_ts)
                    stats.orderbook_polls += 1
                    stats.errors = 0
                except (KalshiError, ParseError) as exc:
                    stats.errors += 1
                    self._log("warning", "poll_error", f"orderbook {market.ticker}: {exc}")
                    if stats.errors >= self._max_consecutive_failures:
                        return stop("repeated_failures", f"{stats.errors} consecutive failures")
                else:
                    # outside the except above: a hook failure is a real bug, not a network blip, and must
                    # not be miscounted as a poll error or silently swallowed.
                    if self.on_orderbook is not None:
                        await self.on_orderbook(market, book, poll_ts)

                await self._poll_tape(market.ticker)
                await self._finalize_pending(pending_settlement, stats)
                # Start-to-start cadence: request time is part of the interval.
                # On an overrun, retain a full cooldown rather than burst-catching up.
                elapsed = (self._clock() - now).total_seconds()
                delay = self._poll_interval_sec - elapsed
                await self._sleep(delay if delay > 0 else self._poll_interval_sec)
        except asyncio.CancelledError:
            self._log("info", "stop", "cancelled: task cancelled")
            raise
