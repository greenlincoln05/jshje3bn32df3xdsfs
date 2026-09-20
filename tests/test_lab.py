"""Offline tests for the strategy lab: synthetic windows in a real recorder-schema SQLite file, no network."""

import dataclasses
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from btcbot.backtest import (
    EntryFilters,
    ReplayData,
    load_replay_data,
    merge_replay_data,
    prepare_replay,
    replay_prepared,
    run_backtest,
)
from btcbot.config import BotConfig, ExitRules, Sizing
from btcbot.candidate_suite import load_candidate_suite, render_candidate_suite, run_candidate_suite
from btcbot.lab import (
    AccountSettings,
    LabError,
    LabParams,
    expand_grid,
    parse_values,
    render_lab_report,
    render_ml_ablation_report,
    run_lab,
    run_ml_ablation,
    split_windows,
)
from btcbot.ml_model import save_model
from btcbot.paper_broker import QueueAssumption
from btcbot.recorder import Recorder
from test_backtest import FULL_FILL_SIZES, _constant_model, seed_window_with_price_drop

T0 = datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc)
WIN = 1000  # seconds between window starts


def make_db(tmp_path, name="lab.sqlite"):
    db_path = tmp_path / name
    Recorder(None, series_ticker="KXBTC15M", db_path=db_path).close()
    return sqlite3.connect(str(db_path))


def seed_window(conn, index, result, *, yes_price="0.30", start=T0, sizes=None):
    """One window that, with the default ``sizes``, produces exactly one maker fill of 4 contracts (the depth
    shrinks, then refills), with spot drifting up ~+15 USD per minute. Same shape as
    tests/test_backtest.py's seed_fillable_window, including its optional full-filling ``sizes`` override."""
    sizes = sizes if sizes is not None else [15, 14, 13, 12, 11, 10, 14, 0]
    ticker = f"KXBTC15M-LAB{index:03d}-00"
    start_ts = start + timedelta(seconds=index * WIN)
    close_time = start_ts + timedelta(seconds=500)
    price = Decimal("80000") + index
    for i in range(110):
        ts = start_ts - timedelta(seconds=90) + timedelta(seconds=i)
        price = price + Decimal("1") if i % 2 == 0 else price - Decimal("0.5")
        conn.execute("INSERT INTO spot_ticks (source, price, source_ts, receive_ts, monotonic_ts) VALUES (?,?,?,?,?)",
                     ("coinbase-ws", str(price), ts.isoformat(), ts.isoformat(), float(i)))
    open_time = start_ts - timedelta(seconds=300)
    conn.execute(
        """INSERT INTO market_state (ticker, event_ticker, poll_ts, status, strike, open_time, close_time, volume, open_interest)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (ticker, ticker.rsplit("-", 1)[0], open_time.isoformat(), "active", "80000", open_time.isoformat(),
         close_time.isoformat(), "0", "0"))
    for offset, size in enumerate(sizes):
        ts = start_ts + timedelta(seconds=offset)
        payload = json.dumps({"yes": [[yes_price, str(size)]], "no": [["0.68", "15"]]})
        conn.execute(
            """INSERT INTO orderbook_snapshots (ticker, request_started_ts, poll_ts, latency_ms, yes_bid_price, yes_bid_size,
               yes_ask_price, yes_ask_size, no_bid_price, no_bid_size, no_ask_price, no_ask_size, book_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticker, ts.isoformat(), ts.isoformat(), 10.0, yes_price, str(size), None, None, "0.68", "15", None, None, payload))
    if result is not None:
        conn.execute(
            """INSERT INTO settlements (ticker, event_ticker, result, settled_avg, strike, close_time, finalized_poll_ts)
               VALUES (?,?,?,?,?,?,?)""",
            (ticker, ticker.rsplit("-", 1)[0], result, "80000", "80000", close_time.isoformat(), close_time.isoformat()))
    conn.commit()
    return ticker


def seeded(tmp_path, results):
    conn = make_db(tmp_path)
    for i, result in enumerate(results):
        seed_window(conn, i, result)
    return load_replay_data(conn)


def replay(data, config=None, *, filters=None, tickers=None):
    config = config or BotConfig()
    prepared = prepare_replay(data, config, tickers=tickers)
    return replay_prepared(prepared, config, queue_assumption=QueueAssumption.OPTIMISTIC, filters=filters)


# --------------------------------------------------------------------------- grid parsing


class TestParsing:
    def test_lab_baseline_inherits_live_price_bounds(self):
        config = BotConfig(min_price=Decimal("0.20"), max_price=Decimal("0.80"))

        params = LabParams.from_config(config)

        assert params.min_price == Decimal("0.20")
        assert params.max_price == Decimal("0.80")

    def test_typed_values_and_optional_none(self):
        assert parse_values("min_edge", "0.02, 0.04") == [Decimal("0.02"), Decimal("0.04")]
        assert parse_values("min_tau_sec", "30,120") == [30, 120]
        assert parse_values("max_price", "none, 0.6") == [None, Decimal("0.6")]
        assert parse_values("trend_mode", "Off, WITH") == ["off", "with"]
        assert parse_values("model_blend", ["0.3", 0.7]) == [0.3, 0.7]
        assert parse_values("book_move_mode", "against, with") == ["against", "with"]
        assert parse_values("book_move_min", "0.03,0.08") == [Decimal("0.03"), Decimal("0.08")]

    @pytest.mark.parametrize("key,raw", [
        ("min_edge", "1.5"), ("min_edge", "abc"), ("min_tau_sec", "1.5"), ("trend_mode", "sideways"),
        ("max_price", "1.2"), ("risk_pct", "0"), ("bogus", "1"), ("min_edge", ""), ("min_edge", "NaN"),
        ("book_move_mode", "sideways"), ("book_move_min", "1.0"), ("book_move_lookback_sec", "2"),
    ])
    def test_bad_values_are_rejected(self, key, raw):
        with pytest.raises(LabError):
            parse_values(key, raw)

    def test_contradictory_combinations_are_dropped_not_errors(self):
        base = LabParams.from_config(BotConfig())
        combos = expand_grid(base, {"min_tau_sec": [30, 700], "max_tau_sec": [600, 780],
                                    "min_price": [Decimal("0.5")], "max_price": [Decimal("0.4"), Decimal("0.6")]})
        assert all(c.min_tau_sec < c.max_tau_sec for c in combos)
        assert all(c.max_price == Decimal("0.6") for c in combos)
        assert len(combos) == 3  # (30,600), (30,780), (700,780)

    def test_too_many_combinations_is_an_error(self):
        base = LabParams.from_config(BotConfig())
        with pytest.raises(LabError, match="too many"):
            expand_grid(base, {"min_edge": [Decimal(i) / 1000 for i in range(1, 30)],
                               "max_spread": [Decimal(i) / 100 for i in range(1, 30)]}, max_combos=100)


# --------------------------------------------------------------------------- split


class TestSplit:
    def test_train_is_earlier_than_test_with_a_window_skipped(self, tmp_path):
        data = seeded(tmp_path, ["yes"] * 10)
        train, test, ordered = split_windows(data, 0.7)
        assert len(ordered) == 10 and train.isdisjoint(test)
        assert train == set(ordered[:7]) and test == set(ordered[8:])  # ordered[7] is the embargo

    def test_too_few_windows_explains_what_to_do(self, tmp_path):
        data = seeded(tmp_path, ["yes"] * 3)
        with pytest.raises(LabError, match="at least 6"):
            split_windows(data, 0.7)


# --------------------------------------------------------------------------- filters and sizing


class TestFilters:
    def test_no_filters_is_identical_to_the_plain_backtest(self, tmp_path):
        conn = make_db(tmp_path)
        for i, r in enumerate(["yes", "no", "yes"]):
            seed_window(conn, i, r)
        plain = run_backtest(conn, BotConfig(), queue_assumption=QueueAssumption.OPTIMISTIC)
        result = replay(load_replay_data(conn))
        assert len(result.trades) == plain.trades == 3
        assert sum((t.pnl_usd for t in result.trades), Decimal(0)) == plain.total_pnl_usd

    def test_price_band_blocks_and_counts(self, tmp_path):
        data = seeded(tmp_path, ["yes"] * 3)
        blocked = replay(data, filters=EntryFilters(max_price=Decimal("0.20")))
        assert blocked.trades == [] and blocked.filter_counts["price_band"] > 0
        allowed = replay(data, filters=EntryFilters(min_price=Decimal("0.25"), max_price=Decimal("0.35")))
        assert len(allowed.trades) == 3
        assert replay(data, filters=EntryFilters(min_price=Decimal("0.31"))).trades == []

    def test_trend_with_and_against(self, tmp_path):
        data = seeded(tmp_path, ["yes"] * 3)  # spot drifts up, the strategy wants YES
        with_trend = replay(data, filters=EntryFilters(trend_mode="with", trend_lookback_sec=60, trend_min_move_usd=Decimal(5)))
        assert len(with_trend.trades) == 3
        against = replay(data, filters=EntryFilters(trend_mode="against", trend_lookback_sec=60, trend_min_move_usd=Decimal(5)))
        assert against.trades == [] and against.filter_counts["trend"] > 0
        too_strict = replay(data, filters=EntryFilters(trend_mode="with", trend_lookback_sec=60, trend_min_move_usd=Decimal(500)))
        assert too_strict.trades == []

    def test_missing_trend_history_is_not_also_counted_as_a_trend_direction_rejection(self, tmp_path):
        # Each seeded window only has ~110s of prior spot history, so a lookback well past that (and past
        # the gap between windows) can never resolve: those decisions belong in "trend_missing_history"
        # only, not double-booked into "trend" (a real direction rejection) too.
        data = seeded(tmp_path, ["yes"] * 3)
        result = replay(data, filters=EntryFilters(trend_mode="with", trend_lookback_sec=200, trend_min_move_usd=Decimal(5)))
        assert result.trades == []
        assert result.filter_counts["trend_missing_history"] > 0
        assert result.filter_counts["trend"] == 0

    def test_book_move_filter_requires_causal_history(self, tmp_path):
        data = seeded(tmp_path, ["yes"])
        result = replay(
            data,
            filters=EntryFilters(book_move_mode="against", book_move_lookback_sec=60, book_move_min=Decimal("0.03")),
        )
        assert result.trades == []
        assert result.filter_counts["book_move_missing_history"] > 0

    def test_percent_of_account_sizing_and_bankroll(self, tmp_path):
        data = seeded(tmp_path, ["yes", "yes", "no", "yes"])
        config = BotConfig()
        result = replay(data, config, filters=EntryFilters(account_usd=Decimal(500), risk_pct_per_trade=Decimal("0.05")))
        # 5% of 500 = $25 at 0.30 -> 83 contracts ordered; the simulated fill is what the book gives (4)
        assert len(result.trades) == 4 and all(t.size == 4 for t in result.trades)
        assert result.final_bankroll == Decimal(500) + sum((t.pnl_usd for t in result.trades), Decimal(0))
        assert len(result.equity_curve) == 4

    def test_a_tiny_account_cannot_afford_a_contract(self, tmp_path):
        data = seeded(tmp_path, ["yes"] * 3)
        result = replay(data, filters=EntryFilters(account_usd=Decimal(5), risk_pct_per_trade=Decimal("0.01")))
        assert result.trades == [] and result.filter_counts["too_small"] > 0

    def test_train_and_test_subsets_replay_only_their_windows(self, tmp_path):
        data = seeded(tmp_path, ["yes"] * 10)
        train, test, _ = split_windows(data, 0.7)
        assert {t.ticker for t in replay(data, tickers=train).trades} <= train
        assert {t.ticker for t in replay(data, tickers=test).trades} <= test


class TestRampSizing:
    """sizing.mode "ramp" is still fully supported but no longer the shipped live default (percent-of-account
    is -- see TestPercentDefaultInLab below), and the lab's replay only ever sized flat contracts unless a
    caller passed EntryFilters.ramp_growth_pct explicitly -- so btcbot lab did not reflect ramp sizing on its
    own (docs/research/overnight-handoff.md) even while it WAS the default; it still needs it passed
    explicitly now that trying it is opt-in. FULL_FILL_SIZES (unlike seed_window's own default, a
    deliberately partial fill) fills whatever is actually ordered, so trade sizes below are the true ramped
    order sizes, not a fixed partial-fill amount."""

    def test_a_loss_resets_to_base_and_a_later_win_ramps_again(self, tmp_path):
        conn = make_db(tmp_path)
        for i, result in enumerate(["yes", "no", "yes", "yes"]):
            seed_window(conn, i, result, sizes=FULL_FILL_SIZES)
        config = BotConfig(sizing=Sizing(mode="ramp"))  # contracts_per_trade=5, ramp_growth_pct=20

        result = replay(load_replay_data(conn), config, filters=EntryFilters(ramp_growth_pct=config.sizing.ramp_growth_pct))

        sizes = [t.size for t in sorted(result.trades, key=lambda t: t.entry_ts)]
        assert sizes == [Decimal(5), Decimal(6), Decimal(5), Decimal(6)]

    def test_the_lab_baseline_does_not_auto_apply_ramp_now_that_it_is_not_the_default(self):
        params = LabParams.from_config(BotConfig())
        assert params.ramp_growth_pct is None

    def test_from_config_applies_ramp_only_when_it_is_the_configured_mode(self):
        params = LabParams.from_config(BotConfig(sizing=Sizing(mode="ramp")))
        assert params.ramp_growth_pct == Decimal(20)
        params = LabParams.from_config(BotConfig(sizing=Sizing(mode="fixed")))
        assert params.ramp_growth_pct is None

    def test_ramp_growth_pct_is_a_sweepable_grid_axis(self):
        assert parse_values("ramp_growth_pct", "0, 20, none") == [Decimal(0), Decimal(20), None]
        base = LabParams.from_config(BotConfig(sizing=Sizing(mode="ramp")))
        combos = expand_grid(base, {"ramp_growth_pct": [Decimal(0), Decimal(20), None]})
        assert {c.ramp_growth_pct for c in combos} == {Decimal(0), Decimal(20), None}


class TestPercentDefaultInLab:
    """Percent-of-account is now the shipped live default (test_percent_sizing.py), so -- the same reasoning
    TestRampSizing above documents for ramp when IT was the default -- the lab's baseline should reflect it
    automatically too, without asking for it explicitly via --grid."""

    def test_the_lab_baseline_reflects_the_shipped_percent_default(self):
        params = LabParams.from_config(BotConfig())
        assert params.risk_pct == Decimal("0.5")
        assert params.max_growth_pct == Decimal(20)

    def test_from_config_does_not_apply_percent_for_other_sizing_modes(self):
        params = LabParams.from_config(BotConfig(sizing=Sizing(mode="fixed")))
        assert params.risk_pct is None and params.max_growth_pct is None
        params = LabParams.from_config(BotConfig(sizing=Sizing(mode="ramp")))
        assert params.risk_pct is None and params.max_growth_pct is None


class TestExitRulesGrid:
    """Stop-loss/take-profit (btcbot.config.ExitRules, docs/research/stop-loss-handoff.md step 3) only ever
    came from whatever --config file was passed to a whole btcbot lab/backtest run -- fixed for every
    combination, so sweeping it needed separate runs compared by hand (the handoff doc's "Not done" note).
    Exposed as LabParams.stop_loss_pct/take_profit_pct/stop_min_hold_sec/stop_min_tau_sec, independent of
    sizing, so a single `--grid stop_loss_pct=...` run ranks it against everything else on the same
    train/test split as any other parameter."""

    def test_the_lab_baseline_has_exits_off_by_default(self):
        params = LabParams.from_config(BotConfig())
        assert params.stop_loss_pct is None and params.take_profit_pct is None
        assert params.stop_min_hold_sec == 0 and params.stop_min_tau_sec == 0

    def test_lab_baseline_inherits_exit_rules_from_config(self):
        config = BotConfig(exit=ExitRules(
            stop_loss_pct=Decimal("15"), take_profit_pct=Decimal("30"),
            stop_min_hold_sec=20, stop_min_tau_sec=10,
        ))

        params = LabParams.from_config(config)

        assert params.stop_loss_pct == Decimal("15")
        assert params.take_profit_pct == Decimal("30")
        assert params.stop_min_hold_sec == 20
        assert params.stop_min_tau_sec == 10

    def test_stop_loss_and_take_profit_are_sweepable_grid_axes(self):
        assert parse_values("stop_loss_pct", "none, 10, 20") == [None, Decimal(10), Decimal(20)]
        assert parse_values("take_profit_pct", "none, 15") == [None, Decimal(15)]
        assert parse_values("stop_min_hold_sec", "0, 30") == [0, 30]
        assert parse_values("stop_min_tau_sec", "0, 60") == [0, 60]
        base = LabParams.from_config(BotConfig())
        combos = expand_grid(base, {"stop_loss_pct": [None, Decimal(10), Decimal(20)]})
        assert {c.stop_loss_pct for c in combos} == {None, Decimal(10), Decimal(20)}

    def test_config_for_wires_the_swept_params_into_config_exit(self):
        from btcbot.lab import _config_for
        base = LabParams.from_config(BotConfig())
        params = dataclasses.replace(base, stop_loss_pct=Decimal("15"), take_profit_pct=Decimal("30"),
                                      stop_min_hold_sec=20, stop_min_tau_sec=10)

        config = _config_for(BotConfig(), params, AccountSettings())

        assert config.exit == ExitRules(stop_loss_pct=Decimal("15"), take_profit_pct=Decimal("30"),
                                         stop_min_hold_sec=20, stop_min_tau_sec=10)

    def test_sweeping_a_tight_stop_loss_closes_a_dropped_position_early(self, tmp_path):
        from btcbot.lab import _config_for
        conn = make_db(tmp_path)
        seed_window_with_price_drop(conn, "KXBTC15M-LAB000-00", start_ts=T0)  # no settlement inserted
        data = load_replay_data(conn)
        account = AccountSettings()
        base = LabParams.from_config(BotConfig())
        tight_stop = dataclasses.replace(base, stop_loss_pct=Decimal("20"))

        held = replay(data, _config_for(BotConfig(), base, account))
        stopped = replay(data, _config_for(BotConfig(), tight_stop, account))

        assert len(held.trades) == 1
        assert held.trades[0].exit_reason is None and held.trades[0].pnl_usd is None  # still pending, unresolved
        assert len(stopped.trades) == 1
        assert stopped.trades[0].exit_reason == "stop_loss"
        assert stopped.trades[0].pnl_usd is not None  # closed early, PnL known without the market settling


# --------------------------------------------------------------------------- merge


class TestMerge:
    def test_a_window_recorded_twice_is_replayed_once(self, tmp_path):
        a = seeded(tmp_path, ["yes", "no"])
        conn_b = make_db(tmp_path, "b.sqlite")
        for i, r in enumerate(["yes", "no"]):
            seed_window(conn_b, i, r)
        merged = merge_replay_data([a, load_replay_data(conn_b)])
        assert len(merged.snapshots) == len(a.snapshots)          # not doubled
        assert len(merged.spot_ticks) == len(a.spot_ticks)        # overlapping span not doubled either
        assert [s.poll_ts for s in merged.snapshots] == sorted(s.poll_ts for s in merged.snapshots)

    def test_disjoint_recordings_are_concatenated(self, tmp_path):
        a = seeded(tmp_path, ["yes", "no"])
        conn_b = make_db(tmp_path, "b.sqlite")
        seed_window(conn_b, 5, "yes")
        merged = merge_replay_data([a, load_replay_data(conn_b)])
        assert len({s.ticker for s in merged.snapshots}) == 3


# --------------------------------------------------------------------------- the sweep


def alternating(n):
    return ["yes" if i % 3 else "no" for i in range(n)]  # wins and losses, so per-trade PnL has variance


class TestRunLab:
    def grid(self):
        return {"min_edge": [Decimal("0.02"), Decimal("0.04")], "max_price": [None, Decimal("0.20")]}

    def test_ranks_on_train_reports_test_and_never_calls_anything_profitable(self, tmp_path):
        data = seeded(tmp_path, alternating(12))
        progress = []
        report = run_lab(data, BotConfig(), self.grid(), min_train_trades=3,
                         progress=lambda done, total, label: progress.append((done, total)))
        assert report.windows_train + report.windows_test == report.windows_total - 1
        assert report.combinations == 4 and progress[-1] == (4, 4)
        assert report.rows, "the unfiltered configurations trade on every window"
        t_stats = [r.train.t_stat for r in report.rows]
        assert t_stats == sorted(t_stats, reverse=True)
        text = render_lab_report(report)
        assert "profitable" not in report.verdict.replace("not a profitability claim", "")
        assert "combinations were tried" in " ".join(report.warnings)
        assert "Verdict [" in text and "TRAIN" in text and "TEST" in text
        # too few test trades in a 12-window sample: the lab must say so rather than crown a winner
        assert report.verdict_level == "insufficient"

    def test_the_price_band_that_blocks_everything_is_not_ranked(self, tmp_path):
        data = seeded(tmp_path, alternating(12))
        report = run_lab(data, BotConfig(), {"max_price": [Decimal("0.20")]}, min_train_trades=3)
        assert report.rows == [] and report.verdict_level == "insufficient"

    def test_account_size_and_risk_percent_flow_through(self, tmp_path):
        data = seeded(tmp_path, alternating(12))
        report = run_lab(data, BotConfig(), {"risk_pct": [Decimal(2), Decimal(5)]},
                         account=AccountSettings(account_usd=Decimal(1000)), min_train_trades=3)
        assert report.account_usd == "1000" and report.rows
        assert all(r.train.return_pct is not None for r in report.rows)

    def test_cancel_stops_the_sweep(self, tmp_path):
        data = seeded(tmp_path, alternating(12))
        with pytest.raises(LabError, match="cancelled"):
            run_lab(data, BotConfig(), self.grid(), min_train_trades=3, cancelled=lambda: True)

    def test_too_little_data_says_how_much_is_needed(self, tmp_path):
        data = seeded(tmp_path, ["yes", "no"])
        with pytest.raises(LabError, match="96 per day"):
            run_lab(data, BotConfig(), self.grid())


class TestMLAblation:
    """The four independently-testable layers requested in docs/research/ml-layers-handoff.md: current entry
    + settlement hold, ML entry + settlement hold, current entry + ML exit, ML entry + ML exit -- all four
    sharing the same train/test split so they are directly comparable."""

    def test_requires_at_least_one_model_path(self, tmp_path):
        data = seeded(tmp_path, alternating(12))
        with pytest.raises(LabError, match="at least one"):
            run_ml_ablation(data, BotConfig(), ml_entry_model_path=None, ml_exit_model_path=None)

    def test_rejects_a_model_trained_for_a_different_feature_schema(self, tmp_path):
        from btcbot.ml_model import LogisticModel

        data = seeded(tmp_path, alternating(12))
        # A model whose feature_names don't overlap ENTRY_FEATURES at all -- e.g. one trained on the
        # feature-store CSV's own column names (see docs/research/ml-layers-handoff.md's "feature schemas").
        wrong_schema = LogisticModel(("book_imbalance", "yes_depth3"), (1.0, 1.0), 0.0, (0.0, 0.0), (1.0, 1.0))
        entry_path = tmp_path / "wrong_schema.json"
        save_model(wrong_schema, entry_path)

        with pytest.raises(LabError, match="different feature schema"):
            run_ml_ablation(data, BotConfig(), ml_entry_model_path=str(entry_path), ml_exit_model_path=None)

    def test_four_layers_share_the_same_train_test_split(self, tmp_path):
        data = seeded(tmp_path, alternating(12))
        entry_path = tmp_path / "entry.json"
        save_model(_constant_model(1.0), entry_path)

        report = run_ml_ablation(data, BotConfig(), ml_entry_model_path=str(entry_path), ml_exit_model_path=None)

        assert [layer.name for layer in report.layers] == ["1", "2", "3", "4"]
        assert report.windows_train + report.windows_test == report.windows_total - 1  # one window embargoed

    def test_without_an_exit_model_layers_3_and_4_equal_1_and_2(self, tmp_path):
        data = seeded(tmp_path, alternating(12))
        entry_path = tmp_path / "entry.json"
        save_model(_constant_model(1.0), entry_path)

        report = run_ml_ablation(data, BotConfig(), ml_entry_model_path=str(entry_path), ml_exit_model_path=None)
        by_name = {layer.name: layer for layer in report.layers}

        assert by_name["1"].test.pnl == by_name["3"].test.pnl
        assert by_name["2"].test.pnl == by_name["4"].test.pnl
        assert "layers 3 and 4 are identical" in " ".join(report.warnings)

    def test_a_confident_no_entry_model_blocks_every_trade_without_touching_the_baseline(self, tmp_path):
        data = seeded(tmp_path, alternating(12))
        entry_path = tmp_path / "entry.json"
        save_model(_constant_model(0.0), entry_path)

        report = run_ml_ablation(data, BotConfig(), ml_entry_model_path=str(entry_path), ml_exit_model_path=None)
        by_name = {layer.name: layer for layer in report.layers}

        assert by_name["2"].train.trades == 0 and by_name["4"].train.trades == 0
        assert by_name["1"].train.trades > 0  # the un-filtered baseline is unaffected

    def test_render_produces_readable_text(self, tmp_path):
        data = seeded(tmp_path, alternating(12))
        entry_path = tmp_path / "entry.json"
        save_model(_constant_model(1.0), entry_path)
        report = run_ml_ablation(data, BotConfig(), ml_entry_model_path=str(entry_path), ml_exit_model_path=None)

        text = render_ml_ablation_report(report)

        assert "TRAIN" in text and "TEST" in text
        assert "not a profitability claim" in text
        assert "Verdict [" in text
        assert "vs layer 1" in text  # only layers 2-4 get a delta note; layer 1 is the baseline itself

    def test_a_small_seeded_dataset_is_flagged_insufficient_on_every_layer(self, tmp_path):
        # 12 windows -> nowhere near ENOUGH_TEST_TRADES=30 test trades; every layer must refuse a verdict.
        data = seeded(tmp_path, alternating(12))
        entry_path = tmp_path / "entry.json"
        save_model(_constant_model(1.0), entry_path)

        report = run_ml_ablation(data, BotConfig(), ml_entry_model_path=str(entry_path), ml_exit_model_path=None)

        assert all(layer.verdict_level == "insufficient" for layer in report.layers)


def _metrics(*, resolved, pnl, t_stat, trades=None, windows=10):
    from btcbot.lab import Metrics

    return Metrics(
        windows=windows, trades=trades if trades is not None else resolved, resolved=resolved, wins=0,
        win_rate=None, pnl=Decimal(pnl), fees=Decimal(0), avg_pnl_per_trade=None, t_stat=t_stat,
        max_drawdown=Decimal(0), return_pct=None, max_drawdown_pct=None, trades_per_day=None,
    )


class TestAblationLayerVerdict:
    def test_insufficient_below_the_test_trade_threshold(self):
        from btcbot.lab import ENOUGH_TEST_TRADES, _ablation_layer_verdict

        train = _metrics(resolved=100, pnl="50", t_stat=5.0)
        test = _metrics(resolved=ENOUGH_TEST_TRADES - 1, pnl="10", t_stat=5.0)

        level, verdict = _ablation_layer_verdict(train, test)

        assert level == "insufficient"
        assert "no conclusion" in verdict

    def test_not_supported_when_test_pnl_is_negative(self):
        from btcbot.lab import ENOUGH_TEST_TRADES, _ablation_layer_verdict

        train = _metrics(resolved=100, pnl="50", t_stat=5.0)
        test = _metrics(resolved=ENOUGH_TEST_TRADES, pnl="-5", t_stat=None)

        level, verdict = _ablation_layer_verdict(train, test)

        assert level == "not_supported"
        assert "lost money" in verdict

    def test_not_supported_when_positive_but_not_significant(self):
        from btcbot.lab import ENOUGH_TEST_TRADES, _ablation_layer_verdict

        train = _metrics(resolved=100, pnl="50", t_stat=5.0)
        test = _metrics(resolved=ENOUGH_TEST_TRADES, pnl="5", t_stat=1.0)

        level, verdict = _ablation_layer_verdict(train, test)

        assert level == "not_supported"
        assert "not distinguishable from luck" in verdict

    def test_weak_signal_when_positive_and_significant(self):
        from btcbot.lab import ENOUGH_TEST_TRADES, _ablation_layer_verdict

        train = _metrics(resolved=100, pnl="50", t_stat=3.0)
        test = _metrics(resolved=ENOUGH_TEST_TRADES, pnl="20", t_stat=2.5)

        level, verdict = _ablation_layer_verdict(train, test)

        assert level == "weak_signal"
        assert "forward paper-test" in verdict

    def test_flags_a_large_train_to_test_t_stat_drop_as_overfitting(self):
        from btcbot.lab import ENOUGH_TEST_TRADES, _ablation_layer_verdict

        train = _metrics(resolved=100, pnl="50", t_stat=10.0)
        test = _metrics(resolved=ENOUGH_TEST_TRADES, pnl="20", t_stat=2.5)

        level, verdict = _ablation_layer_verdict(train, test)

        assert level == "weak_signal"
        assert "overfitting" in verdict


class TestEquivalentSettings:
    def test_settings_that_never_bind_are_collapsed_into_one_row(self, tmp_path):
        data = seeded(tmp_path, alternating(12))
        # a 0.90 price cap never binds (bids are 0.30), so all three settings make identical trades
        report = run_lab(data, BotConfig(), {"max_price": [Decimal("0.90"), Decimal("0.80"), Decimal("0.70")]},
                         min_train_trades=3)
        assert len(report.rows) == 1 and report.rows[0].equivalent == 2
        assert "identical results" in render_lab_report(report)

    def test_matching_aggregate_metrics_do_not_hide_different_ledgers(self, tmp_path, monkeypatch):
        import btcbot.lab as lab_module

        data = seeded(tmp_path, alternating(12))
        original = lab_module._evaluate_with_signature

        def same_metrics_different_trades(prepared, base, params, account, **kwargs):
            metrics, _ = original(prepared, base, params, account, **kwargs)
            # Force the old aggregate signature to match while preserving an
            # exact-ledger difference for the two tested configurations.
            signature = ((str(params.max_price), "different-entry"),)
            return metrics, signature

        monkeypatch.setattr(lab_module, "_evaluate_with_signature", same_metrics_different_trades)
        report = run_lab(
            data, BotConfig(), {"max_price": [Decimal("0.90"), Decimal("0.80")]},
            min_train_trades=3,
        )

        assert len(report.rows) == 2
        assert all(row.equivalent == 0 for row in report.rows)

class TestAuditFixes:
    def test_demo_files_rejected_before_merge(self, tmp_path):
        from btcbot.lab import load_lab_data
        with pytest.raises(LabError, match='demo/synthetic'):
            load_lab_data([tmp_path / 'paper-prod.sqlite', tmp_path / 'demo-X-demo.sqlite'])

    def test_aligned4_requires_all_history(self, tmp_path):
        data = seeded(tmp_path, ['yes'])
        result = replay(data, filters=EntryFilters(trend_mode='aligned4'))
        assert not result.trades
        # Missing history (aligned4 needs 24h of it) is its own bucket, not also counted as a direction
        # rejection: there was never enough data to judge a direction at all.
        assert result.filter_counts['trend_missing_history'] > 0
        assert result.filter_counts['trend'] == 0

    def test_aligned4_uses_four_causal_lookbacks(self, tmp_path, monkeypatch):
        from btcbot.backtest import SpotSeries
        calls = []
        def move(self, ts, lookback):
            calls.append(lookback)
            return Decimal(10)
        monkeypatch.setattr(SpotSeries, 'move', move)
        data = seeded(tmp_path, ['yes'])
        result = replay(data, filters=EntryFilters(trend_mode='aligned4'))
        assert result.trades
        assert set(calls) == {900, 1800, 3600, 86400}
        monkeypatch.setattr(SpotSeries, 'move', lambda self, ts, lookback: Decimal(-1) if lookback == 86400 else Decimal(10))
        assert not replay(data, filters=EntryFilters(trend_mode='aligned4')).trades

    def test_spot_move_rejects_stale_current_tick(self):
        from btcbot.backtest import SpotSeries
        series = SpotSeries([(T0, Decimal(100)), (T0 + timedelta(seconds=50), Decimal(110))])
        assert series.move(T0 + timedelta(seconds=60), 60) is None

    @pytest.mark.parametrize('balance', [100, 500, 1000, 5000])
    def test_minimum_premium_scales_and_rounds_up(self, tmp_path, monkeypatch, balance):
        from btcbot.paper_broker import PaperBroker
        from btcbot.lab import _filters_for, _config_for
        placed = []
        original = PaperBroker.place_resting_order
        def record(self, side, price, size, **kwargs):
            placed.append(dict(price=price, size=size))
            return original(self, side, price, size, **kwargs)
        monkeypatch.setattr(PaperBroker, 'place_resting_order', record)
        account = AccountSettings(account_usd=Decimal(balance))
        params = LabParams.from_config(BotConfig())
        filters = _filters_for(params, account)
        replay(seeded(tmp_path, ['yes']), _config_for(BotConfig(), params, account), filters=filters)
        assert placed
        premium = placed[0]['size'] * placed[0]['price']
        assert Decimal(balance) * Decimal('.05') <= premium < Decimal(balance) * Decimal('.05') + placed[0]['price']

    def test_minimum_cannot_overdraw_account(self, tmp_path):
        result = replay(seeded(tmp_path, ['yes']), filters=EntryFilters(account_usd=Decimal(1), min_stake_usd=Decimal(5)))
        assert not result.trades
        assert result.filter_counts['too_small'] > 0

    def test_low_threshold_cannot_claim_evidence(self, tmp_path):
        report = run_lab(seeded(tmp_path, ['yes'] * 10), BotConfig(), {}, min_train_trades=1)
        assert 'Exploratory' in report.verdict or 'No combination' in report.verdict

class TestConfidenceFilters:
    def test_persistence_and_min_p_side_are_parsed_and_range_checked(self):
        assert parse_values("persist_steps", "1, 5, 30") == [1, 5, 30]
        assert parse_values("min_p_side", "none, 0.5") == [None, Decimal("0.5")]
        for key, raw in (("persist_steps", "0"), ("persist_steps", "1000"), ("min_p_side", "1.2"), ("min_p_side", "0")):
            with pytest.raises(LabError):
                parse_values(key, raw)

    def test_persistence_of_one_changes_nothing_and_a_long_requirement_blocks_every_entry(self, tmp_path):
        data = seeded(tmp_path, ["yes"] * 3)
        assert len(replay(data, filters=EntryFilters(persist_steps=1)).trades) == 3
        blocked = replay(data, filters=EntryFilters(persist_steps=10_000))
        assert blocked.trades == [] and blocked.filter_counts["persistence"] > 0

    def test_persistence_needs_that_many_consecutive_wanted_snapshots(self, tmp_path):
        data = seeded(tmp_path, ["yes"] * 3)
        base = replay(data, filters=EntryFilters(persist_steps=1))
        slower = replay(data, filters=EntryFilters(persist_steps=3))
        assert len(slower.trades) <= len(base.trades)
        assert slower.filter_counts["persistence"] >= 2  # the first two snapshots of each streak were refused
        assert all(a.entry_ts <= b.entry_ts for a, b in zip(sorted(base.trades, key=lambda t: t.ticker),
                                                          sorted(slower.trades, key=lambda t: t.ticker)))  # never earlier

    def test_min_p_side_refuses_a_side_the_model_does_not_favour(self, tmp_path):
        data = seeded(tmp_path, ["yes"] * 3)
        assert len(replay(data, filters=EntryFilters(min_p_side=Decimal("0.01"))).trades) == 3
        strict = replay(data, filters=EntryFilters(min_p_side=Decimal("0.999")))
        assert strict.trades == [] and strict.filter_counts["low_confidence"] > 0

    def test_the_lab_sweeps_the_new_keys_and_describes_them(self, tmp_path):
        data = seeded(tmp_path, alternating(12))
        report = run_lab(data, BotConfig(), {"persist_steps": [1, 3], "min_p_side": [None, Decimal("0.5")]}, min_train_trades=3)
        assert report.combinations == 4
        text = render_lab_report(report)
        assert "persist_steps" in text or "min_p_side" in text or "(defaults)" in text


class TestFrozenCandidateSuite:
    def test_named_suite_runs_only_post_cutoff_windows_and_keeps_full_ledger(self, tmp_path):
        suite_path = tmp_path / "suite.yaml"
        suite_path.write_text(
            """version: 1
frozen_at: 2026-09-19T00:00:00Z
assumptions: {account_sizes: [100, 500], max_exposure_pct: 25, daily_loss_pct: 10, maker_fee_multiplier: 0.25}
candidates:
  - name: baseline
    hypothesis: fixed before outcomes
    params: {}
""",
            encoding="utf-8",
        )
        suite = load_candidate_suite(suite_path, BotConfig())
        report = run_candidate_suite(
            seeded(tmp_path, ["yes", "no", "yes", "yes"]),
            BotConfig(),
            suite,
            after=T0 + timedelta(seconds=2 * WIN),
        )
        assert report["strategy_count"] == 1 and report["candidate_count"] == 2
        assert report["windows"] == 2
        assert {row["account_usd"] for row in report["results"]} == {Decimal(100), Decimal(500)}
        assert all(row["metrics"]["resolved"] == 2 for row in report["results"])
        assert all(len(row["trades"]) == 2 for row in report["results"])
        assert all(row["evidence"] == "insufficient" for row in report["results"])
        assert "insufficient" in render_candidate_suite(report).lower()

    def test_duplicate_names_are_rejected(self, tmp_path):
        suite_path = tmp_path / "bad.yaml"
        suite_path.write_text(
            """version: 1
frozen_at: 2026-09-19T00:00:00Z
candidates:
  - {name: same, params: {}}
  - {name: same, params: {}}
""",
            encoding="utf-8",
        )
        with pytest.raises(LabError, match="duplicate candidate"):
            load_candidate_suite(suite_path, BotConfig())
