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
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from btcbot.coinbase_history import Candle
from btcbot.models import Market, parse_time

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
"""


class HistoryError(Exception):
    """A database missing the schema, or a row that cannot be parsed back out."""


class KalshiHistorySource(Protocol):
    async def list_markets(self, *, series_ticker: str, status: str | None = None) -> list[Market]: ...


def init_history_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


async def fetch_all_settled_markets(client: KalshiHistorySource, *, series_ticker: str = "KXBTC15M") -> list[Market]:
    """Wraps the existing, already-public ``list_markets(status="settled")`` -- no new endpoint, no key.
    Kept as its own function (rather than calling ``list_markets`` directly from the CLI) so there is one
    obvious seam for tests to mock, matching :mod:`btcbot.recorder`'s own ``KalshiSource`` protocol."""
    return await client.list_markets(series_ticker=series_ticker, status="settled")


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
