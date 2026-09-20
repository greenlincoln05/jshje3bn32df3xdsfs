"""Offline tests for the strategy lab: synthetic windows in a real recorder-schema SQLite file, no network."""

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
from btcbot.config import BotConfig
from btcbot.lab import (
    AccountSettings,
    LabError,
    LabParams,
    expand_grid,
    parse_values,
    render_lab_report,
    run_lab,
    split_windows,
)
from btcbot.paper_broker import QueueAssumption
from btcbot.recorder import Recorder

T0 = datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc)
WIN = 1000  # seconds between window starts


def make_db(tmp_path, name="lab.sqlite"):
    db_path = tmp_path / name
    Recorder(None, series_ticker="KXBTC15M", db_path=db_path).close()
    return sqlite3.connect(str(db_path))


def seed_window(conn, index, result, *, yes_price="0.30", start=T0):
    """One window that produces exactly one maker fill of 4 contracts (the depth shrinks, then refills), with
    spot drifting up ~+15 USD per minute. Same shape as tests/test_backtest.py's seed_fillable_window."""
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
    for offset, size in enumerate([15, 14, 13, 12, 11, 10, 14, 0]):
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

    @pytest.mark.parametrize("key,raw", [
        ("min_edge", "1.5"), ("min_edge", "abc"), ("min_tau_sec", "1.5"), ("trend_mode", "sideways"),
        ("max_price", "1.2"), ("risk_pct", "0"), ("bogus", "1"), ("min_edge", ""), ("min_edge", "NaN"),
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
        assert result.filter_counts['trend'] > 0

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
