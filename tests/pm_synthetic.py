"""Synthetic Polymarket-vs-Binance data with a KNOWN reaction lag, for the pm_reaction tests.

Not market data and not a claim about how Polymarket behaves: BTC is a driftless random walk, and the
simulated Polymarket Up price is the lognormal fair value of BTC as it was ``lag`` seconds ago, printed at a
random subset of seconds with a half-spread of bounce. The tests only check that the analysis RECOVERS the lag
and the ordering of baselines it was built with -- a harness check, never a result.
"""

from __future__ import annotations

import math
import random
import sqlite3
from decimal import Decimal

from btcbot.model import normal_cdf
from btcbot.pm_history import init_pm_history_schema

SIGMA = 1e-4  # per-second, about 56% annualised


def make_db(
    conn: sqlite3.Connection,
    *,
    windows: int = 40,
    horizon_sec: int = 900,
    lag: int = 3,
    print_prob: float = 0.6,
    half_spread: float = 0.01,
    seed: int = 7,
    first_start: int = 1_790_000_100 - (1_790_000_100 % 900),
    flip_labels: int = 0,
) -> None:
    rng = random.Random(seed)
    init_pm_history_schema(conn)
    pre = 400
    t_begin = first_start - pre
    t_end = first_start + windows * horizon_sec + 30
    price = 60_000.0
    btc: dict[int, float] = {}
    rows = []
    for ts in range(t_begin, t_end):
        price *= math.exp(rng.gauss(0.0, SIGMA))
        btc[ts] = price
        vol = 1.0 + rng.random()
        rows.append((ts, str(price), str(price), str(price), f"{price:.2f}", f"{vol:.4f}", 10, f"{vol * rng.random():.4f}"))
    conn.executemany(
        "INSERT INTO btc_klines_1s (ts, open, high, low, close, volume, n_trades, taker_buy_volume) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )

    def instant(t: int) -> float:
        return float(f"{btc[t - 1]:.2f}")

    for w in range(windows):
        start = first_start + w * horizon_sec
        end = start + horizon_sec
        slug = f"btc-updown-15m-{start}"
        open_px = instant(start)
        result_up = instant(end) >= open_px
        if w < flip_labels:
            result_up = not result_up
        trades = []
        for ts in range(start - 120, end):
            if rng.random() > print_prob:
                continue
            seen = ts - lag  # a negative lag makes Polymarket "see the future": a planted clock error
            ref = instant(seen) if seen >= start else open_px
            tau = max(end - max(seen, start), 1)
            fair = normal_cdf(math.log(ref / open_px) / (SIGMA * math.sqrt(tau))) if seen >= start else 0.5
            fair = min(0.99, max(0.01, fair))
            buy = rng.random() < 0.5
            px = min(0.99, max(0.01, fair + (half_spread if buy else -half_spread)))
            outcome = "up" if rng.random() < 0.5 else "down"
            if outcome == "down":
                px, buy = round(1 - px, 4), not buy  # buying Down at 1-p is the mirror of selling Up at p
            trades.append((slug, ts, outcome, "BUY" if buy else "SELL", str(Decimal(str(round(px, 4)))), "10", f"0x{w:04x}{ts}"))
        conn.execute(
            "INSERT INTO pm_hist_markets (slug, horizon_sec, condition_id, up_token_id, down_token_id, window_start,"
            " window_end, result_up, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'x')",
            (slug, horizon_sec, f"0xc{w}", f"up{w}", f"down{w}", start, end, 1 if result_up else 0),
        )
        # At most one print per second here, so each becomes its own per-second tape row (n = 1).
        conn.executemany(
            "INSERT INTO pm_hist_tape (slug, ts, outcome, side, n, size, notional) VALUES (?, ?, ?, ?, 1, ?, ?)",
            [(sl, ts, o, sd, size, str(Decimal(price) * Decimal(size))) for sl, ts, o, sd, price, size, _tx in trades],
        )
        conn.execute(
            "INSERT INTO pm_hist_progress (slug, status, trade_count, truncated, fetched_at) VALUES (?, 'done', ?, 0, 'x')",
            (slug, len(trades)),
        )
    conn.commit()
