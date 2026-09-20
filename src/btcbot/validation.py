"""Time-split validation harness for probability models (feature-store rows in, honest verdict out).

Any model is a callable ``row -> P(YES)`` (or None to abstain). The harness splits whole markets by time (train =
earlier windows, an embargo gap, test = later windows: never a random split, never rows of one window on both
sides), scores the probabilities (Brier), and runs one fixed, deliberately simple entry policy on the held-out
windows: the first row inside the tau range whose fee-adjusted edge clears ``min_edge``, bought at the best BID of
that side (an optimistic maker fill: adverse selection is invisible here). It reports trades, wins, a Wilson
interval, P&L and a t-statistic, and REFUSES to give a verdict below ``min_test_trades`` (default 30). Its wording
never calls a model profitable: a result is "evidence consistent with an edge" or "no evidence", nothing more.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Sequence

from btcbot.paper_broker import maker_fee

Predictor = Callable[[dict], "float | None"]


class ValidationError(Exception):
    pass


def split_windows(rows: Sequence[dict], train_frac: float = 0.7, embargo: int = 1) -> tuple[list[str], list[str]]:
    """Whole markets ordered by first row time; ``embargo`` windows between train and test are dropped."""
    if not 0 < train_frac < 1:
        raise ValidationError("train_frac must be between 0 and 1")
    first: dict[str, str] = {}
    for r in rows:
        first.setdefault(r["ticker"], r["ts"])
    ordered = sorted(first, key=lambda t: (first[t], t))
    n_train = int(len(ordered) * train_frac)
    if n_train < 1 or len(ordered) - n_train - embargo < 1:
        raise ValidationError("too few windows to split into train and test")
    return ordered[:n_train], ordered[n_train + embargo:]


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


@dataclass(frozen=True, slots=True)
class Policy:
    min_edge: float = 0.04
    min_tau_sec: float = 30
    max_tau_sec: float = 780
    min_price: float = 0.15
    max_price: float = 0.85
    maker_fee_multiplier: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class Evaluation:
    windows: int
    scored_rows: int
    brier: float | None
    trades: int
    wins: int
    win_rate: float | None
    ci95: tuple[float, float]
    pnl_per_contract: float
    mean_per_trade: float | None
    t_stat: float | None
    verdict: str


def _fee(price: float, mult: Decimal) -> float:
    return float(maker_fee(Decimal(1), Decimal(str(price)), multiplier=mult))


def evaluate(rows: Sequence[dict], tickers: Sequence[str], predict: Predictor, policy: Policy = Policy(),
             *, min_test_trades: int = 30) -> Evaluation:
    want = set(tickers)
    by: dict[str, list[dict]] = {}
    for r in rows:
        if r["ticker"] in want:
            by.setdefault(r["ticker"], []).append(r)
    pairs: list[tuple[float, bool]] = []
    pnls: list[float] = []
    wins = 0
    for rs in by.values():
        rs.sort(key=lambda r: r["ts"])
        label = next((r["outcome_yes"] for r in rs if r["outcome_yes"] is not None), None)
        if label is None:
            continue  # unsettled: no outcome, no score, no trade
        entered = False
        for r in rs:
            p = predict(r)
            if p is None:
                continue
            pairs.append((p, bool(label)))
            if entered or not (policy.min_tau_sec <= r["tau_sec"] <= policy.max_tau_sec):
                continue
            for side, p_side, bid in (("yes", p, r["yes_bid"]), ("no", 1 - p, r["no_bid"])):
                if bid is None or not (policy.min_price <= bid <= policy.max_price):
                    continue
                fee = _fee(bid, policy.maker_fee_multiplier)
                if p_side - bid - fee >= policy.min_edge:
                    won = (label == 1) == (side == "yes")
                    pnls.append((1 - bid if won else -bid) - fee)
                    wins += won
                    entered = True
                    break
    n = len(pnls)
    brier = None if not pairs else sum((p - int(y)) ** 2 for p, y in pairs) / len(pairs)
    mean = None if n == 0 else sum(pnls) / n
    t = None
    if n >= 2:
        sd = statistics.stdev(pnls)
        t = None if sd == 0 else mean / (sd / math.sqrt(n))
    if n < min_test_trades:
        verdict = f"insufficient: {n} test trades, need {min_test_trades}; no conclusion"
    elif (t is not None and t >= 2 and mean > 0) or (sd_zero(pnls) and mean > 0):
        verdict = "evidence consistent with an edge on held-out windows (optimistic fills); needs forward paper confirmation"
    else:
        verdict = "no evidence of an edge on held-out windows"
    return Evaluation(len(by), len(pairs), brier, n, wins, None if n == 0 else wins / n, wilson(wins, n),
                      sum(pnls), mean, t, verdict)


def sd_zero(xs: Sequence[float]) -> bool:
    """All results identical (t is undefined, e.g. every trade won at one price)."""
    return len(xs) >= 2 and statistics.pstdev(xs) == 0


def validate(rows: Sequence[dict], predict: Predictor, policy: Policy = Policy(), *, train_frac: float = 0.7,
             embargo: int = 1, min_test_trades: int = 30) -> dict:
    train, test = split_windows(rows, train_frac, embargo)
    return {"train": evaluate(rows, train, predict, policy, min_test_trades=min_test_trades),
            "test": evaluate(rows, test, predict, policy, min_test_trades=min_test_trades)}


def blend_predictor(row: dict) -> float | None:
    """The bot's current model (``p_blend`` as recorded), as a baseline to compare any new model against."""
    return row.get("p_blend")
