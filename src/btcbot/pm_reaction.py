"""How Polymarket's BTC Up/Down market reacts to BTC price events, and what a learned model can (and cannot)
add on top of the market's own price (``btcbot pm-reaction``, docs/research/polymarket-reaction-data.md).

Offline research over a :mod:`btcbot.pm_history` database (Polymarket trade tape + Binance 1s klines), or a
:mod:`btcbot.polymarket_recorder` database for the Polymarket side. No network, no key, no order code, and
nothing here is wired into ``strategy.py``, ``btcbot lab``, ``btcbot paper``/``demo`` or any Kalshi decision
(CLAUDE.md's Polymarket bullet). Four outputs:

1. **Data validity** (:func:`validity_report`): coverage, trade-tape truncation, Binance gaps, how often a
   Binance-derived up/down agrees with Polymarket's own resolution (Polymarket settles on Chainlink's BTC/USD
   stream, not Binance, so this measures the basis rather than assuming it away), whether Up and Down prints
   in the same second are complementary, and whether the lead-lag peak sits where physics says it should
   (Polymarket following Binance, never leading it -- a negative peak means a clock problem, not a discovery).
2. **Lead-lag** (:func:`lead_lag`): correlation of Binance 1s returns with Polymarket 1s price changes at each
   lag. Published millisecond data puts the median response near 350 ms (OpenMarket, arXiv 2607.26245), so at
   this dataset's 1-second resolution most of it should land at lag 0-1.
3. **Event study** (:func:`event_study`): the average Polymarket response curve after BTC shocks (a move of
   ``k_sigma`` sigmas inside ``lookback`` seconds) and after BTC crossing the window's opening price, next to
   the move a driftless-lognormal fair value says it SHOULD have made -- "how much of the move is priced in,
   and how fast".
4. **Two models** on :class:`btcbot.ml_model.LogisticModel` (the project's own dependency-free logistic
   regression), each judged on LATER windows it never trained on, against the baseline that matters:
   - ``outcome``: P(Up wins | state now). Baseline: Polymarket's own price. Prior work found a 43-feature
     logistic model *slightly worse* than the Polymarket mid out-of-sample (OpenMarket); ``beats_market`` is the
     only number that says otherwise, and a calibration measure is still not a trading edge.
   - ``reaction``: P(Polymarket's Up price is higher in ``horizon_sec`` seconds | state now), only on rows
     where it moved by at least ``min_move``. Baseline: the same model on :data:`REACTION_CONTROL_FEATURES`
     -- Polymarket's own history, the side of its last print, and BTC's move SINCE that print. Without the
     last two, a perfectly efficient market still "loses" to BTC information (verified on synthetic data with
     zero planted lag: bid-ask bounce and within-second timing alone gave t < -6), so ``beats_control`` only
     says YES when BTC information from BEFORE the last print still predicts the next move -- a Granger-style
     test of a slow reaction, not a PnL claim (a move of a cent or two is inside the spread and fees).

**Stale prints.** A trade tape is sparse: if Polymarket last printed 40 s ago, the next print "reacts" to 40 s
of BTC movement regardless of how fast the quotes actually moved, which would make the reaction model look far
better than the market really is. Rows are therefore only kept when the last Polymarket print is at most
``max_staleness_sec`` old; do not loosen that without reading this paragraph again.

Timing convention (both venues, no lookahead): the value "at instant t" is what traded in the second ENDING at
t -- Binance's 1s bar opening at t - 1 and Polymarket prints stamped t - 1 -- forward-filled.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from btcbot.ml_model import LogisticModel, fit
from btcbot.model import normal_cdf
from btcbot.pm_history import HistTrade, HistWindow, load_btc, load_trades, load_windows, parse_slug

PRE_SEC = 300  # BTC/Polymarket history kept before each window, for sigma and lookbacks
VOL_WINDOW_SEC = 300
MIN_VOL_SAMPLES = 30
SIGMA_FLOOR = 1e-6  # per-second; BTC is never literally flat for 5 minutes, but a data gap can look like it
P_CLAMP = 0.01
LOOKBACKS = (5, 15, 30, 60)
STALE_CAP_SEC = 120
MAX_EDGE_GAP_SEC = 5  # a real Binance bar must exist this close to the window's start and end
MAX_MISSING_SEC = 60  # more forward-filled Binance seconds than this inside a window and it is dropped
DEFAULT_LAGS = (0, 1, 2, 3, 5, 10, 15, 30, 60, 120)
MIN_WINDOWS_TO_TRAIN = 20
MIN_VALIDATE_ROWS = 30

OUTCOME_FEATURES = (
    "logit_pm", "logit_fair", "tau_sec", "sigma", "btc_ret_30s", "pm_move_30s", "pm_flow_60s", "btc_flow_30s",
)
# The reaction CONTROL: everything that would make Polymarket's next print predictable even if its quotes
# absorbed every BTC move instantly -- its own recent history, the side of its last print (bid-ask bounce: a
# print at the ask tends to be followed by one nearer the bid), and BTC's move SINCE that print (which no print
# could have shown yet; at 1 s resolution this also absorbs within-second timing). The full model adds BTC
# information from BEFORE the last print; only that surviving the control counts as a slow reaction.
REACTION_CONTROL_FEATURES = (
    "pm_move_5s", "pm_move_15s", "pm_move_30s", "pm_move_60s", "pm_flow_60s", "pm_staleness_sec", "tau_sec",
    "pm_last_side", "btc_ret_since_print",
)
REACTION_FEATURES = REACTION_CONTROL_FEATURES + (
    "fair_minus_pm", "btc_ret_5s", "btc_ret_15s", "btc_ret_30s", "btc_ret_60s", "btc_flow_30s", "sigma",
)


class PmReactionError(Exception):
    """Not enough usable data for the requested analysis, or bad parameters."""


def _clamp_p(p: float) -> float:
    return min(1.0 - P_CLAMP, max(P_CLAMP, p))


def logit(p: float) -> float:
    p = _clamp_p(p)
    return math.log(p / (1.0 - p))


# --------------------------------------------------------------------------- per-window series


def _prefix(values: Sequence[float]) -> list[float]:
    out = [0.0]
    for v in values:
        out.append(out[-1] + v)
    return out


@dataclass
class WindowSeries:
    """Per-second arrays for one window: index ``i`` is the instant ``t0 + i``, ``t0 = start - pre``."""

    slug: str
    start: int
    end: int
    result_up: bool | None
    truncated: bool
    t0: int
    btc: list[float | None]
    btc_missing: int  # Binance bars absent inside the window (forward-filled over)
    pm: list[float | None]
    last_trade_idx: list[int]  # index of the latest second with a Polymarket print, -1 if none yet
    sigma: list[float]
    p_fair: list[float | None]  # None before the window starts
    complement_gaps: list[float]
    trade_count: int
    _pm_flow: list[float] = field(repr=False, default_factory=list)  # prefix sums
    _pm_absflow: list[float] = field(repr=False, default_factory=list)
    _pm_count: list[float] = field(repr=False, default_factory=list)
    _btc_flow: list[float] = field(repr=False, default_factory=list)
    _btc_vol: list[float] = field(repr=False, default_factory=list)

    @property
    def start_idx(self) -> int:
        return self.start - self.t0

    @property
    def end_idx(self) -> int:
        return self.end - self.t0

    @property
    def btc_open(self) -> float | None:
        return self.btc[self.start_idx]

    @property
    def btc_close(self) -> float | None:
        return self.btc[self.end_idx]

    def binance_up(self) -> bool | None:
        o, c = self.btc_open, self.btc_close
        return None if o is None or c is None else c >= o

    def pm_flow_sum(self, i: int, n: int) -> tuple[float, float, float]:
        """(signed flow, |flow|, print count) over the ``n`` seconds ending at instant ``i``."""
        lo = max(0, i - n + 1)
        return (self._pm_flow[i + 1] - self._pm_flow[lo], self._pm_absflow[i + 1] - self._pm_absflow[lo],
                self._pm_count[i + 1] - self._pm_count[lo])

    def btc_flow_ratio(self, i: int, n: int) -> float:
        lo = max(0, i - n + 1)
        vol = self._btc_vol[i + 1] - self._btc_vol[lo]
        return (self._btc_flow[i + 1] - self._btc_flow[lo]) / vol if vol > 0 else 0.0


def build_series(
    window: HistWindow,
    trades: Sequence[HistTrade],
    btc_bars: Mapping[int, tuple[float, float, float]],
    *,
    pre: int = PRE_SEC,
    vol_window: int = VOL_WINDOW_SEC,
    max_missing_sec: int = MAX_MISSING_SEC,
) -> WindowSeries | None:
    """None when Binance cannot price the window: no real bar within ``MAX_EDGE_GAP_SEC`` of its start or end,
    or more than ``max_missing_sec`` forward-filled seconds inside it. Forward-filling is for the odd missing
    second, never for a data gap -- without this a window with no Binance data at all would inherit a stale
    pre-window price as both its "open" and its "close"."""
    t0 = window.start - pre
    n = window.end - t0 + 1

    btc: list[float | None] = [None] * n
    btc_flow = [0.0] * n
    btc_vol = [0.0] * n
    missing = 0
    last: float | None = None
    last_real = [-10**9] * n
    real_idx = -10**9
    for i in range(n):
        bar = btc_bars.get(t0 + i - 1)
        if bar is not None and bar[0] > 0:
            last = bar[0]
            real_idx = i
            btc_vol[i] = bar[1]
            btc_flow[i] = 2.0 * bar[2] - bar[1]
        elif i > pre:
            missing += 1
        btc[i] = last
        last_real[i] = real_idx
    if btc[pre] is None or btc[n - 1] is None or missing > max_missing_sec:
        return None
    if pre - last_real[pre] > MAX_EDGE_GAP_SEC or (n - 1) - last_real[n - 1] > MAX_EDGE_GAP_SEC:
        return None

    # Polymarket prints stamped s belong to instant s + 1 (index s + 1 - t0).
    buckets: dict[int, list[HistTrade]] = {}
    initial: float | None = None
    for tr in trades:
        idx = tr.ts + 1 - t0
        if idx < 0:
            initial = tr.up_price  # trades arrive sorted, so this ends as the last print before the series
        elif idx < n:
            buckets.setdefault(idx, []).append(tr)
    pm: list[float | None] = [None] * n
    last_trade_idx = [-1] * n
    pm_flow = [0.0] * n
    pm_absflow = [0.0] * n
    pm_count = [0.0] * n
    complement_gaps: list[float] = []
    cur, cur_idx = initial, -1
    for i in range(n):
        group = buckets.get(i)
        if group:
            weight = sum(abs(t.up_flow) for t in group)
            cur = (sum(t.up_price * abs(t.up_flow) for t in group) / weight) if weight > 0 else (
                sum(t.up_price for t in group) / len(group))
            cur_idx = i
            pm_flow[i] = sum(t.up_flow for t in group)
            pm_absflow[i] = weight
            pm_count[i] = float(len(group))
            ups = [t.up_price for t in group if t.outcome == "up"]
            downs = [t.up_price for t in group if t.outcome == "down"]
            if ups and downs:
                complement_gaps.append(abs(sum(ups) / len(ups) - sum(downs) / len(downs)))
        pm[i] = cur
        last_trade_idx[i] = cur_idx

    # Realized per-second vol over a trailing window, via prefix sums of 1s log returns.
    rets = [0.0] * n
    have = [0.0] * n
    for i in range(1, n):
        a, b = btc[i - 1], btc[i]
        if a is not None and b is not None and a > 0 and b > 0:
            rets[i] = math.log(b / a)
            have[i] = 1.0
    s1, s2, cnt = _prefix(rets), _prefix([r * r for r in rets]), _prefix(have)
    sigma = [SIGMA_FLOOR] * n
    for i in range(n):
        lo = max(0, i - vol_window + 1)
        k = cnt[i + 1] - cnt[lo]
        if k >= MIN_VOL_SAMPLES:
            mean = (s1[i + 1] - s1[lo]) / k
            var = (s2[i + 1] - s2[lo]) / k - mean * mean
            sigma[i] = max(math.sqrt(max(var, 0.0)), SIGMA_FLOOR)

    # Driftless-lognormal fair P(Up) inside the window: Up wins when the END price >= the START price
    # (Polymarket's rule, one print at each end -- unlike Kalshi's 60 s settlement average, see btcbot.model).
    btc_open = btc[pre]
    p_fair: list[float | None] = [None] * n
    for i in range(pre, n):
        price = btc[i]
        if price is None or btc_open is None:
            continue
        tau = window.end - (t0 + i)
        if tau <= 0:
            p_fair[i] = 1.0 if price >= btc_open else 0.0
        else:
            z = math.log(price / btc_open) / (sigma[i] * math.sqrt(tau))
            p_fair[i] = normal_cdf(max(-8.0, min(8.0, z)))

    return WindowSeries(
        slug=window.slug, start=window.start, end=window.end, result_up=window.result_up, truncated=window.truncated,
        t0=t0, btc=btc, btc_missing=missing, pm=pm, last_trade_idx=last_trade_idx, sigma=sigma, p_fair=p_fair,
        complement_gaps=complement_gaps, trade_count=len(trades),
        _pm_flow=_prefix(pm_flow), _pm_absflow=_prefix(pm_absflow), _pm_count=_prefix(pm_count),
        _btc_flow=_prefix(btc_flow), _btc_vol=_prefix(btc_vol),
    )


def features_at(ws: WindowSeries, i: int) -> dict[str, float] | None:
    """Model inputs at instant ``t0 + i`` from information available at that instant only. None when a
    required value is missing (no Polymarket print yet, no fair value, not enough lookback)."""
    pm, price, pf = ws.pm[i], ws.btc[i], ws.p_fair[i]
    if pm is None or price is None or pf is None or i < max(LOOKBACKS):
        return None
    sigma = ws.sigma[i]
    feats: dict[str, float] = {
        "tau_sec": float(ws.end - (ws.t0 + i)), "sigma": sigma, "p_fair": pf, "pm_up": pm,
        "logit_pm": logit(pm), "logit_fair": logit(pf), "fair_minus_pm": pf - pm,
    }
    for k in LOOKBACKS:
        prev_btc = ws.btc[i - k]
        feats[f"btc_ret_{k}s"] = math.log(price / prev_btc) / (sigma * math.sqrt(k)) if prev_btc else 0.0
        prev_pm = ws.pm[i - k]
        feats[f"pm_move_{k}s"] = pm - prev_pm if prev_pm is not None else 0.0
    flow, absflow, count = ws.pm_flow_sum(i, 60)
    feats["pm_flow_60s"] = flow / absflow if absflow > 0 else 0.0
    feats["log_trades_60s"] = math.log1p(count)
    feats["btc_flow_30s"] = ws.btc_flow_ratio(i, 30)
    last = ws.last_trade_idx[i]
    feats["pm_staleness_sec"] = float(min(i - last, STALE_CAP_SEC)) if last >= 0 else float(STALE_CAP_SEC)
    last_flow = ws.pm_flow_sum(last, 1)[0] if last >= 0 else 0.0
    feats["pm_last_side"] = 1.0 if last_flow > 0 else (-1.0 if last_flow < 0 else 0.0)
    before = ws.btc[last - 1] if last >= 1 else None  # BTC just before the second the last print happened in
    feats["btc_ret_since_print"] = (
        math.log(price / before) / (sigma * math.sqrt(i - last + 1)) if before else 0.0)
    return feats


@dataclass(frozen=True, slots=True)
class Example:
    slug: str
    window_start: int
    t: int
    features: dict[str, float]
    outcome_up: bool | None
    future_move: float | None  # pm(t + h) - pm(t) when Polymarket printed in (t, t + h], else None


def build_examples(
    series: Iterable[WindowSeries],
    *,
    sample_every: int = 15,
    horizon_sec: int = 15,
    warmup_sec: int = 60,
    max_staleness_sec: int = 5,
) -> list[Example]:
    """Rows sampled every ``sample_every`` seconds from ``warmup_sec`` into each window until ``horizon_sec``
    before its end, kept only where the last Polymarket print is at most ``max_staleness_sec`` old (see the
    module docstring on stale prints)."""
    if sample_every <= 0 or horizon_sec <= 0 or warmup_sec < 0 or max_staleness_sec < 0:
        raise PmReactionError("sample_every/horizon_sec must be positive, warmup/max_staleness non-negative")
    out: list[Example] = []
    for ws in series:
        length = ws.end - ws.start
        for s in range(warmup_sec, length - horizon_sec + 1, sample_every):
            i = ws.start_idx + s
            feats = features_at(ws, i)
            if feats is None or feats["pm_staleness_sec"] > max_staleness_sec:
                continue
            j = i + horizon_sec
            move = None
            future = ws.pm[j]
            if ws.last_trade_idx[j] > i and future is not None:
                move = future - feats["pm_up"]
            out.append(Example(ws.slug, ws.start, ws.t0 + i, feats, ws.result_up, move))
    return out


# --------------------------------------------------------------------------- validation split + scoring


def split_by_window(
    examples: Sequence[Example], train_fraction: float, *, embargo: int = 1,
) -> tuple[list[Example], list[Example], int, int]:
    """Earlier windows train, later windows validate, ``embargo`` windows dropped at the boundary, grouped by
    whole window -- the same discipline as btcbot.lab.split_windows / market_level_pipeline.split_markets_by_time.
    Returns ``(train, validate, n_train_windows, n_validate_windows)``."""
    if not 0.2 <= train_fraction <= 0.9:
        raise PmReactionError("train fraction must be between 0.2 and 0.9")
    starts = sorted({e.window_start for e in examples})
    if len(starts) < MIN_WINDOWS_TO_TRAIN:
        raise PmReactionError(
            f"only {len(starts)} usable windows; need at least {MIN_WINDOWS_TO_TRAIN} to split into train and "
            "validate (and thousands before a result means much)"
        )
    cut = max(1, min(len(starts) - embargo - 1, int(len(starts) * train_fraction)))
    train_set, val_set = set(starts[:cut]), set(starts[cut + embargo:])
    return ([e for e in examples if e.window_start in train_set], [e for e in examples if e.window_start in val_set],
            len(train_set), len(val_set))


@dataclass(frozen=True, slots=True)
class Score:
    n: int
    brier: float
    log_loss: float
    accuracy: float


def score(probs: Sequence[float], labels: Sequence[bool]) -> Score:
    if not probs or len(probs) != len(labels):
        raise PmReactionError("score needs matching, non-empty predictions and labels")
    brier = ll = hits = 0.0
    for p, y in zip(probs, labels):
        t = 1.0 if y else 0.0
        brier += (p - t) ** 2
        pc = min(1 - 1e-6, max(1e-6, p))
        ll -= t * math.log(pc) + (1 - t) * math.log(1 - pc)
        hits += 1.0 if (p >= 0.5) == y else 0.0
    n = len(probs)
    return Score(n, brier / n, ll / n, hits / n)


SIGNIFICANCE_T = 2.0


def paired_window_t(
    examples: Sequence[Example], probs_a: Sequence[float], probs_b: Sequence[float], labels: Sequence[bool],
) -> float | None:
    """t-statistic of the per-WINDOW mean squared-error difference (a minus b), across windows. Rows inside one
    window share one outcome and are heavily correlated, so the window, not the row, is the independent unit;
    a negative t means ``a`` has lower error. None with fewer than 3 windows or zero spread."""
    per: dict[int, list[float]] = {}
    for e, pa, pb, y in zip(examples, probs_a, probs_b, labels):
        t = 1.0 if y else 0.0
        per.setdefault(e.window_start, []).append((pa - t) ** 2 - (pb - t) ** 2)
    diffs = [sum(v) / len(v) for v in per.values()]
    if len(diffs) < 3:
        return None
    sd = statistics.stdev(diffs)
    return None if sd == 0 else statistics.fmean(diffs) / (sd / math.sqrt(len(diffs)))


def _beats(t: float | None) -> bool:
    return t is not None and t <= -SIGNIFICANCE_T


def _thin(rows: list, max_rows: int | None) -> list:
    """Deterministic, evenly-strided subsample (pure-Python gradient descent cost scales with rows)."""
    if max_rows is None or len(rows) <= max_rows:
        return rows
    step = len(rows) / max_rows
    return [rows[int(k * step)] for k in range(max_rows)]


@dataclass(frozen=True, slots=True)
class OutcomeReport:
    windows_train: int
    windows_validate: int
    train: Score
    validate: Score
    market_validate: Score  # Polymarket's own price, as a probability, on the same rows
    fair_validate: Score  # the driftless-lognormal fair value alone
    vs_market_t: float | None  # window-grouped paired t of squared error, model minus market (negative = model better)
    beats_market: bool  # only when vs_market_t <= -2: a lower Brier by noise alone does not count
    weights: dict[str, float]


def train_outcome_model(
    examples: Sequence[Example], *, train_fraction: float = 0.7, embargo: int = 1, epochs: int = 200,
    max_train_rows: int | None = 200_000,
) -> tuple[LogisticModel, OutcomeReport]:
    usable = [e for e in examples if e.outcome_up is not None]
    train, val, n_tr, n_va = split_by_window(usable, train_fraction, embargo=embargo)
    if len(val) < MIN_VALIDATE_ROWS:
        raise PmReactionError(f"only {len(val)} validate rows; need at least {MIN_VALIDATE_ROWS}")
    fit_rows = _thin(train, max_train_rows)
    model = fit([e.features for e in fit_rows], [bool(e.outcome_up) for e in fit_rows], OUTCOME_FEATURES, epochs=epochs)
    y_tr = [bool(e.outcome_up) for e in train]
    y_va = [bool(e.outcome_up) for e in val]
    model_p = [model.predict_proba(e.features) for e in val]
    market_p = [e.features["pm_up"] for e in val]
    t = paired_window_t(val, model_p, market_p, y_va)
    report = OutcomeReport(
        windows_train=n_tr, windows_validate=n_va,
        train=score([model.predict_proba(e.features) for e in train], y_tr), validate=score(model_p, y_va),
        market_validate=score(market_p, y_va), fair_validate=score([e.features["p_fair"] for e in val], y_va),
        vs_market_t=t, beats_market=_beats(t), weights=dict(zip(model.feature_names, model.weights)),
    )
    return model, report


@dataclass(frozen=True, slots=True)
class ReactionReport:
    horizon_sec: int
    min_move: float
    windows_train: int
    windows_validate: int
    base_rate_train: float  # share of training rows where Polymarket's price went UP
    train: Score
    validate: Score
    control_validate: Score  # same model family on REACTION_CONTROL_FEATURES only
    constant_validate: Score  # always predict the training base rate
    vs_control_t: float | None  # window-grouped paired t, full minus control (negative = earlier BTC info helps)
    beats_control: bool  # only when vs_control_t <= -2
    weights: dict[str, float]


def train_reaction_model(
    examples: Sequence[Example], *, horizon_sec: int, min_move: float = 0.01, train_fraction: float = 0.7,
    embargo: int = 1, epochs: int = 200, max_train_rows: int | None = 200_000,
) -> tuple[LogisticModel, ReactionReport]:
    moved = [e for e in examples if e.future_move is not None and abs(e.future_move) >= min_move]
    train, val, n_tr, n_va = split_by_window(moved, train_fraction, embargo=embargo)
    if len(val) < MIN_VALIDATE_ROWS or len(train) < MIN_VALIDATE_ROWS:
        raise PmReactionError(f"only {len(train)} train / {len(val)} validate rows where Polymarket moved >= {min_move}")
    fit_rows = _thin(train, max_train_rows)
    labels_fit = [e.future_move > 0 for e in fit_rows]  # type: ignore[operator]
    full = fit([e.features for e in fit_rows], labels_fit, REACTION_FEATURES, epochs=epochs)
    control = fit([e.features for e in fit_rows], labels_fit, REACTION_CONTROL_FEATURES, epochs=epochs)
    y_tr = [e.future_move > 0 for e in train]  # type: ignore[operator]
    y_va = [e.future_move > 0 for e in val]  # type: ignore[operator]
    base = sum(y_tr) / len(y_tr)
    full_p = [full.predict_proba(e.features) for e in val]
    control_p = [control.predict_proba(e.features) for e in val]
    t = paired_window_t(val, full_p, control_p, y_va)
    report = ReactionReport(
        horizon_sec=horizon_sec, min_move=min_move, windows_train=n_tr, windows_validate=n_va, base_rate_train=base,
        train=score([full.predict_proba(e.features) for e in train], y_tr), validate=score(full_p, y_va),
        control_validate=score(control_p, y_va), constant_validate=score([base] * len(val), y_va),
        vs_control_t=t, beats_control=_beats(t), weights=dict(zip(full.feature_names, full.weights)),
    )
    return full, report


# --------------------------------------------------------------------------- events


@dataclass(frozen=True, slots=True)
class Event:
    slug: str
    idx: int
    direction: int  # +1 BTC moved up / crossed above the open, -1 the reverse
    kind: str


def detect_btc_shocks(
    ws: WindowSeries, *, lookback: int = 5, k_sigma: float = 3.0, cooldown: int = 30, warmup: int = 60, min_tau: int = 60,
) -> list[Event]:
    """A BTC move of at least ``k_sigma`` sigmas inside ``lookback`` seconds. Sigma is taken from BEFORE the
    move, so a shock cannot raise its own bar; ``cooldown`` keeps one event per burst."""
    events: list[Event] = []
    last = -10**9
    for i in range(ws.start_idx + warmup, ws.end_idx - min_tau + 1):
        a, b = ws.btc[i - lookback], ws.btc[i]
        if a is None or b is None or i - last < cooldown:
            continue
        r = math.log(b / a)
        if abs(r) >= k_sigma * ws.sigma[i - lookback] * math.sqrt(lookback):
            events.append(Event(ws.slug, i, 1 if r > 0 else -1, "btc_shock"))
            last = i
    return events


def detect_strike_crosses(
    ws: WindowSeries, *, cooldown: int = 30, warmup: int = 60, min_tau: int = 60,
) -> list[Event]:
    """BTC crossing the window's opening price: the moment the "fair" answer flips sides."""
    events: list[Event] = []
    ref = ws.btc_open
    if ref is None:
        return events
    last = -10**9
    prev_side = 0
    for i in range(ws.start_idx + warmup - 1, ws.end_idx - min_tau + 1):
        price = ws.btc[i]
        side = 0 if price is None or price == ref else (1 if price > ref else -1)
        if side and prev_side and side != prev_side and i - last >= cooldown and i >= ws.start_idx + warmup:
            events.append(Event(ws.slug, i, side, "strike_cross"))
            last = i
        if side:
            prev_side = side
    return events


@dataclass(frozen=True, slots=True)
class ResponseRow:
    lag: int
    n: int
    pm_mean: float  # direction-signed Polymarket Up-price change since just before the event
    fair_mean: float  # the same for the lognormal fair value


@dataclass(frozen=True, slots=True)
class EventStudy:
    kind: str
    events: int
    rows: tuple[ResponseRow, ...]
    half_life_sec: int | None  # first lag reaching half of the 60 s response
    priced_in_60s: float | None  # pm_mean / fair_mean at 60 s


def event_study(
    series_by_slug: Mapping[str, WindowSeries], events: Sequence[Event], *, kind: str,
    lags: Sequence[int] = DEFAULT_LAGS, base_offset: int = 5,
) -> EventStudy:
    sums: dict[int, list[float]] = {lag: [0.0, 0.0, 0.0] for lag in lags}
    for ev in events:
        ws = series_by_slug.get(ev.slug)
        if ws is None:
            continue
        b = ev.idx - base_offset
        pm_b, pf_b = ws.pm[b], ws.p_fair[b]
        if pm_b is None or pf_b is None:
            continue
        for lag in lags:
            j = ev.idx + lag
            if j > ws.end_idx:
                continue
            pm_j, pf_j = ws.pm[j], ws.p_fair[j]
            if pm_j is None or pf_j is None:
                continue
            acc = sums[lag]
            acc[0] += 1
            acc[1] += ev.direction * (pm_j - pm_b)
            acc[2] += ev.direction * (pf_j - pf_b)
    rows = tuple(
        ResponseRow(lag, int(acc[0]), acc[1] / acc[0] if acc[0] else 0.0, acc[2] / acc[0] if acc[0] else 0.0)
        for lag, acc in sums.items()
    )
    ref = next((r for r in rows if r.lag == 60 and r.n), None)
    half_life = priced_in = None
    if ref is not None and ref.pm_mean > 0:
        half_life = next((r.lag for r in rows if r.n and r.pm_mean >= 0.5 * ref.pm_mean), None)
        priced_in = ref.pm_mean / ref.fair_mean if ref.fair_mean > 0 else None
    return EventStudy(kind, len(events), rows, half_life, priced_in)


# --------------------------------------------------------------------------- lead-lag


@dataclass(frozen=True, slots=True)
class LagCorr:
    lag: int  # positive: Polymarket's change comes AFTER Binance's return
    n: int
    corr: float | None


def lead_lag(series: Sequence[WindowSeries], *, max_lag: int = 10, max_windows: int | None = 2000) -> list[LagCorr]:
    """Pearson correlation of Binance 1s log returns with Polymarket 1s Up-price changes ``lag`` seconds later,
    pooled across windows (pairs never straddle two windows)."""
    acc = {lag: [0.0] * 6 for lag in range(-max_lag, max_lag + 1)}  # n, sx, sy, sxx, syy, sxy
    for ws in _thin(list(series), max_windows):
        lo, hi = ws.start_idx + 1, ws.end_idx
        xs: dict[int, float] = {}
        ys: dict[int, float] = {}
        for i in range(lo, hi + 1):
            a, b = ws.btc[i - 1], ws.btc[i]
            if a and b:
                xs[i] = math.log(b / a)
            p, q = ws.pm[i - 1], ws.pm[i]
            if p is not None and q is not None:
                ys[i] = q - p
        for lag, s in acc.items():
            for i, x in xs.items():
                y = ys.get(i + lag)
                if y is None:
                    continue
                s[0] += 1
                s[1] += x
                s[2] += y
                s[3] += x * x
                s[4] += y * y
                s[5] += x * y
    out = []
    for lag, (n, sx, sy, sxx, syy, sxy) in sorted(acc.items()):
        corr = None
        if n > 2:
            vx, vy = n * sxx - sx * sx, n * syy - sy * sy
            if vx > 0 and vy > 0:
                corr = (n * sxy - sx * sy) / math.sqrt(vx * vy)
        out.append(LagCorr(lag, int(n), corr))
    return out


def peak_lag(rows: Sequence[LagCorr]) -> int | None:
    scored = [r for r in rows if r.corr is not None]
    return max(scored, key=lambda r: r.corr).lag if scored else None  # type: ignore[arg-type,return-value]


# --------------------------------------------------------------------------- validity


@dataclass(frozen=True, slots=True)
class ValidityReport:
    windows_in_db: int
    windows_usable: int
    windows_no_btc: int
    truncated: int
    trades_total: int
    trades_median: float
    thin_windows: int  # fewer than min_trades prints
    btc_missing_seconds: int
    windows_with_btc_gaps: int
    label_checked: int
    label_agree: int
    move_median_bp: float | None  # |open->close| Binance move, all windows
    disagree_move_median_bp: float | None  # the same, only where Binance disagreed with the resolution
    complement_pairs: int
    complement_mean_gap: float | None
    lead_lag_peak: int | None
    warnings: tuple[str, ...]


def validity_report(
    windows_in_db: int, series: Sequence[WindowSeries], lags: Sequence[LagCorr], *, min_trades: int = 20,
) -> ValidityReport:
    usable = list(series)
    agree = checked = 0
    moves: list[float] = []
    disagree_moves: list[float] = []
    gaps: list[float] = []
    for ws in usable:
        gaps.extend(ws.complement_gaps)
        up = ws.binance_up()
        o, c = ws.btc_open, ws.btc_close
        if up is None or ws.result_up is None or not o or not c:
            continue
        checked += 1
        move_bp = abs(math.log(c / o)) * 1e4
        moves.append(move_bp)
        if up == ws.result_up:
            agree += 1
        else:
            disagree_moves.append(move_bp)
    counts = [ws.trade_count for ws in usable]
    truncated = sum(1 for ws in usable if ws.truncated)
    thin = sum(1 for c in counts if c < min_trades)
    missing = sum(ws.btc_missing for ws in usable)
    gap_windows = sum(1 for ws in usable if ws.btc_missing)
    peak = peak_lag(lags)
    warnings: list[str] = []
    no_btc = windows_in_db - len(usable)
    if windows_in_db and no_btc / windows_in_db > 0.05:
        warnings.append(f"{no_btc} of {windows_in_db} windows have no Binance price at their start/end: backfill BTC for the whole range")
    if checked and agree / checked < 0.9:
        warnings.append(
            f"Binance agrees with Polymarket's resolution on only {agree / checked:.1%} of windows: either the "
            "window alignment is wrong or the Binance-vs-Chainlink basis is too wide to use Binance as the price"
        )
    if usable and truncated / len(usable) > 0.05:
        warnings.append(f"{truncated} windows hit the Data API offset cap (oldest trades missing); their early minutes are unreliable")
    if usable and thin / len(usable) > 0.2:
        warnings.append(f"{thin} windows have fewer than {min_trades} prints: the trade tape is too thin for a price series there")
    if peak is not None and peak < 0:
        warnings.append(
            f"lead-lag peaks at {peak}s (Polymarket LEADING Binance): almost certainly a clock/alignment problem, not a finding"
        )
    if gaps and statistics.fmean(gaps) > 0.05:
        warnings.append("Up and Down prints in the same second disagree by more than 5 cents on average: check the Down->Up conversion")
    return ValidityReport(
        windows_in_db=windows_in_db, windows_usable=len(usable), windows_no_btc=no_btc, truncated=truncated,
        trades_total=sum(counts), trades_median=statistics.median(counts) if counts else 0.0, thin_windows=thin,
        btc_missing_seconds=missing, windows_with_btc_gaps=gap_windows, label_checked=checked, label_agree=agree,
        move_median_bp=statistics.median(moves) if moves else None,
        disagree_move_median_bp=statistics.median(disagree_moves) if disagree_moves else None,
        complement_pairs=len(gaps), complement_mean_gap=statistics.fmean(gaps) if gaps else None,
        lead_lag_peak=peak, warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------- loading


def load_series_from_history(conn: sqlite3.Connection, *, pre: int = PRE_SEC) -> tuple[int, list[WindowSeries]]:
    """Every resolved window in a download-polymarket-history database, as series. Returns
    ``(windows in the database, usable series)``; a window without Binance coverage is dropped."""
    windows = load_windows(conn)
    out = []
    for w in windows:
        ws = build_series(w, load_trades(conn, w.slug), load_btc(conn, w.start - pre - 1, w.end), pre=pre)
        if ws is not None:
            out.append(ws)
    return len(windows), out


def load_recorder_points(conn: sqlite3.Connection) -> tuple[list[HistWindow], dict[str, list[HistTrade]]]:
    """Windows and a per-poll Up MID series from a ``btcbot record-polymarket`` database (2 s book polls,
    no flow -- ``up_flow`` is 0, so the flow features are 0 for these windows). Window bounds come from the
    slug epoch, never the Gamma ``startDate``."""
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"pm_orderbook_snapshots", "pm_settlements"} <= have:
        raise PmReactionError("not a record-polymarket database (no pm_orderbook_snapshots / pm_settlements)")
    results = {slug: bool(r) for slug, r in conn.execute("SELECT event_slug, result_up FROM pm_settlements")}
    windows: list[HistWindow] = []
    points: dict[str, list[HistTrade]] = {}
    for slug, result in sorted(results.items()):
        parsed = parse_slug(slug)
        if parsed is None:
            continue
        horizon, start = parsed
        end = start + (300 if horizon == "5m" else 900)
        windows.append(HistWindow(slug, start, end, result, False))
        pts: list[HistTrade] = []
        for poll_ts, book_json in conn.execute(
            "SELECT poll_ts, book_json FROM pm_orderbook_snapshots WHERE event_slug = ? AND outcome = 'up' ORDER BY poll_ts", (slug,)
        ):
            book = json.loads(book_json)
            bids = [float(p) for p, _ in book.get("bids", [])]
            asks = [float(p) for p, _ in book.get("asks", [])]
            if not bids or not asks:
                continue
            ts = int(datetime.fromisoformat(poll_ts).astimezone(timezone.utc).timestamp())
            pts.append(HistTrade(ts, (max(bids) + min(asks)) / 2.0, 0.0, "mid"))
        points[slug] = pts
    return windows, points


def load_series_from_recorder(
    recorder_conn: sqlite3.Connection, btc_conn: sqlite3.Connection, *, pre: int = PRE_SEC,
) -> tuple[int, list[WindowSeries]]:
    windows, points = load_recorder_points(recorder_conn)
    out = []
    for w in windows:
        ws = build_series(w, points.get(w.slug, []), load_btc(btc_conn, w.start - pre - 1, w.end), pre=pre)
        if ws is not None:
            out.append(ws)
    return len(windows), out


# --------------------------------------------------------------------------- the whole study


@dataclass
class ReactionStudy:
    validity: ValidityReport
    lead_lag: list[LagCorr]
    events: list[EventStudy]
    examples: int
    outcome: OutcomeReport | None = None
    outcome_model: LogisticModel | None = None
    outcome_error: str | None = None
    reaction: ReactionReport | None = None
    reaction_model: LogisticModel | None = None
    reaction_error: str | None = None

    def to_json(self) -> dict:
        def conv(obj):
            return asdict(obj) if obj is not None else None
        return {
            "validity": conv(self.validity), "lead_lag": [asdict(r) for r in self.lead_lag],
            "events": [asdict(e) for e in self.events], "examples": self.examples,
            "outcome": conv(self.outcome), "outcome_error": self.outcome_error,
            "reaction": conv(self.reaction), "reaction_error": self.reaction_error,
        }


def run_study(
    windows_in_db: int,
    series: Sequence[WindowSeries],
    *,
    horizon_sec: int = 15,
    sample_every: int = 15,
    max_staleness_sec: int = 5,
    min_move: float = 0.01,
    train_fraction: float = 0.7,
    embargo: int = 1,
    epochs: int = 200,
    max_train_rows: int | None = 200_000,
    k_sigma: float = 3.0,
    max_lag: int = 10,
) -> ReactionStudy:
    if not series:
        raise PmReactionError("no usable windows (none with both a Polymarket series and Binance prices at start and end)")
    lags = lead_lag(series, max_lag=max_lag)
    validity = validity_report(windows_in_db, series, lags)
    by_slug = {ws.slug: ws for ws in series}
    shocks = [e for ws in series for e in detect_btc_shocks(ws, k_sigma=k_sigma)]
    crosses = [e for ws in series for e in detect_strike_crosses(ws)]
    events = [event_study(by_slug, shocks, kind=f"btc_shock_{k_sigma:g}sigma"), event_study(by_slug, crosses, kind="strike_cross")]
    examples = build_examples(series, sample_every=sample_every, horizon_sec=horizon_sec, max_staleness_sec=max_staleness_sec)
    study = ReactionStudy(validity=validity, lead_lag=lags, events=events, examples=len(examples))
    try:
        study.outcome_model, study.outcome = train_outcome_model(
            examples, train_fraction=train_fraction, embargo=embargo, epochs=epochs, max_train_rows=max_train_rows)
    except PmReactionError as exc:
        study.outcome_error = str(exc)
    try:
        study.reaction_model, study.reaction = train_reaction_model(
            examples, horizon_sec=horizon_sec, min_move=min_move, train_fraction=train_fraction, embargo=embargo,
            epochs=epochs, max_train_rows=max_train_rows)
    except PmReactionError as exc:
        study.reaction_error = str(exc)
    return study


def _fmt(x: float | None, spec: str = ".4f") -> str:
    return "n/a" if x is None else format(x, spec)


def render_study(study: ReactionStudy) -> str:
    v = study.validity
    lines = ["== Data validity =="]
    lines.append(
        f"Windows: {v.windows_in_db} in database, {v.windows_usable} usable ({v.windows_no_btc} without Binance "
        f"coverage), {v.truncated} with a truncated trade tape, {v.thin_windows} with too few prints."
    )
    lines.append(f"Polymarket prints: {v.trades_total} total, median {v.trades_median:g} per window.")
    lines.append(f"Binance 1s gaps: {v.btc_missing_seconds} missing seconds across {v.windows_with_btc_gaps} windows (forward-filled).")
    if v.label_checked:
        lines.append(
            f"Binance open->close direction agrees with Polymarket's (Chainlink) resolution on {v.label_agree}/"
            f"{v.label_checked} windows ({v.label_agree / v.label_checked:.1%}); median |move| {_fmt(v.move_median_bp, '.1f')} bp, "
            f"median |move| where they disagree {_fmt(v.disagree_move_median_bp, '.1f')} bp."
        )
    lines.append(f"Same-second Up vs Down-implied price gap: {_fmt(v.complement_mean_gap)} mean over {v.complement_pairs} seconds.")
    for w in v.warnings:
        lines.append(f"WARNING: {w}")

    lines.append("\n== Lead-lag: corr(Binance 1s return, Polymarket Up change `lag` s later) ==")
    lines.append("  " + "  ".join(f"{r.lag:+d}s:{_fmt(r.corr, '.3f')}" for r in study.lead_lag))
    lines.append(f"  peak at {v.lead_lag_peak}s (positive = Polymarket follows Binance)")

    for es in study.events:
        lines.append(f"\n== Event study: {es.kind} ({es.events} events) ==")
        lines.append("  lag  n      pm_move  fair_move   (direction-signed Up-price change since 5 s before the event)")
        for r in es.rows:
            lines.append(f"  {r.lag:>3}s {r.n:>6} {r.pm_mean:>+8.4f} {r.fair_mean:>+9.4f}")
        lines.append(
            f"  half of the 60 s response by: {es.half_life_sec if es.half_life_sec is not None else 'n/a'}s; "
            f"share of the fair-value move priced in at 60 s: {_fmt(es.priced_in_60s, '.2f')}"
        )

    lines.append(f"\n== Models ({study.examples} sampled rows) ==")
    if study.outcome is not None:
        o = study.outcome
        lines.append(
            f"Outcome P(Up): trained on {o.windows_train} windows, validated on {o.windows_validate} LATER windows "
            f"({o.validate.n} rows).\n  Brier  model {o.validate.brier:.4f} | Polymarket price {o.market_validate.brier:.4f} "
            f"| lognormal fair {o.fair_validate.brier:.4f}   (train {o.train.brier:.4f})\n"
            f"  LogLoss model {o.validate.log_loss:.4f} | Polymarket price {o.market_validate.log_loss:.4f} "
            f"| lognormal fair {o.fair_validate.log_loss:.4f}\n"
            f"  beats the market's own price: {'YES' if o.beats_market else 'no'} (window-paired t {_fmt(o.vs_market_t, '+.2f')}; "
            f"YES needs t <= -{SIGNIFICANCE_T:g} over {o.windows_validate} windows)"
        )
    else:
        lines.append(f"Outcome model: not trained ({study.outcome_error})")
    if study.reaction is not None:
        r = study.reaction
        lines.append(
            f"Reaction P(Polymarket Up price higher in {r.horizon_sec}s | it moved >= {r.min_move:g}): trained on "
            f"{r.windows_train} windows, validated on {r.windows_validate} later windows ({r.validate.n} rows).\n"
            f"  Brier  full {r.validate.brier:.4f} | control {r.control_validate.brier:.4f} "
            f"| base rate {r.constant_validate.brier:.4f}   (train {r.train.brier:.4f})\n"
            f"  Hit rate full {r.validate.accuracy:.1%} | control {r.control_validate.accuracy:.1%}\n"
            f"  (control = Polymarket's own history + its last print's side + BTC's move since that print)\n"
            f"  BTC information from before the last print predicts the next move (a slow reaction): "
            f"{'YES' if r.beats_control else 'no'} (window-paired t {_fmt(r.vs_control_t, '+.2f')}; YES needs t <= -{SIGNIFICANCE_T:g})"
        )
    else:
        lines.append(f"Reaction model: not trained ({study.reaction_error})")
    lines.append(
        "\nResearch only: calibration and predictability measures on held-out windows, not a profitability claim. "
        "A one-to-two-cent move is inside Polymarket's spread and taker fees, and nothing here is wired into any "
        "trading decision."
    )
    return "\n".join(lines)
