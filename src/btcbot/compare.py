"""Demo-vs-prod comparison (``btcbot compare``): the same 15-minute windows side by side.

Reads two recorder databases (typically one ``demo-*`` and one ``paper-*`` prod run), lines up the windows both
captured, and reports how the books differ (price, spread, depth), how the strategy's decisions and fills differ, and
how often the two disagree about the market. Offline and read-only: no network, no key, no order code.
"""

from __future__ import annotations

import sqlite3
import statistics
from pathlib import Path

from btcbot.backtest import load_trades
from btcbot.features import build_rows


class CompareError(Exception):
    pass


def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _per_window(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r["ticker"], {"rows": []})["rows"].append(r)
    for w in out.values():
        rs = w["rows"]
        w["mid"], w["spread"] = _med(r["yes_mid"] for r in rs), _med(r["yes_spread"] for r in rs)
        w["depth"] = _med((r["yes_depth3"] or 0) + (r["no_depth3"] or 0) for r in rs)
        w["by_sec"] = {r["ts"][:19]: r["yes_mid"] for r in rs if r["yes_mid"] is not None}
        w["outcome"] = next((r["outcome_yes"] for r in rs if r["outcome_yes"] is not None), None)
    return out


def _trades(path: Path) -> dict[str, list]:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        by: dict[str, list] = {}
        try:
            trades = load_trades(conn)
        except sqlite3.OperationalError:  # a recorder-only database has no trades table
            return by
        for t in trades:
            by.setdefault(t.ticker, []).append(t)
        return by
    finally:
        conn.close()


def compare(demo_db: str | Path, prod_db: str | Path) -> dict:
    demo_db, prod_db = Path(demo_db), Path(prod_db)
    d, p = _per_window(build_rows([demo_db], step_sec=1)), _per_window(build_rows([prod_db], step_sec=1))
    common = sorted(set(d) & set(p))
    if not common:
        raise CompareError("the two databases share no market window")
    dt, pt = _trades(demo_db), _trades(prod_db)
    rows, diffs = [], []
    for t in common:
        secs = set(d[t]["by_sec"]) & set(p[t]["by_sec"])
        gaps = [abs(d[t]["by_sec"][s] - p[t]["by_sec"][s]) for s in secs]
        diffs += gaps
        pnl = lambda ts: None if not ts else sum(float(x.pnl_usd) for x in ts if x.pnl_usd is not None)
        rows.append({
            "ticker": t, "demo_mid": d[t]["mid"], "prod_mid": p[t]["mid"],
            "demo_spread": d[t]["spread"], "prod_spread": p[t]["spread"],
            "demo_depth": d[t]["depth"], "prod_depth": p[t]["depth"],
            "median_abs_mid_gap": _med(gaps), "demo_trades": len(dt.get(t, [])), "prod_trades": len(pt.get(t, [])),
            "demo_pnl": pnl(dt.get(t)), "prod_pnl": pnl(pt.get(t)), "outcome_yes": p[t]["outcome"],
        })
    return {"windows": rows, "median_abs_mid_gap": _med(diffs), "n_windows": len(common)}


def render(report: dict) -> str:
    def f(x, nd=3):
        return "-" if x is None else f"{x:.{nd}f}"
    lines = ["Demo vs prod, same windows (mid = YES mid; depth = top-3 levels both sides; PnL only where settled)",
             "window               demo_mid prod_mid gap    dspread pspread ddepth pdepth  d/p trades   d/p pnl"]
    for w in report["windows"]:
        lines.append(f"{w['ticker'][-13:]:<20} {f(w['demo_mid']):>8} {f(w['prod_mid']):>8} {f(w['median_abs_mid_gap']):>6} "
                     f"{f(w['demo_spread']):>7} {f(w['prod_spread']):>7} {f(w['demo_depth'], 0):>6} {f(w['prod_depth'], 0):>6}  "
                     f"{w['demo_trades']}/{w['prod_trades']}        {f(w['demo_pnl'], 2)}/{f(w['prod_pnl'], 2)}")
    lines.append(f"windows compared: {report['n_windows']}; median |demo mid - prod mid| at the same second: "
                 f"{f(report['median_abs_mid_gap'], 4)}")
    lines.append("Small samples: a handful of windows says the books differ, not which one is right.")
    return "\n".join(lines)
