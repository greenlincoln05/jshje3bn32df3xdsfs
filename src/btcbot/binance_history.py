"""Public, unauthenticated Binance BTCUSDT 1-second klines: the BTC side of the Polymarket reaction dataset
(:mod:`btcbot.pm_reaction`, docs/research/polymarket-reaction-data.md).

Why Binance and why 1 second: Binance BTCUSDT spot is the deepest, fastest BTC venue, so it is where a BTC move
shows up first -- the "event" a short-window prediction market then reacts to. Published measurements put
Polymarket's BTC 15m quotes about 350 ms behind large Binance moves (OpenMarket, arXiv 2607.26245), so anything
coarser than 1 s (Coinbase's REST candles are 1 minute, :mod:`btcbot.coinbase_history`) cannot see the reaction
at all. It is NOT the settlement source: Polymarket resolves on the Chainlink BTC/USD data stream and Kalshi on
CF Benchmarks' BRTI, neither of which publishes a free second-by-second history. :mod:`btcbot.pm_reaction`'s
validity report measures how often a Binance-derived up/down agrees with Polymarket's own resolution, so that
basis is a measured number rather than an assumption.

Two sources, same rows:

- ``data.binance.vision`` daily archives (``data/spot/daily/klines/BTCUSDT/1s/BTCUSDT-1s-YYYY-MM-DD.zip``), each
  with a published ``.CHECKSUM`` (SHA-256) that :func:`fetch_daily_klines` verifies before parsing. A day is
  published the following day, so the current day is never there (HTTP 404 -> :class:`BinanceNotPublished`).
  Binance changed spot archive timestamps from milliseconds to MICROseconds on 2025-01-01; the parser detects
  the unit per row rather than trusting a date cut-over.
- ``data-api.binance.vision/api/v3/klines`` (Binance's public market-data-only REST host), 1,000 rows per call:
  the fallback for a day the archive does not have yet. ``api.binance.com`` itself refuses US IP addresses
  (HTTP 451); the data-api host and the archive are the ones meant for public data.

No key, no account, no order code of any kind -- read-only public market data, the same footing as the
Coinbase candles :mod:`btcbot.coinbase_history` already fetches.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx

ARCHIVE_BASE = "https://data.binance.vision"
REST_BASE = "https://data-api.binance.vision"
REST_MAX_ROWS = 1000


class BinanceHistoryError(Exception):
    """A kline file or response could not be fetched, verified or parsed."""


class BinanceNotPublished(BinanceHistoryError):
    """The daily archive for that day does not exist (yet): HTTP 404."""


@dataclass(frozen=True, slots=True)
class Kline1s:
    """One 1-second bar. ``ts`` is the bar's OPEN time in unix seconds, so ``close`` is the last price
    traded in ``[ts, ts + 1)`` -- "the price at instant T" is the close of the bar opening at ``T - 1``."""

    ts: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    n_trades: int
    taker_buy_volume: Decimal  # base-asset volume where the TAKER bought: (2 * this - volume) is signed flow


def _to_unix_seconds(raw: str | int) -> int:
    """Binance archive timestamps are milliseconds before 2025-01-01 and microseconds from then on; REST is
    milliseconds. Decide per value by magnitude (seconds ~1e9, ms ~1e12, us ~1e15) instead of by date."""
    value = int(raw)
    if value >= 10**14:
        return value // 1_000_000
    if value >= 10**11:
        return value // 1000
    return value


def _dec(raw: str, what: str) -> Decimal:
    try:
        out = Decimal(str(raw))
    except InvalidOperation:
        raise BinanceHistoryError(f"{what}: not a number: {raw!r}") from None
    if not out.is_finite():
        raise BinanceHistoryError(f"{what}: not finite: {raw!r}")
    return out


def _parse_row(row: list[str] | list, where: str) -> Kline1s:
    if len(row) < 11:
        raise BinanceHistoryError(f"{where}: expected at least 11 kline columns, got {len(row)}: {row!r}")
    try:
        ts = _to_unix_seconds(row[0])
        n_trades = int(row[8])
    except (TypeError, ValueError) as exc:
        raise BinanceHistoryError(f"{where}: bad open time / trade count in {row!r}: {exc}") from exc
    return Kline1s(
        ts=ts, open=_dec(row[1], f"{where} open"), high=_dec(row[2], f"{where} high"),
        low=_dec(row[3], f"{where} low"), close=_dec(row[4], f"{where} close"),
        volume=_dec(row[5], f"{where} volume"), n_trades=n_trades,
        taker_buy_volume=_dec(row[9], f"{where} taker_buy_volume"),
    )


def parse_kline_csv(text: str, *, where: str = "klines") -> list[Kline1s]:
    """Rows of a Binance kline CSV (spot archives have no header; a header row, if present, is skipped)."""
    out: list[Kline1s] = []
    for i, row in enumerate(csv.reader(io.StringIO(text))):
        if not row:
            continue
        if i == 0 and not row[0].strip().isdigit():
            continue  # header
        out.append(_parse_row(row, f"{where} row {i + 1}"))
    out.sort(key=lambda k: k.ts)
    return out


def daily_archive_path(day: date, *, symbol: str = "BTCUSDT") -> str:
    return f"/data/spot/daily/klines/{symbol}/1s/{symbol}-1s-{day.isoformat()}.zip"


async def fetch_daily_klines(
    client: httpx.AsyncClient, day: date, *, symbol: str = "BTCUSDT", verify_checksum: bool = True,
) -> list[Kline1s]:
    """One UTC day of 1s klines from the public archive, checksum-verified. ``client`` must not carry a
    base_url for another host; this passes absolute URLs."""
    url = ARCHIVE_BASE + daily_archive_path(day, symbol=symbol)
    try:
        resp = await client.get(url)
    except httpx.TransportError as exc:
        raise BinanceHistoryError(f"{url}: {exc}") from exc
    if resp.status_code == 404:
        raise BinanceNotPublished(f"{url}: not published (yet)")
    if resp.status_code != 200:
        raise BinanceHistoryError(f"{url}: HTTP {resp.status_code}")
    content = resp.content
    if verify_checksum:
        try:
            chk = await client.get(url + ".CHECKSUM")
        except httpx.TransportError as exc:
            raise BinanceHistoryError(f"{url}.CHECKSUM: {exc}") from exc
        if chk.status_code != 200:
            raise BinanceHistoryError(f"{url}.CHECKSUM: HTTP {chk.status_code}")
        expected = chk.text.strip().split()[0].lower() if chk.text.strip() else ""
        actual = hashlib.sha256(content).hexdigest()
        if expected != actual:
            raise BinanceHistoryError(f"{url}: SHA-256 mismatch (published {expected!r}, got {actual!r})")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            members = [n for n in zf.namelist() if n.endswith(".csv")]
            if len(members) != 1:
                raise BinanceHistoryError(f"{url}: expected exactly one CSV inside, found {members!r}")
            text = zf.read(members[0]).decode("ascii")
    except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise BinanceHistoryError(f"{url}: not a readable zip of ASCII CSV: {exc}") from exc
    return parse_kline_csv(text, where=f"{symbol} {day.isoformat()}")


async def fetch_klines_rest(
    client: httpx.AsyncClient,
    *,
    start: datetime,
    end: datetime,
    symbol: str = "BTCUSDT",
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    pause_sec: float = 0.1,
) -> list[Kline1s]:
    """1s klines opening in ``[start, end)`` from the public REST host, paged 1,000 at a time (a full day is
    87 calls). The fallback for days the archive has not published yet."""
    if end <= start:
        raise BinanceHistoryError("end must be after start")
    out: list[Kline1s] = []
    cursor_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    while cursor_ms < end_ms:
        params = {
            "symbol": symbol, "interval": "1s", "startTime": str(cursor_ms), "endTime": str(end_ms - 1),
            "limit": str(REST_MAX_ROWS),
        }
        try:
            resp = await client.get(REST_BASE + "/api/v3/klines", params=params)
        except httpx.TransportError as exc:
            raise BinanceHistoryError(f"klines REST: {exc}") from exc
        if resp.status_code != 200:
            raise BinanceHistoryError(f"klines REST: HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            rows = resp.json()
        except ValueError as exc:
            raise BinanceHistoryError(f"klines REST: not JSON: {exc}") from exc
        if not isinstance(rows, list):
            raise BinanceHistoryError("klines REST: expected an array")
        if not rows:
            break
        page = [_parse_row(r, "klines REST") for r in rows]
        out.extend(page)
        cursor_ms = (page[-1].ts + 1) * 1000
        if len(rows) < REST_MAX_ROWS:
            break
        await sleep(pause_sec)
    out.sort(key=lambda k: k.ts)
    return out


def days_covering(start: datetime, end: datetime) -> list[date]:
    """Every UTC calendar day touched by ``[start, end)``."""
    if end <= start:
        return []
    first = start.astimezone(timezone.utc).date()
    last = (end.astimezone(timezone.utc) - timedelta(microseconds=1)).date()
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]
