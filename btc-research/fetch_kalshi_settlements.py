"""Fetch real KXBTC15M settlement ground truth from Kalshi's public API.

This is the "live Kalshi data" piece the Coinbase-only backtest was missing: for each past
15-minute window, Kalshi itself records the exact opening BRTI average (``floor_strike``) and
the exact settlement outcome (``expiration_value``, ``result``). Both are public -- no API key
needed, same as ``btcbot discover`` -- and reusing ``btcbot.kalshi_client.KalshiClient`` here
gets pagination, retry/backoff and Decimal-safe parsing for free instead of re-implementing them.

This does NOT give you the in-window contract price path (bid/ask over the life of a window):
Kalshi has no historical order-book endpoint, so that can only be captured going forward by
polling ``get_orderbook`` while a window is open (see ``record_live_orderbook.py`` in this
directory) or by running the Phase 2 recorder. Settlement history alone already removes the
biggest approximation in trend_backtest.py: it no longer has to guess win/loss from a Coinbase
close price.

Usage:
    python btc-research/fetch_kalshi_settlements.py --limit 1000 --out btc-research/data/kalshi_settlements.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from btcbot.config import KalshiEnv  # noqa: E402
from btcbot.kalshi_client import KalshiClient  # noqa: E402


async def fetch_settled(limit: int) -> list[dict]:
    rows: list[dict] = []
    async with KalshiClient(KalshiEnv.PROD) as client:
        markets = await client.list_markets(series_ticker="KXBTC15M", status="settled")
        for market in markets[:limit] if limit else markets:
            raw = market.raw
            rows.append(
                {
                    "ticker": market.ticker,
                    "open_time": market.open_time.isoformat(),
                    "close_time": market.close_time.isoformat(),
                    "floor_strike": market.strike,
                    "expiration_value": raw.get("expiration_value"),
                    "result": raw.get("result"),
                }
            )
    return rows


def write_csv(rows: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: r["open_time"])
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "open_time", "close_time", "floor_strike", "expiration_value", "result"])
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=1000, help="most recent N settled windows, 0 for all available")
    parser.add_argument("--out", type=Path, default=Path("btc-research/data/kalshi_settlements.csv"))
    args = parser.parse_args()

    rows = asyncio.run(fetch_settled(args.limit))
    write_csv(rows, args.out)
    print(f"wrote {len(rows)} settled windows to {args.out}")


if __name__ == "__main__":
    main()
