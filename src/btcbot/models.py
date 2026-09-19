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
    levels = []
    for item in raw or ():
        try:
            price, size = item
        except (TypeError, ValueError):
            raise ParseError(f"{name}: expected [price, size] pairs, got {item!r}") from None
        levels.append(PriceLevel(_require_decimal(price, f"{name} price"), _require_decimal(size, f"{name} size")))
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

    def seconds_to_close(self, now: datetime) -> float:
        return (self.close_time - now).total_seconds()


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


@dataclass(frozen=True)
class Balance:
    available: Decimal  # dollars
    portfolio_value: Decimal  # dollars

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> Self:
        available = to_decimal(payload.get("balance_dollars"), "balance_dollars")
        if available is None:  # older payloads carried integer cents only
            available = _require_decimal(require(payload, "balance", "balance"), "balance") / 100
        portfolio_cents = _require_decimal(payload.get("portfolio_value", 0), "portfolio_value")
        return cls(available=available, portfolio_value=portfolio_cents / 100)
