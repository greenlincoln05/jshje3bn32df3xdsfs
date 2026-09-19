"""Authenticated WebSocket stream recorder: BRTI ticks and Kalshi order-book deltas into SQLite.

READ-ONLY. This module subscribes to market-data channels only (``cfbenchmarks_value``,
``cfbenchmarks_value_5hz``, ``orderbook_delta``). It has no order-placing code and sends no order,
cancel or portfolio command; CLAUDE.md's Phase 6 gate is untouched.

Why it exists: ``recorder.py`` polls REST once a second and uses Coinbase as a stand-in for BRTI, the index
Kalshi settles on. Kalshi's WebSocket handshake requires a signed request even for public channels, so this
needs the owner's own API key (``KALSHI_KEY_ID`` / ``KALSHI_PRIVATE_KEY_PATH``) on the owner's machine. A
Claude Code session never uses that key; this module is tested offline against fake sockets only.

Message shapes and channel names were read from docs.kalshi.com on 2026-09-19 and have NOT been exercised
against the live server. The parser therefore drops what it does not recognise and counts it (``unknown``)
instead of guessing, and every unrecognised type is logged once so a wrong assumption shows up on the first
real run rather than as silently missing data.

Order-book consistency: each ``orderbook_delta`` subscription has a ``seq``. A gap, or a delta that would
drive a level negative, means the local book can no longer be trusted, so the connection is dropped and
re-established (which yields a fresh snapshot) rather than patched, and the event is written to ``run_log``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import sqlite3
import time
from bisect import bisect_right
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import mean, median
from typing import Any, Protocol

import websockets
import websockets.exceptions

from btcbot.config import KalshiEnv
from btcbot.kalshi_client import KalshiAuth
from btcbot.models import OrderBook, PriceLevel

log = logging.getLogger("btcbot.stream")

WS_PATH = "/trade-api/ws/v2"
WS_URLS: dict[KalshiEnv, str] = {
    KalshiEnv.PROD: "wss://external-api-ws.kalshi.com" + WS_PATH,
    KalshiEnv.DEMO: "wss://external-api-ws.demo.kalshi.co" + WS_PATH,
}
CH_BRTI = "cfbenchmarks_value"
CH_BRTI_5HZ = "cfbenchmarks_value_5hz"
CH_BOOK = "orderbook_delta"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS brti_ticks (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    index_id TEXT NOT NULL,
    seq INTEGER,
    source_ts_ms INTEGER,
    received_at_ms INTEGER,
    receive_ts TEXT NOT NULL,
    value TEXT NOT NULL,
    avg60_value TEXT,
    avg60_window_size INTEGER
);
CREATE INDEX IF NOT EXISTS idx_brti_ticks_ts ON brti_ticks (receive_ts);

CREATE TABLE IF NOT EXISTS ws_book_events (
    id INTEGER PRIMARY KEY,
    receive_ts TEXT NOT NULL,
    ticker TEXT NOT NULL,
    sid INTEGER,
    seq INTEGER,
    kind TEXT NOT NULL,          -- 'snapshot' | 'delta'
    side TEXT,
    price TEXT,
    delta TEXT,
    ts_ms INTEGER,
    book_json TEXT               -- snapshots only: {"yes": [[price, size]], "no": [[price, size]]}
);
CREATE INDEX IF NOT EXISTS idx_ws_book_events_ticker_ts ON ws_book_events (ticker, receive_ts);
"""


class StreamError(Exception):
    """Stream recorder failure that reconnecting will not fix (bad credentials, refused handshake)."""


class StreamParseError(ValueError):
    """A WebSocket message had a field or shape this module relies on missing or malformed."""


class _Resync(Exception):
    """Internal: the local book is untrustworthy; reconnect for a fresh snapshot."""


# --------------------------------------------------------------------------- parsed messages


@dataclass(frozen=True, slots=True)
class BrtiTick:
    channel: str
    index_id: str
    value: Decimal
    source_ts_ms: int | None
    received_at_ms: int | None
    receive_ts: datetime
    seq: int | None = None
    avg60: Decimal | None = None
    avg60_window_size: int | None = None


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    ticker: str
    sid: int | None
    seq: int | None
    yes: tuple[tuple[Decimal, Decimal], ...]
    no: tuple[tuple[Decimal, Decimal], ...]


@dataclass(frozen=True, slots=True)
class BookDelta:
    ticker: str
    sid: int | None
    seq: int | None
    side: str
    price: Decimal
    delta: Decimal
    ts_ms: int | None


@dataclass(frozen=True, slots=True)
class Control:
    kind: str  # "subscribed" | "unsubscribed" | "ok" | "error" | "other"
    command_id: int | None
    sid: int | None
    channel: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class Unknown:
    type: str


Event = BrtiTick | BookSnapshot | BookDelta | Control | Unknown


def _dec(raw: Any, name: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise StreamParseError(f"{name}: not a number: {raw!r}") from None
    if not value.is_finite():
        raise StreamParseError(f"{name}: not finite: {raw!r}")
    return value


def _opt_int(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _levels(raw: Any, name: str) -> tuple[tuple[Decimal, Decimal], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise StreamParseError(f"{name}: expected a list of [price, size] pairs")
    out = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise StreamParseError(f"{name}: expected [price, size], got {item!r}")
        price, size = _dec(item[0], f"{name} price"), _dec(item[1], f"{name} size")
        if not Decimal(0) <= price <= Decimal(1) or size < 0:
            raise StreamParseError(f"{name}: price must be in [0, 1] and size nonnegative")
        out.append((price, size))
    return tuple(out)


def parse_message(raw: str | bytes, *, receive_ts: datetime) -> Event:
    """One WebSocket text frame to one event. Raises :class:`StreamParseError` on a malformed frame of a
    type this module understands; returns :class:`Unknown` for a type it does not."""
    try:
        frame = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise StreamParseError(f"not JSON: {exc}") from None
    if not isinstance(frame, dict):
        raise StreamParseError("frame is not an object")
    kind = frame.get("type")
    msg = frame.get("msg")
    sid, seq = _opt_int(frame.get("sid")), _opt_int(frame.get("seq"))

    if kind in (CH_BRTI, CH_BRTI_5HZ):
        if not isinstance(msg, dict):
            raise StreamParseError(f"{kind}: missing msg object")
        index_id = msg.get("index_id")
        if not isinstance(index_id, str) or not index_id:
            raise StreamParseError(f"{kind}: missing index_id")
        source_ts = _opt_int(msg.get("source_ts_ms"))
        raw_value = msg.get("value_usd")
        data = msg.get("data")
        if isinstance(data, str):  # the raw CF Benchmarks frame: {"type","id","time","value"}
            try:
                inner = json.loads(data)
            except ValueError:
                inner = {}
            if isinstance(inner, dict):
                raw_value = raw_value if raw_value is not None else inner.get("value")
                source_ts = source_ts if source_ts is not None else _opt_int(inner.get("time"))
        if raw_value is None:
            raise StreamParseError(f"{kind}: no value in value_usd or data")
        avg = msg.get("avg_60s_data")
        avg60 = _dec(avg["value"], "avg_60s_data.value") if isinstance(avg, dict) and avg.get("value") is not None else None
        return BrtiTick(
            channel=kind,
            index_id=index_id,
            value=_dec(raw_value, "value"),
            source_ts_ms=source_ts,
            received_at_ms=_opt_int(msg.get("received_at")),
            receive_ts=receive_ts,
            seq=seq,
            avg60=avg60,
            avg60_window_size=_opt_int(avg.get("window_size")) if isinstance(avg, dict) else None,
        )

    if kind == "orderbook_snapshot":
        if not isinstance(msg, dict) or not isinstance(msg.get("market_ticker"), str):
            raise StreamParseError("orderbook_snapshot: missing market_ticker")
        return BookSnapshot(
            ticker=msg["market_ticker"],
            sid=sid,
            seq=seq,
            yes=_levels(msg.get("yes_dollars_fp", msg.get("yes_dollars")), "yes"),
            no=_levels(msg.get("no_dollars_fp", msg.get("no_dollars")), "no"),
        )

    if kind == "orderbook_delta":
        if not isinstance(msg, dict) or not isinstance(msg.get("market_ticker"), str):
            raise StreamParseError("orderbook_delta: missing market_ticker")
        side = msg.get("side")
        if side not in ("yes", "no"):
            raise StreamParseError(f"orderbook_delta: side must be yes/no, got {side!r}")
        price = msg.get("price_dollars")
        change = msg.get("delta_fp")
        if price is None or change is None:
            raise StreamParseError("orderbook_delta: missing price_dollars or delta_fp")
        return BookDelta(msg["market_ticker"], sid, seq, side, _dec(price, "price_dollars"), _dec(change, "delta_fp"),
                         _opt_int(msg.get("ts_ms")))

    if kind in ("subscribed", "unsubscribed", "ok", "error"):
        body = msg if isinstance(msg, dict) else {}
        detail = json.dumps(body, separators=(",", ":"))[:300] if body else ""
        return Control(kind, _opt_int(frame.get("id")), _opt_int(body.get("sid")) if body else sid,
                       body.get("channel") if isinstance(body.get("channel"), str) else None, detail)

    return Unknown(str(kind))


# --------------------------------------------------------------------------- local order book


class LiveBook:
    """One market's book rebuilt from a snapshot plus deltas. Raises :class:`_Resync` if a delta would make a
    level negative: that means a missed message and the book cannot be trusted."""

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self.levels: dict[str, dict[Decimal, Decimal]] = {"yes": {}, "no": {}}
        self.ready = False

    def apply_snapshot(self, snap: BookSnapshot) -> None:
        self.levels = {
            "yes": {p: s for p, s in snap.yes if s > 0},
            "no": {p: s for p, s in snap.no if s > 0},
        }
        self.ready = True

    def apply_delta(self, delta: BookDelta) -> None:
        if not self.ready:
            return
        side = self.levels[delta.side]
        new = side.get(delta.price, Decimal(0)) + delta.delta
        if new < 0:
            raise _Resync(f"{self.ticker} {delta.side} {delta.price} would go to {new}")
        if new == 0:
            side.pop(delta.price, None)
        else:
            side[delta.price] = new

    def to_orderbook(self) -> OrderBook:
        def ascending(side: str) -> tuple[PriceLevel, ...]:
            return tuple(PriceLevel(p, s) for p, s in sorted(self.levels[side].items()))

        return OrderBook(self.ticker, ascending("yes"), ascending("no"))


# --------------------------------------------------------------------------- recorder


class WebSocketConnection(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


async def _default_connect(url: str, headers: dict[str, str]) -> WebSocketConnection:
    return await websockets.connect(url, additional_headers=headers, open_timeout=10, close_timeout=5)


_DISCONNECT_ERRORS = (websockets.exceptions.WebSocketException, OSError, asyncio.TimeoutError)


@dataclass
class StreamStats:
    brti_ticks: int = 0
    book_snapshots: int = 0
    book_deltas: int = 0
    unknown_messages: int = 0
    malformed_messages: int = 0
    reconnects: int = 0
    resyncs: int = 0
    last_brti: Decimal | None = None
    last_brti_ts: datetime | None = None
    unknown_types: set[str] = field(default_factory=set)


class StreamRecorder:
    """Subscribe to BRTI and the current market's order-book deltas, and write everything to SQLite.

    ``ticker_provider`` returns the ticker of the market that is open right now (or None between windows).
    It is polled every ``ticker_poll_sec``; on a change the new market is subscribed and the old one
    unsubscribed, so BRTI ticks keep flowing across the 15-minute rollover without a reconnect.
    """

    def __init__(
        self,
        db_path: str | Path,
        auth: KalshiAuth,
        env: KalshiEnv,
        *,
        ticker_provider: Callable[[], Awaitable[str | None]],
        index_ids: tuple[str, ...] = ("BRTI",),
        include_5hz: bool = True,
        connect: Callable[[str, dict[str, str]], Awaitable[WebSocketConnection]] = _default_connect,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        ticker_poll_sec: float = 2.0,
        commit_every_sec: float = 1.0,
        backoff_base: float = 0.5,
        backoff_cap: float = 30.0,
        rng: random.Random | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._auth = auth
        self._url = WS_URLS[env]
        self._ticker_provider = ticker_provider
        self._index_ids = list(index_ids)
        self._include_5hz = include_5hz
        self._connect = connect
        self._clock = clock
        self._sleep = sleep
        self._ticker_poll_sec = ticker_poll_sec
        self._commit_every = commit_every_sec
        self._backoff_base, self._backoff_cap = backoff_base, backoff_cap
        self._rng = rng or random.Random()
        self._monotonic = monotonic
        self.stats = StreamStats()
        self.books: dict[str, LiveBook] = {}

        self._db = sqlite3.connect(str(db_path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._last_commit = monotonic()
        self._next_id = 0

    def close(self) -> None:
        self._db.commit()
        self._db.close()

    # ---- writers

    def _maybe_commit(self) -> None:
        now = self._monotonic()
        if now - self._last_commit >= self._commit_every:
            self._db.commit()
            self._last_commit = now

    def _note(self, level: str, event: str, detail: str) -> None:
        getattr(log, level, log.info)("%s: %s", event, detail)
        try:
            self._db.execute(
                "INSERT INTO run_log (ts, level, event, detail) VALUES (?, ?, ?, ?)",
                (self._clock().astimezone(timezone.utc).isoformat(), level, event, detail),
            )
            self._db.commit()
        except sqlite3.OperationalError:  # the base recorder's run_log is missing: keep streaming regardless
            pass

    def _write_brti(self, tick: BrtiTick) -> None:
        self._db.execute(
            """INSERT INTO brti_ticks (channel, index_id, seq, source_ts_ms, received_at_ms, receive_ts, value,
                                       avg60_value, avg60_window_size) VALUES (?,?,?,?,?,?,?,?,?)""",
            (tick.channel, tick.index_id, tick.seq, tick.source_ts_ms, tick.received_at_ms,
             tick.receive_ts.astimezone(timezone.utc).isoformat(), str(tick.value),
             None if tick.avg60 is None else str(tick.avg60), tick.avg60_window_size),
        )
        self.stats.brti_ticks += 1
        self.stats.last_brti, self.stats.last_brti_ts = tick.value, tick.receive_ts

    def _write_book(self, event: BookSnapshot | BookDelta, receive_ts: datetime) -> None:
        ts = receive_ts.astimezone(timezone.utc).isoformat()
        if isinstance(event, BookSnapshot):
            book_json = json.dumps(
                {"yes": [[str(p), str(s)] for p, s in event.yes], "no": [[str(p), str(s)] for p, s in event.no]},
                separators=(",", ":"),
            )
            self._db.execute(
                "INSERT INTO ws_book_events (receive_ts, ticker, sid, seq, kind, book_json) VALUES (?,?,?,?,?,?)",
                (ts, event.ticker, event.sid, event.seq, "snapshot", book_json),
            )
            self.stats.book_snapshots += 1
        else:
            self._db.execute(
                """INSERT INTO ws_book_events (receive_ts, ticker, sid, seq, kind, side, price, delta, ts_ms)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (ts, event.ticker, event.sid, event.seq, "delta", event.side, str(event.price), str(event.delta),
                 event.ts_ms),
            )
            self.stats.book_deltas += 1

    # ---- message handling (pure enough to test without a socket)

    def handle(self, raw: str | bytes, receive_ts: datetime, session: _Session) -> None:
        try:
            event = parse_message(raw, receive_ts=receive_ts)
        except StreamParseError as exc:
            self.stats.malformed_messages += 1
            self._note("warning", "malformed_message", str(exc)[:200])
            return
        if isinstance(event, BrtiTick):
            self._write_brti(event)
        elif isinstance(event, BookSnapshot):
            book = self.books.setdefault(event.ticker, LiveBook(event.ticker))
            book.apply_snapshot(event)
            session.last_seq[event.sid] = event.seq
            self._write_book(event, receive_ts)
        elif isinstance(event, BookDelta):
            last = session.last_seq.get(event.sid)
            if event.seq is not None and last is not None and event.seq != last + 1:
                raise _Resync(f"seq gap on sid {event.sid}: expected {last + 1}, got {event.seq}")
            if event.seq is not None:
                session.last_seq[event.sid] = event.seq
            book = self.books.get(event.ticker)
            if book is not None:
                book.apply_delta(event)
            self._write_book(event, receive_ts)
        elif isinstance(event, Control):
            session.on_control(event)
            if event.kind == "error":
                self._note("warning", "ws_error", event.detail)
        else:
            self.stats.unknown_messages += 1
            if event.type not in self.stats.unknown_types:
                self.stats.unknown_types.add(event.type)
                self._note("warning", "unknown_message_type", event.type)
        self._maybe_commit()

    # ---- connection loop

    def _next_command_id(self, session: _Session) -> int:
        session.command_id += 1
        return session.command_id

    async def _send(self, conn: WebSocketConnection, session: _Session, cmd: str, params: dict[str, Any]) -> int:
        command_id = self._next_command_id(session)
        await conn.send(json.dumps({"id": command_id, "cmd": cmd, "params": params}, separators=(",", ":")))
        return command_id

    async def _subscribe_indices(self, conn: WebSocketConnection, session: _Session) -> None:
        channels = [CH_BRTI] + ([CH_BRTI_5HZ] if self._include_5hz else [])
        for channel in channels:  # one subscription per channel: a rejected 5 Hz one must not cost the 1 Hz one
            await self._send(conn, session, "subscribe", {"channels": [channel], "index_ids": self._index_ids})

    async def _follow_ticker(self, conn: WebSocketConnection, session: _Session) -> None:
        while True:
            try:
                ticker = await self._ticker_provider()
            except Exception as exc:  # a REST blip must not end the stream
                self._note("warning", "ticker_provider_error", f"{type(exc).__name__}: {exc}")
                ticker = session.ticker
            if ticker is not None and ticker != session.ticker:
                command_id = await self._send(conn, session, "subscribe", {"channels": [CH_BOOK], "market_tickers": [ticker]})
                session.pending_book[command_id] = ticker
                old, session.ticker = session.ticker, ticker
                self._note("info", "ws_subscribe_market", ticker)
                if old is not None and (old_sid := session.book_sid.get(old)) is not None:
                    await self._send(conn, session, "unsubscribe", {"sids": [old_sid]})
                    self.books.pop(old, None)
            await self._sleep(self._ticker_poll_sec)

    async def run_forever(self) -> None:
        """Stream until cancelled. Reconnects with jittered backoff on any disconnect or resync. Raises
        :class:`StreamError` when the server refuses the handshake for credential reasons, since retrying a
        rejected key only hammers the endpoint."""
        attempt = 0
        while True:
            headers = self._auth.headers("GET", WS_PATH)
            try:
                conn = await self._connect(self._url, headers)
            except websockets.exceptions.InvalidStatus as exc:
                status = exc.response.status_code
                if status in (401, 403):
                    raise StreamError(
                        f"Kalshi refused the WebSocket handshake (HTTP {status}). Check KALSHI_KEY_ID, the private "
                        "key file, and that the key belongs to this environment (demo and prod keys differ)."
                    ) from None
                await self._backoff(attempt, f"handshake HTTP {status}")
                attempt += 1
                continue
            except _DISCONNECT_ERRORS as exc:
                await self._backoff(attempt, f"connect failed ({type(exc).__name__}: {exc})")
                attempt += 1
                continue

            session = _Session()
            watcher: asyncio.Task[None] | None = None
            try:
                await self._subscribe_indices(conn, session)
                watcher = asyncio.ensure_future(self._follow_ticker(conn, session))
                attempt = 0
                while True:
                    raw = await conn.recv()
                    self.handle(raw, self._clock(), session)
                    if watcher.done() and watcher.exception() is not None:
                        raise watcher.exception()  # type: ignore[misc]
            except _Resync as exc:
                self.stats.resyncs += 1
                self._note("warning", "ws_resync", str(exc))
                self.books.clear()
            except _DISCONNECT_ERRORS as exc:
                self._note("warning", "ws_disconnected", f"{type(exc).__name__}: {exc}")
            finally:
                if watcher is not None:
                    watcher.cancel()
                    try:
                        await watcher
                    except (asyncio.CancelledError, Exception):
                        pass
                try:
                    await conn.close()
                except _DISCONNECT_ERRORS:
                    pass
                self._db.commit()
            self.stats.reconnects += 1
            await self._backoff(attempt, "reconnecting")
            attempt += 1

    async def _backoff(self, attempt: int, reason: str) -> None:
        ceiling = min(self._backoff_cap, self._backoff_base * 2**attempt)
        delay = self._rng.uniform(ceiling / 2, ceiling)
        log.info("stream: %s; retrying in %.1fs", reason, delay)
        await self._sleep(delay)


@dataclass
class _Session:
    """State that lives and dies with one WebSocket connection."""

    command_id: int = 0
    ticker: str | None = None
    last_seq: dict[int | None, int | None] = field(default_factory=dict)
    pending_book: dict[int, str] = field(default_factory=dict)
    book_sid: dict[str, int] = field(default_factory=dict)

    def on_control(self, event: Control) -> None:
        if event.kind == "subscribed" and event.channel == CH_BOOK and event.command_id in self.pending_book:
            if event.sid is not None:
                self.book_sid[self.pending_book.pop(event.command_id)] = event.sid


# --------------------------------------------------------------------------- BRTI vs spot comparison


@dataclass(frozen=True, slots=True)
class BrtiComparison:
    n: int
    mean_diff: Decimal | None       # BRTI minus Coinbase, dollars
    mean_abs_diff: Decimal | None
    median_abs_diff: Decimal | None
    max_abs_diff: Decimal | None
    mean_feed_lag_ms: float | None  # Kalshi receive time minus CF source time


def _to_epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def compare_brti_to_spot(conn: sqlite3.Connection, *, max_age_sec: float = 2.0) -> BrtiComparison:
    """BRTI (1 Hz channel) against the most recent Coinbase tick at or before each BRTI tick, ignoring pairs
    where that spot tick is older than ``max_age_sec``. This measures how well the Coinbase proxy tracks
    the index Kalshi settles on; it is not a claim about which one is "right"."""
    try:
        brti = conn.execute(
            "SELECT receive_ts, value, source_ts_ms, received_at_ms FROM brti_ticks WHERE channel=? ORDER BY receive_ts",
            (CH_BRTI,)).fetchall()
        spot = conn.execute("SELECT receive_ts, price FROM spot_ticks ORDER BY receive_ts").fetchall()
    except sqlite3.OperationalError:
        return BrtiComparison(0, None, None, None, None, None)
    spot_ts = [_to_epoch(r[0]) for r in spot]
    diffs: list[Decimal] = []
    lags: list[float] = []
    for ts, value, src, recv in brti:
        if src is not None and recv is not None:
            lags.append(float(recv - src))
        t = _to_epoch(ts)
        i = bisect_right(spot_ts, t) - 1
        if i >= 0 and t - spot_ts[i] <= max_age_sec:
            diffs.append(Decimal(value) - Decimal(spot[i][1]))
    if not diffs:
        return BrtiComparison(0, None, None, None, None, mean(lags) if lags else None)
    absd = sorted(abs(d) for d in diffs)
    return BrtiComparison(
        n=len(diffs),
        mean_diff=sum(diffs, Decimal(0)) / len(diffs),
        mean_abs_diff=sum(absd, Decimal(0)) / len(absd),
        median_abs_diff=Decimal(str(median(absd))),
        max_abs_diff=absd[-1],
        mean_feed_lag_ms=mean(lags) if lags else None,
    )
