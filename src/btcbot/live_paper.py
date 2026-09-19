"""Live paper trading loop (Phase 5), per docs/btc15m-bot-spec.md section 8.5.

Runs the strategy, risk and paper broker against data as it is polled, sharing every building block
backtest.py replays offline: :mod:`btcbot.model` for predictions, :mod:`btcbot.strategy` for decisions,
:mod:`btcbot.risk` for limits, :mod:`btcbot.execution`'s ``PaperExecutionBackend`` for fills. Every
prediction is logged (``btcbot.model.log_prediction``) and every trade is recorded in the same shape
backtest.py uses (``TradeRecord``, ``build_report``) *and* persisted to this run's database as it happens
(``btcbot.backtest.log_trade``, into the ``trades`` table ``init_trades_schema`` creates), so a second
process reading the same file -- ``btcbot backtest --db <this run's database>`` for a replay comparison
(spec section 8.5), or ``btcbot dashboard`` for a live view -- sees trades as they resolve, not just at the
end of the run.

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

from btcbot.backtest import BacktestReport, TradeRecord, build_report, init_trades_schema, log_trade
from btcbot.config import SizingMode
from btcbot.execution import PaperExecutionBackend
from btcbot.model import TimedVolatility, ModelState, Prediction, init_predictions_schema, log_prediction, predict
from btcbot.models import Market, OrderBook
from btcbot.paper_broker import Fill, PaperBroker, QueueAssumption, settle
from btcbot.risk import RiskManager, TradeOutcome
from btcbot.strategy import Action, Decision, decide, kelly_size, percent_size

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
        init_trades_schema(conn)
        self._conn = conn
        self._config = config
        self._spot_buffer = spot_buffer
        self._queue_assumption = queue_assumption
        self._maker_fee_multiplier = maker_fee_multiplier
        self._broker = PaperBroker(maker_fee_multiplier=maker_fee_multiplier, queue_assumption=queue_assumption)
        self._backend = PaperExecutionBackend(self._broker)
        self._risk = RiskManager(config.risk, kill_file=kill_file, clock=lambda: self.last_ts or datetime.now(timezone.utc))
        self._vol = TimedVolatility(config.vol_window_sec)

        self._current_ticker: str | None = None
        self._resting_order_id: str | None = None
        self._position: TradeRecord | None = None
        self._pending_settlements: dict[str, TradeRecord] = {}

        self.trades: list[TradeRecord] = []
        self.windows_traded: set[str] = set()
        # sizing mode "percent": the account grows and shrinks only with SETTLED results (see strategy.percent_size)
        self._bankroll: Decimal = config.sizing.account_usd
        self._last_order_size: Decimal | None = None
        self._last_result: str | None = None  # "win" or "loss" of the most recent settled trade
        if config.sizing.mode is SizingMode.PERCENT:
            self._risk.set_account_value(self._bankroll)
        self.first_ts: datetime | None = None
        self.last_ts: datetime | None = None
        self._tickers_seen: set[str] = set()

    @property
    def risk(self) -> RiskManager:
        return self._risk

    @property
    def bankroll(self) -> Decimal:
        """Starting account plus settled profit and loss (sizing mode "percent" grows bets from this)."""
        return self._bankroll

    @property
    def unresolved_count(self) -> int:
        return len(self._pending_settlements)

    def on_spot_tick(self, price: Decimal, ts: datetime) -> None:
        self._vol.update(price, ts)

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
        if tau_sec <= 0:
            return

        state = ModelState(spot=spot, strike=market.strike, tau_sec=tau_sec, sigma=self._vol.sigma, market_mid=book.mid("yes"))
        p_model, p_blend = predict(state, blend=float(self._config.model_blend))
        pricing_ready = self._vol.ready and not self._spot_buffer.is_stale() and tau_sec > 60
        if pricing_ready:
            log_prediction(
                self._conn, Prediction(market.ticker, poll_ts, state, float(self._config.model_blend), p_model, p_blend)
            )

        for fill in await self._sync_fills(book, poll_ts):
            self.windows_traded.add(market.ticker)
            self._apply_fill(market.ticker, fill, poll_ts, p_blend)
        if self._resting_order_id is not None and self._resting_order_filled():
            self._resting_order_id = None

        decision = decide(
            book=book,
            tau_sec=tau_sec,
            p_yes=p_blend,
            # No BRTI partial average is supplied yet: do not open new positions
            # in the final minute using the model's spot fallback as an average.
            spot_is_stale=not pricing_ready,
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
            min_price=self._config.min_price,
            max_price=self._config.max_price,
        )
        if decision.action is Action.REST and decision.kelly_fraction is not None and self._config.sizing.mode is SizingMode.KELLY:
            size = kelly_size(
                decision.kelly_fraction, decision.price,
                bankroll_usd=self._config.risk.max_open_exposure_usd,
                multiplier=self._config.sizing.kelly_fraction_multiplier,
                max_contracts=Decimal(self._config.risk.max_contracts_per_trade),
            )
            decision = replace(decision, size=size)
        elif decision.action is Action.REST and self._config.sizing.mode is SizingMode.PERCENT:
            size = percent_size(
                decision.price, cash_usd=self._bankroll - self._risk.open_exposure_usd,
                risk_pct=self._config.sizing.risk_pct_per_trade, previous_size=self._last_order_size,
                last_result=self._last_result, max_growth_pct=self._config.sizing.max_growth_per_win_pct,
                max_contracts=Decimal(self._config.risk.max_contracts_per_trade),
            )
            if size >= 1:
                decision = replace(decision, size=size)
            else:
                decision = Decision(Action.SKIP, reason="account too small for one contract at this risk percent")
        if decision.action is Action.REST:
            approval = self._risk.check_new_order(size=decision.size, price=decision.price, now=poll_ts)
            if approval.approved:
                order_id = await self._place_resting(decision, poll_ts)
                if order_id is not None:  # None: the exchange rejected it, so nothing rests and no exposure is taken
                    self._resting_order_id = order_id
                    self._last_order_size = decision.size
                    self._risk.record_order_opened(size=decision.size, price=decision.price, now=poll_ts)
        elif decision.action is Action.CANCEL and self._resting_order_id is not None:
            await self._cancel_resting()

    async def on_settlement(self, market: Market) -> None:
        result = market.raw.get("result") or None
        pending = self._pending_settlements.get(market.ticker)
        if pending is None:
            # Settlement can arrive BEFORE this trader has seen the next window's first snapshot: a 10-25 s gap
            # between windows is normal, and Kalshi often finalizes within ~10 s. The position is then still the
            # current one, not yet "pending", and used to be ignored here -- never resolved, never scored, and
            # its exposure never released (so after a few such windows the risk cap would block all new orders).
            position = self._position
            if position is not None and position.ticker == market.ticker and result in ("yes", "no"):
                self._position = None
                if self._resting_order_id is not None:
                    await self._cancel_resting()  # the market is closed: the unfilled remainder is dead
                self._resolve(position, result, market.close_time)
            return
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
            log_trade(self._conn, pending)
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

    # ---- execution hooks: the paper trader's versions here; ``btcbot.demo_trader.DemoTrader`` overrides them to
    # ---- place real demo-environment orders while every decision above stays exactly the same code.

    async def _sync_fills(self, book: OrderBook, poll_ts: datetime) -> list[Fill]:
        return self._backend.sync_market(book, poll_ts)

    def _resting_order_filled(self) -> bool:
        return self._broker.get_order(self._resting_order_id).status == "filled"

    async def _place_resting(self, decision: Decision, poll_ts: datetime) -> str | None:
        return await self._backend.place_resting_order(decision.side, decision.price, decision.size)

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
        resolved = replace(pending, result=result, pnl_usd=pnl)
        self.trades.append(resolved)
        log_trade(self._conn, resolved)
        self._bankroll += pnl
        self._last_result = "loss" if pnl < 0 else "win"
        if self._config.sizing.mode is SizingMode.PERCENT:
            self._risk.set_account_value(self._bankroll)
        self._risk.record_trade_closed(
            TradeOutcome(ts=settled_ts, size=pending.size, pnl_usd=pnl), exposure_released_usd=exposure
        )
