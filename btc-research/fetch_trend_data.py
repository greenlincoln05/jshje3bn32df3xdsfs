"""Fetch historical BTC-USD 1-minute candles from Coinbase's public REST API.

Standalone research script, separate from the phase-gated bot in src/btcbot.
Coinbase is the same public spot feed the build spec already names as the
Phase 2 proxy for BRTI, so this reuses an already-approved source rather than
introducing a new one. No credentials are needed; the endpoint is public.

Usage:
    python btc-research/fetch_trend_data.py --hours 24 --out btc-research/data/btc_1m.csv

Coinbase caps each request at 300 candles, so a large --hours value is
fetched in multiple requests, oldest first, and rate-limited politely.
"""
from __future__ import annotations

import argparse
import csv
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
GRANULARITY_SEC = 60
MAX_CANDLES_PER_REQUEST = 300


def fetch_range(client: httpx.Client, start: datetime, end: datetime) -> list[list[float]]:
    resp = client.get(
        CANDLES_URL,
        params={
            "start": start.isoformat(),
            "end": end.isoformat(),
            "granularity": GRANULARITY_SEC,
        },
        headers={"User-Agent": "btc15m-bot-research/0.1"},
    )
    resp.raise_for_status()
    return resp.json()  # each row: [time, low, high, open, close, volume]


def fetch_hours(hours: float) -> list[list[float]]:
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(hours=hours)
    chunk = timedelta(seconds=GRANULARITY_SEC * MAX_CANDLES_PER_REQUEST)

    rows: list[list[float]] = []
    with httpx.Client(timeout=10.0) as client:
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + chunk, end)
            rows.extend(fetch_range(client, cursor, chunk_end))
            cursor = chunk_end
            time.sleep(0.35)  # stay well under Coinbase's public rate limit
    return rows


def write_csv(rows: list[list[float]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Coinbase returns newest first and can overlap between chunks; dedupe and sort.
    by_time = {int(r[0]): r for r in rows}
    with out_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_utc", "open", "high", "low", "close", "volume"])
        for ts in sorted(by_time):
            _, low, high, open_, close, volume = by_time[ts]
            when = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            writer.writerow([when, open_, high, low, close, volume])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=24.0, help="how many hours of history to fetch")
    parser.add_argument("--out", type=Path, default=Path("btc-research/data/btc_1m.csv"))
    args = parser.parse_args()

    rows = fetch_hours(args.hours)
    write_csv(rows, args.out)
    print(f"wrote {len(rows)} candles to {args.out}")


if __name__ == "__main__":
    main()
