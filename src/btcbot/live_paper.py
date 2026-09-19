"""Live paper trading loop (Phase 5), per docs/btc15m-bot-spec.md section 8.5.

Runs the strategy, risk and paper broker against data as it is polled, sharing every building block
backtest.py replays offline: :mod:`btcbot.model` for predictions, :mod:`btcbot.strategy` for decisions,
:mod:`btcbot.risk` for limits, :mod:`btcbot.execution`'s ``PaperExecutionBackend`` for fills. Every
prediction is logged (``btcbot.model.log_prediction``) and every trade is recorded in the same shape
backtest.py uses (``TradeRecord``, ``build_report``), so ``btcbot backtest --db <this run's database>``
replays the exact same recorded data afterward -- comparing its report to this run's own summary is spec
section 8.5's "compare live paper results to the backtest."

:class:`btcbot.recorder.Recorder` still owns polling and the base tables (order books, market state, spot
ticks, settlements); this module only hooks into it (``on_orderbook``/``on_settlement``) rather than polling
a second time, so ``btcbot paper`` records exactly the same data ``btcbot record`` does, with this run's
trading activity layered on top of it via a second connection to the same database file.

Settlement for a window is very often not yet known by the time the *next* window's first snapshot rolls
in (Kalshi finalizes a market a few seconds after close, per the README's verified timings), so a filled
position moves to a pending-settlement table at rollover and is only resolved into a :class:`TradeRecord`
once ``on_settlement`` actually fires for that ticker -- mirroring recorder.py's own ``pending_settlement``
handling of the same timing gap. Anything still pending when the run stops is reported unresolved, never
guessed at as a loss.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from btcbot.backtest import BacktestReport, TradeRecord, build_report
from btcbot.execution import PaperExecutionBackend
from btcbot.model import EwmaVolatility, ModelState, Prediction, init_predictions_schema, log_prediction, log_return, predict
from btcbot.models import Market, OrderBook
from btcbot.paper_broker import Fill, PaperBroker, QueueAssumption, settle
from btcbot.risk import RiskManager, TradeOutcome
from btcbot.strategy import Action, decide

if TYPE_CHECKING:
    from btcbot.config import BotConfig
    from btcbot.spot_feed import SpotBuffer


class LivePaperTrader:
    """Owns per-tick trading state. Feed it from a live source: ``on_spot_tick`` from a spot feed's
    ``on_tick``, ``on_orderbook_snapshot``/``on_settlement`` from :class:`btcbot.recorder.Recorder`'s hooks
    of the same names. Call :meth:`shutdown` once the recorder loop stops, then :meth:`report`."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        config: BotConfig,
        spot_buffer: SpotBuffer,
        *,
        queue_assumption: QueueAssumption = QueueAssumption.OPTIMISTIC,
        maker_fee_multiplier: Decimal = Decimal("0"),
        kill_file: str = "KILL",
    ) -> None:
        init_predictions_schema(conn)
        self._conn = conn
        self._config = config
        self._spot_buffer = spot_buffer
        self._queue_assumption = queue_assumption
        self._maker_fee_multiplier = maker_fee_multiplier
        self._broker = PaperBroker(maker_fee_multiplier=maker_fee_multiplier, queue_assumption=queue_assumption)
        self._backend = PaperExecutionBackend(self._broker)
        self._risk = RiskManager(config.risk, kill_file=kill_file, clock=lambda: self.last_ts or datetime.now(timezone.utc))
        self._vol = EwmaVolatility(config.vol_window_sec)

        self._last_price_for_vol: Decimal | None = None
        self._current_ticker: str | None = None
        self._resting_order_id: str | None = None
        self._position: TradeRecord | None = None
        self._pending_settlements: dict[str, TradeRecord] = {}

        self.trades: list[TradeRecord] = []
        self.windows_traded: set[str] = set()
        self.first_ts: datetime | None = None
        self.last_ts: datetime | None = None
        self._tickers_seen: set[str] = set()

    @property
    def risk(self) -> RiskManager:
        return self._risk

    @property
    def unresolved_count(self) -> int:
        return len(self._pending_settlements)

    def on_spot_tick(self, price: Decimal) -> None:
        if self._last_price_for_vol is not None:
            self._vol.update(log_return(self._last_price_for_vol, price))
        self._last_price_for_vol = price

    async def on_orderbook_snapshot(self, market: Market, book: OrderBook, poll_ts: datetime) -> None:
        self.first_ts = self.first_ts or poll_ts
        self.last_ts = poll_ts
        self._tickers_seen.add(market.ticker)

        if market.ticker != self._current_ticker:
            if self._current_ticker is not None:
                await self._roll_over(poll_ts)
            self._current_ticker = market.ticker

        if market.strike is None:
            return
        spot = self._spot_buffer.price()
        if spot is None:
            return
        tau_sec = (market.close_time - poll_ts).total_seconds()
        if tau_sec < 0:
            return

        state = ModelState(spot=spot, strike=market.strike, tau_sec=tau_sec, sigma=self._vol.sigma, market_mid=book.mid("yes"))
        p_model, p_blend = predict(state, blend=float(self._config.model_blend))
        log_prediction(
            self._conn, Prediction(market.ticker, poll_ts, state, float(self._config.model_blend), p_model, p_blend)
        )

        for fill in self._backend.sync_market(book, poll_ts):
            self.windows_traded.add(market.ticker)
            self._apply_fill(market.ticker, fill, poll_ts, p_blend)
        if self._resting_order_id is not None and self._broker.get_order(self._resting_order_id).status == "filled":
            self._resting_order_id = None

        decision = decide(
            book=book,
            tau_sec=tau_sec,
            p_yes=p_blend,
            spot_is_stale=self._spot_buffer.is_stale(),
            min_edge=self._config.min_edge,
            min_depth=self._config.min_depth,
            max_spread=self._config.max_spread,
            min_tau_sec=self._config.min_tau_sec,
            max_tau_sec=self._config.max_tau_sec,
            cancel_before_close_sec=self._config.cancel_before_close_sec,
            contracts_per_trade=Decimal(self._config.sizing.contracts_per_trade),
            maker_fee_multiplier=self._maker_fee_multiplier,
            has_resting_order=self._resting_order_id is not None,
            has_position=self._position is not None,
        )
        if decision.action is Action.REST:
            approval = self._risk.check_new_order(size=decision.size, price=decision.price, now=poll_ts)
            if approval.approved:
                self._resting_order_id = await self._backend.place_resting_order(decision.side, decision.price, decision.size)
                self._risk.record_order_opened(size=decision.size, price=decision.price, now=poll_ts)
        elif decision.action is Action.CANCEL and self._resting_order_id is not None:
            await self._cancel_resting()

    async def on_settlement(self, market: Market) -> None:
        pending = self._pending_settlements.get(market.ticker)
        if pending is None:
            return
        result = market.raw.get("result") or None
        if result not in ("yes", "no"):
            return  # defensive: a "finalized" market should always carry one of these; leave it pending
            # rather than popping and silently dropping a real position -- shutdown() reports it unresolved.
        del self._pending_settlements[market.ticker]
        self._resolve(pending, result, market.close_time)

    async def shutdown(self, ts: datetime) -> None:
        """Call once after the recorder loop stops. Cancels any resting order (spec section 5: on exit,
        cancel open orders) and reports the last window's position unresolved if settlement never arrived,
        rather than guessing at an outcome."""
        if self._resting_order_id is not None:
            await self._cancel_resting()
        if self._position is not None and self._current_ticker is not None:
            self._pending_settlements[self._current_ticker] = self._position
            self._position = None
        for pending in self._pending_settlements.values():
            self.trades.append(pending)
        self._pending_settlements.clear()

    def report(self) -> BacktestReport:
        return build_report(
            self.trades,
            windows_seen=len(self._tickers_seen),
            windows_traded=self.windows_traded,
            first_ts=self.first_ts,
            last_ts=self.last_ts,
            queue_assumption=self._queue_assumption,
            maker_fee_multiplier=self._maker_fee_multiplier,
        )

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    # ---- internals

    def _apply_fill(self, ticker: str, fill: Fill, poll_ts: datetime, p_blend: float) -> None:
        if self._position is None:
            self._position = TradeRecord(
                ticker=ticker,
                side=fill.side,
                size=fill.size,
                entry_price=fill.price,
                entry_ts=poll_ts,
                fee_paid=fill.fee,
                p_side_at_entry=p_blend if fill.side == "yes" else 1.0 - p_blend,
            )
        else:
            total_size = self._position.size + fill.size
            avg_price = (self._position.entry_price * self._position.size + fill.price * fill.size) / total_size
            self._position = replace(self._position, size=total_size, entry_price=avg_price, fee_paid=self._position.fee_paid + fill.fee)

    async def _cancel_resting(self) -> None:
        order = self._broker.get_order(self._resting_order_id)
        unfilled = order.remaining_size
        await self._backend.cancel_order(self._resting_order_id)
        if unfilled > 0:
            self._risk.release_exposure(unfilled * order.price)
        self._resting_order_id = None

    async def _roll_over(self, poll_ts: datetime) -> None:
        if self._resting_order_id is not None:
            await self._cancel_resting()
        if self._position is not None:
            self._pending_settlements[self._current_ticker] = self._position
            self._position = None

    def _resolve(self, pending: TradeRecord, result: str, settled_ts: datetime) -> None:
        exposure = pending.entry_price * pending.size
        payout = settle(pending.side, pending.size, result)
        pnl = payout - exposure - pending.fee_paid
        self.trades.append(replace(pending, result=result, pnl_usd=pnl))
        self._risk.record_trade_closed(
            TradeOutcome(ts=settled_ts, size=pending.size, pnl_usd=pnl), exposure_released_usd=exposure
        )
