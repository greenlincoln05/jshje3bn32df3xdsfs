"""Entry and exit decisions (Phase 4), per docs/btc15m-bot-spec.md section 5.

v1 only ever proposes resting (maker) orders at the current best bid -- joining the existing queue, never
improving on it or crossing the spread -- which is what "prefer resting limit orders over crossing the
spread" comes down to once crossing is simply never chosen. Exit is always hold-to-settlement (section 5's
default); the optional take-profit/stop-loss exits it mentions are off by default and not implemented here.

This module never touches risk or execution: it proposes a :class:`Decision`, and the caller (a live loop or
:mod:`btcbot.backtest`) is responsible for getting it past :class:`btcbot.risk.RiskManager` before acting on
it. That separation is what lets risk enforce every limit "before every order" regardless of what a strategy
proposes.

``min_edge`` alone is not a risk-adjusted bar: the same raw edge is a very different bet at a price near 0.5
(roughly even stakes either way) than at a price near 0 or 1 (a small, capped win against a much larger
loss, or vice versa) -- and the model has never been calibration-checked at those extremes (that is what
``btcbot calibrate`` is for). ``min_price``/``max_price`` let a caller refuse to enter outside a price band
at all; :func:`kelly_fraction` gives the same trade's Kelly-optimal stake as a fraction of bankroll, which
callers should scale down (fractional Kelly) rather than stake directly, precisely because full Kelly is
most aggressive exactly where a probability estimate is least trustworthy (see its docstring).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
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
    edge: float | None = None  # modeled probability minus price minus fee, for the chosen side
    kelly_fraction: float | None = None  # full-Kelly bankroll fraction for this side/price; see kelly_fraction()


def kelly_fraction(p: float, price: Decimal) -> float:
    """Kelly-optimal fraction of bankroll to stake on a contract worth probability ``p`` bought at ``price``
    (a $1 payout on a win, the whole stake lost otherwise): ``(p - price) / (1 - price)``.

    This is FULL Kelly, and it is most aggressive exactly where this strategy is least able to trust ``p``:
    for a fixed edge ``p - price``, the fraction grows without bound as ``price`` approaches 1, because the
    formula divides by the shrinking amount left to win (``1 - price``). A small overestimate of ``p`` near
    an extreme price therefore produces a wildly oversized stake, not a proportionally larger one. Callers
    should stake a fixed fraction of this (e.g. 0.2x, "quarter-to-fifth Kelly"), never the raw value, and
    :func:`decide` additionally supports a hard ``min_price``/``max_price`` band for exactly this reason.
    """
    price_f = float(price)
    if price_f >= 1.0:
        return 0.0
    return max(0.0, (p - price_f) / (1.0 - price_f))


def kelly_size(
    fraction_of_full_kelly: float, price: Decimal, *, bankroll_usd: Decimal, multiplier: float, max_contracts: Decimal
) -> Decimal:
    """Contracts to buy at ``price`` under fractional Kelly: ``multiplier`` times ``fraction_of_full_kelly``
    (see :func:`kelly_fraction`) of ``bankroll_usd``, floored to a whole number of contracts and clamped to
    ``[1, max_contracts]``. Always at least 1: whether to trade at all is ``min_edge``'s job, not this one's.
    """
    stake_usd = bankroll_usd * Decimal(str(fraction_of_full_kelly * multiplier))
    contracts = (stake_usd / price).to_integral_value(rounding=ROUND_FLOOR)
    return max(Decimal(1), min(contracts, max_contracts))


def ramp_next_size(settled_size: Decimal, won: bool, *, base: Decimal, growth_pct: Decimal, max_contracts: Decimal) -> Decimal:
    """Size of the next order after a trade settles: a win grows it by ``max(1, growth_pct%)`` contracts, a loss
    drops it straight back to ``base``. Never above ``max_contracts``. Growth comes only from settled wins."""
    if not won:
        return min(base, max_contracts)
    step = max(Decimal(1), (settled_size * growth_pct / 100).to_integral_value(rounding=ROUND_FLOOR))
    return min(max(settled_size, base) + step, max_contracts)


def percent_size(
    price: Decimal,
    *,
    cash_usd: Decimal,
    risk_pct: Decimal,
    previous_size: Decimal | None,
    last_result: str | None,
    max_growth_pct: Decimal | None,
    max_contracts: Decimal,
) -> Decimal:
    """Contracts to buy at ``price`` when staking ``risk_pct`` percent of the current account (``cash_usd``: the
    account, settled profit and loss included, less capital already at risk). Returns 0 when the account cannot
    afford one contract at that percent, and the caller then skips the trade.

    The rules, in order, all of which exist to make growth gradual and to keep the repo's "no size increase after a
    loss" rule true by construction instead of by a later veto:

    1. The target is ``floor(cash * risk_pct% / price)``, at most ``max_contracts``. It follows the account down
       immediately, so a shrinking account always means smaller bets.
    2. After a LOSS the size never exceeds ``previous_size`` (the order before it). A loss can only shrink or hold
       the next order; ``btcbot.risk.RiskManager`` enforces the same rule as a backstop.
    3. Otherwise, when the target is above ``previous_size`` (the account has grown), the next order may rise by at
       most ``max_growth_pct`` percent of ``previous_size``, and by at least one contract so small sizes can still
       move. With ``max_growth_pct`` None the ramp is off and the target is used as is.

    This is fixed-fractional sizing: bets get larger only because settled results made the account larger, and
    smaller when it shrank. It is not a martingale (nothing is ever raised to win back a loss), and it does not
    create an edge: with a strategy that loses on average, compounding just loses faster at a larger size."""
    if price <= 0:
        return Decimal(0)
    target = ((cash_usd * risk_pct / 100) / price).to_integral_value(rounding=ROUND_FLOOR)
    target = min(target, max_contracts)
    if target < 1:
        return Decimal(0)
    if previous_size is None or target <= previous_size:
        return target
    if last_result == "loss":
        return previous_size
    if max_growth_pct is None:
        return target
    step = max(Decimal(1), (previous_size * max_growth_pct / 100).to_integral_value(rounding=ROUND_FLOOR))
    return min(target, previous_size + step)


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
    min_price: Decimal | None = None,
    max_price: Decimal | None = None,
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

    candidates: list[tuple[float, Side, Decimal, float]] = []
    for side in ("yes", "no"):
        bid = book.best_bid(side)
        if bid is None:
            continue
        if min_price is not None and bid.price < min_price:
            continue
        if max_price is not None and bid.price > max_price:
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
            candidates.append((edge, side, bid.price, p_side))

    if not candidates:
        return Decision(Action.SKIP, reason="no side clears min_edge/min_depth/max_spread/price_band")

    edge, side, price, p_side = max(candidates, key=lambda c: c[0])
    return Decision(
        Action.REST, side=side, price=price, size=contracts_per_trade, reason=f"edge={edge:.4f} on {side}",
        edge=edge, kelly_fraction=kelly_fraction(p_side, price),
    )
