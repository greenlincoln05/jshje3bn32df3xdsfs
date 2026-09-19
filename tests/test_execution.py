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
              fee=Decimal("0"), is_taker=False, fill_id=""):
    return KalshiFill(
        trade_id=trade_id, order_id=order_id, ticker=ticker, side=side, action="buy", price=price,
        count=count, fee_usd=fee, is_taker=is_taker, created_time=T0, fill_id=fill_id,
    )


class FakeKalshiClient:
    """Records calls and returns scripted results. DemoExecutionBackend only needs this method surface --
    real signing/retry/HTTP behavior is already covered by test_client.py, so this fake skips all of it."""

    def __init__(self):
        self.created_orders: list[dict] = []
        self.cancelled: list[tuple[str, str | None]] = []
        self.orders_to_return: list[KalshiOrder] = []
        self.positions_to_return: list[Position] = []
        self.fills_to_return: list[KalshiFill] = []
        self.fills_on_create: list[KalshiFill] = []  # what a just-placed marketable order would immediately fill
        self.calls: list[str] = []
        self._ids = itertools.count(1)

    @property
    def cancelled_order_ids(self) -> list[str]:
        return [order_id for order_id, _ in self.cancelled]

    async def create_order(self, ticker, side, *, count, price=None, client_order_id=None):
        self.calls.append("create")
        order_id = f"ord-{next(self._ids)}"
        self.created_orders.append(
            {"ticker": ticker, "side": side, "count": count, "price": price, "client_order_id": client_order_id}
        )
        self.fills_to_return = self.fills_to_return + self.fills_on_create
        return make_order(order_id, ticker=ticker, side=side, price=price, count=count)

    async def cancel_order(self, order_id, *, market_ticker=None):
        self.calls.append("cancel")
        self.cancelled.append((order_id, market_ticker))
        return make_order(order_id, status="canceled")

    async def list_orders(self, *, ticker=None, status=None):
        return self.orders_to_return

    async def list_fills(self, *, ticker=None, order_id=None, min_ts=None):
        self.calls.append("list_fills")
        return [f for f in self.fills_to_return if order_id is None or f.order_id == order_id]

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

    async def test_cancel_order_passes_the_market_ticker_kalshi_needs_for_routing(self):
        client = FakeKalshiClient()
        backend = DemoExecutionBackend(client, TICKER)
        await backend.cancel_order("ord-1")
        assert client.cancelled == [("ord-1", TICKER)]

    async def test_taker_order_places_a_marketable_order_and_returns_its_fills(self):
        client = FakeKalshiClient()
        backend = DemoExecutionBackend(client, TICKER)
        client.fills_on_create = [make_fill("t-1", "ord-1", side="yes", price=Decimal("0.55"), count=Decimal(3), is_taker=True)]

        fills = await backend.place_taker_order("yes", Decimal(3))

        assert client.created_orders[0]["price"] is None  # no price -> marketable order
        assert len(fills) == 1
        assert fills[0].size == Decimal(3) and fills[0].price == Decimal("0.55") and fills[0].maker is False

    async def test_a_taker_fill_is_not_returned_again_by_the_next_poll(self):
        client = FakeKalshiClient()
        backend = DemoExecutionBackend(client, TICKER)
        client.fills_on_create = [make_fill("t-1", "ord-1", is_taker=True)]
        await backend.place_taker_order("yes", Decimal(1))
        assert await backend.poll_fills() == []


class TestDemoExecutionBackendFillPolling:
    async def test_the_first_poll_only_learns_what_already_exists_and_returns_nothing(self):
        """A restart mid-window must not re-book fills the account already holds."""
        client = FakeKalshiClient()
        client.fills_to_return = [make_fill("t-old", "ord-old", fill_id="f-old")]
        backend = DemoExecutionBackend(client, TICKER)

        assert await backend.poll_fills() == []
        assert await backend.poll_fills() == []  # and it stays quiet: the old fill is remembered

    async def test_a_new_fill_after_priming_is_returned_exactly_once(self):
        client = FakeKalshiClient()
        client.fills_to_return = [make_fill("t-old", "ord-old", fill_id="f-old")]
        backend = DemoExecutionBackend(client, TICKER)
        await backend.poll_fills()  # primes
        client.fills_to_return.append(make_fill("t-1", "ord-1", fee=Decimal("0.01"), fill_id="f-1"))

        first = await backend.poll_fills()
        second = await backend.poll_fills()  # same fill still in list_fills's response

        assert len(first) == 1 and first[0].fee == Decimal("0.01") and first[0].maker is True
        assert second == []

    async def test_placing_an_order_primes_first_so_earlier_fills_are_never_its_own(self):
        client = FakeKalshiClient()
        client.fills_to_return = [make_fill("t-old", "ord-old", fill_id="f-old")]
        backend = DemoExecutionBackend(client, TICKER)

        await backend.place_resting_order("yes", Decimal("0.30"), Decimal(1))

        assert client.calls == ["list_fills", "create"]  # the history was read BEFORE the order existed
        assert await backend.poll_fills() == []

    async def test_fills_that_share_a_trade_id_are_told_apart_by_fill_id(self):
        client = FakeKalshiClient()
        backend = DemoExecutionBackend(client, TICKER)
        await backend.poll_fills()  # primes on an empty account
        client.fills_to_return = [
            make_fill("t-1", "ord-1", fill_id="f-a", count=Decimal(2)),
            make_fill("t-1", "ord-1", fill_id="f-b", count=Decimal(3)),  # same trade, a second partial fill
        ]

        fills = await backend.poll_fills()

        assert sorted(f.size for f in fills) == [Decimal(2), Decimal(3)]

    async def test_a_fill_without_a_fill_id_falls_back_to_the_trade_id(self):
        client = FakeKalshiClient()
        backend = DemoExecutionBackend(client, TICKER)
        await backend.poll_fills()
        client.fills_to_return = [make_fill("t-1", "ord-1")]
        assert len(await backend.poll_fills()) == 1
        assert await backend.poll_fills() == []


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
        assert all(ticker == TICKER for _, ticker in client.cancelled)  # each cancel names its own market
        assert report.cancelled_order_ids == client.cancelled_order_ids
        assert [p.ticker for p in report.open_positions] == [TICKER]

    async def test_reconcile_primes_fill_history_so_leftovers_are_not_re_booked(self):
        client = FakeKalshiClient()
        client.fills_to_return = [make_fill("t-old", "ord-old", fill_id="f-old")]
        backend = DemoExecutionBackend(client, TICKER)
        await backend.reconcile()
        assert await backend.poll_fills() == []

    async def test_no_open_orders_or_positions_is_a_clean_no_op(self):
        client = FakeKalshiClient()
        backend = DemoExecutionBackend(client, TICKER)
        report = await backend.reconcile()
        assert report.cancelled_order_ids == [] and report.open_positions == []
