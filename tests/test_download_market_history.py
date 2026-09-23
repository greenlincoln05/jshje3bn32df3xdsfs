"""CLI end-to-end tests for `btcbot download-market-history` (A4/A6) and `btcbot disagree --history` (A5),
driven by a fake multi-endpoint Kalshi API. Offline: httpx.MockTransport only, no network."""

import asyncio
import json
import sqlite3

import httpx
import pytest
from test_cli import install_mock_api

from btcbot import cli
from btcbot.history_pipeline import init_history_schema, is_market_done, save_backfill_progress

MARKET = {
    "ticker": "KXBTC15M-26SEP180000-00", "event_ticker": "KXBTC15M-26SEP180000",
    "status": "settled", "title": "t", "open_time": "2026-09-18T00:00:00Z",
    "close_time": "2026-09-18T00:15:00Z", "floor_strike": "80000", "result": "yes",
}
TRADE = {
    "trade_id": "t1", "ticker": MARKET["ticker"], "count_fp": "3.00", "yes_price_dollars": "0.5000",
    "no_price_dollars": "0.5000", "taker_outcome_side": "yes", "taker_book_side": "bid",
    "created_time": "2026-09-18T00:14:59Z", "is_block_trade": False,
}
CANDLE = {
    "end_period_ts": 1758154440, "open_interest": "1.00", "volume": "1.00",
    "price": {"close": "0.5", "high": "0.5", "low": "0.5", "mean": "0.5", "open": "0.5", "previous": None},
    "yes_bid": {"close": "0.40", "high": "0.5", "low": "0.4", "open": "0.5"},
    "yes_ask": {"close": "0.44", "high": "0.5", "low": "0.4", "open": "0.5"},
}
CUTOFF = {"market_settled_ts": "2020-01-01T00:00:00Z", "trades_created_ts": "2020-01-01T00:00:00Z"}  # everything is "live"


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/trade-api/v2/markets":
        return httpx.Response(200, json={"markets": [MARKET], "cursor": ""})
    if path == "/trade-api/v2/historical/markets":
        return httpx.Response(200, json={"markets": [], "cursor": ""})
    if path == "/trade-api/v2/historical/cutoff":
        return httpx.Response(200, json=CUTOFF)
    if path == "/trade-api/v2/markets/trades":
        return httpx.Response(200, json={"trades": [TRADE], "cursor": ""})
    if path.startswith("/trade-api/v2/series/") and path.endswith("/candlesticks"):
        return httpx.Response(200, json={"candlesticks": [CANDLE], "ticker": MARKET["ticker"]})
    return httpx.Response(404, json={"error": f"unhandled path {path}"})


class TestArgumentParsing:
    def test_no_trades_and_no_candles_together_still_marks_progress(self, monkeypatch, tmp_path, capsys):
        install_mock_api(monkeypatch, handler)
        code = cli.main([
            "download-market-history", "--env", "prod", "--data-dir", str(tmp_path),
            "--limit-markets", "1", "--no-trades", "--no-candles", "--sleep-ms", "0",
        ])
        assert code == 0
        db = list(tmp_path.glob("history-*.sqlite"))[0]
        conn = sqlite3.connect(db)
        # "done" is relative to what was actually requested: neither trades nor candles were asked for, so a
        # re-run with the same flags must skip it, but a later run that DOES want trades/candles must not.
        assert is_market_done(conn, MARKET["ticker"], need_trades=False, need_candles=False)
        assert not is_market_done(conn, MARKET["ticker"])
        assert conn.execute("SELECT COUNT(*) FROM trade_tape").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM market_candles").fetchone()[0] == 0

    def test_rejects_a_malformed_since(self, tmp_path):
        assert cli.main(["download-market-history", "--data-dir", str(tmp_path), "--since", "not-a-date"]) == 2


class TestEndToEnd:
    def test_full_run_writes_outcomes_trades_and_candles(self, monkeypatch, tmp_path, capsys):
        install_mock_api(monkeypatch, handler)
        code = cli.main([
            "download-market-history", "--env", "prod", "--data-dir", str(tmp_path),
            "--limit-markets", "1", "--sleep-ms", "0",
        ])
        assert code == 0
        out = capsys.readouterr().out
        assert "1 processed, 0 already done" in out
        assert "1 failed" not in out

        db = list(tmp_path.glob("history-*.sqlite"))[0]
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM market_outcomes").fetchone()[0] == 1
        assert conn.execute("SELECT SUM(contracts) FROM trade_tape").fetchone()[0] == pytest.approx(3.0)
        assert conn.execute("SELECT COUNT(*) FROM market_candles").fetchone()[0] == 1
        assert is_market_done(conn, MARKET["ticker"])

    def test_resuming_an_already_done_market_skips_it(self, monkeypatch, tmp_path, capsys):
        db_path = tmp_path / "history-KXBTC15M-prod-existing.sqlite"
        conn = sqlite3.connect(db_path)
        init_history_schema(conn)
        from btcbot.trade_tape import init_trade_tape_schema
        init_trade_tape_schema(conn)
        save_backfill_progress(conn, MARKET["ticker"], trades_done=True, candles_done=True, trade_count=99,
                               fetched_at=__import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc))
        conn.close()

        calls = {"trades": 0}
        def counting_handler(request):
            if request.url.path == "/trade-api/v2/markets/trades":
                calls["trades"] += 1
            return handler(request)

        install_mock_api(monkeypatch, counting_handler)
        code = cli.main([
            "download-market-history", "--env", "prod", "--db", str(db_path), "--limit-markets", "1", "--sleep-ms", "0",
        ])
        assert code == 0
        assert calls["trades"] == 0  # never re-fetched: backfill_progress said it was already done
        out = capsys.readouterr().out
        assert "0 processed, 1 already done" in out

    def test_a_failed_market_is_reported_and_not_marked_done(self, monkeypatch, tmp_path, capsys):
        def failing_handler(request):
            if request.url.path == "/trade-api/v2/markets/trades":
                return httpx.Response(500, json={"error": "boom"})
            return handler(request)

        install_mock_api(monkeypatch, failing_handler)
        code = cli.main([
            "download-market-history", "--env", "prod", "--data-dir", str(tmp_path),
            "--limit-markets", "1", "--sleep-ms", "0",
        ])
        assert code == 1
        err = capsys.readouterr().err
        assert "FAILED" in err
        db = list(tmp_path.glob("history-*.sqlite"))[0]
        conn = sqlite3.connect(db)
        assert not is_market_done(conn, MARKET["ticker"])

    def test_resume_after_a_no_candles_run_still_fetches_candles(self, monkeypatch, tmp_path, capsys):
        # First run backfills trades only; a later run without --no-candles must not skip this market just
        # because a backfill_progress row already exists for it (is_market_done regression coverage).
        install_mock_api(monkeypatch, handler)
        assert cli.main([
            "download-market-history", "--env", "prod", "--data-dir", str(tmp_path),
            "--limit-markets", "1", "--no-candles", "--sleep-ms", "0",
        ]) == 0
        db = list(tmp_path.glob("history-*.sqlite"))[0]
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM market_candles").fetchone()[0] == 0
        capsys.readouterr()

        code = cli.main([
            "download-market-history", "--env", "prod", "--db", str(db), "--limit-markets", "1", "--sleep-ms", "0",
        ])
        assert code == 0
        out = capsys.readouterr().out
        assert "1 processed, 0 already done" in out
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM market_candles").fetchone()[0] == 1
        assert is_market_done(conn, MARKET["ticker"])

    def test_a_locked_database_is_reported_as_a_clean_error_not_a_raw_traceback(self, monkeypatch, tmp_path):
        install_mock_api(monkeypatch, handler)

        def boom(*a, **kw):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(cli, "save_backfill_progress", boom)
        code = cli.main([
            "download-market-history", "--env", "prod", "--data-dir", str(tmp_path),
            "--limit-markets", "1", "--sleep-ms", "0",
        ])
        assert code == 1  # a sqlite3.Error inside do_one is counted as a failed market, not an uncaught crash

    def test_concurrency_actually_overlaps_requests_instead_of_running_serially(self, monkeypatch, tmp_path):
        # Regression coverage for the confirmed --concurrency no-op bug: the old sequential `for ... await`
        # loop could never have more than one request in flight, so this test would fail against it.
        markets = [{**MARKET, "ticker": f"T{i}", "close_time": f"2026-09-18T00:{15 + i}:00Z"} for i in range(4)]
        in_flight = {"current": 0, "max": 0}

        async def slow_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/trade-api/v2/markets":
                return httpx.Response(200, json={"markets": markets, "cursor": ""})
            if request.url.path == "/trade-api/v2/markets/trades":
                in_flight["current"] += 1
                in_flight["max"] = max(in_flight["max"], in_flight["current"])
                await asyncio.sleep(0.05)
                in_flight["current"] -= 1
                ticker = request.url.params["ticker"]
                return httpx.Response(200, json={"trades": [{**TRADE, "ticker": ticker}], "cursor": ""})
            return handler(request)

        install_mock_api(monkeypatch, slow_handler)
        code = cli.main([
            "download-market-history", "--env", "prod", "--data-dir", str(tmp_path),
            "--concurrency", "4", "--sleep-ms", "0",
        ])
        assert code == 0
        assert in_flight["max"] > 1

    def test_limit_markets_bounds_the_run(self, monkeypatch, tmp_path):
        two_markets = {**MARKET, "ticker": "T2", "close_time": "2026-09-18T00:30:00Z"}
        def two_handler(request):
            if request.url.path == "/trade-api/v2/markets":
                return httpx.Response(200, json={"markets": [MARKET, two_markets], "cursor": ""})
            return handler(request)

        install_mock_api(monkeypatch, two_handler)
        code = cli.main([
            "download-market-history", "--env", "prod", "--data-dir", str(tmp_path),
            "--limit-markets", "1", "--sleep-ms", "0",
        ])
        assert code == 0
        db = list(tmp_path.glob("history-*.sqlite"))[0]
        conn = sqlite3.connect(db)
        assert is_market_done(conn, MARKET["ticker"]) and not is_market_done(conn, "T2")


class TestDisagreeHistory:
    def test_runs_the_existing_report_unchanged_on_a_backfilled_db(self, monkeypatch, tmp_path, capsys):
        install_mock_api(monkeypatch, handler)
        assert cli.main([
            "download-market-history", "--env", "prod", "--data-dir", str(tmp_path),
            "--limit-markets", "1", "--sleep-ms", "0",
        ]) == 0
        db = list(tmp_path.glob("history-*.sqlite"))[0]
        capsys.readouterr()

        # No Coinbase spot_candles were backfilled in this test (that's download-history's job), so
        # market_level_examples has no history to price against and finds nothing -- confirm the command
        # still runs cleanly end-to-end rather than crashing, which is what this test is really checking.
        code = cli.main(["disagree", "--history", str(db), "--min-n", "1"])
        assert code == 0
        assert "windows usable" in capsys.readouterr().out

    def test_a_missing_database_is_a_clean_error(self, tmp_path, capsys):
        code = cli.main(["disagree", "--history", str(tmp_path / "nope.sqlite")])
        assert code == 1
        assert "error:" in capsys.readouterr().err
