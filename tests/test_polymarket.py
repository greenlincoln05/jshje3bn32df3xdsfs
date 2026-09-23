"""Offline tests for the Polymarket read-only client and recorder. Never touch the network; httpx.MockTransport
and a fake client stand in for the real APIs (same patterns as test_client.py / test_recorder.py)."""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from btcbot.models import ParseError
from btcbot.polymarket_client import (
    OrderBook,
    PolymarketAPIError,
    PolymarketClient,
    PolymarketConnectionError,
    UpdownEvent,
)
from btcbot.polymarket_recorder import PolymarketRecorder

T0 = datetime(2026, 9, 22, 20, 0, 0, tzinfo=timezone.utc)


def event_payload(slug="btc-updown-15m-1000", *, closed=False, outcome_prices=None, start=None, end=None):
    return {
        "slug": slug,
        "startDate": (start or T0).isoformat(),
        "endDate": (end or (T0 + timedelta(minutes=15))).isoformat(),
        "markets": [{
            "question": "Bitcoin Up or Down - test",
            "conditionId": "0xcond",
            "clobTokenIds": json.dumps(["up-token", "down-token"]),
            "outcomes": json.dumps(["Up", "Down"]),
            "closed": closed,
            "outcomePrices": json.dumps(outcome_prices) if outcome_prices else None,
        }],
    }


def book_payload(bids=(("0.4", "10"),), asks=(("0.6", "5"),)):
    return {
        "bids": [{"price": p, "size": s} for p, s in bids],
        "asks": [{"price": p, "size": s} for p, s in asks],
        "timestamp": "1790000000000",
    }


def make_client(handler):
    return PolymarketClient(transport=httpx.MockTransport(handler))


class TestUpdownEventParsing:
    def test_parses_the_live_event_shape(self):
        ev = UpdownEvent.from_api(event_payload())
        assert ev.up_token_id == "up-token" and ev.down_token_id == "down-token"
        assert ev.closed is False and ev.result_up is None

    def test_a_resolved_event_reports_the_winner(self):
        yes = UpdownEvent.from_api(event_payload(closed=True, outcome_prices=["1", "0"]))
        no = UpdownEvent.from_api(event_payload(closed=True, outcome_prices=["0", "1"]))
        assert yes.result_up is True and no.result_up is False

    def test_closed_but_not_yet_settled_is_none_not_a_guess(self):
        # e.g. still in an UMA dispute window: outcomePrices not yet a clean 1/0
        ev = UpdownEvent.from_api(event_payload(closed=True, outcome_prices=["0.5", "0.5"]))
        assert ev.result_up is None

    def test_missing_condition_id_is_a_parse_error(self):
        payload = event_payload()
        del payload["markets"][0]["conditionId"]
        with pytest.raises(ParseError):
            UpdownEvent.from_api(payload)

    def test_unexpected_outcome_order_is_rejected_not_silently_trusted(self):
        payload = event_payload()
        payload["markets"][0]["outcomes"] = json.dumps(["Down", "Up"])
        with pytest.raises(ParseError):
            UpdownEvent.from_api(payload)


class TestOrderBookParsing:
    def test_parses_and_sorts_levels(self):
        from decimal import Decimal

        book = OrderBook.from_api("tok", book_payload(bids=(("0.3", "1"), ("0.4", "2")), asks=(("0.6", "1"), ("0.5", "2"))))
        assert book.best_bid().price == Decimal("0.4")  # highest bid last -> best
        assert book.best_ask().price == Decimal("0.5")  # lowest ask first -> best

    def test_empty_book_has_no_best_levels(self):
        book = OrderBook.from_api("tok", {"bids": [], "asks": [], "timestamp": "1790000000000"})
        assert book.best_bid() is None and book.best_ask() is None

    def test_a_non_object_level_entry_is_a_parse_error(self):
        with pytest.raises(ParseError):
            OrderBook.from_api("tok", book_payload() | {"bids": [["0.4", "10"]]})

    def test_a_missing_or_malformed_timestamp_is_a_parse_error_not_a_silent_now(self):
        with pytest.raises(ParseError):
            OrderBook.from_api("tok", {"bids": [], "asks": []})  # no timestamp field at all
        with pytest.raises(ParseError):
            OrderBook.from_api("tok", book_payload() | {"timestamp": "not-a-number"})


class TestClient:
    async def test_list_recent_updown_events_filters_by_horizon_prefix(self):
        events = [event_payload("btc-updown-15m-1"), event_payload("btc-updown-5m-1"), event_payload("some-other-market")]

        def handler(request):
            assert request.url.path == "/events"
            return httpx.Response(200, json=events)

        async with make_client(handler) as client:
            got = await client.list_recent_updown_events(horizon="15m")
        assert [e.slug for e in got] == ["btc-updown-15m-1"]

    async def test_get_order_book_hits_the_clob_api(self):
        def handler(request):
            assert "clob.polymarket.com" in str(request.url)
            assert request.url.params["token_id"] == "up-token"
            return httpx.Response(200, json=book_payload())

        async with make_client(handler) as client:
            book = await client.get_order_book("up-token")
        assert book.token_id == "up-token" and book.bids

    async def test_http_error_is_a_clean_api_error(self):
        async with make_client(lambda r: httpx.Response(500, text="boom")) as client:
            with pytest.raises(PolymarketAPIError):
                await client.get_order_book("up-token")

    async def test_transport_failure_is_a_connection_error(self):
        def handler(request):
            raise httpx.ConnectTimeout("no route")

        async with make_client(handler) as client:
            with pytest.raises(PolymarketConnectionError):
                await client.get_order_book("up-token")

    def test_no_write_method_exists_anywhere_on_the_client(self):
        # There is no demo/paper environment on Polymarket to gate a write behind, so the only safe design is
        # for the capability to simply not exist -- confirm that by construction, not by convention.
        public_methods = {name for name in dir(PolymarketClient) if not name.startswith("_")}
        assert public_methods == {"list_recent_updown_events", "get_event", "get_order_book"}


class FakeClock:
    def __init__(self, start=T0):
        self.now = start

    def tick(self):
        return self.now

    async def sleep(self, delay):
        self.now += timedelta(seconds=delay)
        await asyncio.sleep(0)


class FakePolymarketClient:
    """Scriptable stand-in for PolymarketClient. Each of events_script/books_script pops its next scripted
    outcome (last one repeats); an Exception instance in a script is raised instead of returned, same
    convention as test_recorder.py's FakeKalshiSource."""

    def __init__(self, *, events_script=(), books=None, books_script=None, event_lookup=None):
        self.events_script = list(events_script)
        self.books = books or {}
        self.books_script = list(books_script) if books_script is not None else None
        self.event_lookup = event_lookup or {}

    @staticmethod
    def _pop(script):
        outcome = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def list_recent_updown_events(self, *, horizon, limit=20):
        return self._pop(self.events_script)

    async def get_order_book(self, token_id):
        if self.books_script is not None:
            return self._pop(self.books_script)
        return self.books[token_id]

    async def get_event(self, slug):
        return self.event_lookup[slug]


def make_recorder(tmp_path, client, **kwargs):
    clock = kwargs.pop("clock", None) or FakeClock()
    kwargs.setdefault("min_free_bytes", 0)
    return PolymarketRecorder(
        client, db_path=tmp_path / "pm.sqlite", kill_file=tmp_path / "KILL_PM",
        clock=kwargs.pop("clock_fn", clock.tick), sleep=kwargs.pop("sleep_fn", clock.sleep), **kwargs,
    ), clock


def rows(db_path, table):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]


def one_event(slug="btc-updown-15m-1000", **kw):
    return UpdownEvent.from_api(event_payload(slug, **kw))


class TestRecorder:
    async def test_records_market_state_and_both_outcome_books(self, tmp_path):
        ev = one_event()
        client = FakePolymarketClient(
            events_script=[[ev]],
            books={"up-token": OrderBook.from_api("up-token", book_payload()),
                  "down-token": OrderBook.from_api("down-token", book_payload())},
        )
        recorder, _ = make_recorder(tmp_path, client)
        try:
            summary = await recorder.run(duration_sec=1.0)
        finally:
            recorder.close()
        assert summary.book_polls >= 1 and summary.market_state_changes == 1
        assert len(rows(tmp_path / "pm.sqlite", "pm_market_state")) == 1
        book_rows = rows(tmp_path / "pm.sqlite", "pm_orderbook_snapshots")
        assert {r["outcome"] for r in book_rows} == {"up", "down"}

    async def test_settlement_is_recorded_once_the_event_closes(self, tmp_path):
        live = one_event("btc-updown-15m-1000")
        resolved = one_event("btc-updown-15m-1000", closed=True, outcome_prices=["1", "0"])
        client = FakePolymarketClient(
            events_script=[[live], []],  # live, then the window rolls over (no open event)
            books={"up-token": OrderBook.from_api("up-token", book_payload()),
                  "down-token": OrderBook.from_api("down-token", book_payload())},
            event_lookup={"btc-updown-15m-1000": resolved},
        )
        recorder, _ = make_recorder(tmp_path, client)
        try:
            summary = await recorder.run(duration_sec=3.0)
        finally:
            recorder.close()
        assert summary.settlements == 1
        settled = rows(tmp_path / "pm.sqlite", "pm_settlements")
        assert settled and settled[0]["result_up"] == 1

    async def test_unresolved_settlement_is_reported_not_dropped(self, tmp_path):
        live = one_event("btc-updown-15m-1000")
        still_open = one_event("btc-updown-15m-1000", closed=False)
        client = FakePolymarketClient(
            events_script=[[live], []],
            books={"up-token": OrderBook.from_api("up-token", book_payload()),
                  "down-token": OrderBook.from_api("down-token", book_payload())},
            event_lookup={"btc-updown-15m-1000": still_open},  # never reaches closed=True
        )
        recorder, _ = make_recorder(tmp_path, client)
        try:
            summary = await recorder.run(duration_sec=3.0)
        finally:
            recorder.close()
        assert summary.settlements == 0
        assert summary.unresolved_settlements == ("btc-updown-15m-1000",)

    async def test_kill_file_stops_the_run(self, tmp_path):
        (tmp_path / "KILL_PM").write_text("stop")
        client = FakePolymarketClient(events_script=[[]])
        recorder, _ = make_recorder(tmp_path, client)
        try:
            summary = await recorder.run(duration_sec=100.0)
        finally:
            recorder.close()
        assert summary.stop_reason == "kill_file"

    async def test_transient_book_errors_are_counted_and_do_not_stop_the_run(self, tmp_path):
        ev = one_event()
        good_book = OrderBook.from_api("tok", book_payload())
        # up and down are each one get_order_book call; the error consumes the first, then this repeats
        # forever (the shared _pop convention: the last scripted item repeats), so both calls succeed soon after.
        client = FakePolymarketClient(events_script=[[ev]], books_script=[PolymarketAPIError(503, "unavailable"), good_book])
        recorder, _ = make_recorder(tmp_path, client, max_consecutive_failures=5, poll_interval_sec=0.5)
        try:
            summary = await recorder.run(duration_sec=2.0)
        finally:
            recorder.close()
        assert summary.errors == 0  # a later successful poll reset the streak
        assert summary.book_polls >= 1

    async def test_repeated_failures_stop_the_recorder(self, tmp_path):
        client = FakePolymarketClient(events_script=[PolymarketAPIError(500, "boom")])
        recorder, _ = make_recorder(tmp_path, client, max_consecutive_failures=3, poll_interval_sec=0.001)
        try:
            summary = await recorder.run(duration_sec=3600.0)
        finally:
            recorder.close()
        assert summary.stop_reason == "repeated_failures"
        assert summary.errors == 3

    async def test_a_parse_error_is_a_counted_error_not_a_crash(self, tmp_path):
        ev = one_event()
        good_book = OrderBook.from_api("tok", book_payload())
        client = FakePolymarketClient(events_script=[[ev]], books_script=[ParseError("bad book"), good_book])
        recorder, _ = make_recorder(tmp_path, client, poll_interval_sec=0.5)
        try:
            summary = await recorder.run(duration_sec=2.0)
        finally:
            recorder.close()
        assert summary.errors == 0
        assert summary.book_polls >= 1

    async def test_an_unexpected_exception_propagates(self, tmp_path):
        client = FakePolymarketClient(events_script=[RuntimeError("not a Polymarket problem")])
        recorder, _ = make_recorder(tmp_path, client)
        with pytest.raises(RuntimeError):
            await recorder.run(duration_sec=2.0)
        recorder.close()

    async def test_disk_floor_stops_the_recorder(self, tmp_path):
        client = FakePolymarketClient(events_script=[[]])
        recorder, _ = make_recorder(tmp_path, client, min_free_bytes=2_000_000_000, disk_free_bytes=lambda path: 500_000_000)
        try:
            summary = await recorder.run(duration_sec=3600.0)
        finally:
            recorder.close()
        assert summary.stop_reason == "disk_floor"

    async def test_db_size_cap_stops_the_recorder(self, tmp_path):
        client = FakePolymarketClient(events_script=[[]])
        recorder, _ = make_recorder(tmp_path, client, max_db_bytes=1)
        try:
            summary = await recorder.run(duration_sec=3600.0)
        finally:
            recorder.close()
        assert summary.stop_reason == "db_size_cap"

    async def test_cancellation_logs_and_propagates(self, tmp_path):
        client = FakePolymarketClient(events_script=[[]])
        recorder, _ = make_recorder(tmp_path, client)
        task = asyncio.ensure_future(recorder.run(duration_sec=3600.0))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        recorder.close()
        log_rows = rows(tmp_path / "pm.sqlite", "run_log")
        assert any("cancelled" in r["detail"] for r in log_rows)

    def test_rejects_non_positive_poll_interval(self, tmp_path):
        client = FakePolymarketClient(events_script=[[]])
        with pytest.raises(ValueError):
            make_recorder(tmp_path, client, poll_interval_sec=0)

    async def test_a_write_capable_database_never_gets_kalshi_tables(self, tmp_path):
        """Every table this recorder creates must be pm_-prefixed (or run_log), so btcbot.features'
        Kalshi-schema check (which looks for orderbook_snapshots) can never mistake this for a Kalshi db."""
        client = FakePolymarketClient(events_script=[[]])
        recorder, _ = make_recorder(tmp_path, client)
        recorder.close()
        conn = sqlite3.connect(tmp_path / "pm.sqlite")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "orderbook_snapshots" not in tables
        assert tables <= {"pm_orderbook_snapshots", "pm_market_state", "pm_settlements", "run_log"}
