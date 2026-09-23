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
from dataclasses import dataclass, replace
from decimal import Decimal

from btcbot.coinbase_history import Candle
from btcbot.history_pipeline import MarketOutcome
from btcbot.ml_model import LogisticModel, brier_score, fit
from btcbot.model import ModelState, predict_p_yes
from btcbot.models import MarketCandle
from btcbot.paper_broker import taker_fee

MIN_MARKETS = 6
MIN_TRAIN_EXAMPLES = 20
MIN_VALIDATE_EXAMPLES = 5
MARKET_LEVEL_FEATURES = ("p_model", "sigma")


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
    market_candles: dict[str, Sequence[MarketCandle]] | None = None,
) -> list[tuple[dict[str, float], bool]]:
    """One example per settled market with enough candle history: features are the v1 model's own
    ``p_yes`` (priced at the last candle close before the market's close, with a candle-derived sigma) and
    that sigma itself, label is whether YES won. A market with no candle covering it, or no strike, is
    skipped -- there is nothing to price it with. Training an :mod:`btcbot.ml_model.LogisticModel` on this
    corrects the v1 formula's own miscalibration (the same thing ``btcbot calibrate`` measures with a
    reliability table); it has no price or edge feature by default, because no recorded book price exists
    this far back to compute an edge against.

    ``market_candles``, if given (a ``ticker -> that market's own MarketCandle bars`` mapping, e.g. from
    :func:`btcbot.history_pipeline.load_market_candles` grouped by ticker), adds an OPTIONAL
    ``market_mid_at_decision`` feature: ``(yes_bid_close + yes_ask_close) / 2`` from the latest bar ending at
    or before the market's close, or omitted for that example if there is no such bar. This is off by
    default (``market_candles=None``) specifically so existing results reproduce unchanged; it is not in
    :data:`MARKET_LEVEL_FEATURES`, so a model trained without passing this stays on the original two-feature
    schema, and a caller who does pass it opts in per call, not by a global default changing under them."""
    ordered = sorted(candles, key=lambda c: c.start)
    starts = [c.start for c in ordered]
    examples: list[tuple[dict[str, float], bool]] = []
    for outcome in outcomes:
        if outcome.strike is None:
            continue
        cut = bisect_right(starts, outcome.close_time)
        if cut < 2:
            continue
        # Only the trailing `vol_window` candles (plus the last close) are ever used below; slicing the whole
        # prefix (`ordered[:cut]`) instead copies up to the entire candle history PER OUTCOME -- with ~380k
        # candles and thousands of settled markets that is an O(outcomes * candles) MemoryError in practice.
        history = ordered[max(0, cut - vol_window - 1):cut]
        sigma = realized_vol_from_candles(history, window=vol_window)
        state = ModelState(spot=history[-1].close, strike=outcome.strike, tau_sec=1.0, sigma=sigma)
        p_yes = predict_p_yes(state)
        features = {"p_model": p_yes, "sigma": sigma}
        if market_candles is not None:
            bars = market_candles.get(outcome.ticker)
            if bars:
                at_or_before = [b for b in bars if b.end_ts <= outcome.close_time]
                if at_or_before:
                    latest = max(at_or_before, key=lambda b: b.end_ts)
                    if latest.yes_bid_close is not None and latest.yes_ask_close is not None:
                        features["market_mid_at_decision"] = float((latest.yes_bid_close + latest.yes_ask_close) / 2)
        examples.append((features, outcome.result == "yes"))
    return examples


def rows_for_calibration(
    outcomes: Sequence[MarketOutcome], candles: Sequence[Candle], market_candles: dict[str, Sequence[MarketCandle]],
    *, vol_window: int = 15,
) -> list[dict]:
    """Rows shaped exactly like :mod:`btcbot.features`' (``ticker``, ``tau_sec``, ``p_model``, ``yes_mid``,
    ``outcome_yes``) so :func:`btcbot.calibration_report.build_report` -- unchanged -- can run its existing
    reliability/disagreement tables on HISTORICAL markets, comparing the v1 model against the market's own
    price the same way it already does for live-recorded windows (``btcbot disagree``). Requires
    ``market_candles`` (unlike :func:`market_level_examples`, where it is optional): a row with no market
    price to compare against is not useful here and is skipped, not included with a null mid. ``tau_sec`` is
    fixed at 0 for every row (matching the fixed ``tau_sec=1.0`` this whole module prices markets at, near
    close) so :func:`btcbot.calibration_report.one_row_per_window`'s tau-gap filter is a no-op here; pass
    ``at_tau_sec=0`` when calling ``build_report`` on this data."""
    ordered = sorted(candles, key=lambda c: c.start)
    starts = [c.start for c in ordered]
    rows: list[dict] = []
    for outcome in outcomes:
        if outcome.strike is None:
            continue
        cut = bisect_right(starts, outcome.close_time)
        if cut < 2:
            continue
        history = ordered[max(0, cut - vol_window - 1):cut]
        sigma = realized_vol_from_candles(history, window=vol_window)
        state = ModelState(spot=history[-1].close, strike=outcome.strike, tau_sec=1.0, sigma=sigma)
        p_yes = predict_p_yes(state)
        bars = market_candles.get(outcome.ticker)
        if not bars:
            continue
        at_or_before = [b for b in bars if b.end_ts <= outcome.close_time]
        if not at_or_before:
            continue
        latest = max(at_or_before, key=lambda b: b.end_ts)
        if latest.yes_bid_close is None or latest.yes_ask_close is None:
            continue
        rows.append({
            "ticker": outcome.ticker, "tau_sec": 0.0, "p_model": p_yes,
            "yes_mid": float((latest.yes_bid_close + latest.yes_ask_close) / 2),
            "outcome_yes": 1 if outcome.result == "yes" else 0,
        })
    return rows


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


@dataclass(frozen=True, slots=True)
class MarketLevelTrainingReport:
    markets_train: int
    markets_validate: int
    train_examples: int
    validate_examples: int
    train_brier: float
    validate_brier: float
    baseline_validate_brier: float  # the v1 model's own p_model, UNCHANGED, on the same validate markets
    beats_baseline: bool


def train_and_validate_market_level(
    outcomes: Sequence[MarketOutcome],
    candles: Sequence[Candle],
    *,
    train_fraction: float = 0.7,
    embargo: int = 1,
    vol_window: int = 15,
    epochs: int = 300,
    learning_rate: float = 0.3,
    l2: float = 0.001,
) -> tuple[LogisticModel, MarketLevelTrainingReport]:
    """Trains a calibration-correction model on this coarse, candle-only dataset and validates it on later
    markets it never trained on (:func:`split_markets_by_time`'s time-ordered, embargoed split) -- the same
    ``beats_baseline`` discipline :func:`btcbot.ml_pipeline.train_and_validate_from_features` applies to the
    richer feature-store path, completing it for all three feature schemas
    (docs/research/ml-layers-handoff.md's "Review" section). The baseline here is the v1 model's own
    ``p_model``, UNCHANGED, scored on the exact same held-out markets: since ``p_model`` is one of this
    model's own two features, ``beats_baseline`` answers "does correcting the v1 formula with sigma actually
    help, or is the raw formula already as good." A calibration measure, never a PnL or profitability claim
    -- there is no recorded book price this far back to simulate a trade against at all.
    """
    train_tickers, validate_tickers, _ = split_markets_by_time(outcomes, train_fraction, embargo=embargo)
    train_outcomes = [o for o in outcomes if o.ticker in train_tickers]
    validate_outcomes = [o for o in outcomes if o.ticker in validate_tickers]

    train_examples = market_level_examples(train_outcomes, candles, vol_window=vol_window)
    validate_examples = market_level_examples(validate_outcomes, candles, vol_window=vol_window)
    if len(train_examples) < MIN_TRAIN_EXAMPLES:
        raise MarketLevelError(f"only {len(train_examples)} usable train markets; need at least {MIN_TRAIN_EXAMPLES}")
    if len(validate_examples) < MIN_VALIDATE_EXAMPLES:
        raise MarketLevelError(f"only {len(validate_examples)} usable validate markets; need at least {MIN_VALIDATE_EXAMPLES}")

    train_rows = [r for r, _ in train_examples]
    train_labels = [y for _, y in train_examples]
    model = fit(train_rows, train_labels, MARKET_LEVEL_FEATURES, epochs=epochs, learning_rate=learning_rate, l2=l2)

    validate_rows = [r for r, _ in validate_examples]
    validate_labels = [y for _, y in validate_examples]
    validate_brier = brier_score(model, validate_rows, validate_labels)
    baseline_brier = sum(
        (row["p_model"] - (1.0 if label else 0.0)) ** 2 for row, label in zip(validate_rows, validate_labels)
    ) / len(validate_rows)

    report = MarketLevelTrainingReport(
        markets_train=len(train_tickers), markets_validate=len(validate_tickers),
        train_examples=len(train_examples), validate_examples=len(validate_examples),
        train_brier=brier_score(model, train_rows, train_labels), validate_brier=validate_brier,
        baseline_validate_brier=baseline_brier, beats_baseline=validate_brier < baseline_brier,
    )
    return model, report
