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
# These payloads are this project's own best-effort guess at Kalshi's order/fill/position shape (see
# kalshi_client.py's module docstring): unlike tests/fixtures/*.json, they were never captured from a real
# Kalshi response, so they are inlined here rather than added to that directory of verified payloads.

ORDER_PAYLOAD = {
    "order": {
        "order_id": "ord-1", "client_order_id": "coid-1", "ticker": "KXBTC15M-26SEP182145-45",
        "side": "yes", "action": "buy", "type": "limit", "status": "resting",
        "yes_price_dollars": "0.30", "initial_count": "5", "remaining_count": "5",
        "created_time": "2026-09-19T00:00:00Z",
    }
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
        assert order.side == "yes" and order.price == Decimal("0.30")
        assert order.initial_count == Decimal("5") and order.remaining_count == Decimal("5")
        assert order.is_done is False

    async def test_order_is_done_for_terminal_statuses(self, rsa_key):
        for status_value in ("canceled", "executed"):
            payload = {"order": {**ORDER_PAYLOAD["order"], "status": status_value}}
            script = Script(ok(payload))
            client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
            async with client:
                order = await client.get_order("ord-1")
            assert order.is_done is True

    async def test_list_orders_builds_the_query_and_paginates(self, rsa_key):
        script = Script(
            ok({"orders": [ORDER_PAYLOAD["order"]], "cursor": "next"}),
            ok({"orders": [{**ORDER_PAYLOAD["order"], "order_id": "ord-2"}], "cursor": ""}),
        )
        client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            orders = await client.list_orders(ticker="KXBTC15M-26SEP182145-45", status="resting")

        assert dict(script.requests[0].url.params) == {
            "limit": "1000", "ticker": "KXBTC15M-26SEP182145-45", "status": "resting",
        }
        assert [o.order_id for o in orders] == ["ord-1", "ord-2"]

    async def test_list_orders_rejects_a_repeated_cursor(self, rsa_key):
        script = Script(ok({"orders": [], "cursor": "loop"}))
        client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            with pytest.raises(KalshiError, match="repeated.*cursor"):
                await client.list_orders()

    async def test_list_fills_parses_side_and_price(self, rsa_key):
        fill = {
            "trade_id": "t-1", "order_id": "ord-1", "ticker": "KXBTC15M-26SEP182145-45",
            "side": "no", "action": "buy", "no_price_dollars": "0.68", "count": "3",
            "is_taker": False, "created_time": "2026-09-19T00:00:05Z",
        }
        script = Script(ok({"fills": [fill], "cursor": ""}))
        client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            fills = await client.list_fills(order_id="ord-1")

        assert script.requests[0].url.params["order_id"] == "ord-1"
        assert fills[0].side == "no" and fills[0].price == Decimal("0.68") and fills[0].count == Decimal("3")

    async def test_get_positions_derives_side_from_the_signed_position_field(self, rsa_key):
        payload = {
            "market_positions": [
                {"ticker": "A", "position": "5", "market_exposure_dollars": "1.50"},
                {"ticker": "B", "position": "-3"},
            ]
        }
        script = Script(ok(payload))
        client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            positions = await client.get_positions()

        assert positions[0].side == "yes" and positions[0].count == Decimal("5")
        assert positions[0].market_exposure_usd == Decimal("1.50")
        assert positions[1].side == "no" and positions[1].count == Decimal("3")  # sign strips to a positive count


class TestWriteEndpointsAreDemoOnly:
    async def test_create_order_refuses_against_prod_without_sending_anything(self, rsa_key):
        script = Script(ok(ORDER_PAYLOAD))
        client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            with pytest.raises(KalshiWriteNotAllowedError):
                await client.create_order("KXBTC15M-26SEP182145-45", "yes", count=Decimal("5"), price=Decimal("0.30"))
        assert script.requests == []  # refused before any request was built, let alone signed

    async def test_cancel_order_refuses_against_prod_without_sending_anything(self, rsa_key):
        script = Script(ok(ORDER_PAYLOAD))
        client, _ = make_client(script, env=KalshiEnv.PROD, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            with pytest.raises(KalshiWriteNotAllowedError):
                await client.cancel_order("ord-1")
        assert script.requests == []

    async def test_create_order_against_demo_is_allowed(self, rsa_key):
        script = Script(ok(ORDER_PAYLOAD))
        client, _ = make_client(script, env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            order = await client.create_order("T", "yes", count=Decimal("1"), price=Decimal("0.5"))
        assert order.order_id == "ord-1"


class TestCreateAndCancelOrder:
    async def test_create_order_sends_a_limit_buy_with_a_generated_client_order_id(self, rsa_key):
        script = Script(ok(ORDER_PAYLOAD))
        client, _ = make_client(script, env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            order = await client.create_order(
                "KXBTC15M-26SEP182145-45", "yes", count=Decimal("5"), price=Decimal("0.30")
            )

        assert script.requests[0].url.path == "/trade-api/v2/portfolio/orders"
        body = json.loads(script.requests[0].content)
        assert body["ticker"] == "KXBTC15M-26SEP182145-45"
        assert body["side"] == "yes" and body["action"] == "buy" and body["type"] == "limit"
        assert body["count"] == "5" and body["yes_price_dollars"] == "0.30"
        uuid.UUID(body["client_order_id"])  # a real UUID was generated, not left blank
        assert order.order_id == "ord-1"

    async def test_create_order_honors_an_explicit_client_order_id(self, rsa_key):
        script = Script(ok(ORDER_PAYLOAD))
        client, _ = make_client(script, env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            await client.create_order("T", "no", count=Decimal("2"), price=Decimal("0.6"), client_order_id="my-id")

        body = json.loads(script.requests[0].content)
        assert body["client_order_id"] == "my-id"
        assert body["no_price_dollars"] == "0.6"

    async def test_create_order_without_a_price_is_a_market_order(self, rsa_key):
        script = Script(ok(ORDER_PAYLOAD))
        client, _ = make_client(script, env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            await client.create_order("T", "yes", count=Decimal("1"))

        body = json.loads(script.requests[0].content)
        assert body["type"] == "market"
        assert "yes_price_dollars" not in body and "no_price_dollars" not in body

    async def test_cancel_order_sends_delete_to_the_order_path(self, rsa_key):
        script = Script(ok(ORDER_PAYLOAD))
        client, _ = make_client(script, env=KalshiEnv.DEMO, auth=KalshiAuth("test-key", rsa_key))
        async with client:
            order = await client.cancel_order("ord-1")

        assert script.requests[0].method == "DELETE"
        assert script.requests[0].url.path == "/trade-api/v2/portfolio/orders/ord-1"
        assert order.order_id == "ord-1"
