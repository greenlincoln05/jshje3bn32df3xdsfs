"""Poll the currently open KXBTC15M market's orderbook and append it to a CSV, going forward.

Kalshi has no historical order-book endpoint, so this is the only way to get a real (not
modeled) in-window contract price path for the flip-vs-hold exit comparison in
trend_backtest.py: run this continuously for a while (hours to days) and it builds up a
timestamped record of top-of-book prices per window. This is read-only market data, needs no
credentials, and places no orders -- it is a research-only, much smaller cousin of the Phase 2
recorder described in docs/btc15m-bot-spec.md, not a replacement for it.

Usage:
    python btc-research/record_live_orderbook.py --seconds 3 --out btc-research/data/orderbook_live.csv
    # Ctrl-C to stop; append-safe to resume into the same file later.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from btcbot.config import KalshiEnv  # noqa: E402
from btcbot.kalshi_client import KalshiClient  # noqa: E402
from btcbot.market_discovery import find_current_market  # noqa: E402

FIELDNAMES = ["timestamp_utc", "ticker", "seconds_to_close", "yes_bid", "yes_ask", "no_bid", "no_ask"]


async def poll_forever(out_path: Path, interval_sec: float, series_ticker: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not out_path.exists() or out_path.stat().st_size == 0
    async with KalshiClient(KalshiEnv.PROD) as client, out_path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        while True:
            now = datetime.now(timezone.utc)
            market = await find_current_market(client, series_ticker, now=now)
            if market is not None:
                book = await client.get_orderbook(market.ticker)
                yes_bid = book.best_bid("yes")
                yes_ask = book.best_ask("yes")
                no_bid = book.best_bid("no")
                no_ask = book.best_ask("no")
                writer.writerow(
                    {
                        "timestamp_utc": now.isoformat(),
                        "ticker": market.ticker,
                        "seconds_to_close": market.seconds_to_close(now),
                        "yes_bid": yes_bid.price if yes_bid else "",
                        "yes_ask": yes_ask.price if yes_ask else "",
                        "no_bid": no_bid.price if no_bid else "",
                        "no_ask": no_ask.price if no_ask else "",
                    }
                )
                f.flush()
            await asyncio.sleep(interval_sec)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=3.0, help="poll interval")
    parser.add_argument("--series", default="KXBTC15M")
    parser.add_argument("--out", type=Path, default=Path("btc-research/data/orderbook_live.csv"))
    args = parser.parse_args()

    try:
        asyncio.run(poll_forever(args.out, args.seconds, args.series))
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
