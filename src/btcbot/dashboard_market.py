"""Bounded, read-only market data for the local dashboard.

The fast quote path performs index seeks and decodes one order book. Chart history
uses at most 400 time samples across the selected window, never loading a growing
recording into Python. Timestamps describe recorder receipt, not exchange time.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

HISTORY_LIMIT = 400
STALE_AFTER_MS = 2500


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def order_book_depth(book: dict[str, Any]) -> dict[str, Any]:
    """Build executable YES depth; a NO bid at q implies a YES ask at 1-q.

    Prices/notionals are dollars, sizes are contracts, and imbalance is in [-1,1].
    Cumulative values run outward from the best price on each side. Zero quantity
    and malformed levels do not contribute liquidity or a misleading best price.
    """
    invalid_levels = 0

    def levels(side: str, *, implied: bool = False) -> list[dict[str, float]]:
        nonlocal invalid_levels
        aggregated: dict[Decimal, Decimal] = {}
        raw = book.get(side, [])
        if not isinstance(raw, (list, tuple)):
            invalid_levels += 1
            raw = []
        for level in raw:
            if not isinstance(level, (list, tuple)) or len(level) != 2:
                invalid_levels += 1
                continue
            price, size = _decimal(level[0]), _decimal(level[1])
            if price is None or size is None or not 0 <= price <= 1 or size < 0:
                invalid_levels += 1
                continue
            if size == 0:
                continue
            price = 1 - price if implied else price
            aggregated[price] = aggregated.get(price, Decimal(0)) + size
        total_size = total_notional = Decimal(0)
        result = []
        for price, size in sorted(aggregated.items(), reverse=not implied):
            total_size += size
            total_notional += price * size
            result.append({"price": float(price), "size": float(size),
                           "cumulative_size": float(total_size), "notional": float(price * size),
                           "cumulative_notional": float(total_notional)})
        return result

    bids, asks = levels("yes"), levels("no", implied=True)
    bid_size = bids[-1]["cumulative_size"] if bids else 0.0
    ask_size = asks[-1]["cumulative_size"] if asks else 0.0
    best_bid = bids[0]["price"] if bids else None
    best_ask = asks[0]["price"] if asks else None
    spread = mid = microprice = None
    if bids and asks:
        spread = float(Decimal(str(best_ask)) - Decimal(str(best_bid)))
        mid = float((Decimal(str(best_bid)) + Decimal(str(best_ask))) / 2)
        microprice = (best_ask * bids[0]["size"] + best_bid * asks[0]["size"]) / (
            bids[0]["size"] + asks[0]["size"])
    top_bid = sum(level["size"] for level in bids[:5])
    top_ask = sum(level["size"] for level in asks[:5])
    return {
        "bids": bids, "asks": asks, "best_bid": best_bid, "best_ask": best_ask,
        "spread": spread, "spread_cents": None if spread is None else round(spread * 100, 8),
        "mid": mid, "microprice": microprice, "bid_size": bid_size, "ask_size": ask_size,
        "bid_notional": bids[-1]["cumulative_notional"] if bids else 0.0,
        "ask_notional": asks[-1]["cumulative_notional"] if asks else 0.0,
        "imbalance": (bid_size - ask_size) / (bid_size + ask_size) if bid_size + ask_size else None,
        "top5_imbalance": (top_bid - top_ask) / (top_bid + top_ask) if top_bid + top_ask else None,
        "crossed": spread is not None and spread < 0, "invalid_levels": invalid_levels,
        "ask_source": "implied_from_no_bids", "price_unit": "USD", "size_unit": "contracts",
    }


class _Reader:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._columns: dict[str, set[str]] = {}

    def columns(self, table: str) -> set[str]:
        if table not in self._columns:
            self._columns[table] = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        return self._columns[table]

    def projection(self, table: str, names: tuple[str, ...]) -> str:
        columns = self.columns(table)
        return ", ".join(name if name in columns else f"NULL AS {name}" for name in names)

    def latest(self, table: str, ticker: str, names: tuple[str, ...]) -> dict[str, Any]:
        columns = self.columns(table)
        if "ticker" not in columns:
            return {}
        order = "poll_ts DESC, rowid DESC" if "poll_ts" in columns else "rowid DESC"
        row = self.conn.execute(
            f"SELECT {self.projection(table, names)} FROM {table} WHERE ticker=? ORDER BY {order} LIMIT 1",
            (ticker,),
        ).fetchone()
        return dict(zip(names, row)) if row else {}


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return None


def _age_ms(timestamp: Any, now: datetime) -> float | None:
    parsed = _parse_time(timestamp)
    return round((now - parsed).total_seconds() * 1000, 3) if parsed else None


def _spot_bounds(reader: _Reader, ticker: str, meta: dict[str, Any], book: dict[str, Any]) -> tuple[str, str] | None:
    start, end = meta.get("open_time"), meta.get("close_time")
    if start and end:
        return start, end
    if not book.get("poll_ts"):
        return None
    row = reader.conn.execute(
        "SELECT poll_ts FROM orderbook_snapshots WHERE ticker=? ORDER BY poll_ts LIMIT 1", (ticker,),
    ).fetchone()
    return (start or row[0], end or book["poll_ts"]) if row else None


def _quote(reader: _Reader, ticker: str, now: datetime) -> tuple[dict[str, Any], dict[str, Any], tuple[str, str] | None]:
    meta = reader.latest("market_state", ticker,
                         ("status", "strike", "open_time", "close_time", "volume", "open_interest"))
    snapshot = reader.latest("orderbook_snapshots", ticker, ("poll_ts", "book_json", "latency_ms"))
    corrupt_book = False
    try:
        decoded = json.loads(snapshot.get("book_json") or '{"yes":[],"no":[]}')
        if not isinstance(decoded, dict):
            raise ValueError("book is not an object")
        book = {"yes": decoded.get("yes", []), "no": decoded.get("no", [])}
    except (ValueError, TypeError):
        book, corrupt_book = {"yes": [], "no": []}, True
    bounds = _spot_bounds(reader, ticker, meta, snapshot)
    spot = None
    if bounds and {"price", "receive_ts"} <= reader.columns("spot_ticks"):
        spot = reader.conn.execute(
            "SELECT price, receive_ts FROM spot_ticks WHERE receive_ts>=? AND receive_ts<=? "
            "ORDER BY receive_ts DESC LIMIT 1", bounds,
        ).fetchone()
    book_age = _age_ms(snapshot.get("poll_ts"), now)
    spot_age = _age_ms(spot[1], now) if spot else None
    return ({
        "ticker": ticker, "book": book, "book_ts": snapshot.get("poll_ts"),
        "book_latency_ms": snapshot.get("latency_ms"), "depth": order_book_depth(book),
        "spot": spot[0] if spot else None, "spot_ts": spot[1] if spot else None,
        "server_time_ms": round(now.timestamp() * 1000, 3),
        "book_age_ms": book_age, "spot_age_ms": spot_age,
        "book_stale": book_age is None or book_age > STALE_AFTER_MS or book_age < -1000,
        "spot_stale": spot_age is None or spot_age > STALE_AFTER_MS or spot_age < -1000,
        "timing": {"clock_source": "server_wall_clock", "book_timestamp_source": "recorder_receive_time",
                   "latency_source": "REST_request_duration", "stale_after_ms": STALE_AFTER_MS,
                   "exchange_clock_synchronized": False},
        "book_error": "Recorded order book could not be decoded" if corrupt_book else None,
    }, meta, bounds)


def market_quote(db_path: Path, ticker: str, *, now: datetime | None = None) -> dict[str, Any]:
    """One current snapshot; no chart history, trade history, or network calls."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=0.1)
    try:
        return _quote(_Reader(conn), ticker, now)[0]
    finally:
        conn.close()


def _history(reader: _Reader, table: str, timestamp: str, fields: tuple[str, ...],
             where: str, params: tuple[Any, ...], limit: int) -> tuple[list[list[Any]], bool]:
    """Sample the whole interval with bounded index probes and result sizes.

    Reading limit+1 rows first cheaply preserves every observation in short
    windows. Longer windows select the latest observation at each time target.
    """
    if timestamp not in reader.columns(table):
        return [], False
    projection = reader.projection(table, (timestamp, *fields))
    recent = reader.conn.execute(
        f"SELECT {projection} FROM {table} WHERE {where} ORDER BY {timestamp} DESC LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    if len(recent) <= limit:
        return [list(row) for row in reversed(recent)], False
    first = reader.conn.execute(
        f"SELECT {timestamp} FROM {table} WHERE {where} ORDER BY {timestamp} LIMIT 1", params,
    ).fetchone()[0]
    last = recent[0][0]
    start, end = _parse_time(first), _parse_time(last)
    if start is None or end is None or start >= end:
        return [list(row) for row in reversed(recent[:limit])], True
    targets = [first] + [(start + (end - start) * (i / (limit - 1))).isoformat()
                         for i in range(1, limit - 1)] + [last]
    placeholders = ",".join("(?)" for _ in targets)
    # rowid equality ensures each target performs one index seek, not a join
    # against every recorded row. DISTINCT collapses gaps with no observations.
    rows = reader.conn.execute(
        f"WITH targets(ts) AS (VALUES {placeholders}) "
        f"SELECT {projection} FROM {table} WHERE rowid IN ("
        f"SELECT (SELECT rowid FROM {table} WHERE {where} AND {timestamp}<=targets.ts "
        f"ORDER BY {timestamp} DESC LIMIT 1) FROM targets) ORDER BY {timestamp} LIMIT ?",
        (*targets, *params, limit),
    ).fetchall()
    return [list(row) for row in rows], True


def _tickers(reader: _Reader) -> list[str]:
    if not {"ticker", "poll_ts"} <= reader.columns("orderbook_snapshots"):
        return []
    # Skip from one distinct ticker to the next using (ticker,poll_ts), rather
    # than GROUP BY over every snapshot in a multi-day recording.
    return [row[0] for row in reader.conn.execute(
        "WITH RECURSIVE names(ticker) AS ("
        "SELECT MIN(ticker) FROM orderbook_snapshots UNION ALL "
        "SELECT (SELECT MIN(ticker) FROM orderbook_snapshots WHERE ticker>names.ticker) "
        "FROM names WHERE ticker IS NOT NULL) "
        "SELECT ticker FROM names WHERE ticker IS NOT NULL ORDER BY "
        "(SELECT poll_ts FROM orderbook_snapshots WHERE ticker=names.ticker ORDER BY poll_ts DESC LIMIT 1) "
        "DESC LIMIT 50")]


def market_view(db_path: Path, ticker: str | None = None, *, history_limit: int = HISTORY_LIMIT,
                now: datetime | None = None) -> dict[str, Any]:
    """Full-window charts and selected trades, with bounded SQL result sets."""
    history_limit = max(2, min(HISTORY_LIMIT, int(history_limit)))
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=0.1)
    try:
        reader = _Reader(conn)
        tickers = _tickers(reader)
        if not tickers:
            return {"tickers": [], "windows": [], "ticker": None}
        ticker = ticker if ticker in tickers else tickers[0]
        quote, meta, bounds = _quote(reader, ticker, now)
        mids, mids_sampled = _history(reader, "orderbook_snapshots", "poll_ts",
                                     ("yes_bid_price", "yes_ask_price"), "ticker=?", (ticker,), history_limit)
        spots, spots_sampled = _history(reader, "spot_ticks", "receive_ts", ("price",),
                                       "receive_ts>=? AND receive_ts<=?", bounds, history_limit) if bounds else ([], False)
        settlement = reader.latest("settlements", ticker, ("result", "settled_avg"))
        trade_fields = ("ticker", "side", "size", "entry_price", "entry_ts", "fee_paid",
                        "p_side_at_entry", "result", "pnl_usd")
        trades, trade_counts = [], {}
        if "ticker" in reader.columns("trades"):
            trade_projection = reader.projection("trades", trade_fields)
            trade_order = "entry_ts DESC" if "entry_ts" in reader.columns("trades") else "rowid DESC"
            trades = [dict(zip(trade_fields, row)) for row in conn.execute(
                f"SELECT {trade_projection} FROM trades WHERE ticker=? ORDER BY {trade_order} LIMIT 200", (ticker,))]
            marks = ",".join("?" for _ in tickers)
            trade_counts = dict(conn.execute(
                f"SELECT ticker,COUNT(*) FROM trades WHERE ticker IN ({marks}) GROUP BY ticker", tickers))
        windows = []
        for name in tickers:
            close = reader.latest("market_state", name, ("close_time",))
            result = reader.latest("settlements", name, ("result",))
            windows.append({"ticker": name, "close_time": close.get("close_time"),
                            "result": result.get("result"), "trades": trade_counts.get(name, 0)})
        return {
            **quote, **{name: meta.get(name) for name in (
                "status", "strike", "open_time", "close_time", "volume", "open_interest")},
            "tickers": tickers, "windows": windows, "mid_series": mids, "spot_series": spots,
            "history": {"max_points": history_limit, "mid_sampled": mids_sampled,
                        "spot_sampled": spots_sampled, "sampling": "last_observation_at_uniform_time_targets"},
            "settlement": settlement or None, "trades": trades,
            "trades_truncated": trade_counts.get(ticker, 0) > len(trades),
        }
    finally:
        conn.close()
