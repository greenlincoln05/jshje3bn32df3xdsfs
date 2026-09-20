"""Offline tests for the ML training pipeline: synthetic recorder-schema data, no network, no real training
data (see docs/research/ml-layers-handoff.md for what would need real recorded/downloaded data)."""

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from btcbot.backtest import load_replay_data, prepare_replay
from btcbot.config import BotConfig
from btcbot.ml_features import ENTRY_FEATURES, EXIT_FEATURES
from btcbot.ml_pipeline import (
    MLPipelineError,
    build_training_examples,
    train_and_validate,
    train_entry_model,
    train_exit_model,
)
from test_backtest import FULL_FILL_SIZES, insert_settlement, make_db, seed_fillable_window

T0 = datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc)


def seed_windows(conn, results, *, sizes=None):
    tickers = []
    for i, result in enumerate(results):
        start = T0 + timedelta(seconds=i * 700)
        ticker = f"T{i:03d}"
        close_time = seed_fillable_window(conn, ticker, start_ts=start, sizes=sizes)
        if result is not None:
            insert_settlement(conn, ticker, result, strike=Decimal("80000"), close_time=close_time)
        tickers.append(ticker)
    return tickers


class TestBuildTrainingExamples:
    def test_labels_entries_by_eventual_settlement(self, tmp_path):
        conn = make_db(tmp_path)
        seed_windows(conn, ["yes", "no", "yes"], sizes=FULL_FILL_SIZES)
        data = load_replay_data(conn)
        config = BotConfig()
        prepared = prepare_replay(data, config)

        entry_examples, _ = build_training_examples(prepared, config, prepared.settlements)

        assert len(entry_examples) == 3
        # The strategy only ever takes YES here (spot trends up in seed_fillable_window), so a "yes"
        # settlement is a win and a "no" settlement is a loss for every one of these entries.
        assert sorted(label for _, label in entry_examples) == [False, True, True]
        for features, _ in entry_examples:
            assert set(features) == set(ENTRY_FEATURES)

    def test_a_position_that_never_settles_produces_no_entry_example(self, tmp_path):
        conn = make_db(tmp_path)
        seed_fillable_window(conn, "T0", start_ts=T0, sizes=FULL_FILL_SIZES)  # no settlement row at all
        data = load_replay_data(conn)
        config = BotConfig()
        prepared = prepare_replay(data, config)

        entry_examples, _ = build_training_examples(prepared, config, prepared.settlements)

        assert entry_examples == []

    def test_exit_examples_are_produced_while_a_labeled_position_is_held(self, tmp_path):
        conn = make_db(tmp_path)
        seed_windows(conn, ["yes"], sizes=FULL_FILL_SIZES)
        data = load_replay_data(conn)
        config = BotConfig()
        prepared = prepare_replay(data, config)

        _, exit_examples = build_training_examples(prepared, config, prepared.settlements)

        assert exit_examples  # FULL_FILL_SIZES leaves post-entry ticks before the window ends
        for features, _ in exit_examples:
            assert set(features) == set(EXIT_FEATURES)

    def test_selling_now_beats_holding_when_price_rose_but_the_market_settles_no(self, tmp_path):
        conn = make_db(tmp_path)
        ticker = "T0"
        close_time = seed_fillable_window(conn, ticker, start_ts=T0, sizes=FULL_FILL_SIZES, yes_price="0.30")
        # A YES position entered at 0.30 whose book later shows a much higher bid (say 0.90) is worth far
        # more sold now than a "no" settlement, which pays 0 -- selling now must beat holding here.
        from test_backtest import insert_snapshot
        insert_snapshot(conn, ticker, T0 + timedelta(seconds=len(FULL_FILL_SIZES)), yes=[("0.90", "20")], no=[("0.09", "20")])
        insert_settlement(conn, ticker, "no", strike=Decimal("80000"), close_time=close_time)
        data = load_replay_data(conn)
        config = BotConfig()
        prepared = prepare_replay(data, config)

        _, exit_examples = build_training_examples(prepared, config, prepared.settlements)

        assert any(label is True for _, label in exit_examples)


class TestTrainModels:
    def test_rejects_too_few_examples(self):
        with pytest.raises(MLPipelineError):
            train_entry_model([({"edge": 0.1}, True)] * 5)
        with pytest.raises(MLPipelineError):
            train_exit_model([({"held_sec": 10.0}, True)] * 5)

    def test_fits_and_reports_a_brier_score_in_range(self):
        examples = [({"edge": 0.2, "p_side": 0.6, "price": 0.4, "tau_sec": 300.0, "spread": 0.02,
                      "depth": 10.0, "sigma": 0.0004, "momentum_60s": 1.0}, True)] * 15 + \
                   [({"edge": -0.2, "p_side": 0.3, "price": 0.6, "tau_sec": 300.0, "spread": 0.02,
                      "depth": 10.0, "sigma": 0.0004, "momentum_60s": -1.0}, False)] * 15
        model, brier = train_entry_model(examples)
        assert 0.0 <= brier <= 1.0
        assert model.feature_names == ENTRY_FEATURES


class TestTrainAndValidate:
    def test_rejects_unknown_which(self, tmp_path):
        conn = make_db(tmp_path)
        seed_windows(conn, ["yes"] * 10, sizes=FULL_FILL_SIZES)
        data = load_replay_data(conn)
        with pytest.raises(MLPipelineError, match="which"):
            train_and_validate(data, BotConfig(), which="bogus")

    def test_trains_an_entry_model_end_to_end_on_a_time_ordered_split(self, tmp_path):
        conn = make_db(tmp_path)
        results = ["yes" if i % 3 else "no" for i in range(30)]  # some variance for labels
        seed_windows(conn, results, sizes=FULL_FILL_SIZES)
        data = load_replay_data(conn)

        model, report = train_and_validate(data, BotConfig(), which="entry", train_fraction=0.7)

        assert report.which == "entry"
        assert report.windows_train + report.windows_validate < 30  # one window embargoed at the boundary
        assert report.train_examples > 0 and report.validate_examples >= 5
        assert 0.0 <= report.train_brier <= 1.0 and 0.0 <= report.validate_brier <= 1.0
        assert model.feature_names == ENTRY_FEATURES

    def test_too_few_windows_for_a_validate_split_raises(self, tmp_path):
        conn = make_db(tmp_path)
        seed_windows(conn, ["yes"] * 6, sizes=FULL_FILL_SIZES)  # split_windows' own minimum, but too few after it
        data = load_replay_data(conn)
        with pytest.raises(MLPipelineError):
            train_and_validate(data, BotConfig(), which="entry")
