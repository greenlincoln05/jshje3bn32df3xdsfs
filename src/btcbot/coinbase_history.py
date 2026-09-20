"""Public, unauthenticated Coinbase Exchange REST candles: 1-minute BTC-USD history for the larger,
market-level (not tick-level) ML entry training set (docs/research/ml-layers-handoff.md).

This session's own environment cannot reach Coinbase (CLAUDE.md, docs/running-live.md): built and tested
here entirely via httpx.MockTransport, never run against the real network from this session -- the owner
runs ``btcbot download-history`` on their own machine, the same division as ``record``/``stream``.

docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductcandles: ``GET /products/{id}/candles``
with ``start``/``end`` (ISO 8601) and ``granularity`` (seconds; 60 = one minute); each row is
``[time, low, high, open, close, volume]``, and the endpoint returns at most 300 rows per call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
GRANULARITY_SEC = 60
MAX_CANDLES_PER_REQUEST = 300


class CoinbaseHistoryError(Exception):
    """A candles response could not be parsed, or the exchange returned an error."""


@dataclass(frozen=True, slots=True)
class Candle:
    start: datetime  # UTC, the candle's opening minute
    low: Decimal
    high: Decimal
    open: Decimal
    close: Decimal
    volume: Decimal


def _parse_candle(row: Any) -> Candle:
    try:
        ts, low, high, open_, close, volume = row
        return Candle(
            start=datetime.fromtimestamp(float(ts), tz=timezone.utc),
            low=Decimal(str(low)), high=Decimal(str(high)), open=Decimal(str(open_)),
            close=Decimal(str(close)), volume=Decimal(str(volume)),
        )
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise CoinbaseHistoryError(f"malformed candle row: {row!r}: {exc}") from exc


async def fetch_candles(
    client: httpx.AsyncClient, *, start: datetime, end: datetime, granularity: int = GRANULARITY_SEC,
) -> list[Candle]:
    """One page (at most 300 candles) of BTC-USD candles over ``[start, end)``, oldest first. Use
    :func:`fetch_candle_history` for a range wider than one page."""
    params = {
        "start": start.astimezone(timezone.utc).isoformat(), "end": end.astimezone(timezone.utc).isoformat(),
        "granularity": str(granularity),
    }
    response = await client.get(COINBASE_CANDLES_URL, params=params)
    if response.status_code != 200:
        raise CoinbaseHistoryError(f"Coinbase candles request failed: {response.status_code} {response.text[:200]}")
    try:
        rows = response.json()
    except ValueError as exc:
        raise CoinbaseHistoryError(f"candles response was not JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise CoinbaseHistoryError("candles response: expected a JSON array")
    candles = [_parse_candle(row) for row in rows]
    candles.sort(key=lambda c: c.start)
    return candles


async def fetch_candle_history(
    client: httpx.AsyncClient,
    *,
    start: datetime,
    end: datetime,
    granularity: int = GRANULARITY_SEC,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    pause_sec: float = 0.2,
) -> list[Candle]:
    """Pages across ``[start, end)`` in chunks of at most ``MAX_CANDLES_PER_REQUEST`` candles, oldest first
    overall. A short pause between pages is a courtesy to a public, unauthenticated endpoint this project
    holds no key or rate-limit agreement for."""
    if end <= start:
        raise CoinbaseHistoryError("end must be after start")
    all_candles: list[Candle] = []
    chunk = timedelta(seconds=granularity * MAX_CANDLES_PER_REQUEST)
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + chunk, end)
        all_candles.extend(await fetch_candles(client, start=cursor, end=chunk_end, granularity=granularity))
        cursor = chunk_end
        if cursor < end:
            await sleep(pause_sec)
    return all_candles
