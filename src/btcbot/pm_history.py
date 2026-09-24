"""Resumable historical backfill for the Polymarket reaction dataset (``btcbot download-polymarket-history``):
settled Polymarket "Bitcoin Up or Down" windows, their public trade tape, and Binance BTCUSDT 1-second klines,
into ONE SQLite file (docs/research/polymarket-reaction-data.md).

READ-ONLY research into a separate venue, same footing as :mod:`btcbot.polymarket_recorder`: public,
unauthenticated GETs only (:class:`btcbot.polymarket_client.PolymarketClient`, which has no order method at
all, and :mod:`btcbot.binance_history`). Every table here is ``pm_``- or ``btc_``-prefixed so a database built
by this module can never be mistaken for -- or read as -- a Kalshi recording; ``btcbot.features.build_rows``
looks specifically for Kalshi's ``orderbook_snapshots`` and skips it.

Resumable: a window with a ``pm_hist_progress`` row is skipped on a re-run (a window whose event does not
exist is remembered as ``missing`` and only re-asked with ``retry_missing``; a window not yet resolved gets no
row at all, so the next run asks again), and a Binance day with a ``btc_days_done`` row is never re-fetched.
Trades are replaced per window, never appended, so an interrupted window cannot end up double-counted.

Disk: the tape is stored as per-second AGGREGATES (``pm_hist_tape``: one row per window/second/token/taker
side with the print count, total size and total notional), not raw prints -- a busy 15-minute window has up to
~11,000 prints, and storing each one filled the owner's disk (2026-09-24) before ``btcbot paper``/``demo`` could
even create their own databases. Everything :mod:`btcbot.pm_reaction` reads (per-second volume-weighted price,
signed flow, print counts) survives the aggregation exactly. Both backfills also stop cleanly once free space
falls below ``min_free_bytes`` (default 2 GiB, leaving room for live recordings). A database written before this
change (raw ``pm_hist_trades``) is still read.
"""

from __future__ import annotations

import asyncio
import math
import re
import shutil
import sqlite3
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx

from btcbot.binance_history import (
    BinanceHistoryError,
    BinanceNotPublished,
    Kline1s,
    days_covering,
    fetch_daily_klines,
    fetch_klines_rest,
)
from btcbot.models import ParseError
from btcbot.polymarket_client import PmTrade, PolymarketAPIError, PolymarketClient, PolymarketConnectionError

HORIZON_SEC = {"5m": 300, "15m": 900}
DEFAULT_MIN_FREE_BYTES = 2 * 1024**3  # 2 GiB: leave room for btcbot paper/demo/record on the same disk
_SLUG_RE = re.compile(r"^btc-updown-(5m|15m)-(\d+)$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pm_hist_markets (
    slug TEXT PRIMARY KEY,
    horizon_sec INTEGER NOT NULL,
    condition_id TEXT NOT NULL,
    up_token_id TEXT NOT NULL,
    down_token_id TEXT NOT NULL,
    window_start INTEGER NOT NULL,   -- unix seconds (the slug's epoch)
    window_end INTEGER NOT NULL,     -- unix seconds (the event's endDate)
    result_up INTEGER NOT NULL,      -- 1 if "Up" won, 0 if "Down" won (only resolved windows are stored)
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pm_hist_markets_start ON pm_hist_markets (window_start);

-- Taker prints aggregated per second (see the module docstring on disk). No wallet/name/profile columns.
CREATE TABLE IF NOT EXISTS pm_hist_tape (
    slug TEXT NOT NULL,
    ts INTEGER NOT NULL,             -- unix seconds
    outcome TEXT NOT NULL,           -- "up" | "down"
    side TEXT NOT NULL,              -- taker side, "BUY" | "SELL"
    n INTEGER NOT NULL,              -- prints in this second/token/side
    size TEXT NOT NULL,              -- total shares
    notional TEXT NOT NULL,          -- total price * shares (VWAP = notional / size)
    PRIMARY KEY (slug, ts, outcome, side)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS pm_hist_progress (
    slug TEXT PRIMARY KEY,
    status TEXT NOT NULL,            -- "done" | "missing" | "window_mismatch"
    trade_count INTEGER NOT NULL,
    truncated INTEGER NOT NULL,      -- 1 if the Data API offset cap cut off the oldest trades
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS btc_klines_1s (
    ts INTEGER PRIMARY KEY,          -- bar OPEN time, unix seconds; close = last price in [ts, ts + 1)
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume TEXT NOT NULL,
    n_trades INTEGER NOT NULL,
    taker_buy_volume TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS btc_days_done (
    day TEXT PRIMARY KEY,
    source TEXT NOT NULL,            -- "archive" | "rest"
    rows INTEGER NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


class PmHistoryError(Exception):
    """Bad arguments, or a database that does not have this module's schema."""


def init_pm_history_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(conn: sqlite3.Connection, event: str, detail: str, *, level: str = "info") -> None:
    conn.execute("INSERT INTO run_log (ts, level, event, detail) VALUES (?, ?, ?, ?)", (_now_iso(), level, event, detail))
    conn.commit()


def slug_for(horizon: str, window_start: int) -> str:
    return f"btc-updown-{horizon}-{window_start}"


def parse_slug(slug: str) -> tuple[str, int] | None:
    """``(horizon, window_start)`` for a ``btc-updown-<5m|15m>-<epoch>`` slug, else None."""
    m = _SLUG_RE.match(slug)
    return (m.group(1), int(m.group(2))) if m else None


def window_starts(horizon: str, since: datetime, until: datetime) -> list[int]:
    """Start epochs of every window that starts at/after ``since`` and ENDS at/before ``until``, oldest first."""
    if horizon not in HORIZON_SEC:
        raise PmHistoryError(f"horizon must be one of {sorted(HORIZON_SEC)}, got {horizon!r}")
    step = HORIZON_SEC[horizon]
    lo = math.ceil(since.timestamp() / step) * step
    hi = int(until.timestamp()) - step
    return list(range(lo, hi + 1, step)) if hi >= lo else []


# --------------------------------------------------------------------------- writes


def _compact(value: Decimal) -> str:
    """A Decimal as the shortest plain string ("93000.10000000" -> "93000.1", never "9.3E+4")."""
    text = format(value.normalize(), "f")
    return text if text not in ("-0", "") else "0"


def aggregate_tape(trades: list[PmTrade]) -> list[tuple[int, str, str, int, Decimal, Decimal]]:
    """``(ts, outcome, side, prints, total size, total notional)`` per second/token/taker side, oldest first."""
    groups: dict[tuple[int, str, str], list[Decimal | int]] = defaultdict(lambda: [0, Decimal(0), Decimal(0)])
    for t in trades:
        g = groups[(int(t.timestamp.timestamp()), t.outcome, t.side)]
        g[0] += 1
        g[1] += t.size
        g[2] += t.price * t.size
    return [(ts, o, sd, int(g[0]), g[1], g[2]) for (ts, o, sd), g in sorted(groups.items())]


def save_window(
    conn: sqlite3.Connection, *, slug: str, horizon_sec: int, condition_id: str, up_token_id: str,
    down_token_id: str, window_start: int, window_end: int, result_up: bool, trades: list[PmTrade], truncated: bool,
) -> None:
    """One resolved window and its whole tape, in one transaction: a crash mid-write leaves neither."""
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO pm_hist_markets (slug, horizon_sec, condition_id, up_token_id, down_token_id,"
            " window_start, window_end, result_up, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (slug, horizon_sec, condition_id, up_token_id, down_token_id, window_start, window_end,
             1 if result_up else 0, _now_iso()),
        )
        conn.execute("DELETE FROM pm_hist_tape WHERE slug = ?", (slug,))
        conn.executemany(
            "INSERT INTO pm_hist_tape (slug, ts, outcome, side, n, size, notional) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(slug, ts, o, sd, n, _compact(size), _compact(notional)) for ts, o, sd, n, size, notional in aggregate_tape(trades)],
        )
        conn.execute(
            "INSERT OR REPLACE INTO pm_hist_progress (slug, status, trade_count, truncated, fetched_at)"
            " VALUES (?, 'done', ?, ?, ?)",
            (slug, len(trades), 1 if truncated else 0, _now_iso()),
        )


def mark_window(conn: sqlite3.Connection, slug: str, status: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO pm_hist_progress (slug, status, trade_count, truncated, fetched_at) VALUES (?, ?, 0, 0, ?)",
            (slug, status, _now_iso()),
        )


def save_klines(conn: sqlite3.Connection, klines: list[Kline1s]) -> int:
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO btc_klines_1s (ts, open, high, low, close, volume, n_trades, taker_buy_volume)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(k.ts, _compact(k.open), _compact(k.high), _compact(k.low), _compact(k.close), _compact(k.volume),
              k.n_trades, _compact(k.taker_buy_volume)) for k in klines],
        )
    return len(klines)


def mark_day(conn: sqlite3.Connection, day: date, source: str, rows: int) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO btc_days_done (day, source, rows, fetched_at) VALUES (?, ?, ?, ?)",
            (day.isoformat(), source, rows, _now_iso()),
        )


def progress_status(conn: sqlite3.Connection, slug: str) -> str | None:
    row = conn.execute("SELECT status FROM pm_hist_progress WHERE slug = ?", (slug,)).fetchone()
    return row[0] if row else None


def day_done(conn: sqlite3.Connection, day: date) -> bool:
    return conn.execute("SELECT 1 FROM btc_days_done WHERE day = ?", (day.isoformat(),)).fetchone() is not None


# --------------------------------------------------------------------------- backfill


@dataclass
class PolymarketBackfillSummary:
    windows: int = 0
    done: int = 0
    skipped: int = 0
    missing: int = 0
    unresolved: int = 0
    window_mismatch: int = 0
    failed: int = 0
    truncated: int = 0
    trades: int = 0
    failures: list[str] = field(default_factory=list)
    stopped: str | None = None  # why the run stopped early (e.g. the free-disk floor), else None


def disk_problem(path: str, min_free_bytes: int, disk_free: Callable[[str], int]) -> str | None:
    try:
        free = disk_free(path)
    except OSError:
        return None
    if free >= min_free_bytes:
        return None
    return (f"only {free / 1024**3:.2f} GiB free on the disk holding {path} (floor {min_free_bytes / 1024**3:.2f} GiB); "
            "stopped cleanly -- free space and rerun the same command to resume")


async def backfill_polymarket(
    client: PolymarketClient,
    conn: sqlite3.Connection,
    *,
    horizon: str,
    since: datetime,
    until: datetime,
    concurrency: int = 2,
    sleep_sec: float = 0.25,
    retry_missing: bool = False,
    limit_markets: int | None = None,
    progress: Callable[[str], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    db_dir: str = ".",
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    disk_free: Callable[[str], int] = lambda path: shutil.disk_usage(path).free,
) -> PolymarketBackfillSummary:
    """Every ``btc-updown-<horizon>`` window in range: its Gamma event (condition id, token ids, resolution)
    and full taker trade tape. Windows are derived from the slug epoch (the window START), and the event's own
    ``endDate`` must equal start + horizon or the window is recorded as ``window_mismatch`` and left out --
    every downstream feature assumes the slug's window."""
    step = HORIZON_SEC.get(horizon)
    if step is None:
        raise PmHistoryError(f"horizon must be one of {sorted(HORIZON_SEC)}, got {horizon!r}")
    starts = window_starts(horizon, since, until)
    if limit_markets is not None:
        starts = starts[:limit_markets]
    summary = PolymarketBackfillSummary(windows=len(starts))
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(start: int) -> None:
        slug = slug_for(horizon, start)
        status = progress_status(conn, slug)
        if status in ("done", "window_mismatch") or (status == "missing" and not retry_missing):
            summary.skipped += 1
            return
        async with sem:
            if summary.stopped is not None:
                return
            problem = disk_problem(db_dir, min_free_bytes, disk_free)
            if problem is not None:
                summary.stopped = problem
                return
            if sleep_sec:
                await sleep(sleep_sec)
            try:
                try:
                    ev = await client.get_event(slug)
                except PolymarketAPIError as exc:
                    if exc.status_code == 404:
                        mark_window(conn, slug, "missing")
                        summary.missing += 1
                        return
                    raise
                if not ev.closed or ev.result_up is None:
                    summary.unresolved += 1  # no progress row: the next run asks again
                    return
                end = int(ev.end_time.timestamp())
                if end - start != step:
                    mark_window(conn, slug, "window_mismatch")
                    log_event(conn, "window_mismatch", f"{slug}: endDate {ev.end_time.isoformat()} is not start + {step}s", level="warning")
                    summary.window_mismatch += 1
                    return
                trades, truncated = await client.get_market_trades(ev.condition_id)
                save_window(
                    conn, slug=slug, horizon_sec=step, condition_id=ev.condition_id, up_token_id=ev.up_token_id,
                    down_token_id=ev.down_token_id, window_start=start, window_end=end, result_up=ev.result_up,
                    trades=trades, truncated=truncated,
                )
            except (PolymarketAPIError, PolymarketConnectionError, ParseError, sqlite3.Error) as exc:
                summary.failed += 1
                summary.failures.append(f"{slug}: {exc}")
                log_event(conn, "window_failed", f"{slug}: {exc}", level="warning")
                return
            summary.done += 1
            summary.trades += len(trades)
            if truncated:
                summary.truncated += 1
                log_event(conn, "trades_truncated", f"{slug}: hit the Data API offset cap; oldest trades missing", level="warning")
            if progress is not None and (summary.done % 25 == 0):
                progress(f"  {summary.done + summary.skipped + summary.missing}/{summary.windows} windows, {summary.trades} trades so far")

    await asyncio.gather(*(one(s) for s in starts))
    return summary


@dataclass
class BtcBackfillSummary:
    days: int = 0
    skipped: int = 0
    archive_days: int = 0
    rest_days: int = 0
    rows: int = 0
    failed: list[str] = field(default_factory=list)
    stopped: str | None = None


async def backfill_btc(
    http: httpx.AsyncClient,
    conn: sqlite3.Connection,
    *,
    start: datetime,
    end: datetime,
    rest_fallback: bool = True,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    progress: Callable[[str], None] | None = None,
    db_dir: str = ".",
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    disk_free: Callable[[str], int] = lambda path: shutil.disk_usage(path).free,
) -> BtcBackfillSummary:
    """Binance BTCUSDT 1s klines for every UTC day touched by ``[start, end)``: the checksum-verified daily
    archive first, the REST host for a day the archive has not published yet. A day is only marked done once
    it is complete (it has ended), so a partial today is re-fetched next run."""
    summary = BtcBackfillSummary()
    for day in days_covering(start, end):
        summary.days += 1
        if day_done(conn, day):
            summary.skipped += 1
            continue
        problem = disk_problem(db_dir, min_free_bytes, disk_free)
        if problem is not None:
            summary.stopped = problem
            break
        day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)
        try:
            try:
                klines = await fetch_daily_klines(http, day)
                source = "archive"
            except BinanceNotPublished:
                if not rest_fallback:
                    raise
                klines = await fetch_klines_rest(http, start=day_start, end=min(day_end, now()))
                source = "rest"
        except BinanceHistoryError as exc:
            summary.failed.append(f"{day.isoformat()}: {exc}")
            log_event(conn, "btc_day_failed", f"{day.isoformat()}: {exc}", level="warning")
            continue
        summary.rows += save_klines(conn, klines)
        if source == "archive":
            summary.archive_days += 1
        else:
            summary.rest_days += 1
        if day_end <= now():
            mark_day(conn, day, source, len(klines))
        if progress is not None:
            progress(f"  BTC {day.isoformat()}: {len(klines)} 1s bars ({source})")
    return summary


# --------------------------------------------------------------------------- reads (for btcbot.pm_reaction)


@dataclass(frozen=True, slots=True)
class HistWindow:
    slug: str
    start: int
    end: int
    result_up: bool
    truncated: bool


@dataclass(frozen=True, slots=True)
class HistTrade:
    """One print, or one second's aggregate of prints on one token and taker side (``count`` of them)."""

    ts: int
    up_price: float  # (volume-weighted) price in "Up" terms (a Down print is 1 - price)
    up_flow: float  # signed taker size in "Up" terms
    outcome: str
    count: int = 1


def _require_tables(conn: sqlite3.Connection, *names: str) -> None:
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [n for n in names if n not in have]
    if missing:
        raise PmHistoryError(f"not a download-polymarket-history database: missing tables {missing}")


def load_windows(conn: sqlite3.Connection) -> list[HistWindow]:
    _require_tables(conn, "pm_hist_markets", "pm_hist_progress")
    rows = conn.execute(
        "SELECT m.slug, m.window_start, m.window_end, m.result_up, COALESCE(p.truncated, 0)"
        " FROM pm_hist_markets m LEFT JOIN pm_hist_progress p ON p.slug = m.slug ORDER BY m.window_start"
    ).fetchall()
    return [HistWindow(slug, int(s), int(e), bool(r), bool(t)) for slug, s, e, r, t in rows]


def _as_hist(ts: int, outcome: str, side: str, price: Decimal, size: Decimal, count: int) -> HistTrade:
    up_price = price if outcome == "up" else 1 - price
    bullish = (outcome == "up") == (side == "BUY")
    return HistTrade(int(ts), float(up_price), float(size if bullish else -size), outcome, count)


def load_trades(conn: sqlite3.Connection, slug: str) -> list[HistTrade]:
    """The window's tape, oldest first: per-second aggregates, or raw prints from a database written before
    aggregation existed (``pm_hist_trades``)."""
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "pm_hist_tape" not in have and "pm_hist_trades" not in have:
        raise PmHistoryError("not a download-polymarket-history database: no pm_hist_tape table")
    out: list[HistTrade] = []
    if "pm_hist_tape" in have:
        for ts, outcome, side, n, size, notional in conn.execute(
            "SELECT ts, outcome, side, n, size, notional FROM pm_hist_tape WHERE slug = ? ORDER BY ts", (slug,)
        ):
            size_d = Decimal(size)
            if size_d > 0:
                out.append(_as_hist(ts, outcome, side, Decimal(notional) / size_d, size_d, int(n)))
    if not out and "pm_hist_trades" in have:
        for ts, outcome, side, price, size in conn.execute(
            "SELECT ts, outcome, side, price, size FROM pm_hist_trades WHERE slug = ? ORDER BY ts", (slug,)
        ):
            out.append(_as_hist(ts, outcome, side, Decimal(price), Decimal(size), 1))
    return out


def load_btc(conn: sqlite3.Connection, start: int, end: int) -> dict[int, tuple[float, float, float]]:
    """``{bar open ts: (close, volume, taker_buy_volume)}`` for bars opening in ``[start, end]``."""
    _require_tables(conn, "btc_klines_1s")
    return {
        int(ts): (float(c), float(v), float(tb))
        for ts, c, v, tb in conn.execute(
            "SELECT ts, close, volume, taker_buy_volume FROM btc_klines_1s WHERE ts BETWEEN ? AND ? ORDER BY ts", (start, end)
        )
    }
