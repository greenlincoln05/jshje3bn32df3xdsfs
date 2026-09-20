"""Feature vectors for the ML entry/exit models (docs/research/ml-layers-handoff.md).

Both models see only information a live trader would have at that instant: no settlement result, no later
book state, nothing past the current snapshot. Features are plain floats -- these feed a learned model, not
an order, so the repo's "Decimal for prices and counts" rule stops at this module's boundary, the same way
btcbot.model converts Decimal inputs to float once and works in float from there.

Every feature name below appears in a fixed tuple (``ENTRY_FEATURES`` / ``EXIT_FEATURES``) so a saved
:class:`btcbot.ml_model.LogisticModel`'s weights always line up with the same named inputs it was trained on,
regardless of dict ordering.
"""

from __future__ import annotations

from decimal import Decimal

ENTRY_FEATURES = ("edge", "p_side", "price", "tau_sec", "spread", "depth", "sigma", "momentum_60s")
EXIT_FEATURES = ("held_sec", "tau_sec", "unrealized_pct", "sigma", "momentum_60s")


def entry_features(
    *,
    p_side: float,
    price: Decimal,
    tau_sec: float,
    spread: Decimal,
    depth: Decimal,
    sigma: float,
    momentum_60s: float | None,
) -> dict[str, float]:
    """Inputs for the ML entry model, at the moment a candidate side has already cleared the strategy's own
    min_edge/min_depth/max_spread bar (see btcbot.strategy.decide) -- this model re-scores a candidate the
    existing strategy already proposed, it does not invent new ones (there is no recorded outcome for a side
    the strategy never took, so training data only ever covers taken candidates)."""
    price_f = float(price)
    return {
        "edge": p_side - price_f,
        "p_side": p_side,
        "price": price_f,
        "tau_sec": float(tau_sec),
        "spread": float(spread),
        "depth": float(depth),
        "sigma": sigma,
        "momentum_60s": 0.0 if momentum_60s is None else momentum_60s,
    }


def exit_features(
    *,
    entry_price: Decimal,
    current_bid: Decimal,
    held_sec: float,
    tau_sec: float,
    sigma: float,
    momentum_60s: float | None,
) -> dict[str, float]:
    """Inputs for the ML exit model, computed each tick a position is held. ``unrealized_pct`` mirrors
    btcbot.strategy.should_exit's own stop-loss/take-profit percentage (``current_bid`` must be the best bid
    of the side actually HELD, per that function's docstring), so the ML exit model and the fixed-threshold
    one see the same underlying signal."""
    unrealized_pct = float((current_bid - entry_price) / entry_price * 100) if entry_price > 0 else 0.0
    return {
        "held_sec": float(held_sec),
        "tau_sec": float(tau_sec),
        "unrealized_pct": unrealized_pct,
        "sigma": sigma,
        "momentum_60s": 0.0 if momentum_60s is None else momentum_60s,
    }
