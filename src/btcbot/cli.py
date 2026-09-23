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

Phase 6:
  demo-check  place and cancel real (fake-money) orders against Kalshi's demo environment to validate
              order/cancel/fill handling; never targets prod (see kalshi_client.py's demo-only write gate)

Not a phase (a monitoring tool):
  dashboard    local web UI for backtests, live paper PnL/trades, and local Kalshi settings
  demo-report  re-render the demo-vs-paper fill-gap report from an existing demo database (read-only, no key)

ML entry/exit layers (docs/research/ml-layers-handoff.md), owner-driven, not a numbered phase:
  download-history  ONE-TIME backfill of settled markets + Coinbase candles (public data, no key --
                     owner-run: this session's environment cannot reach Kalshi/Coinbase, see CLAUDE.md)
  ml-train          train an entry/exit model from recorded data, validated on markets it never trained on
  ml-ablation       compare current/ML entries x settlement-hold/ML-exit as four independent layers
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
import yaml

from btcbot.backtest import BacktestError, BacktestReport, load_replay_data, run_backtest
from btcbot.candidate_suite import load_candidate_suite, render_candidate_suite, run_candidate_suite
from btcbot.coinbase_history import CoinbaseHistoryError, fetch_candle_history
from btcbot.config import ConfigError, KalshiEnv, KalshiSettings, load_config
from btcbot.demo_check import DemoCheckReport, run_demo_check
from btcbot.history_pipeline import (
    HistoryError,
    fetch_all_settled_markets,
    init_history_schema,
    load_candles,
    load_market_outcomes,
    save_candles,
    save_market_outcomes,
)
from btcbot.kalshi_client import HOSTS, KalshiAuth, KalshiAuthError, KalshiClient, KalshiError
from btcbot.lab import (
    DEFAULT_GRID,
    AccountSettings,
    LabError,
    load_lab_data,
    parse_values,
    render_lab_report,
    render_ml_ablation_report,
    run_lab,
    run_ml_ablation,
)
from btcbot.demo_check import collateral_preflight
from btcbot.demo_probe import run_probe
from btcbot.demo_trader import DemoTrader, render_demo_report
from btcbot.execution import DemoExecutionBackend
from btcbot.live_paper import LivePaperTrader
from btcbot.market_discovery import find_current_market
from btcbot.market_level_pipeline import MarketLevelError, train_and_validate_market_level
from btcbot.ml_model import MLModelError, check_feature_coverage, load_model, save_model
from btcbot.ml_pipeline import (
    FEATURE_STORE_ENTRY_FEATURES,
    MLPipelineError,
    train_and_validate,
    train_and_validate_from_features,
)
from btcbot.model import CalibrationSummary, compute_calibration_report
from btcbot.models import Market, OrderBook, ParseError, Series, Side, parse_time
from btcbot.paper_broker import QueueAssumption
from btcbot.recorder import Recorder, RecorderSummary
from btcbot.spot_feed import CoinbaseSpotFeed, SpotBuffer
from btcbot.stream_recorder import StreamRecorder, compare_brti_to_spot
from btcbot.webui import create_dashboard_server

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


def render_demo_check_report(report: DemoCheckReport) -> str:
    lines = [f"Demo check against {report.ticker}" if report.ticker else "Demo check"]
    for result in report.results:
        marker = "PASS" if result.passed else "SKIP" if result.passed is None else "FAIL"
        lines.append(f"[{marker}] {result.name}: {result.detail}")
    lines.append("")
    lines.append(f"Fidelity (paper fee model vs real demo fills): {report.fidelity.summary}")
    lines.append("")
    lines.append("Overall: OK" if report.ok else "Overall: FAILED -- see the FAIL row(s) above")
    return "\n".join(lines)


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


def describe_credentials(auth: KalshiAuth, key_path: Path, env: KalshiEnv) -> str:
    """What auth-check is about to use, so it can be compared with what Kalshi lists. Never the key itself."""
    size = key_path.stat().st_size if key_path.exists() else "missing"
    return (
        f"Checking the {env.value} environment ({HOSTS[env]})\n"
        f"    key id ends in ...{auth.key_id_suffix} (compare with the id Kalshi lists for this key)\n"
        f"    key file {key_path} ({size} bytes)"
    )


async def _cmd_auth_check(args: argparse.Namespace) -> int:
    settings = KalshiSettings()
    if settings.key_id is None or settings.private_key_path is None:
        raise ConfigError("KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH must both be set (see .env.example)")
    auth = KalshiAuth.from_pem_file(settings.key_id.get_secret_value(), settings.private_key_path)
    env = KalshiEnv(args.env) if args.env else settings.env
    print(describe_credentials(auth, Path(settings.private_key_path), env), flush=True)
    async with KalshiClient(env, auth=auth) as client:
        try:
            balance = await client.get_balance()
        except KalshiAuthError:
            skew = await client.server_time_skew_sec()
            if skew is None:
                print("    clock: could not be measured", file=sys.stderr)
            else:
                verdict = "fine" if abs(skew) < 5 else "TOO FAR OFF: fix the system clock, Kalshi rejects skewed timestamps"
                print(f"    clock: {skew:+.1f} s vs Kalshi ({verdict})", file=sys.stderr)
            print(
                "    If the clock is fine, Kalshi does not accept this key id with this key file. A private key cannot "
                "be re-downloaded, so if this file is not the one generated with that id, create a new API key.",
                file=sys.stderr,
            )
            raise
    print(f"OK: the {env.value} environment accepted the signed request.")
    print(f"    available balance ${balance.available:,.2f}; portfolio value ${balance.portfolio_value:,.2f}")
    return 0


async def _cmd_demo_check(args: argparse.Namespace) -> int:
    settings = KalshiSettings()
    if settings.key_id is None or settings.private_key_path is None:
        raise ConfigError("KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH must both be set (see .env.example)")
    auth = KalshiAuth.from_pem_file(settings.key_id.get_secret_value(), settings.private_key_path)
    config = load_config(args.config)
    series_ticker = args.series or config.series_ticker
    print(
        f"Running demo-check against {series_ticker} on the demo environment (fake money; places and cancels "
        "real demo orders). This never touches prod -- see kalshi_client.py's demo-only write gate.",
        flush=True,
    )
    async with KalshiClient(KalshiEnv.DEMO, auth=auth, write_log=order_audit_log()) as client:  # hardcoded: demo-check never targets prod
        report = await run_demo_check(client, series_ticker, wait_for_settlement=args.wait_for_settlement)
    print(render_demo_check_report(report))
    return 0 if report.ok else 1


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


def render_polymarket_summary(summary) -> str:
    lines = [
        f"Started    : {summary.started_at.isoformat()}",
        f"Stopped    : {summary.stopped_at.isoformat()} ({summary.stop_reason}: {summary.stop_detail})",
        f"Book polls : {summary.book_polls:,}",
        f"Markets    : {summary.market_state_changes:,} seen, {summary.settlements:,} settlements recorded, "
        f"{summary.rollover_gaps:,} gaps with no open event",
        f"Errors     : {summary.errors:,}",
    ]
    if summary.unresolved_settlements:
        lines.append(f"Unresolved : {', '.join(summary.unresolved_settlements)}")
    return "\n".join(lines)


async def _cmd_record_polymarket(args: argparse.Namespace) -> int:
    """READ-ONLY: poll Polymarket's public "Bitcoin Up or Down" series into SQLite. No wallet, no key, no
    order code -- see polymarket_client.py's module docstring. A separate venue from Kalshi; this command
    does not place, and cannot place, any order."""
    from btcbot.polymarket_client import PolymarketClient
    from btcbot.polymarket_recorder import PolymarketRecorder

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    db_path = data_dir / f"polymarket-btc-updown-{args.horizon}-{timestamp}.sqlite"

    async with PolymarketClient() as client:
        recorder = PolymarketRecorder(
            client, horizon=args.horizon, db_path=db_path, kill_file=args.kill_file,
            poll_interval_sec=args.poll_interval,
        )
        print(
            f"Recording Polymarket btc-updown-{args.horizon} (public data only, no wallet/key) to {db_path}\n"
            f"for up to {args.hours:.2f}h. Create {args.kill_file} to stop early.",
            flush=True,
        )
        try:
            summary = await recorder.run(duration_sec=args.hours * 3600)
        finally:
            recorder.close()
    print(render_polymarket_summary(summary))
    return 0


def render_stream_summary(recorder_summary: RecorderSummary | None, stream: StreamRecorder, db_path: Path) -> str:
    stats = stream.stats
    lines = []
    if recorder_summary is not None:
        lines.append(render_recorder_summary(recorder_summary))
    last = f" (last ${stats.last_brti:,.2f})" if stats.last_brti is not None else ""
    unknown = f" ({', '.join(sorted(stats.unknown_types))})" if stats.unknown_types else ""
    lines += [
        f"BRTI ticks  : {stats.brti_ticks:,}{last}",
        f"Book stream : {stats.book_snapshots:,} snapshots, {stats.book_deltas:,} deltas",
        f"Reconnects  : {stats.reconnects:,} ({stats.resyncs:,} because a book could not be trusted)",
        f"Dropped     : {stats.malformed_messages:,} malformed, {stats.unknown_messages:,} unrecognised{unknown}",
    ]
    conn = sqlite3.connect(str(db_path))
    try:
        cmp = compare_brti_to_spot(conn)
    finally:
        conn.close()
    if cmp.n:
        lines.append(
            f"BRTI vs Coinbase ({cmp.n:,} paired ticks): mean diff ${cmp.mean_diff:,.2f}, "
            f"mean |diff| ${cmp.mean_abs_diff:,.2f}, median |diff| ${cmp.median_abs_diff:,.2f}, "
            f"max |diff| ${cmp.max_abs_diff:,.2f}"
        )
    else:
        lines.append("BRTI vs Coinbase: no paired ticks (no BRTI received, or no Coinbase ticks in the same window)")
    if cmp.mean_feed_lag_ms is not None:
        lines.append(f"Feed lag    : Kalshi received BRTI {cmp.mean_feed_lag_ms:,.0f} ms after CF published it (mean)")
    return "\n".join(lines)


async def _print_stream_status(stream: StreamRecorder, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        st = stream.stats
        last = f"${st.last_brti:,.2f}" if st.last_brti is not None else "waiting"
        print(
            f"{datetime.now(timezone.utc):%H:%M:%SZ} BRTI {last}  ticks {st.brti_ticks:,}  "
            f"book events {st.book_snapshots + st.book_deltas:,}  reconnects {st.reconnects}",
            flush=True,
        )


async def _cmd_stream(args: argparse.Namespace) -> int:
    """Authenticated, READ-ONLY WebSocket capture. Needs the owner's own key in .env; no order is ever sent."""
    config = load_config(args.config)
    series_ticker = args.series or config.series_ticker
    settings = KalshiSettings()
    if settings.key_id is None or settings.private_key_path is None:
        raise ConfigError(
            "KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH must both be set (see .env.example, or the dashboard's "
            "Settings tab). Kalshi's WebSocket needs a signed handshake even for public data."
        )
    auth = KalshiAuth.from_pem_file(settings.key_id.get_secret_value(), settings.private_key_path)
    env = KalshiEnv(args.env) if args.env else settings.env
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    db_path = data_dir / f"stream-{series_ticker}-{env.value}-{timestamp}.sqlite"

    async with KalshiClient(env) as client:  # REST stays public/unsigned; only the WebSocket handshake signs
        recorder = Recorder(
            client, series_ticker=series_ticker, db_path=db_path, kill_file=args.kill_file,
            poll_interval_sec=args.poll_interval,
        )

        async def current_ticker() -> str | None:
            market = await find_current_market(client, series_ticker)
            return market.ticker if market is not None else None

        stream = StreamRecorder(
            db_path, auth, env, ticker_provider=current_ticker, index_ids=(args.index,), include_5hz=not args.no_5hz,
        )
        spot_feed = CoinbaseSpotFeed(SpotBuffer(), on_tick=recorder.record_spot_tick)
        spot_task = asyncio.ensure_future(spot_feed.run_forever())
        rec_task = asyncio.ensure_future(recorder.run(duration_sec=args.hours * 3600))
        stream_task = asyncio.ensure_future(stream.run_forever())
        status_task = asyncio.ensure_future(_print_stream_status(stream, args.status_every))
        print(
            f"Streaming {args.index} + {series_ticker} order book ({env.value}, read-only, no orders) to {db_path}\n"
            f"for up to {args.hours:.2f}h. Create {args.kill_file} to stop early.",
            flush=True,
        )
        summary: RecorderSummary | None = None
        error: BaseException | None = None
        try:
            await asyncio.wait({rec_task, stream_task}, return_when=asyncio.FIRST_COMPLETED)
            if stream_task.done() and not stream_task.cancelled():
                error = stream_task.exception()  # run_forever only ends on its own with a StreamError
            if rec_task.done() and not rec_task.cancelled() and rec_task.exception() is None:
                summary = rec_task.result()
        finally:
            for task in (status_task, stream_task, spot_task, rec_task):
                task.cancel()
            for task in (status_task, stream_task, spot_task, rec_task):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            await spot_feed.aclose()
            stream.close()
            recorder.close()
    print(render_stream_summary(summary, stream, db_path))
    if error is not None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


async def _cmd_download_history(args: argparse.Namespace) -> int:
    """ONE-TIME backfill of settled KXBTC15M markets + Coinbase 1-minute candles, for the market-level ML
    pipeline (docs/research/ml-layers-handoff.md). Public, unauthenticated endpoints only, same as `record` --
    but this session's own environment cannot reach Kalshi/Coinbase (see CLAUDE.md, docs/running-live.md), so
    this is the owner's to run, not something a Claude Code session ever executes for real."""
    env = KalshiEnv(args.env) if args.env else KalshiSettings().env
    series_ticker = args.series or "KXBTC15M"
    try:
        start, end = parse_time(args.start), parse_time(args.end)
    except ParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    db_path = Path(args.output) if args.output else data_dir / f"history-{series_ticker}-{env.value}-{timestamp}.sqlite"

    conn = sqlite3.connect(str(db_path))
    try:
        init_history_schema(conn)
        async with KalshiClient(env) as client:  # public data only, no auth, same rule as `record`
            markets = await fetch_all_settled_markets(client, series_ticker=series_ticker)
        written_markets = save_market_outcomes(conn, markets)
        async with httpx.AsyncClient() as http:
            candles = await fetch_candle_history(http, start=start, end=end)
        written_candles = save_candles(conn, candles)
    except (KalshiError, CoinbaseHistoryError, HistoryError, ParseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(
        f"Wrote {written_markets} settled markets and {written_candles} Coinbase 1-minute candles to {db_path}.\n"
        "This is raw historical data, not a profitability claim; see btcbot ml-train / btcbot.market_level_pipeline."
    )
    return 0


async def _cmd_lab(args: argparse.Namespace) -> int:
    """Sweep entry timing / price band / trend / account size / risk sizing on recorded data, ranked on a
    training slice and judged on a held-out test slice. Offline: no network, no credentials."""
    config = load_config(args.config)
    paths = [Path(p) for p in args.db]
    if not paths:
        # demo-environment books are mostly synthetic (1-contract bids, 22c/78c quotes); sweeping them would
        # teach the lab nothing true about prod, so they are only used when named with --db.
        found = sorted(Path(args.data_dir).glob("*.sqlite"))
        paths = [p for p in found if "-demo-" not in p.name]
        if len(paths) != len(found):
            print(f"Skipping {len(found) - len(paths)} demo-environment file(s); name one with --db to include it.", file=sys.stderr)
    if not paths:
        print(f"error: no data files. Run `btcbot paper` or `btcbot record` first (looked in {args.data_dir}).", file=sys.stderr)
        return 1
    grid: dict = {}
    for spec in args.grid or []:
        key, sep, raw = spec.partition("=")
        if not sep:
            print(f"error: --grid expects key=value1,value2 (got {spec!r})", file=sys.stderr)
            return 2
        grid[key.strip()] = parse_values(key.strip(), raw)
    if not grid:
        grid = DEFAULT_GRID
        print("No --grid given; sweeping the default grid (see `btcbot lab --help`).", file=sys.stderr)
    account = AccountSettings(
        account_usd=Decimal(args.account), max_exposure_pct=Decimal(args.exposure_pct),
        daily_loss_pct=Decimal(args.daily_loss_pct),
    )

    def progress(done: int, total: int, label: str) -> None:
        if done % 10 == 0 or done == total:
            print(f"  {done}/{total} {label}", file=sys.stderr, flush=True)

    try:
        data = load_lab_data(paths)
        report = run_lab(
            data, config, grid, account=account, train_fraction=args.split, queue=QueueAssumption(args.queue),
            maker_fee_multiplier=Decimal(args.maker_fee_multiplier), top_k=args.top,
            min_train_trades=args.min_train_trades, max_combos=args.max_combos, progress=progress,
        )
    except (LabError, BacktestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(render_lab_report(report))
    return 0


async def _cmd_features(args: argparse.Namespace) -> int:
    """Flatten recorder databases into one model-ready CSV. Offline: no network, no key."""
    from btcbot.features import FeatureError, build_rows, write_csv

    paths = [Path(path) for path in args.db]
    if not paths:
        paths = sorted(path for path in Path(args.data_dir).glob("*.sqlite") if args.include_demo or "-demo-" not in path.name)
    try:
        rows = build_rows(paths, step_sec=args.step_sec)
    except (FeatureError, BacktestError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    n = write_csv(rows, output)
    labeled = sum(1 for r in rows if r["outcome_yes"] is not None)
    print(f"wrote {n} rows ({labeled} with a settled outcome) from {len(paths)} database(s) to {output}")
    return 0


async def _cmd_retrain_check(args: argparse.Namespace) -> int:
    """One-shot weekly check: build the features CSV, train an entry model on it, validate it against the
    current model on the same held-out windows -- `btcbot features` + `btcbot ml-train --features` +
    `btcbot validate --model` chained into one call. Offline: no network, no key, nothing wired into
    `btcbot paper`/`btcbot demo`; only writes the features CSV and the model JSON and prints a report."""
    from btcbot.retrain_check import RetrainCheckError, render, run_retrain_check

    paths = [Path(path) for path in args.db]
    if not paths:
        paths = sorted(path for path in Path(args.data_dir).glob("*.sqlite") if args.include_demo or "-demo-" not in path.name)
    try:
        result = run_retrain_check(
            paths, features_path=args.features_out, model_path=args.model_out, step_sec=args.step_sec,
            split=args.split, embargo=args.embargo, min_test_trades=args.min_test_trades,
        )
    except (RetrainCheckError, BacktestError, MLModelError, sqlite3.Error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(render(result))
    return 0


async def _cmd_validate(args: argparse.Namespace) -> int:
    """Time-split validation of the recorded model probabilities on a features CSV, optionally alongside a
    trained ML model (--model, btcbot ml-train's output) on the SAME split -- the answer to "does the ML
    model actually beat what is already running" (docs/research/ml-layers-handoff.md). Offline: no network,
    no key."""
    from btcbot.features import read_csv
    from btcbot.validation import ValidationError, blend_predictor, validate

    try:
        rows = read_csv(args.features)
        results = {
            "current model (p_blend)": validate(
                rows, blend_predictor, train_frac=args.split, embargo=args.embargo, min_test_trades=args.min_test_trades
            ),
        }
        if args.model:
            model = load_model(Path(args.model))
            check_feature_coverage(model, FEATURE_STORE_ENTRY_FEATURES)
            results[f"ML model ({args.model})"] = validate(
                rows, model.predict_proba, train_frac=args.split, embargo=args.embargo, min_test_trades=args.min_test_trades
            )
    except (ValidationError, OSError, ValueError, KeyError, MLModelError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for label, out in results.items():
        print(f"=== {label} ===")
        for name, ev in out.items():
            rate = "-" if ev.win_rate is None else f"{ev.win_rate:.0%} (95% {ev.ci95[0]:.0%}-{ev.ci95[1]:.0%})"
            brier = "-" if ev.brier is None else f"{ev.brier:.4f}"
            print(f"{name}: windows {ev.windows}, trades {ev.trades}, wins {ev.wins}, win rate {rate}, "
                  f"pnl/contract ${ev.pnl_per_contract:.2f}, brier {brier}\n  verdict: {ev.verdict}")
    if args.model:
        base_test = results["current model (p_blend)"]["test"]
        model_test = results[f"ML model ({args.model})"]["test"]
        print(
            f"\nTest-window pnl/contract: current model ${base_test.pnl_per_contract:.2f} vs ML model "
            f"${model_test.pnl_per_contract:.2f} -- not a paired test between the two; read each verdict "
            "above on its own terms, not just which number is larger."
        )
    return 0


async def _cmd_disagree(args: argparse.Namespace) -> int:
    """Calibration and model-vs-market disagreement report on a features CSV. Offline: no network, no key."""
    from btcbot.calibration_report import build_report, render
    from btcbot.features import read_csv

    try:
        print(render(build_report(read_csv(args.features), at_tau_sec=args.at_tau, field=args.field, min_n=args.min_n)))
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


async def _cmd_watch(args: argparse.Namespace) -> int:
    """Health check and summary of the newest recorder/demo databases. Read-only; no network, no key."""
    from btcbot.watch import render, summarize

    try:
        items = summarize(args.data_dir, stale_min=args.stale_min)
    except (OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(render(items))
    return 2 if any(i.stale for i in items) else 0


async def _cmd_demo_report(args: argparse.Namespace) -> int:
    """Re-render the demo-vs-paper fill-gap report (the same one `btcbot demo` prints when it finishes) from
    an already-recorded demo database, without re-running it -- the roadmap milestone "a paper-vs-demo fill
    gap we understand" needs no live run to check the next morning. Offline, read-only; no key, no network."""
    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"error: no such database: {db_path}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        print(render_demo_report(conn))
    except sqlite3.OperationalError as exc:
        print(f"error: {db_path} does not look like a demo database: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


async def _cmd_fillcheck(args: argparse.Namespace) -> int:
    """Check recorded fills against the public trade tape. Offline: no network, no key."""
    from btcbot.fillcheck import FillCheckError, check_fills, render

    try:
        print(render(check_fills(args.db, before_sec=args.before_sec)))
    except (FillCheckError, sqlite3.Error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


async def _cmd_lab_suite(args: argparse.Namespace) -> int:
    """Evaluate a frozen set of named candidates against shared recorded data."""
    config = load_config(args.config)
    paths = [Path(path) for path in args.db]
    if not paths:
        paths = sorted(path for path in Path(args.data_dir).glob("*.sqlite") if "-demo-" not in path.name)
    if not paths:
        print("error: no production recording files found", file=sys.stderr)
        return 1
    try:
        suite = load_candidate_suite(args.suite, config)
        cutoff = None if args.after is None else parse_time(args.after)
        report = run_candidate_suite(load_lab_data(paths), config, suite, after=cutoff)
    except (LabError, BacktestError, KeyError, TypeError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, default=str, indent=2), encoding="utf-8")
    print(render_candidate_suite(report))
    print(f"\nFull ledger: {output}")
    return 0


async def _cmd_ml_train(args: argparse.Namespace) -> int:
    """Train an ML entry/exit model, validated on markets it never trained on
    (docs/research/ml-layers-handoff.md). --features trains an entry model on a `btcbot features` CSV
    instead of replaying a recorder database directly -- the richer, book-imbalance/depth/multi-window
    -momentum feature set `btcbot validate`/`btcbot disagree` already share, and its output plugs straight
    into `btcbot validate --model` for a full PnL-level comparison against the bot's current model. Offline
    either way: no network, no credentials. --history trains the coarser, candle-only market-level model
    (btcbot.market_level_pipeline) from a `btcbot download-history` database instead -- a calibration
    correction over the v1 model's own formula, with a DIFFERENT feature schema (`p_model`, `sigma`) that
    `btcbot ml-ablation` / `btcbot validate --model` cannot load."""
    output = Path(args.out)
    if args.history:
        db_path = Path(args.history)
        if not db_path.is_file():
            print(f"error: no such database: {db_path}", file=sys.stderr)
            return 1
        conn = sqlite3.connect(str(db_path))
        try:
            outcomes = load_market_outcomes(conn)
            candles = load_candles(conn)
        except sqlite3.OperationalError as exc:
            print(f"error: {db_path} does not look like a download-history database: {exc}", file=sys.stderr)
            return 1
        finally:
            conn.close()
        try:
            model, mreport = train_and_validate_market_level(outcomes, candles, train_fraction=args.split, embargo=args.embargo)
        except MarketLevelError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        output.parent.mkdir(parents=True, exist_ok=True)
        save_model(model, output)
        print(
            f"Trained a market-level calibration model on {mreport.markets_train} settled markets "
            f"({mreport.train_examples} examples); validated on {mreport.markets_validate} later markets it "
            f"never trained on ({mreport.validate_examples} examples).\n"
            f"Brier score: train {mreport.train_brier:.4f}, validate {mreport.validate_brier:.4f}; the v1 "
            f"model's own p_model, unchanged, scores {mreport.baseline_validate_brier:.4f} on the SAME validate "
            f"markets -- beats baseline: {'YES' if mreport.beats_baseline else 'no'}.\n"
            f"Saved to {output}. Feature schema is (p_model, sigma) -- NOT compatible with `btcbot ml-ablation` "
            "or `btcbot validate --model`, which expect the tick-level or feature-store schemas. A calibration "
            "measure only: there is no recorded book price this far back to simulate a trade against."
        )
        return 0
    if args.features:
        from btcbot.features import read_csv

        try:
            rows = read_csv(args.features)
            model, freport = train_and_validate_from_features(rows, train_fraction=args.split, embargo=args.embargo)
        except (MLPipelineError, OSError, ValueError, KeyError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        output.parent.mkdir(parents=True, exist_ok=True)
        save_model(model, output)
        baseline = "n/a (no p_blend logged for the validate rows)" if freport.baseline_validate_brier is None else f"{freport.baseline_validate_brier:.4f}"
        beats = "n/a" if freport.beats_baseline is None else ("YES" if freport.beats_baseline else "no")
        print(
            f"Trained an entry model on {freport.windows_train} windows ({freport.train_examples} examples); "
            f"validated on {freport.windows_validate} later windows it never trained on ({freport.validate_examples} examples).\n"
            f"Brier score: train {freport.train_brier:.4f}, validate {freport.validate_brier:.4f}; the bot's "
            f"own current model (p_blend) scores {baseline} on the SAME validate rows -- beats baseline: {beats}.\n"
            f"Saved to {output}. This is a calibration measure, not a profitability claim; run "
            "`btcbot validate --model ...` against this file for a PnL-level comparison."
        )
        return 0

    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"error: no such database: {db_path}", file=sys.stderr)
        return 1
    config = load_config(args.config)
    conn = sqlite3.connect(str(db_path))
    try:
        data = load_replay_data(conn)
    except sqlite3.OperationalError as exc:
        print(f"error: {db_path} does not look like a recorder database: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    try:
        model, report = train_and_validate(data, config, which=args.which, train_fraction=args.split)
    except (MLPipelineError, LabError, BacktestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    save_model(model, output)
    print(
        f"Trained a {report.which} model on {report.windows_train} windows ({report.train_examples} examples); "
        f"validated on {report.windows_validate} later windows it never trained on ({report.validate_examples} examples).\n"
        f"Brier score: train {report.train_brier:.4f}, validate {report.validate_brier:.4f} "
        "(lower is better; a much higher validate score than train means it fit noise in training).\n"
        f"Saved to {output}. This is a calibration measure, not a profitability claim."
    )
    return 0


async def _cmd_ml_ablation(args: argparse.Namespace) -> int:
    """Compare current-entry/ML-entry x settlement-hold/ML-exit as four independent layers on the same
    train/test split (docs/research/ml-layers-handoff.md). Offline: no network, no credentials."""
    paths = [Path(p) for p in args.db]
    if not paths:
        found = sorted(Path(args.data_dir).glob("*.sqlite"))
        paths = [p for p in found if "-demo-" not in p.name]
    if not paths:
        print(f"error: no data files. Run `btcbot paper` or `btcbot record` first (looked in {args.data_dir}).", file=sys.stderr)
        return 1
    config = load_config(args.config)
    account = AccountSettings(
        account_usd=Decimal(args.account), max_exposure_pct=Decimal(args.exposure_pct),
        daily_loss_pct=Decimal(args.daily_loss_pct),
    )
    try:
        data = load_lab_data(paths)
        report = run_ml_ablation(
            data, config, ml_entry_model_path=args.entry_model, ml_exit_model_path=args.exit_model,
            account=account, train_fraction=args.split, queue=QueueAssumption(args.queue),
            maker_fee_multiplier=Decimal(args.maker_fee_multiplier),
        )
    except (LabError, BacktestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(render_ml_ablation_report(report))
    return 0


ORDER_AUDIT_FILE = Path("data") / "order-audit.jsonl"


def order_audit_log(path: Path = ORDER_AUDIT_FILE):
    """Append-only JSON-lines ledger of every write the bot sends to Kalshi (orders, cancels, allocations): the
    bot's own record to check the account's order history against."""
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(record: dict) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str, separators=(",", ":")) + "\n")

    return write


CRYPTO_EXCHANGE_INDEX = 2  # docs.kalshi.com "Exchange Sharding": crypto (KXBTC...) markets trade on shard 2


def allocation_for(shard: int | None, percent: int) -> dict[int, int]:
    """Target percentages: ``percent`` of the balance on the market's shard, the remainder on shard 0."""
    shard = CRYPTO_EXCHANGE_INDEX if shard is None else shard
    if not 1 <= percent <= 100:
        raise ValueError("percent must be between 1 and 100")
    if shard == 0 or percent == 100:
        return {shard: 100}
    return {shard: percent, 0: 100 - percent}


async def _cmd_demo_allocate(args: argparse.Namespace) -> int:
    """Move demo collateral onto the exchange shard the BTC market trades on (Kalshi splits balances across shards;
    an order on a shard with no collateral is rejected with insufficient_shard_balance). Demo only, run once."""
    config = load_config(args.config)
    series_ticker = args.series or config.series_ticker
    settings = KalshiSettings()
    if settings.key_id is None or settings.private_key_path is None:
        raise ConfigError("KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH must both be set (a DEMO key)")
    auth = KalshiAuth.from_pem_file(settings.key_id.get_secret_value(), settings.private_key_path)
    async with KalshiClient(KalshiEnv.DEMO, auth=auth, write_log=order_audit_log()) as client:
        market = await find_current_market(client, series_ticker)
        shard = market.exchange_index if market is not None else None
        if shard is None:
            print(f"Kalshi did not report an exchange shard for {series_ticker}; assuming crypto's shard {CRYPTO_EXCHANGE_INDEX}.")
        target = allocation_for(shard, args.percent)
        shard = shard if shard is not None else CRYPTO_EXCHANGE_INDEX
        before = await client.get_balance()
        print(f"Demo balance ${before.available:,.2f}; by shard: {dict(sorted(before.by_exchange.items())) or 'not reported'}")
        print(f"Setting target allocation {target} (percent by exchange shard). Kalshi rebalances about every 10 s.")
        await client.set_target_balance_allocation(target)
        for _ in range(15):
            await asyncio.sleep(3)
            after = await client.get_balance()
            if after.by_exchange.get(shard, Decimal(0)) > 0:
                print(f"OK: ${after.by_exchange[shard]:,.2f} is now on shard {shard}. Re-run `btcbot demo-check`.")
                return 0
        print(
            f"Allocation accepted, but shard {shard} still shows no funds after 45 s "
            f"(by shard: {dict(sorted(after.by_exchange.items())) or 'not reported'}). Wait a little and re-run "
            "`btcbot demo-check`; if it still fails, paste this output.",
            file=sys.stderr,
        )
        return 1


async def _cmd_demo_probe(args: argparse.Namespace) -> int:
    """Diagnostic: how does the DEMO exchange answer every way of reading an order back? Owner-run; demo only."""
    config = load_config(args.config)
    settings = KalshiSettings()
    if settings.key_id is None or settings.private_key_path is None:
        raise ConfigError("KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH must both be set (a DEMO key)")
    auth = KalshiAuth.from_pem_file(settings.key_id.get_secret_value(), settings.private_key_path)
    async with KalshiClient(KalshiEnv.DEMO, auth=auth, write_log=order_audit_log()) as client:
        return await run_probe(client, args.series or config.series_ticker)


async def _cmd_demo(args: argparse.Namespace) -> int:
    """The paper trader's strategy placing REAL orders in Kalshi's DEMO environment (fake money). Needs the
    owner's own demo key in .env. Always the demo environment: the client itself also refuses to sign an order
    against prod, so this is a second lock, not the only one."""
    config = load_config(args.config)
    if args.plumbing:
        # Thin-book test mode: accept the demo book's wide spread and shallow depth, and bid inside the spread.
        # This exercises placement/fills/settlement/sizing, it says nothing about the strategy.
        config = config.model_copy(update={"max_spread": Decimal(1), "min_depth": Decimal(1), "bid_improve_ticks": 5})
        print("PLUMBING MODE: relaxed spread/depth filters, bidding up to 5 cents inside the spread. "
              "Results here do not test the strategy.", flush=True)
    series_ticker = args.series or config.series_ticker
    settings = KalshiSettings()
    if settings.key_id is None or settings.private_key_path is None:
        raise ConfigError(
            "KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH must both be set (a DEMO key: see .env.example, or the "
            "dashboard's Settings tab)"
        )
    auth = KalshiAuth.from_pem_file(settings.key_id.get_secret_value(), settings.private_key_path)
    env = KalshiEnv.DEMO
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    db_path = data_dir / f"demo-{series_ticker}-{env.value}-{timestamp}.sqlite"
    maker_fee_multiplier = Decimal(args.maker_fee_multiplier)

    async with KalshiClient(env, auth=auth, write_log=order_audit_log()) as client:  # signs only the calls that need it; market data stays public
        balance = await client.get_balance()  # proves the demo key works before anything is placed
        market_now = await find_current_market(client, series_ticker)
        if market_now is not None:
            collateral = collateral_preflight(balance, market_now)
            if collateral.passed is False:
                print(f"error: {collateral.detail}", file=sys.stderr)
                return 1
        reconcile = await DemoExecutionBackend(client, "").reconcile()
        print(
            f"Demo account: available ${balance.available:,.2f}. Cancelled {len(reconcile.cancelled_order_ids)} "
            f"leftover resting order(s); {len(reconcile.open_positions)} open position(s) left alone.",
            flush=True,
        )
        recorder = Recorder(
            client, series_ticker=series_ticker, db_path=db_path, kill_file=args.kill_file,
            poll_interval_sec=args.poll_interval, settle_on_determined=True,
        )
        # The dashboard reads the run's starting account from here, so nobody types a capital number in.
        recorder.log_event("account_start", json.dumps({
            "kind": "demo", "available_usd": str(balance.available), "portfolio_value_usd": str(balance.portfolio_value),
            "source": "kalshi demo balance at start",
        }))
        trader_conn = sqlite3.connect(str(db_path))
        spot_buffer = SpotBuffer()
        trader = DemoTrader(
            trader_conn, config, spot_buffer, client, maker_fee_multiplier=maker_fee_multiplier,
            kill_file=args.kill_file,
        )
        recorder.on_orderbook = trader.on_orderbook_snapshot
        recorder.on_settlement = trader.on_settlement

        def on_tick(tick):
            recorder.record_spot_tick(tick)
            trader.on_spot_tick(tick.price, tick.receive_ts)

        spot_feed = CoinbaseSpotFeed(spot_buffer, on_tick=on_tick)
        spot_task = asyncio.ensure_future(spot_feed.run_forever())
        print(
            f"DEMO trading {series_ticker}: REAL orders, FAKE money, environment={env.value} -> {db_path}\n"
            f"for up to {args.hours:.2f}h. Create {args.kill_file} to stop early (open orders are cancelled).",
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
    print("Trading report (real demo fills; PnL only for windows that have settled):")
    print(render_backtest_reports([trader.report()]))
    print()
    report_conn = sqlite3.connect(str(db_path))
    try:
        print(render_demo_report(report_conn, trader.stats))
    finally:
        report_conn.close()
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
    if config.bid_improve_ticks:
        raise ConfigError("bid_improve_ticks is demo-plumbing only (btcbot demo --plumbing); it must be 0 for paper trading")
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
        recorder.log_event("account_start", json.dumps({
            "kind": "paper", "account_usd": str(config.sizing.account_usd),
            "source": "config sizing.account_usd (paper has no exchange balance)",
        }))
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
            trader.on_spot_tick(tick.price, tick.receive_ts)

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


async def _cmd_dashboard(args: argparse.Namespace) -> int:
    server = create_dashboard_server(
        data_dir=Path(args.data_dir), env_path=Path(args.env_file), config_path=Path(args.config), port=args.port
    )
    port = server.server_address[1]
    print(
        f"Dashboard at http://127.0.0.1:{port} (binds to 127.0.0.1 only -- not reachable from other machines).\n"
        f"Data dir: {args.data_dir}  Settings file: {args.env_file}  Backtest config: {args.config}\n"
        "This dashboard has no path to Kalshi's order endpoints, so nothing here can place, cancel, or "
        "modify an order (even the demo-only ones Phase 6 added elsewhere in this repo). Ctrl+C to stop.",
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
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

    record_pm = commands.add_parser(
        "record-polymarket",
        help="READ-ONLY: poll Polymarket's public 'Bitcoin Up or Down' order books into SQLite (no wallet/key; a separate venue from Kalshi)",
    )
    record_pm.add_argument("--horizon", default="15m", choices=["5m", "15m"], help="which rolling window series to record (default: 15m)")
    record_pm.add_argument("--hours", type=float, default=9.0, help="stop after this many hours (default: 9)")
    record_pm.add_argument("--data-dir", default="data", help="directory for the SQLite database (default: ./data)")
    record_pm.add_argument("--kill-file", default="KILL_PM", help="creating this file stops recording (default: ./KILL_PM)")
    record_pm.add_argument("--poll-interval", type=float, default=2.0, metavar="SECONDS", help="seconds between book polls (default: 2.0)")
    record_pm.set_defaults(handler=_cmd_record_polymarket)

    stream = commands.add_parser(
        "stream",
        help="READ-ONLY authenticated WebSocket capture of BRTI + order-book deltas (needs your own key in .env)",
    )
    stream.add_argument("--env", choices=envs, help="Kalshi environment (default: KALSHI_ENV, else demo)")
    stream.add_argument("--series", help="override series_ticker from config.yaml")
    stream.add_argument("--hours", type=float, default=9.0, help="stop after this many hours (default: 9)")
    stream.add_argument("--data-dir", default="data", help="directory for the SQLite database (default: ./data)")
    stream.add_argument("--kill-file", default="KILL", help="creating this file stops recording (default: ./KILL)")
    stream.add_argument("--index", default="BRTI", help="CF Benchmarks index id (default: BRTI)")
    stream.add_argument("--no-5hz", action="store_true", help="skip the 5 Hz BRTI channel, keep the 1 Hz one")
    stream.add_argument(
        "--poll-interval", type=float, default=5.0, metavar="SECONDS",
        help="seconds between REST polls for market state/settlements/fallback books (default: 5.0)",
    )
    stream.add_argument(
        "--status-every", type=float, default=10.0, metavar="SECONDS", help="status line interval (default: 10)"
    )
    stream.set_defaults(handler=_cmd_stream)

    download = commands.add_parser(
        "download-history",
        help="ONE-TIME backfill: settled KXBTC15M markets + Coinbase 1-minute candles into SQLite "
        "(public data, no key -- owner-run: this session's environment cannot reach Kalshi/Coinbase)",
    )
    download.add_argument("--env", choices=envs, help="Kalshi environment (default: KALSHI_ENV, else demo)")
    download.add_argument("--series", help="override series_ticker from config.yaml")
    download.add_argument("--start", required=True, help="ISO 8601 start of the Coinbase candle range")
    download.add_argument("--end", required=True, help="ISO 8601 end of the Coinbase candle range")
    download.add_argument("--data-dir", default="data", help="directory for the SQLite database (default: ./data)")
    download.add_argument("--output", help="explicit database path (default: a timestamped file in --data-dir)")
    download.set_defaults(handler=_cmd_download_history)

    lab = commands.add_parser(
        "lab",
        help="strategy lab: sweep entry timing, price band, trend, account size, risk sizing; judged on held-out windows",
        description="Sweep strategy parameters over recorded data. Combinations are ranked on the first part of "
        "the windows and shown on the held-out rest. Tunable keys: "
        + ", ".join(sorted(DEFAULT_GRID.keys() | {"min_depth", "max_spread", "min_price", "trend_lookback_sec",
                                                  "trend_min_move_usd", "model_blend", "risk_pct", "contracts", "persist_steps", "max_growth_pct",
                                                  "min_p_side"})),
    )
    lab.add_argument("--db", action="append", default=[], help="recorded database (repeatable; default: every *.sqlite in --data-dir)")
    lab.add_argument("--data-dir", default="data", help="where to look when no --db is given (default: ./data)")
    lab.add_argument("--grid", action="append", metavar="KEY=V1,V2", help="e.g. --grid min_edge=0.02,0.04 --grid max_price=none,0.6 (repeatable)")
    lab.add_argument("--account", default="500", help="pretend account size in USD (default: 500)")
    lab.add_argument("--exposure-pct", default="25", help="max percent of the account at risk at once (default: 25)")
    lab.add_argument("--daily-loss-pct", default="10", help="daily loss percent that halts new orders (default: 10)")
    lab.add_argument("--split", type=float, default=0.7, help="fraction of windows used for ranking; the rest is the held-out test (default: 0.7)")
    lab.add_argument("--queue", choices=[q.value for q in QueueAssumption], default="optimistic", help="queue-fill assumption (default: optimistic, a best case)")
    lab.add_argument("--maker-fee-multiplier", default="0", help="a non-negative number (default: 0)")
    lab.add_argument("--top", type=int, default=8, help="how many top combinations to show on the test slice (default: 8)")
    lab.add_argument("--min-train-trades", type=int, default=20, help="combinations with fewer resolved training trades are not ranked (default: 20)")
    lab.add_argument("--max-combos", type=int, default=400, help="refuse grids larger than this (default: 400)")
    lab.set_defaults(handler=_cmd_lab)

    features = commands.add_parser(
        "features", help="flatten recorder databases into one model-ready CSV of per-snapshot features (offline)"
    )
    features.add_argument("--db", action="append", default=[], help="recorded database (repeatable; default: every prod *.sqlite in --data-dir)")
    features.add_argument("--data-dir", default="data")
    features.add_argument("--include-demo", action="store_true", help="also read demo-* databases when no --db is given")
    features.add_argument("--step-sec", type=float, default=5.0, help="keep at most one row per window per this many seconds (default: 5)")
    features.add_argument("--output", default="data/research/features.csv")
    features.set_defaults(handler=_cmd_features)

    retrain_check = commands.add_parser(
        "retrain-check",
        help="one-shot: features + ml-train --features + validate --model chained (offline; for a weekly re-check)",
    )
    retrain_check.add_argument("--db", action="append", default=[], help="recorded database (repeatable; default: every prod *.sqlite in --data-dir)")
    retrain_check.add_argument("--data-dir", default="data")
    retrain_check.add_argument("--include-demo", action="store_true", help="also read demo-* databases when no --db is given")
    retrain_check.add_argument("--step-sec", type=float, default=5.0, help="keep at most one row per window per this many seconds (default: 5)")
    retrain_check.add_argument("--features-out", default="data/research/features-latest.csv")
    retrain_check.add_argument("--model-out", default="models/entry_latest.json")
    retrain_check.add_argument("--split", type=float, default=0.7)
    retrain_check.add_argument("--embargo", type=int, default=1, help="windows dropped between train and test (default: 1)")
    retrain_check.add_argument("--min-test-trades", type=int, default=30)
    retrain_check.set_defaults(handler=_cmd_retrain_check)

    disagree = commands.add_parser(
        "disagree", help="calibration + model-vs-market disagreement report on a features CSV (offline)")
    disagree.add_argument("--features", default="data/research/features.csv")
    disagree.add_argument("--field", choices=["p_model", "p_blend"], default="p_model")
    disagree.add_argument("--at-tau", type=float, default=300.0, help="read each window this many seconds before close (default: 300)")
    disagree.add_argument("--min-n", type=int, default=10, help="buckets with fewer windows are marked small (default: 10)")
    disagree.set_defaults(handler=_cmd_disagree)

    fillcheck = commands.add_parser(
        "fillcheck", help="check recorded paper/demo fills against the public trade tape (offline)")
    fillcheck.add_argument("--db", required=True, help="a paper-* or demo-* database recorded with the trade tape")
    fillcheck.add_argument("--before-sec", type=float, default=120.0, help="look this far before each fill for a print (default: 120)")
    fillcheck.set_defaults(handler=_cmd_fillcheck)

    watch = commands.add_parser("watch", help="health check + summary of the newest paper/demo databases (read-only)")
    watch.add_argument("--data-dir", default="data")
    watch.add_argument("--stale-min", type=float, default=5.0, help="flag a database not written for this many minutes (default: 5)")
    watch.set_defaults(handler=_cmd_watch)

    demo_report = commands.add_parser(
        "demo-report",
        help="re-render the demo-vs-paper fill-gap report from an existing demo database (read-only, no key)",
    )
    demo_report.add_argument("--db", required=True, help="a demo-*.sqlite database from a previous `btcbot demo` run")
    demo_report.set_defaults(handler=_cmd_demo_report)

    validate_cmd = commands.add_parser(
        "validate", help="time-split validation of the bot's recorded model on a features CSV (offline)")
    validate_cmd.add_argument("--features", default="data/research/features.csv", help="CSV from `btcbot features`")
    validate_cmd.add_argument("--model", help="also validate a trained ML model (btcbot ml-train's output) side by side with the current model")
    validate_cmd.add_argument("--split", type=float, default=0.7)
    validate_cmd.add_argument("--embargo", type=int, default=1, help="windows dropped between train and test (default: 1)")
    validate_cmd.add_argument("--min-test-trades", type=int, default=30)
    validate_cmd.set_defaults(handler=_cmd_validate)

    suite = commands.add_parser(
        "lab-suite",
        help="replay a frozen named candidate suite without placing orders",
    )
    suite.add_argument("--suite", default="docs/research/candidate-suite-v1.yaml", help="frozen candidate YAML")
    suite.add_argument("--db", action="append", default=[], help="recorded production database (repeatable)")
    suite.add_argument("--data-dir", default="data", help="where to look when no --db is given")
    suite.add_argument("--after", help="only evaluate market windows first observed at/after this ISO timestamp")
    suite.add_argument("--output", default="data/research/candidate-suite-latest.json", help="full JSON ledger output")
    suite.set_defaults(handler=_cmd_lab_suite)

    ml_train = commands.add_parser(
        "ml-train",
        help="train an ML entry/exit model from recorded data, validated on later markets it never trained on (offline)",
    )
    ml_train.add_argument("--db", help="path to a recorder SQLite database (tick-level entry/exit model; requires --which)")
    ml_train.add_argument("--features", help="CSV from `btcbot features` instead of --db (richer entry-only model, usable with `btcbot validate --model`)")
    ml_train.add_argument("--history", help="database from `btcbot download-history` instead of --db/--features (coarse market-level calibration model; p_model/sigma schema, not usable with ml-ablation/validate --model)")
    ml_train.add_argument("--which", choices=["entry", "exit"], help="which model to train (--db mode only; required with --db)")
    ml_train.add_argument("--out", required=True, help="output path for the trained model JSON")
    ml_train.add_argument("--split", type=float, default=0.7, help="fraction of windows used to train; the rest validates it (default: 0.7)")
    ml_train.add_argument("--embargo", type=int, default=1, help="windows dropped between train and test (--features/--history mode only, default: 1)")
    ml_train.set_defaults(handler=_cmd_ml_train)

    ablation = commands.add_parser(
        "ml-ablation",
        help="compare current-entry/ML-entry x settlement-hold/ML-exit as four independent layers on the same train/test split (offline)",
    )
    ablation.add_argument("--db", action="append", default=[], help="recorded database (repeatable; default: every *.sqlite in --data-dir)")
    ablation.add_argument("--data-dir", default="data", help="where to look when no --db is given (default: ./data)")
    ablation.add_argument("--entry-model", help="path to a trained entry model JSON (btcbot ml-train --which entry)")
    ablation.add_argument("--exit-model", help="path to a trained exit model JSON (btcbot ml-train --which exit)")
    ablation.add_argument("--account", default="500", help="pretend account size in USD (default: 500)")
    ablation.add_argument("--exposure-pct", default="25", help="max percent of the account at risk at once (default: 25)")
    ablation.add_argument("--daily-loss-pct", default="10", help="daily loss percent that halts new orders (default: 10)")
    ablation.add_argument("--split", type=float, default=0.7, help="fraction of windows used for ranking; the rest is the held-out test (default: 0.7)")
    ablation.add_argument("--queue", choices=[q.value for q in QueueAssumption], default="optimistic", help="queue-fill assumption (default: optimistic, a best case)")
    ablation.add_argument("--maker-fee-multiplier", default="0", help="a non-negative number (default: 0)")
    ablation.set_defaults(handler=_cmd_ml_ablation)

    demo = commands.add_parser(
        "demo",
        help="the paper strategy placing REAL orders in Kalshi's DEMO environment (fake money; needs your demo key)",
    )
    demo.add_argument("--series", help="override series_ticker from config.yaml")
    demo.add_argument("--hours", type=float, default=2.0, help="stop after this many hours (default: 2)")
    demo.add_argument("--data-dir", default="data", help="directory for the SQLite database (default: ./data)")
    demo.add_argument(
        "--kill-file", default="KILL", help="creating this file stops trading and cancels open orders (default: ./KILL)"
    )
    demo.add_argument("--poll-interval", type=float, default=1.0, metavar="SECONDS", help="seconds between polls (default: 1.0)")
    demo.add_argument("--plumbing", action="store_true",
                      help="thin-demo-book test mode: relax spread/depth filters and bid inside the spread (tests the machinery, not the strategy)")
    demo.add_argument("--maker-fee-multiplier", default="0", help="fee multiplier for the SHADOW paper order only (default: 0)")
    demo.set_defaults(handler=_cmd_demo)

    probe = commands.add_parser(
        "demo-probe",
        help="diagnostic: place 1-contract $0.01 demo orders, try every way to read them back, print raw responses",
    )
    probe.add_argument("--series", help="override series_ticker from config.yaml")
    probe.set_defaults(handler=_cmd_demo_probe)

    allocate = commands.add_parser(
        "demo-allocate",
        help="one-time: move DEMO collateral onto the exchange shard BTC trades on (fixes insufficient_shard_balance)",
    )
    allocate.add_argument("--series", help="override series_ticker from config.yaml")
    allocate.add_argument("--percent", type=int, default=100, help="percent of the balance to put on the market's shard (default: 100)")
    allocate.set_defaults(handler=_cmd_demo_allocate)

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

    demo_check = commands.add_parser(
        "demo-check",
        help="place and cancel real (fake-money) orders against Kalshi's demo environment (Phase 6)",
    )
    demo_check.add_argument("--series", help="override series_ticker from config.yaml")
    demo_check.add_argument(
        "--wait-for-settlement", action="store_true",
        help="also check settlement, but only if the current window is already closed (default: skip that row)",
    )
    demo_check.set_defaults(handler=_cmd_demo_check)

    dashboard = commands.add_parser(
        "dashboard", help="local web UI: backtests, live paper PnL/trades, and local Kalshi settings"
    )
    dashboard.add_argument("--port", type=int, default=8765, help="localhost port to bind (default: 8765)")
    dashboard.add_argument("--data-dir", default="data", help="directory of *.sqlite databases to browse (default: ./data)")
    dashboard.add_argument("--env-file", default=".env", help="local settings file the Settings tab edits (default: ./.env)")
    dashboard.set_defaults(handler=_cmd_dashboard)
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
    if args.command == "stream":
        for name, value in (
            ("--hours", args.hours), ("--poll-interval", args.poll_interval), ("--status-every", args.status_every),
        ):
            if not math.isfinite(value) or value <= 0:
                print(f"error: {name} must be finite and greater than 0", file=sys.stderr)
                return 2
    if args.command == "lab":
        if not _is_non_negative_decimal(args.maker_fee_multiplier):
            print("error: --maker-fee-multiplier must be a non-negative number", file=sys.stderr)
            return 2
        for name, value in (("--account", args.account), ("--exposure-pct", args.exposure_pct), ("--daily-loss-pct", args.daily_loss_pct)):
            if not _is_non_negative_decimal(value) or Decimal(value) == 0:
                print(f"error: {name} must be a positive number", file=sys.stderr)
                return 2
        if not (0.2 <= args.split <= 0.9):
            print("error: --split must be between 0.2 and 0.9", file=sys.stderr)
            return 2
        if args.top < 1 or args.min_train_trades < 1 or args.max_combos < 1:
            print("error: --top, --min-train-trades and --max-combos must be at least 1", file=sys.stderr)
            return 2
    if args.command == "ml-train":
        if not (0.2 <= args.split <= 0.9):
            print("error: --split must be between 0.2 and 0.9", file=sys.stderr)
            return 2
        if sum(1 for source in (args.db, args.features, args.history) if source) != 1:
            print("error: give exactly one of --db / --features / --history", file=sys.stderr)
            return 2
        if args.db and args.which is None:
            print("error: --which is required with --db", file=sys.stderr)
            return 2
    if args.command == "ml-ablation":
        if args.entry_model is None and args.exit_model is None:
            print("error: give at least one of --entry-model / --exit-model", file=sys.stderr)
            return 2
        if not _is_non_negative_decimal(args.maker_fee_multiplier):
            print("error: --maker-fee-multiplier must be a non-negative number", file=sys.stderr)
            return 2
        for name, value in (("--account", args.account), ("--exposure-pct", args.exposure_pct), ("--daily-loss-pct", args.daily_loss_pct)):
            if not _is_non_negative_decimal(value) or Decimal(value) == 0:
                print(f"error: {name} must be a positive number", file=sys.stderr)
                return 2
        if not (0.2 <= args.split <= 0.9):
            print("error: --split must be between 0.2 and 0.9", file=sys.stderr)
            return 2
    if args.command == "demo-allocate" and not 1 <= args.percent <= 100:
        print("error: --percent must be between 1 and 100", file=sys.stderr)
        return 2
    if args.command == "demo":
        if not math.isfinite(args.hours) or args.hours <= 0 or not math.isfinite(args.poll_interval) or args.poll_interval <= 0:
            print("error: --hours and --poll-interval must be finite and greater than 0", file=sys.stderr)
            return 2
        if not _is_non_negative_decimal(args.maker_fee_multiplier):
            print("error: --maker-fee-multiplier must be a non-negative number", file=sys.stderr)
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
    if args.command == "dashboard" and not (0 <= args.port <= 65535):
        print("error: --port must be between 0 and 65535", file=sys.stderr)
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
