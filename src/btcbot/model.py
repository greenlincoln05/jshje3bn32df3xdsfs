"""Fair-probability model (Phase 3), per docs/btc15m-bot-spec.md section 4.

v1 treats the spot proxy as a driftless lognormal process and asks: what is P(spot at the relevant future
time >= strike)? The settlement value is a 60-second average ending at close, not a single price, so "the
relevant future time" needs two cases:

- More than 60s to close: the average hasn't started yet. An average over the last 60s of a driftless
  lognormal process is well approximated, for this v1 model, by the price at the average's midpoint, i.e.
  30s before close (``close - 30``, which is ``tau - 30`` seconds from now).
- 60s or less to close: part of the averaging window has already happened. The part still to come is
  exactly the remaining ``tau`` seconds, so by the same midpoint argument its unobserved contribution is
  approximated by the price ``tau / 2`` seconds from now. What that unobserved part needs to average, for
  the full 60s window to clear the strike, is an adjusted strike computed from what has already been
  observed. At ``tau == 60`` (elapsed 0) this reduces exactly to the first case's ``tau - 30 == 30``, so the
  two branches agree at the boundary.

This is a deliberate v1 approximation (a real Asian-option-style treatment of the averaging window is future
work), not a claim that BRTI genuinely follows this process.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

CLAMP_LOW = 0.02
CLAMP_HIGH = 0.98
SETTLEMENT_WINDOW_SEC = 60.0
SETTLEMENT_MIDPOINT_LAG_SEC = 30.0
MIN_HORIZON_SEC = 1.0


def _clamp(p: float) -> float:
    return min(CLAMP_HIGH, max(CLAMP_LOW, p))


def normal_cdf(x: float) -> float:
    """Standard normal CDF via erf; no scipy dependency needed. Handles +-inf (a zero-sigma degenerate case)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# --------------------------------------------------------------------------- v1 model


@dataclass(frozen=True, slots=True)
class ModelState:
    spot: Decimal  # current proxy spot (e.g. Coinbase BTC-USD)
    strike: Decimal  # floor_strike: the opening 60s average
    tau_sec: float  # seconds until close
    sigma: float  # per-second realized volatility (a fraction, e.g. 0.0004), never negative
    market_mid: Decimal | None = None  # the market's own YES mid, for blending
    observed_window_avg: Decimal | None = None  # avg spot over the elapsed part of the closing 60s window


def predict_p_yes(state: ModelState) -> float:
    """v1 fair-probability estimate for YES, clamped to [0.02, 0.98]. See the module docstring for the
    two-branch derivation."""
    if state.spot <= 0 or state.strike <= 0:
        raise ValueError("spot and strike must be positive")
    if state.sigma < 0:
        raise ValueError("sigma must not be negative")

    spot = float(state.spot)
    tau = max(state.tau_sec, 0.0)

    if state.tau_sec > SETTLEMENT_WINDOW_SEC:
        strike_eff = float(state.strike)
        horizon = max(state.tau_sec - SETTLEMENT_MIDPOINT_LAG_SEC, MIN_HORIZON_SEC)
    else:
        if state.tau_sec <= 0:
            # The window is fully observed; there is no residual uncertainty left to model.
            observed = float(state.observed_window_avg) if state.observed_window_avg is not None else spot
            return CLAMP_HIGH if observed >= float(state.strike) else CLAMP_LOW
        # Floor the divisor, not just the horizon: an unfloored tau arbitrarily close to (but above) zero
        # blows strike_eff up to +-inf by division, which then silently underflows spot/strike_eff to 0 and
        # crashes math.log. Flooring both by the same amount keeps elapsed/strike_eff/horizon consistent.
        tau_calc = max(tau, MIN_HORIZON_SEC)
        elapsed = min(max(SETTLEMENT_WINDOW_SEC - tau_calc, 0.0), SETTLEMENT_WINDOW_SEC)
        observed_avg = float(state.observed_window_avg) if state.observed_window_avg is not None else spot
        strike_eff = (SETTLEMENT_WINDOW_SEC * float(state.strike) - elapsed * observed_avg) / tau_calc
        if strike_eff <= 0:
            # Even a remainder averaging to (the impossible) zero would still clear the strike.
            return CLAMP_HIGH
        horizon = max(tau_calc / 2.0, MIN_HORIZON_SEC)

    log_ratio = math.log(spot / strike_eff)
    if state.sigma == 0:
        d = math.inf if log_ratio > 0 else (-math.inf if log_ratio < 0 else 0.0)
    else:
        d = log_ratio / (state.sigma * math.sqrt(horizon))
    return _clamp(normal_cdf(d))


def blend_with_market(p_model: float, market_mid: Decimal | None, *, blend: float = 0.5) -> float:
    """``blend * p_model + (1 - blend) * p_market_mid``. Falls back to ``p_model`` with no market mid."""
    if not 0.0 <= blend <= 1.0:
        raise ValueError("blend must be in [0, 1]")
    if market_mid is None:
        return p_model
    return blend * p_model + (1.0 - blend) * float(market_mid)


def predict(state: ModelState, *, blend: float = 0.5) -> tuple[float, float]:
    """Returns ``(p_model, p_blend)``. ``predict_p_yes`` is the plug-in interface spec section 4 asks for;
    this wraps it with the market-mid blend so callers get both numbers for logging."""
    p_model = predict_p_yes(state)
    return p_model, blend_with_market(p_model, state.market_mid, blend=blend)


# --------------------------------------------------------------------------- realized volatility


@dataclass
class EwmaVolatility:
    """EWMA estimate of per-second volatility from a stream of 1s log returns.

    Uses the conventional span-based smoothing factor ``alpha = 2 / (span_sec + 1)`` (as in pandas'
    ``ewm(span=...)``); ``span_sec`` is spec section 4's "last N minutes" window, in seconds
    (``config.yaml``'s ``vol_window_sec``, default 900).
    """

    span_sec: float
    _variance: float = field(default=0.0, init=False, repr=False)
    _seen: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.span_sec <= 0:
            raise ValueError("span_sec must be positive")

    @property
    def alpha(self) -> float:
        return 2.0 / (self.span_sec + 1.0)

    def update(self, log_return: float, *, elapsed_sec: float = 1.0) -> None:
        if not math.isfinite(elapsed_sec) or elapsed_sec <= 0:
            raise ValueError("elapsed_sec must be positive and finite")
        squared = log_return * log_return / elapsed_sec
        if not self._seen:
            self._variance = squared
            self._seen = True
        else:
            a = 1.0 - (1.0 - self.alpha) ** elapsed_sec
            self._variance = a * squared + (1.0 - a) * self._variance

    @property
    def sigma(self) -> float:
        return math.sqrt(self._variance)


class TimedVolatility:
    """Per-second EWMA from completed receive-time buckets, not individual trades.

    Only the last price received in each second is used. A later second closes the
    previous bucket; future ticks cannot revise it. Gaps over three seconds reset
    the warmup rather than treating an outage as a low-volatility market.
    """

    def __init__(self, span_sec: float, warmup_sec: int = 60):
        self.span_sec = span_sec
        self.warmup_sec = warmup_sec
        self._vol = EwmaVolatility(span_sec)
        self._bucket: int | None = None
        self._price: Decimal | None = None
        self._previous: tuple[int, Decimal] | None = None
        self._elapsed = 0

    def update(self, price: Decimal, ts: datetime) -> None:
        if not price.is_finite() or price <= 0:
            raise ValueError("price must be positive and finite")
        second = math.floor(ts.timestamp())
        if self._bucket is not None and second < self._bucket:
            return
        if self._bucket is not None and second > self._bucket:
            if second - self._bucket > 3:
                self._vol = EwmaVolatility(self.span_sec)
                self._previous = None
                self._elapsed = 0
            else:
                if self._previous is not None:
                    prev_second, prev_price = self._previous
                    elapsed = self._bucket - prev_second
                    self._vol.update(log_return(prev_price, self._price), elapsed_sec=elapsed)
                    self._elapsed += elapsed
                self._previous = (self._bucket, self._price)
        self._bucket, self._price = second, price

    @property
    def sigma(self) -> float:
        return self._vol.sigma

    @property
    def ready(self) -> bool:
        return self._elapsed >= self.warmup_sec


def log_return(previous: Decimal, current: Decimal) -> float:
    if previous <= 0 or current <= 0:
        raise ValueError("prices must be positive")
    return math.log(float(current) / float(previous))


def ewma_sigma_from_prices(prices: Sequence[Decimal], *, span_sec: float) -> float:
    """Feed consecutive (~1s-spaced) prices through :class:`EwmaVolatility`; return the final sigma. 0.0 for
    fewer than two prices (no return observed yet)."""
    vol = EwmaVolatility(span_sec)
    for previous, current in zip(prices, prices[1:]):
        vol.update(log_return(previous, current))
    return vol.sigma


# --------------------------------------------------------------------------- prediction logging


PREDICTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    ts TEXT NOT NULL,
    spot TEXT NOT NULL,
    strike TEXT NOT NULL,
    tau_sec REAL NOT NULL,
    sigma REAL NOT NULL,
    market_mid TEXT,
    observed_window_avg TEXT,
    blend REAL NOT NULL,
    p_model REAL NOT NULL,
    p_blend REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_predictions_ticker_ts ON predictions (ticker, ts);
"""


@dataclass(frozen=True, slots=True)
class Prediction:
    ticker: str
    ts: datetime
    state: ModelState
    blend: float
    p_model: float
    p_blend: float


def init_predictions_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(PREDICTIONS_SCHEMA)
    conn.commit()


def log_prediction(conn: sqlite3.Connection, prediction: Prediction) -> None:
    state = prediction.state
    conn.execute(
        """INSERT INTO predictions
           (ticker, ts, spot, strike, tau_sec, sigma, market_mid, observed_window_avg, blend, p_model, p_blend)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            prediction.ticker,
            prediction.ts.astimezone(timezone.utc).isoformat(),
            str(state.spot),
            str(state.strike),
            state.tau_sec,
            state.sigma,
            None if state.market_mid is None else str(state.market_mid),
            None if state.observed_window_avg is None else str(state.observed_window_avg),
            prediction.blend,
            prediction.p_model,
            prediction.p_blend,
        ),
    )
    conn.commit()


# --------------------------------------------------------------------------- calibration


@dataclass(frozen=True, slots=True)
class CalibrationRow:
    bin_low: float
    bin_high: float
    count: int
    mean_predicted: float | None
    observed_rate: float | None


@dataclass(frozen=True, slots=True)
class CalibrationSummary:
    label: str
    n: int
    brier_score: float | None
    reliability: tuple[CalibrationRow, ...]


class CalibrationError(Exception):
    """No predictions, or no resolved settlements to score them against."""


def brier_score(pairs: Sequence[tuple[float, bool]]) -> float:
    """Mean squared error between predicted P(YES) and the 0/1 outcome. 0 is perfect; a nothing-scored
    (predict-nothing-but-the-actual-YES-rate) baseline is bounded by 0.25 at p=0.5 for a balanced series."""
    if not pairs:
        raise CalibrationError("no predictions to score")
    total = sum((p - (1.0 if outcome else 0.0)) ** 2 for p, outcome in pairs)
    return total / len(pairs)


def reliability_table(pairs: Sequence[tuple[float, bool]], *, bins: int = 10) -> tuple[CalibrationRow, ...]:
    """Equal-width bins over [0, 1]; a well-calibrated model has ``observed_rate`` close to ``mean_predicted``
    in every populated bin."""
    if bins <= 0:
        raise ValueError("bins must be positive")
    edges = [i / bins for i in range(bins + 1)]
    rows = []
    for i in range(bins):
        low, high = edges[i], edges[i + 1]
        in_bin = [(p, outcome) for p, outcome in pairs if (low <= p < high) or (i == bins - 1 and p == high)]
        if in_bin:
            mean_predicted = sum(p for p, _ in in_bin) / len(in_bin)
            observed_rate = sum(1 for _, outcome in in_bin if outcome) / len(in_bin)
        else:
            mean_predicted = None
            observed_rate = None
        rows.append(CalibrationRow(low, high, len(in_bin), mean_predicted, observed_rate))
    return tuple(rows)


_CALIBRATION_COLUMNS = {"model": "p_model", "blend": "p_blend", "market": "market_mid"}


def load_calibration_pairs(conn: sqlite3.Connection, *, which: str) -> list[tuple[float, bool]]:
    """Join ``predictions`` to ``recorder.py``'s ``settlements`` table by ticker. Only resolved settlements
    (``result`` is not null) are included; an unresolved window is left out, not scored as a loss."""
    try:
        column = _CALIBRATION_COLUMNS[which]
    except KeyError:
        raise ValueError(f"which must be one of {sorted(_CALIBRATION_COLUMNS)}, got {which!r}") from None
    rows = conn.execute(
        f"""
        SELECT predictions.{column}, settlements.result
        FROM predictions
        JOIN settlements ON settlements.ticker = predictions.ticker
        WHERE settlements.result IS NOT NULL AND predictions.{column} IS NOT NULL
        """
    ).fetchall()
    return [(float(p), result == "yes") for p, result in rows]


def compute_calibration_report(conn: sqlite3.Connection, *, bins: int = 10) -> tuple[CalibrationSummary, ...]:
    """One :class:`CalibrationSummary` per label ("model", "market", "blend"), in that order. A label with
    no resolved pairs gets ``n=0`` and ``brier_score=None`` rather than raising, so a partial report (e.g. no
    market mid ever logged) can still be produced."""
    summaries = []
    for label in ("model", "market", "blend"):
        pairs = load_calibration_pairs(conn, which=label)
        if pairs:
            summaries.append(CalibrationSummary(label, len(pairs), brier_score(pairs), reliability_table(pairs, bins=bins)))
        else:
            summaries.append(CalibrationSummary(label, 0, None, ()))
    return tuple(summaries)
