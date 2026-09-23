"""The ``trade_tape`` table's schema and writers, shared between the live recorder (:mod:`btcbot.recorder`) and
the historical backfill (:mod:`btcbot.history_pipeline`) so both write the exact same shape and ``fillcheck``
never has to know which one produced a given database.

One row per market/second/yes_price/taker_side (not one row per print): a busy window can print thousands of
trades a second, and ``fillcheck`` only needs "how much volume crossed at this price around this time," not
individual print IDs -- aggregating keeps the tape roughly 13x smaller in practice with no loss to that question.

Two writers, because the two callers have different consistency needs:

* :func:`upsert_trades` -- the live recorder's shape: called repeatedly as new trades trickle in, deduping by
  trade id ACROSS calls (the caller tracks which ids it has already passed in) and adding to whatever is
  already stored for a (ticker, second, price, side) key. Safe to call many times as a window is still open.
* :func:`replace_ticker_tape` -- the backfill's shape: called ONCE per market with its ENTIRE trade history
  already fetched, deduping WITHIN that one call, and replacing (not adding to) whatever that ticker already
  had -- so re-running the backfill over an overlapping date range is idempotent, unlike calling
  :func:`upsert_trades` twice with the same trades would be (it would double-count them).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from btcbot.models import Trade

SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_tape (
    ticker TEXT NOT NULL,
    second_ts TEXT NOT NULL,       -- the trade second, UTC (a busy window prints thousands of trades; one row per
    yes_price TEXT NOT NULL,       -- second/price/taker keeps the tape ~13x smaller than raw prints, plenty for fills)
    no_price TEXT NOT NULL,
    taker_side TEXT NOT NULL,
    contracts REAL NOT NULL,
    prints INTEGER NOT NULL,
    PRIMARY KEY (ticker, second_ts, yes_price, taker_side)
);
"""


def init_trade_tape_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _second_key(trade: Trade) -> str:
    return trade.created_time.replace(microsecond=0).isoformat()


def upsert_trades(conn: sqlite3.Connection, trades: Sequence[Trade]) -> None:
    """Add ``trades`` to whatever the tape already holds for their (ticker, second, price, side) keys. The
    caller is responsible for not passing the same trade twice (the live recorder tracks seen trade ids
    itself); this function does not dedupe, by design -- see the module docstring."""
    for t in trades:
        conn.execute(
            "INSERT INTO trade_tape (ticker, second_ts, yes_price, no_price, taker_side, contracts, prints)"
            " VALUES (?, ?, ?, ?, ?, ?, 1) ON CONFLICT (ticker, second_ts, yes_price, taker_side) DO UPDATE SET"
            " contracts = contracts + excluded.contracts, prints = prints + 1",
            (t.ticker, _second_key(t), str(t.yes_price), str(t.no_price), t.taker_side, float(t.count)),
        )
    conn.commit()


def replace_ticker_tape(conn: sqlite3.Connection, ticker: str, trades: Sequence[Trade]) -> int:
    """Replace ALL tape rows for ``ticker`` with a fresh aggregation of ``trades`` (deduped by trade id first).
    Idempotent: calling this again with the same or an overlapping trade set for the same ticker gives the
    same rows, not double-counted ones -- unlike :func:`upsert_trades`, which is only safe when the caller
    guarantees each trade is passed in once, ever. Runs as one transaction: a market's tape is either fully
    replaced or (on error) left exactly as it was, never half-written. Returns the row count written."""
    seen: dict[str, Trade] = {}
    for t in trades:
        if t.ticker != ticker:
            raise ValueError(f"replace_ticker_tape({ticker!r}, ...): trade {t.trade_id} belongs to {t.ticker!r}")
        seen[t.trade_id] = t  # last write wins on a duplicate id; a duplicate is the same print, so any copy will do

    agg: dict[tuple[str, str, str], list] = {}
    for t in seen.values():
        key = (_second_key(t), str(t.yes_price), t.taker_side)
        row = agg.setdefault(key, [str(t.no_price), 0.0, 0])
        row[1] += float(t.count)
        row[2] += 1

    with conn:  # one transaction: delete-then-insert never leaves a partially-replaced tape
        conn.execute("DELETE FROM trade_tape WHERE ticker = ?", (ticker,))
        conn.executemany(
            "INSERT INTO trade_tape (ticker, second_ts, yes_price, no_price, taker_side, contracts, prints)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (ticker, second_ts, yes_price, no_price, taker_side, contracts, prints)
                for (second_ts, yes_price, taker_side), (no_price, contracts, prints) in agg.items()
            ],
        )
    return len(agg)
