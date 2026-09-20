"""Read-only portfolio analytics over the recorded paper or demo ledger.

Monetary values remain Decimal through the existing dashboard JSON encoder.  This
is a view of one recording, not a live exchange balance or order-status service.
"""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from decimal import Decimal
from pathlib import Path
from typing import Any

HISTORY_LIMIT = 200
CURVE_LIMIT = 600
ZERO = Decimal(0)
HUNDRED = Decimal(100)


def _decimal(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Recorded monetary values must be finite")
    return result


def _curve(events: list[tuple[str, str | None, Decimal]], balance: Decimal | None) -> tuple[list[dict], Decimal, Decimal | None, str]:
    """Use observation times when complete, otherwise explicitly use entry proxies.

    Orders settled in the same observation are one equity event: arbitrary row
    ordering within that settlement must not introduce artificial drawdown.
    """
    actual_times = bool(events) and all(event[1] is not None for event in events)
    basis = "settlement_observed" if actual_times else "entry_time_proxy" if events else "unavailable"
    batches: dict[str, Decimal] = {}
    for entry_ts, settled_ts, pnl in events:
        ts = settled_ts if actual_times else entry_ts
        assert ts is not None
        batches[ts] = batches.get(ts, ZERO) + pnl
    running = peak = max_dd = ZERO
    max_dd_pct = ZERO if balance is not None else None
    points = []
    for ts, pnl in sorted(batches.items()):
        running += pnl
        peak = max(peak, running)
        drawdown = peak - running
        max_dd = max(max_dd, drawdown)
        peak_equity = balance + peak if balance is not None else None
        dd_pct = drawdown / peak_equity * HUNDRED if peak_equity is not None and peak_equity > 0 else None
        if dd_pct is not None:
            max_dd_pct = max(max_dd_pct or ZERO, dd_pct)
        points.append({
            "ts": ts, "ts_basis": basis, "cumulative_pnl_usd": running,
            "equity_usd": balance + running if balance is not None else None,
            "growth_pct": running / balance * HUNDRED if balance is not None else None,
            "drawdown_usd": drawdown, "drawdown_pct": dd_pct,
        })
    return points, max_dd, max_dd_pct, basis


def _sample_curve(points: list[dict]) -> list[dict]:
    if len(points) <= CURVE_LIMIT:
        return points
    # Preserve the first/last point and worst drawdown as well as evenly spaced
    # observations. Aggregate statistics are always calculated before sampling.
    indices = {round(i * (len(points) - 1) / (CURVE_LIMIT - 2)) for i in range(CURVE_LIMIT - 1)}
    indices.add(max(range(len(points)), key=lambda i: points[i]["drawdown_usd"]))
    return [points[i] for i in sorted(indices)]


def _recorded_starting_balance(conn: sqlite3.Connection, tables: set[str]) -> tuple[Decimal | None, str | None]:
    """The account this run started with, from the ``account_start`` row the paper/demo commands log into ``run_log``:
    a demo run records the demo account's balance, a paper run records its configured account. (Runs recorded before
    that row existed have none.)"""
    if "run_log" not in tables:
        return None, None
    row = conn.execute("SELECT detail FROM run_log WHERE event = 'account_start' ORDER BY id LIMIT 1").fetchone()
    if row is None:
        return None, None
    try:
        info = json.loads(row[0])
        if info.get("kind") == "demo":
            return _decimal(info["available_usd"]), "demo account balance when the run started"
        return _decimal(info["account_usd"]), "configured paper account"
    except (ValueError, KeyError, TypeError, ArithmeticError):
        return None, None


def portfolio_view(db_path: Path, starting_balance: Decimal | str | int | None = None,
                   default_balance: Decimal | str | int | None = None) -> dict[str, Any]:
    """Summarize every ledger row, with bounded history and chart payloads.

    The starting balance is found automatically, in this order: an explicit ``starting_balance`` (API use only; the
    dashboard no longer asks for one), the ``account_start`` row the run itself logged, then ``default_balance``
    (the config's ``sizing.account_usd``). It is never inferred from the trades; ``starting_balance_source`` says
    which one was used. Growth percentages use percentage units (5 means 5%);
    ``win_rate`` is a ratio (0.5 means 50%). A demo database uses ``demo_orders``
    alone, since ``trades`` can contain those same demo fills again.

    ``active_orders`` means locally recorded open demo remainders, with unknown
    exchange status. ``open_positions`` contains recorded unresolved fills.
    Updated paper traders also persist a current runtime snapshot. Older
    recordings do not include it, so zero counts cannot establish inactivity.
    """
    balance = None if starting_balance is None else _decimal(starting_balance)
    if balance is not None and balance <= 0:
        raise ValueError("starting_balance must be positive")
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=0.25)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")  # Keep all reads on one consistent snapshot.
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        balance_source = "entered" if balance is not None else None
        if balance is None:
            balance, balance_source = _recorded_starting_balance(conn, tables)
        if balance is None and default_balance is not None:
            balance, balance_source = _decimal(default_balance), "config sizing.account_usd (assumed for this run)"
        source = "demo" if "demo_orders" in tables else "paper_or_backtest" if "trades" in tables else "none"
        last_snapshot = None
        if "orderbook_snapshots" in tables:
            row = conn.execute("SELECT poll_ts FROM orderbook_snapshots ORDER BY id DESC LIMIT 1").fetchone()
            last_snapshot = row[0] if row else None
        join = ""
        settlement = "NULL AS settlement_ts"
        if "settlements" in tables:
            join = " LEFT JOIN settlements s ON s.ticker=t.ticker AND s.result=t.result AND s.resolved=1"
            settlement = "s.finalized_poll_ts AS settlement_ts"
        if source == "demo":
            rows = conn.execute(f"SELECT t.*, {settlement} FROM demo_orders t{join} ORDER BY t.placed_ts, t.id")
        elif source == "paper_or_backtest":
            rows = conn.execute(f"SELECT t.*, {settlement} FROM trades t{join} ORDER BY t.entry_ts, t.id")
        else:
            rows = iter(())

        history: deque[dict] = deque(maxlen=HISTORY_LIMIT)
        active: deque[dict] = deque(maxlen=HISTORY_LIMIT)
        positions: deque[dict] = deque(maxlen=HISTORY_LIMIT)
        events: list[tuple[str, str | None, Decimal]] = []
        trade_count = order_count = resolved_count = wins = losses = breakeven = 0
        active_count = position_count = incomplete_count = closed_remainder_count = 0
        pnl_total = fees = gross_profit = gross_loss = exposure = resting_notional = ZERO
        settled_fees = ZERO
        first_entry = last_entry = None
        recorded_positions = set()
        for row in rows:
            is_demo = source == "demo"
            order_count += int(is_demo)
            size = _decimal(row["demo_filled"] if is_demo else row["size"])
            entry_ts = (row["demo_first_fill_ts"] or row["placed_ts"]) if is_demo else row["entry_ts"]
            cost = _decimal(row["demo_cost"]) if is_demo else size * _decimal(row["entry_price"])
            fee = _decimal(row["demo_fee"] if is_demo else row["fee_paid"])
            fees += fee
            pnl_raw = row["demo_pnl"] if is_demo else row["pnl_usd"]
            settled = row["result"] in ("yes", "no") and pnl_raw is not None
            pnl = _decimal(pnl_raw) if settled else None
            if is_demo:
                ordered_size = _decimal(row["size"])
                remaining = max(ZERO, ordered_size - size)
                if row["result"] not in ("yes", "no") and remaining > 0:
                    if row["closed_ts"] is None:
                        active_count += 1
                        notional = remaining * _decimal(row["price"])
                        resting_notional += notional
                        active.append({
                            "id": row["id"], "order_id": row["order_id"], "ticker": row["ticker"],
                            "side": row["side"], "price": _decimal(row["price"]), "size": ordered_size,
                            "filled_size": size, "remaining_size": remaining, "placed_ts": row["placed_ts"],
                            "state": "recorded_partial" if size > 0 else "recorded_open",
                            "state_label": "Recorded partial fill" if size > 0 else "Recorded open",
                            "exchange_status": "unknown", "remaining_notional_usd": notional,
                        })
                    else:
                        closed_remainder_count += 1
                # Unfilled orders are not trades, wins, or breakeven outcomes.
                if size <= 0:
                    continue
            trade_count += 1
            first_entry = min(first_entry, entry_ts) if first_entry else entry_ts
            last_entry = max(last_entry, entry_ts) if last_entry else entry_ts
            record = {
                "id": row["id"], "ticker": row["ticker"], "side": row["side"], "size": size,
                "entry_price": cost / size if size > 0 else None, "entry_ts": entry_ts,
                "fee_paid": fee, "cost_usd": cost, "result": row["result"], "pnl_usd": pnl,
                "settlement_ts": row["settlement_ts"] if settled else None,
                "state": "settled" if settled else "recorded_unresolved_position",
                "order_id": row["order_id"] if is_demo else None,
                "source": source,
            }
            history.append(record)
            recorded_positions.add((record["ticker"], record["side"], record["entry_ts"]))
            if settled:
                resolved_count += 1
                assert pnl is not None
                pnl_total += pnl
                settled_fees += fee
                wins += pnl > 0
                losses += pnl < 0
                breakeven += pnl == 0
                gross_profit += max(pnl, ZERO)
                gross_loss += max(-pnl, ZERO)
                observed = row["settlement_ts"]
                # A settlement observed before entry cannot date this trade.
                observed = observed if observed is not None and observed >= entry_ts else None
                events.append((entry_ts, observed, pnl))
            else:
                position_count += 1
                exposure += cost
                incomplete_count += row["result"] is not None
                positions.append(record)
        runtime_ts, runtime_stopped = None, None
        if source != "demo" and "paper_runtime" in tables:
            row = conn.execute("SELECT updated_ts,state_json FROM paper_runtime WHERE id=1").fetchone()
            if row:
                runtime_ts = row[0]
                runtime = json.loads(row[1])
                runtime_stopped = runtime.get("stopped", False)
                for order in runtime.get("active_orders", []):
                    active.append(order)
                    active_count += 1
                    resting_notional += _decimal(order["remaining_notional_usd"])
                for position in runtime.get("open_positions", []):
                    key = (position["ticker"], position["side"], position["entry_ts"])
                    if key in recorded_positions:
                        continue
                    recorded_positions.add(key)
                    positions.append(position)
                    history.append(position)
                    position_count += 1
                    trade_count += 1
                    exposure += _decimal(position["cost_usd"])
                    fees += _decimal(position["fee_paid"])
    finally:
        conn.close()

    curve, max_dd, max_dd_pct, time_basis = _curve(events, balance)
    limits = [
        "One local recording only; totals include every recorded ledger row.",
        "Equity is assumed starting balance plus settled net PnL; it is not an exchange balance or marked portfolio value.",
        "Open exposure is recorded fill cost and excludes unrecorded positions; unrealized PnL is unavailable.",
    ]
    if source == "demo":
        limits.extend([
            "Demo uses fake money. Paper twins and duplicate trades-table entries are excluded from totals.",
            "Order states are local records, not exchange-verified; recorded closure does not prove cancellation succeeded.",
        ])
    elif source == "paper_or_backtest":
        if runtime_ts:
            limits.append("Paper orders and held positions come from the last simulated trader snapshot; stale snapshots do not confirm the process is still running.")
        else:
            limits.append("This older recording does not persist paper resting orders or currently held positions; unresolved rows are recorded fills, not resting orders. Restart the paper recorder with the updated version to enable tracking.")
    if time_basis == "entry_time_proxy":
        limits.append("Settlement observation times are incomplete; the equity curve and drawdown use entry-time ordering as a proxy.")
    if balance is None:
        limits.append("Starting balance was not supplied, so equity and growth percentages are unavailable.")
    else:
        limits.append("Starting balance is a supplied assumption, not a balance captured at the start of this recording.")
    if incomplete_count:
        limits.append("Some fills have a result without recorded PnL; these remain unresolved in the totals.")

    return {
        "source": source, "trade_count": trade_count, "order_count": order_count if source == "demo" else None,
        "resolved_count": resolved_count, "unresolved_count": position_count,
        "wins": wins, "losses": losses, "breakeven": breakeven,
        "win_rate": Decimal(wins) / resolved_count if resolved_count else None,
        "total_pnl_usd": pnl_total, "total_fees_usd": fees, "settled_fees_usd": settled_fees,
        "gross_profit_usd": gross_profit, "gross_loss_usd": gross_loss,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "profit_factor_note": "No recorded losses" if gross_loss == 0 and wins else "No settled trades" if not resolved_count else None,
        "avg_trade_pnl_usd": pnl_total / resolved_count if resolved_count else None,
        "avg_win_usd": gross_profit / wins if wins else None, "avg_loss_usd": -gross_loss / losses if losses else None,
        "starting_balance_usd": balance, "starting_balance_source": balance_source, "equity_usd": balance + pnl_total if balance is not None else None,
        "growth_pct": pnl_total / balance * HUNDRED if balance is not None else None,
        "max_drawdown_usd": max_dd, "max_drawdown_pct": max_dd_pct,
        "open_exposure_usd": exposure, "recorded_resting_notional_usd": resting_notional,
        "active_order_count": active_count, "open_position_count": position_count,
        "recorded_closed_remainder_count": closed_remainder_count, "incomplete_settlement_count": incomplete_count,
        "first_entry_ts": first_entry, "last_entry_ts": last_entry, "last_snapshot_ts": last_snapshot,
        "runtime_snapshot_ts": runtime_ts, "runtime_stopped": runtime_stopped,
        "equity_time_basis": time_basis, "equity_curve": _sample_curve(curve),
        "trade_history": list(reversed(history)), "active_orders": list(reversed(active)),
        "open_positions": list(reversed(positions)), "limitations": limits,
        "provenance": {
            "database": Path(db_path).name, "ledger": "demo_orders" if source == "demo" else "trades" if source != "none" else None,
            "balance_basis": "supplied_assumption" if balance is not None else "unavailable",
            "pnl_basis": "recorded_settled_net_of_fees", "exchange_status_verified": False,
            "history_limit": HISTORY_LIMIT, "history_truncated": trade_count > HISTORY_LIMIT,
            "active_orders_truncated": active_count > HISTORY_LIMIT,
            "open_positions_truncated": position_count > HISTORY_LIMIT,
            "curve_limit": CURVE_LIMIT, "curve_events": len(curve), "curve_sampled": len(curve) > CURVE_LIMIT,
        },
    }
