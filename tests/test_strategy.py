from decimal import Decimal

import pytest

from btcbot.models import OrderBook, PriceLevel
from btcbot.strategy import Action, decide, kelly_fraction, kelly_size, should_exit


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

    def test_a_rest_decision_reports_its_edge_and_kelly_fraction(self):
        d = decide_with(p_yes=0.9, book=book(yes=[("0.50", "20")], no=[("0.45", "20")]))
        assert d.action is Action.REST
        assert d.edge == pytest.approx(0.40)  # 0.9 - 0.50 - 0 fee
        assert d.kelly_fraction == pytest.approx(0.8)  # (0.9 - 0.5) / (1 - 0.5)

    def test_a_skip_decision_has_no_edge_or_kelly_fraction(self):
        d = decide_with(p_yes=0.5, book=book(yes=[("0.49", "20")], no=[("0.49", "20")]))
        assert d.action is Action.SKIP
        assert d.edge is None
        assert d.kelly_fraction is None


class TestPriceBand:
    def test_max_price_excludes_a_side_priced_above_it(self):
        # yes edge clears the bar (0.95 - 0.90 = 0.05 >= 0.04); the no side never qualifies either way.
        without_band = decide_with(p_yes=0.95, book=book(yes=[("0.90", "20")], no=[("0.05", "20")]))
        with_band = decide_with(
            p_yes=0.95, book=book(yes=[("0.90", "20")], no=[("0.05", "20")]), max_price=Decimal("0.85")
        )
        assert without_band.action is Action.REST and without_band.side == "yes"
        assert with_band.action is Action.SKIP

    def test_min_price_excludes_a_side_priced_below_it(self):
        # yes_bid + no_bid = 0.95 keeps both sides' spread within the default max_spread (0.06).
        # no edge clears the bar (0.95 - 0.10 = 0.85 >= 0.04); yes never qualifies either way (edge < 0).
        b = book(yes=[("0.85", "20")], no=[("0.10", "20")])
        without_band = decide_with(p_yes=0.05, book=b)
        with_band = decide_with(p_yes=0.05, book=b, min_price=Decimal("0.15"))
        assert without_band.action is Action.REST and without_band.side == "no"
        assert with_band.action is Action.SKIP

    def test_a_price_exactly_on_the_band_edge_is_allowed(self):
        # yes_bid + no_bid = 0.95 keeps both sides' spread within the default max_spread (0.06).
        d = decide_with(
            p_yes=0.95, book=book(yes=[("0.85", "20")], no=[("0.10", "20")]), max_price=Decimal("0.85")
        )
        assert d.action is Action.REST and d.price == Decimal("0.85")

    def test_a_qualifying_side_inside_the_band_still_trades(self):
        d = decide_with(
            p_yes=0.9, book=book(yes=[("0.50", "20")], no=[("0.45", "20")]),
            min_price=Decimal("0.15"), max_price=Decimal("0.85"),
        )
        assert d.action is Action.REST and d.side == "yes"


class TestShouldExit:
    def test_off_when_neither_threshold_is_set(self):
        assert should_exit(
            entry_price=Decimal("0.50"), current_bid=Decimal("0.10"), tau_sec=300, held_sec=300,
            stop_loss_pct=None, take_profit_pct=None,
        ) is None

    def test_stop_loss_triggers_when_the_mark_falls_far_enough(self):
        assert should_exit(  # entry 0.50, bid 0.40: down 20%
            entry_price=Decimal("0.50"), current_bid=Decimal("0.40"), tau_sec=300, held_sec=300,
            stop_loss_pct=Decimal("20"), take_profit_pct=None,
        ) == "stop_loss"

    def test_stop_loss_does_not_trigger_just_short_of_the_threshold(self):
        assert should_exit(  # down 19%, threshold 20%
            entry_price=Decimal("0.50"), current_bid=Decimal("0.405"), tau_sec=300, held_sec=300,
            stop_loss_pct=Decimal("20"), take_profit_pct=None,
        ) is None

    def test_take_profit_triggers_when_the_mark_rises_far_enough(self):
        assert should_exit(  # entry 0.50, bid 0.65: up 30%
            entry_price=Decimal("0.50"), current_bid=Decimal("0.65"), tau_sec=300, held_sec=300,
            stop_loss_pct=None, take_profit_pct=Decimal("20"),
        ) == "take_profit"

    def test_no_signal_inside_the_neutral_band(self):
        assert should_exit(
            entry_price=Decimal("0.50"), current_bid=Decimal("0.48"), tau_sec=300, held_sec=300,
            stop_loss_pct=Decimal("20"), take_profit_pct=Decimal("20"),
        ) is None

    def test_no_depth_on_the_held_side_means_no_signal(self):
        # nothing to sell into: no exit fill is possible, so there is nothing to do but keep holding.
        assert should_exit(
            entry_price=Decimal("0.50"), current_bid=None, tau_sec=300, held_sec=300,
            stop_loss_pct=Decimal("1"), take_profit_pct=None,
        ) is None

    def test_a_degenerate_entry_price_is_refused(self):
        assert should_exit(
            entry_price=Decimal("0"), current_bid=Decimal("0.10"), tau_sec=300, held_sec=300,
            stop_loss_pct=Decimal("1"), take_profit_pct=None,
        ) is None

    def test_minimum_hold_blocks_an_early_exit(self):
        too_soon = should_exit(
            entry_price=Decimal("0.50"), current_bid=Decimal("0.30"), tau_sec=300, held_sec=5,
            stop_loss_pct=Decimal("20"), take_profit_pct=None, stop_min_hold_sec=30,
        )
        long_enough = should_exit(
            entry_price=Decimal("0.50"), current_bid=Decimal("0.30"), tau_sec=300, held_sec=30,
            stop_loss_pct=Decimal("20"), take_profit_pct=None, stop_min_hold_sec=30,
        )
        assert too_soon is None and long_enough == "stop_loss"

    def test_minimum_tau_holds_to_settlement_near_close(self):
        too_close = should_exit(
            entry_price=Decimal("0.50"), current_bid=Decimal("0.30"), tau_sec=15, held_sec=300,
            stop_loss_pct=Decimal("20"), take_profit_pct=None, stop_min_tau_sec=30,
        )
        still_time = should_exit(
            entry_price=Decimal("0.50"), current_bid=Decimal("0.30"), tau_sec=30, held_sec=300,
            stop_loss_pct=Decimal("20"), take_profit_pct=None, stop_min_tau_sec=30,
        )
        assert too_close is None and still_time == "stop_loss"


class TestExit:
    def test_no_exit_signal_holds_as_before(self):
        d = decide_with(
            has_position=True, book=book(yes=[("0.50", "20")], no=[("0.45", "20")]),
            position_side="yes", position_entry_price=Decimal("0.50"), position_held_sec=300,
        )
        assert d.action is Action.HOLD

    def test_exits_when_the_stop_triggers(self):
        d = decide_with(
            has_position=True, book=book(yes=[("0.35", "20")], no=[("0.60", "20")]),
            position_side="yes", position_entry_price=Decimal("0.50"), position_held_sec=300,
            stop_loss_pct=Decimal("20"),
        )
        assert d.action is Action.EXIT
        assert d.side == "yes"
        assert d.price == Decimal("0.35")
        assert d.exit_reason == "stop_loss"

    def test_without_position_details_never_exits_even_with_a_stop_configured(self):
        # decide() cannot check a stop it has no entry price/side for; passing none of the new kwargs
        # keeps a caller's behavior identical to before this feature existed.
        d = decide_with(has_position=True, stop_loss_pct=Decimal("0.01"))
        assert d.action is Action.HOLD

    def test_a_resting_order_still_takes_priority_over_an_exit_check(self):
        d = decide_with(
            has_resting_order=True, has_position=True, tau_sec=300,
            book=book(yes=[("0.10", "20")], no=[("0.85", "20")]),
            position_side="yes", position_entry_price=Decimal("0.50"), position_held_sec=300,
            stop_loss_pct=Decimal("1"),
        )
        assert d.action is Action.HOLD  # today's guard ordering is unchanged: a resting order is handled first

    def test_no_depth_on_the_held_side_falls_back_to_holding(self):
        d = decide_with(
            has_position=True, book=book(yes=[], no=[("0.60", "20")]),
            position_side="yes", position_entry_price=Decimal("0.50"), position_held_sec=300,
            stop_loss_pct=Decimal("1"),
        )
        assert d.action is Action.HOLD


class TestKellyFraction:
    def test_zero_at_a_fair_price(self):
        assert kelly_fraction(0.5, Decimal("0.5")) == pytest.approx(0.0)

    def test_matches_the_textbook_formula(self):
        assert kelly_fraction(0.94, Decimal("0.90")) == pytest.approx(0.4)
        assert kelly_fraction(0.14, Decimal("0.10")) == pytest.approx(0.04 / 0.9)

    def test_the_same_raw_edge_is_a_larger_fraction_at_an_extreme_price(self):
        # This is precisely why full Kelly must never be staked directly near a price of 0 or 1.
        low = kelly_fraction(0.54, Decimal("0.50"))
        high = kelly_fraction(0.94, Decimal("0.90"))
        assert high > low

    def test_negative_edge_clamps_to_zero_not_negative(self):
        assert kelly_fraction(0.4, Decimal("0.5")) == 0.0

    def test_a_price_of_one_is_defined_as_zero(self):
        assert kelly_fraction(0.99, Decimal("1")) == 0.0


class TestKellySize:
    def test_matches_hand_computed_stake(self):
        # full kelly 0.40 * multiplier 0.2 = 0.08 of a $25 bankroll = $2.00 / $0.90 -> floor to 2 contracts
        size = kelly_size(0.40, Decimal("0.90"), bankroll_usd=Decimal("25"), multiplier=0.2, max_contracts=Decimal(10))
        assert size == Decimal(2)

    def test_floors_up_to_one_contract_rather_than_zero(self):
        size = kelly_size(0.01, Decimal("0.50"), bankroll_usd=Decimal("25"), multiplier=0.2, max_contracts=Decimal(10))
        assert size == Decimal(1)

    def test_never_exceeds_max_contracts(self):
        size = kelly_size(1.0, Decimal("0.10"), bankroll_usd=Decimal("100"), multiplier=1.0, max_contracts=Decimal(10))
        assert size == Decimal(10)
