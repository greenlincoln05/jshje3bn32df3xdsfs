"""One continuous, scrollable trade history for a whole *kind* of run (``paper`` or ``demo``), merged across
every one of its own ``{kind}-*.sqlite`` files instead of picking one file at a time -- with each trade
tagged by which PR/commit (see :mod:`btcbot.code_version`) was live when its run started, so a shift in
performance can be lined up against the change that plausibly caused it.

Deliberately NOT a re-derived multi-file equity curve: each run starts its own bankroll (paper resets to
``sizing.account_usd`` on every restart; demo's real balance does carry over on the exchange, but nothing
here reconstructs that across files). This is a chronological ledger of settled trades plus per-PR-segment
win-rate/PnL summaries -- enough to see how a change affected performance without pretending to know a
single continuous account balance this data doesn't actually represent.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from btcbot.code_version import load_code_versions

ZERO = Decimal(0)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


@dataclass(frozen=True, slots=True)
class HistoryTrade:
    ts: str
    ticker: str
    side: str
    size: Decimal
    price: Decimal
    result: str | None
    pnl_usd: Decimal | None
    fee_usd: Decimal
    run_file: str


def _paper_trades(conn: sqlite3.Connection, run_file: str) -> list[HistoryTrade]:
    rows = conn.execute(
        "SELECT entry_ts, ticker, side, size, entry_price, result, pnl_usd, fee_paid FROM trades "
        "WHERE pnl_usd IS NOT NULL ORDER BY entry_ts, id"
    ).fetchall()
    out = []
    for ts, ticker, side, size, price, result, pnl_usd, fee in rows:
        size_d, price_d, pnl_d, fee_d = _decimal(size), _decimal(price), _decimal(pnl_usd), _decimal(fee)
        if size_d is None or price_d is None or pnl_d is None:
            continue  # a row this malformed says nothing trustworthy; skip it rather than guess
        out.append(HistoryTrade(ts, ticker, side, size_d, price_d, result, pnl_d, fee_d or ZERO, run_file))
    return out


def _demo_trades(conn: sqlite3.Connection, run_file: str) -> list[HistoryTrade]:
    # The REAL exchange fill (demo_filled/demo_cost/demo_fee/demo_pnl), not the shadow paper twin -- this is
    # what portfolio_view (single-file view) also treats as this run's actual trade for a demo database.
    rows = conn.execute(
        "SELECT placed_ts, ticker, side, price, demo_filled, result, demo_pnl, demo_fee FROM demo_orders "
        "WHERE demo_pnl IS NOT NULL ORDER BY placed_ts, id"
    ).fetchall()
    out = []
    for ts, ticker, side, price, filled, result, pnl_usd, fee in rows:
        filled_d, price_d, pnl_d, fee_d = _decimal(filled), _decimal(price), _decimal(pnl_usd), _decimal(fee)
        if filled_d is None or price_d is None or pnl_d is None or filled_d <= 0:
            continue  # never filled, or unresolved/malformed: not a realized trade yet
        out.append(HistoryTrade(ts, ticker, side, filled_d, price_d, result, pnl_d, fee_d or ZERO, run_file))
    return out


def _run_files(data_dir: Path, kind: str) -> list[Path]:
    return sorted(p for p in data_dir.glob(f"{kind}-*.sqlite") if p.is_file())


def portfolio_history(data_dir: Path, kind: str) -> dict[str, Any]:
    """Every settled trade across every ``{kind}-*.sqlite`` file in ``data_dir``, oldest first, plus every
    recorded code-version marker, plus a per-version-segment summary. ``kind`` is ``"paper"`` or
    ``"demo"`` -- ``"prod"`` isn't wired to anything yet (Phase 7)."""
    if kind not in ("paper", "demo"):
        raise ValueError(f"kind must be 'paper' or 'demo', got {kind!r}")

    trades: list[HistoryTrade] = []
    versions: list[dict[str, Any]] = []
    files = _run_files(data_dir, kind)
    for path in files:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.25)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if kind == "demo" and "demo_orders" in tables:
                trades.extend(_demo_trades(conn, path.name))
            elif kind == "paper" and "trades" in tables:
                trades.extend(_paper_trades(conn, path.name))
            for recorded_ts, version, source in load_code_versions(conn):
                versions.append({
                    "ts": recorded_ts.isoformat(), "label": version.label, "pr_number": version.pr_number,
                    "commit_hash": version.commit_hash, "source": source, "run_file": path.name,
                })
        except sqlite3.OperationalError:
            continue  # a file mid-write (WAL) or otherwise unreadable right now: skip it, not fatal
        finally:
            conn.close()

    trades.sort(key=lambda t: t.ts)
    versions.sort(key=lambda v: v["ts"])
    trade_dicts = [
        {"ts": t.ts, "ticker": t.ticker, "side": t.side, "size": t.size, "price": t.price,
         "result": t.result, "pnl_usd": t.pnl_usd, "fee_usd": t.fee_usd, "run_file": t.run_file}
        for t in trades
    ]
    return {
        "kind": kind,
        "run_files": len(files),
        "trades": trade_dicts,
        "versions": versions,
        "segments": _segment_stats(trade_dicts, versions),
    }


def _collapse_repeated_versions(versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Two separate runs that both happened to start while the SAME commit was current (e.g. a restart with
    no code change in between) must not read as two different PRs -- keep only the first sighting of each
    consecutive run of the same commit, so they render as one continuous segment instead of a confusing
    back-to-back repeat of the same label."""
    collapsed: list[dict[str, Any]] = []
    for v in versions:
        if collapsed and collapsed[-1]["commit_hash"] == v["commit_hash"]:
            continue
        collapsed.append(v)
    return collapsed


def _segment_stats(trades: list[dict[str, Any]], versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per distinct code version (in trade order), each carrying its OWN slice of ``trades`` (every
    trade from that version's first-seen ``ts`` up to the next distinct version's) plus a win-rate/PnL
    summary -- "how did performance look while this PR was live." Trades that predate the very first
    recorded marker (older runs, before this feature existed) are their own leading, unlabeled segment
    rather than silently folded into the first PR's numbers. The caller (the dashboard) renders straight
    from each segment's own trade list instead of re-deriving time-range membership itself, so there's
    exactly one place this logic lives."""
    if not versions:
        return [_summarize_segment(None, trades)] if trades else []
    versions = _collapse_repeated_versions(versions)
    segments = []
    leading = [t for t in trades if t["ts"] < versions[0]["ts"]]
    if leading:
        segments.append(_summarize_segment(None, leading))
    for i, version in enumerate(versions):
        end_ts = versions[i + 1]["ts"] if i + 1 < len(versions) else None
        bucket = [t for t in trades if t["ts"] >= version["ts"] and (end_ts is None or t["ts"] < end_ts)]
        segments.append(_summarize_segment(version, bucket))
    return segments


def _summarize_segment(version: dict[str, Any] | None, trades: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(1 for t in trades if t["pnl_usd"] is not None and t["pnl_usd"] > 0)
    losses = sum(1 for t in trades if t["pnl_usd"] is not None and t["pnl_usd"] < 0)
    pnl_total = sum((t["pnl_usd"] for t in trades if t["pnl_usd"] is not None), ZERO)
    return {
        "version": version,
        "trades": trades,
        "trade_count": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / (wins + losses) if (wins + losses) > 0 else None,
        "pnl_usd": pnl_total,
    }
