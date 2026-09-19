from decimal import Decimal

import pytest

from btcbot.models import OrderBook, PriceLevel
from btcbot.strategy import Action, decide


def book(yes=(), no=()):
    return OrderBook(
        "T",
        yes_bids=tuple(PriceLevel(Decimal(p), Decimal(s)) for p, s in yes),
        no_bids=tuple(PriceLevel(Decimal(p), Decimal(s)) for p, s in no),
    )


DEFAULTS = dict(
    min_edge=Decimal("0.04"),
    min_depth=Decimal("10"),
    max_spread=Decimal("0.06"),
    min_tau_sec=30,
    max_tau_sec=780,
    cancel_before_close_sec=20,
    contracts_per_trade=Decimal("5"),
    maker_fee_multiplier=Decimal("0"),
    has_resting_order=False,
    has_position=False,
)


def decide_with(**overrides):
    kwargs = {**DEFAULTS, **overrides}
    kwargs.setdefault("book", book(yes=[("0.50", "20")], no=[("0.45", "20")]))
    kwargs.setdefault("tau_sec", 300)
    kwargs.setdefault("spot_is_stale", False)
    kwargs.setdefault("p_yes", 0.5)
    return decide(**kwargs)


class TestGuards:
    def test_holds_when_already_positioned(self):
        d = decide_with(has_position=True, p_yes=0.9)
        assert d.action is Action.HOLD

    def test_holds_a_resting_order_while_time_remains(self):
        d = decide_with(has_resting_order=True, tau_sec=300, cancel_before_close_sec=20)
        assert d.action is Action.HOLD

    def test_cancels_a_resting_order_near_the_close(self):
        d = decide_with(has_resting_order=True, tau_sec=15, cancel_before_close_sec=20)
        assert d.action is Action.CANCEL

    def test_cancels_at_exactly_the_threshold(self):
        d = decide_with(has_resting_order=True, tau_sec=20, cancel_before_close_sec=20)
        assert d.action is Action.CANCEL

    def test_skips_on_a_stale_feed(self):
        d = decide_with(spot_is_stale=True, p_yes=0.9)
        assert d.action is Action.SKIP and "stale" in d.reason

    def test_skips_when_tau_is_below_the_minimum(self):
        d = decide_with(tau_sec=10, min_tau_sec=30, p_yes=0.9)
        assert d.action is Action.SKIP

    def test_skips_when_tau_is_above_the_maximum(self):
        d = decide_with(tau_sec=800, max_tau_sec=780, p_yes=0.9)
        assert d.action is Action.SKIP

    def test_position_and_resting_order_checks_short_circuit_before_tau_bounds(self):
        # has_position is checked first regardless of how bad everything else looks
        d = decide_with(has_position=True, tau_sec=-100, spot_is_stale=True)
        assert d.action is Action.HOLD


class TestEntry:
    def test_rests_at_the_best_bid_when_edge_clears_the_bar(self):
        d = decide_with(p_yes=0.9, book=book(yes=[("0.50", "20")], no=[("0.45", "20")]))
        assert d.action is Action.REST
        assert d.side == "yes"
        assert d.price == Decimal("0.50")
        assert d.size == Decimal("5")

    def test_skips_when_no_side_clears_min_edge(self):
        # both sides priced right at fair value for p_yes=0.5: neither side has real edge
        d = decide_with(p_yes=0.5, book=book(yes=[("0.49", "20")], no=[("0.49", "20")]))
        assert d.action is Action.SKIP

    def test_skips_on_a_wide_spread(self):
        # yes ask = 1 - 0.20 = 0.80; spread = 0.80 - 0.50 = 0.30
        d = decide_with(p_yes=0.9, max_spread=Decimal("0.06"), book=book(yes=[("0.50", "20")], no=[("0.20", "20")]))
        assert d.action is Action.SKIP

    def test_skips_on_a_thin_book(self):
        d = decide_with(p_yes=0.9, min_depth=Decimal("50"), book=book(yes=[("0.50", "20")], no=[("0.45", "20")]))
        assert d.action is Action.SKIP

    def test_skips_when_a_side_has_no_bid_at_all(self):
        d = decide_with(p_yes=0.9, book=book(yes=[], no=[("0.45", "20")]))
        assert d.action is Action.SKIP or (d.action is Action.REST and d.side == "no")

    def test_picks_the_higher_edge_when_both_sides_qualify(self):
        # yes edge ~ 0.65-0.30=0.35 (large); no probability=0.35, no edge ~ 0.35-0.30=0.05 (small but qualifies)
        d = decide_with(
            p_yes=0.65, max_spread=Decimal("0.5"), book=book(yes=[("0.30", "20")], no=[("0.30", "20")])
        )
        assert d.action is Action.REST and d.side == "yes"

    def test_takes_the_no_side_when_it_has_the_better_edge(self):
        # p_yes=0.1: no probability=0.9, no bid=0.50 -> no edge ~0.40; yes edge ~0.1-0.50 (negative)
        d = decide_with(p_yes=0.1, book=book(yes=[("0.50", "20")], no=[("0.50", "20")]))
        assert d.action is Action.REST and d.side == "no"

    def test_proposed_size_is_the_configured_contracts_per_trade(self):
        d = decide_with(p_yes=0.9, contracts_per_trade=Decimal("7"))
        assert d.size == Decimal("7")

    def test_higher_maker_fee_multiplier_can_turn_a_marginal_edge_into_a_skip(self):
        free = decide_with(p_yes=0.6, min_edge=Decimal("0.09"), maker_fee_multiplier=Decimal("0"))
        taxed = decide_with(p_yes=0.6, min_edge=Decimal("0.09"), maker_fee_multiplier=Decimal("1"))
        assert free.action is Action.REST
        assert taxed.action is Action.SKIP
