"""Offline unit tests for trend_backtest.py. No network access."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from trend_backtest import (
    WINDOW_MIN,
    ewma_sigma,
    fee,
    model_price,
    simulate_window,
    trend_signal,
)


def test_fee_matches_spec_worked_example():
    # docs/btc15m-bot-spec.md: 0.07 * 1 * 0.055 * 0.945 == the spec's worked example.
    assert math.isclose(fee(1, 0.055), 0.07 * 0.055 * 0.945, rel_tol=1e-9)


def test_model_price_is_half_at_the_money():
    assert math.isclose(model_price(spot=100.0, strike=100.0, tau_min=5.0, sigma_per_min=0.001), 0.5, abs_tol=1e-9)


def test_model_price_favors_side_above_strike():
    p = model_price(spot=101.0, strike=100.0, tau_min=5.0, sigma_per_min=0.001)
    assert p > 0.5


def test_model_price_is_clamped():
    p_high = model_price(spot=1000.0, strike=100.0, tau_min=5.0, sigma_per_min=0.001)
    p_low = model_price(spot=1.0, strike=100.0, tau_min=5.0, sigma_per_min=0.001)
    assert p_high == 0.98
    assert p_low == 0.02


def test_ewma_sigma_zero_for_flat_prices():
    assert ewma_sigma([100.0] * 20, span=15) == 0.0


def test_ewma_sigma_positive_for_moving_prices():
    prices = [100.0 * (1.001 ** i) for i in range(20)]
    assert ewma_sigma(prices, span=15) > 0.0


def _make_uptrend_series(hours: float, start: datetime, start_price: float, drift_per_min: float):
    minutes = int(hours * 60) + WINDOW_MIN + 1
    return {
        start + timedelta(minutes=i): start_price * (1 + drift_per_min) ** i
        for i in range(minutes)
    }


def test_trend_signal_all_up():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    closes = _make_uptrend_series(hours=24, start=start, start_price=50_000.0, drift_per_min=0.0003)
    t_enter = start + timedelta(hours=24, minutes=6)
    assert trend_signal(closes, t_enter) == "yes"


def test_trend_signal_none_when_lookback_missing():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    closes = {start: 100.0, start + timedelta(minutes=6): 101.0}
    assert trend_signal(closes, start + timedelta(minutes=6)) is None


def test_simulate_window_hold_wins_when_trend_confirms():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    closes = _make_uptrend_series(hours=24, start=start, start_price=50_000.0, drift_per_min=0.0003)
    window_open = start + timedelta(hours=24)

    result = simulate_window(
        closes, window_open,
        entry_minute=6, entry_price=0.60,
        flip_threshold=0.20, min_minutes_to_flip=2.0,
        vol_span=15, contracts=1.0,
    )

    assert result is not None
    assert result.side == "yes"
    assert result.settled_yes is True
    # Won at $0.60 entry: payout 1.0 minus entry minus a small fee.
    assert result.hold_pnl > 0.35


def test_simulate_window_none_without_strike_or_settlement():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    closes = {start: 100.0}  # no data at window close
    result = simulate_window(
        closes, start,
        entry_minute=6, entry_price=0.60,
        flip_threshold=0.20, min_minutes_to_flip=2.0,
        vol_span=15, contracts=1.0,
    )
    assert result is None
