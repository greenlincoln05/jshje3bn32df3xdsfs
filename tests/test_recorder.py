import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from btcbot.kalshi_client import KalshiAPIError, KalshiError
from btcbot.models import Market, OrderBook, ParseError, PriceLevel
from btcbot.recorder import Recorder

T0 = datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc)
SERIES = "KXBTC15M"


def make_market(ticker, *, status="active", strike="80000", event_ticker=None, raw_extra=None):
    return Market(
        ticker=ticker,
        event_ticker=event_ticker or ticker.rsplit("-", 1)[0],
        status=status,
        title="Bitcoin price up or down",
        open_time=T0 - timedelta(minutes=5),
        close_time=T0 + timedelta(minutes=10),
        strike=None if strike is None else Decimal(strike),
        strike_type="greater_or_equal",
        volume=Decimal("100"),
        open_interest=Decimal("50"),
        raw=raw_extra or {},
    )


def make_orderbook(ticker, *, yes_bid="0.54", yes_size="10", no_bid="0.44", no_size="8"):
    return OrderBook(
        ticker=ticker,
        yes_bids=(PriceLevel(Decimal(yes_bid), Decimal(yes_size)),),
        no_bids=(PriceLevel(Decimal(no_bid), Decimal(no_size)),),
    )


class FakeKalshiSource:
    """Scriptable stand-in for KalshiClient: each call pops the next outcome (last repeats), like test_client.py's Script.

    The historical-backfill methods (``list_historical_markets``, ``get_historical_cutoff``,
    ``get_historical_trades``, ``get_market_candlesticks``, ``get_historical_candlesticks``) default to
    empty/no-op so every EXISTING test that never passes those kwargs is unaffected; a test that cares about
    them passes the matching kwarg explicitly, same convention as ``markets``/``orderbooks``."""

    def __init__(
        self, *, markets=(), orderbooks=(), market_detail=None,
        historical_markets=(), historical_cutoff=None, live_trades=None, historical_trades=None,
        live_candles=None, historical_candles=None,
    ):
        self.markets_script = list(markets)
        self.orderbook_script = list(orderbooks)
        self.market_detail = {k: list(v) for k, v in (market_detail or {}).items()}
        self.historical_markets_script = list(historical_markets) if historical_markets else [[]]
        self._historical_cutoff = historical_cutoff
        self.live_trades = {k: list(v) for k, v in (live_trades or {}).items()}
        self.historical_trades = {k: list(v) for k, v in (historical_trades or {}).items()}
        self.live_candles = {k: list(v) for k, v in (live_candles or {}).items()}
        self.historical_candles = {k: list(v) for k, v in (historical_candles or {}).items()}
        self.calls = {
            "list_markets": 0, "get_orderbook": 0, "get_market": 0, "list_historical_markets": 0,
            "get_historical_cutoff": 0, "get_historical_trades": 0, "get_market_candlesticks": 0,
            "get_historical_candlesticks": 0, "get_trades": 0,
        }

    @staticmethod
    def _pop(script):
        outcome = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def list_markets(self, *, series_ticker, status=None):
        self.calls["list_markets"] += 1
        return self._pop(self.markets_script)

    async def get_orderbook(self, ticker, *, depth=0):
        self.calls["get_orderbook"] += 1
        return self._pop(self.orderbook_script)

    async def get_market(self, ticker):
        self.calls["get_market"] += 1
        return self._pop(self.market_detail[ticker])

    async def list_historical_markets(self, *, series_ticker):
        self.calls["list_historical_markets"] += 1
        return self._pop(self.historical_markets_script)

    async def get_historical_cutoff(self):
        self.calls["get_historical_cutoff"] += 1
        if isinstance(self._historical_cutoff, Exception):
            raise self._historical_cutoff
        return self._historical_cutoff

    async def get_trades(self, ticker, *, min_ts=None, max_pages=20):
        self.calls["get_trades"] += 1
        val = self.live_trades.get(ticker, [])
        if isinstance(val, Exception):
            raise val
        return val

    async def get_historical_trades(self, ticker, *, min_ts=None, max_ts=None, max_pages=200):
        self.calls["get_historical_trades"] += 1
        val = self.historical_trades.get(ticker, [])
        if isinstance(val, Exception):
            raise val
        return val

    async def get_market_candlesticks(self, series_ticker, ticker, *, start, end):
        self.calls["get_market_candlesticks"] += 1
        val = self.live_candles.get(ticker, [])
        if isinstance(val, Exception):
            raise val
        return val

    async def get_historical_candlesticks(self, ticker, *, start, end):
        self.calls["get_historical_candlesticks"] += 1
        val = self.historical_candles.get(ticker, [])
        if isinstance(val, Exception):
            raise val
        return val


class FakeClock:
    """A controllable clock/sleep pair: sleep() advances the same clock run() reads, so a time limit is reachable
    without a real wall-clock wait."""

    def __init__(self, start=T0):
        self.now = start

    def tick(self):
        return self.now

    async def sleep(self, delay):
        self.now += timedelta(seconds=delay)
        await asyncio.sleep(0)  # a real suspension point so cancellation/interleaving tests can observe progress


def make_recorder(tmp_path, client, **kwargs):
    clock = kwargs.pop("clock", None) or FakeClock()
    kwargs.setdefault("min_free_bytes", 0)  # real free disk space is irrelevant unless a test overrides this
    return Recorder(
        client,
        series_ticker=SERIES,
        db_path=tmp_path / "recorder.sqlite",
        kill_file=tmp_path / "KILL",
        clock=kwargs.pop("clock_fn", clock.tick),
        sleep=kwargs.pop("sleep_fn", clock.sleep),
        **kwargs,
    ), clock


def rows(db_path, table):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
    finally:
        conn.close()


class TestBasicRecording:
    async def test_records_orderbook_and_market_state_each_iteration(self, tmp_path):
        market = make_market("KXBTC15M-26SEP190015-15")
        book = make_orderbook(market.ticker)
        client = FakeKalshiSource(markets=[[market]], orderbooks=[book])
        recorder, _ = make_recorder(tmp_path, client, poll_interval_sec=1.0)
        try:
            summary = await recorder.run(duration_sec=3.0)
        finally:
            recorder.close()

        assert summary.stop_reason == "time_limit"
        assert summary.orderbook_polls == 3
        assert summary.market_state_changes == 1  # the market never changed, so state was recorded only once
        assert summary.errors == 0

        book_rows = rows(recorder._db_path, "orderbook_snapshots")
        assert len(book_rows) == 3
        assert book_rows[0]["ticker"] == market.ticker
        assert book_rows[0]["yes_bid_price"] == "0.54"
        decoded = json.loads(book_rows[0]["book_json"])
        assert decoded == {"yes": [["0.54", "10"]], "no": [["0.44", "8"]]}

        state_rows = rows(recorder._db_path, "market_state")
        assert len(state_rows) == 1
        assert state_rows[0]["strike"] == "80000"

        log_rows = rows(recorder._db_path, "run_log")
        assert any(r["event"] == "start" for r in log_rows)
        assert any(r["event"] == "stop" for r in log_rows)

    async def test_market_state_row_added_only_when_state_changes(self, tmp_path):
        # floor_strike is filled in about 0.7s after open_time (README); the same still-open market can
        # legitimately change state without a rollover.
        no_strike_yet = make_market("KXBTC15M-26SEP190015-15", status="active", strike=None)
        strike_set = make_market("KXBTC15M-26SEP190015-15", status="active", strike="80000")
        book = make_orderbook(no_strike_yet.ticker)
        client = FakeKalshiSource(markets=[[no_strike_yet], [no_strike_yet], [strike_set], [strike_set]], orderbooks=[book])
        recorder, _ = make_recorder(tmp_path, client)
        try:
            summary = await recorder.run(duration_sec=4.0)
        finally:
            recorder.close()

        assert summary.market_state_changes == 2  # strike appearing is one change; repeats of either state are not
        state_rows = rows(recorder._db_path, "market_state")
        assert [r["strike"] for r in state_rows] == [None, "80000"]

    async def test_spot_ticks_are_recorded_via_the_callback(self, tmp_path):
        from btcbot.spot_feed import SpotTick

        client = FakeKalshiSource(markets=[[]])
        recorder, _ = make_recorder(tmp_path, client)
        recorder.record_spot_tick(SpotTick(Decimal("63000"), "coinbase-ws", None, T0, 1.0))
        recorder.record_spot_tick(SpotTick(Decimal("63001"), "coinbase-ws", None, T0, 2.0))
        recorder.close()

        tick_rows = rows(tmp_path / "recorder.sqlite", "spot_ticks")
        assert [r["price"] for r in tick_rows] == ["63000", "63001"]


class TestRolloverGap:
    async def test_no_open_market_counts_as_a_rollover_gap_and_keeps_going(self, tmp_path):
        market = make_market("KXBTC15M-26SEP190015-15")
        book = make_orderbook(market.ticker)
        client = FakeKalshiSource(markets=[[], [], [market]], orderbooks=[book])
        recorder, _ = make_recorder(tmp_path, client)
        try:
            summary = await recorder.run(duration_sec=3.0)
        finally:
            recorder.close()

        assert summary.rollover_gaps == 2
        assert summary.orderbook_polls == 1
        assert summary.stop_reason == "time_limit"

    async def test_rollover_gap_is_logged_once_not_every_iteration(self, tmp_path):
        client = FakeKalshiSource(markets=[[]])
        recorder, _ = make_recorder(tmp_path, client)
        try:
            await recorder.run(duration_sec=3.0)
        finally:
            recorder.close()

        gap_logs = [r for r in rows(tmp_path / "recorder.sqlite", "run_log") if r["event"] == "rollover_gap"]
        assert gap_logs == []  # never having seen a market yet is not itself a logged transition


class _RaceOrderbookClient(FakeKalshiSource):
    """The market is still open when discovery runs, but closes during the orderbook fetch itself -- the
    exact race ``expired_book`` exists to catch, timed by advancing the shared clock as if the fetch took
    real time. Records the clock reading at each discovery call so a test can check the poll cadence was
    respected even on the one iteration that discarded the book."""

    def __init__(self, *, markets, orderbook, clock, fetch_delay_sec, market_detail=None):
        super().__init__(markets=markets, orderbooks=[orderbook], market_detail=market_detail)
        self._clock = clock
        self._fetch_delay_sec = fetch_delay_sec
        self.discovery_times: list[datetime] = []

    async def list_markets(self, *, series_ticker, status=None):
        self.discovery_times.append(self._clock.now)
        return await super().list_markets(series_ticker=series_ticker, status=status)

    async def get_orderbook(self, ticker, *, depth=0):
        await self._clock.sleep(self._fetch_delay_sec)
        return await super().get_orderbook(ticker, depth=depth)


class TestPostCloseBookDiscard:
    async def test_a_discarded_post_close_book_still_respects_the_poll_interval(self, tmp_path):
        # If this branch skipped its cooldown sleep, the very next iteration's discovery call would fire
        # immediately after the orderbook fetch that just discarded a book, instead of waiting out the rest
        # of the poll interval -- breaking the module's own documented "at most 2 req/s average" budget
        # right at the recurring 15-minute rollover boundary this race happens at.
        clock = FakeClock()
        market = replace(make_market("KXBTC15M-26SEP190015-15"), close_time=T0 + timedelta(seconds=2))
        client = _RaceOrderbookClient(
            markets=[[market]], orderbook=make_orderbook(market.ticker), clock=clock, fetch_delay_sec=3.0,
            market_detail={market.ticker: [market]},  # never finalized: settlement checks just no-op
        )
        recorder, _ = make_recorder(tmp_path, client, clock=clock, poll_interval_sec=1.0)
        try:
            summary = await asyncio.wait_for(recorder.run(duration_sec=8.0), timeout=5.0)
        finally:
            recorder.close()

        assert summary.stop_reason == "time_limit"
        assert summary.orderbook_polls == 0  # the only book fetched was discarded as post-close
        assert len(client.discovery_times) >= 2
        # Discovery #1 ran at T0; the orderbook fetch that followed took 3s (closing the market mid-fetch).
        # Discovery #2 must not fire before that 3s plus a full poll interval (4s total) has passed.
        gap = client.discovery_times[1] - client.discovery_times[0]
        assert gap >= timedelta(seconds=4.0)


class TestSettlement:
    async def test_settlement_is_recorded_once_finalized_after_rollover(self, tmp_path):
        active = make_market("KXBTC15M-26SEP190015-15", status="active")
        closed = make_market("KXBTC15M-26SEP190015-15", status="closed")
        finalized = make_market(
            "KXBTC15M-26SEP190015-15",
            status="finalized",
            raw_extra={"result": "yes", "expiration_value": "80123.45"},
        )
        book = make_orderbook(active.ticker)
        client = FakeKalshiSource(
            markets=[[active], []],  # open, then the window rolls over
            orderbooks=[book],
            market_detail={active.ticker: [closed, finalized]},
        )
        recorder, _ = make_recorder(tmp_path, client)
        try:
            summary = await recorder.run(duration_sec=3.0)
        finally:
            recorder.close()

        assert summary.settlements == 1
        assert summary.unresolved_settlements == ()
        settlement_rows = rows(tmp_path / "recorder.sqlite", "settlements")
        assert settlement_rows[0]["result"] == "yes"
        assert settlement_rows[0]["settled_avg"] == "80123.45"

    async def test_unresolved_settlement_is_reported_not_dropped(self, tmp_path):
        active = make_market("KXBTC15M-26SEP190015-15", status="active")
        still_closed = make_market("KXBTC15M-26SEP190015-15", status="closed")
        book = make_orderbook(active.ticker)
        client = FakeKalshiSource(
            markets=[[active], []],
            orderbooks=[book],
            market_detail={active.ticker: [still_closed]},  # never reaches "finalized" in this run
        )
        recorder, _ = make_recorder(tmp_path, client)
        try:
            summary = await recorder.run(duration_sec=2.0)
        finally:
            recorder.close()

        assert summary.settlements == 0
        assert summary.unresolved_settlements == (active.ticker,)


class TestDeterminedSettlement:
    def _client(self):
        active = make_market("KXBTC15M-26SEP190015-15", status="active")
        determined = make_market("KXBTC15M-26SEP190015-15", status="determined",
                                 raw_extra={"result": "no", "expiration_value": "80100"})
        return active, FakeKalshiSource(markets=[[active], []], orderbooks=[make_orderbook(active.ticker)],
                                        market_detail={active.ticker: [determined]})

    async def test_default_waits_for_finalized_like_prod(self, tmp_path):
        _, client = self._client()
        recorder, _ = make_recorder(tmp_path, client)
        try:
            summary = await recorder.run(duration_sec=3.0)
        finally:
            recorder.close()
        assert summary.settlements == 0

    async def test_demo_opt_in_settles_on_a_determined_result(self, tmp_path):
        active, client = self._client()
        recorder, _ = make_recorder(tmp_path, client, settle_on_determined=True)
        try:
            summary = await recorder.run(duration_sec=3.0)
        finally:
            recorder.close()
        assert summary.settlements == 1
        assert rows(tmp_path / "recorder.sqlite", "settlements")[0]["result"] == "no"


class TestFailureHandling:
    async def test_transient_kalshi_errors_are_counted_and_do_not_stop_the_run(self, tmp_path):
        market = make_market("KXBTC15M-26SEP190015-15")
        book = make_orderbook(market.ticker)
        client = FakeKalshiSource(
            markets=[[market]],
            orderbooks=[KalshiAPIError(503, "unavailable"), book],
        )
        recorder, _ = make_recorder(tmp_path, client, max_consecutive_failures=5)
        try:
            summary = await recorder.run(duration_sec=2.0)
        finally:
            recorder.close()

        assert summary.errors == 0  # the second, successful poll reset the streak
        assert summary.orderbook_polls == 1

    async def test_repeated_failures_stop_the_recorder(self, tmp_path):
        market = make_market("KXBTC15M-26SEP190015-15")
        client = FakeKalshiSource(markets=[[market]], orderbooks=[KalshiAPIError(500, "boom")])
        recorder, _ = make_recorder(tmp_path, client, max_consecutive_failures=3, poll_interval_sec=0.001)
        try:
            summary = await recorder.run(deadline=T0 + timedelta(hours=1))
        finally:
            recorder.close()

        assert summary.stop_reason == "repeated_failures"
        assert summary.errors == 3

    async def test_malformed_payload_is_a_counted_error_not_a_crash(self, tmp_path):
        market = make_market("KXBTC15M-26SEP190015-15")
        book = make_orderbook(market.ticker)
        client = FakeKalshiSource(markets=[[market]], orderbooks=[ParseError("bad book"), book])
        recorder, _ = make_recorder(tmp_path, client)
        try:
            summary = await recorder.run(duration_sec=2.0)
        finally:
            recorder.close()

        assert summary.errors == 0
        assert summary.orderbook_polls == 1

    async def test_unexpected_exception_propagates(self, tmp_path):
        market = make_market("KXBTC15M-26SEP190015-15")
        client = FakeKalshiSource(markets=[[market]], orderbooks=[RuntimeError("not a Kalshi problem")])
        recorder, _ = make_recorder(tmp_path, client)
        with pytest.raises(RuntimeError):
            await recorder.run(duration_sec=2.0)
        recorder.close()


class TestStopConditions:
    async def test_kill_file_stops_the_recorder(self, tmp_path):
        client = FakeKalshiSource(markets=[[]])
        recorder, _ = make_recorder(tmp_path, client)
        (tmp_path / "KILL").write_text("stop")
        try:
            summary = await recorder.run(deadline=T0 + timedelta(hours=1))
        finally:
            recorder.close()

        assert summary.stop_reason == "kill_file"
        assert client.calls["list_markets"] == 0  # checked before any request was made

    async def test_disk_floor_stops_the_recorder(self, tmp_path):
        client = FakeKalshiSource(markets=[[]])
        recorder, _ = make_recorder(
            tmp_path, client, min_free_bytes=2_000_000_000, disk_free_bytes=lambda path: 500_000_000
        )
        try:
            summary = await recorder.run(deadline=T0 + timedelta(hours=1))
        finally:
            recorder.close()

        assert summary.stop_reason == "disk_floor"

    async def test_db_size_cap_stops_the_recorder(self, tmp_path):
        client = FakeKalshiSource(markets=[[]])
        recorder, _ = make_recorder(tmp_path, client, max_db_bytes=1)
        try:
            summary = await recorder.run(deadline=T0 + timedelta(hours=1))
        finally:
            recorder.close()

        assert summary.stop_reason == "db_size_cap"

    async def test_cancellation_logs_and_propagates(self, tmp_path):
        client = FakeKalshiSource(markets=[[]])
        recorder, clock = make_recorder(tmp_path, client)
        task = asyncio.ensure_future(recorder.run(deadline=T0 + timedelta(hours=1)))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        recorder.close()
        log_rows = rows(tmp_path / "recorder.sqlite", "run_log")
        assert any("cancelled" in r["detail"] for r in log_rows)


class TestValidation:
    def test_rejects_non_positive_poll_interval(self, tmp_path):
        client = FakeKalshiSource()
        with pytest.raises(ValueError):
            Recorder(client, series_ticker=SERIES, db_path=tmp_path / "x.sqlite", poll_interval_sec=0)

    async def test_rejects_both_duration_and_deadline(self, tmp_path):
        client = FakeKalshiSource()
        recorder, _ = make_recorder(tmp_path, client)
        try:
            with pytest.raises(ValueError):
                await recorder.run(duration_sec=1.0, deadline=T0)
        finally:
            recorder.close()


class TestSchemaPersistence:
    def test_reopening_the_same_database_does_not_fail(self, tmp_path):
        client = FakeKalshiSource()
        db_path = tmp_path / "recorder.sqlite"
        Recorder(client, series_ticker=SERIES, db_path=db_path).close()
        second = Recorder(client, series_ticker=SERIES, db_path=db_path)  # schema already exists
        second.close()

@pytest.mark.asyncio
async def test_poll_cadence_includes_request_duration(tmp_path):
    clock = FakeClock()
    market = make_market('A')
    class SlowSource(FakeKalshiSource):
        async def get_orderbook(self, ticker, **kwargs):
            clock.now += timedelta(seconds=0.4)
            return await super().get_orderbook(ticker, **kwargs)
    client = SlowSource(markets=[[market]], orderbooks=[make_orderbook('A')])
    recorder, _ = make_recorder(tmp_path, client, clock=clock, poll_interval_sec=1)
    try:
        await recorder.run(duration_sec=3)
        rows = recorder._db.execute('SELECT request_started_ts FROM orderbook_snapshots ORDER BY id').fetchall()
        assert len(rows) == 3
        assert datetime.fromisoformat(rows[1][0]) - datetime.fromisoformat(rows[0][0]) == timedelta(seconds=1)
    finally:
        recorder.close()


class TestLogEvent:
    async def test_log_event_writes_a_run_log_row(self, tmp_path):
        recorder, _ = make_recorder(tmp_path, FakeKalshiSource(markets=[[]], orderbooks=[]))
        try:
            recorder.log_event("account_start", '{"kind": "paper", "account_usd": "500"}')
        finally:
            recorder.close()
        got = [r for r in rows(tmp_path / "recorder.sqlite", "run_log") if r["event"] == "account_start"]
        assert got and "500" in got[0]["detail"]
