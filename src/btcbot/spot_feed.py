"""Spot price feed: Coinbase's public WebSocket ticker for BTC-USD into a rolling ~1 s price buffer.

Kalshi settles on CF Benchmarks' BRTI, which needs an authenticated channel we do not have a key for
(see README, "Verified Kalshi API facts"). Coinbase BTC-USD is an unauthenticated proxy; the gap between
it and BRTI is model error to be measured later, not something this module tries to close.

The ``ticker`` channel needs no authentication. A REST poll is the fallback when the socket is down or
stale (see docs.cdp.coinbase.com/exchange/websocket-feed/channels for the message shape).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx
import websockets
import websockets.exceptions

log = logging.getLogger("btcbot.spot_feed")

COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
COINBASE_REST_URL = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
PRODUCT_ID = "BTC-USD"
STALE_AFTER_SEC = 3.0


class SpotFeedError(Exception):
    """A ticker message could not be parsed, or the exchange sent an error message."""


# --------------------------------------------------------------------------- data


@dataclass(frozen=True, slots=True)
class SpotTick:
    """One price observation. Kept even when parsing later downsamples it, per the review's guidance that raw
    ticks -- not just the rolling buffer they feed -- are the thing worth recording."""

    price: Decimal
    source: str  # "coinbase-ws" or "coinbase-rest"
    source_ts: datetime | None  # exchange-reported trade/quote time, when the message carries one
    receive_ts: datetime  # local wall-clock UTC time this process saw the message
    monotonic_ts: float  # time.monotonic() at receipt: immune to wall-clock jumps, used for staleness/gaps


# --------------------------------------------------------------------------- parsing


def _parse_price(raw: Any, name: str) -> Decimal:
    try:
        price = Decimal(str(raw))
    except (InvalidOperation, TypeError):
        raise SpotFeedError(f"{name}: not a number: {raw!r}") from None
    if not price.is_finite() or price <= 0:
        raise SpotFeedError(f"{name}: not a positive finite price: {raw!r}")
    return price


def _parse_source_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None  # a bad timestamp string is not a reason to drop a real price


def parse_ticker_message(raw: str, *, receive_ts: datetime, monotonic_ts: float) -> SpotTick | None:
    """Parse one Coinbase WebSocket message. Returns None for messages that carry no price
    (subscription acks, heartbeats). Raises :class:`SpotFeedError` for malformed JSON or an error message.
    """
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SpotFeedError(f"malformed WebSocket message: {exc}") from exc
    if not isinstance(data, dict):
        raise SpotFeedError(f"expected a JSON object, got {type(data).__name__}")
    msg_type = data.get("type")
    if msg_type == "error":
        raise SpotFeedError(f"Coinbase error: {data.get('message') or data}")
    if msg_type != "ticker":
        return None
    return SpotTick(
        price=_parse_price(data.get("price"), "ticker price"),
        source="coinbase-ws",
        source_ts=_parse_source_ts(data.get("time")),
        receive_ts=receive_ts,
        monotonic_ts=monotonic_ts,
    )


def parse_rest_ticker(payload: Any, *, receive_ts: datetime, monotonic_ts: float) -> SpotTick:
    if not isinstance(payload, dict):
        raise SpotFeedError(f"REST ticker: expected a JSON object, got {type(payload).__name__}")
    return SpotTick(
        price=_parse_price(payload.get("price"), "REST ticker price"),
        source="coinbase-rest",
        source_ts=_parse_source_ts(payload.get("time")),
        receive_ts=receive_ts,
        monotonic_ts=monotonic_ts,
    )


# --------------------------------------------------------------------------- rolling buffer


class SpotBuffer:
    """The last ``window_sec`` seconds of ticks, the latest price, and a staleness flag.

    Staleness is measured on ``monotonic_ts`` so a wall-clock adjustment cannot hide a dead feed
    (or falsely report one).
    """

    def __init__(self, window_sec: float = 1.0, *, stale_after_sec: float = STALE_AFTER_SEC) -> None:
        if window_sec <= 0 or stale_after_sec <= 0:
            raise ValueError("window_sec and stale_after_sec must be positive")
        self.window_sec = window_sec
        self.stale_after_sec = stale_after_sec
        self._ticks: deque[SpotTick] = deque()

    def add(self, tick: SpotTick) -> None:
        self._ticks.append(tick)
        cutoff = tick.monotonic_ts - self.window_sec
        while len(self._ticks) > 1 and self._ticks[0].monotonic_ts < cutoff:
            self._ticks.popleft()

    @property
    def latest(self) -> SpotTick | None:
        return self._ticks[-1] if self._ticks else None

    def price(self) -> Decimal | None:
        latest = self.latest
        return latest.price if latest else None

    def window_ticks(self) -> tuple[SpotTick, ...]:
        return tuple(self._ticks)

    def is_stale(self, *, now_monotonic: float | None = None) -> bool:
        latest = self.latest
        if latest is None:
            return True
        now = time.monotonic() if now_monotonic is None else now_monotonic
        return (now - latest.monotonic_ts) > self.stale_after_sec


# --------------------------------------------------------------------------- connection


class WebSocketConnection(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...


async def _default_connect(url: str) -> WebSocketConnection:
    return await websockets.connect(url, open_timeout=10, close_timeout=5)


# Errors that mean "this connection is dead, reconnect" rather than a bug worth surfacing.
_DISCONNECT_ERRORS = (websockets.exceptions.WebSocketException, OSError, asyncio.TimeoutError)


# --------------------------------------------------------------------------- feed


class CoinbaseSpotFeed:
    """Streams Coinbase's ``ticker`` channel for one product into a :class:`SpotBuffer`, reconnecting with
    backoff on any disconnect, and offers a REST poll for when the socket is down or stale.
    """

    def __init__(
        self,
        buffer: SpotBuffer,
        *,
        product_id: str = PRODUCT_ID,
        ws_url: str = COINBASE_WS_URL,
        rest_url: str = COINBASE_REST_URL,
        connect: Callable[[str], Awaitable[WebSocketConnection]] = _default_connect,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backoff_base: float = 0.5,
        backoff_cap: float = 30.0,
        rng: random.Random | None = None,
        on_tick: Callable[[SpotTick], None] | None = None,
    ) -> None:
        self.buffer = buffer
        self._product_id = product_id
        self._ws_url = ws_url
        self._rest_url = rest_url
        self._connect = connect
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        self._owns_http = http is None
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._rng = rng or random.Random()
        self._on_tick = on_tick

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _record(self, tick: SpotTick) -> None:
        self.buffer.add(tick)
        if self._on_tick is not None:
            self._on_tick(tick)

    async def poll_rest_once(self) -> SpotTick:
        """One REST fallback poll. Raises :class:`SpotFeedError` or an httpx error on failure; callers decide
        whether to retry."""
        response = await self._http.get(self._rest_url)
        response.raise_for_status()
        tick = parse_rest_ticker(response.json(), receive_ts=self._clock(), monotonic_ts=self._monotonic())
        self._record(tick)
        return tick

    async def run_forever(self) -> None:
        """Connect, subscribe, and stream ticks until cancelled. Never returns on its own; reconnects with
        jittered exponential backoff on any disconnect or malformed message it cannot recover from."""
        attempt = 0
        subscribe_message = json.dumps(
            {"type": "subscribe", "product_ids": [self._product_id], "channels": ["ticker"]}
        )
        while True:
            try:
                connection = await self._connect(self._ws_url)
            except _DISCONNECT_ERRORS as exc:
                await self._backoff(attempt, f"connect failed ({type(exc).__name__}: {exc})")
                attempt += 1
                continue
            try:
                await connection.send(subscribe_message)
                attempt = 0  # a working connection resets backoff
                while True:
                    raw = await connection.recv()
                    receive_ts, mono = self._clock(), self._monotonic()
                    try:
                        tick = parse_ticker_message(raw, receive_ts=receive_ts, monotonic_ts=mono)
                    except SpotFeedError as exc:
                        log.warning("dropping malformed ticker message: %s", exc)
                        continue
                    if tick is not None:
                        self._record(tick)
            except _DISCONNECT_ERRORS as exc:
                log.warning("spot feed disconnected (%s: %s); reconnecting", type(exc).__name__, exc)
            finally:
                try:
                    await connection.close()
                except _DISCONNECT_ERRORS:
                    pass
            await self._backoff(attempt, "reconnecting")
            attempt += 1

    async def _backoff(self, attempt: int, reason: str) -> None:
        ceiling = min(self._backoff_cap, self._backoff_base * 2**attempt)
        delay = self._rng.uniform(ceiling / 2, ceiling)
        log.warning("spot feed: %s; retry %d in %.2fs", reason, attempt + 1, delay)
        await self._sleep(delay)
