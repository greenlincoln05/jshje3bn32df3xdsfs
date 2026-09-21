"""Fill-realism check against the public trade tape (``btcbot fillcheck``).

The paper broker decides a resting bid filled from order-book size changes alone. The tape (``trade_tape``, recorded by
``btcbot record``/``paper``/``demo`` since the tape was added) shows who really crossed. For each recorded fill this asks:
did a taker actually print at or through our price around that time, and was there enough volume for our size?
A resting YES bid at ``p`` is hit by a taker BUYING NO with ``yes_price <= p`` (a NO bid at ``q`` by a taker buying YES with
``no_price <= q``). A fill with no such print is one paper credited that real flow would not have delivered.
Offline, read-only, no network and no key. Recordings made before the tape existed have no tape and cannot be checked.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from btcbot.models import parse_time


class FillCheckError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class FillVerdict:
    ticker: str
    side: str
    price: Decimal
    size: Decimal
    at: str
    prints: int          # qualifying tape prints in the window
    volume: Decimal      # contracts those prints carried
    supported: bool      # at least one qualifying print
    covered: bool        # enough volume for our whole size


def check_fills(db_path: str | Path, *, before_sec: float = 120.0, after_sec: float = 5.0) -> list[FillVerdict]:
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "trade_tape" not in tables:
            raise FillCheckError("this recording has no trade_tape (recorded before the tape existed)")
        if "trades" not in tables:
            raise FillCheckError("this recording has no trades table (use a paper or demo database)")
        fills = conn.execute("SELECT ticker, side, entry_price, size, entry_ts FROM trades ORDER BY entry_ts").fetchall()
        out = []
        for ticker, side, price, size, at in fills:
            t0 = parse_time(at)
            lo = (t0 - timedelta(seconds=before_sec)).replace(microsecond=0).isoformat()
            hi = (t0 + timedelta(seconds=after_sec)).replace(microsecond=0).isoformat()
            price, size = Decimal(price), Decimal(size)
            taker, col = ("no", "yes_price") if side == "yes" else ("yes", "no_price")
            rows = conn.execute(
                f"SELECT contracts, prints FROM trade_tape WHERE ticker = ? AND taker_side = ? AND CAST({col} AS REAL) <= ? "
                "AND second_ts >= ? AND second_ts <= ?",
                (ticker, taker, float(price), lo, hi),
            ).fetchall()
            volume = sum((Decimal(str(r[0])) for r in rows), Decimal(0))
            out.append(FillVerdict(ticker, side, price, size, at, sum(r[1] for r in rows), volume, bool(rows), volume >= size))
        return out
    finally:
        conn.close()


def render(verdicts: list[FillVerdict]) -> str:
    n = len(verdicts)
    if not n:
        return "no fills recorded in this database"
    sup, cov = sum(v.supported for v in verdicts), sum(v.covered for v in verdicts)
    lines = [f"{n} recorded fills checked against the public trade tape",
             f"  a taker printed at/through our price near the fill: {sup}/{n} ({sup / n:.0%})",
             f"  ...with enough volume for our whole size:            {cov}/{n} ({cov / n:.0%})"]
    for v in verdicts:
        if not v.supported:
            lines.append(f"  NOT supported: {v.ticker[-8:]} {v.side} {v.size}@{v.price} at {v.at[11:19]} (no qualifying print)")
    lines.append("Paper fills without a print were credited by the book model only; treat those as optimistic. Small samples prove little.")
    return "\n".join(lines)
