import asyncio
import random
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
import websockets.exceptions as ws_exc

from btcbot.spot_feed import (
    CoinbaseSpotFeed,
    SpotBuffer,
    SpotFeedError,
    SpotTick,
    parse_rest_ticker,
    parse_ticker_message,
)

T0 = datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc)


def tick(price="100", mono=0.0, source="coinbase-ws"):
    return SpotTick(price=Decimal(price), source=source, source_ts=None, receive_ts=T0, monotonic_ts=mono)


class FakeConnection:
    """Plays back a scripted sequence of recv() outcomes; strings are returned, exceptions are raised."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message):
        self.sent.append(message)

    async def recv(self):
        if not self.outcomes:
            raise ws_exc.ConnectionClosedOK(None, None)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def close(self):
        self.closed = True


def make_feed(connections, **kwargs):
    conns = list(connections)
    sleeps = []

    async def fake_connect(url):
        if not conns:
            raise AssertionError("ran out of scripted connections")
        return conns.pop(0)

    async def fake_sleep(delay):
        sleeps.append(delay)
        await asyncio.sleep(0)  # a real suspension point so the test loop can observe progress between attempts

    clock = iter([T0] * 1000)
    mono = iter(range(1000))
    feed = CoinbaseSpotFeed(
        SpotBuffer(window_sec=5.0, stale_after_sec=3.0),
        connect=fake_connect,
        clock=lambda: next(clock),
        monotonic=lambda: float(next(mono)),
        sleep=fake_sleep,
        rng=random.Random(1),
        **kwargs,
    )
    return feed, sleeps


class TestParseTickerMessage:
    def test_valid_ticker(self):
        raw = '{"type": "ticker", "price": "63000.12", "time": "2026-09-19T00:00:01.500000Z"}'
        result = parse_ticker_message(raw, receive_ts=T0, monotonic_ts=1.0)
        assert result.price == Decimal("63000.12")
        assert result.source == "coinbase-ws"
        assert result.source_ts == datetime(2026, 9, 19, 0, 0, 1, 500000, tzinfo=timezone.utc)
        assert result.receive_ts == T0 and result.monotonic_ts == 1.0

    def test_non_ticker_message_returns_none(self):
        assert parse_ticker_message('{"type": "subscriptions", "channels": []}', receive_ts=T0, monotonic_ts=0) is None

    def test_missing_time_field_keeps_the_price(self):
        result = parse_ticker_message('{"type": "ticker", "price": "1"}', receive_ts=T0, monotonic_ts=0)
        assert result.price == Decimal("1") and result.source_ts is None

    def test_bad_time_field_keeps_the_price(self):
        result = parse_ticker_message('{"type": "ticker", "price": "1", "time": "not-a-time"}', receive_ts=T0, monotonic_ts=0)
        assert result.price == Decimal("1") and result.source_ts is None

    def test_error_message_raises(self):
        with pytest.raises(SpotFeedError, match="Coinbase error"):
            parse_ticker_message('{"type": "error", "message": "bad subscription"}', receive_ts=T0, monotonic_ts=0)

    def test_malformed_json_raises(self):
        with pytest.raises(SpotFeedError, match="malformed"):
            parse_ticker_message("not json", receive_ts=T0, monotonic_ts=0)

    def test_top_level_array_raises(self):
        with pytest.raises(SpotFeedError, match="expected a JSON object"):
            parse_ticker_message("[1, 2]", receive_ts=T0, monotonic_ts=0)

    @pytest.mark.parametrize("price", ["nan", "-1", "0", "abc", None])
    def test_bad_price_raises(self, price):
        import json

        with pytest.raises(SpotFeedError):
            parse_ticker_message(json.dumps({"type": "ticker", "price": price}), receive_ts=T0, monotonic_ts=0)


class TestParseRestTicker:
    def test_valid_payload(self):
        result = parse_rest_ticker({"price": "63000.5", "time": "2026-09-19T00:00:00Z"}, receive_ts=T0, monotonic_ts=2.0)
        assert result.price == Decimal("63000.5") and result.source == "coinbase-rest"

    def test_non_object_payload_raises(self):
        with pytest.raises(SpotFeedError, match="expected a JSON object"):
            parse_rest_ticker([1, 2, 3], receive_ts=T0, monotonic_ts=0)


class TestSpotBuffer:
    def test_empty_buffer_is_stale_with_no_price(self):
        buf = SpotBuffer()
        assert buf.price() is None
        assert buf.is_stale(now_monotonic=100.0) is True

    def test_latest_price_and_freshness(self):
        buf = SpotBuffer(stale_after_sec=3.0)
        buf.add(tick("100", mono=10.0))
        assert buf.price() == Decimal("100")
        assert buf.is_stale(now_monotonic=11.0) is False
        assert buf.is_stale(now_monotonic=13.01) is True

    def test_window_trims_old_ticks_but_keeps_at_least_one(self):
        buf = SpotBuffer(window_sec=2.0, stale_after_sec=10.0)
        buf.add(tick("1", mono=0.0))
        buf.add(tick("2", mono=1.0))
        buf.add(tick("3", mono=5.0))  # drops the first two: both older than 5.0 - 2.0
        assert [t.price for t in buf.window_ticks()] == [Decimal("3")]

    def test_single_stale_tick_is_never_evicted(self):
        buf = SpotBuffer(window_sec=1.0)
        buf.add(tick("1", mono=0.0))
        buf.add(tick("2", mono=100.0))
        # only the most recent tick is guaranteed to survive a long gap
        assert buf.price() == Decimal("2")

    def test_rejects_non_positive_window(self):
        with pytest.raises(ValueError):
            SpotBuffer(window_sec=0)


class TestCoinbaseSpotFeedReconnect:
    async def test_streams_ticks_and_skips_malformed_messages(self):
        conn = FakeConnection(
            [
                '{"type": "subscriptions", "channels": []}',
                '{"type": "ticker", "price": "100"}',
                "not json",
                '{"type": "ticker", "price": "101"}',
            ]
        )
        feed, sleeps = make_feed([conn, FakeConnection([])])
        task = asyncio.ensure_future(feed.run_forever())
        for _ in range(200):
            if feed.buffer.price() == Decimal("101"):
                break
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert feed.buffer.price() == Decimal("101")

    async def test_reconnects_after_disconnect_and_resets_backoff(self):
        first = FakeConnection(['{"type": "ticker", "price": "100"}', ws_exc.ConnectionClosedError(None, None)])
        second = FakeConnection(['{"type": "ticker", "price": "200"}'])
        feed, sleeps = make_feed([first, second])
        task = asyncio.ensure_future(feed.run_forever())
        for _ in range(200):
            if feed.buffer.price() == Decimal("200"):
                break
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert feed.buffer.price() == Decimal("200")
        assert first.closed is True
        assert sleeps  # backoff was used between the two connections
        assert first.sent == second.sent  # both connections were subscribed the same way

    async def test_connect_failure_backs_off_and_retries(self):
        calls = {"n": 0}

        async def flaky_connect(url):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("connection refused")
            return FakeConnection(['{"type": "ticker", "price": "50"}'])

        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)
            await asyncio.sleep(0)

        clock = iter([T0] * 1000)
        mono = iter(range(1000))
        feed = CoinbaseSpotFeed(
            SpotBuffer(),
            connect=flaky_connect,
            clock=lambda: next(clock),
            monotonic=lambda: float(next(mono)),
            sleep=fake_sleep,
            rng=random.Random(1),
        )
        task = asyncio.ensure_future(feed.run_forever())
        for _ in range(200):
            if feed.buffer.price() == Decimal("50"):
                break
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert feed.buffer.price() == Decimal("50")
        assert sleeps and calls["n"] == 2

    async def test_on_tick_callback_fires(self):
        seen = []
        conn = FakeConnection(['{"type": "ticker", "price": "1"}'])
        feed, _ = make_feed([conn, FakeConnection([])], on_tick=seen.append)
        task = asyncio.ensure_future(feed.run_forever())
        for _ in range(200):
            if seen:
                break
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(seen) == 1 and seen[0].price == Decimal("1")


class TestRestFallback:
    async def test_poll_rest_once_parses_and_records(self):
        def handler(request):
            return httpx.Response(200, json={"price": "64000.00", "time": "2026-09-19T00:00:00Z"})

        clock = iter([T0])
        mono = iter([5.0])
        feed = CoinbaseSpotFeed(
            SpotBuffer(),
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            clock=lambda: next(clock),
            monotonic=lambda: next(mono),
        )
        try:
            result = await feed.poll_rest_once()
        finally:
            await feed.aclose()
        assert result.price == Decimal("64000.00")
        assert feed.buffer.price() == Decimal("64000.00")

    async def test_poll_rest_once_raises_on_http_error(self):
        def handler(request):
            return httpx.Response(503)

        feed = CoinbaseSpotFeed(SpotBuffer(), http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await feed.poll_rest_once()
        finally:
            await feed.aclose()
