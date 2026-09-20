import sqlite3
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from btcbot.config import BotConfig, Sizing, SizingMode
from btcbot.live_paper import LivePaperTrader
from btcbot.models import Market, OrderBook, PriceLevel
from btcbot.paper_broker import QueueAssumption
from btcbot.spot_feed import SpotBuffer, SpotTick

T0 = datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc)
NO_KILL_FILE = "/tmp/btcbot-test-should-never-exist-kill-file"
TICKER = "KXBTC15M-26SEP190000-00"


def make_market(ticker, *, strike="80000", status="active", close_time=None, raw_extra=None):
    return Market(
        ticker=ticker,
        event_ticker=ticker.rsplit("-", 1)[0],
        status=status,
        title="Bitcoin price up or down",
        open_time=T0 - timedelta(minutes=5),
        close_time=close_time or (T0 + timedelta(seconds=500)),
        strike=None if strike is None else Decimal(strike),
        strike_type="greater_or_equal",
        volume=Decimal(0),
        open_interest=Decimal(0),
        raw=raw_extra or {},
    )


def make_book(yes_price="0.30", yes_size="15", no_price="0.68", no_size="15"):
    return OrderBook(
        "T",
        yes_bids=(PriceLevel(Decimal(yes_price), Decimal(yes_size)),),
        no_bids=(PriceLevel(Decimal(no_price), Decimal(no_size)),),
    )


def make_trader(conn, *, config=None, buffer=None, **kwargs):
    buffer = buffer or SpotBuffer(window_sec=5.0, stale_after_sec=3.0)
    # Fixed-size by default: percent-of-account is the shipped live default (test_percent_sizing.py), but a
    # bare "5 contracts every order" is what most of this file's tests actually rely on for order-mechanics
    # assertions unrelated to sizing mode. Tests exercising a specific mode pass their own `config=`.
    trader = LivePaperTrader(conn, config or BotConfig(sizing=Sizing(mode="fixed")), buffer, kill_file=NO_KILL_FILE, **kwargs)
    return trader, buffer


def feed_fresh_spot(trader, buffer, price=Decimal("80000"), *, ts=T0):
    """Give the buffer a fresh (non-stale) tick and the trader an EWMA history for it."""
    if not trader._vol.ready:
        for i in range(62, 0, -1):
            trader.on_spot_tick(price, ts - timedelta(seconds=i))
    trader.on_spot_tick(price, ts)
    buffer.add(SpotTick(price=price, source="test", source_ts=None, receive_ts=ts, monotonic_ts=time.monotonic()))


def feed_stale_spot(trader, buffer, price=Decimal("80000"), *, ts=T0):
    if not trader._vol.ready:
        for i in range(62, 0, -1):
            trader.on_spot_tick(price, ts - timedelta(seconds=i))
    trader.on_spot_tick(price, ts)
    buffer.add(SpotTick(price=price, source="test", source_ts=None, receive_ts=ts, monotonic_ts=time.monotonic() - 100))


async def fill_a_window(trader, buffer, ticker=TICKER, *, strike="80000", close_time=None, start_ts=T0):
    """Drive one window through enough snapshots to place and fill a resting YES order (mirrors
    test_backtest.py's seed_fillable_window shrink pattern), leaving a filled, unsettled position."""
    for i in range(4):
        feed_fresh_spot(trader, buffer, Decimal("80000") + i, ts=start_ts + timedelta(seconds=i))
    market = make_market(ticker, strike=strike, close_time=close_time)
    for offset, size in enumerate([15, 14, 13, 12, 11, 10, 14, 0]):
        await trader.on_orderbook_snapshot(market, make_book(yes_size=str(size)), start_ts + timedelta(seconds=offset))
    return market


class TestBasicFillAndSettlement:
    async def test_a_win_produces_a_correctly_priced_trade(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        trader, buffer = make_trader(conn)
        market = await fill_a_window(trader, buffer)
        # 4 of the order's 5 contracts filled (see fill_a_window's shrink sequence); the order still rests
        # with 1 contract unfilled until the window rolls over and cancels the remainder.
        assert trader._position is not None and trader._position.size == 4
        assert trader._resting_order_id is not None

        next_market = make_market("KXBTC15M-26SEP190015-15", close_time=T0 + timedelta(seconds=1500))
        await trader.on_orderbook_snapshot(next_market, make_book(), T0 + timedelta(seconds=600))
        assert trader._position is None
        assert market.ticker in trader._pending_settlements

        settled = make_market(market.ticker, status="finalized", raw_extra={"result": "yes", "expiration_value": "80500"})
        await trader.on_settlement(settled)

        assert len(trader.trades) == 1
        trade = trader.trades[0]
        assert trade.result == "yes"
        assert trade.size == 4  # matches the shrink-to-0-then-refill-to-14-then-0 sequence
        assert trade.pnl_usd == Decimal("4") * (Decimal(1) - Decimal("0.30"))
        assert trader.unresolved_count == 0
        trader.close()

    async def test_a_loss_is_priced_correctly(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        trader, buffer = make_trader(conn)
        market = await fill_a_window(trader, buffer)
        next_market = make_market("T2", close_time=T0 + timedelta(seconds=1500))
        await trader.on_orderbook_snapshot(next_market, make_book(), T0 + timedelta(seconds=600))

        settled = make_market(market.ticker, status="finalized", raw_extra={"result": "no"})
        await trader.on_settlement(settled)

        assert trader.trades[0].result == "no"
        assert trader.trades[0].pnl_usd == Decimal("-4") * Decimal("0.30")
        trader.close()

    async def test_report_reflects_the_configured_assumptions(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        trader, buffer = make_trader(conn, queue_assumption=QueueAssumption.PESSIMISTIC, maker_fee_multiplier=Decimal("0.25"))
        report = trader.report()
        assert report.queue_assumption == "pessimistic"
        assert report.maker_fee_multiplier == Decimal("0.25")
        trader.close()


class TestPriceBandAndKellySizing:
    """strike=70000 with a flat (zero-volatility) spot warmup at 80000 clamps the model to its p_model=0.98
    ceiling (model.py's CLAMP_HIGH) regardless of horizon -- a simple, deterministic way to get a strong,
    reproducible model opinion without needing real volatility."""

    async def test_the_default_price_band_refuses_a_trade_the_model_would_otherwise_take(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        trader, buffer = make_trader(conn)  # default BotConfig: max_price=0.85
        feed_fresh_spot(trader, buffer, Decimal("80000"), ts=T0)
        market = make_market(TICKER, strike="70000")
        book = make_book(yes_price="0.90", yes_size="20", no_price="0.05", no_size="20")  # yes ask price band

        await trader.on_orderbook_snapshot(market, book, T0)

        assert trader._resting_order_id is None
        trader.close()

    async def test_disabling_the_band_lets_the_same_trade_through(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        trader, buffer = make_trader(conn, config=BotConfig(max_price=None))
        feed_fresh_spot(trader, buffer, Decimal("80000"), ts=T0)
        market = make_market(TICKER, strike="70000")
        book = make_book(yes_price="0.90", yes_size="20", no_price="0.05", no_size="20")

        await trader.on_orderbook_snapshot(market, book, T0)

        assert trader._resting_order_id is not None
        trader.close()

    async def test_kelly_mode_sizes_by_edge_instead_of_a_flat_contract_count(self, tmp_path):
        # p_blend = 0.5*0.98 + 0.5*mid(0.90, 0.95) = 0.9525; kelly_fraction=(0.9525-0.90)/0.10=0.525;
        # 0.525 * 0.2 multiplier * $25 bankroll = $2.625 -> floor($2.625 / $0.90) = 2 contracts (not 5).
        conn = sqlite3.connect(":memory:")
        config = BotConfig(
            max_price=None, sizing=Sizing(contracts_per_trade=5, mode=SizingMode.KELLY, kelly_fraction_multiplier=0.2)
        )
        trader, buffer = make_trader(conn, config=config)
        feed_fresh_spot(trader, buffer, Decimal("80000"), ts=T0)
        market = make_market(TICKER, strike="70000")
        book = make_book(yes_price="0.90", yes_size="20", no_price="0.05", no_size="20")

        await trader.on_orderbook_snapshot(market, book, T0)

        assert trader._resting_order_id is not None
        assert trader.risk.open_exposure_usd == Decimal("0.90") * Decimal(2)
        trader.close()


class TestUnresolvedHandling:
    async def test_settlement_for_an_unknown_ticker_is_a_no_op(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        trader, buffer = make_trader(conn)
        await trader.on_settlement(make_market("never-traded", status="finalized", raw_extra={"result": "yes"}))
        assert trader.trades == []
        trader.close()

    async def test_a_non_yes_no_result_is_ignored_defensively(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        trader, buffer = make_trader(conn)
        market = await fill_a_window(trader, buffer)
        next_market = make_market("T2", close_time=T0 + timedelta(seconds=1500))
        await trader.on_orderbook_snapshot(next_market, make_book(), T0 + timedelta(seconds=600))

        await trader.on_settlement(make_market(market.ticker, status="finalized", raw_extra={"result": None}))
        assert trader.trades == []
        assert trader.unresolved_count == 1
        trader.close()

    async def test_shutdown_reports_a_still_pending_position_as_unresolved(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        trader, buffer = make_trader(conn)
        market = await fill_a_window(trader, buffer)
        next_market = make_market("T2", close_time=T0 + timedelta(seconds=1500))
        await trader.on_orderbook_snapshot(next_market, make_book(), T0 + timedelta(seconds=600))
        assert market.ticker in trader._pending_settlements

        await trader.shutdown(T0 + timedelta(seconds=700))

        assert len(trader.trades) == 1
        assert trader.trades[0].result is None and trader.trades[0].pnl_usd is None
        report = trader.report()
        assert report.unresolved == 1 and report.trades == 1
        trader.close()

    async def test_shutdown_finalizes_a_still_open_position_in_the_current_window_too(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        trader, buffer = make_trader(conn)
        await fill_a_window(trader, buffer)  # never rolls over: position stays "current", not yet pending

        await trader.shutdown(T0 + timedelta(seconds=700))

        assert len(trader.trades) == 1
        assert trader.trades[0].result is None
        trader.close()

    async def test_shutdown_cancels_a_still_resting_unfilled_order(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        trader, buffer = make_trader(conn)
        feed_fresh_spot(trader, buffer)
        market = make_market(TICKER)
        # depth never shrinks: order rests but never fills
        await trader.on_orderbook_snapshot(market, make_book(yes_size="20"), T0)
        order_id = trader._resting_order_id
        assert order_id is not None

        await trader.shutdown(T0 + timedelta(seconds=5))

        assert trader.trades == []  # nothing was ever filled, so nothing to report as a trade
        assert trader._resting_order_id is None
        assert trader._broker.get_order(order_id).status == "cancelled"
        trader.close()


class TestPartialFills:
    async def test_partial_fills_average_into_one_position(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        trader, buffer = make_trader(conn)
        feed_fresh_spot(trader, buffer)
        market = make_market(TICKER)

        # queue_ahead=10 at placement (order size is the default contracts_per_trade, 5): drain to 0 (reaches
        # the front, no fill), replenish to 3, drain to 0 (fills 3 of 5), replenish to 2, drain to 0 (fills
        # the remaining 2) -- two partial fills at the same price average trivially to that price.
        sizes = [10, 0, 3, 0]
        for offset, size in enumerate(sizes):
            await trader.on_orderbook_snapshot(market, make_book(yes_size=str(size)), T0 + timedelta(seconds=offset))
        assert trader._position is not None and trader._position.size == 3
        assert trader._resting_order_id is not None  # 2 contracts still unfilled

        for offset, size in enumerate([2, 0], start=len(sizes)):
            await trader.on_orderbook_snapshot(market, make_book(yes_size=str(size)), T0 + timedelta(seconds=offset))

        assert trader._position.size == 5
        assert trader._position.entry_price == Decimal("0.30")
        assert trader._resting_order_id is None  # fully filled
        trader.close()


class TestGuards:
    async def test_no_strike_yet_skips_the_tick_entirely(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        trader, buffer = make_trader(conn)
        feed_fresh_spot(trader, buffer)
        market = make_market(TICKER, strike=None)
        await trader.on_orderbook_snapshot(market, make_book(), T0)
        assert trader._resting_order_id is None and trader._position is None

    async def test_no_spot_data_yet_skips_the_tick(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        trader, buffer = make_trader(conn)  # no spot ticks fed at all
        market = make_market(TICKER)
        await trader.on_orderbook_snapshot(market, make_book(), T0)
        assert trader._resting_order_id is None

    async def test_stale_spot_feed_prevents_new_entries(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        trader, buffer = make_trader(conn)
        feed_stale_spot(trader, buffer)
        market = make_market(TICKER)
        await trader.on_orderbook_snapshot(market, make_book(), T0)
        assert trader._resting_order_id is None

    async def test_a_snapshot_polled_after_close_is_ignored(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        trader, buffer = make_trader(conn)
        feed_fresh_spot(trader, buffer)
        market = make_market(TICKER, close_time=T0 - timedelta(seconds=1))  # already closed
        await trader.on_orderbook_snapshot(market, make_book(), T0)
        assert trader._resting_order_id is None


class TestRiskIntegration:
    async def test_a_consecutive_loss_pause_blocks_the_next_windows_entry(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        config = BotConfig(
            risk={
                "max_contracts_per_trade": 10,
                "max_open_exposure_usd": Decimal("25"),
                "daily_loss_limit_usd": Decimal("20"),
                "max_consecutive_losses": 1,
                "max_trades_per_hour": 12,
            }
        )
        trader, buffer = make_trader(conn, config=config)

        market = await fill_a_window(trader, buffer)
        next_market = make_market("T2", close_time=T0 + timedelta(seconds=1500))
        await trader.on_orderbook_snapshot(next_market, make_book(), T0 + timedelta(seconds=600))
        await trader.on_settlement(make_market(market.ticker, status="finalized", raw_extra={"result": "no"}))
        assert trader.risk.is_paused is True

        # a fresh window with an obviously fillable setup: no order should be placed while paused
        feed_fresh_spot(trader, buffer, ts=T0 + timedelta(seconds=601))
        for offset, size in enumerate([15, 14, 13, 12, 11, 10, 14, 0]):
            await trader.on_orderbook_snapshot(
                next_market, make_book(yes_size=str(size)), T0 + timedelta(seconds=610 + offset)
            )
        assert trader._resting_order_id is None and trader._position is None
        trader.close()


class TestSettlementArrivingBeforeTheNextWindow:
    """Regression: on a real prod run, windows 2 and 3 filled but were never logged, because Kalshi finalized them
    (17:30:13, 17:45:07) before the next window's first snapshot (17:30:23, 17:45:15) reached the trader."""

    async def test_a_settlement_that_beats_the_rollover_still_resolves_the_trade(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        trader, buffer = make_trader(conn)
        market = await fill_a_window(trader, buffer)
        assert trader._position is not None and market.ticker not in trader._pending_settlements

        settled = make_market(market.ticker, status="finalized", raw_extra={"result": "yes", "expiration_value": "80500"})
        await trader.on_settlement(settled)  # arrives with no next-window snapshot yet

        assert len(trader.trades) == 1 and trader._position is None
        assert trader.trades[0].result == "yes"
        assert trader.trades[0].pnl_usd == Decimal("4") * (Decimal(1) - Decimal("0.30"))
        assert trader._resting_order_id is None  # the closed market's unfilled remainder was cancelled
        assert trader.risk.open_exposure_usd == Decimal("0")  # capital released, so later windows can still trade
        assert conn.execute("SELECT COUNT(*) FROM trades WHERE result IS NOT NULL").fetchone()[0] == 1
        trader.close()

    async def test_a_loss_that_beats_the_rollover_still_counts_toward_the_loss_streak(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        trader, buffer = make_trader(conn)
        market = await fill_a_window(trader, buffer)
        await trader.on_settlement(make_market(market.ticker, status="finalized", raw_extra={"result": "no"}))
        assert trader.trades[0].pnl_usd < 0 and trader.risk.consecutive_losses == 1
        trader.close()

    async def test_it_is_not_resolved_twice_when_the_rollover_comes_later(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        trader, buffer = make_trader(conn)
        market = await fill_a_window(trader, buffer)
        await trader.on_settlement(make_market(market.ticker, status="finalized", raw_extra={"result": "yes"}))
        next_market = make_market("KXBTC15M-26SEP190015-15", close_time=T0 + timedelta(seconds=1500))
        await trader.on_orderbook_snapshot(next_market, make_book(), T0 + timedelta(seconds=600))
        await trader.shutdown(T0 + timedelta(seconds=700))
        assert len(trader.trades) == 1 and trader.unresolved_count == 0
        trader.close()

    async def test_a_settlement_for_a_different_ticker_leaves_the_position_alone(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        trader, buffer = make_trader(conn)
        await fill_a_window(trader, buffer)
        await trader.on_settlement(make_market("KXBTC15M-OTHER-00", status="finalized", raw_extra={"result": "yes"}))
        assert trader._position is not None and trader.trades == []
        trader.close()
