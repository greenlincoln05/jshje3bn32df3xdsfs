"""Feature store: turns recorder databases into one flat, model-ready table (``btcbot features``).

One row per order-book snapshot (thinned to at most one per ``step_sec`` per window). Every FEATURE uses only
data recorded at or before that snapshot's timestamp (a test proves later data cannot change an earlier row);
the LABEL (``outcome_yes``) is the settled result and is the only column that looks forward, so training code must
treat it as the target and never as an input. Offline only: reads SQLite files, writes CSV, no network, no key.

Several recordings of the same window are never double counted: a window is taken whole from the database that
captured the most snapshots of it (same rule as ``btcbot.backtest.merge_replay_data``).
"""

from __future__ import annotations

import csv
import sqlite3
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence

from btcbot.backtest import SpotSeries, load_replay_data, parse_time

SPOT_LOOKBACKS_SEC = (60, 300, 900)
DEPTH_LEVELS = 3

COLUMNS = (
    "source", "ticker", "ts", "tau_sec", "strike", "spot", "spot_minus_strike",
    *(f"spot_move_{n}s" for n in SPOT_LOOKBACKS_SEC),
    "yes_bid", "yes_ask", "no_bid", "no_ask", "yes_spread", "yes_mid",
    "yes_bid_size", "no_bid_size", "yes_depth3", "no_depth3", "book_imbalance",
    "p_model", "p_blend", "sigma", "model_minus_market", "outcome_yes",
)


class FeatureError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _Preds:
    ts: list[float]
    rows: list[tuple[float, float, float, Decimal | None]]  # p_model, p_blend, sigma, market_mid


def _load_preds(conn: sqlite3.Connection) -> dict[str, _Preds]:
    try:
        rows = conn.execute(
            "SELECT ticker, ts, p_model, p_blend, sigma, market_mid FROM predictions ORDER BY ts"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, tuple[list[float], list]] = {}
    for ticker, ts, p_model, p_blend, sigma, mid in rows:
        ts_l, r_l = out.setdefault(ticker, ([], []))
        ts_l.append(parse_time(ts).timestamp())
        r_l.append((p_model, p_blend, sigma, None if mid is None else Decimal(mid)))
    return {t: _Preds(a, b) for t, (a, b) in out.items()}


def _asof(preds: _Preds | None, ts: float):
    """Latest prediction at or before ``ts`` (never a later one)."""
    if preds is None:
        return None
    i = bisect_right(preds.ts, ts) - 1
    return None if i < 0 else preds.rows[i]


def _depth(levels: Sequence, n: int) -> Decimal:
    return sum((lvl.size for lvl in levels[-n:]), Decimal(0))


def _f(x: Decimal | float | None) -> float | None:
    return None if x is None else float(x)


def build_rows(db_paths: Sequence[str | Path], *, step_sec: float = 5.0) -> list[dict]:
    if not db_paths:
        raise FeatureError("no databases given")
    parts = []
    for path in db_paths:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            parts.append((Path(path).name, load_replay_data(conn), _load_preds(conn)))
        finally:
            conn.close()
    counts: dict[str, tuple[int, int]] = {}
    for i, (_, data, _) in enumerate(parts):
        per: dict[str, int] = {}
        for s in data.snapshots:
            per[s.ticker] = per.get(s.ticker, 0) + 1
        for t, n in per.items():
            if n > counts.get(t, (0, -1))[0]:
                counts[t] = (n, i)
    windows: dict[str, tuple[Decimal, datetime]] = {}
    outcomes: dict[str, str] = {}
    for _, data, _ in parts:
        windows.update(data.windows)
        outcomes.update({t: s.result for t, s in data.settlements.items()})

    rows: list[dict] = []
    for i, (name, data, preds) in enumerate(parts):
        spot = SpotSeries(data.spot_ticks)
        last_kept: dict[str, float] = {}
        for snap in data.snapshots:
            t = snap.ticker
            if counts[t][1] != i or t not in windows:
                continue
            ts = snap.poll_ts.timestamp()
            if ts - last_kept.get(t, -1e18) < step_sec:
                continue
            strike, close = windows[t]
            tau = (close - snap.poll_ts).total_seconds()
            if tau < 0:
                continue  # post-close book
            last_kept[t] = ts
            book = snap.book
            yb, nb = book.best_bid("yes"), book.best_bid("no")
            ya, na = book.best_ask("yes"), book.best_ask("no")
            idx = bisect_right(spot._t, ts) - 1
            price = spot._p[idx] if idx >= 0 and ts - spot._t[idx] <= 5 else None
            yd, nd = _depth(book.yes_bids, DEPTH_LEVELS), _depth(book.no_bids, DEPTH_LEVELS)
            p = _asof(preds.get(t), ts)
            mid = book.mid("yes")
            row = {
                "source": name, "ticker": t, "ts": snap.poll_ts.isoformat(), "tau_sec": round(tau, 3),
                "strike": _f(strike), "spot": _f(price),
                "spot_minus_strike": None if price is None else _f(price - strike),
                "yes_bid": _f(yb.price) if yb else None, "yes_ask": _f(ya.price) if ya else None,
                "no_bid": _f(nb.price) if nb else None, "no_ask": _f(na.price) if na else None,
                "yes_spread": _f(book.spread("yes")), "yes_mid": _f(mid),
                "yes_bid_size": _f(yb.size) if yb else None, "no_bid_size": _f(nb.size) if nb else None,
                "yes_depth3": _f(yd), "no_depth3": _f(nd),
                "book_imbalance": None if yd + nd == 0 else _f((yd - nd) / (yd + nd)),
                "p_model": None if p is None else p[0], "p_blend": None if p is None else p[1],
                "sigma": None if p is None else p[2],
                "model_minus_market": None if p is None or mid is None else p[0] - float(mid),
                "outcome_yes": None if t not in outcomes else int(outcomes[t] == "yes"),
            }
            for n in SPOT_LOOKBACKS_SEC:
                row[f"spot_move_{n}s"] = _f(spot.move(snap.poll_ts, n))
            rows.append(row)
    rows.sort(key=lambda r: (r["ts"], r["ticker"]))
    return rows


def write_csv(rows: Iterable[dict], path: str | Path) -> int:
    n = 0
    with open(path, "w", newline="", encoding="ascii") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c) for c in COLUMNS})
            n += 1
    return n
