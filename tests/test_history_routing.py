"""Offline tests for A2's routing helpers (fetch_market_trades/fetch_market_candles) and A3's storage
(market_candles, backfill_progress) -- docs/research/kalshi-history-backfill-handoff.md."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from test_recorder import FakeKalshiSource

from btcbot.history_pipeline import (
    RoutingStraddle,
    fetch_market_candles,
    fetch_market_trades,
    init_history_schema,
    is_market_done,
    load_market_candles,
    save_backfill_progress,
    save_market_candles,
)
from btcbot.models import HistoricalCutoff, Market, MarketCandle, Trade

T0 = datetime(2026, 7, 17, 23, 45, tzinfo=timezone.utc)
T15 = T0 + timedelta(minutes=15)


def market(ticker="T", open_time=T0, close_time=T15):
    return Market(
        ticker=ticker, event_ticker="E", status="settled", title="t", open_time=open_time, close_time=close_time,
        strike=Decimal("80000"), strike_type="greater_or_equal", volume=None, open_interest=None,
        raw={"result": "yes"},
    )


def trade(ticker, ts, tid="a"):
    return Trade(ticker=ticker, trade_id=tid, count=Decimal(1), yes_price=Decimal("0.5"), no_price=Decimal("0.5"),
                taker_side="yes", created_time=ts)


def candle(ticker, end_ts):
    return MarketCandle(
        ticker=ticker, end_ts=end_ts, yes_bid_open=None, yes_bid_high=None, yes_bid_low=None, yes_bid_close=None,
        yes_ask_open=None, yes_ask_high=None, yes_ask_low=None, yes_ask_close=None,
        price_open=None, price_high=None, price_low=None, price_close=None, price_mean=None, price_previous=None,
        volume=Decimal(1), open_interest=Decimal(1),
    )


def cutoff(ts):
    return HistoricalCutoff(market_settled_ts=ts, trades_created_ts=ts, orders_updated_ts=None, market_positions_last_updated_ts=None)


class TestFetchMarketTradesRouting:
    async def test_fully_before_the_cutoff_calls_only_historical(self):
        m = market()
        c = cutoff(T15 + timedelta(days=1))  # cutoff well after this window
        client = FakeKalshiSource(historical_trades={m.ticker: [trade(m.ticker, T0)]})
        trades, straddle = await fetch_market_trades(client, m, c)
        assert len(trades) == 1 and straddle is None
        assert client.calls["get_historical_trades"] == 1 and client.calls["get_trades"] == 0

    async def test_fully_after_the_cutoff_calls_only_live(self):
        m = market()
        c = cutoff(T0 - timedelta(days=1))  # cutoff well before this window
        client = FakeKalshiSource(live_trades={m.ticker: [trade(m.ticker, T0)]})
        trades, straddle = await fetch_market_trades(client, m, c)
        assert len(trades) == 1 and straddle is None
        assert client.calls["get_trades"] == 1 and client.calls["get_historical_trades"] == 0

    async def test_straddling_calls_both_and_dedupes_by_trade_id(self):
        m = market()
        c = cutoff(T0 + timedelta(minutes=7))  # inside the window: a real straddle
        shared = trade(m.ticker, T0, tid="shared")
        client = FakeKalshiSource(
            historical_trades={m.ticker: [shared]},
            live_trades={m.ticker: [shared, trade(m.ticker, T0 + timedelta(minutes=1), tid="live-only")]},
        )
        trades, straddle = await fetch_market_trades(client, m, c)
        assert sorted(t.trade_id for t in trades) == ["live-only", "shared"]  # "shared" counted once
        assert isinstance(straddle, RoutingStraddle) and straddle.what == "trades"
        assert client.calls["get_historical_trades"] == 1 and client.calls["get_trades"] == 1

    async def test_window_end_exactly_at_the_cutoff_straddles_rather_than_going_historical_only(self):
        # The cutoff's own docstring says records OLDER than it are historical-only; a window whose end lands
        # exactly ON the cutoff is not older than it, so it must not take the historical-only branch (which
        # would silently miss a live record printed at that exact instant with no straddle/fallback).
        m = market()
        end = m.close_time + timedelta(seconds=10)  # matches fetch_market_trades' own grace window
        c = cutoff(end)
        client = FakeKalshiSource(historical_trades={m.ticker: []}, live_trades={m.ticker: []})
        _, straddle = await fetch_market_trades(client, m, c)
        assert isinstance(straddle, RoutingStraddle)
        assert client.calls["get_historical_trades"] == 1 and client.calls["get_trades"] == 1


class TestFetchMarketCandlesRouting:
    async def test_fully_before_the_cutoff_calls_only_historical(self):
        m = market()
        c = cutoff(T15 + timedelta(days=1))
        client = FakeKalshiSource(historical_candles={m.ticker: [candle(m.ticker, T0 + timedelta(minutes=1))]})
        candles, straddle = await fetch_market_candles(client, "KXBTC15M", m, c)
        assert len(candles) == 1 and straddle is None
        assert client.calls["get_historical_candlesticks"] == 1 and client.calls["get_market_candlesticks"] == 0

    async def test_fully_after_the_cutoff_calls_only_live(self):
        m = market()
        c = cutoff(T0 - timedelta(days=1))
        client = FakeKalshiSource(live_candles={m.ticker: [candle(m.ticker, T0 + timedelta(minutes=1))]})
        candles, straddle = await fetch_market_candles(client, "KXBTC15M", m, c)
        assert len(candles) == 1 and straddle is None
        assert client.calls["get_market_candlesticks"] == 1 and client.calls["get_historical_candlesticks"] == 0

    async def test_straddling_calls_both_and_dedupes_by_end_ts(self):
        m = market()
        c = cutoff(T0 + timedelta(minutes=7))
        shared = candle(m.ticker, T0 + timedelta(minutes=1))
        client = FakeKalshiSource(
            historical_candles={m.ticker: [shared]},
            live_candles={m.ticker: [shared, candle(m.ticker, T0 + timedelta(minutes=10))]},
        )
        candles, straddle = await fetch_market_candles(client, "KXBTC15M", m, c)
        assert len(candles) == 2 and isinstance(straddle, RoutingStraddle) and straddle.what == "candles"

    async def test_window_end_exactly_at_the_cutoff_straddles_rather_than_going_historical_only(self):
        m = market()
        end = m.close_time + timedelta(minutes=1)  # matches fetch_market_candles' own grace window
        c = cutoff(end)
        client = FakeKalshiSource(historical_candles={m.ticker: []}, live_candles={m.ticker: []})
        _, straddle = await fetch_market_candles(client, "KXBTC15M", m, c)
        assert isinstance(straddle, RoutingStraddle)
        assert client.calls["get_historical_candlesticks"] == 1 and client.calls["get_market_candlesticks"] == 1


class TestMarketCandleStorage:
    def test_round_trips_including_null_price_fields(self):
        conn = __import__("sqlite3").connect(":memory:")
        init_history_schema(conn)
        c = candle("T", T0)
        n = save_market_candles(conn, [c])
        assert n == 1
        back = load_market_candles(conn, "T")
        assert back == [c]

    def test_filters_by_ticker(self):
        conn = __import__("sqlite3").connect(":memory:")
        init_history_schema(conn)
        save_market_candles(conn, [candle("A", T0), candle("B", T0)])
        assert [c.ticker for c in load_market_candles(conn, "A")] == ["A"]
        assert len(load_market_candles(conn)) == 2

    def test_insert_or_replace_is_idempotent(self):
        conn = __import__("sqlite3").connect(":memory:")
        init_history_schema(conn)
        save_market_candles(conn, [candle("T", T0)])
        save_market_candles(conn, [candle("T", T0)])
        assert len(load_market_candles(conn, "T")) == 1


class TestBackfillProgress:
    def test_is_market_done_and_resume(self):
        conn = __import__("sqlite3").connect(":memory:")
        init_history_schema(conn)
        assert is_market_done(conn, "T") is False
        save_backfill_progress(conn, "T", trades_done=True, candles_done=True, trade_count=5, fetched_at=T0)
        assert is_market_done(conn, "T") is True

    def test_a_different_ticker_is_unaffected(self):
        conn = __import__("sqlite3").connect(":memory:")
        init_history_schema(conn)
        save_backfill_progress(conn, "T1", trades_done=True, candles_done=True, trade_count=0, fetched_at=T0)
        assert is_market_done(conn, "T2") is False

    def test_a_trades_only_run_is_not_done_for_a_run_that_also_wants_candles(self):
        # A prior `--no-candles` run wrote trades_done=1, candles_done=0. A later run that wants both must
        # not skip this market just because a row exists -- it still needs to fetch candles.
        conn = __import__("sqlite3").connect(":memory:")
        init_history_schema(conn)
        save_backfill_progress(conn, "T", trades_done=True, candles_done=False, trade_count=5, fetched_at=T0)
        assert is_market_done(conn, "T") is False
        assert is_market_done(conn, "T", need_candles=False) is True
        assert is_market_done(conn, "T", need_trades=False) is False

    def test_a_candles_only_run_is_not_done_for_a_run_that_also_wants_trades(self):
        conn = __import__("sqlite3").connect(":memory:")
        init_history_schema(conn)
        save_backfill_progress(conn, "T", trades_done=False, candles_done=True, trade_count=0, fetched_at=T0)
        assert is_market_done(conn, "T") is False
        assert is_market_done(conn, "T", need_trades=False) is True
        assert is_market_done(conn, "T", need_candles=False) is False
