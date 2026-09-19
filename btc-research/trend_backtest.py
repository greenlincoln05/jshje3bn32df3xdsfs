"""Backtest a multi-timeframe trend-following strategy against KXBTC15M-style windows.

This is exploratory research, separate from the phase-gated bot in src/btcbot. It
answers two questions from watching the market "freehand":

1. Does agreement across the 15m/30m/1h/24h trend predict the 15-minute settlement
   direction well enough to matter?
2. Given a losing position, is it better to hold to settlement or cut and flip sides
   when the move against you gets bad enough and there is still time left?

Important limitation: the in-window contract-price path used for the exit comparison is
*modeled*, not observed: it reuses the spec's v1 fair-probability approximation (Phi(d) with
realized EWMA volatility) driven by the real BTC/USD price path from Coinbase, because Kalshi
has no historical order-book endpoint to backtest real fills against (see
record_live_orderbook.py to start capturing that going forward). Entry price is a fixed
assumption you pass in (default $0.60, matching the "60 cents a piece" scenario).

Pass --kalshi-csv (from fetch_kalshi_settlements.py) to replace the Coinbase-derived strike and
win/loss outcome with Kalshi's own recorded floor_strike/expiration_value/result for any window
where that ground truth is available -- Coinbase is then only used for the trend signal and
volatility, not for deciding who actually won. Treat results as a sanity check on the exit
logic, not a profitability claim -- CLAUDE.md is explicit that no profitability claim is valid
without recorded, out-of-sample Kalshi results.

Usage:
    python btc-research/trend_backtest.py --csv btc-research/data/btc_1m.csv \
        --kalshi-csv btc-research/data/kalshi_settlements.csv
"""
from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

WINDOW_MIN = 15
LOOKBACKS_MIN = {"15m": 15, "30m": 30, "1h": 60, "24h": 24 * 60}
FEE_COEFFICIENT = 0.07  # from the spec's quadratic fee schedule: 0.07 * n * P * (1-P)


@dataclass(frozen=True)
class Candle:
    ts: datetime
    close: float


def load_candles(csv_path: Path) -> list[Candle]:
    rows: list[Candle] = []
    with csv_path.open() as f:
        for row in csv.DictReader(f):
            ts = datetime.fromisoformat(row["timestamp_utc"]).astimezone(timezone.utc)
            rows.append(Candle(ts=ts, close=float(row["close"])))
    rows.sort(key=lambda c: c.ts)
    return rows


def load_kalshi_ground_truth(csv_path: Path) -> dict[datetime, tuple[float, bool]]:
    """Real (strike, settled_yes) per window, keyed by open_time truncated to the minute.

    From fetch_kalshi_settlements.py. Rows missing floor_strike/expiration_value (settlement
    still pending, or a field Kalshi hasn't backfilled) are skipped rather than guessed at.
    """
    ground_truth: dict[datetime, tuple[float, bool]] = {}
    with csv_path.open() as f:
        for row in csv.DictReader(f):
            if not row["floor_strike"] or not row["result"]:
                continue
            open_time = datetime.fromisoformat(row["open_time"]).astimezone(timezone.utc).replace(second=0, microsecond=0)
            ground_truth[open_time] = (float(row["floor_strike"]), row["result"] == "yes")
    return ground_truth


def phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def ewma_sigma(closes: list[float], span: int) -> float:
    """Per-minute EWMA volatility of log returns, sized like vol_window_sec=900 (15 candles)."""
    if len(closes) < 2:
        return 0.0
    alpha = 2.0 / (span + 1.0)
    var = 0.0
    prev = closes[0]
    initialized = False
    for c in closes[1:]:
        r = math.log(c / prev)
        var = r * r if not initialized else alpha * r * r + (1 - alpha) * var
        initialized = True
        prev = c
    return math.sqrt(var)


def model_price(spot: float, strike: float, tau_min: float, sigma_per_min: float) -> float:
    """v1 fair-probability approximation from docs/btc15m-bot-spec.md section 4."""
    tau_eff = max(tau_min - 0.5, 1 / 60)  # 30s lock-in, in minutes
    if sigma_per_min <= 0:
        return 1.0 if spot >= strike else 0.0
    d = math.log(spot / strike) / (sigma_per_min * math.sqrt(tau_eff))
    return min(0.98, max(0.02, phi(d)))


def fee(contracts: float, price: float) -> float:
    return FEE_COEFFICIENT * contracts * price * (1 - price)


def trend_signal(closes_by_ts: dict[datetime, float], t_enter: datetime) -> str | None:
    """'yes' if all lookbacks point up, 'no' if all point down, else None (mixed/no data)."""
    if t_enter not in closes_by_ts:
        return None
    now = closes_by_ts[t_enter]
    directions = []
    for minutes in LOOKBACKS_MIN.values():
        past_ts = t_enter.replace(microsecond=0)
        past_ts = past_ts.fromtimestamp(past_ts.timestamp() - minutes * 60, tz=timezone.utc)
        past = closes_by_ts.get(past_ts)
        if past is None:
            return None
        directions.append(1 if now >= past else -1)
    if all(d > 0 for d in directions):
        return "yes"
    if all(d < 0 for d in directions):
        return "no"
    return None


@dataclass
class TradeResult:
    window_open: datetime
    side: str
    settled_yes: bool
    hold_pnl: float
    flip_pnl: float
    flipped: bool


def simulate_window(
    closes_by_ts: dict[datetime, float],
    window_open: datetime,
    entry_minute: int,
    entry_price: float,
    flip_threshold: float,
    min_minutes_to_flip: float,
    vol_span: int,
    contracts: float,
    ground_truth: dict[datetime, tuple[float, bool]] | None = None,
) -> TradeResult | None:
    window_close = window_open.fromtimestamp(window_open.timestamp() + WINDOW_MIN * 60, tz=timezone.utc)
    t_enter = window_open.fromtimestamp(window_open.timestamp() + entry_minute * 60, tz=timezone.utc)

    real = ground_truth.get(window_open) if ground_truth else None
    if real is not None:
        strike, settled_yes = real
    else:
        strike = closes_by_ts.get(window_open)
        settle_price = closes_by_ts.get(window_close)
        if strike is None or settle_price is None:
            return None
        settled_yes = settle_price >= strike

    side = trend_signal(closes_by_ts, t_enter)
    if side is None:
        return None

    settled_side_won = settled_yes if side == "yes" else not settled_yes
    hold_pnl = (1.0 if settled_side_won else 0.0) - entry_price
    hold_pnl -= fee(contracts, entry_price)

    lookback_closes = [
        v for ts, v in sorted(closes_by_ts.items())
        if window_open.fromtimestamp(window_open.timestamp() - vol_span * 60, tz=timezone.utc) <= ts <= t_enter
    ]
    sigma = ewma_sigma(lookback_closes, span=vol_span) or 1e-6

    flipped = False
    flip_pnl = hold_pnl
    minute = entry_minute + 1
    while minute <= WINDOW_MIN:
        t = window_open.fromtimestamp(window_open.timestamp() + minute * 60, tz=timezone.utc)
        spot = closes_by_ts.get(t)
        minutes_left = WINDOW_MIN - minute
        if spot is not None and minutes_left >= min_minutes_to_flip:
            p_yes = model_price(spot, strike, minutes_left, sigma)
            p_side = p_yes if side == "yes" else (1 - p_yes)
            if p_side <= flip_threshold:
                other_side = "no" if side == "yes" else "yes"
                other_won = (not settled_yes) if other_side == "no" else settled_yes
                exit_price = 1 - p_side  # price to close the losing leg
                new_entry_price = 1 - exit_price  # price paid to enter the other side
                flip_pnl = (1.0 if other_won else 0.0) - new_entry_price
                flip_pnl -= fee(contracts, entry_price) + fee(contracts, new_entry_price)
                flipped = True
                break
        minute += 1

    return TradeResult(window_open, side, settled_yes, hold_pnl, flip_pnl, flipped)


def run(args: argparse.Namespace) -> None:
    candles = load_candles(args.csv)
    closes_by_ts = {c.ts: c.close for c in candles}
    if not candles:
        print("no data")
        return

    ground_truth = load_kalshi_ground_truth(args.kalshi_csv) if args.kalshi_csv else None
    if ground_truth is not None:
        print(f"using {len(ground_truth)} real Kalshi-settled windows as ground truth (from {args.kalshi_csv})")

    first, last = candles[0].ts, candles[-1].ts
    window_starts = []
    t = first.replace(minute=(first.minute // WINDOW_MIN) * WINDOW_MIN, second=0, microsecond=0)
    while t + (last - first) * 0 <= last:  # advance until past last candle
        t = t.fromtimestamp(t.timestamp() + WINDOW_MIN * 60, tz=timezone.utc)
        if t > last:
            break
        window_starts.append(t.fromtimestamp(t.timestamp() - WINDOW_MIN * 60, tz=timezone.utc))

    results = [
        r for r in (
            simulate_window(
                closes_by_ts, w, args.entry_minute, args.entry_price,
                args.flip_threshold, args.min_minutes_to_flip, args.vol_span, args.contracts,
                ground_truth,
            )
            for w in window_starts
        ) if r is not None
    ]

    if not results:
        print("no windows had complete data + a one-sided trend signal")
        return

    split = int(len(results) * 0.7)
    train, test = results[:split], results[split:]

    def summarize(label: str, rows: list[TradeResult]) -> None:
        if not rows:
            print(f"{label}: no trades")
            return
        n = len(rows)
        hold_avg = sum(r.hold_pnl for r in rows) / n
        flip_avg = sum(r.flip_pnl for r in rows) / n
        win_rate = sum(1 for r in rows if r.settled_yes == (r.side == "yes")) / n
        flip_rate = sum(1 for r in rows if r.flipped) / n
        print(
            f"{label}: n={n} win_rate={win_rate:.2%} flip_rate={flip_rate:.2%} "
            f"avg_pnl_hold={hold_avg:+.4f} avg_pnl_flip={flip_avg:+.4f}"
        )

    print("Trend-following entry, hold vs. flip-on-adverse-move exit (research only, not a profitability claim)")
    summarize("train (in-sample, do not act on this alone)", train)
    summarize("test (out-of-sample)", test)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=Path("btc-research/data/btc_1m.csv"))
    parser.add_argument(
        "--kalshi-csv", type=Path, default=None,
        help="from fetch_kalshi_settlements.py; overrides strike/settlement with real Kalshi ground truth",
    )
    parser.add_argument("--entry-minute", type=int, default=6, help="minutes into the window to enter (default: 9 min left)")
    parser.add_argument("--entry-price", type=float, default=0.60)
    parser.add_argument("--flip-threshold", type=float, default=0.20, help="flip when model-implied p(your side) drops to this")
    parser.add_argument("--min-minutes-to-flip", type=float, default=2.0, help="never flip with less time left than this")
    parser.add_argument("--vol-span", type=int, default=15, help="EWMA span in minutes, matches vol_window_sec=900")
    parser.add_argument("--contracts", type=float, default=1.0)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
