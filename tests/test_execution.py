from datetime import datetime, timezone
from decimal import Decimal

import pytest

from btcbot.execution import PaperExecutionBackend
from btcbot.models import OrderBook, PriceLevel
from btcbot.paper_broker import PaperBroker, QueueAssumption

T0 = datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc)


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
