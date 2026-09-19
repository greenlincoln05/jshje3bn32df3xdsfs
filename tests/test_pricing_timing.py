import math
import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest

from btcbot.model import EwmaVolatility, TimedVolatility
from btcbot.backtest import run_backtest
from btcbot.config import BotConfig
from btcbot.strategy import Action
from test_strategy import decide_with
from test_backtest import make_db, seed_fillable_window, insert_settlement, T0, TICKER
from test_live_paper import make_trader, make_market, make_book, feed_fresh_spot


def test_trade_message_frequency_does_not_change_second_returns():
    sparse, dense = TimedVolatility(900), TimedVolatility(900)
    for second in range(70):
        price = Decimal('80000') + second * (1 if second % 2 else -1)
        stamp = T0 + timedelta(seconds=second)
        sparse.update(price, stamp)
        for fraction in range(10):
            dense.update(price, stamp + timedelta(milliseconds=fraction*90))
    assert sparse.ready and dense.ready
    assert sparse.sigma == dense.sigma


def test_volatility_has_per_second_units_for_irregular_intervals():
    vol = EwmaVolatility(900)
    vol.update(.02, elapsed_sec=4)
    assert vol.sigma == pytest.approx(.01)
    with pytest.raises(ValueError):
        vol.update(.02, elapsed_sec=0)


def test_completed_bucket_only_and_out_of_order_ignored():
    vol = TimedVolatility(900, warmup_sec=1)
    vol.update(Decimal(100), T0)
    vol.update(Decimal(110), T0 + timedelta(seconds=1))
    assert vol.sigma == 0
    vol.update(Decimal(120), T0 + timedelta(seconds=2))
    assert vol.sigma == pytest.approx(math.log(1.1))
    before=vol.sigma
    vol.update(Decimal(999), T0)
    assert vol.sigma == before


def test_feed_gap_requires_new_warmup():
    vol=TimedVolatility(900)
    for i in range(63): vol.update(Decimal(100+i), T0+timedelta(seconds=i))
    assert vol.ready
    vol.update(Decimal(170),T0+timedelta(seconds=70))
    assert not vol.ready and vol.sigma==0


def test_partial_position_does_not_prevent_cancelling_remainder():
    assert decide_with(has_position=True,has_resting_order=True,tau_sec=10).action is Action.CANCEL
    assert decide_with(has_position=True,has_resting_order=True,spot_is_stale=True).action is Action.CANCEL


def test_stale_backtest_spot_does_not_open_new_position(tmp_path):
    conn=make_db(tmp_path)
    close=seed_fillable_window(conn,TICKER,start_ts=T0)
    conn.execute('DELETE FROM spot_ticks WHERE receive_ts > ?',((T0-timedelta(seconds=4)).isoformat(),))
    insert_settlement(conn,TICKER,'yes',strike=80000,close_time=close)
    assert run_backtest(conn,BotConfig()).trades==0
    conn.close()


async def test_live_warmup_does_not_log_or_trade_uninitialized_model():
    from btcbot.spot_feed import SpotTick
    import time
    conn=sqlite3.connect(':memory:');trader,buffer=make_trader(conn)
    price=Decimal(80000)
    trader.on_spot_tick(price,T0)
    buffer.add(SpotTick(price,'test',T0,T0,time.monotonic()))
    await trader.on_orderbook_snapshot(make_market(TICKER),make_book(),T0)
    assert trader._resting_order_id is None
    assert conn.execute('SELECT COUNT(*) FROM predictions').fetchone()[0]==0
    trader.close()


async def test_final_minute_without_settlement_average_does_not_open_order():
    conn=sqlite3.connect(':memory:');trader,buffer=make_trader(conn)
    feed_fresh_spot(trader,buffer)
    await trader.on_orderbook_snapshot(make_market(TICKER,close_time=T0+timedelta(seconds=45)),make_book(),T0)
    assert trader._resting_order_id is None
    trader.close()


def test_unannounced_settlement_does_not_release_exposure(tmp_path):
    conn=make_db(tmp_path)
    first_close=seed_fillable_window(conn,TICKER,start_ts=T0)
    second='KXBTC15M-SECOND'
    second_close=seed_fillable_window(conn,second,start_ts=first_close+timedelta(seconds=1))
    insert_settlement(conn,TICKER,'yes',strike=80000,close_time=first_close)
    conn.execute('UPDATE settlements SET finalized_poll_ts=? WHERE ticker=?',((first_close+timedelta(seconds=20)).isoformat(),TICKER))
    insert_settlement(conn,second,'yes',strike=80000,close_time=second_close)
    # A first-window fill uses $1.20. Its announced settlement is later than all
    # second-window entry attempts; releasing it at rollover admits a bad order.
    config=BotConfig(risk={'max_open_exposure_usd':Decimal('2')})
    report=run_backtest(conn,config)
    assert report.trades==1
    assert report.wins==1
    assert report.total_pnl_usd==Decimal('2.80')
    conn.close()


def test_missing_settlement_retains_exposure_during_next_window(tmp_path):
    conn=make_db(tmp_path)
    first_close=seed_fillable_window(conn,TICKER,start_ts=T0)
    second='KXBTC15M-SECOND'
    second_close=seed_fillable_window(conn,second,start_ts=first_close+timedelta(seconds=1))
    insert_settlement(conn,second,'yes',strike=80000,close_time=second_close)
    report=run_backtest(conn,BotConfig(risk={'max_open_exposure_usd':Decimal('2')}))
    assert report.trades==1 and report.unresolved==1
    assert report.total_pnl_usd==0
    conn.close()
