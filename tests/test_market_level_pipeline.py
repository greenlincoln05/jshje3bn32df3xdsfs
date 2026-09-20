"""Offline tests for the coarser, market-level ML pipeline: synthetic candles/outcomes, no network."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from btcbot.coinbase_history import Candle
from btcbot.history_pipeline import MarketOutcome
from btcbot.market_level_pipeline import (
    MIN_TRAIN_EXAMPLES,
    MIN_VALIDATE_EXAMPLES,
    MarketLevelError,
    adverse_exit_pnl,
    adverse_exit_price,
    market_level_examples,
    realized_vol_from_candles,
    split_markets_by_time,
    train_and_validate_market_level,
)
from btcbot.model import ModelState, predict_p_yes
from btcbot.paper_broker import taker_fee

T0 = datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc)


def candle(ts, close, *, low=None, high=None, open_=None, volume="10"):
    close = Decimal(close)
    return Candle(
        start=ts, low=Decimal(low) if low is not None else close, high=Decimal(high) if high is not None else close,
        open=Decimal(open_) if open_ is not None else close, close=close, volume=Decimal(volume),
    )


def outcome(ticker, close_time, result, *, strike="80000"):
    return MarketOutcome(
        ticker=ticker, event_ticker=ticker.rsplit("-", 1)[0], open_time=close_time - timedelta(minutes=15),
        close_time=close_time, strike=None if strike is None else Decimal(strike), result=result,
    )


class TestRealizedVol:
    def test_zero_for_fewer_than_two_candles(self):
        assert realized_vol_from_candles([]) == 0.0
        assert realized_vol_from_candles([candle(T0, "80000")]) == 0.0

    def test_zero_for_a_constant_price(self):
        candles = [candle(T0 + timedelta(minutes=i), "80000") for i in range(10)]
        assert realized_vol_from_candles(candles) == 0.0

    def test_positive_for_moving_prices(self):
        prices = ["80000", "80500", "79800", "80900", "80100"]
        candles = [candle(T0 + timedelta(minutes=i), p) for i, p in enumerate(prices)]
        assert realized_vol_from_candles(candles) > 0.0

    def test_only_uses_the_trailing_window(self):
        flat = [candle(T0 + timedelta(minutes=i), "80000") for i in range(20)]
        moving = [candle(T0 + timedelta(minutes=20 + i), p) for i, p in enumerate(["80000", "90000", "70000"])]
        assert realized_vol_from_candles(flat + moving, window=3) > 0.0
        assert realized_vol_from_candles(flat, window=3) == 0.0


class TestMarketLevelExamples:
    def test_one_example_per_priceable_market(self):
        candles = [candle(T0 + timedelta(minutes=i), "80000") for i in range(5)]
        outcomes = [outcome("T0", T0 + timedelta(minutes=3), "yes"), outcome("T1", T0 + timedelta(minutes=4), "no")]

        examples = market_level_examples(outcomes, candles)

        assert len(examples) == 2
        assert {label for _, label in examples} == {True, False}
        for features, _ in examples:
            assert set(features) == {"p_model", "sigma"}
            assert 0.0 <= features["p_model"] <= 1.0

    def test_skips_a_market_with_no_strike(self):
        candles = [candle(T0 + timedelta(minutes=i), "80000") for i in range(5)]
        outcomes = [outcome("T0", T0 + timedelta(minutes=3), "yes", strike=None)]

        assert market_level_examples(outcomes, candles) == []

    def test_skips_a_market_with_no_candle_history_yet(self):
        candles = [candle(T0 + timedelta(minutes=10), "80000")]  # only AFTER the market closes
        outcomes = [outcome("T0", T0, "yes")]

        assert market_level_examples(outcomes, candles) == []


class TestSplitMarketsByTime:
    def test_train_is_earlier_than_validate_with_a_market_embargoed(self):
        outcomes = [outcome(f"T{i}", T0 + timedelta(minutes=15 * i), "yes") for i in range(10)]
        train, validate, ordered = split_markets_by_time(outcomes, 0.7)
        assert len(ordered) == 10 and train.isdisjoint(validate)
        assert train == set(ordered[:7]) and validate == set(ordered[8:])

    def test_too_few_markets_explains_what_is_needed(self):
        outcomes = [outcome(f"T{i}", T0 + timedelta(minutes=15 * i), "yes") for i in range(3)]
        with pytest.raises(MarketLevelError, match="at least 6"):
            split_markets_by_time(outcomes, 0.7)

    def test_bad_train_fraction_is_rejected(self):
        outcomes = [outcome(f"T{i}", T0 + timedelta(minutes=15 * i), "yes") for i in range(10)]
        with pytest.raises(MarketLevelError):
            split_markets_by_time(outcomes, 0.95)


class TestAdverseExit:
    def test_yes_holder_is_priced_at_the_candle_low(self):
        state = ModelState(spot=Decimal(80000), strike=Decimal(80000), tau_sec=1.0, sigma=0.0004)
        c = candle(T0, "80000", low="79000", high="81000")

        price = adverse_exit_price(state, c, "yes")

        expected = predict_p_yes(ModelState(spot=c.low, strike=state.strike, tau_sec=state.tau_sec, sigma=state.sigma))
        assert price == Decimal(str(expected))

    def test_no_holder_is_priced_at_the_candle_high(self):
        state = ModelState(spot=Decimal(80000), strike=Decimal(80000), tau_sec=1.0, sigma=0.0004)
        c = candle(T0, "80000", low="79000", high="81000")

        price = adverse_exit_price(state, c, "no")

        expected_p_yes = predict_p_yes(ModelState(spot=c.high, strike=state.strike, tau_sec=state.tau_sec, sigma=state.sigma))
        assert price == Decimal(1) - Decimal(str(expected_p_yes))

    def test_invalid_side_is_rejected(self):
        state = ModelState(spot=Decimal(80000), strike=Decimal(80000), tau_sec=1.0, sigma=0.0004)
        with pytest.raises(MarketLevelError):
            adverse_exit_price(state, candle(T0, "80000"), "sideways")

    def test_adverse_exit_pnl_matches_the_manual_formula(self):
        state = ModelState(spot=Decimal(80000), strike=Decimal(80000), tau_sec=1.0, sigma=0.0004)
        c = candle(T0, "80000", low="70000", high="80000")  # a sharp drop against a YES holder
        entry_price = Decimal("0.60")
        size = Decimal(5)

        pnl = adverse_exit_pnl(entry_price, size, state, c, "yes")

        exit_price = adverse_exit_price(state, c, "yes")
        assert pnl == (exit_price - entry_price) * size - taker_fee(size, exit_price)
        assert pnl < 0  # the worst-case low is well below the strike: this exit is a clear loss


# ``predict_p_yes`` at the tau_sec=1.0 :func:`market_level_examples` always prices at is extremely sensitive
# (strike_eff amplifies any spot/strike gap ~59x -- see btcbot.model's module docstring): a raw spot/strike
# delta of even a few dollars saturates p_model to the 0.02/0.98 clamp, which is already "confident and
# correct" and leaves nothing for a trained model to visibly improve on. This dataset instead derives its
# parameters from the exact v1 formula (60*strike - 59*spot, then normal_cdf(log_ratio / sigma)) so that a
# tiny, sub-dollar final-candle delta (0.5) against a realistic realized-vol sigma (~0.0004, from a small
# +-0.155% oscillation in the preceding candles) lands p_model at a weakly-but-consistently-directional
# ~0.83/~0.17 -- not saturated, not noise -- which a trained LogisticModel can then sharpen toward 0/1.
def _market_level_dataset(n_markets):
    strike = Decimal("80000")
    oscillation = Decimal("0.00155")
    final_delta = Decimal("0.5")
    candles: list[Candle] = []
    outcomes: list[MarketOutcome] = []
    t = T0
    for i in range(n_markets):
        is_yes = i % 2 == 0
        fd = final_delta if is_yes else -final_delta
        for j in range(16):
            if j < 15:
                close = strike * (1 + oscillation) if j % 2 == 0 else strike * (1 - oscillation)
            else:
                close = strike + fd
            candles.append(candle(t, close))
            t += timedelta(minutes=1)
        close_time = t - timedelta(minutes=1)  # the start of this market's own last candle
        outcomes.append(outcome(f"T{i:03d}", close_time, "yes" if is_yes else "no", strike=str(strike)))
    return outcomes, candles


class TestTrainAndValidateMarketLevel:
    def test_a_weakly_separated_but_consistent_signal_beats_the_raw_v1_baseline(self):
        outcomes, candles = _market_level_dataset(80)

        model, report = train_and_validate_market_level(outcomes, candles)

        assert report.markets_train == 56 and report.markets_validate == 23  # 0.7 split, embargo of 1
        assert report.train_examples >= MIN_TRAIN_EXAMPLES
        assert report.validate_examples >= MIN_VALIDATE_EXAMPLES
        assert 0.0 <= report.train_brier <= 1.0 and 0.0 <= report.validate_brier <= 1.0
        assert report.validate_brier < report.baseline_validate_brier
        assert report.beats_baseline is True
        assert set(model.feature_names) == {"p_model", "sigma"}

    def test_too_few_markets_is_an_error(self):
        outcomes, candles = _market_level_dataset(4)

        with pytest.raises(MarketLevelError, match="at least 6"):
            train_and_validate_market_level(outcomes, candles)
