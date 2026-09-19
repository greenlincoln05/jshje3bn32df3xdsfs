"""Offline tests for ``btcbot demo-probe``: a scripted fake client, no network, no key."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_demo_check import make_market

from btcbot.config import KalshiEnv
from btcbot.demo_probe import low_levels, redact, run_probe
from btcbot.kalshi_client import KalshiAPIError, KalshiConnectionError, KalshiWriteNotAllowedError
from btcbot.models import OrderBook, PriceLevel

NOW = datetime.now(timezone.utc)


class FakeProbeClient:
    env = KalshiEnv.DEMO

    def __init__(self):
        self.gets: list[tuple[str, dict | None]] = []
        self.created: list[tuple[str, str, Decimal]] = []
        self.cancelled: list[tuple[str, str | None]] = []
        self.get_raises: Exception | None = None
        self.cancel_fails = 0
        self.responses: dict[str, tuple[int, str]] = {}
        market = make_market(raw={"exchange_index": 2})
        self.market = SimpleNamespace(**{**market.__dict__})  # unused; find_current_market needs list_markets below
        self._market = make_market(
            open_time=NOW - timedelta(seconds=60), close_time=NOW + timedelta(seconds=600), raw={"exchange_index": 2}
        )

    async def list_markets(self, *, series_ticker, status=None):
        return [self._market]

    async def get_orderbook(self, ticker, *, depth=0):
        return OrderBook(ticker, (PriceLevel(Decimal("0.01"), Decimal(2)), PriceLevel(Decimal("0.6"), Decimal(9))),
                         (PriceLevel(Decimal("0.02"), Decimal(5)),))

    async def create_order(self, ticker, side, *, count, price=None):
        self.created.append((ticker, side, price))
        return SimpleNamespace(order_id=f"ord-{len(self.created)}", remaining_count=count)

    async def cancel_order(self, order_id, *, market_ticker=None):
        if self.cancel_fails > 0:
            self.cancel_fails -= 1
            raise KalshiConnectionError("network down")
        self.cancelled.append((order_id, market_ticker))
        return SimpleNamespace(reduced_by=Decimal(1))

    async def probe_get(self, endpoint, params=None, *, authenticated=True):
        self.gets.append((endpoint, params))
        if self.get_raises is not None:
            raise self.get_raises
        return self.responses.get(endpoint, (404, '{"code":"not_found","message":"not found"}'))


async def no_sleep(_):
    return None


def run(client, lines):
    return run_probe(client, "KXBTC15M", say=lines.append, sleep=no_sleep)


class TestSafety:
    async def test_refuses_anything_but_the_demo_environment(self):
        client = FakeProbeClient()
        client.env = KalshiEnv.PROD
        with pytest.raises(KalshiWriteNotAllowedError):
            await run(client, [])
        assert client.created == [] and client.gets == []

    async def test_every_order_it_places_is_tiny_and_gets_cancelled_with_the_market_ticker(self):
        client, lines = FakeProbeClient(), []
        assert await run(client, lines) == 0
        assert [(s, p) for _, s, p in client.created] == [("yes", Decimal("0.01")), ("no", Decimal("0.01"))]
        assert [o for o, _ in client.cancelled] == ["ord-1", "ord-2"]
        assert all(t == client._market.ticker for _, t in client.cancelled)

    async def test_a_transport_failure_while_reading_still_cancels_what_it_placed(self):
        client, lines = FakeProbeClient(), []
        client.get_raises = KalshiConnectionError("boom")
        assert await run(client, lines) == 0  # reads failing is data, not a reason to skip the cleanup
        assert len(client.cancelled) == 2

    async def test_a_failed_cancel_is_retried_in_cleanup_and_reported_if_it_still_fails(self):
        client, lines = FakeProbeClient(), []
        client.cancel_fails = 99
        await run(client, lines)
        assert any("CLEANUP FAILED" in line for line in lines)

    async def test_a_rejected_order_is_reported_and_the_other_side_still_runs(self):
        client, lines = FakeProbeClient(), []
        original = client.create_order
        calls = []

        async def flaky(ticker, side, *, count, price=None):
            calls.append(side)
            if side == "yes":
                raise KalshiAPIError(400, "nope", code="bad")
            return await original(ticker, side, count=count, price=price)

        client.create_order = flaky
        await run(client, lines)
        assert calls == ["yes", "no"] and any("create_order ->" in line for line in lines)


class TestOutput:
    async def test_it_prints_the_raw_status_and_body_of_every_read_path_it_tries(self):
        client, lines = FakeProbeClient(), []
        client.responses["/portfolio/orders/ord-1"] = (404, '{"code":"not_found"}')
        await run(client, lines)
        text = "\n".join(lines)
        assert "GET /portfolio/orders/{id}" in text and "HTTP 404" in text
        assert "GET /portfolio/orders?exchange_index=2&status=resting" in text
        assert "GET /portfolio/fills?ticker=T" in text and "queue_position" in text
        assert "after cancel" in text and "order book YES levels" in text and "order book NO levels" in text

    def test_account_ids_are_masked_and_long_bodies_truncated(self):
        masked = redact('{"order":{"order_id":"o-1","user_id":"secret-user-123","status":"resting"}}')
        assert "secret-user-123" not in masked and "o-1" in masked and "***" in masked
        assert "more chars" in redact(json.dumps({"x": "y" * 5000}))

    def test_low_levels_only_shows_the_bottom_of_the_book(self):
        book = OrderBook("T", (PriceLevel(Decimal("0.01"), Decimal(2)), PriceLevel(Decimal("0.6"), Decimal(9))), ())
        assert low_levels(book, "yes") == [("0.01", "2")] and low_levels(book, "no") == []

    async def test_no_open_market_says_so(self):
        client, lines = FakeProbeClient(), []
        client._market = make_market(open_time=NOW - timedelta(hours=2), close_time=NOW - timedelta(hours=1))
        assert await run(client, lines) == 1 and "No open" in lines[0]
        assert client.created == []
