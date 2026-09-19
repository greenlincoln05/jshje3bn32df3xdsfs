"""Kalshi Trade API client: RSA-PSS request signing and an async REST client with retries.

Public market-data endpoints (series, events, markets, orderbook) need no credentials. ``get_balance``,
``get_order``, ``list_orders``, ``list_fills`` and ``get_positions`` are signed, read-only account queries,
allowed against either environment (same as ``get_balance`` today). ``create_order`` and ``cancel_order``
are the only calls that can change real-world state, so they refuse to sign against anything but the demo
environment (:exc:`KalshiWriteNotAllowedError`) until the owner explicitly approves Phase 7 and its four
gates (spec section 7) -- see CLAUDE.md.

API facts for every method above ``create_order`` were checked against docs.kalshi.com and live responses
on 2026-09-18 (see README, "Verified Kalshi API facts"). The order/fill/position write-endpoint shapes were
added in Phase 6 without network access to this sandbox and are **not** similarly verified -- see
:class:`btcbot.models.KalshiOrder`'s docstring. The owner's first real ``btcbot demo-check`` run is what
actually confirms or corrects them; a shape mismatch fails loudly (a 400/422 from Kalshi's demo API, not a
silent wrong order) precisely because nothing here can touch real money.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from btcbot import __version__
from btcbot.config import KalshiEnv
from btcbot.models import (
    Balance,
    CancelAck,
    KalshiFill,
    KalshiOrder,
    Market,
    OrderAck,
    OrderBook,
    ParseError,
    Position,
    Series,
    Side,
    require,
)

log = logging.getLogger("btcbot.kalshi")

API_PREFIX = "/trade-api/v2"

# Kalshi's recommended hosts. The older api.elections.kalshi.com / demo-api.kalshi.co hosts also work.
HOSTS: Mapping[KalshiEnv, str] = {
    KalshiEnv.DEMO: "https://external-api.demo.kalshi.co",
    KalshiEnv.PROD: "https://external-api.kalshi.com",
}


# --------------------------------------------------------------------------- signing


def signing_message(timestamp_ms: str, method: str, path: str) -> str:
    """The exact string Kalshi expects signed: timestamp + METHOD + path, with the query string stripped.

    ``path`` is the full request path from the host root, including the ``/trade-api/v2`` prefix.
    """
    return f"{timestamp_ms}{method.upper()}{path.split('?', 1)[0]}"


def _now_ms() -> int:
    """Epoch milliseconds as an exact integer (int(time.time() * 1000) can be off by one from float rounding)."""
    return time.time_ns() // 1_000_000


class KalshiAuth:
    """An API key id plus RSA private key, producing the three signed headers Kalshi requires."""

    def __init__(
        self,
        key_id: str,
        private_key: rsa.RSAPrivateKey,
        *,
        clock_ms: Callable[[], int] = _now_ms,
    ) -> None:
        if not key_id:
            raise ValueError("key_id is empty")
        self._key_id = key_id
        self._private_key = private_key
        self._clock_ms = clock_ms

    @classmethod
    def from_pem_file(cls, key_id: str, path: str | Path, *, clock_ms: Callable[[], int] = _now_ms) -> KalshiAuth:
        try:
            key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"could not load a PEM private key from {path}: {exc}") from None
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError(f"{path} does not hold an RSA private key")
        return cls(key_id, key, clock_ms=clock_ms)

    def sign(self, message: str) -> str:
        """RSA-PSS over SHA-256, MGF1(SHA-256), salt length = digest length; base64-encoded."""
        signature = self._private_key.sign(
            message.encode("utf-8"),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    def headers(self, method: str, path: str) -> dict[str, str]:
        timestamp = str(self._clock_ms())
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": self.sign(signing_message(timestamp, method, path)),
        }

    @property
    def key_id_suffix(self) -> str:
        """The last four characters of the key id: enough to match it against the id Kalshi lists, not enough to
        use it."""
        return self._key_id[-4:]

    def __repr__(self) -> str:  # never expose key material
        return f"KalshiAuth(key_id=...{self._key_id[-4:]})"


# --------------------------------------------------------------------------- errors


class KalshiError(Exception):
    """Base class for everything this client raises."""


class KalshiConnectionError(KalshiError):
    """No HTTP response was obtained (DNS, TLS, timeout, ...), after any retries."""


class KalshiAPIError(KalshiError):
    """Kalshi answered with an HTTP error status."""

    def __init__(self, status_code: int, message: str, *, code: str | None = None, request: str = "") -> None:
        prefix = f"{request}: " if request else ""
        suffix = f" [{code}]" if code else ""
        super().__init__(f"{prefix}HTTP {status_code}: {message}{suffix}")
        self.status_code = status_code
        self.message = message
        self.code = code


class KalshiAuthError(KalshiAPIError):
    """401/403: bad key or signature, wrong environment, skewed clock, or missing entitlement."""


class KalshiWriteNotAllowedError(KalshiError):
    """Raised instead of signing a write call (``create_order``/``cancel_order``) against a non-demo
    environment. Not an HTTP error: the request is never sent. The only way to lift this is Phase 7's four
    gates (spec section 7), never a client constructor argument or an environment variable alone."""


def _api_error(label: str, response: httpx.Response) -> KalshiAPIError:
    message = response.reason_phrase or "error"
    code: str | None = None
    try:
        body = response.json()
    except ValueError:
        body = None
        if response.text.strip():
            message = response.text.strip()[:200]
    if isinstance(body, dict):
        # 429 sends {"error": "too many requests"}; other errors nest an object or go flat.
        error = body.get("error", body)
        if isinstance(error, str):
            message = error
        elif isinstance(error, dict):
            raw_code = error.get("code")
            code = None if raw_code is None else str(raw_code)
            message = str(error.get("message") or error.get("details") or message)
    cls = KalshiAuthError if response.status_code in (401, 403) else KalshiAPIError
    return cls(response.status_code, message, code=code, request=label)


# --------------------------------------------------------------------------- client

_RETRYABLE_SERVER_STATUSES = frozenset({500, 502, 503, 504})
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_ORDERS_V2 = "/portfolio/events/orders"  # the legacy POST/DELETE /portfolio/orders are deprecated (docs changelog)
_MARKETABLE_PRICE = Decimal("0.99")  # a buy priced here crosses any resting order on the other side


def _fixed(value: Decimal, places: int) -> str:
    """Kalshi's fixed-point strings ("0.5600", "10.00"). A value with more precision than that is sent
    exactly as given, NOT rounded: rounding would quietly turn an off-grid price into a valid one, and
    ``demo-check`` sends one deliberately to prove the exchange rejects it."""
    unit = Decimal(1).scaleb(-places)
    quantized = value.quantize(unit)
    return format(quantized if quantized == value else value, "f")
_MAX_RETRY_AFTER_SEC = 60.0


class KalshiClient:
    """Async REST client. Use as ``async with KalshiClient(env) as client``. Defaults to the demo environment.

    Retry policy: a 429 (rejected before processing) is retried for any method; 5xx responses and
    transport errors, which may have been processed, are retried only for idempotent methods. Backoff is
    exponential with jitter; Kalshi's 429s carry no ``Retry-After`` today but one is honoured if present.
    """

    def __init__(
        self,
        env: KalshiEnv = KalshiEnv.DEMO,
        *,
        auth: KalshiAuth | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
        max_retries: int = 4,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
        write_log: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.env = env
        self._write_log = write_log
        self._auth = auth
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._http = httpx.AsyncClient(
            base_url=HOSTS[env],
            transport=transport,
            timeout=httpx.Timeout(timeout, connect=5.0),
            headers={"User-Agent": f"btcbot/{__version__}", "Accept": "application/json"},
        )

    async def __aenter__(self) -> KalshiClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- market data (public, no credentials)

    async def get_series(self, series_ticker: str) -> Series:
        data = await self._request("GET", f"/series/{quote(series_ticker, safe='')}")
        return Series.from_api(require(data, "series", "series response"))

    async def list_markets(self, *, series_ticker: str, status: str | None = None) -> list[Market]:
        """Markets in a series. ``status`` filters server-side: unopened, open, closed or settled (unset: all).

        Each market carries its own ``event_ticker``, so no separate events call is needed. The events
        endpoint's status filter was measured lagging the market state by 60+ seconds at a window rollover.
        """
        params = {"series_ticker": series_ticker, "limit": "1000"}  # 1000 is the API maximum
        if status:
            params["status"] = status
        markets: list[Market] = []
        seen_cursors: set[str] = set()
        while True:
            data = await self._request("GET", "/markets", params=params)
            page = require(data, "markets", "markets response")
            if not isinstance(page, list):
                raise ParseError("markets response: markets must be an array")
            markets.extend(Market.from_api(market) for market in page)
            cursor = data.get("cursor")
            if cursor is None or cursor == "":
                return markets
            if not isinstance(cursor, str):
                raise ParseError("markets response: cursor must be a string")
            if cursor in seen_cursors:
                raise KalshiError("markets response repeated a pagination cursor; refusing incomplete results")
            seen_cursors.add(cursor)
            params["cursor"] = cursor

    async def get_market(self, ticker: str) -> Market:
        data = await self._request("GET", f"/markets/{quote(ticker, safe='')}")
        return Market.from_api(require(data, "market", "market response"))

    async def get_orderbook(self, ticker: str, *, depth: int = 0) -> OrderBook:
        """Resting bids for both sides. ``depth`` 0 returns every level; 1-100 limits it."""
        params = {"depth": str(depth)} if depth else None
        data = await self._request("GET", f"/markets/{quote(ticker, safe='')}/orderbook", params=params)
        return OrderBook.from_api(ticker, data)

    # ---- account (signed, read-only -- allowed against either environment, same as get_balance)

    async def get_balance(self) -> Balance:
        data = await self._request("GET", "/portfolio/balance", authenticated=True)
        return Balance.from_api(data)

    async def get_order(self, order_id: str) -> KalshiOrder:
        data = await self._request("GET", f"/portfolio/orders/{quote(order_id, safe='')}", authenticated=True)
        return KalshiOrder.from_api(require(data, "order", "order response"))

    async def list_orders(self, *, ticker: str | None = None, status: str | None = None) -> list[KalshiOrder]:
        params = {"limit": "1000"}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        orders: list[KalshiOrder] = []
        seen_cursors: set[str] = set()
        while True:
            data = await self._request("GET", "/portfolio/orders", params=params, authenticated=True)
            page = require(data, "orders", "orders response")
            if not isinstance(page, list):
                raise ParseError("orders response: orders must be an array")
            orders.extend(KalshiOrder.from_api(o) for o in page)
            cursor = data.get("cursor")
            if cursor is None or cursor == "":
                return orders
            if not isinstance(cursor, str):
                raise ParseError("orders response: cursor must be a string")
            if cursor in seen_cursors:
                raise KalshiError("orders response repeated a pagination cursor; refusing incomplete results")
            seen_cursors.add(cursor)
            params["cursor"] = cursor

    async def list_fills(
        self, *, ticker: str | None = None, order_id: str | None = None, min_ts: int | None = None
    ) -> list[KalshiFill]:
        params = {"limit": "1000"}
        if min_ts is not None:
            params["min_ts"] = str(min_ts)
        if ticker:
            params["ticker"] = ticker
        if order_id:
            params["order_id"] = order_id
        fills: list[KalshiFill] = []
        seen_cursors: set[str] = set()
        while True:
            data = await self._request("GET", "/portfolio/fills", params=params, authenticated=True)
            page = require(data, "fills", "fills response")
            if not isinstance(page, list):
                raise ParseError("fills response: fills must be an array")
            fills.extend(KalshiFill.from_api(f) for f in page)
            cursor = data.get("cursor")
            if cursor is None or cursor == "":
                return fills
            if not isinstance(cursor, str):
                raise ParseError("fills response: cursor must be a string")
            if cursor in seen_cursors:
                raise KalshiError("fills response repeated a pagination cursor; refusing incomplete results")
            seen_cursors.add(cursor)
            params["cursor"] = cursor

    async def get_positions(self) -> list[Position]:
        data = await self._request("GET", "/portfolio/positions", authenticated=True)
        page = require(data, "market_positions", "positions response")
        if not isinstance(page, list):
            raise ParseError("positions response: market_positions must be an array")
        return [Position.from_api(p) for p in page]

    # ---- account (signed, write -- demo-only until the owner approves Phase 7)

    async def create_order(
        self,
        ticker: str,
        side: Side,
        *,
        count: Decimal,
        price: Decimal | None = None,
        client_order_id: str | None = None,
        post_only: bool = True,
    ) -> OrderAck:
        """Places a buy of ``side`` (``"yes"`` or ``"no"``): a resting limit order when ``price`` (in that
        side's own dollars) is given, otherwise a marketable immediate-or-cancel order.

        Kalshi's V2 endpoint (``POST /portfolio/events/orders``) quotes everything from the YES side:
        ``bid`` buys YES, ``ask`` sells YES, and selling YES at ``p`` is buying NO at ``1 - p``. So buying
        NO at 0.68 is sent as an ``ask`` at a YES price of 0.32. (Read from docs.kalshi.com on 2026-09-19;
        ``btcbot demo-check`` verifies it against the real demo account by reading the order back.)

        Resting orders are ``post_only`` by default: a bid meant to join the queue must never cross the
        spread and pay a taker fee, so a book that moved is rejected instead of silently crossed.
        ``client_order_id`` defaults to a fresh UUID so an application-level retry after an ambiguous
        failure cannot double-place."""
        self._require_demo("create_order")
        if count <= 0:
            raise ValueError("count must be positive")
        if price is not None and not (Decimal(0) < price < Decimal(1)):
            raise ValueError("price must be strictly between 0 and 1 dollars")
        marketable = price is None
        own_price = _MARKETABLE_PRICE if marketable else price
        book_side, yes_price = ("bid", own_price) if side == "yes" else ("ask", Decimal(1) - own_price)
        body: dict[str, Any] = {
            "ticker": ticker,
            "side": book_side,
            "count": _fixed(count, 2),
            "price": _fixed(yes_price, 4),
            "time_in_force": "immediate_or_cancel" if marketable else "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        if not marketable and post_only:
            body["post_only"] = True
        self._audit("create_order", ticker=ticker, side=side, book_side=book_side, count=body["count"],
                    price=body["price"], time_in_force=body["time_in_force"], client_order_id=body["client_order_id"])
        try:
            data = await self._request("POST", _ORDERS_V2, authenticated=True, json_body=body)
        except KalshiError as exc:
            self._audit("create_order_failed", client_order_id=body["client_order_id"], error=str(exc)[:300])
            raise
        try:
            ack = OrderAck.from_api(data)
        except ParseError as exc:
            # The exchange answered 2xx: the order may well exist even though its id could not be read.
            self._audit("create_order_unreadable_ack", client_order_id=body["client_order_id"], error=str(exc), raw=str(data)[:300])
            raise
        self._audit("create_order_ack", client_order_id=body["client_order_id"], order_id=ack.order_id,
                    fill_count=str(ack.fill_count), remaining_count=str(ack.remaining_count))
        return ack

    async def cancel_order(self, order_id: str, *, market_ticker: str | None = None) -> CancelAck:
        """``market_ticker`` is needed for Kalshi's auto-routing: an order id alone cannot identify the
        exchange shard (docs: DELETE /portfolio/events/orders/{order_id})."""
        self._require_demo("cancel_order")
        params = {"market_ticker": market_ticker} if market_ticker else None
        self._audit("cancel_order", order_id=order_id, ticker=market_ticker)
        try:
            data = await self._request(
                "DELETE", f"{_ORDERS_V2}/{quote(order_id, safe='')}", params=params, authenticated=True
            )
            ack = CancelAck.from_api(data)
        except (KalshiError, ParseError) as exc:
            self._audit("cancel_order_failed", order_id=order_id, error=str(exc)[:300])
            raise
        self._audit("cancel_order_ack", order_id=order_id, reduced_by=str(ack.reduced_by))
        return ack

    async def cancel_all_resting_orders(self) -> dict[str, Any]:
        """Cancel every resting order on the account (``DELETE /portfolio/events/orders``). Needs no reads, so
        it works even when an order cannot be looked up, which is exactly when it is needed: it is the safety
        net that stops a failed check leaving orders behind. Demo-only like every write."""
        self._require_demo("cancel_all_resting_orders")
        self._audit("cancel_all_resting_orders")
        try:
            data = await self._request("DELETE", _ORDERS_V2, authenticated=True, allow_empty=True)
        except KalshiError as exc:
            self._audit("cancel_all_resting_orders_failed", error=str(exc)[:300])
            raise
        self._audit("cancel_all_resting_orders_ack", response=str(data)[:300])
        return data

    def _audit(self, event: str, **fields: Any) -> None:
        """One JSON-serialisable record per write action, handed to ``write_log`` (the CLI appends it to
        ``data/order-audit.jsonl``). This is the bot's own ledger of everything it asked Kalshi to do, so the
        account's order history can be checked against it. Never blocks trading: a logging failure is only
        a warning."""
        if self._write_log is None:
            return
        try:
            self._write_log({"ts": datetime.now(timezone.utc).isoformat(), "env": self.env.value, "event": event, **fields})
        except Exception as exc:  # noqa: BLE001 -- an audit-log problem must never take an order path down
            log.warning("order audit log failed: %s", exc)

    async def server_time_skew_sec(self) -> float | None:
        """How far this machine's clock is ahead of Kalshi's (negative = behind), from the ``Date`` header of one
        public request; None if it could not be measured. Kalshi rejects a signature whose timestamp is too far
        from its own clock with the same 401 as a bad key, so this separates the two."""
        try:
            response = await self._http.request("GET", f"{API_PREFIX}/exchange/status")
            server = parsedate_to_datetime(response.headers["Date"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return None
        return (datetime.now(timezone.utc) - server).total_seconds()

    async def probe_get(
        self, endpoint: str, params: Mapping[str, str] | None = None, *, authenticated: bool = True
    ) -> tuple[int, str]:
        """A raw GET for diagnostics (``btcbot demo-probe``): the status code and body text, whatever they are.
        Unlike every other method it does not retry, parse, or raise on an HTTP error, because its whole job is
        to show exactly what Kalshi said. Read-only."""
        path = f"{API_PREFIX}{endpoint}"
        headers = self._auth_headers("GET", path) if authenticated else None
        try:
            response = await self._http.request("GET", path, params=params, headers=headers)
        except httpx.TransportError as exc:
            raise KalshiConnectionError(f"GET {path}: {type(exc).__name__}: {exc}") from exc
        return response.status_code, response.text

    async def set_target_balance_allocation(self, percent_by_exchange: Mapping[int, int]) -> None:
        """Opt in to (or change) Kalshi's automatic collateral rebalancing across exchange shards: every ~10 s
        it moves the account's sweepable balance toward these percentages. Needed because crypto markets trade on
        their own shard (2) and an order there is rejected with ``insufficient_shard_balance`` until collateral
        is on it. A write to account state, so demo-only like the order calls. Percentages must total 100;
        an empty mapping disables rebalancing."""
        self._require_demo("set_target_balance_allocation")
        if percent_by_exchange and sum(percent_by_exchange.values()) != 100:
            raise ValueError("allocation percentages must total 100")
        if any(i < 0 or not 0 <= p <= 100 for i, p in percent_by_exchange.items()):
            raise ValueError("exchange_index must be >= 0 and each percent between 0 and 100")
        body = {"allocations": [{"exchange_index": i, "percent": p} for i, p in sorted(percent_by_exchange.items())]}
        self._audit("set_target_balance_allocation", allocations=body["allocations"])
        await self._request("POST", "/portfolio/target_balance_allocation", authenticated=True, json_body=body)

    def _require_demo(self, action: str) -> None:
        if self.env is not KalshiEnv.DEMO:
            raise KalshiWriteNotAllowedError(
                f"{action}: refusing to sign a write call against {self.env.value} -- write endpoints are "
                "demo-only until the owner explicitly approves Phase 7 (spec section 7's four gates)"
            )

    # ---- transport

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, str] | None = None,
        authenticated: bool = False,
        json_body: Mapping[str, Any] | None = None,
        allow_empty: bool = False,
    ) -> Any:
        method = method.upper()
        path = f"{API_PREFIX}{endpoint}"
        label = f"{method} {path}"
        idempotent = method in _IDEMPOTENT_METHODS
        attempt = 0
        while True:
            # Sign inside the loop so every retry carries a fresh timestamp and signature.
            headers = self._auth_headers(method, path) if authenticated else None
            started = time.monotonic()
            try:
                response = await self._http.request(method, path, params=params, headers=headers, json=json_body)
            except httpx.TransportError as exc:
                if idempotent and attempt < self._max_retries:
                    await self._backoff(attempt, None, f"{label} ({type(exc).__name__})")
                    attempt += 1
                    continue
                raise KalshiConnectionError(f"{label}: {type(exc).__name__}: {exc}") from exc
            status = response.status_code
            log.debug("%s -> %d in %.0f ms", label, status, (time.monotonic() - started) * 1000)
            if status < 400:
                return self._decode(label, response, allow_empty=allow_empty)
            retryable = status == 429 or (status in _RETRYABLE_SERVER_STATUSES and idempotent)
            if retryable and attempt < self._max_retries:
                await self._backoff(attempt, response.headers.get("Retry-After"), f"{label} (HTTP {status})")
                attempt += 1
                continue
            raise _api_error(label, response)

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        if self._auth is None:
            raise KalshiError(
                f"{method} {path} needs API credentials (set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH)"
            )
        return self._auth.headers(method, path)

    @staticmethod
    def _decode(label: str, response: httpx.Response, *, allow_empty: bool = False) -> dict[str, Any]:
        if allow_empty and not response.content.strip():
            return {}  # e.g. cancel-all: Kalshi answers a success with no body at all
        try:
            # parse_float=Decimal keeps bare JSON numbers (floor_strike) out of binary floating point.
            data = response.json(parse_float=Decimal)
        except ValueError as exc:
            raise KalshiError(f"{label}: response was not valid JSON") from exc
        if not isinstance(data, dict):  # every Trade API endpoint used here returns a JSON object
            raise KalshiError(f"{label}: expected a JSON object, got {type(data).__name__}")
        return data

    async def _backoff(self, attempt: int, retry_after: str | None, reason: str) -> None:
        ceiling = min(self._backoff_cap, self._backoff_base * 2**attempt)
        delay = self._rng.uniform(ceiling / 2, ceiling)  # jitter so parallel clients don't retry in lockstep
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), _MAX_RETRY_AFTER_SEC))
            except ValueError:
                pass  # HTTP-date form; Kalshi does not send it, so the computed backoff stands
        log.warning("%s; retry %d/%d in %.2fs", reason, attempt + 1, self._max_retries, delay)
        await self._sleep(delay)
