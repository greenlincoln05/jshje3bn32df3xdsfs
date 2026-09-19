"""Command-line entry point.

Phase 1 commands, both read-only:
  discover    print the open KXBTC15M market: strike, close time, and top of book (no credentials needed)
  auth-check  make one signed GET /portfolio/balance call to prove your API key and signing work
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal

from btcbot.config import ConfigError, KalshiEnv, KalshiSettings, load_config
from btcbot.kalshi_client import KalshiAuth, KalshiAuthError, KalshiClient, KalshiError
from btcbot.market_discovery import find_current_market
from btcbot.models import Market, OrderBook, ParseError, Series, Side

# --------------------------------------------------------------------------- formatting


def fmt_price(price: Decimal | None) -> str:
    """Dollars with 2 to 4 decimals: 0.5400 -> 0.54, 0.0010 -> 0.001."""
    if price is None:
        return "-"
    whole, _, frac = f"{price:.4f}".partition(".")
    return f"{whole}.{frac.rstrip('0').ljust(2, '0')}"


def fmt_size(size: Decimal | None) -> str:
    return "-" if size is None else f"{size:,.2f}"


def fmt_remaining(seconds: float) -> str:
    if seconds <= 0:
        return "closed"
    minutes, tenths = divmod(round(seconds * 10), 600)  # round first so 59.96s cannot print as "0m 60.0s"
    return f"{minutes}m {tenths / 10:04.1f}s"


def fmt_time(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S UTC")


def fmt_strike(market: Market) -> str:
    return "not published" if market.strike is None else f"{market.strike:,.2f}"


def _side_line(book: OrderBook, side: Side) -> str:
    bid, ask = book.best_bid(side), book.best_ask(side)
    return (
        f"  {side.upper():<3}  bid {fmt_price(bid.price if bid else None):>6} x {fmt_size(bid.size if bid else None):>12}"
        f"   ask {fmt_price(ask.price if ask else None):>6} x {fmt_size(ask.size if ask else None):>12}"
        f"   spread {fmt_price(book.spread(side)):>5}   mid {fmt_price(book.mid(side))}"
    )


def render_discovery(env: KalshiEnv, series: Series, market: Market, book: OrderBook, now: datetime) -> str:
    remaining = market.seconds_to_close(now)
    return "\n".join(
        [
            f"Environment : {env.value} (read-only market data, no credentials used)",
            f"Series      : {series.ticker} - {series.title} ({series.frequency}; fees: {series.fee_type} x{series.fee_multiplier})",
            f"Market      : {market.ticker} [{market.status}]",
            f"Event       : {market.event_ticker}",
            f"Question    : {market.title}",
            f"Strike      : {fmt_strike(market)}  (floor_strike, strike_type={market.strike_type or 'unknown'})",
            f"Opened      : {fmt_time(market.open_time)}",
            f"Closes      : {fmt_time(market.close_time)}",
            f"Remaining   : {fmt_remaining(remaining)}  ({remaining:.1f} s)",
            f"Liquidity   : volume {fmt_size(market.volume)} contracts, open interest {fmt_size(market.open_interest)}",
            "Top of book (dollars per contract; asks are implied from opposite-side bids):",
            _side_line(book, "yes"),
            _side_line(book, "no"),
            f"Depth       : {len(book.yes_bids)} YES bid levels, {len(book.no_bids)} NO bid levels",
        ]
    )


def render_watch_line(market: Market, book: OrderBook, now: datetime) -> str:
    def quote(side: Side) -> str:
        bid, ask = book.best_bid(side), book.best_ask(side)
        return f"{fmt_price(bid.price if bid else None)}/{fmt_price(ask.price if ask else None)}"

    return (
        f"{now:%H:%M:%SZ} {market.ticker} strike={fmt_strike(market)} "
        f"tau={market.seconds_to_close(now):6.1f}s YES {quote('yes')} NO {quote('no')}"
    )


# --------------------------------------------------------------------------- commands


async def _watch(client: KalshiClient, series_ticker: str, interval: float) -> int:
    while True:
        now = datetime.now(timezone.utc)
        try:
            market = await find_current_market(client, series_ticker)
            if market is None:
                print(f"{now:%H:%M:%SZ} no open {series_ticker} market", flush=True)
            else:
                book = await client.get_orderbook(market.ticker)
                now = datetime.now(timezone.utc)
                if not market.is_open_at(now):
                    print(f"{now:%H:%M:%SZ} {market.ticker} closed during quote fetch; retrying discovery", flush=True)
                else:
                    print(render_watch_line(market, book, now), flush=True)
        except (KalshiError, ParseError) as exc:  # a network blip or odd payload should not end a long watch
            print(f"{now:%H:%M:%SZ} error: {exc}", flush=True)
        await asyncio.sleep(interval)


async def _cmd_discover(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    series_ticker = args.series or config.series_ticker
    env = KalshiEnv(args.env) if args.env else KalshiSettings().env
    async with KalshiClient(env) as client:  # no auth: market data is public
        if args.watch:
            return await _watch(client, series_ticker, args.watch)
        series = await client.get_series(series_ticker)
        market = await find_current_market(client, series_ticker)
        if market is None:
            print(
                f"No open {series_ticker} market on {env.value} right now "
                "(between windows, or nothing is listed). Try again in a few seconds.",
                file=sys.stderr,
            )
            return 1
        book = await client.get_orderbook(market.ticker)
    now = datetime.now(timezone.utc)
    if not market.is_open_at(now):
        print(f"Market {market.ticker} closed during quote fetch; retry discovery.", file=sys.stderr)
        return 1
    print(render_discovery(env, series, market, book, now))
    return 0


async def _cmd_auth_check(args: argparse.Namespace) -> int:
    settings = KalshiSettings()
    if settings.key_id is None or settings.private_key_path is None:
        raise ConfigError("KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH must both be set (see .env.example)")
    auth = KalshiAuth.from_pem_file(settings.key_id.get_secret_value(), settings.private_key_path)
    env = KalshiEnv(args.env) if args.env else settings.env
    async with KalshiClient(env, auth=auth) as client:
        balance = await client.get_balance()
    print(f"OK: the {env.value} environment accepted the signed request.")
    print(f"    available balance ${balance.available:,.2f}; portfolio value ${balance.portfolio_value:,.2f}")
    return 0


# --------------------------------------------------------------------------- entry point


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="btcbot", description="Paper-first bot for Kalshi's 15-minute BTC markets.")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml (default: ./config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging (never logs credentials)")
    commands = parser.add_subparsers(dest="command", required=True)
    envs = [env.value for env in KalshiEnv]

    discover = commands.add_parser("discover", help="show the open market: strike, close time, top of book")
    discover.add_argument("--env", choices=envs, help="Kalshi environment (default: KALSHI_ENV, else demo)")
    discover.add_argument("--series", help="override series_ticker from config.yaml")
    discover.add_argument(
        "--watch", type=float, metavar="SECONDS", help="print one compact line every SECONDS until Ctrl+C"
    )
    discover.set_defaults(handler=_cmd_discover)

    auth_check = commands.add_parser("auth-check", help="verify API key + signing with one signed balance request")
    auth_check.add_argument("--env", choices=envs, help="Kalshi environment (default: KALSHI_ENV, else demo)")
    auth_check.set_defaults(handler=_cmd_auth_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "discover" and args.watch is not None and (not math.isfinite(args.watch) or args.watch <= 0):
        print("error: --watch must be finite and greater than 0", file=sys.stderr)
        return 2
    for stream in (sys.stdout, sys.stderr):  # a non-ASCII title must not crash a Windows console
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpcore").setLevel(logging.WARNING)  # its debug traces include request headers
    try:
        return asyncio.run(args.handler(args))
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    except KalshiAuthError as exc:
        print(
            f"error: {exc}\nhint: demo and production credentials are separate, so KALSHI_ENV / --env must "
            "match the environment the key was created in; also check that your system clock is accurate.",
            file=sys.stderr,
        )
        return 1
    except (ConfigError, KalshiError, ValueError, OSError) as exc:  # ValueError covers ParseError and bad settings
        print(f"error: {exc}", file=sys.stderr)
        return 1
