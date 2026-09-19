"""Strategy lab: sweep entry timing, price bands, trend filters, account size and risk sizing over recorded
data, with a time-ordered train/test split so a result is judged on windows it was not tuned on.

This is a research tool, not an advisor. It replays recorded order books through the same model, strategy,
risk manager and queue-aware paper broker as ``btcbot backtest`` (see :mod:`btcbot.backtest`), so every
number carries that replay's limits: REST-polled books (coarse), simulated queue fills, a Coinbase price
standing in for BRTI. What it adds is a guard against fooling yourself:

* Every combination is ranked on the TRAIN windows only, then shown on the TEST windows. A configuration
  that only looks good in-sample shows up as a big train/test gap.
* One window is skipped between the two halves so the volatility estimate and any resting order cannot leak
  across the boundary.
* Ranking uses a t-statistic of per-trade PnL (size-invariant, penalises thin samples), not raw PnL, so
  "risk more" cannot win by construction, and combinations with too few train trades are not ranked at all.
* The verdict states how many combinations were tried (the best of N random configurations looks good by
  chance) and never says a configuration is profitable. Forward paper trading (``btcbot paper``) on data
  the lab never saw is the next gate; CLAUDE.md's "no profitability claims without out-of-sample results"
  applies to this output unchanged.

Nothing here talks to Kalshi or places an order.
"""

from __future__ import annotations

import itertools
import math
import sqlite3
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from btcbot.backtest import (
    BacktestError,
    EntryFilters,
    PreparedReplay,
    ReplayData,
    ReplayResult,
    load_replay_data,
    merge_replay_data,
    prepare_replay,
    replay_prepared,
)
from btcbot.config import BotConfig, RiskLimits, Sizing
from btcbot.paper_broker import QueueAssumption

DEFAULT_MAX_COMBOS = 400
MIN_WINDOWS = 6
ENOUGH_TEST_TRADES = 30


class LabError(Exception):
    """A lab request that cannot be run (bad grid value, too many combinations, too little data)."""


# --------------------------------------------------------------------------- parameters


@dataclass(frozen=True, slots=True)
class LabParams:
    min_edge: Decimal
    max_spread: Decimal
    min_depth: Decimal
    min_tau_sec: int
    max_tau_sec: int
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    trend_mode: str = "off"
    trend_lookback_sec: int = 60
    trend_min_move_usd: Decimal = Decimal(0)
    model_blend: float = 0.5
    risk_pct: Decimal | None = None   # percent of bankroll risked per trade; None = fixed contracts
    contracts: int = 5
    min_stake_pct: Decimal = Decimal(5)  # minimum order premium as % of initial account

    @classmethod
    def from_config(cls, config: BotConfig) -> LabParams:
        return cls(
            min_edge=config.min_edge, max_spread=config.max_spread, min_depth=config.min_depth,
            min_tau_sec=config.min_tau_sec, max_tau_sec=config.max_tau_sec, model_blend=config.model_blend,
            contracts=config.sizing.contracts_per_trade,
        )

    def describe(self, base: LabParams) -> str:
        """Only what differs from ``base``, so a row reads as "what was changed"."""
        parts = []
        for name in self.__dataclass_fields__:
            mine, theirs = getattr(self, name), getattr(base, name)
            if mine != theirs:
                parts.append(f"{name}={'-' if mine is None else mine}")
        return ", ".join(parts) or "(defaults)"


@dataclass(frozen=True, slots=True)
class AccountSettings:
    """The account the lab pretends to run. Limits scale with it so a $200 and a $5,000 account face
    proportionate rules."""

    account_usd: Decimal = Decimal(500)
    max_exposure_pct: Decimal = Decimal(25)   # of the account, at risk across open orders and positions
    daily_loss_pct: Decimal = Decimal(10)     # UTC-day loss that halts new orders
    max_consecutive_losses: int = 5
    max_trades_per_hour: int = 12


_DECIMAL_KEYS = {"min_edge", "max_spread", "min_depth", "min_price", "max_price", "trend_min_move_usd", "risk_pct", "min_stake_pct"}
_INT_KEYS = {"min_tau_sec", "max_tau_sec", "trend_lookback_sec", "contracts"}
_FLOAT_KEYS = {"model_blend"}
_OPTIONAL_KEYS = {"min_price", "max_price", "risk_pct"}
TUNABLE = tuple(sorted(_DECIMAL_KEYS | _INT_KEYS | _FLOAT_KEYS | {"trend_mode"}))
_TREND_MODES = ("off", "with", "against", "aligned4")


def _check(key: str, value: Any) -> Any:
    ok = {
        "min_edge": lambda v: 0 <= v < 1,
        "max_spread": lambda v: 0 < v <= 1,
        "min_depth": lambda v: v >= 0,
        "min_price": lambda v: v is None or 0 < v < 1,
        "max_price": lambda v: v is None or 0 < v < 1,
        "trend_min_move_usd": lambda v: v >= 0,
        "min_stake_pct": lambda v: 0 <= v <= 100,
        "risk_pct": lambda v: v is None or 0 < v <= 100,
        "min_tau_sec": lambda v: 0 <= v <= 900,
        "max_tau_sec": lambda v: 0 < v <= 900,
        "trend_lookback_sec": lambda v: 5 <= v <= 900,
        "contracts": lambda v: v >= 1,
        "model_blend": lambda v: 0 <= v <= 1,
        "trend_mode": lambda v: v in _TREND_MODES,
    }[key]
    if not ok(value):
        raise LabError(f"{key}: {value!r} is out of range")
    return value


def parse_values(key: str, raw: str | Sequence[Any]) -> list[Any]:
    """``"0.02, 0.04"`` (or a list) into typed, range-checked values. Blank / "none" means "no limit" for the
    optional keys (price bounds, risk percent)."""
    if key not in TUNABLE:
        raise LabError(f"unknown parameter {key!r}; choose from {', '.join(TUNABLE)}")
    items = [p.strip() for p in raw.split(",")] if isinstance(raw, str) else list(raw)
    items = [p for p in items if not (isinstance(p, str) and p == "" and key not in _OPTIONAL_KEYS)]
    if not items:
        raise LabError(f"{key}: no values given")
    out: list[Any] = []
    for item in items:
        if key in _OPTIONAL_KEYS and (item is None or (isinstance(item, str) and item.lower() in ("", "none"))):
            value: Any = None
        elif key in _DECIMAL_KEYS:
            try:
                value = Decimal(str(item))
            except InvalidOperation:
                raise LabError(f"{key}: {item!r} is not a number") from None
            if not value.is_finite():
                raise LabError(f"{key}: {item!r} is not finite")
        elif key in _INT_KEYS:
            try:
                value = int(str(item))
            except ValueError:
                raise LabError(f"{key}: {item!r} is not a whole number") from None
        elif key in _FLOAT_KEYS:
            try:
                value = float(item)
            except ValueError:
                raise LabError(f"{key}: {item!r} is not a number") from None
        else:
            value = str(item).lower()
        _check(key, value)
        if value not in out:
            out.append(value)
    return out


def expand_grid(base: LabParams, grid: Mapping[str, Sequence[Any]], *, max_combos: int = DEFAULT_MAX_COMBOS) -> list[LabParams]:
    """Cartesian product of the grid over ``base``. Combinations that contradict themselves (entry window
    closing before it opens, price band inverted) are dropped, not errors, so a wide grid still runs."""
    keys = list(grid)
    for key in keys:
        if key not in TUNABLE:
            raise LabError(f"unknown parameter {key!r}")
    raw_total = math.prod(len(grid[k]) for k in keys) if keys else 1
    if raw_total > max_combos * 4:
        raise LabError(f"{raw_total:,} combinations is too many; narrow the grid (limit {max_combos})")
    combos: list[LabParams] = []
    seen: set[LabParams] = set()
    for values in itertools.product(*(grid[k] for k in keys)):
        params = base
        for key, value in zip(keys, values, strict=True):
            params = _replace(params, key, value)
        if params.min_tau_sec >= params.max_tau_sec:
            continue
        if params.min_price is not None and params.max_price is not None and params.min_price >= params.max_price:
            continue
        if params not in seen:
            seen.add(params)
            combos.append(params)
    if not combos:
        raise LabError("every combination in the grid contradicts itself (check entry window and price band)")
    if len(combos) > max_combos:
        raise LabError(f"{len(combos):,} combinations is too many; narrow the grid (limit {max_combos})")
    return combos


def _replace(params: LabParams, key: str, value: Any) -> LabParams:
    data = {name: getattr(params, name) for name in params.__dataclass_fields__}
    data[key] = value
    return LabParams(**data)


DEFAULT_GRID: dict[str, list[Any]] = {
    "min_edge": [Decimal("0.02"), Decimal("0.04"), Decimal("0.06")],
    "min_tau_sec": [30, 120, 300],
    "max_tau_sec": [480, 780],
    "max_price": [None, Decimal("0.60")],
    "trend_mode": ["off", "with"],
}


# --------------------------------------------------------------------------- one evaluation


@dataclass(frozen=True, slots=True)
class Metrics:
    windows: int
    trades: int
    resolved: int
    wins: int
    win_rate: float | None
    pnl: Decimal
    fees: Decimal
    avg_pnl_per_trade: Decimal | None
    t_stat: float | None
    max_drawdown: Decimal
    return_pct: float | None
    max_drawdown_pct: float | None
    trades_per_day: float | None
    filtered: dict[str, int] = field(default_factory=dict)


def _metrics(result: ReplayResult, account_usd: Decimal) -> Metrics:
    resolved = sorted((t for t in result.trades if t.pnl_usd is not None), key=lambda t: t.entry_ts)
    pnls = [t.pnl_usd for t in resolved]
    total = sum(pnls, Decimal(0))
    running = peak = drawdown = Decimal(0)
    for pnl in pnls:
        running += pnl
        peak = max(peak, running)
        drawdown = max(drawdown, peak - running)
    t_stat = None
    if len(pnls) >= 2:
        sd = statistics.stdev(float(p) for p in pnls)
        if sd > 0:
            t_stat = (float(total) / len(pnls)) / (sd / math.sqrt(len(pnls)))
    span_days = (result.last_ts - result.first_ts).total_seconds() / 86400
    return Metrics(
        windows=result.windows_seen,
        trades=len(result.trades),
        resolved=len(resolved),
        wins=sum(1 for p in pnls if p > 0),
        win_rate=(sum(1 for p in pnls if p > 0) / len(pnls)) if pnls else None,
        pnl=total,
        fees=sum((t.fee_paid for t in result.trades), Decimal(0)),
        avg_pnl_per_trade=(total / len(pnls)) if pnls else None,
        t_stat=t_stat,
        max_drawdown=drawdown,
        return_pct=float(total / account_usd * 100) if account_usd > 0 else None,
        max_drawdown_pct=float(drawdown / account_usd * 100) if account_usd > 0 else None,
        trades_per_day=(len(result.trades) / span_days) if span_days > 0 else None,
        filtered=dict(result.filter_counts),
    )


def _config_for(base: BotConfig, params: LabParams, account: AccountSettings) -> BotConfig:
    exposure = account.account_usd * account.max_exposure_pct / 100
    risk = RiskLimits(
        max_contracts_per_trade=max(1, int(account.account_usd * 100)) if params.risk_pct is not None or params.min_stake_pct > 0 else max(params.contracts, 1),
        max_open_exposure_usd=max(exposure, Decimal("0.01")),
        daily_loss_limit_usd=max(account.account_usd * account.daily_loss_pct / 100, Decimal("0.01")),
        max_consecutive_losses=account.max_consecutive_losses,
        max_trades_per_hour=account.max_trades_per_hour,
    )
    data = base.model_dump()
    data.update(
        min_edge=params.min_edge, max_spread=params.max_spread, min_depth=params.min_depth,
        min_tau_sec=params.min_tau_sec, max_tau_sec=params.max_tau_sec, model_blend=params.model_blend,
        sizing=Sizing(contracts_per_trade=params.contracts).model_dump(), risk=risk.model_dump(),
    )
    return BotConfig(**data)


def _filters_for(params: LabParams, account: AccountSettings) -> EntryFilters:
    return EntryFilters(
        min_price=params.min_price, max_price=params.max_price, trend_mode=params.trend_mode,
        trend_lookback_sec=params.trend_lookback_sec, trend_min_move_usd=params.trend_min_move_usd,
        account_usd=account.account_usd,
        min_stake_usd=account.account_usd * params.min_stake_pct / 100,
        risk_pct_per_trade=None if params.risk_pct is None else params.risk_pct / 100,
    )


def evaluate(
    prepared: PreparedReplay, base: BotConfig, params: LabParams, account: AccountSettings, *,
    queue: QueueAssumption, maker_fee_multiplier: Decimal,
) -> Metrics:
    result = replay_prepared(
        prepared, _config_for(base, params, account), queue_assumption=queue,
        maker_fee_multiplier=maker_fee_multiplier, filters=_filters_for(params, account),
    )
    return _metrics(result, account.account_usd)


# --------------------------------------------------------------------------- data


def load_lab_data(paths: Sequence[Path]) -> ReplayData:
    """Load and merge recordings. A file with no order-book table or no snapshots yet (for example a paper
    run that has only just started) is skipped rather than failing the whole request."""
    if any("demo" in path.name.lower() for path in paths):
        raise LabError("Strategy Lab excludes demo/synthetic files. Use Demo orders to audit execution; select only production recordings here.")
    parts = []
    for path in paths:
        conn = sqlite3.connect(str(path))
        try:
            data = load_replay_data(conn)
        except sqlite3.OperationalError:
            continue
        finally:
            conn.close()
        if data.snapshots:
            parts.append(data)
    if not parts:
        raise LabError("none of the selected data files has order-book snapshots yet")
    return merge_replay_data(parts)


# --------------------------------------------------------------------------- split


def split_windows(data: ReplayData, train_fraction: float, *, embargo: int = 1) -> tuple[set[str], set[str], list[str]]:
    """Windows in time order, first ``train_fraction`` for training, then ``embargo`` skipped, the rest for
    testing. Returns (train, test, ordered_all)."""
    if not 0.2 <= train_fraction <= 0.9:
        raise LabError("train fraction must be between 0.2 and 0.9")
    first_seen: dict[str, Any] = {}
    for snap in data.snapshots:
        first_seen.setdefault(snap.ticker, snap.poll_ts)
    ordered = sorted(first_seen, key=first_seen.__getitem__)
    if len(ordered) < MIN_WINDOWS:
        raise LabError(
            f"only {len(ordered)} market windows recorded; the lab needs at least {MIN_WINDOWS} to split into "
            "train and test, and days of them (96 per day) before any result means much. Keep `btcbot paper` "
            "or `btcbot record` running and try again."
        )
    cut = max(1, min(len(ordered) - embargo - 1, int(len(ordered) * train_fraction)))
    return set(ordered[:cut]), set(ordered[cut + embargo:]), ordered


# --------------------------------------------------------------------------- the sweep


@dataclass(frozen=True, slots=True)
class LabRow:
    rank: int
    params: dict[str, Any]
    description: str
    train: Metrics
    test: Metrics
    equivalent: int = 0  # other settings that produced exactly the same training trades (filters that never bound)


@dataclass(frozen=True, slots=True)
class LabReport:
    windows_total: int
    windows_train: int
    windows_test: int
    combinations: int
    ranked_combinations: int
    min_train_trades: int
    queue: str
    maker_fee_multiplier: str
    account_usd: str
    baseline_params: dict[str, Any]
    baseline_train: Metrics
    baseline_test: Metrics
    rows: list[LabRow]
    verdict_level: str  # "insufficient" | "not_supported" | "weak_signal"
    verdict: str
    warnings: list[str]
    seconds: float


ProgressFn = Callable[[int, int, str], None]


def run_lab(
    data: ReplayData,
    base_config: BotConfig,
    grid: Mapping[str, Sequence[Any]],
    *,
    account: AccountSettings | None = None,
    train_fraction: float = 0.7,
    queue: QueueAssumption = QueueAssumption.OPTIMISTIC,
    maker_fee_multiplier: Decimal = Decimal(0),
    top_k: int = 8,
    min_train_trades: int = 20,
    max_combos: int = DEFAULT_MAX_COMBOS,
    progress: ProgressFn | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> LabReport:
    started = time.monotonic()
    account = account or AccountSettings()
    if account.account_usd <= 0:
        raise LabError("account size must be positive")
    base = LabParams.from_config(base_config)
    combos = expand_grid(base, grid, max_combos=max_combos)
    train, test, ordered = split_windows(data, train_fraction)

    cache: dict[tuple[str, float], PreparedReplay] = {}

    def prepared_for(part: str, blend: float) -> PreparedReplay:
        key = (part, blend)
        if key not in cache:
            tickers = train if part == "train" else test
            cache[key] = prepare_replay(data, base_config, tickers=tickers, model_blend=blend)
        return cache[key]

    def run(params: LabParams, part: str) -> Metrics:
        return evaluate(prepared_for(part, params.model_blend), base_config, params, account,
                        queue=queue, maker_fee_multiplier=maker_fee_multiplier)

    try:
        baseline_train, baseline_test = run(base, "train"), run(base, "test")
    except BacktestError as exc:
        raise LabError(str(exc)) from None

    scored: list[tuple[float, LabParams, Metrics]] = []
    total = len(combos)
    for i, params in enumerate(combos, 1):
        if cancelled():
            raise LabError("cancelled")
        if progress is not None:
            progress(i - 1, total, params.describe(base))
        m = run(params, "train")
        if m.resolved >= min_train_trades and m.t_stat is not None:
            scored.append((m.t_stat, params, m))
    if progress is not None:
        progress(total, total, "evaluating the top combinations on the test windows")

    scored.sort(key=lambda item: item[0], reverse=True)
    # Settings whose filters never bound make identical trades; show one row and count the rest.
    unique: list[tuple[float, LabParams, Metrics, int]] = []
    index: dict[tuple[int, int, Decimal], int] = {}
    for score, params, m in scored:
        signature = (m.trades, m.wins, m.pnl)
        if signature in index:
            score0, p0, m0, n0 = unique[index[signature]]
            unique[index[signature]] = (score0, p0, m0, n0 + 1)
        else:
            index[signature] = len(unique)
            unique.append((score, params, m, 0))
    rows = [
        LabRow(rank=i, params=_jsonable(asdict(p)), description=p.describe(base), train=m, test=run(p, "test"),
               equivalent=n)
        for i, (_, p, m, n) in enumerate(unique[:top_k], 1)
    ]

    level, verdict, warnings = _verdict(rows, baseline_test, combos=total, ranked=len(scored),
                                        min_train_trades=min_train_trades, windows_test=len(test))
    if queue is QueueAssumption.OPTIMISTIC:
        warnings.insert(0, (
            "Fills use the OPTIMISTIC queue assumption: every drop in a price level's size counts as a trade that "
            "reached us, so fills and PnL here are a best case, not an expectation. The pessimistic assumption "
            "never fills at all, zero fills are not a lower bound on live losses; adverse selection can make live results worse. A recorded trade tape is needed to constrain fills."
        ))
    return LabReport(
        windows_total=len(ordered), windows_train=len(train), windows_test=len(test), combinations=total,
        ranked_combinations=len(scored), min_train_trades=min_train_trades, queue=queue.value,
        maker_fee_multiplier=str(maker_fee_multiplier), account_usd=str(account.account_usd),
        baseline_params=_jsonable(asdict(base)), baseline_train=baseline_train, baseline_test=baseline_test,
        rows=rows, verdict_level=level, verdict=verdict, warnings=warnings, seconds=time.monotonic() - started,
    )


def _jsonable(data: dict[str, Any]) -> dict[str, Any]:
    return {k: (None if v is None else str(v) if isinstance(v, Decimal) else v) for k, v in data.items()}


def _verdict(
    rows: list[LabRow], baseline_test: Metrics, *, combos: int, ranked: int, min_train_trades: int, windows_test: int
) -> tuple[str, str, list[str]]:
    warnings = [
        f"{combos:,} combinations were tried. The best of many random configurations looks good in-sample by "
        "chance alone; only the test columns mean anything, and even they are one sample.",
        "Replays use REST-polled books and simulated queue fills, with Coinbase standing in for BRTI. Treat "
        "fills and prices as approximate.",
        "Minimum stake is an order-premium floor, not a guaranteed fill size; partial fills may be smaller. Fees are extra; cash and risk limits can skip orders.",
        "aligned4 needs 24 hours of causal spot history; missing history blocks entries. This is an entry filter, not an 80/99-cent exit or side-switching strategy.",
        "Adverse selection is invisible here: with model weight below 1, part of the 'edge' is the gap between "
        "the market mid and your bid. That is only real if being filled does not tell you the price is about to "
        "move against you, and a replay cannot show that.",
    ]
    if not rows:
        return (
            "insufficient",
            f"No combination made at least {min_train_trades} resolved trades on the training windows, so nothing "
            "could be ranked. That usually means too little recorded data (or filters too strict). Record more "
            "days. Lowering the minimum does not create evidence.",
            warnings,
        )
    if min_train_trades < 20:
        return "insufficient", "Exploratory ranking only: the training threshold is below 20. Collect more data before evaluating an edge.", warnings
    best = rows[0]
    if best.test.resolved < ENOUGH_TEST_TRADES:
        warnings.append(
            f"The top configuration made only {best.test.resolved} resolved trades on the test windows "
            f"({windows_test} windows); under {ENOUGH_TEST_TRADES} is too few to tell skill from luck."
        )
        return (
            "insufficient",
            "Not enough test trades to say anything. The train ranking is not evidence; collect more data.",
            warnings,
        )
    if best.test.pnl <= 0:
        return (
            "not_supported",
            "The top training configuration did NOT make money on the test windows. It fit noise in the training "
            "data. Do not trade it.",
            warnings,
        )
    if best.test.t_stat is None or best.test.t_stat < 2:
        return (
            "not_supported",
            f"The top configuration was positive on the test windows (${best.test.pnl:,.2f}) but not distinguishable "
            f"from luck (t = {best.test.t_stat if best.test.t_stat is not None else 'n/a'}, want at least 2).",
            warnings,
        )
    gap = best.train.t_stat - best.test.t_stat if best.train.t_stat is not None else None
    if gap is not None and gap > 2:
        warnings.append("Large train-to-test drop in t-statistic: partly overfit.")
    return (
        "weak_signal",
        f"The top configuration held up on the test windows (${best.test.pnl:,.2f}, t = {best.test.t_stat:.1f}). "
        "That is a reason to forward paper-trade it on new data, not a profitability claim.",
        warnings,
    )


# --------------------------------------------------------------------------- text report


def render_lab_report(report: LabReport) -> str:
    def fmt(m: Metrics) -> str:
        wr = "--" if m.win_rate is None else f"{m.win_rate * 100:.0f}%"
        t = "--" if m.t_stat is None else f"{m.t_stat:+.1f}"
        return f"n={m.resolved:<4d} win {wr:>4s}  pnl ${m.pnl:>9,.2f}  t {t:>5s}  dd {m.max_drawdown_pct or 0:>4.1f}%"

    lines = [
        f"Windows: {report.windows_total} ({report.windows_train} train, {report.windows_test} test, 1 skipped between)."
        f"  {report.combinations:,} combinations, {report.ranked_combinations:,} with >= {report.min_train_trades} train trades."
        f"  Account ${report.account_usd}, queue={report.queue}, maker fee x{report.maker_fee_multiplier}.",
        "",
        f"{'':>4s}  {'TRAIN':<58s}  TEST",
        f"{'base':>4s}  {fmt(report.baseline_train):<58s}  {fmt(report.baseline_test)}   (config.yaml defaults)",
    ]
    for row in report.rows:
        same = f"  (+{row.equivalent} settings gave identical results)" if row.equivalent else ""
        lines.append(f"{row.rank:>4d}  {fmt(row.train):<58s}  {fmt(row.test)}   {row.description}{same}")
    lines += ["", f"Verdict [{report.verdict_level}]: {report.verdict}", ""]
    lines += [f"- {w}" for w in report.warnings]
    lines.append(f"({report.seconds:.1f}s)")
    return "\n".join(lines)
