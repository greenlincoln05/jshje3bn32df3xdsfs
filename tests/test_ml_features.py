"""Offline tests for the ML entry/exit feature vectors: pure functions, no recorded data, no network."""

from decimal import Decimal

from btcbot.ml_features import ENTRY_FEATURES, EXIT_FEATURES, entry_features, exit_features


class TestEntryFeatures:
    def test_matches_the_fixed_feature_order(self):
        features = entry_features(
            p_side=0.6, price=Decimal("0.30"), tau_sec=300.0, spread=Decimal("0.02"),
            depth=Decimal(10), sigma=0.0004, momentum_60s=5.0,
        )
        assert set(features) == set(ENTRY_FEATURES)

    def test_edge_is_p_side_minus_price(self):
        features = entry_features(
            p_side=0.6, price=Decimal("0.30"), tau_sec=300.0, spread=Decimal("0.02"),
            depth=Decimal(10), sigma=0.0004, momentum_60s=None,
        )
        assert features["edge"] == 0.6 - 0.30
        assert features["price"] == 0.30

    def test_missing_momentum_defaults_to_zero_not_none(self):
        features = entry_features(
            p_side=0.6, price=Decimal("0.30"), tau_sec=300.0, spread=Decimal("0.02"),
            depth=Decimal(10), sigma=0.0004, momentum_60s=None,
        )
        assert features["momentum_60s"] == 0.0


class TestExitFeatures:
    def test_matches_the_fixed_feature_order(self):
        features = exit_features(
            entry_price=Decimal("0.30"), current_bid=Decimal("0.25"), held_sec=60.0, tau_sec=200.0,
            sigma=0.0004, momentum_60s=-3.0,
        )
        assert set(features) == set(EXIT_FEATURES)

    def test_unrealized_pct_matches_should_exit_s_own_formula(self):
        # Same shape as btcbot.strategy.should_exit's change_pct: (current - entry) / entry * 100.
        features = exit_features(
            entry_price=Decimal("0.30"), current_bid=Decimal("0.24"), held_sec=60.0, tau_sec=200.0,
            sigma=0.0004, momentum_60s=None,
        )
        assert features["unrealized_pct"] == (0.24 - 0.30) / 0.30 * 100

    def test_zero_entry_price_does_not_divide_by_zero(self):
        features = exit_features(
            entry_price=Decimal(0), current_bid=Decimal("0.24"), held_sec=60.0, tau_sec=200.0,
            sigma=0.0004, momentum_60s=None,
        )
        assert features["unrealized_pct"] == 0.0
