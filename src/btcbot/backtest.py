"""Backtest: replay recorded data through the model, strategy, risk and paper broker (Phase 4), per
docs/btc15m-bot-spec.md section 8.4.

Replays recorder.py's SQLite tables in one global, chronological pass: order-book snapshots drive
strategy/risk/broker decisions window by window, while a single EWMA volatility estimate carries across
window boundaries from the spot-tick stream, exactly as it would in a live run. A window's still-open
resting order is cancelled and its still-open position is settled (or, with no settlement row yet, left
unresolved and excluded from PnL, not scored as a loss) the moment the next window's ticker appears.

Every report is a sensitivity result, not a claim: it names its queue assumption (see paper_broker.py's
module docstring for why one is needed) and its maker-fee-multiplier assumption, and always states its
sample size plainly, per CLAUDE.md's "no profitability claims without recorded out-of-sample results."
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from btcbot.model import EwmaVolatility, ModelState, log_return, predict
from btcbot.models import OrderBook, ParseError, PriceLevel, Side, parse_time
from btcbot.paper_broker import PaperBroker, QueueAssumption, settle
from btcbot.risk import RiskManager, TradeOutcome
from btcbot.strategy import Action, decide

if TYPE_CHECKING:
    from btcbot.config import BotConfig

_NO_KILL_FILE = ".btcbot-backtest-should-never-exist"  # a backtest replay has no live process to kill


class BacktestError(Exception):
    """The database has nothing usable to replay."""


# --------------------------------------------------------------------------- loading


def _book_from_json(ticker: str, raw: str) -> OrderBook:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ParseError(f"orderbook_snapshots.book_json for {ticker}: not valid JSON: {exc}") from exc

    def levels(key: str) -> tuple[PriceLevel, ...]:
        return tuple(PriceLevel(Decimal(p), Decimal(s)) for p, s in data.get(key, []))

    return OrderBook(ticker=ticker, yes_bids=levels("yes"), no_bids=levels("no"))


@dataclass(frozen=True, slots=True)
class Snapshot:
    ticker: str
    poll_ts: datetime
    book: OrderBook


def load_snapshots(conn: sqlite3.Connection) -> list[Snapshot]:
    rows = conn.execute("SELECT ticker, poll_ts, book_json FROM orderbook_snapshots ORDER BY poll_ts").fetchall()
    return [Snapshot(ticker, parse_time(poll_ts), _book_from_json(ticker, book_json)) for ticker, poll_ts, book_json in rows]


def load_windows(conn: sqlite3.Connection) -> dict[str, tuple[Decimal, datetime]]:
    """``ticker -> (strike, close_time)``, from the most recent ``market_state`` row that has a strike."""
    rows = conn.execute(
        "SELECT ticker, strike, close_time FROM market_state WHERE strike IS NOT NULL ORDER BY poll_ts"
    ).fetchall()
    return {ticker: (Decimal(strike), parse_time(close_time)) for ticker, strike, close_time in rows}


def load_settlements(conn: sqlite3.Connection) -> dict[str, Side]:
    rows = conn.execute("SELECT ticker, result FROM settlements WHERE result IS NOT NULL").fetchall()
    return dict(rows)


def load_spot_ticks(conn: sqlite3.Connection) -> list[tuple[datetime, Decimal]]:
    rows = conn.execute("SELECT receive_ts, price FROM spot_ticks ORDER BY receive_ts").fetchall()
    return [(parse_time(ts), Decimal(price)) for ts, price in rows]


# --------------------------------------------------------------------------- report


@dataclass(frozen=True, slots=True)
class TradeRecord:
    ticker: str
    side: Side
    size: Decimal
    entry_price: Decimal
    entry_ts: datetime
    fee_paid: Decimal
    p_side_at_entry: float
    result: Side | None = None
    pnl_usd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class BacktestReport:
    queue_assumption: str
    maker_fee_multiplier: Decimal
    windows_seen: int
    windows_traded: int
    trades: int
    wins: int
    losses: int
    unresolved: int
    win_rate: float | None
    total_pnl_usd: Decimal
    total_fees_usd: Decimal
    max_drawdown_usd: Decimal
    trades_per_day: float | None
    avg_p_side_at_entry: float | None
    beats_trade_nothing: bool | None
    sample_size_note: str


# --------------------------------------------------------------------------- replay


def run_backtest(
    conn: sqlite3.Connection,
    config: BotConfig,
    *,
    queue_assumption: QueueAssumption = QueueAssumption.OPTIMISTIC,
    maker_fee_multiplier: Decimal = Decimal("0"),
) -> BacktestReport:
    snapshots = load_snapshots(conn)
    if not snapshots:
        raise BacktestError("no order-book snapshots to replay")
    windows = load_windows(conn)
    settlements = load_settlements(conn)
    spot_ticks = load_spot_ticks(conn)

    broker = PaperBroker(maker_fee_multiplier=maker_fee_multiplier, queue_assumption=queue_assumption)
    risk = RiskManager(config.risk, kill_file=_NO_KILL_FILE, clock=lambda: snapshots[0].poll_ts)
    vol = EwmaVolatility(config.vol_window_sec)

    spot_idx = 0
    last_spot_price: Decimal | None = None
    current_ticker: str | None = None
    resting_order_id: str | None = None
    position: TradeRecord | None = None
    trades: list[TradeRecord] = []
    windows_traded: set[str] = set()

    def finalize_window(ticker: str, ts: datetime) -> None:
        nonlocal resting_order_id, position
        if resting_order_id is not None:
            order = broker.get_order(resting_order_id)
            unfilled = order.remaining_size
            if not order.is_done:
                broker.cancel(resting_order_id, ts=ts)
            if unfilled > 0:
                risk.release_exposure(unfilled * order.price)
            resting_order_id = None
        if position is not None:
            result = settlements.get(ticker)
            exposure = position.entry_price * position.size
            if result is None:
                trades.append(position)
                risk.release_exposure(exposure)
            else:
                payout = settle(position.side, position.size, result)
                pnl = payout - exposure - position.fee_paid
                trades.append(replace(position, result=result, pnl_usd=pnl))
                risk.record_trade_closed(
                    TradeOutcome(ts=ts, size=position.size, pnl_usd=pnl), exposure_released_usd=exposure
                )
            position = None

    for snap in snapshots:
        while spot_idx < len(spot_ticks) and spot_ticks[spot_idx][0] <= snap.poll_ts:
            ts, price = spot_ticks[spot_idx]
            if last_spot_price is not None:
                vol.update(log_return(last_spot_price, price))
            last_spot_price = price
            spot_idx += 1
        if last_spot_price is None:
            continue  # no spot data yet: cannot price anything

        if snap.ticker != current_ticker:
            if current_ticker is not None:
                finalize_window(current_ticker, snap.poll_ts)
            current_ticker = snap.ticker

        if snap.ticker not in windows:
            continue  # strike not yet known for this ticker
        strike, close_time = windows[snap.ticker]
        tau_sec = (close_time - snap.poll_ts).total_seconds()
        if tau_sec < 0:
            continue  # a stray snapshot polled after close

        state = ModelState(
            spot=last_spot_price, strike=strike, tau_sec=tau_sec, sigma=vol.sigma, market_mid=snap.book.mid("yes")
        )
        _, p_blend = predict(state, blend=float(config.model_blend))

        if resting_order_id is not None:
            for fill in broker.on_book_update(snap.book, ts=snap.poll_ts):
                windows_traded.add(snap.ticker)
                if position is None:
                    position = TradeRecord(
                        ticker=snap.ticker,
                        side=fill.side,
                        size=fill.size,
                        entry_price=fill.price,
                        entry_ts=snap.poll_ts,
                        fee_paid=fill.fee,
                        p_side_at_entry=p_blend if fill.side == "yes" else 1.0 - p_blend,
                    )
                else:
                    total_size = position.size + fill.size
                    avg_price = (position.entry_price * position.size + fill.price * fill.size) / total_size
                    position = replace(position, size=total_size, entry_price=avg_price, fee_paid=position.fee_paid + fill.fee)
            if broker.get_order(resting_order_id).status == "filled":
                resting_order_id = None

        decision = decide(
            book=snap.book,
            tau_sec=tau_sec,
            p_yes=p_blend,
            spot_is_stale=False,
            min_edge=config.min_edge,
            min_depth=config.min_depth,
            max_spread=config.max_spread,
            min_tau_sec=config.min_tau_sec,
            max_tau_sec=config.max_tau_sec,
            cancel_before_close_sec=config.cancel_before_close_sec,
            contracts_per_trade=Decimal(config.sizing.contracts_per_trade),
            maker_fee_multiplier=maker_fee_multiplier,
            has_resting_order=resting_order_id is not None,
            has_position=position is not None,
        )

        if decision.action is Action.REST:
            approval = risk.check_new_order(size=decision.size, price=decision.price, now=snap.poll_ts)
            if approval.approved:
                resting_order_id = broker.place_resting_order(
                    decision.side, decision.price, decision.size, ts=snap.poll_ts, book=snap.book
                )
                risk.record_order_opened(size=decision.size, price=decision.price, now=snap.poll_ts)
        elif decision.action is Action.CANCEL and resting_order_id is not None:
            order = broker.get_order(resting_order_id)
            unfilled = order.remaining_size
            broker.cancel(resting_order_id, ts=snap.poll_ts)
            if unfilled > 0:
                risk.release_exposure(unfilled * order.price)
            resting_order_id = None

    if current_ticker is not None:
        finalize_window(current_ticker, snapshots[-1].poll_ts)

    return _build_report(
        trades,
        windows_seen=len({s.ticker for s in snapshots}),
        windows_traded=windows_traded,
        snapshots=snapshots,
        queue_assumption=queue_assumption,
        maker_fee_multiplier=maker_fee_multiplier,
    )


def _build_report(
    trades: list[TradeRecord],
    *,
    windows_seen: int,
    windows_traded: set[str],
    snapshots: list[Snapshot],
    queue_assumption: QueueAssumption,
    maker_fee_multiplier: Decimal,
) -> BacktestReport:
    resolved = [t for t in trades if t.result is not None]
    unresolved = [t for t in trades if t.result is None]
    wins = sum(1 for t in resolved if t.pnl_usd is not None and t.pnl_usd > 0)
    losses = sum(1 for t in resolved if t.pnl_usd is not None and t.pnl_usd < 0)
    total_pnl = sum((t.pnl_usd for t in resolved if t.pnl_usd is not None), Decimal(0))
    total_fees = sum((t.fee_paid for t in trades), Decimal(0))

    running = Decimal(0)
    peak = Decimal(0)
    max_dd = Decimal(0)
    for trade in sorted(resolved, key=lambda t: t.entry_ts):
        if trade.pnl_usd is None:
            continue
        running += trade.pnl_usd
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)

    span_days = None
    if len(snapshots) >= 2:
        span_seconds = (snapshots[-1].poll_ts - snapshots[0].poll_ts).total_seconds()
        if span_seconds > 0:
            span_days = span_seconds / 86400
    trades_per_day = (len(trades) / span_days) if span_days else None
    win_rate = (wins / len(resolved)) if resolved else None
    avg_p_side = (sum(t.p_side_at_entry for t in trades) / len(trades)) if trades else None

    note = (
        f"{len(trades)} trades across {len(windows_traded)} of {windows_seen} windows seen "
        f"({len(resolved)} resolved, {len(unresolved)} unresolved). "
    )
    note += (
        "Far too small a sample to draw any conclusion, let alone a profitability claim."
        if len(resolved) < 30
        else "Still label any conclusion with this sample size; it is not an out-of-sample guarantee."
    )

    return BacktestReport(
        queue_assumption=queue_assumption.value,
        maker_fee_multiplier=maker_fee_multiplier,
        windows_seen=windows_seen,
        windows_traded=len(windows_traded),
        trades=len(trades),
        wins=wins,
        losses=losses,
        unresolved=len(unresolved),
        win_rate=win_rate,
        total_pnl_usd=total_pnl,
        total_fees_usd=total_fees,
        max_drawdown_usd=max_dd,
        trades_per_day=trades_per_day,
        avg_p_side_at_entry=avg_p_side,
        beats_trade_nothing=(total_pnl > 0) if resolved else None,
        sample_size_note=note,
    )
