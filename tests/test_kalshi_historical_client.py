"""Offline client tests for the five historical-backfill methods (Part A1 of
docs/research/kalshi-history-backfill-handoff.md): get_historical_cutoff, list_historical_markets,
get_historical_trades, get_market_candlesticks, get_historical_candlesticks. Same Script/make_client
conventions as test_client.py; never touches the network."""

from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from test_client import Script, make_client, ok

from btcbot.config import KalshiEnv
from btcbot.kalshi_client import KalshiAuth, KalshiClient, KalshiError
from btcbot.models import ParseError

CUTOFF = {
    "market_settled_ts": "2026-07-24T00:00:00Z", "trades_created_ts": "2026-07-24T00:00:00Z",
    "orders_updated_ts": "2026-07-24T00:00:00Z", "market_positions_last_updated_ts": "2026-07-24T00:00:00Z",
}
TRADE = {
    "trade_id": "t1", "ticker": "KXBTC15M-26JUL172000-00", "count_fp": "1.00",
    "yes_price_dollars": "0.5000", "no_price_dollars": "0.5000", "taker_outcome_side": "yes",
    "taker_book_side": "bid", "created_time": "2026-07-17T23:59:59Z", "is_block_trade": False,
}
CANDLE_HIST = {
    "end_period_ts": 1784331960, "open_interest": "1.00", "volume": "2.00",
    "price": {"close": "0.5", "high": "0.5", "low": "0.5", "mean": "0.5", "open": "0.5", "previous": None},
    "yes_bid": {"close": "0.5", "high": "0.5", "low": "0.5", "open": "0.5"},
    "yes_ask": {"close": "0.5", "high": "0.5", "low": "0.5", "open": "0.5"},
}
MARKET = {
    "ticker": "T", "event_ticker": "E", "status": "settled", "title": "t",
    "open_time": "2026-01-01T00:00:00Z", "close_time": "2026-01-01T00:15:00Z", "result": "yes",
}
START = datetime(2026, 7, 17, 23, 45, tzinfo=timezone.utc)
END = datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc)


class TestGetHistoricalCutoff:
    async def test_parses_the_response(self):
        script = Script(ok(CUTOFF))
        client, _ = make_client(script)
        async with client:
            c = await client.get_historical_cutoff()
        assert script.requests[0].url.path == "/trade-api/v2/historical/cutoff"
        assert c.market_settled_ts.isoformat() == "2026-07-24T00:00:00+00:00"


class TestListHistoricalMarkets:
    async def test_builds_the_documented_query_and_paginates(self):
        script = Script(
            ok({"markets": [MARKET], "cursor": "p2"}),
            ok({"markets": [{**MARKET, "ticker": "T2"}], "cursor": ""}),
        )
        client, _ = make_client(script)
        async with client:
            markets = await client.list_historical_markets(series_ticker="KXBTC15M")
        assert script.requests[0].url.path == "/trade-api/v2/historical/markets"
        assert dict(script.requests[0].url.params) == {"series_ticker": "KXBTC15M", "limit": "1000"}
        assert [m.ticker for m in markets] == ["T", "T2"]

    async def test_rejects_a_repeated_cursor(self):
        script = Script(ok({"markets": [], "cursor": "loop"}))
        client, _ = make_client(script)
        async with client:
            with pytest.raises(KalshiError, match="repeated.*cursor"):
                await client.list_historical_markets(series_ticker="S")
        assert len(script.requests) == 2


class TestGetHistoricalTrades:
    async def test_builds_the_documented_query_with_min_and_max_ts(self):
        script = Script(ok({"trades": [TRADE], "cursor": ""}))
        client, _ = make_client(script)
        async with client:
            trades = await client.get_historical_trades("T", min_ts=START, max_ts=END)
        assert script.requests[0].url.path == "/trade-api/v2/historical/trades"
        params = dict(script.requests[0].url.params)
        assert params["ticker"] == "T" and params["min_ts"] == str(int(START.timestamp())) and params["max_ts"] == str(int(END.timestamp()))
        assert trades[0].taker_side == "yes"

    async def test_paginates_and_sorts_oldest_first(self):
        older = {**TRADE, "trade_id": "old", "created_time": "2026-07-17T20:00:00Z"}
        newer = {**TRADE, "trade_id": "new", "created_time": "2026-07-17T23:00:00Z"}
        script = Script(ok({"trades": [newer], "cursor": "p2"}), ok({"trades": [older], "cursor": ""}))
        client, _ = make_client(script)
        async with client:
            trades = await client.get_historical_trades("T")
        assert [t.trade_id for t in trades] == ["old", "new"]

    async def test_respects_max_pages(self):
        script = Script(ok({"trades": [TRADE], "cursor": "again"}))
        client, _ = make_client(script)
        async with client:
            trades = await client.get_historical_trades("T", max_pages=2)
        assert len(script.requests) == 2 and len(trades) == 2  # both pages accepted, loop just stopped


class TestCandlesticks:
    async def test_historical_has_no_series_in_the_path(self):
        script = Script(ok({"candlesticks": [CANDLE_HIST], "ticker": "T"}))
        client, _ = make_client(script)
        async with client:
            candles = await client.get_historical_candlesticks("T", start=START, end=END)
        assert script.requests[0].url.path == "/trade-api/v2/historical/markets/T/candlesticks"
        params = dict(script.requests[0].url.params)
        assert params == {"start_ts": str(int(START.timestamp())), "end_ts": str(int(END.timestamp())), "period_interval": "1"}
        assert candles[0].price_close == Decimal("0.5")

    async def test_live_has_the_series_in_the_path(self):
        script = Script(ok({"candlesticks": [CANDLE_HIST], "ticker": "T"}))
        client, _ = make_client(script)
        async with client:
            await client.get_market_candlesticks("KXBTC15M", "T", start=START, end=END)
        assert script.requests[0].url.path == "/trade-api/v2/series/KXBTC15M/markets/T/candlesticks"

    async def test_a_non_list_candlesticks_field_is_a_parse_error(self):
        script = Script(ok({"candlesticks": "nope"}))
        client, _ = make_client(script)
        async with client:
            with pytest.raises(ParseError):
                await client.get_historical_candlesticks("T", start=START, end=END)


class TestNoneOfTheseEverSign:
    """None of the five historical methods may send an auth header, even when the client HAS credentials --
    they are public GETs (Part A1's explicit requirement)."""

    async def test_no_auth_header_sent_even_with_credentials_configured(self, rsa_key):
        script = Script(
            ok(CUTOFF), ok({"markets": [], "cursor": ""}), ok({"trades": [], "cursor": ""}),
            ok({"candlesticks": []}), ok({"candlesticks": []}),
        )
        client, _ = make_client(script, auth=KalshiAuth("key-id", rsa_key, clock_ms=lambda: 1))
        async with client:
            await client.get_historical_cutoff()
            await client.list_historical_markets(series_ticker="S")
            await client.get_historical_trades("T")
            await client.get_historical_candlesticks("T", start=START, end=END)
            await client.get_market_candlesticks("S", "T", start=START, end=END)
        for req in script.requests:
            assert "KALSHI-ACCESS-KEY" not in req.headers
            assert "KALSHI-ACCESS-SIGNATURE" not in req.headers
