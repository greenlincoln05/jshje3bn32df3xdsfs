"""Polymarket public read-only client: research only, a SEPARATE venue from Kalshi (spec/CLAUDE.md's phase gates
are all Kalshi-specific and do not apply here or grant any permission here).

Every call in this module is an unauthenticated GET against Polymarket's public APIs -- market discovery via the
Gamma API (``gamma-api.polymarket.com``), order books via the CLOB API (``clob.polymarket.com``). There is no
wallet, no private key, no signing, and -- unlike ``kalshi_client.py`` -- **no order-placing method exists in this
module at all**, on either environment; Polymarket settles trades on-chain in real USDC with no free/demo
equivalent to Kalshi's DEMO environment, so there is nothing here for a demo-only gate to even restrict. Building
real order placement against Polymarket is a separate, much larger decision than anything this file does, and
nothing here should be read as a step toward it.

Confirmed against the live public API 2026-09-22: Polymarket runs a rolling "Bitcoin Up or Down" series at
multiple horizons (event slugs ``btc-updown-5m-<epoch>``, ``btc-updown-15m-<epoch>``, ...), each a two-outcome
(``Up``/``Down``) market with its own CLOB order book, closely analogous in shape to Kalshi's ``KXBTC15M``. A
resolved event's Gamma record has ``closed: true`` and ``outcomePrices: ["1","0"]`` or ``["0","1"]``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

import httpx

from btcbot.models import ParseError, parse_time, require, to_decimal

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


class PolymarketError(Exception):
    """Base class for everything this client raises."""


class PolymarketConnectionError(PolymarketError):
    """No HTTP response was obtained (DNS, TLS, timeout, ...)."""


class PolymarketAPIError(PolymarketError):
    def __init__(self, status_code: int, message: str, *, request: str = "") -> None:
        prefix = f"{request}: " if request else ""
        super().__init__(f"{prefix}HTTP {status_code}: {message}")
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class UpdownEvent:
    """One rolling-window "Bitcoin Up or Down" event: two outcome tokens, "Up" first."""

    slug: str
    condition_id: str
    question: str
    start_time: datetime
    end_time: datetime
    up_token_id: str
    down_token_id: str
    closed: bool
    result_up: bool | None  # None until resolved

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> "UpdownEvent":
        markets = payload.get("markets")
        if not isinstance(markets, list) or not markets:
            raise ParseError("event: no markets in payload")
        m = markets[0]
        token_ids_raw = require(m, "clobTokenIds", "market")
        try:
            token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
        except (json.JSONDecodeError, TypeError) as exc:
            raise ParseError(f"market: clobTokenIds not valid JSON: {exc}") from exc
        if not isinstance(token_ids, list) or len(token_ids) != 2:
            raise ParseError("market: clobTokenIds must have exactly 2 entries (Up, Down)")
        outcomes_raw = m.get("outcomes")
        try:
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        except (json.JSONDecodeError, TypeError):
            outcomes = None
        if outcomes not in (None, ["Up", "Down"]):
            raise ParseError(f"market: unexpected outcomes order {outcomes!r}, expected ['Up', 'Down']")
        closed = bool(m.get("closed", False))
        result_up = None
        if closed:
            prices_raw = m.get("outcomePrices")
            try:
                prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            except (json.JSONDecodeError, TypeError):
                prices = None
            if isinstance(prices, list) and len(prices) == 2:
                up_price = to_decimal(prices[0], "outcomePrices[0]")
                if up_price == 1:
                    result_up = True
                elif up_price == 0:
                    result_up = False
                # else: closed but not yet in a clean 1/0 state (e.g. still resolving) -- leave None
        return cls(
            slug=require(payload, "slug", "event"),
            condition_id=require(m, "conditionId", "market"),
            question=str(m.get("question") or payload.get("title") or ""),
            start_time=parse_time(require(payload, "startDate", "event"), "event startDate"),
            end_time=parse_time(require(payload, "endDate", "event"), "event endDate"),
            up_token_id=str(token_ids[0]),
            down_token_id=str(token_ids[1]),
            closed=closed,
            result_up=result_up,
        )


@dataclass(frozen=True, slots=True)
class PriceLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class OrderBook:
    token_id: str
    bids: tuple[PriceLevel, ...]  # ascending by price (best bid last), same convention as btcbot.models.OrderBook
    asks: tuple[PriceLevel, ...]  # ascending by price (best ask first)
    timestamp: datetime

    @classmethod
    def from_api(cls, token_id: str, payload: Mapping[str, Any]) -> "OrderBook":
        def levels(key: str) -> tuple[PriceLevel, ...]:
            raw = payload.get(key) or []
            if not isinstance(raw, list):
                raise ParseError(f"book: {key} must be an array")
            out = []
            for lvl in raw:
                if not isinstance(lvl, Mapping):
                    raise ParseError(f"book: {key} entry must be an object, got {type(lvl).__name__}")
                price = to_decimal(lvl.get("price"), f"{key}.price")
                size = to_decimal(lvl.get("size"), f"{key}.size")
                if price is None or size is None:
                    raise ParseError(f"book: {key} entry missing price/size")
                out.append(PriceLevel(price, size))
            return tuple(out)

        bids = tuple(sorted(levels("bids"), key=lambda p: p.price))
        asks = tuple(sorted(levels("asks"), key=lambda p: p.price))
        ts_ms = require(payload, "timestamp", "book")
        try:
            ts = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
        except (TypeError, ValueError) as exc:
            raise ParseError(f"book: timestamp must be milliseconds since epoch, got {ts_ms!r}: {exc}") from exc
        return cls(token_id=token_id, bids=bids, asks=asks, timestamp=ts)

    def best_bid(self) -> PriceLevel | None:
        return self.bids[-1] if self.bids else None

    def best_ask(self) -> PriceLevel | None:
        return self.asks[0] if self.asks else None


class PolymarketClient:
    """Public, unauthenticated reads only. No credentials of any kind, no write method exists."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0) -> None:
        self._gamma = httpx.AsyncClient(base_url=GAMMA_BASE, transport=transport, timeout=timeout)
        self._clob = httpx.AsyncClient(base_url=CLOB_BASE, transport=transport, timeout=timeout)

    async def __aenter__(self) -> "PolymarketClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._gamma.aclose()
        await self._clob.aclose()

    async def _get(self, client: httpx.AsyncClient, path: str, *, params: Mapping[str, str] | None = None) -> Any:
        try:
            resp = await client.get(path, params=params)
        except httpx.TransportError as exc:
            raise PolymarketConnectionError(f"{path}: {exc}") from exc
        if resp.status_code >= 400:
            raise PolymarketAPIError(resp.status_code, resp.text[:500], request=path)
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise ParseError(f"{path}: response was not valid JSON: {exc}") from exc

    async def list_recent_updown_events(self, *, horizon: str, limit: int = 20) -> list[UpdownEvent]:
        """Recent "Bitcoin Up or Down" events at ``horizon`` ("5m", "15m", ...), newest first. Filters
        client-side by slug prefix -- the Gamma API has no server-side slug-prefix filter -- from the most
        recent bitcoin-tagged events, open or closed, so both the live window and its just-closed
        predecessor (for settlement) are visible."""
        data = await self._get(
            self._gamma, "/events",
            params={"limit": str(limit), "order": "startDate", "ascending": "false", "tag_slug": "bitcoin"},
        )
        if not isinstance(data, list):
            raise ParseError("events: expected an array")
        prefix = f"btc-updown-{horizon}-"
        out = []
        for payload in data:
            if not isinstance(payload, dict) or not str(payload.get("slug", "")).startswith(prefix):
                continue
            out.append(UpdownEvent.from_api(payload))
        return out

    async def get_event(self, slug: str) -> UpdownEvent:
        data = await self._get(self._gamma, f"/events/slug/{slug}")
        if not isinstance(data, dict):
            raise ParseError(f"event {slug}: expected an object")
        return UpdownEvent.from_api(data)

    async def get_order_book(self, token_id: str) -> OrderBook:
        data = await self._get(self._clob, "/book", params={"token_id": token_id})
        if not isinstance(data, dict):
            raise ParseError(f"book {token_id}: expected an object")
        return OrderBook.from_api(token_id, data)
