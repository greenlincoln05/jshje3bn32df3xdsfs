"""Health watchdog and morning summary (``btcbot watch``): is anything recording, and what did it do?

Read-only. It looks only at the newest ``paper-*`` (prod) and ``demo-*`` databases in the data directory: how long
ago each was last written, how many predictions/trades it holds, wins and P&L, sizes traded, and the market gaps
its run log recorded. A database not written for ``stale_min`` minutes is flagged (a recorder that died leaves
exactly that trace). It does not inspect processes and cannot restart anything. No network, no key.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DbHealth:
    kind: str
    name: str
    age_min: float
    trades: int
    resolved: int
    wins: int
    pnl: float
    sizes: list[float]
    gaps: int
    last_prediction_age_min: float | None
    stale: bool
    risk_paused: bool


def _newest(data_dir: Path, pattern: str) -> Path | None:
    files = sorted(data_dir.glob(pattern), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def inspect_db(path: Path, kind: str, *, stale_min: float, now: float | None = None) -> DbHealth:
    now = time.time() if now is None else now
    mtimes = [q.stat().st_mtime for q in (path, Path(str(path) + "-wal"), Path(str(path) + "-journal")) if q.exists()]
    age = (now - max(mtimes)) / 60  # a live SQLite writer may only touch the -wal file between checkpoints
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        def q(sql):
            try:
                return conn.execute(sql).fetchall()
            except sqlite3.OperationalError:
                return []
        trades = q("SELECT size, pnl_usd FROM trades ORDER BY entry_ts")
        pnls = [float(p) for _, p in trades if p is not None]
        gaps = q("SELECT COUNT(*) FROM run_log WHERE event = 'rollover_gap'")
        # A risk-manager pause (e.g. max_consecutive_losses) writes no exception and keeps polling/predicting
        # normally, so it looks identical to a healthy run everywhere except here: the most recent of these two
        # events tells us whether a pause is still in effect (risk_blocked_order) or was cleared (risk_resumed).
        last_risk_event = q(
            "SELECT event FROM run_log WHERE event IN ('risk_blocked_order', 'risk_resumed') ORDER BY id DESC LIMIT 1"
        )
        risk_paused = bool(last_risk_event) and last_risk_event[0][0] == "risk_blocked_order"
        last = q("SELECT MAX(ts) FROM predictions")
        last_age = None
        if last and last[0][0]:
            from btcbot.models import ParseError, parse_time
            try:
                last_age = (now - parse_time(last[0][0]).timestamp()) / 60
            except ParseError:
                last_age = None
        return DbHealth(kind, path.name, age, len(trades), len(pnls), sum(1 for p in pnls if p > 0), sum(pnls),
                        [float(s) for s, _ in trades], gaps[0][0] if gaps else 0, last_age, age > stale_min, risk_paused)
    finally:
        conn.close()


def summarize(data_dir: str | Path = "data", *, stale_min: float = 5.0, now: float | None = None) -> list[DbHealth]:
    d = Path(data_dir)
    out = []
    for kind, pattern in (("paper(prod)", "paper-*.sqlite"), ("demo", "demo-*.sqlite")):
        p = _newest(d, pattern)
        if p is not None:
            out.append(inspect_db(p, kind, stale_min=stale_min, now=now))
    return out


def render(items: list[DbHealth]) -> str:
    if not items:
        return "no paper-*/demo-* databases found"
    lines = []
    for h in items:
        state = "STALE - nothing written recently; the process may have stopped" if h.stale else "ok (written recently)"
        if h.risk_paused:
            state += " -- BUT RISK-PAUSED: still polling/predicting, but every new order is being vetoed (e.g. " \
                     "max_consecutive_losses); create a resume-file (see `btcbot paper --help`) or restart to trade again"
        wr = "-" if not h.resolved else f"{h.wins}/{h.resolved} ({h.wins / h.resolved:.0%})"
        sizes = ",".join(f"{s:g}" for s in h.sizes[-12:]) or "-"
        pred = "-" if h.last_prediction_age_min is None else f"{h.last_prediction_age_min:.1f} min ago"
        lines.append(f"{h.kind}: {h.name}\n  status: {state}; last write {h.age_min:.1f} min ago; last prediction {pred}\n"
                     f"  trades {h.trades} (resolved {h.resolved}), wins {wr}, pnl ${h.pnl:.2f}, recent sizes {sizes}\n"
                     f"  'no open market' gaps logged: {h.gaps}")
    lines.append("Small samples prove nothing; a demo run and a prod paper run are not comparable trade for trade.")
    return "\n".join(lines)
