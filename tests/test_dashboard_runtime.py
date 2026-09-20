"""Exercise active monitoring through the real paper-trading lifecycle."""

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal

from btcbot.dashboard_analytics import portfolio_view
from test_live_paper import T0, fill_a_window, make_market, make_trader


async def test_partial_fill_is_visible_before_settlement_and_not_counted_twice(tmp_path):
    path = tmp_path / "paper.sqlite"
    conn = sqlite3.connect(path)
    trader, buffer = make_trader(conn)
    market = await fill_a_window(trader, buffer)
    view = portfolio_view(path, 500)
    assert view["active_order_count"] == 1
    assert view["open_position_count"] == 1
    assert Decimal(view["active_orders"][0]["filled_size"]) == 4
    assert Decimal(view["active_orders"][0]["remaining_size"]) == 1
    assert view["open_exposure_usd"] == Decimal("1.20")
    assert view["recorded_resting_notional_usd"] == Decimal("0.30")
    assert view["total_pnl_usd"] == 0
    assert view["trade_count"] == 1
    # A reader may land between the settled ledger commit and the monitor update.
    previous = conn.execute("SELECT updated_ts,state_json FROM paper_runtime").fetchone()
    settled = make_market(market.ticker, status="finalized", raw_extra={"result": "yes"})
    await trader.on_settlement(settled)
    view = portfolio_view(path, 500)
    assert view["active_order_count"] == 0
    assert view["open_position_count"] == 0
    assert view["total_pnl_usd"] == Decimal("2.80")
    assert view["trade_count"] == 1
    state = json.loads(previous[1])
    state["active_orders"] = []
    conn.execute("UPDATE paper_runtime SET updated_ts=?,state_json=?", (previous[0], json.dumps(state)))
    conn.commit()
    assert portfolio_view(path, 500)["open_position_count"] == 0
    trader.close()


async def test_shutdown_clears_resting_state_and_preserves_unresolved_fill_once(tmp_path):
    path = tmp_path / "paper.sqlite"
    trader, buffer = make_trader(sqlite3.connect(path))
    await fill_a_window(trader, buffer)
    await trader.shutdown(T0 + timedelta(seconds=9))
    view = portfolio_view(path, 500)
    assert view["runtime_stopped"] is True
    assert view["active_order_count"] == 0
    assert view["open_position_count"] == 1
    assert view["trade_count"] == 1
    assert view["total_pnl_usd"] == 0
    trader.close()
