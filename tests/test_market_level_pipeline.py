"""Offline tests for the coarser, market-level ML pipeline: synthetic candles/outcomes, no network."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from btcbot.coinbase_history import Candle
from btcbot.history_pipeline import MarketOutcome
from btcbot.market_level_pipeline import (
    MarketLevelError,
    adverse_exit_pnl,
    adverse_exit_price,
    market_level_examples,
    realized_vol_from_candles,
    split_markets_by_time,
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
