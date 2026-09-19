"""Command-line entry point.

Phase 1 commands, both read-only:
  discover    print the open KXBTC15M market: strike, close time, and top of book (no credentials needed)
  auth-check  make one signed GET /portfolio/balance call to prove your API key and signing work

Phase 2:
  record      poll public market data + a Coinbase spot feed into a SQLite database (no credentials needed)

Phase 3:
  calibrate   Brier score + reliability table from predictions logged into a recorder database

Phase 4:
  backtest    replay recorded data through the model, strategy, risk and paper broker

Phase 5:
  paper       run the paper strategy against live public data in real time (no real orders)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import math
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from btcbot.backtest import BacktestError, BacktestReport, run_backtest
from btcbot.config import ConfigError, KalshiEnv, KalshiSettings, load_config
from btcbot.kalshi_client import KalshiAuth, KalshiAuthError, KalshiClient, KalshiError
from btcbot.live_paper import LivePaperTrader
from btcbot.market_discovery import find_current_market
from btcbot.model import CalibrationSummary, compute_calibration_report
from btcbot.models import Market, OrderBook, ParseError, Series, Side
from btcbot.paper_broker import QueueAssumption
from btcbot.recorder import Recorder, RecorderSummary
from btcbot.spot_feed import CoinbaseSpotFeed, SpotBuffer

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


def render_recorder_summary(summary: RecorderSummary) -> str:
    elapsed = (summary.stopped_at - summary.started_at).total_seconds()
    lines = [
        f"Stopped     : {summary.stop_reason} ({summary.stop_detail})",
        f"Ran         : {fmt_time(summary.started_at)} to {fmt_time(summary.stopped_at)} ({elapsed:,.0f}s)",
        f"Order books : {summary.orderbook_polls:,} polls",
        f"Market state: {summary.market_state_changes:,} rows",
        f"Spot ticks  : {summary.spot_ticks:,}",
        f"Settlements : {summary.settlements:,}",
        f"Rollover gaps: {summary.rollover_gaps:,}",
        f"Errors      : {summary.errors:,}",
    ]
    if summary.unresolved_settlements:
        lines.append(f"Unresolved settlements (never saw 'finalized'): {', '.join(summary.unresolved_settlements)}")
    return "\n".join(lines)


def render_calibration_report(report: Sequence[CalibrationSummary]) -> str:
    lines = []
    for summary in report:
        if summary.n == 0:
            lines.append(f"{summary.label:<6s}: no resolved predictions logged")
            continue
        lines.append(f"{summary.label:<6s}: n={summary.n:,}  brier score={summary.brier_score:.4f} (0 is perfect)")
        for row in summary.reliability:
            if row.count == 0:
                continue
            lines.append(
                f"  p in [{row.bin_low:.2f}, {row.bin_high:.2f})  n={row.count:>5,}  "
                f"mean predicted={row.mean_predicted:.3f}  observed rate={row.observed_rate:.3f}"
            )
    return "\n".join(lines)


def render_backtest_reports(reports: Sequence[BacktestReport]) -> str:
    blocks = []
    for r in reports:
        win_rate = "-" if r.win_rate is None else f"{r.win_rate:.1%}"
        avg_p = "-" if r.avg_p_side_at_entry is None else f"{r.avg_p_side_at_entry:.3f}"
        trades_per_day = "-" if r.trades_per_day is None else f"{r.trades_per_day:,.2f}"
        beats = "-" if r.beats_trade_nothing is None else ("yes" if r.beats_trade_nothing else "no")
        blocks.append(
            "\n".join(
                [
                    f"--- queue={r.queue_assumption}  maker_fee_multiplier={r.maker_fee_multiplier} ---",
                    f"Windows      : {r.windows_traded:,} traded of {r.windows_seen:,} seen",
                    f"Trades       : {r.trades:,} ({r.wins:,} won, {r.losses:,} lost, {r.unresolved:,} unresolved)",
                    f"Win rate     : {win_rate}   avg predicted prob at entry: {avg_p}",
                    f"PnL          : ${r.total_pnl_usd:,.6f}   fees paid: ${r.total_fees_usd:,.6f}",
                    f"Max drawdown : ${r.max_drawdown_usd:,.6f}",
                    f"Trades/day   : {trades_per_day}",
                    f"Beats trade-nothing after fees? {beats}",
                    r.sample_size_note,
                ]
            )
        )
    return "\n\n".join(blocks)


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


async def _cmd_record(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    series_ticker = args.series or config.series_ticker
    env = KalshiEnv(args.env) if args.env else KalshiSettings().env
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    db_path = data_dir / f"recorder-{series_ticker}-{env.value}-{timestamp}.sqlite"

    async with KalshiClient(env) as client:  # public data only: no auth, per Phase 2 scope
        recorder = Recorder(
            client,
            series_ticker=series_ticker,
            db_path=db_path,
            kill_file=args.kill_file,
            poll_interval_sec=args.poll_interval,
        )
        spot_feed = CoinbaseSpotFeed(SpotBuffer(), on_tick=recorder.record_spot_tick)
        spot_task = asyncio.ensure_future(spot_feed.run_forever())
        print(
            f"Recording {series_ticker} ({env.value}, public data only) to {db_path}\n"
            f"for up to {args.hours:.2f}h. Create {args.kill_file} to stop early.",
            flush=True,
        )
        try:
            summary = await recorder.run(duration_sec=args.hours * 3600)
        finally:
            spot_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await spot_task
            await spot_feed.aclose()
            recorder.close()
    print(render_recorder_summary(summary))
    return 0


async def _cmd_calibrate(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"error: no such database: {db_path}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(str(db_path))
    try:
        report = compute_calibration_report(conn, bins=args.bins)
    except sqlite3.OperationalError as exc:
        print(f"error: {db_path} does not look like a recorder database with logged predictions: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if all(summary.n == 0 for summary in report):
        print(f"No resolved predictions found in {db_path} yet: nothing to score.")
        return 0
    print(render_calibration_report(report))
    return 0


async def _cmd_backtest(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"error: no such database: {db_path}", file=sys.stderr)
        return 1
    config = load_config(args.config)
    queues = (
        (QueueAssumption.OPTIMISTIC, QueueAssumption.PESSIMISTIC)
        if args.queue == "both"
        else (QueueAssumption(args.queue),)
    )
    multipliers = (Decimal("0"), Decimal("0.25")) if args.maker_fee_multiplier == "both" else (Decimal(args.maker_fee_multiplier),)
    conn = sqlite3.connect(str(db_path))
    try:
        reports = [
            run_backtest(conn, config, queue_assumption=queue, maker_fee_multiplier=multiplier)
            for queue in queues
            for multiplier in multipliers
        ]
    except (BacktestError, sqlite3.OperationalError, ParseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(render_backtest_reports(reports))
    return 0


async def _cmd_paper(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    series_ticker = args.series or config.series_ticker
    env = KalshiEnv(args.env) if args.env else KalshiSettings().env
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    db_path = data_dir / f"paper-{series_ticker}-{env.value}-{timestamp}.sqlite"
    queue_assumption = QueueAssumption(args.queue)
    maker_fee_multiplier = Decimal(args.maker_fee_multiplier)

    async with KalshiClient(env) as client:  # public data only: no auth; paper never places a real order
        recorder = Recorder(
            client,
            series_ticker=series_ticker,
            db_path=db_path,
            kill_file=args.kill_file,
            poll_interval_sec=args.poll_interval,
        )
        # a second connection to the same file: recorder.py owns the base tables, this owns predictions
        trader_conn = sqlite3.connect(str(db_path))
        spot_buffer = SpotBuffer()
        trader = LivePaperTrader(
            trader_conn,
            config,
            spot_buffer,
            queue_assumption=queue_assumption,
            maker_fee_multiplier=maker_fee_multiplier,
            kill_file=args.kill_file,
        )
        recorder.on_orderbook = trader.on_orderbook_snapshot
        recorder.on_settlement = trader.on_settlement

        def on_tick(tick):
            recorder.record_spot_tick(tick)
            trader.on_spot_tick(tick.price)

        spot_feed = CoinbaseSpotFeed(spot_buffer, on_tick=on_tick)
        spot_task = asyncio.ensure_future(spot_feed.run_forever())
        print(
            f"Paper trading {series_ticker} ({env.value}, simulated fills only -- no real orders) to {db_path}\n"
            f"for up to {args.hours:.2f}h. Create {args.kill_file} to stop early and cancel any open order.",
            flush=True,
        )
        summary = None
        try:
            summary = await recorder.run(duration_sec=args.hours * 3600)
        finally:
            spot_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await spot_task
            await spot_feed.aclose()
            await trader.shutdown(summary.stopped_at if summary is not None else datetime.now(timezone.utc))
            trader.close()
            recorder.close()
    print(render_recorder_summary(summary))
    print()
    print("Trading report (feed this database to `btcbot backtest` to compare against a replay of it):")
    print(render_backtest_reports([trader.report()]))
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

    record = commands.add_parser(
        "record", help="poll public market data + a Coinbase spot feed into SQLite (no credentials needed)"
    )
    record.add_argument("--env", choices=envs, help="Kalshi environment (default: KALSHI_ENV, else demo)")
    record.add_argument("--series", help="override series_ticker from config.yaml")
    record.add_argument("--hours", type=float, default=9.0, help="stop after this many hours (default: 9)")
    record.add_argument("--data-dir", default="data", help="directory for the SQLite database (default: ./data)")
    record.add_argument("--kill-file", default="KILL", help="creating this file stops recording (default: ./KILL)")
    record.add_argument(
        "--poll-interval", type=float, default=1.0, metavar="SECONDS", help="seconds between polls (default: 1.0)"
    )
    record.set_defaults(handler=_cmd_record)

    calibrate = commands.add_parser(
        "calibrate", help="Brier score + reliability table from predictions logged into a recorder database"
    )
    calibrate.add_argument("--db", required=True, help="path to a recorder SQLite database with logged predictions")
    calibrate.add_argument("--bins", type=int, default=10, help="number of reliability bins (default: 10)")
    calibrate.set_defaults(handler=_cmd_calibrate)

    backtest = commands.add_parser(
        "backtest", help="replay recorded data through the model, strategy, risk and paper broker (Phase 4)"
    )
    backtest.add_argument("--db", required=True, help="path to a recorder SQLite database")
    backtest.add_argument(
        "--queue", choices=["optimistic", "pessimistic", "both"], default="both",
        help="queue-fill assumption: see paper_broker.py (default: both)",
    )
    backtest.add_argument(
        "--maker-fee-multiplier", default="both",
        help="0, 0.25, another non-negative number, or 'both' for 0 and 0.25 (default: both)",
    )
    backtest.set_defaults(handler=_cmd_backtest)

    paper = commands.add_parser(
        "paper", help="run the paper strategy against live public data in real time (Phase 5, no real orders)"
    )
    paper.add_argument("--env", choices=envs, help="Kalshi environment (default: KALSHI_ENV, else demo)")
    paper.add_argument("--series", help="override series_ticker from config.yaml")
    paper.add_argument("--hours", type=float, default=9.0, help="stop after this many hours (default: 9)")
    paper.add_argument("--data-dir", default="data", help="directory for the SQLite database (default: ./data)")
    paper.add_argument(
        "--kill-file", default="KILL", help="creating this file stops trading, cancelling any open order (default: ./KILL)"
    )
    paper.add_argument(
        "--poll-interval", type=float, default=1.0, metavar="SECONDS", help="seconds between polls (default: 1.0)"
    )
    paper.add_argument(
        "--queue", choices=["optimistic", "pessimistic"], default="optimistic",
        help="queue-fill assumption: see paper_broker.py (default: optimistic)",
    )
    paper.add_argument("--maker-fee-multiplier", default="0", help="a non-negative number (default: 0)")
    paper.set_defaults(handler=_cmd_paper)
    return parser


def _is_non_negative_decimal(value: str) -> bool:
    try:
        return Decimal(value) >= 0
    except InvalidOperation:
        return False


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "discover" and args.watch is not None and (not math.isfinite(args.watch) or args.watch <= 0):
        print("error: --watch must be finite and greater than 0", file=sys.stderr)
        return 2
    if args.command == "record":
        if not math.isfinite(args.hours) or args.hours <= 0:
            print("error: --hours must be finite and greater than 0", file=sys.stderr)
            return 2
        if not math.isfinite(args.poll_interval) or args.poll_interval <= 0:
            print("error: --poll-interval must be finite and greater than 0", file=sys.stderr)
            return 2
    if args.command == "calibrate" and args.bins <= 0:
        print("error: --bins must be greater than 0", file=sys.stderr)
        return 2
    if args.command == "backtest" and args.maker_fee_multiplier != "both" and not _is_non_negative_decimal(args.maker_fee_multiplier):
        print("error: --maker-fee-multiplier must be 'both' or a non-negative number", file=sys.stderr)
        return 2
    if args.command == "paper":
        if not math.isfinite(args.hours) or args.hours <= 0:
            print("error: --hours must be finite and greater than 0", file=sys.stderr)
            return 2
        if not math.isfinite(args.poll_interval) or args.poll_interval <= 0:
            print("error: --poll-interval must be finite and greater than 0", file=sys.stderr)
            return 2
        if not _is_non_negative_decimal(args.maker_fee_multiplier):
            print("error: --maker-fee-multiplier must be a non-negative number", file=sys.stderr)
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
