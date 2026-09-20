"""The coarser, market-level half of the ML pipeline (docs/research/ml-layers-handoff.md, "Codex's backtest
pipeline"): thousands of settled KXBTC15M markets plus Coinbase 1-minute candles
(:mod:`btcbot.history_pipeline`), with no tick-level order book -- Kalshi's public API does not retain one
for markets this far back, only :mod:`btcbot.recorder`'s own live poll does. This is a DIFFERENT dataset from
:mod:`btcbot.ml_pipeline`'s (which needs real recorded order-book ticks and is what backs the 4-layer lab
ablation); this one is a calibration-style correction over the v1 model itself
(:func:`btcbot.model.predict_p_yes`), reconstructed from 1-minute candles standing in for a live spot feed.

``split_markets_by_time`` mirrors :func:`btcbot.lab.split_windows`'s own semantics (time-ordered, one market
embargoed at the boundary) for this dataset's ``MarketOutcome`` rows instead of recorded snapshots, so
"train only on earlier complete markets, validate on later months" is the same discipline either dataset
uses. ``adverse_exit_price`` is a standalone, worst-case-inside-the-candle sanity check for what an early
exit might have cost on this dataset -- not a full replay (there is no book to replay against this far
back), and it does not feed :func:`market_level_examples`.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal

from btcbot.coinbase_history import Candle
from btcbot.history_pipeline import MarketOutcome
from btcbot.model import ModelState, predict_p_yes
from btcbot.paper_broker import taker_fee

MIN_MARKETS = 6


class MarketLevelError(Exception):
    """Too few settled markets to split into train/validate, or a bad train fraction."""


def realized_vol_from_candles(candles: Sequence[Candle], *, window: int = 15) -> float:
    """Per-SECOND realized volatility from the trailing ``window`` 1-minute candle closes (stdev of
    consecutive log returns, scaled down by sqrt(60) so it is on the same footing as
    :class:`btcbot.model.TimedVolatility`'s per-second EWMA). A coarse proxy: this dataset has no tick-level
    spot feed this far back, only one close per minute -- label anything built on it accordingly, the same
    caveat :mod:`btcbot.recorder`'s own module docstring gives its REST-polled order books."""
    recent = candles[-window:] if len(candles) > window else candles
    closes = [float(c.close) for c in recent]
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    return math.sqrt(variance) / math.sqrt(60)


def market_level_examples(
    outcomes: Sequence[MarketOutcome], candles: Sequence[Candle], *, vol_window: int = 15,
) -> list[tuple[dict[str, float], bool]]:
    """One example per settled market with enough candle history: features are the v1 model's own
    ``p_yes`` (priced at the last candle close before the market's close, with a candle-derived sigma) and
    that sigma itself, label is whether YES won. A market with no candle covering it, or no strike, is
    skipped -- there is nothing to price it with. Training an :mod:`btcbot.ml_model.LogisticModel` on this
    corrects the v1 formula's own miscalibration (the same thing ``btcbot calibrate`` measures with a
    reliability table); it has no price or edge feature, because no recorded book price exists this far back
    to compute an edge against."""
    ordered = sorted(candles, key=lambda c: c.start)
    starts = [c.start for c in ordered]
    examples: list[tuple[dict[str, float], bool]] = []
    for outcome in outcomes:
        if outcome.strike is None:
            continue
        cut = bisect_right(starts, outcome.close_time)
        history = ordered[:cut]
        if len(history) < 2:
            continue
        sigma = realized_vol_from_candles(history, window=vol_window)
        state = ModelState(spot=history[-1].close, strike=outcome.strike, tau_sec=1.0, sigma=sigma)
        p_yes = predict_p_yes(state)
        examples.append(({"p_model": p_yes, "sigma": sigma}, outcome.result == "yes"))
    return examples


def split_markets_by_time(
    outcomes: Sequence[MarketOutcome], train_fraction: float, *, embargo: int = 1,
) -> tuple[set[str], set[str], list[str]]:
    """Markets in close-time order, the first ``train_fraction`` for training, then ``embargo`` skipped, the
    rest for validation -- :func:`btcbot.lab.split_windows`'s exact semantics, over this dataset's
    ``MarketOutcome`` rows instead of recorded snapshots. Returns ``(train_tickers, validate_tickers,
    ordered_all)``."""
    if not 0.2 <= train_fraction <= 0.9:
        raise MarketLevelError("train fraction must be between 0.2 and 0.9")
    ordered = [o.ticker for o in sorted(outcomes, key=lambda o: o.close_time)]
    if len(ordered) < MIN_MARKETS:
        raise MarketLevelError(
            f"only {len(ordered)} settled markets; need at least {MIN_MARKETS} to split into train and "
            "validate, and many more than that (thousands, per the pipeline this feeds) before any result "
            "means much"
        )
    cut = max(1, min(len(ordered) - embargo - 1, int(len(ordered) * train_fraction)))
    return set(ordered[:cut]), set(ordered[cut + embargo:]), ordered


def adverse_exit_price(state: ModelState, candle: Candle, side: str) -> Decimal:
    """The worst contract price achievable selling a held ``side`` at any point during ``candle``'s minute:
    re-prices the v1 model at ``state`` (the market's real strike/tau/sigma) but with spot swapped for the
    candle's least favorable print -- a YES holder's worst case is the candle LOW (spot as low as it got that
    minute), a NO holder's worst case is the HIGH. Standing in for "the recorded bid" when no tick-level
    order book exists for this dataset's markets (see this module's docstring); a sanity estimate of exit
    cost, not a full replay. Independent of, and never fed into, :func:`market_level_examples`."""
    if side not in ("yes", "no"):
        raise MarketLevelError(f"side must be 'yes' or 'no', got {side!r}")
    adverse_spot = candle.low if side == "yes" else candle.high
    p_yes = predict_p_yes(replace(state, spot=adverse_spot))
    return Decimal(str(p_yes)) if side == "yes" else Decimal(1) - Decimal(str(p_yes))


def adverse_exit_pnl(entry_price: Decimal, size: Decimal, state: ModelState, candle: Candle, side: str) -> Decimal:
    """PnL of selling ``size`` contracts of ``side`` at :func:`adverse_exit_price`, taker fees included --
    the worst-case exit sale this candle could have produced, for a rough "what would an exit have cost"
    sanity number over this dataset (see the module docstring: not a claim, not a replay)."""
    exit_price = adverse_exit_price(state, candle, side)
    return (exit_price - entry_price) * size - taker_fee(size, exit_price)
