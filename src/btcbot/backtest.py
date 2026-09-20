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
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from typing import TYPE_CHECKING

from btcbot.model import TimedVolatility, ModelState, predict
from btcbot.models import OrderBook, ParseError, PriceLevel, Side, parse_time
from btcbot.paper_broker import PaperBroker, QueueAssumption, settle
from btcbot.risk import RiskManager, TradeOutcome
from btcbot.strategy import Action, Decision, decide, percent_size

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


@dataclass(frozen=True, slots=True)
class Settlement:
    result: Side
    available_ts: datetime


def load_settlements(conn: sqlite3.Connection) -> dict[str, Settlement]:
    """Load outcomes together with when the recorder first learned them."""
    rows = conn.execute(
        "SELECT ticker, result, finalized_poll_ts FROM settlements WHERE result IN ('yes','no')"
    ).fetchall()
    return {ticker: Settlement(result, parse_time(available_ts)) for ticker, result, available_ts in rows}


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


# --------------------------------------------------------------------------- trade logging

TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    size TEXT NOT NULL,
    entry_price TEXT NOT NULL,
    entry_ts TEXT NOT NULL,
    fee_paid TEXT NOT NULL,
    p_side_at_entry REAL NOT NULL,
    result TEXT,
    pnl_usd TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_entry_ts ON trades (entry_ts);
"""


def init_trades_schema(conn: sqlite3.Connection) -> None:
    """Lets a live run's trades (:mod:`btcbot.live_paper`) be read from a second connection while the run
    is still going -- e.g. the dashboard's live-monitor view -- the same pattern
    :func:`btcbot.model.init_predictions_schema` uses for predictions."""
    conn.executescript(TRADES_SCHEMA)
    conn.commit()


def log_trade(conn: sqlite3.Connection, trade: TradeRecord) -> None:
    conn.execute(
        """INSERT INTO trades (ticker, side, size, entry_price, entry_ts, fee_paid, p_side_at_entry, result, pnl_usd)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            trade.ticker,
            trade.side,
            str(trade.size),
            str(trade.entry_price),
            trade.entry_ts.astimezone(timezone.utc).isoformat(),
            str(trade.fee_paid),
            trade.p_side_at_entry,
            trade.result,
            None if trade.pnl_usd is None else str(trade.pnl_usd),
        ),
    )
    conn.commit()


def load_trades(conn: sqlite3.Connection) -> list[TradeRecord]:
    rows = conn.execute(
        """SELECT ticker, side, size, entry_price, entry_ts, fee_paid, p_side_at_entry, result, pnl_usd
           FROM trades ORDER BY entry_ts"""
    ).fetchall()
    return [
        TradeRecord(
            ticker=ticker,
            side=side,
            size=Decimal(size),
            entry_price=Decimal(entry_price),
            entry_ts=parse_time(entry_ts),
            fee_paid=Decimal(fee_paid),
            p_side_at_entry=p_side_at_entry,
            result=result,
            pnl_usd=None if pnl_usd is None else Decimal(pnl_usd),
        )
        for ticker, side, size, entry_price, entry_ts, fee_paid, p_side_at_entry, result, pnl_usd in rows
    ]


# --------------------------------------------------------------------------- replay


# --------------------------------------------------------------------------- replay data and steps


@dataclass(frozen=True, slots=True)
class ReplayData:
    """Everything a replay reads, loaded once so a parameter sweep does not re-parse JSON per combination."""

    snapshots: list[Snapshot]
    windows: dict[str, tuple[Decimal, datetime]]
    settlements: dict[str, Settlement]
    spot_ticks: list[tuple[datetime, Decimal]]


def load_replay_data(conn: sqlite3.Connection) -> ReplayData:
    return ReplayData(load_snapshots(conn), load_windows(conn), load_settlements(conn), load_spot_ticks(conn))


def merge_replay_data(parts: Sequence[ReplayData]) -> ReplayData:
    """Combine several recordings into one chronological replay.

    Two recorders running at once both capture the same window; replaying both would double-count it and
    interleave two different book histories. So a ticker present in several parts is taken whole from the
    part that captured the most snapshots of it, and a later part's spot ticks are used only outside the
    time span an earlier part already covers."""
    if not parts:
        raise BacktestError("no data to merge")
    best_part: dict[str, int] = {}
    counts: dict[str, int] = {}
    for i, part in enumerate(parts):
        per_ticker: dict[str, int] = {}
        for snap in part.snapshots:
            per_ticker[snap.ticker] = per_ticker.get(snap.ticker, 0) + 1
        for ticker, n in per_ticker.items():
            if n > counts.get(ticker, 0):
                counts[ticker], best_part[ticker] = n, i
    snapshots = [s for i, part in enumerate(parts) for s in part.snapshots if best_part[s.ticker] == i]
    snapshots.sort(key=lambda s: s.poll_ts)

    windows: dict[str, tuple[Decimal, datetime]] = {}
    settlements: dict[str, Settlement] = {}
    for part in parts:
        windows.update(part.windows)
        for ticker, settlement in part.settlements.items():
            previous = settlements.get(ticker)
            if previous is None or settlement.available_ts < previous.available_ts:
                settlements[ticker] = settlement

    spot: list[tuple[datetime, Decimal]] = []
    covered: list[tuple[datetime, datetime]] = []
    for part in sorted(parts, key=lambda p: len(p.spot_ticks), reverse=True):
        if not part.spot_ticks:
            continue
        spot.extend(t for t in part.spot_ticks if not any(a <= t[0] <= b for a, b in covered))
        covered.append((part.spot_ticks[0][0], part.spot_ticks[-1][0]))
    spot.sort(key=lambda t: t[0])
    return ReplayData(snapshots, windows, settlements, spot)


@dataclass(frozen=True, slots=True)
class Step:
    """One order-book snapshot with the model's view of it already computed (it does not depend on any
    strategy parameter except ``model_blend``, so a sweep prepares steps once per blend value)."""

    snap: Snapshot
    active: bool  # False: strike unknown or already past close; only the window change is processed
    tau_sec: float
    p_yes: float
    stale: bool
    spot: Decimal | None


@dataclass(frozen=True, slots=True)
class PreparedReplay:
    steps: list[Step]
    windows_seen: int
    first_ts: datetime
    last_ts: datetime
    settlements: dict[str, Settlement]
    spot_series: SpotSeries


class SpotSeries:
    """Spot prices by time, for the trend filter's "how far has spot moved over the last N seconds"."""

    def __init__(self, ticks: Sequence[tuple[datetime, Decimal]]) -> None:
        self._t = [ts.timestamp() for ts, _ in ticks]
        self._p = [price for _, price in ticks]

    def move(self, ts: datetime, lookback_sec: float) -> Decimal | None:
        """Price at ``ts`` minus price ``lookback_sec`` earlier, or None without enough history (the older
        reference tick must be within 5 s of the lookback point, so a gap in the feed yields no signal
        rather than a stale one)."""
        now = ts.timestamp()
        hi = bisect_right(self._t, now) - 1
        lo = bisect_right(self._t, now - lookback_sec) - 1
        if hi < 0 or lo < 0 or now - self._t[hi] > 5 or (now - lookback_sec) - self._t[lo] > 5:
            return None
        return self._p[hi] - self._p[lo]


def prepare_replay(
    data: ReplayData, config: BotConfig, *, tickers: set[str] | None = None, model_blend: float | None = None
) -> PreparedReplay:
    """Walk the snapshots once, carrying volatility across windows, and record what the model says at each.
    ``tickers`` restricts which windows are replayed (a train/test split) while every spot tick still feeds
    the volatility estimate, exactly as it would have live."""
    snapshots = [s for s in data.snapshots if tickers is None or s.ticker in tickers]
    if not snapshots:
        raise BacktestError("no order-book snapshots to replay")
    blend = float(config.model_blend if model_blend is None else model_blend)
    vol = TimedVolatility(config.vol_window_sec)
    spot_ticks = data.spot_ticks
    spot_idx = 0
    last_price: Decimal | None = None
    last_ts: datetime | None = None
    steps: list[Step] = []
    for snap in snapshots:
        while spot_idx < len(spot_ticks) and spot_ticks[spot_idx][0] <= snap.poll_ts:
            ts, price = spot_ticks[spot_idx]
            vol.update(price, ts)
            last_price, last_ts = price, ts
            spot_idx += 1
        if last_price is None or last_ts is None:
            continue  # no spot data yet: cannot price anything
        window = data.windows.get(snap.ticker)
        tau_sec = (window[1] - snap.poll_ts).total_seconds() if window else 0.0
        if window is None or tau_sec <= 0:
            steps.append(Step(snap, False, tau_sec, 0.5, True, last_price))
            continue
        state = ModelState(
            spot=last_price, strike=window[0], tau_sec=tau_sec, sigma=vol.sigma, market_mid=snap.book.mid("yes")
        )
        _, p_blend = predict(state, blend=blend)
        stale = (snap.poll_ts - last_ts).total_seconds() > 3 or not vol.ready or tau_sec <= 60
        steps.append(Step(snap, True, tau_sec, p_blend, stale, last_price))
    return PreparedReplay(
        steps=steps,
        windows_seen=len({s.ticker for s in snapshots}),
        first_ts=snapshots[0].poll_ts,
        last_ts=snapshots[-1].poll_ts,
        settlements={ticker: value for ticker, value in data.settlements.items()
                     if tickers is None or ticker in tickers},
        spot_series=SpotSeries(spot_ticks),
    )


# --------------------------------------------------------------------------- optional entry filters (the lab)


@dataclass(frozen=True, slots=True)
class EntryFilters:
    """Extra conditions layered on top of :func:`btcbot.strategy.decide`. All off by default, in which case a
    replay is exactly the plain backtest.

    ``trend_mode``: ``"with"`` only takes a side spot has been moving toward over ``trend_lookback_sec``
    (at least ``trend_min_move_usd``), ``"against"`` only the side it has been moving away from (a
    mean-reversion bet). ``account_usd`` + ``risk_pct_per_trade`` size each order as that fraction of the
    current bankroll (starting account plus settled PnL, less capital already at risk), rounded down to
    whole contracts; sizing never depends on a prior loss being "made back".

    ``persist_steps``: only enter once the strategy has wanted the SAME side for this many consecutive snapshots
    (about one per second), so a single-quote flicker cannot trigger an entry. ``min_p_side``: only enter a side
    the blended model puts at least this likely to win, so a cheap bet against the favourite is refused."""

    min_price: Decimal | None = None
    max_price: Decimal | None = None
    persist_steps: int = 1
    min_p_side: Decimal | None = None
    trend_mode: str = "off"
    trend_lookback_sec: int = 60
    trend_min_move_usd: Decimal = Decimal(0)
    book_move_mode: str = "off"
    book_move_lookback_sec: int = 60
    book_move_min: Decimal = Decimal(0)
    account_usd: Decimal | None = None
    risk_pct_per_trade: Decimal | None = None
    min_stake_usd: Decimal = Decimal(0)
    max_growth_pct: Decimal | None = None  # after a win the next order may grow by at most this percent (None: no ramp)


@dataclass(frozen=True, slots=True)
class ReplayResult:
    trades: list[TradeRecord]
    windows_traded: set[str]
    windows_seen: int
    first_ts: datetime
    last_ts: datetime
    filter_counts: dict[str, int]
    final_bankroll: Decimal | None
    equity_curve: list[tuple[datetime, Decimal]]


def replay_prepared(
    prepared: PreparedReplay,
    config: BotConfig,
    *,
    queue_assumption: QueueAssumption = QueueAssumption.OPTIMISTIC,
    maker_fee_multiplier: Decimal = Decimal("0"),
    filters: EntryFilters | None = None,
) -> ReplayResult:
    steps, settlements = prepared.steps, prepared.settlements
    broker = PaperBroker(maker_fee_multiplier=maker_fee_multiplier, queue_assumption=queue_assumption)
    risk = RiskManager(config.risk, kill_file=_NO_KILL_FILE, clock=lambda: prepared.first_ts)

    current_ticker: str | None = None
    resting_order_id: str | None = None
    position: TradeRecord | None = None
    pending: dict[str, TradeRecord] = {}
    trades: list[TradeRecord] = []
    windows_traded: set[str] = set()
    counts = {
        "price_band": 0, "trend": 0, "trend_missing_history": 0, "too_small": 0, "risk_blocked": 0,
        "persistence": 0, "low_confidence": 0, "book_move": 0, "book_move_missing_history": 0,
    }
    rest_side: str | None = None  # the side the strategy has wanted on consecutive snapshots, and for how long
    rest_streak = 0
    bankroll = filters.account_usd if filters is not None else None
    equity: list[tuple[datetime, Decimal]] = []
    last_order_size: Decimal | None = None  # the ordered size of the most recent order (for the growth ramp)
    last_result: str | None = None  # "win" / "loss" of the most recent settled trade
    if bankroll is not None and filters.risk_pct_per_trade is not None:
        risk.set_account_value(bankroll)

    def resolve_available(now: datetime) -> None:
        nonlocal bankroll, last_result
        available = sorted(
            ((ticker, settlements.get(ticker)) for ticker in pending),
            key=lambda item: item[1].available_ts if item[1] is not None else now,
        )
        for ticker, settlement in available:
            if settlement is None or settlement.available_ts > now:
                continue
            held = pending.pop(ticker)
            exposure = held.entry_price * held.size
            pnl = settle(held.side, held.size, settlement.result) - exposure - held.fee_paid
            trades.append(replace(held, result=settlement.result, pnl_usd=pnl))
            risk.record_trade_closed(
                TradeOutcome(ts=settlement.available_ts, size=held.size, pnl_usd=pnl),
                exposure_released_usd=exposure,
            )
            last_result = "loss" if pnl < 0 else "win"
            if bankroll is not None:
                bankroll += pnl
                equity.append((settlement.available_ts, bankroll))
                if filters is not None and filters.risk_pct_per_trade is not None:
                    risk.set_account_value(bankroll)

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
            pending[ticker] = position
            position = None

    book_times: list[float] = []
    book_mids: list[Decimal] = []

    def book_move(ts: datetime, lookback_sec: int) -> Decimal | None:
        if not book_times:
            return None
        now = ts.timestamp()
        hi = bisect_right(book_times, now) - 1
        lo = bisect_right(book_times, now - lookback_sec) - 1
        if hi < 0 or lo < 0 or now - book_times[hi] > 5 or (now - lookback_sec) - book_times[lo] > 5:
            return None
        return book_mids[hi] - book_mids[lo]

    def screen(decision: Decision, ts: datetime, p_yes: float, streak: int) -> Decision | None:
        """Apply the lab's filters and sizing to a proposed resting order; None means do not place it."""
        if filters is None:
            return decision
        if streak < filters.persist_steps:
            counts["persistence"] += 1
            return None
        if filters.min_p_side is not None:
            p_side = p_yes if decision.side == "yes" else 1.0 - p_yes
            if p_side < float(filters.min_p_side):
                counts["low_confidence"] += 1
                return None
        price = decision.price
        if (filters.min_price is not None and price < filters.min_price) or (
            filters.max_price is not None and price > filters.max_price
        ):
            counts["price_band"] += 1
            return None
        if filters.trend_mode != "off":
            lookbacks = (900, 1800, 3600, 86400) if filters.trend_mode == "aligned4" else (filters.trend_lookback_sec,)
            moves = [prepared.spot_series.move(ts, lookback) for lookback in lookbacks]
            if any(move is None for move in moves):
                counts["trend_missing_history"] += 1
            toward_yes = all(move is not None and move > 0 and move >= filters.trend_min_move_usd for move in moves)
            toward_no = all(move is not None and move < 0 and move <= -filters.trend_min_move_usd for move in moves)
            wanted = toward_yes if decision.side == "yes" else toward_no
            if filters.trend_mode == "against":
                wanted = toward_no if decision.side == "yes" else toward_yes
            if not wanted:
                counts["trend"] += 1
                return None
        if filters.book_move_mode != "off":
            move = book_move(ts, filters.book_move_lookback_sec)
            if move is None:
                counts["book_move_missing_history"] += 1
                return None
            toward_yes = move >= filters.book_move_min
            toward_no = move <= -filters.book_move_min
            wanted = toward_yes if decision.side == "yes" else toward_no
            if filters.book_move_mode == "against":
                wanted = toward_no if decision.side == "yes" else toward_yes
            if not wanted:
                counts["book_move"] += 1
                return None
        if bankroll is not None:
            cash = bankroll - risk.open_exposure_usd
            if filters.risk_pct_per_trade is not None:
                contracts = int(percent_size(
                    price, cash_usd=max(cash, Decimal(0)), risk_pct=filters.risk_pct_per_trade * 100,
                    previous_size=last_order_size, last_result=last_result, max_growth_pct=filters.max_growth_pct,
                    max_contracts=Decimal(config.risk.max_contracts_per_trade),
                ))
            else:
                contracts = int(decision.size)
            # A minimum order premium (a floor, applied after the percent rule; the risk gates still apply on top).
            minimum = int((filters.min_stake_usd / price).to_integral_value(rounding=ROUND_CEILING))
            contracts = max(contracts, minimum)
            contracts = min(contracts, config.risk.max_contracts_per_trade)
            # Reserve a conservative per-contract fee buffer for possible partial fills.
            fee_buffer = Decimal("0.07") * price * (1 - price) * maker_fee_multiplier + Decimal("0.01")
            if contracts < max(1, minimum) or Decimal(contracts) * (price + fee_buffer) > cash:
                counts["too_small"] += 1
                return None
            return replace(decision, size=Decimal(contracts))
        return decision

    for step in steps:
        snap = step.snap
        if snap.ticker != current_ticker:
            if current_ticker is not None:
                finalize_window(current_ticker, snap.poll_ts)
            current_ticker = snap.ticker
            rest_side, rest_streak = None, 0  # a new window starts a new streak
            book_times.clear()
            book_mids.clear()
        resolve_available(snap.poll_ts)
        yes_mid = snap.book.mid("yes")
        if yes_mid is not None:
            book_times.append(snap.poll_ts.timestamp())
            book_mids.append(yes_mid)
        if not step.active:
            continue
        p_blend = step.p_yes

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
            tau_sec=step.tau_sec,
            p_yes=p_blend,
            spot_is_stale=step.stale,
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
            rest_streak = rest_streak + 1 if rest_side == decision.side else 1
            rest_side = decision.side
        else:
            rest_side, rest_streak = None, 0

        if decision.action is Action.REST:
            screened = screen(decision, snap.poll_ts, p_blend, rest_streak)
            if screened is not None:
                approval = risk.check_new_order(size=screened.size, price=screened.price, now=snap.poll_ts)
                if approval.approved:
                    last_order_size = screened.size
                    resting_order_id = broker.place_resting_order(
                        screened.side, screened.price, screened.size, ts=snap.poll_ts, book=snap.book
                    )
                    risk.record_order_opened(size=screened.size, price=screened.price, now=snap.poll_ts)
                else:
                    counts["risk_blocked"] += 1
        elif decision.action is Action.CANCEL and resting_order_id is not None:
            order = broker.get_order(resting_order_id)
            unfilled = order.remaining_size
            broker.cancel(resting_order_id, ts=snap.poll_ts)
            if unfilled > 0:
                risk.release_exposure(unfilled * order.price)
            resting_order_id = None

    if current_ticker is not None:
        finalize_window(current_ticker, prepared.last_ts)
    relevant_announcements = [
        settlements[ticker].available_ts for ticker in pending if ticker in settlements
    ]
    report_end = max([prepared.last_ts, *relevant_announcements])
    resolve_available(report_end)
    trades.extend(pending.values())

    return ReplayResult(
        trades=trades,
        windows_traded=windows_traded,
        windows_seen=prepared.windows_seen,
        first_ts=prepared.first_ts,
        last_ts=report_end,
        filter_counts=counts,
        final_bankroll=bankroll,
        equity_curve=equity,
    )


def run_backtest(
    conn: sqlite3.Connection,
    config: BotConfig,
    *,
    queue_assumption: QueueAssumption = QueueAssumption.OPTIMISTIC,
    maker_fee_multiplier: Decimal = Decimal("0"),
) -> BacktestReport:
    data = load_replay_data(conn)
    if not data.snapshots:
        raise BacktestError("no order-book snapshots to replay")
    prepared = prepare_replay(data, config)
    result = replay_prepared(
        prepared, config, queue_assumption=queue_assumption, maker_fee_multiplier=maker_fee_multiplier
    )
    return build_report(
        result.trades,
        windows_seen=result.windows_seen,
        windows_traded=result.windows_traded,
        first_ts=result.first_ts,
        last_ts=result.last_ts,
        queue_assumption=queue_assumption,
        maker_fee_multiplier=maker_fee_multiplier,
    )


def build_report(
    trades: list[TradeRecord],
    *,
    windows_seen: int,
    windows_traded: set[str],
    first_ts: datetime | None,
    last_ts: datetime | None,
    queue_assumption: QueueAssumption,
    maker_fee_multiplier: Decimal,
) -> BacktestReport:
    """Shared by ``run_backtest`` and :mod:`btcbot.live_paper`, so a live run and a backtest replay of the
    same recorded data produce directly comparable reports (spec section 8.5's "compare live paper results
    to the backtest")."""
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
    if first_ts is not None and last_ts is not None:
        span_seconds = (last_ts - first_ts).total_seconds()
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
