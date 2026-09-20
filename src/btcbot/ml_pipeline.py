"""Builds labeled (features, outcome) examples for the ML entry/exit models from recorded data, and trains
them against a time-ordered, grouped-by-market split (docs/research/ml-layers-handoff.md).

Decoupled from :func:`btcbot.backtest.replay_prepared` on purpose: that function's job is a realistic,
risk/broker-aware replay (queue fills, sizing, exposure limits), and stays untouched here. This module only
needs :func:`btcbot.strategy.decide`'s bare candidate-side logic and the eventual settlement outcome, so it
walks ``prepared.steps`` directly with much simpler bookkeeping (one open candidate at a time, no risk
manager, no broker) -- just enough to answer "what would the strategy have proposed here, and did it win",
which is what a label needs.

Both example builders only ever label a candidate/tick the base strategy actually reached: the entry model
never sees a side ``decide()`` would have skipped (there is no recorded outcome for a side never taken), and
the exit model only sees ticks while a position from one of those entries is held. Training uses
:func:`btcbot.lab.split_windows` -- the same time-ordered, embargoed, whole-market grouping the lab's own
train/test sweep uses -- so a model is validated only on markets later than every one it trained on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from btcbot.backtest import PreparedReplay, ReplayData, Settlement, prepare_replay
from btcbot.config import BotConfig
from btcbot.lab import split_windows
from btcbot.ml_features import ENTRY_FEATURES, EXIT_FEATURES, entry_features, exit_features
from btcbot.ml_model import LogisticModel, brier_score, fit
from btcbot.paper_broker import taker_fee
from btcbot.strategy import Action, decide
from btcbot.validation import ValidationError
from btcbot.validation import split_windows as split_feature_windows

MIN_TRAIN_EXAMPLES = 20
MIN_VALIDATE_EXAMPLES = 5

Example = tuple[dict[str, float], bool]


class MLPipelineError(Exception):
    """Not enough labeled examples to train or validate, or an unknown model kind."""


@dataclass
class _OpenCandidate:
    ticker: str
    side: str
    entry_price: Decimal
    entry_ts: datetime
    features: dict[str, float]


def build_training_examples(
    prepared: PreparedReplay, config: BotConfig, settlements: Mapping[str, Settlement]
) -> tuple[list[Example], list[Example]]:
    """One pass over ``prepared.steps``. Returns ``(entry_examples, exit_examples)``. A window that never
    settles in ``settlements`` (still pending at the end of the data) contributes no examples for that
    position -- an unknown outcome is left out, never scored as a loss, the same rule
    :func:`btcbot.backtest.build_report` applies to PnL."""
    entry_examples: list[Example] = []
    exit_examples: list[Example] = []
    open_candidate: _OpenCandidate | None = None

    def close(ticker: str) -> None:
        nonlocal open_candidate
        if open_candidate is None:
            return
        settlement = settlements.get(ticker)
        if settlement is not None:
            entry_examples.append((open_candidate.features, settlement.result == open_candidate.side))
        open_candidate = None

    for step in prepared.steps:
        snap = step.snap
        if open_candidate is not None and snap.ticker != open_candidate.ticker:
            close(open_candidate.ticker)
        if not step.active:
            continue
        momentum = prepared.spot_series.move(snap.poll_ts, 60)
        momentum_f = float(momentum) if momentum is not None else None
        if open_candidate is None:
            decision = decide(
                book=snap.book, tau_sec=step.tau_sec, p_yes=step.p_yes, spot_is_stale=step.stale,
                min_edge=config.min_edge, min_depth=config.min_depth, max_spread=config.max_spread,
                min_tau_sec=config.min_tau_sec, max_tau_sec=config.max_tau_sec,
                cancel_before_close_sec=config.cancel_before_close_sec,
                contracts_per_trade=Decimal(config.sizing.contracts_per_trade),
                maker_fee_multiplier=Decimal(0), has_resting_order=False, has_position=False,
            )
            if decision.action is Action.REST:
                bid = snap.book.best_bid(decision.side)
                p_side = step.p_yes if decision.side == "yes" else 1.0 - step.p_yes
                features = entry_features(
                    p_side=p_side, price=decision.price, tau_sec=step.tau_sec,
                    spread=snap.book.spread(decision.side) or Decimal(0),
                    depth=bid.size if bid is not None else Decimal(0), sigma=step.sigma,
                    momentum_60s=momentum_f,
                )
                open_candidate = _OpenCandidate(snap.ticker, decision.side, decision.price, snap.poll_ts, features)
        else:
            current_bid = snap.book.best_bid(open_candidate.side)
            settlement = settlements.get(open_candidate.ticker)
            if current_bid is not None and settlement is not None:
                held_sec = (snap.poll_ts - open_candidate.entry_ts).total_seconds()
                feat = exit_features(
                    entry_price=open_candidate.entry_price, current_bid=current_bid.price, held_sec=held_sec,
                    tau_sec=step.tau_sec, sigma=step.sigma, momentum_60s=momentum_f,
                )
                sell_now = current_bid.price - open_candidate.entry_price - taker_fee(Decimal(1), current_bid.price)
                hold = (Decimal(1) if settlement.result == open_candidate.side else Decimal(0)) - open_candidate.entry_price
                exit_examples.append((feat, sell_now > hold))
    if open_candidate is not None:
        close(open_candidate.ticker)
    return entry_examples, exit_examples


def _fit_and_score(examples: Sequence[Example], feature_names: Sequence[str], *, epochs: int, learning_rate: float, l2: float) -> tuple[LogisticModel, float]:
    if len(examples) < MIN_TRAIN_EXAMPLES:
        raise MLPipelineError(f"only {len(examples)} labeled training examples; need at least {MIN_TRAIN_EXAMPLES}")
    rows = [r for r, _ in examples]
    labels = [y for _, y in examples]
    model = fit(rows, labels, feature_names, epochs=epochs, learning_rate=learning_rate, l2=l2)
    return model, brier_score(model, rows, labels)


def train_entry_model(examples: Sequence[Example], *, epochs: int = 300, learning_rate: float = 0.3, l2: float = 0.001) -> tuple[LogisticModel, float]:
    """Fits on ``examples`` (from :func:`build_training_examples`'s first return value) and returns
    ``(model, brier_on_those_same_examples)``. Callers should pass only TRAIN-split examples here and score
    the model separately on a held-out validate split (see :func:`train_and_validate`)."""
    return _fit_and_score(examples, ENTRY_FEATURES, epochs=epochs, learning_rate=learning_rate, l2=l2)


def train_exit_model(examples: Sequence[Example], *, epochs: int = 300, learning_rate: float = 0.3, l2: float = 0.001) -> tuple[LogisticModel, float]:
    """Same contract as :func:`train_entry_model`, for exit examples (:func:`build_training_examples`'s
    second return value)."""
    return _fit_and_score(examples, EXIT_FEATURES, epochs=epochs, learning_rate=learning_rate, l2=l2)


@dataclass(frozen=True, slots=True)
class TrainingReport:
    which: str  # "entry" | "exit"
    windows_train: int
    windows_validate: int
    train_examples: int
    validate_examples: int
    train_brier: float
    validate_brier: float


def train_and_validate(
    data: ReplayData,
    config: BotConfig,
    *,
    which: str,
    train_fraction: float = 0.7,
    epochs: int = 300,
    learning_rate: float = 0.3,
    l2: float = 0.001,
) -> tuple[LogisticModel, TrainingReport]:
    """Train an entry or exit model on the earlier ``train_fraction`` of recorded markets (time-ordered, one
    window embargoed at the boundary, exactly :func:`btcbot.lab.split_windows`'s split) and score it on the
    later markets it never trained on. ``validate_brier`` -- not ``train_brier`` -- is the number that means
    anything; a low train Brier score with a much higher validate one is the model fitting noise in the
    training windows, the same overfitting risk :mod:`btcbot.lab`'s own train/test gap warns about. This is a
    calibration measure, not a PnL or profitability claim."""
    if which not in ("entry", "exit"):
        raise MLPipelineError(f"which must be 'entry' or 'exit', got {which!r}")
    train_tickers, validate_tickers, _ = split_windows(data, train_fraction)
    train_prepared = prepare_replay(data, config, tickers=train_tickers)
    validate_prepared = prepare_replay(data, config, tickers=validate_tickers)
    train_entry_ex, train_exit_ex = build_training_examples(train_prepared, config, train_prepared.settlements)
    validate_entry_ex, validate_exit_ex = build_training_examples(validate_prepared, config, validate_prepared.settlements)

    train_examples, validate_examples, trainer = (
        (train_entry_ex, validate_entry_ex, train_entry_model) if which == "entry"
        else (train_exit_ex, validate_exit_ex, train_exit_model)
    )
    if len(validate_examples) < MIN_VALIDATE_EXAMPLES:
        raise MLPipelineError(
            f"only {len(validate_examples)} labeled validate-window examples; need at least {MIN_VALIDATE_EXAMPLES}"
        )
    model, train_brier = trainer(train_examples, epochs=epochs, learning_rate=learning_rate, l2=l2)
    validate_brier = brier_score(model, [r for r, _ in validate_examples], [y for _, y in validate_examples])

    report = TrainingReport(
        which=which, windows_train=len(train_tickers), windows_validate=len(validate_tickers),
        train_examples=len(train_examples), validate_examples=len(validate_examples),
        train_brier=train_brier, validate_brier=validate_brier,
    )
    return model, report


# --------------------------------------------------------------------------- training from btcbot.features rows

FEATURE_STORE_ENTRY_FEATURES = (
    "tau_sec", "spot_minus_strike", "spot_move_60s", "spot_move_300s", "spot_move_900s",
    "yes_spread", "yes_bid_size", "no_bid_size", "yes_depth3", "no_depth3", "book_imbalance",
    "p_model", "sigma", "model_minus_market",
)
"""The subset of :data:`btcbot.features.COLUMNS` used to train/predict an entry model here -- deliberately
excludes identifiers (source/ticker/ts), the label (outcome_yes), and raw price levels already summarized by
yes_spread/yes_mid/model_minus_market. Because these are the SAME names btcbot.features' rows already carry,
a features-store row can be passed straight into :meth:`btcbot.ml_model.LogisticModel.predict_proba` -- no
translation layer, and no separate schema for training versus inference -- unlike
:mod:`btcbot.ml_features`' tick-level replay schema (see docs/research/ml-layers-handoff.md's "feature
schemas" section for why the two are not interchangeable)."""


def _feature_store_examples(rows: Sequence[Mapping[str, Any]], feature_names: Sequence[str]) -> list[Example]:
    """Settled rows only (``outcome_yes`` is not None), and only those with a real value for every named
    feature -- a row missing one (e.g. ``spot_move_900s`` before 900s of causal history exist) contributes
    nothing usable, and :func:`btcbot.ml_model.fit`'s single mean/variance pass has no way to skip just one
    cell within an otherwise-used row, so the whole row is dropped rather than silently biased toward zero."""
    examples: list[Example] = []
    for row in rows:
        if row.get("outcome_yes") is None:
            continue
        if any(row.get(name) is None for name in feature_names):
            continue
        examples.append(({name: row[name] for name in feature_names}, bool(row["outcome_yes"])))
    return examples


def train_from_feature_rows(
    rows: Sequence[Mapping[str, Any]],
    feature_names: Sequence[str] = FEATURE_STORE_ENTRY_FEATURES,
    *,
    epochs: int = 300,
    learning_rate: float = 0.3,
    l2: float = 0.001,
) -> tuple[LogisticModel, float]:
    """Trains directly on :mod:`btcbot.features`' row schema (``btcbot features``'s CSV output, or the same
    rows in memory) instead of replaying a recorder database -- the richer, book-imbalance/depth/multi-window
    -momentum feature set the validation and calibration tooling already share. Returns ``(model,
    train_brier)``; see :func:`train_and_validate_from_features` for a proper held-out score."""
    examples = _feature_store_examples(rows, feature_names)
    return _fit_and_score(examples, feature_names, epochs=epochs, learning_rate=learning_rate, l2=l2)


@dataclass(frozen=True, slots=True)
class FeatureStoreTrainingReport:
    windows_train: int
    windows_validate: int
    train_examples: int
    validate_examples: int
    train_brier: float
    validate_brier: float
    baseline_validate_brier: float | None  # the bot's own recorded p_blend, scored on the SAME validate rows
    beats_baseline: bool | None  # None: no baseline rows to compare (p_blend was never logged for them)


def train_and_validate_from_features(
    rows: Sequence[Mapping[str, Any]],
    feature_names: Sequence[str] = FEATURE_STORE_ENTRY_FEATURES,
    *,
    train_fraction: float = 0.7,
    embargo: int = 1,
    epochs: int = 300,
    learning_rate: float = 0.3,
    l2: float = 0.001,
) -> tuple[LogisticModel, FeatureStoreTrainingReport]:
    """Same discipline as :func:`train_and_validate` (time-ordered, embargoed, whole-market split; a Brier
    score on markets the model never trained on, never a PnL claim) but for :mod:`btcbot.features`' row
    schema -- and it goes one step further: it also scores the bot's OWN currently-deployed model
    (``p_blend``, via :func:`btcbot.validation.blend_predictor`'s same field) on the exact same held-out
    rows, so ``FeatureStoreTrainingReport.beats_baseline`` answers the question this whole handoff exists
    for -- does the trained model actually do better than what is already running, on data it never saw --
    instead of reporting a Brier score with nothing to compare it against."""
    try:
        train_tickers, validate_tickers = split_feature_windows(list(rows), train_fraction, embargo)
    except ValidationError as exc:
        raise MLPipelineError(str(exc)) from None
    train_ticker_set, validate_ticker_set = set(train_tickers), set(validate_tickers)
    train_rows = [r for r in rows if r["ticker"] in train_ticker_set]
    validate_rows = [r for r in rows if r["ticker"] in validate_ticker_set]

    train_examples = _feature_store_examples(train_rows, feature_names)
    validate_examples = _feature_store_examples(validate_rows, feature_names)
    if len(validate_examples) < MIN_VALIDATE_EXAMPLES:
        raise MLPipelineError(
            f"only {len(validate_examples)} usable validate-window rows; need at least {MIN_VALIDATE_EXAMPLES}"
        )
    model, train_brier = _fit_and_score(train_examples, feature_names, epochs=epochs, learning_rate=learning_rate, l2=l2)
    validate_brier = brier_score(model, [r for r, _ in validate_examples], [y for _, y in validate_examples])

    baseline_pairs = [
        (row["p_blend"], bool(row["outcome_yes"])) for row in validate_rows
        if row.get("p_blend") is not None and row.get("outcome_yes") is not None
    ]
    baseline_brier = None
    beats_baseline = None
    if baseline_pairs:
        baseline_brier = sum((p - (1.0 if y else 0.0)) ** 2 for p, y in baseline_pairs) / len(baseline_pairs)
        beats_baseline = validate_brier < baseline_brier

    report = FeatureStoreTrainingReport(
        windows_train=len(train_tickers), windows_validate=len(validate_tickers),
        train_examples=len(train_examples), validate_examples=len(validate_examples),
        train_brier=train_brier, validate_brier=validate_brier,
        baseline_validate_brier=baseline_brier, beats_baseline=beats_baseline,
    )
    return model, report
