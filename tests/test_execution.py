import itertools
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from btcbot.execution import DemoExecutionBackend, PaperExecutionBackend
from btcbot.models import KalshiFill, KalshiOrder, OrderBook, Position, PriceLevel
from btcbot.paper_broker import PaperBroker, QueueAssumption

T0 = datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc)
TICKER = "KXBTC15M-26SEP182145-45"


def book(yes=(), no=()):
    return OrderBook(
        "T",
        yes_bids=tuple(PriceLevel(Decimal(p), Decimal(s)) for p, s in yes),
        no_bids=tuple(PriceLevel(Decimal(p), Decimal(s)) for p, s in no),
    )


class TestRequiresSyncFirst:
    async def test_place_resting_order_before_sync_raises(self):
        backend = PaperExecutionBackend(PaperBroker())
        with pytest.raises(RuntimeError, match="sync_market"):
            await backend.place_resting_order("yes", Decimal("0.5"), Decimal(1))

    async def test_cancel_before_sync_raises(self):
        backend = PaperExecutionBackend(PaperBroker())
        with pytest.raises(RuntimeError, match="sync_market"):
            await backend.cancel_order("some-id")

    async def test_taker_order_before_sync_raises(self):
        backend = PaperExecutionBackend(PaperBroker())
        with pytest.raises(RuntimeError, match="sync_market"):
            await backend.place_taker_order("yes", Decimal(1))


class TestDelegatesToTheBroker:
    async def test_sync_market_returns_fills_from_the_underlying_broker(self):
        broker = PaperBroker(queue_assumption=QueueAssumption.OPTIMISTIC)
        backend = PaperExecutionBackend(broker)
        backend.sync_market(book(yes=[("0.5", "1")]), T0)
        oid = await backend.place_resting_order("yes", Decimal("0.5"), Decimal(1))
        backend.sync_market(book(yes=[("0.5", "0")]), T0)  # first update: reaches the front, no fill
        assert backend.sync_market(book(yes=[("0.5", "1")]), T0) == []
        fills = backend.sync_market(book(yes=[]), T0)  # trades through
        assert [f.size for f in fills] == [1]
        assert broker.get_order(oid).status == "filled"

    async def test_place_and_cancel_round_trip(self):
        broker = PaperBroker()
        backend = PaperExecutionBackend(broker)
        backend.sync_market(book(yes=[("0.5", "1")]), T0)
        oid = await backend.place_resting_order("yes", Decimal("0.5"), Decimal(1))
        await backend.cancel_order(oid)
        assert broker.get_order(oid).status == "cancelled"

    async def test_taker_order_delegates_to_the_broker(self):
        broker = PaperBroker()
        backend = PaperExecutionBackend(broker)
        backend.sync_market(book(no=[("0.4", "10")]), T0)
        fills = await backend.place_taker_order("yes", Decimal(3))
        assert len(fills) == 1 and fills[0].size == 3 and fills[0].price == Decimal("0.6")

    async def test_using_the_latest_synced_book_not_a_stale_one(self):
        broker = PaperBroker()
        backend = PaperExecutionBackend(broker)
        backend.sync_market(book(yes=[("0.5", "1")]), T0)
        backend.sync_market(book(yes=[("0.6", "1")]), T0)
        oid = await backend.place_resting_order("yes", Decimal("0.6"), Decimal(1))
        assert broker.get_order(oid).price == Decimal("0.6")


def make_order(order_id, *, ticker=TICKER, side="yes", status="resting", price=Decimal("0.30"), count=Decimal(5)):
    return KalshiOrder(
        order_id=order_id, client_order_id=None, ticker=ticker, side=side, action="buy",
        order_type="limit" if price is not None else "market", status=status, price=price,
        initial_count=count, remaining_count=count, created_time=T0,
    )


def make_fill(trade_id, order_id, *, ticker=TICKER, side="yes", price=Decimal("0.30"), count=Decimal(1),
              fee=Decimal("0"), is_taker=False):
    return KalshiFill(
        trade_id=trade_id, order_id=order_id, ticker=ticker, side=side, action="buy", price=price,
        count=count, fee_usd=fee, is_taker=is_taker, created_time=T0,
    )


class FakeKalshiClient:
    """Records calls and returns scripted results. DemoExecutionBackend only needs this method surface --
    real signing/retry/HTTP behavior is already covered by test_client.py, so this fake skips all of it."""

    def __init__(self):
        self.created_orders: list[dict] = []
        self.cancelled_order_ids: list[str] = []
        self.orders_to_return: list[KalshiOrder] = []
        self.positions_to_return: list[Position] = []
        self.fills_to_return: list[KalshiFill] = []
        self._ids = itertools.count(1)

    async def create_order(self, ticker, side, *, count, price=None, client_order_id=None):
        order_id = f"ord-{next(self._ids)}"
        self.created_orders.append(
            {"ticker": ticker, "side": side, "count": count, "price": price, "client_order_id": client_order_id}
        )
        return make_order(order_id, ticker=ticker, side=side, price=price, count=count)

    async def cancel_order(self, order_id):
        self.cancelled_order_ids.append(order_id)
        return make_order(order_id, status="canceled")

    async def list_orders(self, *, ticker=None, status=None):
        return self.orders_to_return

    async def list_fills(self, *, ticker=None, order_id=None):
        fills = self.fills_to_return
        return [f for f in fills if order_id is None or f.order_id == order_id]

    async def get_positions(self):
        return self.positions_to_return


class TestDemoExecutionBackendOrders:
    async def test_place_resting_order_sends_side_price_count_and_returns_the_order_id(self):
        client = FakeKalshiClient()
        backend = DemoExecutionBackend(client, TICKER)

        order_id = await backend.place_resting_order("yes", Decimal("0.30"), Decimal(5))

        assert client.created_orders == [
            {"ticker": TICKER, "side": "yes", "count": Decimal(5), "price": Decimal("0.30"), "client_order_id": None}
        ]
        assert order_id == "ord-1"

    async def test_cancel_order_delegates_to_the_client(self):
        client = FakeKalshiClient()
        backend = DemoExecutionBackend(client, TICKER)
        await backend.cancel_order("ord-1")
        assert client.cancelled_order_ids == ["ord-1"]

    async def test_taker_order_places_a_market_order_and_returns_its_fills(self):
        client = FakeKalshiClient()
        backend = DemoExecutionBackend(client, TICKER)
        client.fills_to_return = [make_fill("t-1", "ord-1", side="yes", price=Decimal("0.55"), count=Decimal(3), is_taker=True)]
        # the fake assigns "ord-1" to the first created order; place_taker_order must ask for that order's fills
        fills = await backend.place_taker_order("yes", Decimal(3))

        assert client.created_orders[0]["price"] is None  # no price -> market order
        assert len(fills) == 1
        assert fills[0].size == Decimal(3) and fills[0].price == Decimal("0.55") and fills[0].maker is False

    async def test_poll_fills_only_returns_each_fill_once(self):
        client = FakeKalshiClient()
        backend = DemoExecutionBackend(client, TICKER)
        client.fills_to_return = [make_fill("t-1", "ord-1", fee=Decimal("0.01"))]

        first = await backend.poll_fills()
        second = await backend.poll_fills()  # same fill still in list_fills's response

        assert len(first) == 1 and first[0].fee == Decimal("0.01") and first[0].maker is True
        assert second == []

    async def test_poll_fills_picks_up_new_fills_across_calls(self):
        client = FakeKalshiClient()
        backend = DemoExecutionBackend(client, TICKER)
        client.fills_to_return = [make_fill("t-1", "ord-1")]
        await backend.poll_fills()
        client.fills_to_return = [make_fill("t-1", "ord-1"), make_fill("t-2", "ord-1")]

        second = await backend.poll_fills()

        assert [f.side for f in second] == ["yes"]  # only the new one


class TestDemoExecutionBackendReconciliation:
    async def test_cancels_every_open_order_and_reports_positions(self):
        client = FakeKalshiClient()
        client.orders_to_return = [
            make_order("stale-1", status="resting"),
            make_order("stale-2", status="resting"),
            make_order("done-1", status="executed"),  # already terminal: not cancelled
        ]
        client.positions_to_return = [
            Position(ticker=TICKER, side="yes", count=Decimal(4), market_exposure_usd=Decimal("1.20")),
            Position(ticker="OTHER", side="no", count=Decimal(0), market_exposure_usd=None),  # flat: excluded
        ]
        backend = DemoExecutionBackend(client, TICKER)

        report = await backend.reconcile()

        assert set(client.cancelled_order_ids) == {"stale-1", "stale-2"}
        assert report.cancelled_order_ids == client.cancelled_order_ids
        assert [p.ticker for p in report.open_positions] == [TICKER]

    async def test_no_open_orders_or_positions_is_a_clean_no_op(self):
        client = FakeKalshiClient()
        backend = DemoExecutionBackend(client, TICKER)
        report = await backend.reconcile()
        assert report.cancelled_order_ids == [] and report.open_positions == []
