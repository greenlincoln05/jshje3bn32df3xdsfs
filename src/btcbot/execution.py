"""Order placement, cancel/replace and fill tracking, per docs/btc15m-bot-spec.md sections 3 and 7.

One interface a strategy drives without knowing whether it is paper or demo (live stays behind all four
gates in section 7 and has no backend here at all -- :class:`btcbot.kalshi_client.KalshiClient` itself
refuses to sign a write call against anything but the demo environment, so :class:`DemoExecutionBackend`
cannot become a live backend just by pointing it at a different `KalshiEnv`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from btcbot.models import KalshiFill, KalshiOrder, OrderBook, Position, Side
from btcbot.paper_broker import Fill, PaperBroker

if TYPE_CHECKING:
    from btcbot.kalshi_client import KalshiClient


class ExecutionBackend(Protocol):
    """Shared by every backend (paper and demo). A strategy that only calls these methods cannot tell
    which one it is talking to."""

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


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """What :meth:`DemoExecutionBackend.reconcile` found and did, for a caller (``btcbot demo-check``) to
    log rather than silently drop."""

    cancelled_order_ids: list[str]
    open_positions: list[Position]


class DemoExecutionBackend:
    """Places real orders against Kalshi's demo environment (fake money; the demo order book is thin and
    synthetic -- see the README's "Verified Kalshi API facts"). Every write goes through
    :class:`btcbot.kalshi_client.KalshiClient`, whose own hard assertion refuses to sign a write call
    against anything but ``KalshiEnv.DEMO`` -- this class adds no gate of its own and cannot become a live
    backend just by pointing it at a different client.

    Fills come from polling :meth:`btcbot.kalshi_client.KalshiClient.list_fills`, not a push channel (an
    authenticated WebSocket fill/order channel is a later addition, only if polling proves too slow).
    Converted to :class:`btcbot.paper_broker.Fill`'s exact shape, so a run that feeds the same book
    snapshots to both this backend and :class:`PaperExecutionBackend` produces two directly comparable fill
    streams -- this is what Phase 6d's fidelity report compares.
    """

    def __init__(self, client: KalshiClient, ticker: str) -> None:
        self._client = client
        self._ticker = ticker
        self._seen_fill_keys: set[str] = set()
        self._primed = False

    async def _prime(self) -> None:
        """Mark every fill the account already has for this ticker as seen, WITHOUT returning it. Without
        this, the first poll after a restart (or after ``reconcile``) would hand back the window's whole fill
        history as if it were new, and the trader would book a position it already holds a second time."""
        for fill in await self._client.list_fills(ticker=self._ticker):
            self._seen_fill_keys.add(fill.dedupe_key)
        self._primed = True

    async def reconcile(self) -> ReconciliationReport:
        """Call once at startup, before placing anything. A freshly started process has no legitimate
        resting orders yet, so every open order this account holds is by definition a leftover from a
        previous crashed run; cancel all of them. Open positions cannot be cancelled -- they are already
        filled contracts, and this bot does not exit early (spec: hold to settlement by default) -- so they
        are only reported, never acted on."""
        open_orders: list[KalshiOrder] = [order for order in await self._client.list_orders() if not order.is_done]
        cancelled: list[str] = []
        for order in open_orders:
            await self._client.cancel_order(order.order_id, market_ticker=order.ticker)
            cancelled.append(order.order_id)
        await self._prime()
        positions = [position for position in await self._client.get_positions() if position.count > 0]
        return ReconciliationReport(cancelled_order_ids=cancelled, open_positions=positions)

    async def place_resting_order(self, side: Side, price: Decimal, size: Decimal) -> str:
        if not self._primed:
            await self._prime()  # so a fill from BEFORE this order is never mistaken for one of its own
        order = await self._client.create_order(self._ticker, side, count=size, price=price)
        return order.order_id

    async def cancel_order(self, order_id: str) -> None:
        await self._client.cancel_order(order_id, market_ticker=self._ticker)

    async def place_taker_order(self, side: Side, size: Decimal) -> list[Fill]:
        if not self._primed:
            await self._prime()
        order = await self._client.create_order(self._ticker, side, count=size)  # no price -> marketable IOC
        fills = await self._client.list_fills(ticker=self._ticker, order_id=order.order_id)
        return [self._to_fill(f) for f in fills if self._mark_seen(f)]

    async def poll_fills(self) -> list[Fill]:
        """Call once per polled snapshot (the same cadence :meth:`PaperExecutionBackend.sync_market` is fed
        at). Returns only fills not already returned by an earlier call, or present before this backend
        started."""
        if not self._primed:
            await self._prime()
            return []
        fills = await self._client.list_fills(ticker=self._ticker)
        return [self._to_fill(f) for f in fills if self._mark_seen(f)]

    def _mark_seen(self, fill: KalshiFill) -> bool:
        key = fill.dedupe_key
        if key in self._seen_fill_keys:
            return False
        self._seen_fill_keys.add(key)
        return True

    @staticmethod
    def _to_fill(kalshi_fill: KalshiFill) -> Fill:
        return Fill(
            side=kalshi_fill.side,
            price=kalshi_fill.price,
            size=kalshi_fill.count,
            fee=kalshi_fill.fee_usd,
            maker=not kalshi_fill.is_taker,
            ts=kalshi_fill.created_time,
        )
