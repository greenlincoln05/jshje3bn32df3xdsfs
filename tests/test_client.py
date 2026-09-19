import base64
import itertools
import json
import random
import uuid
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from btcbot.config import KalshiEnv
from btcbot.kalshi_client import (
    KalshiAPIError,
    KalshiAuth,
    KalshiAuthError,
    KalshiClient,
    KalshiConnectionError,
    KalshiError,
    KalshiWriteNotAllowedError,
)
from btcbot.models import ParseError

TOO_MANY = {"error": "too many requests"}  # the exact 429 body documented by Kalshi


class Script:
    """A request handler that plays back outcomes in order (the last one repeats) and records requests."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        return outcome()  # outcomes are factories so each call gets a fresh Response, or raises


def ok(payload=None):
    return lambda: httpx.Response(200, json={} if payload is None else payload)


def status(code, payload=None, headers=None):
    return lambda: httpx.Response(code, json=payload if payload is not None else {}, headers=headers)


def raises(exc_type, message="boom"):
    def _raise():
        raise exc_type(message)

    return _raise


def make_client(handler, *, env=KalshiEnv.PROD, auth=None, **kwargs):
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = KalshiClient(
        env, auth=auth, transport=httpx.MockTransport(handler), sleep=fake_sleep, rng=random.Random(7), **kwargs
    )
    return client, sleeps


def verify_signature(rsa_key, request: httpx.Request, expected_message: str) -> None:
    rsa_key.public_key().verify(
        base64.b64decode(request.headers["KALSHI-ACCESS-SIGNATURE"]),
        expected_message.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
        hashes.SHA256(),
    )


class TestEnvironmentsAndAuthHeaders:
    async def test_defaults_to_the_demo_environment(self):
        client = KalshiClient()
        try:
            assert client.env is KalshiEnv.DEMO
        finally:
            await client.aclose()

    @pytest.mark.parametrize(
        ("env", "host"),
        [(KalshiEnv.DEMO, "external-api.demo.kalshi.co"), (KalshiEnv.PROD, "external-api.kalshi.com")],
    )
    async def test_environment_selects_the_documented_host(self, env, host, load_fixture):
        script = Script(ok(load_fixture("series_kxbtc15m.json")))
        client, _ = make_client(script, env=env)
        async with client:
            series = await client.get_series("KXBTC15M")

        assert script.requests[0].url.host == host
        assert script.requests[0].url.path == "/trade-api/v2/series/KXBTC15M"
        assert series.fee_type == "quadratic"

    async def test_public_requests_carry_no_credentials_even_when_auth_is_configured(self, rsa_key, load_fixture):
        script = Script(ok(load_fixture("series_kxbtc15m.json")))
        client, _ = make_client(script, auth=KalshiAuth("key-id", rsa_key))
        async with client:
            await client.get_series("KXBTC15M")

        assert not [name for name in script.requests[0].headers if name.lower().startswith("kalshi-access")]

    async def test_get_balance_is_signed_and_parsed(self, rsa_key):
        script = Script(ok({"balance": 12345, "balance_dollars": "123.4500", "portfolio_value": 6789, "updated_ts": 1}))
        auth = KalshiAuth("key-id-1", rsa_key, clock_ms=lambda: 1_703_123_456_789)
        client, _ = make_client(script, auth=auth)
        async with client:
            balance = await client.get_balance()

        request = script.requests[0]
        assert request.headers["KALSHI-ACCESS-KEY"] == "key-id-1"
        assert request.headers["KALSHI-ACCESS-TIMESTAMP"] == "1703123456789"
        verify_signature(rsa_key, request, "1703123456789GET/trade-api/v2/portfolio/balance")
        assert balance.available == Decimal("123.4500")

    async def test_signature_covers_the_path_but_not_the_query_string(self, rsa_key):
        script = Script(ok())
        client, _ = make_client(script, auth=KalshiAuth("k", rsa_key, clock_ms=lambda: 42))
        async with client:
            await client._request("GET", "/portfolio/positions", params={"limit": "5"}, authenticated=True)

        request = script.requests[0]
        assert request.url.query == b"limit=5"
        verify_signature(rsa_key, request, "42GET/trade-api/v2/portfolio/positions")

    async def test_signed_endpoint_without_credentials_fails_before_any_request(self):
        script = Script(ok())
        client, _ = make_client(script)
        async with client:
            with pytest.raises(KalshiError, match="credentials"):
                await client.get_balance()

        assert script.requests == []

    async def test_every_retry_is_freshly_signed(self, rsa_key):
        script = Script(status(429, TOO_MANY), ok({"balance": 1}))
        ticks = itertools.count(1000)
        client, _ = make_client(script, auth=KalshiAuth("k", rsa_key, clock_ms=lambda: next(ticks)))
        async with client:
            await client.get_balance()

        first, second = script.requests
        assert (first.headers["KALSHI-ACCESS-TIMESTAMP"], second.headers["KALSHI-ACCESS-TIMESTAMP"]) == ("1000", "1001")
        verify_signature(rsa_key, first, "1000GET/trade-api/v2/portfolio/balance")
        verify_signature(rsa_key, second, "1001GET/trade-api/v2/portfolio/balance")


class TestRetries:
    async def test_429_is_retried_with_exponential_jittered_backoff(self):
        script = Script(status(429, TOO_MANY), status(429, TOO_MANY), ok({"ok": True}))
        client, sleeps = make_client(script)
        async with client:
            result = await client._request("GET", "/ping")

        assert result == {"ok": True}
        assert len(script.requests) == 3
        assert len(sleeps) == 2
        assert 0.25 <= sleeps[0] <= 0.5  # attempt 0: ceiling 0.5s, jittered down to at most half
        assert 0.5 <= sleeps[1] <= 1.0  # attempt 1: ceiling 1.0s

    async def test_backoff_never_exceeds_the_cap(self):
        script = Script(*[status(503)] * 6, ok())
        client, sleeps = make_client(script, max_retries=6, backoff_cap=2.0)
        async with client:
            await client._request("GET", "/ping")

        assert len(sleeps) == 6
        assert max(sleeps) <= 2.0

    async def test_gives_up_after_max_retries(self):
        script = Script(status(429, TOO_MANY))  # every attempt is rate limited
        client, sleeps = make_client(script, max_retries=2)
        async with client:
            with pytest.raises(KalshiAPIError) as excinfo:
                await client._request("GET", "/ping")

        assert excinfo.value.status_code == 429
        assert "too many requests" in str(excinfo.value)
        assert len(script.requests) == 3  # first try + 2 retries
        assert len(sleeps) == 2

    async def test_retry_after_header_is_honoured(self):
        script = Script(status(429, TOO_MANY, headers={"Retry-After": "3"}), ok())
        client, sleeps = make_client(script)
        async with client:
            await client._request("GET", "/ping")

        assert sleeps[0] >= 3.0

    async def test_absurd_retry_after_is_capped(self):
        script = Script(status(429, TOO_MANY, headers={"Retry-After": "86400"}), ok())
        client, sleeps = make_client(script)
        async with client:
            await client._request("GET", "/ping")

        assert sleeps[0] <= 60.0

    @pytest.mark.parametrize("code", [500, 502, 503, 504])
    async def test_server_errors_are_retried_for_get(self, code):
        script = Script(status(code, {"error": "unavailable"}), ok({"ok": True}))
        client, sleeps = make_client(script)
        async with client:
            assert await client._request("GET", "/ping") == {"ok": True}

        assert len(script.requests) == 2 and len(sleeps) == 1

    async def test_server_errors_are_not_retried_for_post(self):
        # A 5xx may have been processed; replaying a POST could duplicate an order.
        script = Script(status(500, {"error": {"code": "internal", "message": "boom"}}), ok())
        client, sleeps = make_client(script)
        async with client:
            with pytest.raises(KalshiAPIError, match="boom"):
                await client._request("POST", "/anything")

        assert len(script.requests) == 1
        assert sleeps == []

    async def test_429_is_retried_even_for_post(self):
        # A 429 is rejected before processing, so replaying is safe.
        script = Script(status(429, TOO_MANY), ok({"ok": True}))
        client, _ = make_client(script)
        async with client:
            assert await client._request("POST", "/anything") == {"ok": True}

        assert len(script.requests) == 2

    async def test_transport_errors_are_retried_for_get(self):
        script = Script(raises(httpx.ConnectError), ok({"ok": True}))
        client, sleeps = make_client(script)
        async with client:
            assert await client._request("GET", "/ping") == {"ok": True}

        assert len(script.requests) == 2 and len(sleeps) == 1

    async def test_persistent_transport_errors_raise_connection_error(self):
        script = Script(raises(httpx.ReadTimeout, "slow"))
        client, _ = make_client(script, max_retries=2)
        async with client:
            with pytest.raises(KalshiConnectionError, match="ReadTimeout"):
                await client._request("GET", "/ping")

        assert len(script.requests) == 3

    async def test_transport_errors_are_not_retried_for_post(self):
        script = Script(raises(httpx.ReadTimeout, "slow"), ok())
        client, sleeps = make_client(script)
        async with client:
            with pytest.raises(KalshiConnectionError):
                await client._request("POST", "/anything")

        assert len(script.requests) == 1 and sleeps == []


class TestErrorMapping:
    @pytest.mark.parametrize("code", [401, 403])
    async def test_auth_failures_raise_auth_error_without_retrying(self, code):
        body = {"error": {"code": "authentication_error", "message": "invalid signature"}}
        script = Script(status(code, body))
        client, sleeps = make_client(script)
        async with client:
            with pytest.raises(KalshiAuthError, match="invalid signature") as excinfo:
                await client._request("GET", "/ping")

        assert isinstance(excinfo.value, KalshiAPIError)
        assert (excinfo.value.status_code, excinfo.value.code) == (code, "authentication_error")
        assert len(script.requests) == 1 and sleeps == []

    @pytest.mark.parametrize(
        ("body", "message", "code"),
        [
            ({"error": {"code": "not_found", "message": "market not found", "details": "x"}}, "market not found", "not_found"),
            ({"code": "bad_request", "message": "bad limit", "details": "d"}, "bad limit", "bad_request"),
            ({"error": "boom"}, "boom", None),
            ({"error": {"code": "x", "details": "only details"}}, "only details", "x"),
        ],
    )
    async def test_error_body_shapes_are_all_understood(self, body, message, code):
        script = Script(status(400, body))
        client, sleeps = make_client(script)
        async with client:
            with pytest.raises(KalshiAPIError) as excinfo:
                await client._request("GET", "/ping")

        assert (excinfo.value.status_code, excinfo.value.message, excinfo.value.code) == (400, message, code)
        assert sleeps == []  # ordinary 4xx errors are never retried

    async def test_non_json_error_body(self):
        script = Script(lambda: httpx.Response(404, text="<html>Not Found</html>"))
        client, _ = make_client(script)
        async with client:
            with pytest.raises(KalshiAPIError, match="Not Found"):
                await client._request("GET", "/ping")

    async def test_non_json_success_body(self):
        script = Script(lambda: httpx.Response(200, text="<html>maintenance</html>"))
        client, _ = make_client(script)
        async with client:
            with pytest.raises(KalshiError, match="not valid JSON"):
                await client._request("GET", "/ping")

    async def test_top_level_json_that_is_not_an_object_raises_kalshi_error(self):
        script = Script(ok([1, 2, 3]))
        client, _ = make_client(script)
        async with client:
            with pytest.raises(KalshiError, match="expected a JSON object"):
                await client.list_markets(series_ticker="KXBTC15M")

    async def test_unexpected_payload_shape_raises_parse_error(self):
        script = Script(ok({"unexpected": 1}))
        client, _ = make_client(script)
        async with client:
            with pytest.raises(ParseError, match="series"):
                await client.get_series("KXBTC15M")


class TestMarketDataEndpoints:
    async def test_json_numbers_decode_to_exact_decimals_never_floats(self):
        raw = b'{"strike": 81263.65, "noisy": 0.30000000000000004, "count": 5}'
        script = Script(lambda: httpx.Response(200, content=raw, headers={"content-type": "application/json"}))
        client, _ = make_client(script)
        async with client:
            data = await client._request("GET", "/ping")

        assert data["strike"] == Decimal("81263.65") and isinstance(data["strike"], Decimal)
        assert data["noisy"] == Decimal("0.30000000000000004")  # would be lost in a float round-trip
        assert data["count"] == 5 and isinstance(data["count"], int)

    async def test_get_market_parses_floor_strike_as_decimal(self, load_fixture):
        script = Script(ok(load_fixture("market_active.json")))
        client, _ = make_client(script)
        async with client:
            market = await client.get_market("KXBTC15M-26SEP182130-30")

        assert script.requests[0].url.path == "/trade-api/v2/markets/KXBTC15M-26SEP182130-30"
        assert str(market.strike) == "81263.65"

    async def test_get_orderbook_sends_depth_only_when_asked(self, load_fixture):
        script = Script(ok(load_fixture("orderbook_prod.json")))
        client, _ = make_client(script)
        async with client:
            book = await client.get_orderbook("KXBTC15M-26SEP182145-45")
            await client.get_orderbook("KXBTC15M-26SEP182145-45", depth=10)

        assert script.requests[0].url.path == "/trade-api/v2/markets/KXBTC15M-26SEP182145-45/orderbook"
        assert script.requests[0].url.query == b""
        assert script.requests[1].url.params["depth"] == "10"
        assert book.best_bid("yes").price == Decimal("0.5400")

    async def test_list_markets_builds_the_documented_query(self, load_fixture):
        script = Script(ok(load_fixture("markets_open.json")))
        client, _ = make_client(script)
        async with client:
            markets = await client.list_markets(series_ticker="KXBTC15M", status="open")

        assert script.requests[0].url.path == "/trade-api/v2/markets"
        assert dict(script.requests[0].url.params) == {"series_ticker": "KXBTC15M", "status": "open", "limit": "1000"}
        assert [m.ticker for m in markets] == ["KXBTC15M-26SEP182130-30"]
        assert markets[0].event_ticker == "KXBTC15M-26SEP182130"

    async def test_list_markets_sends_no_status_filter_unless_asked(self):
        script = Script(ok({"markets": []}))
        client, _ = make_client(script)
        async with client:
            assert await client.list_markets(series_ticker="S") == []

        assert dict(script.requests[0].url.params) == {"series_ticker": "S", "limit": "1000"}

    async def test_list_markets_rejects_a_repeated_cursor(self):
        script = Script(ok({"markets": [], "cursor": "next-page"}))
        client, _ = make_client(script)
        async with client:
            with pytest.raises(KalshiError, match="repeated.*cursor"):
                await client.list_markets(series_ticker="S")
        assert len(script.requests) == 2

    async def test_tickers_are_url_quoted(self, load_fixture):
        script = Script(ok(load_fixture("market_active.json")))
        client, _ = make_client(script)
        async with client:
            await client.get_market("A/B?x")

        assert script.requests[0].url.raw_path == b"/trade-api/v2/markets/A%2FB%3Fx"


# --------------------------------------------------------------------------- Phase 6: account endpoints
#
# Shapes follow docs.kalshi.com as read on 2026-09-19 (V2 orders: POST/DELETE /portfolio/events/orders; order,
# fill and position fields such as outcome_side, count_fp, fee_cost, position_fp). They are inlined here, not in
# tests/fixtures/, because they were never captured from a real response: `btcbot demo-check` run against the
# owner's own demo account is what confirms them.

TICKER = "KXBTC15M-26SEP182145-45"

ORDER = {
    "order_id": "ord-1", "client_order_id": "coid-1", "ticker": TICKER,
    "outcome_side": "yes", "book_side": "bid", "type": "limit", "status": "resting",
    "yes_price_dollars": "0.3000", "no_price_dollars": "0.7000",
    "fill_count_fp": "0.00", "remaining_count_fp": "5.00", "initial_count_fp": "5.00",
    "taker_fees_dollars": "0.0000", "maker_fees_dollars": "0.0000",
    "created_time": "2026-09-19T00:00:00Z",
}
ORDER_PAYLOAD = {"order": ORDER}
CREATE_ACK = {
    "order_id": "ord-1", "client_order_id": "coid-1", "fill_count": "0.00", "remaining_count": "5.00",
    "average_fill_price": "0.0000", "average_fee_paid": "0.0000", "ts_ms": 1715793600123,
}
CANCEL_ACK = {"order_id": "ord-1", "client_order_id": "coid-1", "reduced_by": "5.00", "ts_ms": 1715793660456}
FILL = {
    "fill_id": "f-1", "trade_id": "t-1", "order_id": "ord-1", "ticker": TICKER, "market_ticker": TICKER,
    "outcome_side": "no", "book_side": "ask", "count_fp": "3.00", "yes_price_dollars": "0.3200",
    "no_price_dollars": "0.6800", "is_taker": False, "created_time": "2026-09-19T00:00:05Z", "fee_cost": "0.0123",
}


class TestAccountReadOnlyEndpoints:
    """get_order/list_orders/list_fills/get_positions are signed but read-only, so -- like get_balance --
    they are allowed against either environment; only create_order/cancel_order are demo-gated."""

    async def test_get_order_parses_price_and_counts(self, rsa_key):
        script = Script(ok(ORDER_PAYLOAD))
        client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            order = await client.get_order("ord-1")

        assert script.requests[0].url.path == "/trade-api/v2/portfolio/orders/ord-1"
        assert order.side == "yes" and order.book_side == "bid" and order.price == Decimal("0.30")
        assert order.initial_count == Decimal("5") and order.remaining_count == Decimal("5")
        assert order.fill_count == Decimal("0") and order.fees_usd == Decimal("0")
        assert order.is_done is False

    async def test_a_no_order_reports_its_own_sides_price(self, rsa_key):
        payload = {"order": {**ORDER, "outcome_side": "no", "book_side": "ask"}}
        script = Script(ok(payload))
        client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            order = await client.get_order("ord-1")
        assert order.side == "no" and order.price == Decimal("0.70")

    async def test_order_fees_add_maker_and_taker(self, rsa_key):
        payload = {"order": {**ORDER, "maker_fees_dollars": "0.0100", "taker_fees_dollars": "0.0250"}}
        script = Script(ok(payload))
        client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            assert (await client.get_order("ord-1")).fees_usd == Decimal("0.0350")

    async def test_order_is_done_for_terminal_statuses(self, rsa_key):
        for status_value in ("canceled", "executed"):
            script = Script(ok({"order": {**ORDER, "status": status_value}}))
            client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
            async with client:
                order = await client.get_order("ord-1")
            assert order.is_done is True

    @pytest.mark.parametrize("missing", ["outcome_side", "initial_count_fp", "remaining_count_fp", "status", "order_id"])
    async def test_an_order_missing_a_required_field_is_a_parse_error_not_a_silent_zero(self, rsa_key, missing):
        broken = {k: v for k, v in ORDER.items() if k != missing}
        script = Script(ok({"order": broken}))
        client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            with pytest.raises(ParseError):
                await client.get_order("ord-1")

    async def test_the_old_guessed_field_names_are_not_accepted(self, rsa_key):
        legacy = {"order_id": "o", "ticker": "T", "side": "yes", "status": "resting", "initial_count": "5", "remaining_count": "5"}
        script = Script(ok({"order": legacy}))
        client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            with pytest.raises(ParseError):
                await client.get_order("o")

    async def test_list_orders_builds_the_query_and_paginates(self, rsa_key):
        script = Script(
            ok({"orders": [ORDER], "cursor": "next"}),
            ok({"orders": [{**ORDER, "order_id": "ord-2"}], "cursor": ""}),
        )
        client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            orders = await client.list_orders(ticker=TICKER, status="resting")

        assert dict(script.requests[0].url.params) == {"limit": "1000", "ticker": TICKER, "status": "resting"}
        assert [o.order_id for o in orders] == ["ord-1", "ord-2"]

    async def test_list_orders_rejects_a_repeated_cursor(self, rsa_key):
        script = Script(ok({"orders": [], "cursor": "loop"}))
        client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            with pytest.raises(KalshiError, match="repeated.*cursor"):
                await client.list_orders()

    async def test_list_fills_parses_side_price_count_and_fee(self, rsa_key):
        script = Script(ok({"fills": [FILL], "cursor": ""}))
        client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            fills = await client.list_fills(order_id="ord-1", min_ts=1789000000)

        assert script.requests[0].url.params["order_id"] == "ord-1"
        assert script.requests[0].url.params["min_ts"] == "1789000000"
        fill = fills[0]
        assert fill.side == "no" and fill.price == Decimal("0.68") and fill.count == Decimal("3")
        assert fill.fee_usd == Decimal("0.0123") and fill.is_taker is False
        assert fill.fill_id == "f-1" and fill.dedupe_key == "f-1" and fill.ticker == TICKER

    @pytest.mark.parametrize("missing", ["outcome_side", "count_fp", "fee_cost", "is_taker", "order_id", "created_time"])
    async def test_a_fill_missing_a_required_field_is_a_parse_error(self, rsa_key, missing):
        broken = {k: v for k, v in FILL.items() if k != missing}
        script = Script(ok({"fills": [broken], "cursor": ""}))
        client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            with pytest.raises(ParseError):
                await client.list_fills()

    async def test_a_misnamed_fee_field_can_never_look_like_a_free_fill(self, rsa_key):
        """The earlier shape read ``fee_dollars`` and defaulted to 0 -- so had that name been wrong, the
        fidelity report would have confidently claimed makers pay nothing."""
        renamed = {k: v for k, v in FILL.items() if k != "fee_cost"} | {"fee_dollars": "0.0123"}
        script = Script(ok({"fills": [renamed], "cursor": ""}))
        client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            with pytest.raises(ParseError, match="fee_cost"):
                await client.list_fills()

    async def test_get_positions_derives_side_from_the_signed_position_field(self, rsa_key):
        payload = {
            "market_positions": [
                {"ticker": "A", "position_fp": "5.00", "market_exposure_dollars": "1.50",
                 "realized_pnl_dollars": "0.25", "fees_paid_dollars": "0.02"},
                {"ticker": "B", "position_fp": "-3.00"},
            ]
        }
        script = Script(ok(payload))
        client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            positions = await client.get_positions()

        assert positions[0].side == "yes" and positions[0].count == Decimal("5")
        assert positions[0].market_exposure_usd == Decimal("1.50") and positions[0].realized_pnl_usd == Decimal("0.25")
        assert positions[1].side == "no" and positions[1].count == Decimal("3")  # sign strips to a positive count


class TestWriteEndpointsAreDemoOnly:
    async def test_create_order_refuses_against_prod_without_sending_anything(self, rsa_key):
        script = Script(ok(CREATE_ACK))
        client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            with pytest.raises(KalshiWriteNotAllowedError):
                await client.create_order(TICKER, "yes", count=Decimal("5"), price=Decimal("0.30"))
        assert script.requests == []  # refused before any request was built, let alone signed

    async def test_cancel_order_refuses_against_prod_without_sending_anything(self, rsa_key):
        script = Script(ok(CANCEL_ACK))
        client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            with pytest.raises(KalshiWriteNotAllowedError):
                await client.cancel_order("ord-1", market_ticker=TICKER)
        assert script.requests == []

    async def test_create_order_against_demo_is_allowed(self, rsa_key):
        script = Script(ok(CREATE_ACK))
        client, _ = make_client(script, env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            ack = await client.create_order("T", "yes", count=Decimal("1"), price=Decimal("0.5"))
        assert ack.order_id == "ord-1"


class TestCreateAndCancelOrderV2:
    async def create(self, rsa_key, side, **kwargs):
        script = Script(ok(CREATE_ACK))
        client, _ = make_client(script, env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            ack = await client.create_order(TICKER, side, **kwargs)
        return script.requests[0], json.loads(script.requests[0].content), ack

    async def test_buying_yes_is_a_bid_at_the_yes_price(self, rsa_key):
        request, body, ack = await self.create(rsa_key, "yes", count=Decimal("5"), price=Decimal("0.30"))

        assert request.method == "POST" and request.url.path == "/trade-api/v2/portfolio/events/orders"
        assert body["ticker"] == TICKER and body["side"] == "bid"
        assert body["count"] == "5.00" and body["price"] == "0.3000"
        assert body["time_in_force"] == "good_till_canceled" and body["self_trade_prevention_type"] == "taker_at_cross"
        assert body["post_only"] is True
        uuid.UUID(body["client_order_id"])  # a real UUID was generated, not left blank
        assert ack.order_id == "ord-1" and ack.remaining_count == Decimal("5")

    async def test_buying_no_is_an_ask_at_one_minus_the_no_price(self, rsa_key):
        _, body, _ = await self.create(rsa_key, "no", count=Decimal("5"), price=Decimal("0.68"))
        assert body["side"] == "ask" and body["price"] == "0.3200"  # NO at 0.68 == selling YES at 0.32

    async def test_a_marketable_order_is_immediate_or_cancel_through_the_book(self, rsa_key):
        _, yes_body, _ = await self.create(rsa_key, "yes", count=Decimal("2"))
        assert yes_body["side"] == "bid" and yes_body["price"] == "0.9900"
        assert yes_body["time_in_force"] == "immediate_or_cancel" and "post_only" not in yes_body
        _, no_body, _ = await self.create(rsa_key, "no", count=Decimal("2"))
        assert no_body["side"] == "ask" and no_body["price"] == "0.0100"  # buying NO up to 0.99 == YES ask at 0.01

    async def test_post_only_can_be_switched_off(self, rsa_key):
        _, body, _ = await self.create(rsa_key, "yes", count=Decimal("1"), price=Decimal("0.5"), post_only=False)
        assert "post_only" not in body

    async def test_an_explicit_client_order_id_is_honoured(self, rsa_key):
        _, body, _ = await self.create(rsa_key, "no", count=Decimal("2"), price=Decimal("0.6"), client_order_id="my-id")
        assert body["client_order_id"] == "my-id"

    async def test_an_off_grid_price_is_sent_unrounded_so_the_exchange_can_reject_it(self, rsa_key):
        _, body, _ = await self.create(rsa_key, "yes", count=Decimal("1"), price=Decimal("0.123456789"))
        assert body["price"] == "0.123456789"

    @pytest.mark.parametrize("count,price", [(Decimal(0), Decimal("0.5")), (Decimal(-1), Decimal("0.5")),
                                              (Decimal(1), Decimal(0)), (Decimal(1), Decimal(1)), (Decimal(1), Decimal("1.5"))])
    async def test_nonsense_orders_never_reach_the_network(self, rsa_key, count, price):
        script = Script(ok(CREATE_ACK))
        client, _ = make_client(script, env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            with pytest.raises(ValueError):
                await client.create_order(TICKER, "yes", count=count, price=price)
        assert script.requests == []

    async def test_a_500_on_create_is_not_retried(self, rsa_key):
        script = Script(status(500), ok(CREATE_ACK))
        client, _ = make_client(script, env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            with pytest.raises(KalshiAPIError):
                await client.create_order(TICKER, "yes", count=Decimal("1"), price=Decimal("0.5"))
        assert len(script.requests) == 1  # a retried POST could double-place

    async def test_the_legacy_order_endpoint_is_never_used(self, rsa_key):
        request, _, _ = await self.create(rsa_key, "yes", count=Decimal("1"), price=Decimal("0.5"))
        assert request.url.path != "/trade-api/v2/portfolio/orders"

    async def test_cancel_is_a_delete_on_the_v2_path_with_the_market_ticker(self, rsa_key):
        script = Script(ok(CANCEL_ACK))
        client, _ = make_client(script, env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            ack = await client.cancel_order("ord-1", market_ticker=TICKER)

        assert script.requests[0].method == "DELETE"
        assert script.requests[0].url.path == "/trade-api/v2/portfolio/events/orders/ord-1"
        assert script.requests[0].url.params["market_ticker"] == TICKER
        assert ack.order_id == "ord-1" and ack.reduced_by == Decimal("5")

    async def test_cancel_without_a_ticker_sends_no_query(self, rsa_key):
        script = Script(ok(CANCEL_ACK))
        client, _ = make_client(script, env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            await client.cancel_order("ord-1")
        assert "market_ticker" not in dict(script.requests[0].url.params)

    async def test_the_old_wrapped_response_shape_is_a_parse_error(self, rsa_key):
        script = Script(ok({"order": ORDER}))  # what the pre-V2 guess expected; V2 returns a flat ack
        client, _ = make_client(script, env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            with pytest.raises(ParseError):
                await client.create_order(TICKER, "yes", count=Decimal("1"), price=Decimal("0.5"))


class TestShardBalancesAndAllocation:
    async def test_balance_breakdown_is_parsed_per_exchange_shard(self, rsa_key):
        payload = {"balance": 10000, "balance_dollars": "100.00", "portfolio_value": 0,
                   "balance_breakdown": [{"exchange_index": 0, "balance": "100.00"}, {"exchange_index": 2, "balance": "0.00"}]}
        script = Script(ok(payload))
        client, _ = make_client(script, env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            balance = await client.get_balance()
        assert balance.available == Decimal("100.00") and balance.by_exchange == {0: Decimal("100.00"), 2: Decimal("0.00")}

    async def test_a_balance_without_a_breakdown_is_still_fine(self, rsa_key):
        script = Script(ok({"balance": 5000, "portfolio_value": 0}))
        client, _ = make_client(script, env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            assert (await client.get_balance()).by_exchange == {}

    async def test_market_exchange_index_is_read_from_the_market_payload(self, load_fixture):
        from btcbot.models import Market

        payload = {**load_fixture("market_active.json")["market"], "exchange_index": 2}
        assert Market.from_api(payload).exchange_index == 2
        assert Market.from_api({**payload, "exchange_index": None}).exchange_index is None
        assert Market.from_api({k: v for k, v in payload.items() if k != "exchange_index"}).exchange_index is None

    async def test_set_target_balance_allocation_posts_the_documented_body(self, rsa_key):
        script = Script(ok({}))
        client, _ = make_client(script, env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            await client.set_target_balance_allocation({2: 100})
            await client.set_target_balance_allocation({2: 60, 0: 40})
        assert script.requests[0].url.path == "/trade-api/v2/portfolio/target_balance_allocation"
        assert json.loads(script.requests[0].content) == {"allocations": [{"exchange_index": 2, "percent": 100}]}
        assert json.loads(script.requests[1].content) == {
            "allocations": [{"exchange_index": 0, "percent": 40}, {"exchange_index": 2, "percent": 60}]
        }

    async def test_allocation_is_demo_only_and_must_total_100(self, rsa_key):
        prod_script = Script(ok({}))
        prod, _ = make_client(prod_script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
        async with prod:
            with pytest.raises(KalshiWriteNotAllowedError):
                await prod.set_target_balance_allocation({2: 100})
        assert prod_script.requests == []

        demo_script = Script(ok({}))
        demo, _ = make_client(demo_script, env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key))
        async with demo:
            for bad in ({2: 50}, {2: 120, 0: -20}):
                with pytest.raises(ValueError):
                    await demo.set_target_balance_allocation(bad)
        assert demo_script.requests == []


class TestOrderAuditLog:
    """The bot's own ledger of every write it asked Kalshi to do, to check the account's order history against."""

    def logged_client(self, script, rsa_key, env=KalshiEnv.DEMO):
        records: list[dict] = []
        client, _ = make_client(script, env=env, auth=KalshiAuth("test-key", rsa_key), write_log=records.append)
        return client, records

    async def test_a_create_records_the_request_then_the_ack(self, rsa_key):
        client, records = self.logged_client(Script(ok(CREATE_ACK)), rsa_key)
        async with client:
            await client.create_order(TICKER, "no", count=Decimal("5"), price=Decimal("0.68"), client_order_id="cid-9")
        assert [r["event"] for r in records] == ["create_order", "create_order_ack"]
        request, ack = records
        assert request["env"] == "demo" and request["ticker"] == TICKER and request["side"] == "no"
        assert request["book_side"] == "ask" and request["price"] == "0.3200" and request["count"] == "5.00"
        assert request["client_order_id"] == "cid-9" and ack["order_id"] == "ord-1" and "ts" in request

    async def test_a_rejected_order_is_logged_as_failed_so_it_is_never_a_mystery(self, rsa_key):
        client, records = self.logged_client(Script(status(400, {"code": "bad", "message": "no"})), rsa_key)
        async with client:
            with pytest.raises(KalshiAPIError):
                await client.create_order(TICKER, "yes", count=Decimal("1"), price=Decimal("0.5"))
        assert [r["event"] for r in records] == ["create_order", "create_order_failed"]

    async def test_an_accepted_order_with_an_unreadable_ack_is_logged_because_it_may_exist(self, rsa_key):
        client, records = self.logged_client(Script(ok({"unexpected": True})), rsa_key)
        async with client:
            with pytest.raises(ParseError):
                await client.create_order(TICKER, "yes", count=Decimal("1"), price=Decimal("0.5"))
        assert records[-1]["event"] == "create_order_unreadable_ack"

    async def test_cancel_cancel_all_and_allocation_are_logged_too(self, rsa_key):
        client, records = self.logged_client(Script(ok(CANCEL_ACK)), rsa_key)
        async with client:
            await client.cancel_order("ord-1", market_ticker=TICKER)
            await client.cancel_all_resting_orders()
            await client.set_target_balance_allocation({2: 100})
        assert [r["event"] for r in records] == [
            "cancel_order", "cancel_order_ack", "cancel_all_resting_orders", "cancel_all_resting_orders_ack",
            "set_target_balance_allocation",
        ]

    async def test_cancel_all_is_a_delete_on_the_v2_path_and_demo_only(self, rsa_key):
        script = Script(ok({}))
        client, _ = make_client(script, env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            await client.cancel_all_resting_orders()
        assert script.requests[0].method == "DELETE" and script.requests[0].url.path == "/trade-api/v2/portfolio/events/orders"

        prod_script = Script(ok({}))
        prod, records = self.logged_client(prod_script, rsa_key, env=KalshiEnv.PROD)
        async with prod:
            with pytest.raises(KalshiWriteNotAllowedError):
                await prod.cancel_all_resting_orders()
        assert prod_script.requests == [] and records == []  # refused before anything was sent or logged

    async def test_a_broken_log_never_stops_an_order(self, rsa_key):
        def broken(_record):
            raise OSError("disk full")

        client, _ = make_client(Script(ok(CREATE_ACK)), env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key), write_log=broken)
        async with client:
            ack = await client.create_order(TICKER, "yes", count=Decimal("1"), price=Decimal("0.5"))
        assert ack.order_id == "ord-1"


class TestEmptyBodyOnlyWhereKalshiSendsNone:
    async def test_cancel_all_accepts_an_empty_success_body(self, rsa_key):
        script = Script(lambda: httpx.Response(200, content=b""))
        client, _ = make_client(script, env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            assert await client.cancel_all_resting_orders() == {}

    async def test_cancel_all_still_rejects_a_garbage_body(self, rsa_key):
        script = Script(lambda: httpx.Response(200, content=b"<html>oops</html>"))
        client, _ = make_client(script, env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            with pytest.raises(KalshiError, match="not valid JSON"):
                await client.cancel_all_resting_orders()

    async def test_other_endpoints_still_reject_an_empty_body(self, rsa_key):
        script = Script(lambda: httpx.Response(200, content=b""))
        client, _ = make_client(script, env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            with pytest.raises(KalshiError, match="not valid JSON"):
                await client.get_balance()
