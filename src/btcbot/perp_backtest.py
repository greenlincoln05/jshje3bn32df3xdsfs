"""Paper backtest of trading Kalshi's BTC perpetual with idle cash, at no leverage or a little
(``btcbot perp-backtest``, docs/research/perps-paper.md). Offline: reads BTC bars already in a local database, no
network, no key, no order code (see :mod:`btcbot.perp_paper`).

The question it answers is the owner's: "is it even worth a dime" before anything goes near Kalshi's perps demo.
So every strategy is judged against the two things idle cash could do instead:

- ``flat``: stay in cash (return 0). Beating it means covering fees and funding at all.
- ``hold``: go long once and sit (just BTC exposure through the perp). Beating it means the trading itself adds
  something over simply owning BTC.

Two active strategies, fixed BEFORE seeing any data (no parameter search, so there is nothing to overfit):

- ``trend_24h``: each hour, long if BTC is more than ``band`` above its 24-hour average, short if more than
  ``band`` below, otherwise keep the current side. Slow on purpose: at a 12 bps taker fee per side, anything
  that trades often has to clear ~24 bps a round trip.
- ``window_15m``: the 15-minute bot's own idea transplanted to the perp -- five minutes into each 15-minute
  window, go with the window's direction so far, exit at the window's end. Included because it is the
  literal "copy" of the Kalshi strategy; its fee load (up to 96 round trips a day) is the point to measure.

Fills happen at the NEXT bar's open (a decision made on a bar's close cannot also trade at that close), as a
taker, plus :class:`btcbot.perp_paper.PerpSpec`'s slippage. Results are split in time: the later
``1 - train_fraction`` of the bars is the held-out test, judged on daily returns with a t-statistic, and the
verdict wording never calls anything profitable (CLAUDE.md: no profitability claims without recorded
out-of-sample results, and a backtest on a spot proxy with an assumed funding rate is not that).
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from btcbot.perp_paper import PerpAccount, PerpPaperError, PerpSpec, funding_times

MIN_TEST_DAYS = 30
SIGNIFICANCE_T = 2.0
STRATEGIES = ("flat", "hold", "trend_24h", "window_15m")
MAX_PAPER_LEVERAGE = Decimal(3)  # well under Kalshi's ~5.7x; see perps-paper.md before raising it


class PerpBacktestError(Exception):
    """No usable bars, or bad parameters."""


@dataclass(frozen=True, slots=True)
class Bar:
    ts: datetime  # bar open, UTC, one minute long
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


# --------------------------------------------------------------------------- loading


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def load_bars(conn: sqlite3.Connection) -> tuple[list[Bar], str]:
    """1-minute BTC bars from whichever local database this is: a ``download-history`` database's Coinbase
    BTC-USD 1-minute ``spot_candles`` (preferred: USD, and Coinbase is a BRTI constituent), else a
    ``download-polymarket-history`` database's Binance BTCUSDT 1-second klines aggregated to minutes. Returns
    ``(bars, source description)``."""
    tables = _tables(conn)
    if "spot_candles" in tables and conn.execute("SELECT 1 FROM spot_candles LIMIT 1").fetchone():
        from btcbot.history_pipeline import load_candles

        bars = [Bar(c.start, c.open, c.high, c.low, c.close) for c in load_candles(conn)]
        return sorted(bars, key=lambda b: b.ts), "Coinbase BTC-USD 1m candles (spot_candles)"
    if "btc_klines_1s" in tables:
        bars: list[Bar] = []
        cur_min = None
        o = h = low = c = None
        for ts, op, hi, lo, cl in conn.execute("SELECT ts, open, high, low, close FROM btc_klines_1s ORDER BY ts"):
            minute = int(ts) // 60
            op, hi, lo, cl = Decimal(op), Decimal(hi), Decimal(lo), Decimal(cl)
            if minute != cur_min:
                if cur_min is not None:
                    bars.append(Bar(datetime.fromtimestamp(cur_min * 60, tz=timezone.utc), o, h, low, c))
                cur_min, o, h, low, c = minute, op, hi, lo, cl
            else:
                h, low, c = max(h, hi), min(low, lo), cl
        if cur_min is not None:
            bars.append(Bar(datetime.fromtimestamp(cur_min * 60, tz=timezone.utc), o, h, low, c))
        return bars, "Binance BTCUSDT 1s klines aggregated to 1m (btc_klines_1s)"
    raise PerpBacktestError(
        "no BTC bars found: expected a `btcbot download-history` database (spot_candles) or a "
        "`btcbot download-polymarket-history` database (btc_klines_1s)"
    )


# --------------------------------------------------------------------------- strategies


class Strategy:
    """``decide(i)`` runs on bar ``i``'s CLOSE and returns the target side for the next bar's open (+1 long,
    -1 short, 0 flat), or None to keep whatever is held."""

    name = "base"
    warmup = 0

    def __init__(self, bars: Sequence[Bar]) -> None:
        self.bars = bars

    def decide(self, i: int) -> int | None:  # pragma: no cover - interface
        raise NotImplementedError


class Flat(Strategy):
    name = "flat"

    def decide(self, i: int) -> int | None:
        return 0


class Hold(Strategy):
    name = "hold"

    def decide(self, i: int) -> int | None:
        return 1


class Trend24h(Strategy):
    name = "trend_24h"
    lookback = 1440
    band = 0.005

    def __init__(self, bars: Sequence[Bar]) -> None:
        super().__init__(bars)
        self.warmup = self.lookback
        self._cum = [0.0]
        for b in bars:
            self._cum.append(self._cum[-1] + float(b.close))

    def decide(self, i: int) -> int | None:
        if i + 1 < self.lookback or self.bars[i].ts.minute != 59:  # decide on each hour's last bar
            return None
        sma = (self._cum[i + 1] - self._cum[i + 1 - self.lookback]) / self.lookback
        price = float(self.bars[i].close)
        if price > sma * (1 + self.band):
            return 1
        if price < sma * (1 - self.band):
            return -1
        return None


class Window15m(Strategy):
    name = "window_15m"

    def decide(self, i: int) -> int | None:
        bar = self.bars[i]
        m = bar.ts.minute % 15
        if m == 4:  # close of the window's 5th minute -> enter at minute 5's open
            k = i - 4
            if k < 0 or self.bars[k].ts != bar.ts - timedelta(minutes=4):
                return None  # a gap inside the window: no clean window open to compare against
            return 1 if bar.close > self.bars[k].open else (-1 if bar.close < self.bars[k].open else 0)
        if m == 14:  # the window's last bar -> flat at the next window's open
            return 0
        return None


def make_strategy(name: str, bars: Sequence[Bar]) -> Strategy:
    classes = {cls.name: cls for cls in (Flat, Hold, Trend24h, Window15m)}
    if name not in classes:
        raise PerpBacktestError(f"unknown strategy {name!r}; choose from {', '.join(STRATEGIES)}")
    return classes[name](bars)


# --------------------------------------------------------------------------- running


@dataclass(frozen=True, slots=True)
class RunResult:
    strategy: str
    leverage: str
    funding_8h: str
    start: str
    end: str
    start_equity: str
    end_equity: str
    return_pct: float
    max_drawdown_pct: float
    trades: int
    fees_paid: str
    funding_paid: str
    liquidations: int
    exposure_pct: float
    daily_returns: tuple[float, ...] = field(repr=False, default=())


def run(
    bars: Sequence[Bar], strategy_name: str, *, account_usd: Decimal, leverage: Decimal, funding_8h: Decimal,
    spec: PerpSpec,
) -> RunResult:
    if not bars:
        raise PerpBacktestError("no bars to run on")
    strat = make_strategy(strategy_name, bars)
    acct = PerpAccount(cash=account_usd, spec=spec)
    fund_at = funding_times(bars[0].ts - timedelta(seconds=1), bars[-1].ts + timedelta(minutes=1))
    f_idx = 0
    pending: int | None = None
    side = 0
    peak = account_usd
    max_dd = Decimal(0)
    in_market = 0
    day_equity: dict = {}
    for i, bar in enumerate(bars):
        bar_end = bar.ts + timedelta(minutes=1)
        # 1. funding settlements up to this bar (a position held into the stamp pays/receives); a stamp that
        #    fell inside a data gap is still charged, at the first bar after it, never skipped
        while f_idx < len(fund_at) and fund_at[f_idx] < bar_end:
            acct.apply_funding(fund_at[f_idx], funding_8h, bar.open)
            f_idx += 1
        # 2. yesterday's decision executes at this bar's open
        if pending is not None and pending != side:
            if acct.is_open:
                acct.close(bar.ts, bar.open, reason=f"{strat.name} -> {pending}")
            if pending != 0 and acct.cash > 0:
                try:
                    acct.open(bar.ts, pending, bar.open, leverage, reason=strat.name)
                except PerpPaperError:
                    pass  # too little cash left for 0.01 contracts: sits out, the same as a real account would
            side = pending if acct.is_open else 0
        pending = None
        # 3. liquidation against this bar's adverse extreme
        if acct.is_open and acct.check_liquidation(bar.ts, bar.low, bar.high) is not None:
            side = 0
        if acct.is_open:
            in_market += 1
        equity = acct.equity(bar.close)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
        day_equity[bar.ts.date()] = equity
        # 4. decide on this bar's close, for the next bar's open
        if i >= strat.warmup:
            pending = strat.decide(i)
    end_equity = acct.equity(bars[-1].close)
    days = sorted(day_equity)
    prev = account_usd
    daily = []
    for d in days:
        eq = day_equity[d]
        daily.append(float((eq - prev) / prev) if prev > 0 else 0.0)
        prev = eq
    return RunResult(
        strategy=strat.name, leverage=str(leverage), funding_8h=str(funding_8h),
        start=bars[0].ts.isoformat(), end=bars[-1].ts.isoformat(),
        start_equity=str(account_usd), end_equity=str(end_equity.quantize(Decimal("0.01"))),
        return_pct=float((end_equity - account_usd) / account_usd * 100),
        max_drawdown_pct=float(max_dd * 100), trades=sum(1 for e in acct.events if e.kind == "open"),
        fees_paid=str(acct.fees_paid.quantize(Decimal("0.01"))),
        funding_paid=str(acct.funding_paid.quantize(Decimal("0.01"))), liquidations=acct.liquidations,
        exposure_pct=100.0 * in_market / len(bars), daily_returns=tuple(daily),
    )


def excess_t(a: Sequence[float], b: Sequence[float]) -> float | None:
    """t-statistic of the mean daily return difference a - b (paired by day). None if too few days or no spread."""
    diffs = [x - y for x, y in zip(a, b)]
    if len(diffs) < 3:
        return None
    sd = statistics.stdev(diffs)
    return None if sd == 0 else statistics.fmean(diffs) / (sd / math.sqrt(len(diffs)))


def verdict(test_days: int, t_vs_cash: float | None, t_vs_hold: float | None) -> str:
    if test_days < MIN_TEST_DAYS:
        return f"insufficient data: {test_days} test days, need at least {MIN_TEST_DAYS} before any verdict"
    parts = []
    for label, t in (("cash", t_vs_cash), ("buy-and-hold", t_vs_hold)):
        if t is None:
            parts.append(f"vs {label}: n/a")
        elif t >= SIGNIFICANCE_T:
            parts.append(f"vs {label}: evidence consistent with beating it (t={t:+.2f}), not proof of an edge")
        elif t <= -SIGNIFICANCE_T:
            parts.append(f"vs {label}: evidence it does WORSE (t={t:+.2f})")
        else:
            parts.append(f"vs {label}: no evidence either way (t={t:+.2f})")
    return "; ".join(parts)


@dataclass
class PerpBacktestReport:
    source: str
    bars: int
    train_end: str
    spec: dict
    rows: list[dict]  # one per (segment, strategy, leverage, funding)
    verdicts: list[dict]


def split_bars(bars: Sequence[Bar], train_fraction: float) -> tuple[list[Bar], list[Bar]]:
    if not 0.2 <= train_fraction <= 0.9:
        raise PerpBacktestError("train fraction must be between 0.2 and 0.9")
    cut = int(len(bars) * train_fraction)
    return list(bars[:cut]), list(bars[cut:])


def run_backtest(
    bars: Sequence[Bar],
    *,
    source: str = "",
    account_usd: Decimal = Decimal(500),
    leverages: Sequence[Decimal] = (Decimal(1), Decimal(2)),
    fundings: Sequence[Decimal] = (Decimal(0), Decimal("0.0001")),
    strategies: Sequence[str] = STRATEGIES,
    train_fraction: float = 0.7,
    spec: PerpSpec | None = None,
) -> PerpBacktestReport:
    spec = spec or PerpSpec()
    if len(bars) < 2 * 1440:
        raise PerpBacktestError(f"only {len(bars)} one-minute bars; need at least two days of data")
    for lev in leverages:
        if not Decimal(0) < lev <= MAX_PAPER_LEVERAGE:
            raise PerpBacktestError(f"leverage {lev} outside (0, {MAX_PAPER_LEVERAGE}]")
    train, test = split_bars(bars, train_fraction)
    rows: list[dict] = []
    verdicts: list[dict] = []
    for funding in fundings:
        for lev in leverages:
            results: dict[tuple[str, str], RunResult] = {}
            for segment, seg_bars in (("train", train), ("test", test)):
                for name in strategies:
                    r = run(seg_bars, name, account_usd=account_usd, leverage=lev, funding_8h=funding, spec=spec)
                    results[(segment, name)] = r
                    row = asdict(r)
                    row.pop("daily_returns")
                    row["segment"] = segment
                    rows.append(row)
            cash = [0.0] * len(results[("test", strategies[0])].daily_returns)
            hold = results.get(("test", "hold"))
            for name in strategies:
                if name in ("flat", "hold"):
                    continue
                r = results[("test", name)]
                t_cash = excess_t(r.daily_returns, cash)
                t_hold = excess_t(r.daily_returns, hold.daily_returns) if hold else None
                verdicts.append({
                    "strategy": name, "leverage": str(lev), "funding_8h": str(funding),
                    "test_days": len(r.daily_returns), "t_vs_cash": t_cash, "t_vs_hold": t_hold,
                    "verdict": verdict(len(r.daily_returns), t_cash, t_hold),
                })
    spec_dict = {k: str(v) for k, v in asdict(spec).items()}
    return PerpBacktestReport(source, len(bars), train[-1].ts.isoformat(), spec_dict, rows, verdicts)


def render_report(report: PerpBacktestReport) -> str:
    s = report.spec
    lines = [
        f"BTC perpetual PAPER backtest -- {report.source}, {report.bars} one-minute bars; test = bars after {report.train_end}.",
        f"Kalshi BTCPERP rules as read 2026-09-24 (verify before trusting): taker fee {s['taker_fee_bps']} bps on notional, "
        f"maintenance margin {s['maintenance_frac']} x initial, funding cap {s['funding_cap']} / dead band {s['funding_deadband']} "
        f"per 8h. Assumed: {s['half_spread_bps']} bps slippage per fill, {s['liq_slippage_bps']} bps extra on a liquidation, "
        "perp price = the spot proxy, funding = the constant shown per row.",
        "",
        f"{'seg':<5} {'strategy':<11} {'lev':>3} {'fund/8h':>8} {'return%':>8} {'maxDD%':>7} {'trades':>6} "
        f"{'fees$':>9} {'funding$':>9} {'liq':>3} {'in-mkt%':>7}",
    ]
    for r in report.rows:
        lines.append(
            f"{r['segment']:<5} {r['strategy']:<11} {r['leverage']:>3} {r['funding_8h']:>8} {r['return_pct']:>+8.2f} "
            f"{r['max_drawdown_pct']:>7.2f} {r['trades']:>6} {r['fees_paid']:>9} {r['funding_paid']:>9} "
            f"{r['liquidations']:>3} {r['exposure_pct']:>7.1f}"
        )
    lines.append("\nHeld-out test verdicts (daily returns, paired by day):")
    for v in report.verdicts:
        lines.append(f"  {v['strategy']:<11} lev {v['leverage']} funding {v['funding_8h']}: {v['verdict']}")
    lines.append(
        "\nPaper simulation on a spot proxy with an assumed funding rate -- not a profitability claim and not a "
        "recorded out-of-sample result. Nothing here places an order; perps have no order code in this repo."
    )
    return "\n".join(lines)
