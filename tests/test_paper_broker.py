from datetime import datetime, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from btcbot.models import OrderBook, PriceLevel
from btcbot.paper_broker import (
    PaperBroker,
    PaperBrokerError,
    QueueAssumption,
    is_on_tick_grid,
    maker_fee,
    raw_fee,
    settle,
    taker_fee,
    tick_size_for,
)

T0 = datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc)


def book(yes=(), no=()):
    return OrderBook(
        "T",
        yes_bids=tuple(PriceLevel(Decimal(p), Decimal(s)) for p, s in yes),
        no_bids=tuple(PriceLevel(Decimal(p), Decimal(s)) for p, s in no),
    )


class TestFees:
    def test_raw_fee_matches_kalshis_worked_example(self):
        # docs.kalshi.com's Fee Rounding worked example: one contract at $0.055 -> model fee $0.00363825
        assert raw_fee(Decimal(1), Decimal("0.055")) == Decimal("0.00363825")

    def test_raw_fee_is_symmetric_in_price_and_one_minus_price(self):
        assert raw_fee(Decimal(1), Decimal("0.3")) == raw_fee(Decimal(1), Decimal("0.7"))

    def test_raw_fee_is_zero_at_the_edges(self):
        assert raw_fee(Decimal(5), Decimal("0")) == 0
        assert raw_fee(Decimal(5), Decimal("1")) == 0

    @pytest.mark.parametrize("contracts", [Decimal("-1")])
    def test_raw_fee_rejects_negative_contracts(self, contracts):
        with pytest.raises(ValueError):
            raw_fee(contracts, Decimal("0.5"))

    @pytest.mark.parametrize("price", [Decimal("-0.01"), Decimal("1.01")])
    def test_raw_fee_rejects_price_outside_unit_interval(self, price):
        with pytest.raises(ValueError):
            raw_fee(Decimal(1), price)

    def test_round_fee_up_rounds_up_not_to_nearest(self):
        # 0.00363825 has more than 6 decimal places; rounding up to $0.000001 must not round down
        assert taker_fee(Decimal(1), Decimal("0.055")) == Decimal("0.003639")

    def test_round_fee_up_leaves_an_exact_value_alone(self):
        from btcbot.paper_broker import round_fee_up

        assert round_fee_up(Decimal("0.000005")) == Decimal("0.000005")

    def test_zero_fee_rounds_to_zero(self):
        assert taker_fee(Decimal(5), Decimal("0")) == Decimal("0.000000")

    def test_maker_fee_multiplier_zero_means_free(self):
        assert maker_fee(Decimal(10), Decimal("0.5"), multiplier=Decimal("0")) == Decimal("0.000000")

    def test_maker_fee_scales_with_multiplier(self):
        quarter = maker_fee(Decimal(1), Decimal("0.5"), multiplier=Decimal("0.25"))
        full_raw = raw_fee(Decimal(1), Decimal("0.5"))
        assert quarter == full_raw * Decimal("0.25")

    def test_maker_fee_rejects_negative_multiplier(self):
        with pytest.raises(ValueError):
            maker_fee(Decimal(1), Decimal("0.5"), multiplier=Decimal("-0.1"))

    @given(
        contracts=st.decimals(min_value="0", max_value="1000", places=2, allow_nan=False),
        price=st.decimals(min_value="0", max_value="1", places=4, allow_nan=False),
    )
    def test_taker_fee_is_never_negative(self, contracts, price):
        assert taker_fee(contracts, price) >= 0

    @given(
        contracts=st.decimals(min_value="0.01", max_value="1000", places=2, allow_nan=False),
        price=st.decimals(min_value="0.0001", max_value="0.9999", places=4, allow_nan=False),
    )
    def test_taker_fee_rounds_up_never_down(self, contracts, price):
        assert taker_fee(contracts, price) >= raw_fee(contracts, price)


class TestTickGrid:
    @pytest.mark.parametrize(("price", "tick"), [("0.001", "0.001"), ("0.099", "0.001"), ("0.901", "0.001")])
    def test_fine_tick_below_010_and_above_090(self, price, tick):
        assert tick_size_for(Decimal(price)) == Decimal(tick)

    @pytest.mark.parametrize("price", ["0.10", "0.50", "0.90"])
    def test_coarse_tick_between_010_and_090_inclusive(self, price):
        assert tick_size_for(Decimal(price)) == Decimal("0.01")

    @pytest.mark.parametrize("price", ["0.001", "0.054", "0.10", "0.54", "0.90", "0.999"])
    def test_valid_grid_prices_are_on_grid(self, price):
        assert is_on_tick_grid(Decimal(price))

    @pytest.mark.parametrize("price", ["0.0015", "0.545", "0", "1", "1.5", "-0.01"])
    def test_invalid_prices_are_off_grid(self, price):
        assert not is_on_tick_grid(Decimal(price))


class TestSettle:
    def test_pays_a_dollar_per_contract_on_a_win(self):
        assert settle("yes", Decimal(7), "yes") == 7

    def test_pays_nothing_on_a_loss(self):
        assert settle("yes", Decimal(7), "no") == 0


class TestRestingOrders:
    def test_rejects_non_positive_size(self):
        b = PaperBroker()
        with pytest.raises(PaperBrokerError):
            b.place_resting_order("yes", Decimal("0.5"), Decimal(0), ts=T0, book=book())

    def test_rejects_off_grid_price(self):
        b = PaperBroker()
        with pytest.raises(PaperBrokerError):
            b.place_resting_order("yes", Decimal("0.545"), Decimal(1), ts=T0, book=book())

    def test_unknown_order_id_raises(self):
        b = PaperBroker()
        with pytest.raises(PaperBrokerError):
            b.get_order("does-not-exist")

    def test_joining_an_existing_level_sets_queue_ahead_to_its_depth(self):
        b = PaperBroker()
        oid = b.place_resting_order("yes", Decimal("0.54"), Decimal(5), ts=T0, book=book(yes=[("0.54", "20")]))
        assert b.get_order(oid).queue_ahead == 20

    def test_joining_an_empty_price_level_starts_at_the_front(self):
        b = PaperBroker()
        oid = b.place_resting_order("yes", Decimal("0.54"), Decimal(5), ts=T0, book=book())
        assert b.get_order(oid).queue_ahead == 0

    def test_partial_shrinkage_advances_the_queue_without_filling(self):
        b = PaperBroker()
        oid = b.place_resting_order("yes", Decimal("0.54"), Decimal(5), ts=T0, book=book(yes=[("0.54", "10")]))
        fills = b.on_book_update(book(yes=[("0.54", "3")]), ts=T0)
        assert fills == []
        assert b.get_order(oid).queue_ahead == 3
        assert b.get_order(oid).status == "open"

    def test_shrinkage_reaching_exactly_the_front_does_not_fill(self):
        b = PaperBroker()
        oid = b.place_resting_order("yes", Decimal("0.54"), Decimal(5), ts=T0, book=book(yes=[("0.54", "10")]))
        b.on_book_update(book(yes=[("0.54", "0")]), ts=T0)
        assert b.get_order(oid).queue_ahead == 0
        assert b.get_order(oid).status == "open"

    def test_shrinkage_past_the_front_fills_in_optimistic_mode(self):
        # The very first update after placement can only ever bring the queue to the front (shrinkage is
        # bounded by what was observed at placement); a fill needs a *second* round of trading once fresh
        # depth has appeared and is then eaten too -- exactly what "past the front" means here.
        b = PaperBroker(queue_assumption=QueueAssumption.OPTIMISTIC)
        oid = b.place_resting_order("yes", Decimal("0.54"), Decimal(5), ts=T0, book=book(yes=[("0.54", "3")]))
        assert b.on_book_update(book(yes=[("0.54", "0")]), ts=T0) == []  # reaches the front, no fill yet
        fills = b.on_book_update(book(yes=[("0.54", "3")]), ts=T0)  # more depth arrives...
        assert fills == []
        fills = b.on_book_update(book(yes=[("0.54", "0")]), ts=T0)  # ...and trades through us
        assert [f.size for f in fills] == [3]
        assert fills[0].maker is True and fills[0].side == "yes" and fills[0].price == Decimal("0.54")
        order = b.get_order(oid)
        assert order.status == "partially_filled" and order.remaining_size == 2

    def test_full_fill_marks_the_order_filled(self):
        b = PaperBroker(queue_assumption=QueueAssumption.OPTIMISTIC)
        oid = b.place_resting_order("yes", Decimal("0.54"), Decimal(3), ts=T0, book=book(yes=[("0.54", "3")]))
        b.on_book_update(book(yes=[("0.54", "0")]), ts=T0)
        b.on_book_update(book(yes=[("0.54", "3")]), ts=T0)
        fills = b.on_book_update(book(yes=[("0.54", "0")]), ts=T0)
        assert sum(f.size for f in fills) == 3
        assert b.get_order(oid).status == "filled"
        assert b.get_order(oid).remaining_size == 0

    def test_a_fill_never_exceeds_the_orders_remaining_size(self):
        b = PaperBroker(queue_assumption=QueueAssumption.OPTIMISTIC)
        oid = b.place_resting_order("yes", Decimal("0.54"), Decimal(2), ts=T0, book=book(yes=[("0.54", "1")]))
        b.on_book_update(book(yes=[("0.54", "0")]), ts=T0)  # reaches the front, no fill
        b.on_book_update(book(yes=[("0.54", "10")]), ts=T0)  # a lot of fresh depth arrives
        fills = b.on_book_update(book(yes=[]), ts=T0)  # ...and all 10 trade through: excess is 10, order size is 2
        assert sum(f.size for f in fills) == 2
        assert b.get_order(oid).status == "filled"

    def test_no_shrinkage_from_an_empty_level_produces_no_fill(self):
        b = PaperBroker(queue_assumption=QueueAssumption.OPTIMISTIC)
        b.place_resting_order("yes", Decimal("0.54"), Decimal(2), ts=T0, book=book(yes=[("0.54", "0")]))
        fills = b.on_book_update(book(yes=[]), ts=T0)
        assert fills == []

    def test_pessimistic_mode_never_fills_without_a_trade_tape(self):
        b = PaperBroker(queue_assumption=QueueAssumption.PESSIMISTIC)
        oid = b.place_resting_order("yes", Decimal("0.54"), Decimal(5), ts=T0, book=book(yes=[("0.54", "3")]))
        fills = b.on_book_update(book(yes=[("0.54", "0")]), ts=T0)
        assert fills == []
        assert b.get_order(oid).queue_ahead == 0
        assert b.get_order(oid).status == "open"

    def test_new_depth_joining_behind_us_does_not_advance_the_queue(self):
        b = PaperBroker()
        oid = b.place_resting_order("yes", Decimal("0.54"), Decimal(5), ts=T0, book=book(yes=[("0.54", "5")]))
        b.on_book_update(book(yes=[("0.54", "9")]), ts=T0)  # size grew: someone else joined behind us
        assert b.get_order(oid).queue_ahead == 5

    def test_done_orders_are_not_touched_by_further_updates(self):
        b = PaperBroker(queue_assumption=QueueAssumption.OPTIMISTIC)
        oid = b.place_resting_order("yes", Decimal("0.54"), Decimal(1), ts=T0, book=book(yes=[("0.54", "1")]))
        b.cancel(oid, ts=T0)
        fills = b.on_book_update(book(yes=[]), ts=T0)
        assert fills == []
        assert b.get_order(oid).status == "cancelled"

    def test_cancel_is_idempotent(self):
        b = PaperBroker()
        oid = b.place_resting_order("yes", Decimal("0.54"), Decimal(1), ts=T0, book=book(yes=[("0.54", "1")]))
        b.cancel(oid, ts=T0)
        b.cancel(oid, ts=T0)  # must not raise or un-cancel
        assert b.get_order(oid).status == "cancelled"

    def test_open_orders_excludes_done_ones(self):
        b = PaperBroker()
        open_id = b.place_resting_order("yes", Decimal("0.54"), Decimal(1), ts=T0, book=book())
        cancelled_id = b.place_resting_order("no", Decimal("0.40"), Decimal(1), ts=T0, book=book())
        b.cancel(cancelled_id, ts=T0)
        assert [o.order_id for o in b.open_orders()] == [open_id]


class TestTakerOrders:
    def test_rejects_non_positive_size(self):
        b = PaperBroker()
        with pytest.raises(PaperBrokerError):
            b.place_taker_order("yes", Decimal(0), book=book(), ts=T0)

    def test_fills_from_a_single_level(self):
        b = PaperBroker()
        # a YES taker crosses the NO bids (yes ask = 1 - no bid)
        fills = b.place_taker_order("yes", Decimal(3), book=book(no=[("0.40", "10")]), ts=T0)
        assert len(fills) == 1
        assert fills[0].price == Decimal("0.60") and fills[0].size == 3 and fills[0].maker is False

    def test_walks_multiple_levels_best_price_first(self):
        b = PaperBroker()
        # no bids at 0.30 (ask 0.70) and 0.40 (ask 0.60); best (lowest) ask -- 0.60, from the higher no bid -- first
        fills = b.place_taker_order("yes", Decimal(6), book=book(no=[("0.30", "3"), ("0.40", "5")]), ts=T0)
        assert [(f.price, f.size) for f in fills] == [(Decimal("0.60"), Decimal(5)), (Decimal("0.70"), Decimal(1))]

    def test_stops_at_worst_price(self):
        b = PaperBroker()
        fills = b.place_taker_order(
            "yes", Decimal(10), book=book(no=[("0.30", "3"), ("0.40", "5")]), ts=T0, worst_price=Decimal("0.60")
        )
        assert [(f.price, f.size) for f in fills] == [(Decimal("0.60"), Decimal(5))]

    def test_partial_fill_when_the_book_runs_out(self):
        b = PaperBroker()
        fills = b.place_taker_order("yes", Decimal(100), book=book(no=[("0.40", "5")]), ts=T0)
        assert sum(f.size for f in fills) == 5

    def test_empty_opposite_side_fills_nothing(self):
        b = PaperBroker()
        assert b.place_taker_order("yes", Decimal(5), book=book(), ts=T0) == []

    def test_taker_fill_never_exceeds_requested_size(self):
        b = PaperBroker()
        fills = b.place_taker_order("no", Decimal(4), book=book(yes=[("0.90", "100")]), ts=T0)
        assert sum(f.size for f in fills) == 4


class TestQueueInvariants:
    @given(
        initial_depth=st.integers(min_value=0, max_value=50),
        shrinks=st.lists(st.integers(min_value=0, max_value=20), min_size=1, max_size=10),
    )
    def test_queue_ahead_never_increases_and_never_goes_negative(self, initial_depth, shrinks):
        b = PaperBroker()
        oid = b.place_resting_order("yes", Decimal("0.54"), Decimal(1000), ts=T0, book=book(yes=[("0.54", str(initial_depth))]))
        depth = initial_depth
        previous_queue_ahead = b.get_order(oid).queue_ahead
        for shrink in shrinks:
            depth = max(0, depth - shrink)
            b.on_book_update(book(yes=[("0.54", str(depth))]) if depth > 0 else book(), ts=T0)
            current = b.get_order(oid).queue_ahead
            assert 0 <= current <= previous_queue_ahead
            previous_queue_ahead = current
