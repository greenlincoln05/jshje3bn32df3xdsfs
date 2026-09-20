"""Offline tests for the Coinbase 1-minute candle downloader: httpx.MockTransport only, no network."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from btcbot.coinbase_history import (
    MAX_CANDLES_PER_REQUEST,
    Candle,
    CoinbaseHistoryError,
    fetch_candle_history,
    fetch_candles,
)

T0 = datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc)


class Recorder:
    """Records every request a handler sees, then answers with whatever ``pages`` (a list of row-lists)
    gives next, repeating the last page once exhausted."""

    def __init__(self, *pages):
        self.pages = list(pages)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        rows = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        return httpx.Response(200, json=rows)


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# Coinbase returns [time_unix, low, high, open, close, volume], newest-first.
def row(ts: datetime, price: str) -> list:
    return [int(ts.timestamp()), price, price, price, price, "10.5"]


class TestFetchCandles:
    async def test_parses_and_sorts_oldest_first(self):
        recorder = Recorder([row(T0 + timedelta(minutes=2), "80100"), row(T0, "80000"), row(T0 + timedelta(minutes=1), "80050")])
        async with make_client(recorder) as client:
            candles = await fetch_candles(client, start=T0, end=T0 + timedelta(minutes=3))

        assert [c.start for c in candles] == [T0, T0 + timedelta(minutes=1), T0 + timedelta(minutes=2)]
        assert candles[0].close == Decimal("80000")
        assert candles[0].volume == Decimal("10.5")

    async def test_non_200_raises(self):
        def handler(request):
            return httpx.Response(503, text="maintenance")

        async with make_client(handler) as client:
            with pytest.raises(CoinbaseHistoryError):
                await fetch_candles(client, start=T0, end=T0 + timedelta(minutes=1))

    async def test_non_array_body_raises(self):
        def handler(request):
            return httpx.Response(200, json={"not": "a list"})

        async with make_client(handler) as client:
            with pytest.raises(CoinbaseHistoryError):
                await fetch_candles(client, start=T0, end=T0 + timedelta(minutes=1))

    async def test_malformed_row_raises(self):
        def handler(request):
            return httpx.Response(200, json=[["not", "enough", "fields"]])

        async with make_client(handler) as client:
            with pytest.raises(CoinbaseHistoryError):
                await fetch_candles(client, start=T0, end=T0 + timedelta(minutes=1))


class TestFetchCandleHistory:
    async def test_rejects_end_before_start(self):
        async with make_client(lambda r: httpx.Response(200, json=[])) as client:
            with pytest.raises(CoinbaseHistoryError):
                await fetch_candle_history(client, start=T0, end=T0)

    async def test_pages_across_multiple_requests_for_a_wide_range(self):
        span_minutes = MAX_CANDLES_PER_REQUEST * 2 + 10
        recorder = Recorder([row(T0, "80000")], [row(T0 + timedelta(minutes=MAX_CANDLES_PER_REQUEST), "80500")], [row(T0 + timedelta(minutes=2 * MAX_CANDLES_PER_REQUEST), "81000")])
        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        async with make_client(recorder) as client:
            candles = await fetch_candle_history(
                client, start=T0, end=T0 + timedelta(minutes=span_minutes), sleep=fake_sleep,
            )

        assert len(recorder.requests) == 3
        assert len(sleeps) == 2  # a pause between pages, none after the last
        assert [c.close for c in candles] == [Decimal("80000"), Decimal("80500"), Decimal("81000")]

    async def test_a_single_page_range_makes_one_request_and_no_pause(self):
        recorder = Recorder([row(T0, "80000")])
        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        async with make_client(recorder) as client:
            await fetch_candle_history(client, start=T0, end=T0 + timedelta(minutes=5), sleep=fake_sleep)

        assert len(recorder.requests) == 1
        assert sleeps == []
