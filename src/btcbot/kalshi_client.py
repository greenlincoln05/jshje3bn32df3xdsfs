"""Kalshi Trade API client: RSA-PSS request signing and an async REST client with retries.

Phase 1 is read-only: there are deliberately no order-placing methods yet. Public market-data endpoints
(series, events, markets, orderbook) need no credentials; only ``get_balance`` is signed. The WebSocket
client arrives with the recorder in Phase 2 and will reuse :class:`KalshiAuth` for its handshake.

API facts here were checked against docs.kalshi.com on 2026-09-18 (see README, "Verified Kalshi API facts").
"""

from __future__ import annotations

import asyncio
import base64
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from btcbot import __version__
from btcbot.config import KalshiEnv
from btcbot.models import Balance, Market, OrderBook, Series, require

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
    ) -> None:
        self.env = env
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
        data = await self._request("GET", "/markets", params=params)
        if data.get("cursor"):
            log.warning("markets for %s span more than one page; only the first %s were read", series_ticker, params["limit"])
        return [Market.from_api(market) for market in data.get("markets") or ()]

    async def get_market(self, ticker: str) -> Market:
        data = await self._request("GET", f"/markets/{quote(ticker, safe='')}")
        return Market.from_api(require(data, "market", "market response"))

    async def get_orderbook(self, ticker: str, *, depth: int = 0) -> OrderBook:
        """Resting bids for both sides. ``depth`` 0 returns every level; 1-100 limits it."""
        params = {"depth": str(depth)} if depth else None
        data = await self._request("GET", f"/markets/{quote(ticker, safe='')}/orderbook", params=params)
        return OrderBook.from_api(ticker, data)

    # ---- account (signed)

    async def get_balance(self) -> Balance:
        data = await self._request("GET", "/portfolio/balance", authenticated=True)
        return Balance.from_api(data)

    # ---- transport

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, str] | None = None,
        authenticated: bool = False,
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
                response = await self._http.request(method, path, params=params, headers=headers)
            except httpx.TransportError as exc:
                if idempotent and attempt < self._max_retries:
                    await self._backoff(attempt, None, f"{label} ({type(exc).__name__})")
                    attempt += 1
                    continue
                raise KalshiConnectionError(f"{label}: {type(exc).__name__}: {exc}") from exc
            status = response.status_code
            log.debug("%s -> %d in %.0f ms", label, status, (time.monotonic() - started) * 1000)
            if status < 400:
                return self._decode(label, response)
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
    def _decode(label: str, response: httpx.Response) -> dict[str, Any]:
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
