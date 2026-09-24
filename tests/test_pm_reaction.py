"""Offline tests for btcbot.pm_reaction. Synthetic data only (tests/pm_synthetic.py): a random-walk BTC and a
simulated Polymarket that prices it with a KNOWN lag -- these tests check that the analysis recovers what was
planted and refuses what was not, never that anything is true of the real market."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from pm_synthetic import make_db  # noqa: E402

from btcbot.cli import main  # noqa: E402
from btcbot.pm_history import HistTrade, HistWindow  # noqa: E402
from btcbot.pm_reaction import (  # noqa: E402
    REACTION_CONTROL_FEATURES,
    REACTION_FEATURES,
    Example,
    PmReactionError,
    build_examples,
    build_series,
    detect_btc_shocks,
    detect_strike_crosses,
    features_at,
    load_series_from_history,
    load_series_from_recorder,
    paired_window_t,
    render_study,
    run_study,
    split_by_window,
)
from btcbot.polymarket_recorder import PolymarketRecorder  # noqa: E402

START = 1_790_000_100
END = START + 900


def flat_btc(price=60_000.0, *, first=START - 400, last=END + 5, jump_at=None, jump=1.0):
    bars = {}
    for ts in range(first, last):
        p = price * (jump if jump_at is not None and ts >= jump_at else 1.0)
        # a tiny alternating wiggle so realized vol is not zero
        bars[ts] = (p * (1.0 + (1e-5 if ts % 2 else -1e-5)), 1.0, 0.5)
    return bars


def window(result_up=True):
    return HistWindow(f"btc-updown-15m-{START}", START, END, result_up, False)


class TestBuildSeries:
    def test_timing_convention_no_lookahead(self):
        bars = flat_btc()
        bars[START + 99] = (61_000.0, 1.0, 0.5)  # the bar OPENING at START+99 ...
        trades = [HistTrade(START + 99, 0.7, 5.0, "up")]  # ... and a print STAMPED START+99
        ws = build_series(window(), trades, bars)
        i = ws.start_idx + 100
        assert ws.btc[i] == 61_000.0 and ws.btc[i - 1] != 61_000.0  # visible only at instant START+100
        assert ws.pm[i] == 0.7 and ws.pm[i - 1] is None

    def test_vwap_down_conversion_initial_value_and_complement(self):
        trades = [
            HistTrade(START - 1000, 0.52, 1.0, "up"),  # before the series: seeds the forward fill
            HistTrade(START + 10, 0.60, 3.0, "up"),
            HistTrade(START + 10, 0.64, -1.0, "down"),  # a Down print already in Up terms
        ]
        ws = build_series(window(), trades, flat_btc())
        assert ws.pm[0] == 0.52
        assert ws.pm[ws.start_idx + 11] == pytest.approx((0.60 * 3 + 0.64 * 1) / 4)
        assert ws.complement_gaps == [pytest.approx(0.04)]

    def test_no_binance_price_at_the_window_edges_means_no_series(self):
        # Data that stops before the window must not be forward-filled into a fake open AND close.
        bars = {ts: v for ts, v in flat_btc().items() if ts < START - 10}
        assert build_series(window(), [], bars) is None
        tail_gap = {ts: v for ts, v in flat_btc().items() if ts < END - 20}
        assert build_series(window(), [], tail_gap) is None

    def test_a_long_gap_inside_the_window_drops_it_but_a_few_seconds_are_filled(self):
        bars = flat_btc()
        few = {ts: v for ts, v in bars.items() if not START + 100 <= ts < START + 110}
        ws = build_series(window(), [], few)
        assert ws is not None and ws.btc_missing == 10
        many = {ts: v for ts, v in bars.items() if not START + 100 <= ts < START + 200}
        assert build_series(window(), [], many) is None

    def test_features_never_depend_on_later_data(self):
        bars = flat_btc()
        trades = [HistTrade(START + s, 0.5 + s / 10_000, 1.0, "up") for s in range(0, 900, 3)]
        ws_a = build_series(window(), trades, bars)
        later = dict(bars)
        for ts in range(START + 400, END + 5):
            later[ts] = (70_000.0, 9.0, 9.0)
        ws_b = build_series(window(), trades + [HistTrade(START + 500, 0.99, 50.0, "up")], later)
        i = ws_a.start_idx + 300
        assert features_at(ws_a, i) == features_at(ws_b, i)


class TestExamples:
    def test_rows_with_a_stale_last_print_are_dropped(self):
        trades = [HistTrade(START + 62, 0.5, 1.0, "up")]  # one print (visible at START+63), then silence
        ws = build_series(window(), trades, flat_btc())
        rows = build_examples([ws], sample_every=1, max_staleness_sec=5)
        assert [e.t for e in rows] == [START + 63 + k for k in range(6)]

    def test_future_move_needs_a_print_inside_the_horizon(self):
        trades = [HistTrade(START + s, 0.5 if s < 200 else 0.6, 1.0, "up") for s in range(60, 900)]
        ws = build_series(window(), trades, flat_btc())
        rows = {e.t: e for e in build_examples([ws], sample_every=1, horizon_sec=10)}
        assert rows[START + 195].future_move == pytest.approx(0.1)
        assert rows[START + 100].future_move == pytest.approx(0.0)


class TestSplitAndStats:
    def _examples(self, n_windows):
        return [Example("s", 1000 * w, 1000 * w + k, {}, True, None) for w in range(n_windows) for k in range(3)]

    def test_split_is_time_ordered_grouped_and_embargoed(self):
        train, val, n_tr, n_va = split_by_window(self._examples(30), 0.7, embargo=1)
        assert n_tr == 21 and n_va == 8
        assert max(e.window_start for e in train) < min(e.window_start for e in val)
        assert {e.window_start for e in train}.isdisjoint({e.window_start for e in val})

    def test_refuses_too_few_windows(self):
        with pytest.raises(PmReactionError):
            split_by_window(self._examples(10), 0.7)

    def test_paired_t_is_negative_when_a_is_better(self):
        ex = self._examples(10)
        labels = [True] * len(ex)
        better = [0.9 - 0.02 * ((e.window_start // 1000) % 4) for e in ex]  # varies across windows
        assert paired_window_t(ex, better, [0.5] * len(ex), labels) < -2
        assert paired_window_t(ex, [0.5] * len(ex), better, labels) > 2


class TestEvents:
    def test_a_btc_jump_is_one_shock_in_its_direction(self):
        ws = build_series(window(), [HistTrade(START, 0.5, 1.0, "up")], flat_btc(jump_at=START + 300, jump=0.99))
        shocks = detect_btc_shocks(ws)
        assert len(shocks) == 1 and shocks[0].direction == -1
        assert ws.t0 + shocks[0].idx == START + 301

    def test_crossing_the_opening_price(self):
        bars = flat_btc()
        for ts in range(START + 200, END + 5):
            bars[ts] = (60_100.0, 1.0, 0.5)
        ws = build_series(window(), [HistTrade(START, 0.5, 1.0, "up")], bars)
        (cross,) = detect_strike_crosses(ws)
        assert cross.direction == 1 and ws.t0 + cross.idx == START + 201


_STUDIES: dict = {}


def synthetic_study(**kw):
    """Cached per parameter set: several tests read different parts of the same (read-only) study."""
    key = tuple(sorted(kw.items()))
    if key not in _STUDIES:
        conn = sqlite3.connect(":memory:")
        make_db(conn, **kw)
        n, series = load_series_from_history(conn)
        _STUDIES[key] = run_study(n, series)
    return _STUDIES[key]


class TestStudyOnPlantedLags:
    """The harness must find a planted slow reaction AND must not invent one when there is none."""

    def test_a_planted_three_second_lag_is_recovered(self):
        study = synthetic_study(windows=120, lag=3)
        assert study.validity.lead_lag_peak in (3, 4)  # 1 s buckets can add one second
        shock = study.events[0]
        by_lag = {r.lag: r for r in shock.rows}
        assert by_lag[0].pm_mean < 0.3 * by_lag[10].pm_mean  # little response at the event, most by 10 s
        assert shock.half_life_sec is not None and shock.half_life_sec <= 5
        assert study.reaction.beats_control and study.reaction.vs_control_t < -2

    def test_no_planted_lag_is_not_reported_as_a_slow_reaction(self):
        # Bid-ask bounce and within-second timing alone made a Polymarket-only baseline lose (t < -6) before
        # the control features existed; this is the regression test for that false positive.
        study = synthetic_study(windows=120, lag=0, half_spread=0.01)
        assert study.reaction is not None and not study.reaction.beats_control

    def test_the_market_itself_is_not_beaten_when_it_is_already_fair(self):
        study = synthetic_study(windows=120, lag=0, half_spread=0.01)
        assert study.outcome is not None and not study.outcome.beats_market
        assert study.outcome.vs_market_t is not None

    def test_the_control_is_nested_in_the_full_model(self):
        assert set(REACTION_CONTROL_FEATURES) < set(REACTION_FEATURES)


class TestValidityWarnings:
    def test_a_clean_dataset_has_no_warnings(self):
        study = synthetic_study(windows=40, lag=3)
        v = study.validity
        assert v.warnings == () and v.label_agree == v.label_checked == 40 and v.windows_no_btc == 0

    def test_polymarket_leading_binance_is_flagged_as_a_clock_problem(self):
        study = synthetic_study(windows=40, lag=-4)
        assert study.validity.lead_lag_peak < 0
        assert any("clock" in w for w in study.validity.warnings)

    def test_resolution_disagreeing_with_binance_is_flagged(self):
        study = synthetic_study(windows=40, lag=2, flip_labels=8)
        assert study.validity.label_agree == 32
        assert any("resolution" in w for w in study.validity.warnings)

    def test_too_few_windows_reports_instead_of_training(self):
        study = synthetic_study(windows=8, lag=2)
        assert study.outcome is None and "usable windows" in study.outcome_error
        assert "not trained" in render_study(study)


class TestRenderWording:
    def test_never_calls_anything_profitable(self):
        text = render_study(synthetic_study(windows=40, lag=3)).lower()
        assert "not a profitability claim" in text
        assert "profitable" not in text.replace("not a profitability claim", "")


class TestRecorderAdapter:
    def test_book_mids_from_a_record_polymarket_database(self, tmp_path):
        rec_path = tmp_path / "polymarket-btc-updown-15m-x.sqlite"
        recorder = PolymarketRecorder(object(), db_path=rec_path, kill_file=tmp_path / "KILL_PM")
        recorder.close()
        rconn = sqlite3.connect(str(rec_path))
        slug = f"btc-updown-15m-{START}"
        from datetime import datetime, timezone

        for s in range(0, 900, 2):
            ts = datetime.fromtimestamp(START + s, tz=timezone.utc).isoformat()
            book = {"bids": [["0.40", "5"], ["0.48", "5"]], "asks": [["0.52", "5"], ["0.60", "5"]]}
            rconn.execute("INSERT INTO pm_orderbook_snapshots (event_slug, outcome, token_id, poll_ts, book_json) VALUES (?, 'up', 'u', ?, ?)",
                          (slug, ts, json.dumps(book)))
        rconn.execute("INSERT INTO pm_settlements (event_slug, condition_id, result_up, end_time, finalized_poll_ts) VALUES (?, 'c', 1, 'x', 'x')", (slug,))
        rconn.commit()

        btc_conn = sqlite3.connect(":memory:")
        make_db(btc_conn, windows=0, first_start=START)
        rows = [(ts, "1", "1", "1", str(v[0]), "1", 1, "0.5") for ts, v in flat_btc().items()]
        btc_conn.executemany("INSERT OR REPLACE INTO btc_klines_1s VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        n, series = load_series_from_recorder(rconn, btc_conn)
        assert n == 1 and len(series) == 1
        (ws,) = series
        assert (ws.start, ws.end, ws.result_up) == (START, END, True)  # from the slug, not Gamma's startDate
        assert ws.pm[ws.start_idx + 11] == pytest.approx(0.50)

    def test_a_kalshi_or_history_database_is_not_a_recorder_database(self):
        conn = sqlite3.connect(":memory:")
        make_db(conn, windows=1)
        with pytest.raises(PmReactionError):
            load_series_from_recorder(conn, conn)


class TestCli:
    def test_end_to_end_writes_models_and_report(self, tmp_path, capsys):
        db = tmp_path / "pm-history-15m-x.sqlite"
        conn = sqlite3.connect(str(db))
        make_db(conn, windows=40, lag=3)
        conn.close()
        report = tmp_path / "r.json"
        code = main(["pm-reaction", "--db", str(db), "--out-dir", str(tmp_path / "m"), "--report", str(report)])
        out = capsys.readouterr().out
        assert code == 0 and "Data validity" in out and "Event study" in out
        assert (tmp_path / "m" / "pm_outcome.json").is_file() and (tmp_path / "m" / "pm_reaction.json").is_file()
        data = json.loads(report.read_text())
        assert data["validity"]["windows_usable"] == 40 and data["reaction"]["beats_control"] is True

    def test_not_a_history_database(self, tmp_path, capsys):
        db = tmp_path / "other.sqlite"
        sqlite3.connect(str(db)).close()
        assert main(["pm-reaction", "--db", str(db)]) == 1
        assert "not a download-polymarket-history database" in capsys.readouterr().err

    def test_bad_split(self, tmp_path):
        assert main(["pm-reaction", "--db", "x", "--split", "0.95"]) == 2
