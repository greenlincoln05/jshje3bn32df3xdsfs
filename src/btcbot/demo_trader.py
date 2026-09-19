"""``btcbot demo``: the paper trader's exact strategy, placing REAL orders in Kalshi's DEMO environment.

Fake money, real exchange plumbing. Everything that decides (model, strategy, risk manager) is the same code
:class:`btcbot.live_paper.LivePaperTrader` runs; this class only overrides the four execution hooks so an order
becomes a real ``POST`` to the demo exchange and a fill becomes whatever Kalshi's ``/portfolio/fills`` says.

Alongside every real order it places a SHADOW paper order at the same moment on the same book, so each order
ends up with two outcomes in ``demo_orders``: what the exchange actually did (fills, fee, timing) and what the
paper broker's queue model said would happen. Comparing the two is how to see what real routing does to the
numbers the backtest and the Strategy Lab have been assuming: fill rate, price, fees, and how often a
post-only bid is rejected because the book moved.

What this can and cannot tell you (also printed with every report):

* Kalshi's demo book is thin and largely synthetic, so fill behavior here is NOT prod fill behavior. It
  validates plumbing and the fee formula; it does not measure a real edge.
* There is no smart routing: resting bids are ``post_only`` joins of the best bid, so "routing" here means
  the order type and the exchange's response to it.

Hard limits, none of which this module can relax: :class:`btcbot.kalshi_client.KalshiClient` refuses to sign an
order against anything but demo, and this class refuses to be built around a client that is not demo. No
Claude Code session runs this; it needs the owner's own demo key.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from btcbot.config import KalshiEnv
from btcbot.execution import DemoExecutionBackend
from btcbot.kalshi_client import KalshiError, KalshiWriteNotAllowedError
from btcbot.live_paper import LivePaperTrader
from btcbot.models import Market, OrderBook, ParseError
from btcbot.paper_broker import Fill, QueueAssumption, settle

if TYPE_CHECKING:
    from btcbot.config import BotConfig
    from btcbot.kalshi_client import KalshiClient
    from btcbot.spot_feed import SpotBuffer
    from btcbot.strategy import Decision

log = logging.getLogger("btcbot.demo")

DEMO_SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_orders (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    size TEXT NOT NULL,
    placed_ts TEXT NOT NULL,
    order_id TEXT NOT NULL,
    demo_filled TEXT NOT NULL DEFAULT '0',
    demo_cost TEXT NOT NULL DEFAULT '0',
    demo_fee TEXT NOT NULL DEFAULT '0',
    demo_first_fill_ts TEXT,
    paper_filled TEXT NOT NULL DEFAULT '0',
    paper_cost TEXT NOT NULL DEFAULT '0',
    paper_fee TEXT NOT NULL DEFAULT '0',
    paper_first_fill_ts TEXT,
    closed_ts TEXT,
    result TEXT,
    demo_pnl TEXT,
    paper_pnl TEXT
);
CREATE TABLE IF NOT EXISTS demo_events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    ticker TEXT,
    event TEXT NOT NULL,     -- 'order_rejected' | 'cancel_failed' | 'fills_unavailable'
    detail TEXT NOT NULL
);
"""


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat()


@dataclass
class _OrderRecord:
    row_id: int
    ticker: str
    side: str
    price: Decimal
    size: Decimal
    order_id: str
    demo_filled: Decimal = Decimal(0)
    demo_cost: Decimal = Decimal(0)
    demo_fee: Decimal = Decimal(0)
    demo_first_fill_ts: datetime | None = None
    paper_filled: Decimal = Decimal(0)
    paper_cost: Decimal = Decimal(0)
    paper_fee: Decimal = Decimal(0)
    paper_first_fill_ts: datetime | None = None
    closed: bool = False


@dataclass
class DemoStats:
    orders_placed: int = 0
    orders_rejected: int = 0
    cancel_failures: int = 0
    poll_failures: int = 0
    uncancelled_order_ids: list[str] = field(default_factory=list)


class DemoTrader(LivePaperTrader):
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: BotConfig,
        spot_buffer: SpotBuffer,
        client: KalshiClient,
        *,
        maker_fee_multiplier: Decimal = Decimal(0),
        kill_file: str = "KILL",
    ) -> None:
        if getattr(client, "env", None) is not KalshiEnv.DEMO:
            raise KalshiWriteNotAllowedError("DemoTrader only runs against the DEMO environment")
        # The base class's paper broker is kept as the SHADOW: optimistic queue, same fee multiplier.
        super().__init__(
            conn, config, spot_buffer, queue_assumption=QueueAssumption.OPTIMISTIC,
            maker_fee_multiplier=maker_fee_multiplier, kill_file=kill_file,
        )
        conn.executescript(DEMO_SCHEMA)
        conn.commit()
        self._client = client
        self._real: dict[str, DemoExecutionBackend] = {}
        self._shadow_order_id: str | None = None
        self._records: dict[str, list[_OrderRecord]] = {}
        self.stats = DemoStats()

    # ---- bookkeeping

    def _backend_for(self, ticker: str) -> DemoExecutionBackend:
        if ticker not in self._real:
            self._real[ticker] = DemoExecutionBackend(self._client, ticker)
        return self._real[ticker]

    def _latest(self, ticker: str | None) -> _OrderRecord | None:
        records = self._records.get(ticker or "")
        return records[-1] if records else None

    def _event(self, ticker: str | None, event: str, detail: str) -> None:
        ts = self.last_ts or datetime.now(timezone.utc)
        log.warning("%s %s: %s", ticker, event, detail)
        self._conn.execute("INSERT INTO demo_events (ts, ticker, event, detail) VALUES (?,?,?,?)", (_iso(ts), ticker, event, detail))
        self._conn.commit()

    def _save(self, rec: _OrderRecord, *, closed_ts: datetime | None = None) -> None:
        self._conn.execute(
            """UPDATE demo_orders SET demo_filled=?, demo_cost=?, demo_fee=?, demo_first_fill_ts=?, paper_filled=?,
               paper_cost=?, paper_fee=?, paper_first_fill_ts=?, closed_ts=COALESCE(?, closed_ts) WHERE id=?""",
            (str(rec.demo_filled), str(rec.demo_cost), str(rec.demo_fee),
             None if rec.demo_first_fill_ts is None else _iso(rec.demo_first_fill_ts),
             str(rec.paper_filled), str(rec.paper_cost), str(rec.paper_fee),
             None if rec.paper_first_fill_ts is None else _iso(rec.paper_first_fill_ts),
             None if closed_ts is None else _iso(closed_ts), rec.row_id),
        )
        self._conn.commit()

    # ---- execution hooks (see LivePaperTrader)

    async def _sync_fills(self, book: OrderBook, poll_ts: datetime) -> list[Fill]:
        shadow_fills = await super()._sync_fills(book, poll_ts)  # keeps the paper broker's queue model current
        rec = self._latest(self._current_ticker)
        if rec is not None:
            for fill in shadow_fills:
                rec.paper_filled += fill.size
                rec.paper_cost += fill.price * fill.size
                rec.paper_fee += fill.fee
                rec.paper_first_fill_ts = rec.paper_first_fill_ts or fill.ts
        real_fills = await self._poll_real(self._current_ticker, rec)
        if rec is not None and (shadow_fills or real_fills):
            self._save(rec)
        return real_fills

    async def _poll_real(self, ticker: str | None, rec: _OrderRecord | None) -> list[Fill]:
        if ticker is None:
            return []
        try:
            fills = await self._backend_for(ticker).poll_fills()
        except (KalshiError, ParseError) as exc:
            self.stats.poll_failures += 1
            self._event(ticker, "fills_unavailable", str(exc))
            return []  # try again next snapshot; a fill is never lost, only late (poll_fills dedupes by fill id)
        if rec is not None:
            for fill in fills:
                rec.demo_filled += fill.size
                rec.demo_cost += fill.price * fill.size
                rec.demo_fee += fill.fee
                rec.demo_first_fill_ts = rec.demo_first_fill_ts or fill.ts
        return fills

    def _resting_order_filled(self) -> bool:
        rec = self._latest(self._current_ticker)
        return rec is not None and rec.demo_filled >= rec.size

    async def _place_resting(self, decision: Decision, poll_ts: datetime) -> str | None:
        ticker = self._current_ticker
        shadow_id = await super()._place_resting(decision, poll_ts)  # the same order, simulated, on the same book
        try:
            order_id = await self._backend_for(ticker).place_resting_order(decision.side, decision.price, decision.size)
        except KalshiError as exc:
            # Most often a post-only bid that would have crossed because the book moved since the snapshot.
            self.stats.orders_rejected += 1
            await self._backend.cancel_order(shadow_id)
            self._event(ticker, "order_rejected", f"{decision.side} {decision.size}@{decision.price}: {exc}")
            return None
        except ParseError as exc:
            # The exchange may have ACCEPTED the order while we could not read its id: never leave it resting
            # untracked. Find whatever is resting on this market and cancel it.
            self.stats.orders_rejected += 1
            await self._backend.cancel_order(shadow_id)
            self._event(ticker, "order_ack_unreadable", f"{exc}; cancelling anything resting on {ticker}")
            await self._cancel_everything_resting(ticker)
            return None
        self.stats.orders_placed += 1
        self._shadow_order_id = shadow_id
        cursor = self._conn.execute(
            "INSERT INTO demo_orders (ticker, side, price, size, placed_ts, order_id) VALUES (?,?,?,?,?,?)",
            (ticker, decision.side, str(decision.price), str(decision.size), _iso(poll_ts), order_id),
        )
        self._conn.commit()
        self._records.setdefault(ticker, []).append(
            _OrderRecord(cursor.lastrowid, ticker, decision.side, decision.price, decision.size, order_id)
        )
        return order_id

    async def _cancel_everything_resting(self, ticker: str) -> None:
        try:
            for order in await self._client.list_orders(ticker=ticker, status="resting"):
                await self._client.cancel_order(order.order_id, market_ticker=ticker)
        except (KalshiError, ParseError) as exc:
            self.stats.cancel_failures += 1
            self._event(ticker, "cancel_failed", f"could not clear resting orders: {exc}")

    async def _cancel_resting(self) -> None:
        ticker = self._current_ticker
        rec = self._latest(ticker)
        real_id = self._resting_order_id
        remaining = Decimal(0) if rec is None else max(rec.size - rec.demo_filled, Decimal(0))
        try:
            await self._backend_for(ticker).cancel_order(real_id)
        except (KalshiError, ParseError) as exc:
            # Already filled/cancelled is normal; a transport failure leaves it resting, so remember it for the
            # end-of-run retry and the next start's reconcile().
            self.stats.cancel_failures += 1
            self.stats.uncancelled_order_ids.append(real_id)
            self._event(ticker, "cancel_failed", f"{real_id}: {exc}")
        if self._shadow_order_id is not None:
            await self._backend.cancel_order(self._shadow_order_id)
            self._shadow_order_id = None
        if remaining > 0 and rec is not None:
            self._risk.release_exposure(remaining * rec.price)
        if rec is not None:
            rec.closed = True
            self._save(rec, closed_ts=self.last_ts)
        self._resting_order_id = None

    async def _roll_over(self, poll_ts: datetime) -> None:
        if self._resting_order_id is not None:
            await self._cancel_resting()
        # A cancel can race a fill: look once more at the window that just ended before its position is filed.
        for fill in await self._poll_real(self._current_ticker, self._latest(self._current_ticker)):
            self._apply_fill(self._current_ticker, fill, poll_ts, 0.5)
        await super()._roll_over(poll_ts)

    async def on_settlement(self, market: Market) -> None:
        await super().on_settlement(market)
        result = market.raw.get("result") or None
        if result not in ("yes", "no"):
            return
        for rec in self._records.get(market.ticker, []):
            demo_pnl = settle(rec.side, rec.demo_filled, result) - rec.demo_cost - rec.demo_fee
            paper_pnl = settle(rec.side, rec.paper_filled, result) - rec.paper_cost - rec.paper_fee
            self._conn.execute(
                "UPDATE demo_orders SET result=?, demo_pnl=?, paper_pnl=? WHERE id=?",
                (result, str(demo_pnl), str(paper_pnl), rec.row_id),
            )
        self._conn.commit()

    async def shutdown(self, ts: datetime) -> None:
        await super().shutdown(ts)
        for order_id in list(self.stats.uncancelled_order_ids):  # one last try at anything left resting
            for ticker in self._real:
                try:
                    await self._real[ticker].cancel_order(order_id)
                    self.stats.uncancelled_order_ids.remove(order_id)
                    break
                except KalshiError:
                    continue


# --------------------------------------------------------------------------- report


CAVEATS = (
    "Kalshi's demo book is thin and largely synthetic: this validates order plumbing and fee handling, not "
    "what fills you would get on prod, and it says nothing about edge.",
    "'paper' below is the same order simulated by the optimistic queue model at the same moment; the gap "
    "between it and 'demo' is how far the backtest's fill assumptions are from a real exchange response.",
)


def render_demo_report(conn: sqlite3.Connection, stats: DemoStats | None = None) -> str:
    rows = conn.execute(
        """SELECT ticker, side, price, size, demo_filled, demo_fee, demo_pnl, paper_filled, paper_fee, paper_pnl, result
           FROM demo_orders ORDER BY id"""
    ).fetchall()
    lines = ["Demo orders (real, fake money) vs the paper simulation of the same orders:"]
    if not rows:
        lines.append("  no orders were placed.")
    else:
        lines.append(f"  {'window':<10s} {'side':<4s} {'px':>6s} {'size':>5s} | {'demo fill':>9s} {'fee':>7s} {'pnl':>8s} | {'paper fill':>10s} {'fee':>7s} {'pnl':>8s} | result")
        for ticker, side, price, size, df, dfee, dpnl, pf, pfee, ppnl, result in rows:
            def money(v):
                return "--" if v is None else f"{Decimal(v):.4f}"
            lines.append(
                f"  {ticker[-8:]:<10s} {side:<4s} {Decimal(price):>6.2f} {Decimal(size):>5.0f} | {Decimal(df):>9.2f} {money(dfee):>7s} {money(dpnl):>8s} | "
                f"{Decimal(pf):>10.2f} {money(pfee):>7s} {money(ppnl):>8s} | {result or 'pending'}"
            )
        placed = len(rows)
        demo_filled = sum(1 for r in rows if Decimal(r[4]) > 0)
        paper_filled = sum(1 for r in rows if Decimal(r[7]) > 0)
        both = sum(1 for r in rows if Decimal(r[4]) > 0 and Decimal(r[7]) > 0)
        lines.append(
            f"  orders {placed}: filled on demo {demo_filled}, filled in paper {paper_filled}, both {both} "
            f"(paper filled but demo did not: {paper_filled - both}; demo filled but paper did not: {demo_filled - both})"
        )
        demo_total = sum((Decimal(r[6]) for r in rows if r[6] is not None), Decimal(0))
        paper_total = sum((Decimal(r[9]) for r in rows if r[9] is not None), Decimal(0))
        lines.append(f"  settled PnL: demo ${demo_total:.4f}   paper ${paper_total:.4f}   (only orders whose window has settled)")
    if stats is not None:
        lines.append(
            f"  rejected orders {stats.orders_rejected}, failed cancels {stats.cancel_failures}, "
            f"fill-poll failures {stats.poll_failures}"
            + (f", STILL RESTING: {', '.join(stats.uncancelled_order_ids)}" if stats.uncancelled_order_ids else "")
        )
    lines.append("")
    lines += [f"  - {c}" for c in CAVEATS]
    return "\n".join(lines)
