"""Calibration and disagreement report (``btcbot disagree``) over a feature-store CSV.

Answers: when the model says P, how often does YES actually settle -- and, above all, what happens when the model
DISAGREES with the market (model minus market mid, in buckets) or the price is cheap? Overnight demo losses came from
exactly those cases (model ~0.5 against a bid of 0.18). One row per window would be independent; rows inside a window
are not, so counts here are WINDOWS (still correlated through the regime, so intervals are a little too narrow) (the row at the tradeable time nearest ``at_tau_sec``), never raw rows.
Offline, read-only: no network, no key. A bucket under ``min_n`` windows is marked small and proves nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from btcbot.validation import wilson


@dataclass(frozen=True, slots=True)
class Bucket:
    label: str
    n: int
    mean_p: float | None
    mean_mid: float | None
    yes_rate: float | None
    ci95: tuple[float, float]
    small: bool


def one_row_per_window(rows: Sequence[dict], *, at_tau_sec: float = 300.0, field: str = "p_model",
                       max_tau_gap: float = 60.0) -> list[dict]:
    """The settled row of each window closest to ``at_tau_sec`` seconds before close, with the field present.
    A window whose nearest row is more than ``max_tau_gap`` seconds away from the target is dropped, not stretched."""
    best: dict[str, dict] = {}
    for r in rows:
        if r.get("outcome_yes") is None or r.get(field) is None or r.get("yes_mid") is None:
            continue
        if abs(r["tau_sec"] - at_tau_sec) > max_tau_gap:
            continue
        cur = best.get(r["ticker"])
        if cur is None or abs(r["tau_sec"] - at_tau_sec) < abs(cur["tau_sec"] - at_tau_sec):
            best[r["ticker"]] = r
    return list(best.values())


def _bucketize(rows: Sequence[dict], key, edges: Sequence[float], fmt, field: str, min_n: int) -> list[Bucket]:
    out = []
    bounds = [float("-inf"), *edges, float("inf")]
    for lo, hi in zip(bounds, bounds[1:]):
        sel = [r for r in rows if lo <= key(r) < hi]
        n = len(sel)
        wins = sum(int(r["outcome_yes"]) for r in sel)
        out.append(Bucket(fmt(lo, hi), n, None if not n else sum(r[field] for r in sel) / n,
                          None if not n else sum(r["yes_mid"] for r in sel) / n,
                          None if not n else wins / n, wilson(wins, n), n < min_n))
    return out


def _fmt(lo: float, hi: float) -> str:
    if lo == float("-inf"):
        return f"< {hi:+.2f}"
    if hi == float("inf"):
        return f">= {lo:+.2f}"
    return f"{lo:+.2f} .. {hi:+.2f}"


def build_report(rows: Sequence[dict], *, at_tau_sec: float = 300.0, field: str = "p_model", min_n: int = 10) -> dict:
    per = one_row_per_window(rows, at_tau_sec=at_tau_sec, field=field)
    return {
        "windows": len(per), "windows_total": len({r["ticker"] for r in rows}), "field": field, "at_tau_sec": at_tau_sec,
        "reliability": _bucketize(per, lambda r: r[field], [0.2, 0.4, 0.6, 0.8], _fmt, field, min_n),
        "by_disagreement": _bucketize(per, lambda r: r[field] - r["yes_mid"], [-0.3, -0.1, 0.1, 0.3], _fmt, field, min_n),
        "by_market_price": _bucketize(per, lambda r: r["yes_mid"], [0.2, 0.4, 0.6, 0.8], _fmt, field, min_n),
    }


def render(report: dict) -> str:
    def block(title: str, buckets: Sequence[Bucket]) -> list[str]:
        lines = [title, "  bucket              windows  mean P   YES rate  95% interval   note"]
        for b in buckets:
            rate = "-" if b.yes_rate is None else f"{b.yes_rate:.0%}"
            mp = "-" if b.mean_p is None else f"{b.mean_p:.2f}"
            mm = "-" if b.mean_mid is None else f"{b.mean_mid:.2f}"
            ci = "-" if b.n == 0 else f"{b.ci95[0]:.0%}-{b.ci95[1]:.0%}"
            lines.append(f"  {b.label:<19} {b.n:>7}  {mp:>6}  {mm:>7}  {rate:>8}  {ci}"
                         f"{'':<6}{'small sample' if b.small else ''}")
        return lines
    out = [f"{report['windows']} of {report['windows_total']} windows usable, model field {report['field']}, read ~{report['at_tau_sec']:.0f}s before close"]
    out += block("Reliability (does P match the YES rate?)", report["reliability"])
    out += block("Model minus market mid (disagreement; positive = model likes YES more than the market)", report["by_disagreement"])
    out += block("By market YES mid (cheap YES < 0.20, dear YES > 0.80)", report["by_market_price"])
    out.append("A large gap between mean P and the YES rate with a non-small sample means the model is miscalibrated there.")
    return "\n".join(out)
