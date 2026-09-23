"""Historical data pipeline: settled KXBTC15M markets (via the existing, already-public/unauthenticated
:meth:`btcbot.kalshi_client.KalshiClient.list_markets`) and 1-minute Coinbase BTC-USD candles
(:mod:`btcbot.coinbase_history`), into one SQLite database for the market-level ML entry pipeline
(docs/research/ml-layers-handoff.md, "Codex's backtest pipeline").

Like :mod:`btcbot.recorder`, this only ever talks to public, unauthenticated endpoints -- no key, ever.
Unlike ``recorder.py`` it is not a live poll loop: it is a one-shot backfill of PAST settled markets and
candles, run by the owner (this session's own environment cannot reach Kalshi/Coinbase; see CLAUDE.md and
docs/running-live.md) -- the same division of labor as ``btcbot record``/``btcbot stream``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol

from btcbot.coinbase_history import Candle
from btcbot.models import HistoricalCutoff, Market, MarketCandle, Trade, parse_time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_outcomes (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT NOT NULL,
    open_time TEXT NOT NULL,
    close_time TEXT NOT NULL,
    strike TEXT,
    result TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_outcomes_close_time ON market_outcomes (close_time);

CREATE TABLE IF NOT EXISTS spot_candles (
    start_ts TEXT PRIMARY KEY,
    low TEXT NOT NULL,
    high TEXT NOT NULL,
    open TEXT NOT NULL,
    close TEXT NOT NULL,
    volume TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_candles (
    ticker TEXT NOT NULL,
    end_ts TEXT NOT NULL,          -- UTC ISO 8601, end of the 1-minute bar
    yes_bid_open TEXT, yes_bid_high TEXT, yes_bid_low TEXT, yes_bid_close TEXT,
    yes_ask_open TEXT, yes_ask_high TEXT, yes_ask_low TEXT, yes_ask_close TEXT,
    price_open TEXT, price_high TEXT, price_low TEXT, price_close TEXT, price_mean TEXT, price_previous TEXT,
    volume TEXT NOT NULL,
    open_interest TEXT NOT NULL,
    PRIMARY KEY (ticker, end_ts)
);

-- One row per settled market the backfill has finished, so a re-run (or a resume after a crash/Ctrl-C) can
-- skip work already done instead of re-fetching and re-writing it. A market only gets a row here once BOTH
-- its trades and candles steps for that run have committed -- see save_backfill_progress.
CREATE TABLE IF NOT EXISTS backfill_progress (
    ticker TEXT PRIMARY KEY,
    trades_done INTEGER NOT NULL,
    candles_done INTEGER NOT NULL,
    trade_count INTEGER NOT NULL,
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


class HistoryError(Exception):
    """A database missing the schema, or a row that cannot be parsed back out."""


class KalshiHistorySource(Protocol):
    async def list_markets(self, *, series_ticker: str, status: str | None = None) -> list[Market]: ...
    async def list_historical_markets(self, *, series_ticker: str) -> list[Market]: ...
    async def get_historical_cutoff(self) -> HistoricalCutoff: ...
    async def get_trades(self, ticker: str, *, min_ts: datetime | None = None, max_pages: int = 20) -> list[Trade]: ...
    async def get_historical_trades(
        self, ticker: str, *, min_ts: datetime | None = None, max_ts: datetime | None = None, max_pages: int = 200
    ) -> list[Trade]: ...
    async def get_market_candlesticks(
        self, series_ticker: str, ticker: str, *, start: datetime, end: datetime
    ) -> list[MarketCandle]: ...
    async def get_historical_candlesticks(self, ticker: str, *, start: datetime, end: datetime) -> list[MarketCandle]: ...


def init_history_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def log_run_event(conn: sqlite3.Connection, event: str, detail: str, *, level: str = "info") -> None:
    conn.execute(
        "INSERT INTO run_log (ts, level, event, detail) VALUES (?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), level, event, detail),
    )
    conn.commit()


async def fetch_all_settled_markets(client: KalshiHistorySource, *, series_ticker: str = "KXBTC15M") -> list[Market]:
    """Every settled market, live-listed AND historical, unioned and deduped by ticker.

    The live endpoint (``list_markets(status="settled")``) stops serving a market once it ages past
    ``market_settled_ts``; before this fix, that meant markets older than the cutoff were silently missing
    from ``market_outcomes`` entirely -- confirmed live 2026-09-23: the historical listing alone returned
    20,897 KXBTC15M markets, almost all older than what the live listing can still see. The historical
    listing wins on a ticker present in both, since Kalshi's docs describe it as the final record."""
    live = await client.list_markets(series_ticker=series_ticker, status="settled")
    historical = await client.list_historical_markets(series_ticker=series_ticker)
    by_ticker = {m.ticker: m for m in live}
    by_ticker.update({m.ticker: m for m in historical})  # historical wins on conflict
    return list(by_ticker.values())


def save_market_outcomes(conn: sqlite3.Connection, markets: Sequence[Market]) -> int:
    """Writes only SETTLED markets (:attr:`btcbot.models.Market.result` is not None). Returns how many were
    written; a market missing a result (still open, or unsettled/void) is skipped, never written with a
    placeholder outcome. ``INSERT OR REPLACE`` so re-running a backfill over an overlapping range is safe."""
    rows = [
        (
            m.ticker, m.event_ticker, m.open_time.astimezone(timezone.utc).isoformat(),
            m.close_time.astimezone(timezone.utc).isoformat(),
            None if m.strike is None else str(m.strike), m.result,
        )
        for m in markets if m.result is not None
    ]
    conn.executemany(
        """INSERT OR REPLACE INTO market_outcomes (ticker, event_ticker, open_time, close_time, strike, result)
           VALUES (?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    return len(rows)


def save_candles(conn: sqlite3.Connection, candles: Sequence[Candle]) -> int:
    rows = [
        (c.start.astimezone(timezone.utc).isoformat(), str(c.low), str(c.high), str(c.open), str(c.close), str(c.volume))
        for c in candles
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO spot_candles (start_ts, low, high, open, close, volume) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


# A window's own lifetime is short (15 minutes); this grace catches a trade/candle printed right at the
# close, or clock skew between our request and Kalshi's, without pulling in the NEXT window's own data.
_WINDOW_GRACE_TRADES = timedelta(seconds=10)
_WINDOW_GRACE_CANDLES = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class RoutingStraddle:
    """A market whose window crossed the historical/live cutoff mid-flight: both endpoints had to be called
    and their results merged. Rare (the cutoff moves steadily forward, a 15-minute window rarely straddles
    it), but real -- the caller should log/count these, not just silently merge and move on."""

    ticker: str
    what: str  # "trades" | "candles"


async def fetch_market_trades(
    client: KalshiHistorySource, market: Market, cutoff: HistoricalCutoff
) -> tuple[list[Trade], RoutingStraddle | None]:
    """All trade prints for one settled market's lifetime, routed to whichever endpoint(s) actually cover it.
    Fully before ``trades_created_ts``: historical only. Fully after: live only. Straddling (rare): both,
    deduped by ``trade_id`` -- flagged back to the caller rather than silently merged."""
    start, end = market.open_time, market.close_time + _WINDOW_GRACE_TRADES
    cut = cutoff.trades_created_ts
    if end < cut:
        return await client.get_historical_trades(market.ticker, min_ts=start, max_ts=end), None
    if start >= cut:
        return await client.get_trades(market.ticker, min_ts=start), None
    hist = await client.get_historical_trades(market.ticker, min_ts=start, max_ts=end)
    live = await client.get_trades(market.ticker, min_ts=start)
    by_id = {t.trade_id: t for t in hist}
    by_id.update({t.trade_id: t for t in live})
    merged = sorted(by_id.values(), key=lambda t: (t.created_time, t.trade_id))
    return merged, RoutingStraddle(market.ticker, "trades")


async def fetch_market_candles(
    client: KalshiHistorySource, series: str, market: Market, cutoff: HistoricalCutoff
) -> tuple[list[MarketCandle], RoutingStraddle | None]:
    """All 1-minute candles for one settled market's lifetime, routed the same way as
    :func:`fetch_market_trades` but keyed on ``market_settled_ts`` (the field that gates BOTH the markets
    and candlesticks historical endpoints, per Kalshi's docs)."""
    start, end = market.open_time, market.close_time + _WINDOW_GRACE_CANDLES
    cut = cutoff.market_settled_ts
    if end < cut:
        return await client.get_historical_candlesticks(market.ticker, start=start, end=end), None
    if start >= cut:
        return await client.get_market_candlesticks(series, market.ticker, start=start, end=end), None
    hist = await client.get_historical_candlesticks(market.ticker, start=start, end=end)
    live = await client.get_market_candlesticks(series, market.ticker, start=start, end=end)
    by_ts = {c.end_ts: c for c in hist}
    by_ts.update({c.end_ts: c for c in live})
    merged = sorted(by_ts.values(), key=lambda c: c.end_ts)
    return merged, RoutingStraddle(market.ticker, "candles")


def save_market_candles(conn: sqlite3.Connection, candles: Sequence[MarketCandle]) -> int:
    def d(v: Decimal | None) -> str | None:
        return None if v is None else str(v)

    rows = [
        (
            c.ticker, c.end_ts.astimezone(timezone.utc).isoformat(),
            d(c.yes_bid_open), d(c.yes_bid_high), d(c.yes_bid_low), d(c.yes_bid_close),
            d(c.yes_ask_open), d(c.yes_ask_high), d(c.yes_ask_low), d(c.yes_ask_close),
            d(c.price_open), d(c.price_high), d(c.price_low), d(c.price_close), d(c.price_mean), d(c.price_previous),
            str(c.volume), str(c.open_interest),
        )
        for c in candles
    ]
    conn.executemany(
        """INSERT OR REPLACE INTO market_candles
           (ticker, end_ts, yes_bid_open, yes_bid_high, yes_bid_low, yes_bid_close,
            yes_ask_open, yes_ask_high, yes_ask_low, yes_ask_close,
            price_open, price_high, price_low, price_close, price_mean, price_previous, volume, open_interest)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()
    return len(rows)


def load_market_candles(conn: sqlite3.Connection, ticker: str | None = None) -> list[MarketCandle]:
    def dec(v: str | None) -> Decimal | None:
        return None if v is None else Decimal(v)

    sql = (
        "SELECT ticker, end_ts, yes_bid_open, yes_bid_high, yes_bid_low, yes_bid_close, "
        "yes_ask_open, yes_ask_high, yes_ask_low, yes_ask_close, "
        "price_open, price_high, price_low, price_close, price_mean, price_previous, volume, open_interest "
        "FROM market_candles"
    )
    params: tuple = ()
    if ticker is not None:
        sql += " WHERE ticker = ?"
        params = (ticker,)
    sql += " ORDER BY ticker, end_ts"
    rows = conn.execute(sql, params).fetchall()
    return [
        MarketCandle(
            ticker=t, end_ts=parse_time(end_ts),
            yes_bid_open=dec(ybo), yes_bid_high=dec(ybh), yes_bid_low=dec(ybl), yes_bid_close=dec(ybc),
            yes_ask_open=dec(yao), yes_ask_high=dec(yah), yes_ask_low=dec(yal), yes_ask_close=dec(yac),
            price_open=dec(po), price_high=dec(ph), price_low=dec(pl), price_close=dec(pc),
            price_mean=dec(pm), price_previous=dec(pp), volume=Decimal(vol), open_interest=Decimal(oi),
        )
        for t, end_ts, ybo, ybh, ybl, ybc, yao, yah, yal, yac, po, ph, pl, pc, pm, pp, vol, oi in rows
    ]


def is_market_done(
    conn: sqlite3.Connection, ticker: str, *, need_trades: bool = True, need_candles: bool = True
) -> bool:
    """Whether a market's backfill_progress row already covers what this run actually needs. A row written
    by a `--no-trades`-only or `--no-candles`-only run only satisfies the half it recorded -- e.g. a market
    marked candles_done=0 is NOT done for a run that wants candles, even though a row exists for it."""
    row = conn.execute(
        "SELECT trades_done, candles_done FROM backfill_progress WHERE ticker = ?", (ticker,)
    ).fetchone()
    if row is None:
        return False
    trades_done, candles_done = row
    if need_trades and not trades_done:
        return False
    if need_candles and not candles_done:
        return False
    return True


def save_backfill_progress(
    conn: sqlite3.Connection, ticker: str, *, trades_done: bool, candles_done: bool, trade_count: int, fetched_at: datetime
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO backfill_progress (ticker, trades_done, candles_done, trade_count, fetched_at)
           VALUES (?, ?, ?, ?, ?)""",
        (ticker, int(trades_done), int(candles_done), trade_count, fetched_at.astimezone(timezone.utc).isoformat()),
    )
    conn.commit()


@dataclass(frozen=True, slots=True)
class MarketOutcome:
    ticker: str
    event_ticker: str
    open_time: datetime
    close_time: datetime
    strike: Decimal | None
    result: str


def load_market_outcomes(conn: sqlite3.Connection) -> list[MarketOutcome]:
    rows = conn.execute(
        "SELECT ticker, event_ticker, open_time, close_time, strike, result FROM market_outcomes ORDER BY close_time"
    ).fetchall()
    return [
        MarketOutcome(
            ticker=ticker, event_ticker=event_ticker, open_time=parse_time(open_time), close_time=parse_time(close_time),
            strike=None if strike is None else Decimal(strike), result=result,
        )
        for ticker, event_ticker, open_time, close_time, strike, result in rows
    ]


def load_candles(conn: sqlite3.Connection) -> list[Candle]:
    rows = conn.execute("SELECT start_ts, low, high, open, close, volume FROM spot_candles ORDER BY start_ts").fetchall()
    return [
        Candle(start=parse_time(start_ts), low=Decimal(low), high=Decimal(high), open=Decimal(open_),
               close=Decimal(close), volume=Decimal(volume))
        for start_ts, low, high, open_, close, volume in rows
    ]
