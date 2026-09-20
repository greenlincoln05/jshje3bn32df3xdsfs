"""Simulated fills against a recorded or live order book (Phase 4), per docs/btc15m-bot-spec.md sections 6-7.

Two things this module cannot do, both flagged rather than faked:

- **No trade tape.** Phase 2's recorder stores order-book snapshots, not individual trades (see
  recorder.py's module docstring). Without one, a resting order's fill cannot be attributed with certainty:
  when a price level's size shrinks between polls, that could be a trade (which might fill us) or a
  cancellation (which cannot). Queue fills are therefore bracketed, as the overnight plan proposed:
  ``QueueAssumption.OPTIMISTIC`` treats every shrinkage as a trade that could reach us; ``PESSIMISTIC`` treats
  none of it as one, so a resting order only ever fills in optimistic mode. Both are approximations of the
  true, unknown fill rate; report both, not just one.
- **Confirmed fee schedule.** ``raw_fee`` matches Kalshi's one worked example exactly. The rounding step
  applied on top of it, and whether makers pay a fee at all under plain ``quadratic`` (see README's "Verified
  Kalshi API facts"), are not independently confirmed -- see docs/review-and-overnight-plan.md item 6.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from typing import Literal

from btcbot.models import ONE, OrderBook, Side

FEE_RATE = Decimal("0.07")
FEE_ROUNDING_UNIT = Decimal("0.000001")
TICK_TAPER_LOW = Decimal("0.10")
TICK_TAPER_HIGH = Decimal("0.90")
FINE_TICK = Decimal("0.001")
COARSE_TICK = Decimal("0.01")

OrderStatus = Literal["open", "partially_filled", "filled", "cancelled"]


class PaperBrokerError(Exception):
    """An order request could not be honoured (off the tick grid, non-positive size, unknown order id)."""


# --------------------------------------------------------------------------- fees


def raw_fee(contracts: Decimal, price: Decimal) -> Decimal:
    """``0.07 * contracts * price * (1 - price)``, before any rounding. Matches the Fee Rounding doc's
    one-contract-at-$0.055 worked example ($0.00363825) exactly."""
    if contracts < 0:
        raise ValueError("contracts must not be negative")
    if not (0 <= price <= ONE):
        raise ValueError("price must be in [0, 1]")
    return FEE_RATE * contracts * price * (ONE - price)


def round_fee_up(fee: Decimal) -> Decimal:
    """Round up to the nearest $0.000001. Kalshi additionally rounds to account-balance precision with a
    per-order rebate accumulator across many fills; that further step is not implemented (unconfirmed, see
    the module docstring), so this is a slight overestimate of true cost at scale."""
    if fee <= 0:
        return Decimal("0.000000")
    units = (fee / FEE_ROUNDING_UNIT).to_integral_value(rounding=ROUND_CEILING)
    return units * FEE_ROUNDING_UNIT


def taker_fee(contracts: Decimal, price: Decimal) -> Decimal:
    return round_fee_up(raw_fee(contracts, price))


def maker_fee(contracts: Decimal, price: Decimal, *, multiplier: Decimal) -> Decimal:
    """``multiplier`` is a sensitivity assumption (the plan proposes 0 and 0.25), not a confirmed rate:
    whether makers pay a fee at all under plain ``quadratic`` (vs. ``quadratic_with_maker_fees``) is
    unconfirmed. 0 means "assume makers pay nothing"."""
    if multiplier < 0:
        raise ValueError("multiplier must not be negative")
    return round_fee_up(raw_fee(contracts, price) * multiplier)


# --------------------------------------------------------------------------- tick grid


def tick_size_for(price: Decimal) -> Decimal:
    """``tapered_deci_cent``: $0.001 below $0.10 and above $0.90, $0.01 in between (README's verified API
    facts). A given market's own ``price_ranges`` is the documented source of truth; this is the general
    default used when that has not been fetched."""
    if price < TICK_TAPER_LOW or price > TICK_TAPER_HIGH:
        return FINE_TICK
    return COARSE_TICK


def is_on_tick_grid(price: Decimal) -> bool:
    if not (0 < price < ONE):
        return False
    tick = tick_size_for(price)
    ratio = price / tick
    return ratio == ratio.to_integral_value()


# --------------------------------------------------------------------------- orders


@dataclass(frozen=True, slots=True)
class Fill:
    side: Side
    price: Decimal
    size: Decimal
    fee: Decimal
    maker: bool
    ts: datetime
    order_id: str | None = None  # the exchange order this fill belongs to, when the source can tell (see
    # btcbot.demo_trader, which needs it to attribute a fill to the right order when more than one has
    # rested on the same ticker in one window); paper fills have no such id and leave this None.


@dataclass
class RestingOrder:
    order_id: str
    side: Side
    price: Decimal
    size: Decimal
    placed_at: datetime
    queue_ahead: Decimal
    last_observed_level_size: Decimal
    fills: list[Fill] = field(default_factory=list)
    status: OrderStatus = "open"

    @property
    def filled_size(self) -> Decimal:
        return sum((f.size for f in self.fills), Decimal(0))

    @property
    def remaining_size(self) -> Decimal:
        return self.size - self.filled_size

    @property
    def is_done(self) -> bool:
        return self.status in ("filled", "cancelled")


class QueueAssumption(StrEnum):
    OPTIMISTIC = "optimistic"  # every observed size decrease at our price could be a trade that reaches us
    PESSIMISTIC = "pessimistic"  # none of it is, absent an actual trade tape


# --------------------------------------------------------------------------- broker


class PaperBroker:
    """Simulated fills. The paper broker never talks to Kalshi (see README's safety model); it only ever
    reads order-book snapshots handed to it."""

    def __init__(
        self,
        *,
        maker_fee_multiplier: Decimal = Decimal("0"),
        queue_assumption: QueueAssumption = QueueAssumption.OPTIMISTIC,
    ) -> None:
        self._maker_fee_multiplier = maker_fee_multiplier
        self._queue_assumption = queue_assumption
        self._resting: dict[str, RestingOrder] = {}
        self._ids = itertools.count(1)

    @property
    def queue_assumption(self) -> QueueAssumption:
        return self._queue_assumption

    def open_orders(self) -> tuple[RestingOrder, ...]:
        return tuple(o for o in self._resting.values() if not o.is_done)

    def get_order(self, order_id: str) -> RestingOrder:
        try:
            return self._resting[order_id]
        except KeyError:
            raise PaperBrokerError(f"unknown order id: {order_id}") from None

    # ---- placing and cancelling

    def place_resting_order(
        self, side: Side, price: Decimal, size: Decimal, *, ts: datetime, book: OrderBook
    ) -> str:
        if size <= 0:
            raise PaperBrokerError("size must be positive")
        if not is_on_tick_grid(price):
            raise PaperBrokerError(f"price {price} is not on the tick grid ({tick_size_for(price)})")
        level_size = self._level_size(book, side, price)
        order_id = f"paper-{next(self._ids)}"
        self._resting[order_id] = RestingOrder(
            order_id=order_id,
            side=side,
            price=price,
            size=size,
            placed_at=ts,
            queue_ahead=level_size,
            last_observed_level_size=level_size,
        )
        return order_id

    def cancel(self, order_id: str, *, ts: datetime) -> None:
        order = self.get_order(order_id)
        if not order.is_done:
            order.status = "cancelled"

    # ---- taker orders: filled immediately against the current snapshot, no persistent order

    def place_taker_order(
        self, side: Side, size: Decimal, *, book: OrderBook, ts: datetime, worst_price: Decimal | None = None
    ) -> list[Fill]:
        if size <= 0:
            raise PaperBrokerError("size must be positive")
        opposite: Side = "no" if side == "yes" else "yes"
        remaining = size
        fills: list[Fill] = []
        # asks for `side`, best (lowest) first: the opposite side's bids, highest price first (levels are
        # stored ascending with the best bid last, per models.OrderBook).
        for level in reversed(book.bids(opposite)):
            if remaining <= 0:
                break
            ask_price = ONE - level.price
            if worst_price is not None and ask_price > worst_price:
                break
            take = min(remaining, level.size)
            if take <= 0:
                continue
            fee = taker_fee(take, ask_price)
            fills.append(Fill(side=side, price=ask_price, size=take, fee=fee, maker=False, ts=ts))
            remaining -= take
        return fills

    # ---- queue simulation

    def on_book_update(self, book: OrderBook, *, ts: datetime) -> list[Fill]:
        """Advance every open resting order's queue position against the new snapshot, crediting maker
        fills where the queue assumption allows it. Call this once per polled snapshot, in order."""
        produced: list[Fill] = []
        for order in self._resting.values():
            if order.is_done:
                continue
            current_level_size = self._level_size(book, order.side, order.price)
            shrinkage = max(Decimal(0), order.last_observed_level_size - current_level_size)
            order.last_observed_level_size = current_level_size
            if shrinkage <= 0:
                continue
            excess = shrinkage - order.queue_ahead
            order.queue_ahead = max(Decimal(0), order.queue_ahead - shrinkage)
            if self._queue_assumption is QueueAssumption.PESSIMISTIC or excess <= 0:
                continue
            fill_size = min(excess, order.remaining_size)
            if fill_size <= 0:
                continue
            fee = maker_fee(fill_size, order.price, multiplier=self._maker_fee_multiplier)
            fill = Fill(side=order.side, price=order.price, size=fill_size, fee=fee, maker=True, ts=ts)
            order.fills.append(fill)
            produced.append(fill)
            if order.remaining_size <= 0:
                order.status = "filled"
            else:
                order.status = "partially_filled"
        return produced

    @staticmethod
    def _level_size(book: OrderBook, side: Side, price: Decimal) -> Decimal:
        for level in book.bids(side):
            if level.price == price:
                return level.size
        return Decimal(0)


# --------------------------------------------------------------------------- settlement


def settle(side: Side, size: Decimal, result: Side) -> Decimal:
    """$1 per contract if ``side`` matches the settled ``result``, $0 otherwise."""
    return size if side == result else Decimal(0)
