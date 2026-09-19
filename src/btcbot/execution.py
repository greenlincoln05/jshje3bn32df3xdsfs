"""Order placement, cancel/replace and fill tracking (Phase 4), per docs/btc15m-bot-spec.md sections 3 and 7.

One interface a strategy drives without knowing whether it is paper or live. Only the paper backend exists:
the demo and live backends arrive in Phases 6 and 7, and live stays behind all four gates in section 7.
CLAUDE.md is explicit that no order-placing code against Kalshi itself gets added before the owner approves
Phase 6, so :class:`ExecutionBackend` has no implementation here that talks to the network.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from btcbot.models import OrderBook, Side
from btcbot.paper_broker import Fill, PaperBroker


class ExecutionBackend(Protocol):
    """Shared by every backend (paper now; demo and live later). A strategy that only calls these methods
    cannot tell which one it is talking to."""

    async def place_resting_order(self, side: Side, price: Decimal, size: Decimal) -> str: ...
    async def cancel_order(self, order_id: str) -> None: ...
    async def place_taker_order(self, side: Side, size: Decimal) -> list[Fill]: ...


class PaperExecutionBackend:
    """Delegates to :class:`btcbot.paper_broker.PaperBroker`. Never talks to Kalshi."""

    def __init__(self, broker: PaperBroker) -> None:
        self._broker = broker
        self._book: OrderBook | None = None
        self._now: datetime | None = None

    def sync_market(self, book: OrderBook, now: datetime) -> list[Fill]:
        """Feed in the latest polled snapshot; returns fills produced for any resting orders. Call this
        once per snapshot, before any place/cancel call for that snapshot."""
        self._book = book
        self._now = now
        return self._broker.on_book_update(book, ts=now)

    def _require_market(self) -> tuple[OrderBook, datetime]:
        if self._book is None or self._now is None:
            raise RuntimeError("sync_market must be called before placing or cancelling an order")
        return self._book, self._now

    async def place_resting_order(self, side: Side, price: Decimal, size: Decimal) -> str:
        book, now = self._require_market()
        return self._broker.place_resting_order(side, price, size, ts=now, book=book)

    async def cancel_order(self, order_id: str) -> None:
        _, now = self._require_market()
        self._broker.cancel(order_id, ts=now)

    async def place_taker_order(self, side: Side, size: Decimal) -> list[Fill]:
        book, now = self._require_market()
        return self._broker.place_taker_order(side, size, book=book, ts=now)
