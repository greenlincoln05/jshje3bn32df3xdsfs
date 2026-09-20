import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from btcbot.backtest import TRADES_SCHEMA
from btcbot.dashboard_analytics import CURVE_LIMIT, HISTORY_LIMIT, portfolio_view
from btcbot.demo_trader import DEMO_SCHEMA

D = Decimal
T0 = datetime(2026, 9, 20, tzinfo=timezone.utc)


def timestamp(minutes=0):
    return (T0 + timedelta(minutes=minutes)).isoformat()


def database(tmp_path, *, demo=False):
    path = tmp_path / "recording.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(TRADES_SCHEMA)
    if demo:
        conn.executescript(DEMO_SCHEMA)
    conn.execute("CREATE TABLE settlements (ticker TEXT PRIMARY KEY, result TEXT, finalized_poll_ts TEXT, resolved INTEGER)")
    conn.commit()
    conn.close()
    return path


def trade(path, *, ticker="A", size="4", price="0.3", fee="0.01", pnl="2.79", result="yes", minute=0, settled_minute=None):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO trades (ticker,side,size,entry_price,entry_ts,fee_paid,p_side_at_entry,result,pnl_usd) VALUES (?,?,?,?,?,?,?,?,?)",
            (ticker, "yes", size, price, timestamp(minute), fee, 0.5, result, pnl),
        )
        if settled_minute is not None:
            conn.execute("INSERT OR REPLACE INTO settlements VALUES (?,?,?,1)", (ticker, result, timestamp(settled_minute)))


def demo_order(path, *, ticker="A", size="4", filled="4", price="0.3", cost="1.2", fee="0.01", pnl="2.79", result="yes", minute=0, closed=False):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO demo_orders (ticker,side,price,size,placed_ts,order_id,demo_filled,demo_cost,demo_fee,
               demo_first_fill_ts,closed_ts,result,demo_pnl,paper_filled,paper_pnl)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticker, "yes", price, size, timestamp(minute), "order-" + ticker, filled, cost, fee,
             timestamp(minute) if D(filled) > 0 else None, timestamp(minute + 1) if closed else None, result, pnl, size, "999"),
        )


def test_no_database_is_created_on_read(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        portfolio_view(path)
    assert not path.exists()


def test_empty_recorder_has_no_invented_balance_or_orders(tmp_path):
    path = tmp_path / "empty.sqlite"
    sqlite3.connect(path).close()
    view = portfolio_view(path)
    assert view["source"] == "none"
    assert view["trade_count"] == 0
    assert view["total_pnl_usd"] == D(0)
    assert view["equity_usd"] is None
    assert view["growth_pct"] is None
    assert view["win_rate"] is None
    assert view["profit_factor"] is None
    assert view["active_orders"] == view["open_positions"] == view["equity_curve"] == []


def test_complete_ledger_metrics_use_decimal_and_settlement_order(tmp_path):
    path = database(tmp_path)
    # Settlement order is win, loss, breakeven, unlike the entry order.
    trade(path, ticker="LOSS", pnl="-1.21", result="no", minute=0, settled_minute=30)
    trade(path, ticker="WIN", minute=1, settled_minute=20)
    trade(path, ticker="EVEN", pnl="0", minute=2, settled_minute=40)
    before = path.read_bytes()
    view = portfolio_view(path, D("100"))
    assert path.read_bytes() == before
    assert view["total_pnl_usd"] == D("1.58")
    assert view["equity_usd"] == D("101.58")
    assert view["growth_pct"] == D("1.58")
    assert view["total_fees_usd"] == D("0.03")
    assert (view["wins"], view["losses"], view["breakeven"]) == (1, 1, 1)
    assert view["win_rate"] == D(1) / 3
    assert view["profit_factor"] == D("2.79") / D("1.21")
    assert view["avg_trade_pnl_usd"] == D("1.58") / 3
    assert view["max_drawdown_usd"] == D("1.21")
    assert view["max_drawdown_pct"] == D("1.21") / D("102.79") * 100
    assert view["equity_time_basis"] == "settlement_observed"
    assert [point["ts"] for point in view["equity_curve"]] == [timestamp(20), timestamp(30), timestamp(40)]
    assert [item["ticker"] for item in view["trade_history"]] == ["EVEN", "WIN", "LOSS"]


def test_missing_settlement_times_are_explicit_entry_proxies(tmp_path):
    path = database(tmp_path)
    trade(path, ticker="A", minute=0, settled_minute=20)
    trade(path, ticker="B", minute=1, pnl="-1.21", result="no")
    view = portfolio_view(path)
    assert view["equity_time_basis"] == "entry_time_proxy"
    assert view["equity_curve"][0]["ts"] == timestamp(0)
    assert view["max_drawdown_usd"] == D("1.21")
    assert view["max_drawdown_pct"] is None
    assert any("proxy" in limitation for limitation in view["limitations"])


def test_simultaneous_settlement_does_not_invent_intrabatched_drawdown(tmp_path):
    path = database(tmp_path)
    trade(path, ticker="A", pnl="-1.21", result="no", settled_minute=20)
    trade(path, ticker="B", pnl="2.79", minute=1, settled_minute=20)
    view = portfolio_view(path, "100")
    assert len(view["equity_curve"]) == 1
    assert view["max_drawdown_usd"] == 0
    assert view["equity_curve"][0]["equity_usd"] == D("101.58")


def test_unresolved_paper_fills_are_positions_not_resting_orders(tmp_path):
    path = database(tmp_path)
    trade(path, size="1.5", price="0.125", fee="0.003", result=None, pnl=None)
    trade(path, ticker="B", result="yes", pnl=None, minute=1)
    view = portfolio_view(path)
    assert view["active_order_count"] == 0
    assert view["active_orders"] == []
    assert view["open_position_count"] == view["unresolved_count"] == 2
    assert view["open_exposure_usd"] == D("1.3875")
    assert view["total_fees_usd"] == D("0.013")
    assert view["settled_fees_usd"] == 0
    assert view["resolved_count"] == 0
    assert view["total_pnl_usd"] == 0
    assert view["incomplete_settlement_count"] == 1
    assert view["open_positions"][1]["size"] == D("1.5")
    assert "not resting orders" in " ".join(view["limitations"])


def test_demo_does_not_double_count_trades_or_shadow_paper_or_unfilled_orders(tmp_path):
    path = database(tmp_path, demo=True)
    trade(path)
    demo_order(path)
    demo_order(path, ticker="EMPTY", filled="0", cost="0", fee="0", pnl="0", minute=1)
    view = portfolio_view(path, "100")
    assert view["source"] == "demo"
    assert view["order_count"] == 2
    assert view["trade_count"] == view["resolved_count"] == view["wins"] == 1
    assert view["breakeven"] == 0
    assert view["total_pnl_usd"] == D("2.79")
    assert view["total_fees_usd"] == D("0.01")
    assert view["profit_factor"] is None
    assert view["profit_factor_note"] == "No recorded losses"
    assert view["provenance"]["ledger"] == "demo_orders"


def test_demo_partial_orders_and_filled_positions_stay_distinct(tmp_path):
    path = database(tmp_path, demo=True)
    demo_order(path, ticker="PARTIAL", size="4", filled="1.5", cost="0.45", result=None, pnl=None)
    demo_order(path, ticker="FILLED", minute=1, result=None, pnl=None)
    demo_order(path, ticker="OPEN", minute=2, filled="0", cost="0", fee="0", result=None, pnl=None)
    # Local close is not proof of exchange cancellation (the trader writes it on failures too).
    demo_order(path, ticker="CLOSED", minute=3, filled="0", cost="0", fee="0", result=None, pnl=None, closed=True)
    view = portfolio_view(path)
    assert view["active_order_count"] == 2
    assert view["open_position_count"] == 2
    assert view["open_exposure_usd"] == D("1.65")
    assert view["recorded_resting_notional_usd"] == D("1.95")
    assert view["recorded_closed_remainder_count"] == 1
    assert [row["state"] for row in view["active_orders"]] == ["recorded_open", "recorded_partial"]
    assert all(row["exchange_status"] == "unknown" for row in view["active_orders"])
    assert view["provenance"]["exchange_status_verified"] is False
    assert "does not prove cancellation" in " ".join(view["limitations"])


def test_totals_include_entire_history_not_only_visible_rows(tmp_path):
    path = database(tmp_path)
    for index in range(HISTORY_LIMIT + 7):
        trade(path, ticker=str(index), minute=index, pnl="0.01", fee="0.001")
    view = portfolio_view(path, "100")
    assert view["trade_count"] == HISTORY_LIMIT + 7
    assert view["total_pnl_usd"] == D("0.01") * (HISTORY_LIMIT + 7)
    assert view["total_fees_usd"] == D("0.001") * (HISTORY_LIMIT + 7)
    assert len(view["trade_history"]) == HISTORY_LIMIT
    assert view["trade_history"][0]["ticker"] == str(HISTORY_LIMIT + 6)
    assert view["provenance"]["history_truncated"] is True


def test_chart_sampling_retains_last_value_and_drawdown_extreme(tmp_path):
    path = database(tmp_path)
    for index in range(CURVE_LIMIT + 11):
        trade(path, ticker=str(index), minute=index, pnl="-7" if index == 302 else "0.01")
    view = portfolio_view(path, "100")
    assert view["provenance"]["curve_sampled"] is True
    assert len(view["equity_curve"]) <= CURVE_LIMIT
    assert view["equity_curve"][0]["ts"] == timestamp(0)
    assert view["equity_curve"][-1]["cumulative_pnl_usd"] == view["total_pnl_usd"]
    assert max(point["drawdown_usd"] for point in view["equity_curve"]) == view["max_drawdown_usd"] == D(7)


@pytest.mark.parametrize("balance", ["0", "-1", "NaN", "Infinity"])
def test_invalid_assumed_balance_is_rejected(tmp_path, balance):
    path = database(tmp_path)
    with pytest.raises(ValueError):
        portfolio_view(path, balance)


def test_starting_balance_is_found_automatically(tmp_path):
    import json
    import sqlite3
    from btcbot.dashboard_analytics import portfolio_view

    def db(name, detail=None):
        path = tmp_path / name
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE run_log (id INTEGER PRIMARY KEY, ts TEXT, level TEXT, event TEXT, detail TEXT)")
        conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, ticker TEXT, side TEXT, size TEXT, entry_price TEXT, entry_ts TEXT,"
                     " fee_paid TEXT, p_side_at_entry REAL, result TEXT, pnl_usd TEXT, exit_reason TEXT, exit_price TEXT)")
        if detail:
            conn.execute("INSERT INTO run_log (ts, level, event, detail) VALUES ('t','info','account_start',?)", (json.dumps(detail),))
        conn.commit(); conn.close()
        return path

    demo = portfolio_view(db("d.sqlite", {"kind": "demo", "available_usd": "116.40", "portfolio_value_usd": "118.13"}))
    assert str(demo["starting_balance_usd"]) == "118.13" and "demo account" in demo["starting_balance_source"]
    paper = portfolio_view(db("p.sqlite", {"kind": "paper", "account_usd": "500"}))
    assert str(paper["starting_balance_usd"]) == "500" and "paper" in paper["starting_balance_source"]
    old = portfolio_view(db("o.sqlite"), None, "500")                       # recorded before account_start existed
    assert str(old["starting_balance_usd"]) == "500" and "config" in old["starting_balance_source"]
    assert str(portfolio_view(db("e.sqlite"), "250")["starting_balance_usd"]) == "250"  # explicit API override still works


def test_a_demo_run_without_a_recorded_balance_does_not_borrow_the_config_account(tmp_path):
    import sqlite3
    from btcbot.dashboard_analytics import portfolio_view
    path = tmp_path / "d.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE demo_orders (id INTEGER PRIMARY KEY, ticker TEXT, side TEXT, price TEXT, size TEXT, placed_ts TEXT, order_id TEXT,"
                 " demo_filled TEXT, demo_cost TEXT, demo_fee TEXT, demo_first_fill_ts TEXT, paper_filled TEXT, paper_cost TEXT,"
                 " paper_fee TEXT, paper_first_fill_ts TEXT, closed_ts TEXT, result TEXT, demo_pnl TEXT, paper_pnl TEXT)")
    conn.commit(); conn.close()
    view = portfolio_view(path, None, "500")
    assert view["starting_balance_usd"] is None
