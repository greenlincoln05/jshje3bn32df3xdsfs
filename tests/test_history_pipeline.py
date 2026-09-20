"""Offline tests for the historical data pipeline: fake Kalshi source (no network) and a real in-memory
SQLite schema round-trip, matching tests/test_recorder.py's own conventions."""

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from btcbot.coinbase_history import Candle
from btcbot.history_pipeline import (
    fetch_all_settled_markets,
    init_history_schema,
    load_candles,
    load_market_outcomes,
    save_candles,
    save_market_outcomes,
)
from test_recorder import FakeKalshiSource, make_market

T0 = datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc)


def make_db():
    conn = sqlite3.connect(":memory:")
    init_history_schema(conn)
    return conn


class TestFetchAllSettledMarkets:
    async def test_requests_the_settled_status(self):
        markets = [make_market("T0", status="settled", raw_extra={"result": "yes"})]
        source = FakeKalshiSource(markets=[markets])
        calls = []
        original = source.list_markets

        async def spy(*, series_ticker, status=None):
            calls.append((series_ticker, status))
            return await original(series_ticker=series_ticker, status=status)

        source.list_markets = spy
        result = await fetch_all_settled_markets(source, series_ticker="KXBTC15M")

        assert result == markets
        assert calls == [("KXBTC15M", "settled")]


class TestMarketOutcomes:
    def test_round_trips_settled_markets(self):
        conn = make_db()
        markets = [
            make_market("T0", status="settled", strike="80000", raw_extra={"result": "yes"}),
            make_market("T1", status="settled", strike="81000", raw_extra={"result": "no"}),
        ]

        written = save_market_outcomes(conn, markets)
        outcomes = load_market_outcomes(conn)

        assert written == 2
        assert [o.ticker for o in outcomes] == ["T0", "T1"]
        assert outcomes[0].result == "yes" and outcomes[0].strike == Decimal("80000")
        assert outcomes[1].result == "no"

    def test_skips_markets_with_no_result(self):
        conn = make_db()
        markets = [make_market("T0", status="active")]  # never settled, no result

        written = save_market_outcomes(conn, markets)

        assert written == 0
        assert load_market_outcomes(conn) == []

    def test_re_saving_the_same_ticker_replaces_rather_than_duplicates(self):
        conn = make_db()
        save_market_outcomes(conn, [make_market("T0", status="settled", raw_extra={"result": "yes"})])
        save_market_outcomes(conn, [make_market("T0", status="settled", raw_extra={"result": "no"})])

        outcomes = load_market_outcomes(conn)

        assert len(outcomes) == 1
        assert outcomes[0].result == "no"

    def test_orders_by_close_time(self):
        conn = make_db()
        late = replace(make_market("T_LATE", status="settled", raw_extra={"result": "yes"}), close_time=T0 + timedelta(days=1))
        early = make_market("T_EARLY", status="settled", raw_extra={"result": "no"})
        save_market_outcomes(conn, [late, early])

        outcomes = load_market_outcomes(conn)

        assert [o.ticker for o in outcomes] == ["T_EARLY", "T_LATE"]


class TestCandles:
    def test_round_trips_candles(self):
        conn = make_db()
        candles = [
            Candle(T0, Decimal("79900"), Decimal("80100"), Decimal("80000"), Decimal("80050"), Decimal("12.5")),
            Candle(T0 + timedelta(minutes=1), Decimal("80000"), Decimal("80200"), Decimal("80050"), Decimal("80150"), Decimal("8.0")),
        ]

        written = save_candles(conn, candles)
        loaded = load_candles(conn)

        assert written == 2
        assert loaded == candles

    def test_re_saving_the_same_minute_replaces_rather_than_duplicates(self):
        conn = make_db()
        save_candles(conn, [Candle(T0, Decimal(1), Decimal(2), Decimal(1), Decimal(1), Decimal(1))])
        save_candles(conn, [Candle(T0, Decimal(1), Decimal(2), Decimal(1), Decimal("1.5"), Decimal(1))])

        loaded = load_candles(conn)

        assert len(loaded) == 1
        assert loaded[0].close == Decimal("1.5")
