"""Entry and exit decisions (Phase 4), per docs/btc15m-bot-spec.md section 5.

v1 only ever proposes resting (maker) orders at the current best bid -- joining the existing queue, never
improving on it or crossing the spread -- which is what "prefer resting limit orders over crossing the
spread" comes down to once crossing is simply never chosen. Exit is always hold-to-settlement (section 5's
default); the optional take-profit/stop-loss exits it mentions are off by default and not implemented here.

This module never touches risk or execution: it proposes a :class:`Decision`, and the caller (a live loop or
:mod:`btcbot.backtest`) is responsible for getting it past :class:`btcbot.risk.RiskManager` before acting on
it. That separation is what lets risk enforce every limit "before every order" regardless of what a strategy
proposes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from btcbot.models import OrderBook, Side
from btcbot.paper_broker import maker_fee


class Action(StrEnum):
    SKIP = "skip"  # no open position or order, and nothing here clears the bar this tick
    REST = "rest"  # place a new resting maker order
    CANCEL = "cancel"  # cancel the resting order (the close is near)
    HOLD = "hold"  # already resting, or already positioned: nothing to do this tick


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    side: Side | None = None
    price: Decimal | None = None
    size: Decimal | None = None
    reason: str = ""


def decide(
    *,
    book: OrderBook,
    tau_sec: float,
    p_yes: float,
    spot_is_stale: bool,
    min_edge: Decimal,
    min_depth: Decimal,
    max_spread: Decimal,
    min_tau_sec: float,
    max_tau_sec: float,
    cancel_before_close_sec: float,
    contracts_per_trade: Decimal,
    maker_fee_multiplier: Decimal,
    has_resting_order: bool,
    has_position: bool,
) -> Decision:
    if has_resting_order:
        if spot_is_stale:
            return Decision(Action.CANCEL, reason="cancelling because pricing inputs are unavailable")
        if tau_sec <= cancel_before_close_sec:
            return Decision(Action.CANCEL, reason="cancelling before close")
        return Decision(Action.HOLD, reason="order already resting")
    if has_position:
        return Decision(Action.HOLD, reason="already positioned; holding to settlement")
    if spot_is_stale:
        return Decision(Action.SKIP, reason="stale spot feed")
    if not (min_tau_sec <= tau_sec <= max_tau_sec):
        return Decision(Action.SKIP, reason="tau outside [min_tau_sec, max_tau_sec]")

    candidates: list[tuple[float, Side, Decimal]] = []
    for side in ("yes", "no"):
        bid = book.best_bid(side)
        if bid is None:
            continue
        spread = book.spread(side)
        if spread is None or spread > max_spread:
            continue
        if bid.size < min_depth:
            continue
        p_side = p_yes if side == "yes" else (1.0 - p_yes)
        expected_fee = float(maker_fee(Decimal(1), bid.price, multiplier=maker_fee_multiplier))
        edge = p_side - float(bid.price) - expected_fee
        if edge >= float(min_edge):
            candidates.append((edge, side, bid.price))

    if not candidates:
        return Decision(Action.SKIP, reason="no side clears min_edge/min_depth/max_spread")

    edge, side, price = max(candidates, key=lambda c: c[0])
    return Decision(
        Action.REST, side=side, price=price, size=contracts_per_trade, reason=f"edge={edge:.4f} on {side}"
    )
