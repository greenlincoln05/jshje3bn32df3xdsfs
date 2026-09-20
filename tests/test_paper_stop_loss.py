import sqlite3
from datetime import timedelta
from decimal import Decimal as D

from test_live_paper import T0, fill_a_window, make_book, make_trader

from btcbot.config import BotConfig, ExitRules, Sizing


def cfg(stop=30, mode="fixed", **exit_kw):
    return BotConfig(sizing=Sizing(mode=mode, contracts_per_trade=4), exit=ExitRules(stop_loss_pct=D(stop), **exit_kw))


async def held_position(trader, buffer):
    market = await fill_a_window(trader, buffer)
    assert trader._position is not None and trader._position.size == 4 and trader._resting_order_id is None
    return market


async def test_a_falling_bid_closes_the_position_at_the_bid_and_books_the_loss():
    trader, buffer = make_trader(sqlite3.connect(":memory:"), config=cfg(30))
    market = await held_position(trader, buffer)
    await trader.on_orderbook_snapshot(market, make_book(yes_price="0.15"), T0 + timedelta(seconds=9))
    assert trader._position is None and len(trader.trades) == 1
    t = trader.trades[0]
    assert t.exit_reason == "stop_loss" and t.result is None and t.exit_price == D("0.15")
    assert t.pnl_usd < D("4") * (D("0.15") - D("0.30")) + D("0.001")   # the bid loss, fees make it a bit worse
    assert trader.risk.open_exposure_usd == 0                       # exposure released by the exit
    assert trader.bankroll == D("500") + t.pnl_usd


async def test_no_stop_when_the_bid_holds_up():
    trader, buffer = make_trader(sqlite3.connect(":memory:"), config=cfg(30))
    market = await held_position(trader, buffer)
    await trader.on_orderbook_snapshot(market, make_book(yes_price="0.28"), T0 + timedelta(seconds=9))
    assert trader._position is not None and not trader.trades


async def test_stop_is_off_by_default():
    trader, buffer = make_trader(sqlite3.connect(":memory:"), config=BotConfig(sizing=Sizing(mode="fixed", contracts_per_trade=4)))
    market = await held_position(trader, buffer)
    await trader.on_orderbook_snapshot(market, make_book(yes_price="0.05"), T0 + timedelta(seconds=9))
    assert trader._position is not None and not trader.trades


async def test_a_stop_loss_resets_the_ramp_like_any_loss():
    trader, buffer = make_trader(sqlite3.connect(":memory:"), config=cfg(30, mode="ramp"))
    trader._ramp_level, trader._ramp_size = 3, D(4)
    market = await held_position(trader, buffer)
    await trader.on_orderbook_snapshot(market, make_book(yes_price="0.15"), T0 + timedelta(seconds=9))
    assert trader._ramp_level == 0 and trader._ramp_size == 4   # back to base (contracts_per_trade=4)


async def test_the_stop_tightens_with_the_ramp_level():
    trader, buffer = make_trader(sqlite3.connect(":memory:"), config=cfg(50, mode="ramp"))
    trader._ramp_level = 4
    market = await held_position(trader, buffer)          # opened at level 4: 50 - 4*8 = 18%
    assert trader._effective_stop_loss_pct() == D(18)
    await trader.on_orderbook_snapshot(market, make_book(yes_price="0.23"), T0 + timedelta(seconds=9))  # -23%
    assert len(trader.trades) == 1 and trader.trades[0].exit_reason == "stop_loss"


def test_the_demo_trader_does_not_use_exits_until_real_exit_orders_exist():
    from btcbot.demo_trader import DemoTrader
    assert DemoTrader._supports_exits is False
