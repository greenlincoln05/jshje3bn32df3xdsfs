"""Typed views of Kalshi API payloads.

Prices are dollars and contract counts are contracts. Both are ``Decimal``, never ``float``: Kalshi sends
them as fixed-point strings (``*_dollars`` up to 4 decimals, ``*_fp`` 2 decimals) and counts can be
fractional. Bare JSON numbers (``floor_strike``) are decoded straight to ``Decimal`` by the client.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Self

Side = Literal["yes", "no"]
ONE = Decimal(1)


class ParseError(ValueError):
    """A Kalshi payload lacked a field, or had a shape, that this code relies on."""


def to_decimal(value: Any, name: str = "value") -> Decimal | None:
    """Convert an API number (fixed-point string, int or Decimal) to a finite Decimal. None or "" gives None."""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        result = value
    else:
        try:
            result = Decimal(str(value))
        except InvalidOperation:
            raise ParseError(f"{name}: not a number: {value!r}") from None
    if not result.is_finite():
        raise ParseError(f"{name}: not a finite number: {value!r}")
    return result


def parse_time(value: Any, name: str = "timestamp") -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime."""
    if not isinstance(value, str) or not value:
        raise ParseError(f"{name}: expected an ISO-8601 string, got {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ParseError(f"{name}: bad timestamp {value!r}") from None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def require(payload: Mapping[str, Any], key: str, what: str) -> Any:
    """``payload[key]``, raising ParseError (not KeyError/TypeError) when the payload is not shaped as expected."""
    try:
        return payload[key]
    except (KeyError, TypeError):
        raise ParseError(f"{what} payload has no {key!r} field") from None


def _require_decimal(value: Any, name: str) -> Decimal:
    result = to_decimal(value, name)
    if result is None:
        raise ParseError(f"{name}: missing value")
    return result


@dataclass(frozen=True, slots=True)
class PriceLevel:
    price: Decimal  # dollars per contract
    size: Decimal  # contracts


def _parse_levels(raw: Any, name: str) -> tuple[PriceLevel, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ParseError(f"{name}: expected an array of [price, size] pairs")
    levels = []
    seen_prices: set[Decimal] = set()
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ParseError(f"{name}: expected [price, size] pairs, got {item!r}")
        price = _require_decimal(item[0], f"{name} price")
        size = _require_decimal(item[1], f"{name} size")
        if not 0 <= price <= ONE or size < 0:
            raise ParseError(f"{name}: price must be in [0, 1] and size nonnegative")
        if price in seen_prices:
            raise ParseError(f"{name}: duplicate price level {price}")
        seen_prices.add(price)
        if size > 0:
            levels.append(PriceLevel(price, size))
    # Kalshi sends ascending prices with the best bid last; sort anyway rather than rely on it.
    return tuple(sorted(levels, key=lambda level: level.price))


@dataclass(frozen=True, slots=True)
class OrderBook:
    """Resting bids for both sides of a binary market, each ascending by price (best bid last).

    Kalshi publishes bids only. A NO bid at ``q`` is a YES ask at ``1 - q`` with the same size, and
    vice versa, so asks are derived from the opposite side's bids.
    """

    ticker: str
    yes_bids: tuple[PriceLevel, ...]
    no_bids: tuple[PriceLevel, ...]

    @classmethod
    def from_api(cls, ticker: str, payload: Mapping[str, Any]) -> Self:
        book = require(payload, "orderbook_fp", "orderbook")
        if not isinstance(book, Mapping):
            raise ParseError("orderbook_fp: expected an object")
        return cls(
            ticker=ticker,
            yes_bids=_parse_levels(book.get("yes_dollars"), "yes_dollars"),
            no_bids=_parse_levels(book.get("no_dollars"), "no_dollars"),
        )

    def bids(self, side: Side) -> tuple[PriceLevel, ...]:
        return self.yes_bids if side == "yes" else self.no_bids

    def best_bid(self, side: Side) -> PriceLevel | None:
        levels = self.bids(side)
        return levels[-1] if levels else None

    def best_ask(self, side: Side) -> PriceLevel | None:
        opposite = self.best_bid("no" if side == "yes" else "yes")
        return None if opposite is None else PriceLevel(ONE - opposite.price, opposite.size)

    def spread(self, side: Side) -> Decimal | None:
        bid, ask = self.best_bid(side), self.best_ask(side)
        return None if bid is None or ask is None else ask.price - bid.price

    def mid(self, side: Side) -> Decimal | None:
        bid, ask = self.best_bid(side), self.best_ask(side)
        return None if bid is None or ask is None else (bid.price + ask.price) / 2


@dataclass(frozen=True)
class Market:
    ticker: str
    event_ticker: str
    status: str  # "active" while trading; see docs.kalshi.com/getting_started/market_lifecycle
    title: str
    open_time: datetime
    close_time: datetime
    strike: Decimal | None  # floor_strike: the opening 60s BRTI average for KXBTC15M
    strike_type: str | None
    volume: Decimal | None  # contracts traded
    open_interest: Decimal | None  # contracts
    raw: Mapping[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> Self:
        return cls(
            ticker=require(payload, "ticker", "market"),
            event_ticker=require(payload, "event_ticker", "market"),
            status=require(payload, "status", "market"),
            title=payload.get("title") or "",
            open_time=parse_time(require(payload, "open_time", "market"), "open_time"),
            close_time=parse_time(require(payload, "close_time", "market"), "close_time"),
            strike=to_decimal(payload.get("floor_strike"), "floor_strike"),
            strike_type=payload.get("strike_type") or None,
            volume=to_decimal(payload.get("volume_fp"), "volume_fp"),
            open_interest=to_decimal(payload.get("open_interest_fp"), "open_interest_fp"),
            raw=payload,
        )

    def is_open_at(self, now: datetime) -> bool:
        return self.status == "active" and self.open_time <= now < self.close_time

    @property
    def exchange_index(self) -> int | None:
        """Which Kalshi exchange shard trades this market (docs: "Exchange Sharding"; crypto is shard 2). Orders
        for it are only accepted if collateral has been allocated to THAT shard. None if the payload has no
        such field (older payloads, or a fixture)."""
        raw = self.raw.get("exchange_index")
        if raw is None or isinstance(raw, bool):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def seconds_to_close(self, now: datetime) -> float:
        return (self.close_time - now).total_seconds()

    @property
    def result(self) -> str | None:
        """The settlement outcome ("yes"/"no") for a settled market, or None (open, or a payload -- e.g. an
        older fixture -- that never carried one). Kalshi's ``/markets`` response only sets this field once a
        market has actually settled, so this is the historical-download counterpart of
        btcbot.backtest.Settlement (which reads the same field from the recorder's own live poll)."""
        raw = self.raw.get("result")
        return raw if raw in ("yes", "no") else None


@dataclass(frozen=True)
class Series:
    ticker: str
    title: str
    frequency: str
    fee_type: str  # e.g. "quadratic"; the fee model to apply (see docs.kalshi.com and the fee schedule PDF)
    fee_multiplier: Decimal

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> Self:
        return cls(
            ticker=require(payload, "ticker", "series"),
            title=payload.get("title") or "",
            frequency=payload.get("frequency") or "",
            fee_type=require(payload, "fee_type", "series"),
            fee_multiplier=_require_decimal(require(payload, "fee_multiplier", "series"), "fee_multiplier"),
        )


@dataclass(frozen=True, slots=True)
class OrderAck:
    """What ``POST /portfolio/events/orders`` (V2) returns: an acknowledgement, not the full order. Fetch
    :class:`KalshiOrder` with ``get_order`` when the order's own fields (side, price, status) are needed."""

    order_id: str
    client_order_id: str | None
    fill_count: Decimal
    remaining_count: Decimal
    average_fill_price: Decimal | None
    average_fee_paid: Decimal | None

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> Self:
        return cls(
            order_id=require(payload, "order_id", "create-order response"),
            client_order_id=payload.get("client_order_id"),
            fill_count=_require_decimal(require(payload, "fill_count", "create-order response"), "fill_count"),
            remaining_count=_require_decimal(require(payload, "remaining_count", "create-order response"), "remaining_count"),
            average_fill_price=to_decimal(payload.get("average_fill_price"), "average_fill_price"),
            average_fee_paid=to_decimal(payload.get("average_fee_paid"), "average_fee_paid"),
        )


@dataclass(frozen=True, slots=True)
class CancelAck:
    order_id: str
    reduced_by: Decimal

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> Self:
        return cls(
            order_id=require(payload, "order_id", "cancel-order response"),
            reduced_by=_require_decimal(require(payload, "reduced_by", "cancel-order response"), "reduced_by"),
        )


@dataclass(frozen=True, slots=True)
class KalshiOrder:
    """A real order, from ``GET /portfolio/orders[/{id}]`` (Phase 6). Distinct from
    :class:`btcbot.paper_broker.RestingOrder`, which is simulated and never touches Kalshi.

    Field names follow docs.kalshi.com as read on 2026-09-19 (``outcome_side``, ``book_side``, ``*_fp`` counts,
    ``*_dollars`` prices, ``status`` in resting/canceled/executed). Required fields raise
    :class:`ParseError` rather than defaulting: a silent zero count or fee would be worse than a loud failure.
    Still not exercised against the live server -- the owner's ``btcbot demo-check`` run is what confirms it.

    ``side`` is the OUTCOME the order profits from (``outcome_side``): buying NO, and selling YES, are both
    ``"no"``. ``book_side`` is the same fact in book vocabulary (``bid`` = yes, ``ask`` = no).
    """

    order_id: str
    client_order_id: str | None
    ticker: str
    side: Side
    action: str  # always "buy" for this bot; kept so callers written against the old shape still work
    order_type: str  # "limit" or "market"
    status: str  # "resting", "canceled" or "executed"
    price: Decimal | None  # dollars, in the order's own outcome (yes price for a yes order, no price for a no order)
    initial_count: Decimal
    remaining_count: Decimal
    created_time: datetime | None
    book_side: str | None = None
    fill_count: Decimal | None = None
    fees_usd: Decimal | None = None  # maker + taker fees charged so far

    @property
    def is_done(self) -> bool:
        return self.status in ("canceled", "executed")

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> Self:
        side = require(payload, "outcome_side", "order")
        if side not in ("yes", "no"):
            raise ParseError(f"order: outcome_side must be yes or no, got {side!r}")
        price = to_decimal(payload.get("yes_price_dollars" if side == "yes" else "no_price_dollars"), "order price")
        created = payload.get("created_time")
        maker = to_decimal(payload.get("maker_fees_dollars"), "maker_fees_dollars")
        taker = to_decimal(payload.get("taker_fees_dollars"), "taker_fees_dollars")
        return cls(
            order_id=require(payload, "order_id", "order"),
            client_order_id=payload.get("client_order_id"),
            ticker=require(payload, "ticker", "order"),
            side=side,
            action="buy",
            order_type=payload.get("type") or "limit",
            status=require(payload, "status", "order"),
            price=price,
            initial_count=_require_decimal(require(payload, "initial_count_fp", "order"), "initial_count_fp"),
            remaining_count=_require_decimal(require(payload, "remaining_count_fp", "order"), "remaining_count_fp"),
            created_time=parse_time(created, "created_time") if created else None,
            book_side=payload.get("book_side"),
            fill_count=to_decimal(payload.get("fill_count_fp"), "fill_count_fp"),
            fees_usd=None if maker is None and taker is None else (maker or Decimal(0)) + (taker or Decimal(0)),
        )


@dataclass(frozen=True, slots=True)
class Trade:
    """One public trade print from ``GET /markets/trades`` (no key needed): who crossed, at what price, how many.
    ``taker_side`` is the outcome the TAKER bought; the maker on the other side had a resting order on the opposite
    outcome (a taker buying NO at ``no_price`` hit a YES bid at ``yes_price``). Read from the live API 2026-09-20."""

    ticker: str
    trade_id: str
    count: Decimal
    yes_price: Decimal
    no_price: Decimal
    taker_side: Side
    created_time: datetime

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> Self:
        taker = require(payload, "taker_side", "trade")
        if taker not in ("yes", "no"):
            raise ParseError(f"trade: taker_side must be yes or no, got {taker!r}")
        return cls(
            ticker=require(payload, "ticker", "trade"),
            trade_id=require(payload, "trade_id", "trade"),
            count=_require_decimal(payload.get("count_fp"), "trade count_fp"),
            yes_price=_require_decimal(payload.get("yes_price_dollars"), "trade yes_price_dollars"),
            no_price=_require_decimal(payload.get("no_price_dollars"), "trade no_price_dollars"),
            taker_side=taker,
            created_time=parse_time(require(payload, "created_time", "trade"), "trade created_time"),
        )


@dataclass(frozen=True, slots=True)
class KalshiFill:
    """A real fill from ``GET /portfolio/fills`` (Phase 6). Distinct from :class:`btcbot.paper_broker.Fill`,
    which is simulated. Same docs-as-read-2026-09-19 caveat as :class:`KalshiOrder`; ``fee_cost`` and
    ``is_taker`` are REQUIRED, because defaulting them (as an earlier version did) would make the Phase 6d
    fidelity report claim makers pay nothing whenever the field was merely misnamed."""

    trade_id: str
    order_id: str
    ticker: str
    side: Side
    action: str
    price: Decimal
    count: Decimal
    fee_usd: Decimal
    is_taker: bool
    created_time: datetime
    fill_id: str = ""

    @property
    def dedupe_key(self) -> str:
        return self.fill_id or self.trade_id

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> Self:
        side = require(payload, "outcome_side", "fill")
        if side not in ("yes", "no"):
            raise ParseError(f"fill: outcome_side must be yes or no, got {side!r}")
        price = _require_decimal(payload.get("yes_price_dollars" if side == "yes" else "no_price_dollars"), "fill price")
        fee = to_decimal(payload.get("fee_cost"), "fee_cost")
        if fee is None:
            raise ParseError("fill: missing fee_cost")
        is_taker = payload.get("is_taker")
        if not isinstance(is_taker, bool):
            raise ParseError("fill: is_taker must be a boolean")
        fill_id = payload.get("fill_id") or ""
        trade_id = payload.get("trade_id") or fill_id
        if not trade_id:
            raise ParseError("fill: needs a fill_id or trade_id")
        ticker = payload.get("ticker") or payload.get("market_ticker")
        if not ticker:
            raise ParseError("fill: missing ticker")
        return cls(
            trade_id=trade_id,
            order_id=require(payload, "order_id", "fill"),
            ticker=ticker,
            side=side,
            action="buy",
            price=price,
            count=_require_decimal(require(payload, "count_fp", "fill"), "count_fp"),
            fee_usd=fee,
            is_taker=is_taker,
            created_time=parse_time(require(payload, "created_time", "fill"), "created_time"),
            fill_id=fill_id,
        )


@dataclass(frozen=True, slots=True)
class Position:
    """A real position from ``GET /portfolio/positions`` (Phase 6). Kalshi reports one signed ``position_fp``
    per ticker (positive = long YES, negative = long NO, since a binary market's two sides are
    complementary); normalized here to this codebase's ``side`` + non-negative ``count`` convention."""

    ticker: str
    side: Side
    count: Decimal
    market_exposure_usd: Decimal | None
    realized_pnl_usd: Decimal | None = None
    fees_paid_usd: Decimal | None = None

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> Self:
        signed = _require_decimal(require(payload, "position_fp", "position"), "position_fp")
        return cls(
            ticker=require(payload, "ticker", "position"),
            side="yes" if signed >= 0 else "no",
            count=abs(signed),
            market_exposure_usd=to_decimal(payload.get("market_exposure_dollars"), "market_exposure_dollars"),
            realized_pnl_usd=to_decimal(payload.get("realized_pnl_dollars"), "realized_pnl_dollars"),
            fees_paid_usd=to_decimal(payload.get("fees_paid_dollars"), "fees_paid_dollars"),
        )


@dataclass(frozen=True)
class Balance:
    available: Decimal  # dollars, across every exchange shard
    portfolio_value: Decimal  # dollars
    by_exchange: dict[int, Decimal] = field(default_factory=dict)  # dollars per exchange shard; empty if not reported

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> Self:
        available = to_decimal(payload.get("balance_dollars"), "balance_dollars")
        if available is None:  # older payloads carried integer cents only
            available = _require_decimal(require(payload, "balance", "balance"), "balance") / 100
        portfolio_cents = _require_decimal(payload.get("portfolio_value", 0), "portfolio_value")
        by_exchange: dict[int, Decimal] = {}
        breakdown = payload.get("balance_breakdown")  # omitted for subaccount-restricted keys
        if isinstance(breakdown, list):
            for entry in breakdown:
                if not isinstance(entry, Mapping):
                    continue
                index, amount = entry.get("exchange_index"), to_decimal(entry.get("balance"), "balance_breakdown balance")
                if isinstance(index, int) and not isinstance(index, bool) and amount is not None:
                    by_exchange[index] = amount
        return cls(available=available, portfolio_value=portfolio_cents / 100, by_exchange=by_exchange)
