"""``btcbot demo-probe``: a diagnostic that shows exactly how Kalshi's demo environment answers every way of
reading an order back.

Why it exists: ``demo-check`` found that an order accepted by the V2 create endpoint could not be read back with
``GET /portfolio/orders/{id}`` (HTTP 404 not_found) and was not returned by the order list, even though the
docs list neither as sharded or deprecated. Guessing at the cause costs the owner a round trip per guess, so
this places ONE tiny, deliberately unfillable order per side (1 contract at $0.01), tries every documented read
path against it, prints each raw response (status and body, with account ids masked), cancels it, and reads it
again. Read the output, not this docstring.

Demo only: it refuses any other environment, and the client's own gate refuses to sign an order against prod.
No Claude Code session runs it; it needs the owner's demo key.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Protocol

from btcbot.config import KalshiEnv
from btcbot.kalshi_client import KalshiError, KalshiWriteNotAllowedError
from btcbot.market_discovery import find_current_market

_MASK = re.compile(r'("(?:user_id|member_id|subaccount_id)"\s*:\s*")[^"]+(")')
_MAX_BODY = 900


class ProbeClient(Protocol):
    env: KalshiEnv

    async def probe_get(self, endpoint: str, params: dict[str, str] | None = ..., *, authenticated: bool = ...) -> tuple[int, str]: ...
    async def create_order(self, ticker: str, side: str, *, count: Decimal, price: Decimal | None = ...) -> Any: ...
    async def cancel_order(self, order_id: str, *, market_ticker: str | None = ...) -> Any: ...
    async def cancel_all_resting_orders(self) -> Any: ...
    async def get_orderbook(self, ticker: str, *, depth: int = ...) -> Any: ...
    async def list_markets(self, *, series_ticker: str, status: str | None = ...) -> list[Any]: ...


def redact(body: str) -> str:
    """Account identifiers are not secrets, but there is no reason to paste them around either."""
    text = _MASK.sub(r"\1***\2", body)
    try:
        text = json.dumps(json.loads(text), separators=(",", ":"))
    except ValueError:
        pass
    return text if len(text) <= _MAX_BODY else text[:_MAX_BODY] + f"... [{len(text) - _MAX_BODY} more chars]"


def low_levels(book: Any, side: str, ceiling: Decimal = Decimal("0.05")) -> list[tuple[str, str]]:
    levels = book.yes_bids if side == "yes" else book.no_bids
    return [(str(level.price), str(level.size)) for level in levels if level.price <= ceiling]


async def run_probe(client: ProbeClient, series_ticker: str, *, say: Callable[[str], None] = print, sleep=asyncio.sleep) -> int:
    if getattr(client, "env", None) is not KalshiEnv.DEMO:
        raise KalshiWriteNotAllowedError("demo-probe only runs against the DEMO environment")
    market = await find_current_market(client, series_ticker)
    if market is None:
        say(f"No open {series_ticker} market right now; run again in a few seconds.")
        return 1
    ticker, shard = market.ticker, market.exchange_index
    say(f"Probing {ticker} (exchange shard {shard}). One 1-contract order at $0.01 per side, always cancelled.")

    async def get(label: str, endpoint: str, params: dict[str, str] | None = None) -> str:
        try:
            status, body = await client.probe_get(endpoint, params)
        except KalshiError as exc:
            say(f"  {label}\n      -> transport error: {exc}")
            return ""
        say(f"  {label}\n      -> HTTP {status}: {redact(body)}")
        return body

    placed: list[tuple[str, str]] = []
    try:
        for side in ("yes", "no"):
            say(f"\n=== {side.upper()} order ===")
            book = await client.get_orderbook(ticker)
            say(f"  order book {side.upper()} levels <= $0.05 BEFORE: {low_levels(book, side)}")
            try:
                ack = await client.create_order(ticker, side, count=Decimal(1), price=Decimal("0.01"))
            except KalshiError as exc:
                say(f"  create_order -> {exc}")
                continue
            order_id = ack.order_id
            placed.append((order_id, side))
            say(f"  create_order -> accepted: order_id={order_id} remaining={getattr(ack, 'remaining_count', '?')}")
            await sleep(1.5)  # rule out "not visible yet" before blaming the endpoint
            book = await client.get_orderbook(ticker)
            say(f"  order book {side.upper()} levels <= $0.05 AFTER : {low_levels(book, side)}   (our +1 contract should appear at 0.01)")
            await get("GET /portfolio/orders/{id}", f"/portfolio/orders/{order_id}")
            await get("GET /portfolio/orders?ticker=T", "/portfolio/orders", {"ticker": ticker})
            await get("GET /portfolio/orders?ticker=T&status=resting", "/portfolio/orders", {"ticker": ticker, "status": "resting"})
            await get("GET /portfolio/orders?status=resting", "/portfolio/orders", {"status": "resting"})
            if shard is not None:
                await get(f"GET /portfolio/orders?exchange_index={shard}&status=resting", "/portfolio/orders",
                          {"exchange_index": str(shard), "status": "resting"})
            await get("GET /portfolio/orders/{id}/queue_position", f"/portfolio/orders/{order_id}/queue_position")
            await get("GET /portfolio/positions?ticker=T", "/portfolio/positions", {"ticker": ticker})
            await get("GET /portfolio/fills?ticker=T", "/portfolio/fills", {"ticker": ticker})
            if side == "yes":
                await get("GET /portfolio/balance", "/portfolio/balance")
            try:
                cancel = await client.cancel_order(order_id, market_ticker=ticker)
                placed.remove((order_id, side))
                say(f"  cancel_order -> ok: reduced_by={getattr(cancel, 'reduced_by', '?')}")
            except KalshiError as exc:
                say(f"  cancel_order -> {exc}")
            await sleep(1.0)
            await get("GET /portfolio/orders/{id} after cancel", f"/portfolio/orders/{order_id}")
    finally:
        for order_id, side in placed:  # anything not confirmed cancelled above: one more try, loudly
            try:
                await client.cancel_order(order_id, market_ticker=ticker)
                say(f"  cleanup: cancelled {side.upper()} order {order_id}")
            except KalshiError as exc:
                say(f"  CLEANUP FAILED for {side.upper()} order {order_id}: {exc}. Run `btcbot demo` or `demo-check` once: "
                    "both cancel every open demo order at startup.")
    try:
        await client.cancel_all_resting_orders()
        say("  cleanup: cancelled every resting demo order (the read-free safety net)")
    except KalshiError as exc:
        say(f"  cleanup sweep FAILED: {exc}. Cancel resting orders by hand in the demo Orders tab.")
    say("\nPaste everything above (it contains no keys) so the read path can be fixed against real responses.")
    return 0
