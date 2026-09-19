import math
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from btcbot.model import (
    CalibrationError,
    EwmaVolatility,
    ModelState,
    Prediction,
    blend_with_market,
    brier_score,
    compute_calibration_report,
    ewma_sigma_from_prices,
    init_predictions_schema,
    load_calibration_pairs,
    log_prediction,
    log_return,
    normal_cdf,
    predict,
    predict_p_yes,
    reliability_table,
)
from btcbot.recorder import Recorder

T0 = datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc)


def state(**kwargs):
    defaults = dict(spot=Decimal("80000"), strike=Decimal("80000"), tau_sec=780.0, sigma=0.0005)
    defaults.update(kwargs)
    return ModelState(**defaults)


class TestNormalCdf:
    def test_zero_is_one_half(self):
        assert normal_cdf(0.0) == pytest.approx(0.5)

    def test_extremes(self):
        assert normal_cdf(math.inf) == pytest.approx(1.0)
        assert normal_cdf(-math.inf) == pytest.approx(0.0)

    @given(st.floats(min_value=-10, max_value=10))
    def test_symmetric(self, x):
        assert normal_cdf(-x) == pytest.approx(1.0 - normal_cdf(x))


class TestPredictPYes:
    def test_at_the_money_far_from_close_is_one_half(self):
        assert predict_p_yes(state(spot=Decimal("80000"), strike=Decimal("80000"))) == pytest.approx(0.5)

    def test_spot_above_strike_is_more_likely_yes_than_at_the_money(self):
        atm = predict_p_yes(state(spot=Decimal("80000"), strike=Decimal("80000")))
        above = predict_p_yes(state(spot=Decimal("80500"), strike=Decimal("80000")))
        assert above > atm

    def test_clamped_to_bounds_even_for_extreme_inputs(self):
        assert predict_p_yes(state(spot=Decimal("1000000"), strike=Decimal("1"))) == pytest.approx(0.98)
        assert predict_p_yes(state(spot=Decimal("1"), strike=Decimal("1000000"))) == pytest.approx(0.02)

    def test_continuous_across_the_60s_boundary(self):
        just_above = state(spot=Decimal("80050"), strike=Decimal("80000"), tau_sec=60.0 + 1e-6, sigma=0.001)
        at_boundary = state(
            spot=Decimal("80050"), strike=Decimal("80000"), tau_sec=60.0, sigma=0.001,
            observed_window_avg=Decimal("80050"),
        )
        assert predict_p_yes(just_above) == pytest.approx(predict_p_yes(at_boundary), abs=1e-4)

    def test_fully_observed_window_is_decided_by_the_observed_average(self):
        above = state(tau_sec=0.0, observed_window_avg=Decimal("80001"), strike=Decimal("80000"))
        below = state(tau_sec=0.0, observed_window_avg=Decimal("79999"), strike=Decimal("80000"))
        assert predict_p_yes(above) == pytest.approx(0.98)
        assert predict_p_yes(below) == pytest.approx(0.02)

    def test_observed_average_already_clearing_the_strike_is_certain_yes(self):
        # a high observed average with very little time left: even zero for the remainder would still clear
        s = state(
            spot=Decimal("1"), strike=Decimal("80000"), tau_sec=1.0, sigma=0.001,
            observed_window_avg=Decimal("500000"),
        )
        assert predict_p_yes(s) == pytest.approx(0.98)

    def test_zero_sigma_is_a_step_function_at_the_strike(self):
        assert predict_p_yes(state(spot=Decimal("80001"), sigma=0.0)) == pytest.approx(0.98)
        assert predict_p_yes(state(spot=Decimal("79999"), sigma=0.0)) == pytest.approx(0.02)
        assert predict_p_yes(state(spot=Decimal("80000"), strike=Decimal("80000"), sigma=0.0)) == pytest.approx(0.5)

    def test_monotonic_in_observed_average_within_the_closing_window(self):
        values = [
            predict_p_yes(state(tau_sec=20.0, strike=Decimal("80000"), observed_window_avg=Decimal(v)))
            for v in ("79000", "79500", "80000", "80500", "81000")
        ]
        assert values == sorted(values)

    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(spot=Decimal("0")),
            dict(spot=Decimal("-1")),
            dict(strike=Decimal("0")),
            dict(strike=Decimal("-1")),
            dict(sigma=-0.001),
        ],
    )
    def test_rejects_invalid_inputs(self, kwargs):
        with pytest.raises(ValueError):
            predict_p_yes(state(**kwargs))

    @given(
        spot=st.decimals(min_value="1", max_value="1000000", places=2, allow_nan=False),
        strike=st.decimals(min_value="1", max_value="1000000", places=2, allow_nan=False),
        tau_sec=st.floats(min_value=0.0, max_value=900.0, allow_nan=False),
        sigma=st.floats(min_value=0.0, max_value=0.05, allow_nan=False),
    )
    def test_always_within_clamp_bounds(self, spot, strike, tau_sec, sigma):
        p = predict_p_yes(ModelState(spot=spot, strike=strike, tau_sec=tau_sec, sigma=sigma))
        assert 0.02 <= p <= 0.98

    @given(
        strike=st.decimals(min_value="1000", max_value="200000", places=2, allow_nan=False),
        low_spot=st.decimals(min_value="1000", max_value="199999", places=2, allow_nan=False),
        bump=st.decimals(min_value="1", max_value="1000", places=2, allow_nan=False),
        tau_sec=st.floats(min_value=61.0, max_value=900.0, allow_nan=False),
        sigma=st.floats(min_value=0.0001, max_value=0.01, allow_nan=False),
    )
    def test_monotone_increasing_in_spot_over_strike(self, strike, low_spot, bump, tau_sec, sigma):
        high_spot = low_spot + bump
        low = predict_p_yes(ModelState(spot=low_spot, strike=strike, tau_sec=tau_sec, sigma=sigma))
        high = predict_p_yes(ModelState(spot=high_spot, strike=strike, tau_sec=tau_sec, sigma=sigma))
        assert high >= low


class TestBlendAndPredict:
    def test_no_market_mid_falls_back_to_model(self):
        assert blend_with_market(0.7, None, blend=0.3) == 0.7

    def test_blend_one_is_pure_model_zero_is_pure_market(self):
        assert blend_with_market(0.7, Decimal("0.4"), blend=1.0) == pytest.approx(0.7)
        assert blend_with_market(0.7, Decimal("0.4"), blend=0.0) == pytest.approx(0.4)

    def test_blend_is_the_weighted_average(self):
        assert blend_with_market(0.8, Decimal("0.2"), blend=0.25) == pytest.approx(0.25 * 0.8 + 0.75 * 0.2)

    @pytest.mark.parametrize("blend", [-0.1, 1.1])
    def test_rejects_blend_outside_unit_interval(self, blend):
        with pytest.raises(ValueError):
            blend_with_market(0.5, Decimal("0.5"), blend=blend)

    def test_predict_returns_model_and_blended(self):
        s = state(spot=Decimal("80500"), strike=Decimal("80000"), market_mid=Decimal("0.6"))
        p_model, p_blend = predict(s, blend=0.5)
        assert p_model == predict_p_yes(s)
        assert p_blend == pytest.approx(0.5 * p_model + 0.5 * 0.6)


class TestEwmaVolatility:
    def test_rejects_non_positive_span(self):
        with pytest.raises(ValueError):
            EwmaVolatility(0)

    def test_first_update_seeds_variance_from_that_return(self):
        v = EwmaVolatility(900)
        v.update(0.002)
        assert v.sigma == pytest.approx(0.002)

    def test_matches_manual_ewma_calculation(self):
        v = EwmaVolatility(9)  # alpha = 2/10 = 0.2, easy to check by hand
        returns = [0.01, -0.02, 0.005]
        v.update(returns[0])
        v.update(returns[1])
        v.update(returns[2])
        expected_var = returns[0] ** 2
        alpha = 2 / 10
        expected_var = alpha * returns[1] ** 2 + (1 - alpha) * expected_var
        expected_var = alpha * returns[2] ** 2 + (1 - alpha) * expected_var
        assert v.sigma == pytest.approx(math.sqrt(expected_var))

    def test_log_return_known_value(self):
        assert log_return(Decimal("100"), Decimal("200")) == pytest.approx(math.log(2))

    @pytest.mark.parametrize(("prev", "cur"), [(Decimal("0"), Decimal("1")), (Decimal("1"), Decimal("0")), (Decimal("-1"), Decimal("1"))])
    def test_log_return_rejects_non_positive_prices(self, prev, cur):
        with pytest.raises(ValueError):
            log_return(prev, cur)

    def test_ewma_sigma_from_prices_needs_at_least_two_prices(self):
        assert ewma_sigma_from_prices([], span_sec=900) == 0.0
        assert ewma_sigma_from_prices([Decimal("100")], span_sec=900) == 0.0

    def test_ewma_sigma_from_prices_matches_manual_updates(self):
        prices = [Decimal("100"), Decimal("101"), Decimal("99")]
        expected = EwmaVolatility(60)
        expected.update(log_return(prices[0], prices[1]))
        expected.update(log_return(prices[1], prices[2]))
        assert ewma_sigma_from_prices(prices, span_sec=60) == pytest.approx(expected.sigma)


class TestBrierScore:
    def test_empty_raises(self):
        with pytest.raises(CalibrationError):
            brier_score([])

    def test_perfect_predictions_score_zero(self):
        assert brier_score([(1.0, True), (0.0, False)]) == pytest.approx(0.0)

    def test_perfectly_wrong_predictions_score_one(self):
        assert brier_score([(0.0, True), (1.0, False)]) == pytest.approx(1.0)

    def test_known_value(self):
        # (0.9-1)^2 + (0.9-1)^2 + (0.1-0)^2 + (0.5-1)^2 = 0.01+0.01+0.01+0.25 = 0.28 / 4 = 0.07
        assert brier_score([(0.9, True), (0.9, True), (0.1, False), (0.5, True)]) == pytest.approx(0.07)

    @given(st.lists(st.tuples(st.floats(min_value=0, max_value=1), st.booleans()), min_size=1, max_size=50))
    def test_always_between_zero_and_one(self, pairs):
        assert 0.0 <= brier_score(pairs) <= 1.0


class TestReliabilityTable:
    def test_rejects_non_positive_bins(self):
        with pytest.raises(ValueError):
            reliability_table([(0.5, True)], bins=0)

    def test_top_edge_value_falls_in_the_last_bin(self):
        rows = reliability_table([(1.0, True)], bins=5)
        assert rows[-1].count == 1
        assert sum(r.count for r in rows[:-1]) == 0

    def test_empty_bins_report_none(self):
        rows = reliability_table([(0.05, True)], bins=2)
        assert rows[0].count == 1
        assert rows[1].count == 0
        assert rows[1].mean_predicted is None and rows[1].observed_rate is None

    def test_bin_stats_are_correct(self):
        rows = reliability_table([(0.1, True), (0.15, False)], bins=10)
        assert rows[1].count == 2
        assert rows[1].mean_predicted == pytest.approx(0.125)
        assert rows[1].observed_rate == pytest.approx(0.5)


def make_recorder_db(tmp_path):
    """A real recorder.py-schema database (predictions + settlements), for integration-style tests."""
    db_path = tmp_path / "cal.sqlite"
    Recorder(None, series_ticker="KXBTC15M", db_path=db_path).close()
    conn = sqlite3.connect(str(db_path))
    init_predictions_schema(conn)
    return conn


def insert_settlement(conn, ticker, result):
    conn.execute(
        """INSERT INTO settlements (ticker, event_ticker, result, settled_avg, strike, close_time, finalized_poll_ts)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ticker, ticker.rsplit("-", 1)[0], result, "80100", "80000", T0.isoformat(), T0.isoformat()),
    )
    conn.commit()


class TestPredictionLogging:
    def test_round_trips_through_a_real_recorder_database(self, tmp_path):
        conn = make_recorder_db(tmp_path)
        s = state(spot=Decimal("80500"), strike=Decimal("80000"), market_mid=Decimal("0.6"))
        p_model, p_blend = predict(s, blend=0.4)
        log_prediction(conn, Prediction("T-1", T0, s, 0.4, p_model, p_blend))

        row = conn.execute("SELECT ticker, spot, strike, p_model, p_blend, blend FROM predictions").fetchone()
        assert row == ("T-1", "80500", "80000", p_model, p_blend, 0.4)
        conn.close()


class TestCalibrationReport:
    def test_joins_predictions_to_settlements_and_scores_only_resolved_ones(self, tmp_path):
        conn = make_recorder_db(tmp_path)
        insert_settlement(conn, "T-1", "yes")
        insert_settlement(conn, "T-2", "no")
        # T-3 has a prediction but no settlement row at all: must be excluded, not treated as a loss
        for ticker, market_mid in (("T-1", Decimal("0.9")), ("T-2", Decimal("0.1")), ("T-3", Decimal("0.5"))):
            s = state(market_mid=market_mid)
            p_model, p_blend = predict(s)
            log_prediction(conn, Prediction(ticker, T0, s, 0.5, p_model, p_blend))

        pairs = load_calibration_pairs(conn, which="market")
        assert sorted(pairs) == sorted([(0.9, True), (0.1, False)])

        report = compute_calibration_report(conn)
        by_label = {r.label: r for r in report}
        assert by_label["market"].n == 2
        assert by_label["market"].brier_score == pytest.approx(brier_score(pairs))
        conn.close()

    def test_label_with_no_data_reports_n_zero_not_an_error(self, tmp_path):
        conn = make_recorder_db(tmp_path)
        insert_settlement(conn, "T-1", "yes")
        s = state(market_mid=None)  # no market mid ever logged
        p_model, p_blend = predict(s)
        log_prediction(conn, Prediction("T-1", T0, s, 0.5, p_model, p_blend))

        report = compute_calibration_report(conn)
        by_label = {r.label: r for r in report}
        assert by_label["market"].n == 0
        assert by_label["market"].brier_score is None
        assert by_label["model"].n == 1
        conn.close()

    def test_rejects_unknown_calibration_label(self, tmp_path):
        conn = make_recorder_db(tmp_path)
        with pytest.raises(ValueError):
            load_calibration_pairs(conn, which="nonsense")
        conn.close()
