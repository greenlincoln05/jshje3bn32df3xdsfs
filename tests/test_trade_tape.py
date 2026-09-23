import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import httpx
import pytest
from test_client import make_client
from test_recorder import FakeKalshiSource, make_market, make_orderbook, make_recorder, rows

from btcbot.cli import main
from btcbot.fillcheck import FillCheckError, check_fills
from btcbot.models import ParseError, Trade

T = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)


def payload(trade_id="a", taker="no", yes="0.30", no="0.70", count="5.00", ts="2026-09-20T12:00:05.500000Z", ticker="KXBTC15M-26SEP200015-15"):
    return {"ticker": ticker, "trade_id": trade_id, "count_fp": count, "yes_price_dollars": yes, "no_price_dollars": no,
            "taker_side": taker, "created_time": ts, "taker_book_side": "bid", "taker_outcome_side": taker, "is_block_trade": False}


class TestTradeModel:
    def test_parses_a_live_shaped_trade(self):
        t = Trade.from_api(payload())
        assert (t.count, t.yes_price, t.no_price, t.taker_side) == (D("5.00"), D("0.30"), D("0.70"), "no")

    @pytest.mark.parametrize("bad", [{"taker_side": "maybe"}, {"count_fp": None}, {"created_time": "not a time"}])
    def test_bad_fields_are_parse_errors_not_silent_defaults(self, bad):
        with pytest.raises(ParseError):
            Trade.from_api({**payload(), **bad})

    # taker_side is marked deprecated by Kalshi (read 2026-09-23) in favor of taker_outcome_side. Both fields
    # are still sent today; Trade.from_api must handle all four combinations without ever silently guessing.
    def test_only_taker_outcome_side_present_is_used(self):
        p = payload(taker="yes")
        del p["taker_side"]
        assert Trade.from_api(p).taker_side == "yes"

    def test_only_taker_side_present_is_the_fallback(self):
        p = payload(taker="no")
        del p["taker_outcome_side"]
        assert Trade.from_api(p).taker_side == "no"

    def test_both_present_and_agreeing_is_fine(self):
        p = payload(taker="yes")
        assert p["taker_side"] == p["taker_outcome_side"] == "yes"
        assert Trade.from_api(p).taker_side == "yes"

    def test_both_present_and_disagreeing_is_a_parse_error(self):
        p = payload(taker="yes")
        p["taker_side"] = "no"
        with pytest.raises(ParseError, match="disagree"):
            Trade.from_api(p)

    def test_neither_field_present_is_a_parse_error(self):
        p = payload()
        del p["taker_side"]
        del p["taker_outcome_side"]
        with pytest.raises(ParseError):
            Trade.from_api(p)

    def test_a_missing_field_is_an_error(self):
        p = payload()
        del p["trade_id"]
        with pytest.raises(ParseError):
            Trade.from_api(p)


class TestClient:
    async def test_pages_are_followed_and_returned_oldest_first(self):
        pages = [{"trades": [payload("b", ts="2026-09-20T12:00:09Z")], "cursor": "next"},
                 {"trades": [payload("a", ts="2026-09-20T12:00:01Z")], "cursor": ""}]
        seen = []

        def handler(request):
            seen.append(dict(request.url.params))
            return httpx.Response(200, json=pages[len(seen) - 1])

        client, _ = make_client(handler)
        async with client:
            trades = await client.get_trades("KXBTC15M-26SEP200015-15", min_ts=T)
        assert [t.trade_id for t in trades] == ["a", "b"]
        assert seen[0]["ticker"] == "KXBTC15M-26SEP200015-15" and seen[0]["min_ts"] == str(int(T.timestamp()))
        assert seen[1]["cursor"] == "next"


class TapeSource(FakeKalshiSource):
    def __init__(self, *args, tape=(), **kw):
        super().__init__(*args, **kw)
        self.tape = list(tape)
        self.trade_calls = []

    async def get_trades(self, ticker, *, min_ts=None):
        self.trade_calls.append(min_ts)
        return [t for t in self.tape if t.ticker == ticker]


class TestRecorderTape:
    async def test_trades_are_stored_once_even_when_polled_repeatedly(self, tmp_path):
        active = make_market("KXBTC15M-26SEP190015-15", status="active")
        tape = [Trade.from_api(payload("t1", ticker=active.ticker)), Trade.from_api(payload("t2", ticker=active.ticker, taker="yes"))]
        client = TapeSource(markets=[[active]], orderbooks=[make_orderbook(active.ticker)], tape=tape)
        recorder, _ = make_recorder(tmp_path, client, tape_poll_interval_sec=0.5)
        try:
            await recorder.run(duration_sec=4.0)
        finally:
            recorder.close()
        stored = rows(tmp_path / "recorder.sqlite", "trade_tape")
        assert sorted((r["taker_side"], r["prints"], r["contracts"]) for r in stored) == [("no", 1, 5.0), ("yes", 1, 5.0)]
        assert len(client.trade_calls) >= 2                              # it polled repeatedly: each print counted once

    async def test_prints_in_the_same_second_at_the_same_price_are_summed(self, tmp_path):
        active = make_market("KXBTC15M-26SEP190015-15", status="active")
        tape = [Trade.from_api(payload(f"t{i}", ticker=active.ticker, count="2.00")) for i in range(3)]
        client = TapeSource(markets=[[active]], orderbooks=[make_orderbook(active.ticker)], tape=tape)
        recorder, _ = make_recorder(tmp_path, client, tape_poll_interval_sec=0.5)
        try:
            await recorder.run(duration_sec=3.0)
        finally:
            recorder.close()
        stored = rows(tmp_path / "recorder.sqlite", "trade_tape")
        assert len(stored) == 1 and stored[0]["prints"] == 3 and stored[0]["contracts"] == 6.0

    async def test_a_failing_tape_never_stops_the_order_book_recording(self, tmp_path):
        from btcbot.kalshi_client import KalshiAPIError
        active = make_market("KXBTC15M-26SEP190015-15", status="active")

        class Broken(TapeSource):
            async def get_trades(self, ticker, *, min_ts=None):
                raise KalshiAPIError(500, "tape down")

        client = Broken(markets=[[active]], orderbooks=[make_orderbook(active.ticker)])
        recorder, _ = make_recorder(tmp_path, client, tape_poll_interval_sec=0.5, max_consecutive_failures=2)
        try:
            summary = await recorder.run(duration_sec=4.0)
        finally:
            recorder.close()
        assert summary.orderbook_polls >= 2 and summary.stop_reason == "time_limit"


def make_fill_db(tmp_path, tape):
    path = tmp_path / "paper.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, ticker TEXT, side TEXT, size TEXT, entry_price TEXT, entry_ts TEXT,"
                 " fee_paid TEXT, p_side_at_entry REAL, result TEXT, pnl_usd TEXT, exit_reason TEXT, exit_price TEXT)")
    conn.execute("CREATE TABLE trade_tape (ticker TEXT, second_ts TEXT, yes_price TEXT, no_price TEXT, taker_side TEXT,"
                 " contracts REAL, prints INTEGER, PRIMARY KEY (ticker, second_ts, yes_price, taker_side))")
    conn.execute("INSERT INTO trades (ticker, side, size, entry_price, entry_ts, fee_paid, p_side_at_entry) VALUES"
                 " ('W','yes','5','0.30','2026-09-20T12:00:10+00:00','0',0.5)")
    for i, (taker, yes, no, count, ts) in enumerate(tape):
        conn.execute("INSERT INTO trade_tape VALUES (?,?,?,?,?,?,?)", ("W", ts, yes, no, taker, float(count), 1))
    conn.commit(); conn.close()
    return path


class TestFillCheck:
    def test_a_taker_no_print_at_or_through_our_yes_bid_supports_the_fill(self, tmp_path):
        db = make_fill_db(tmp_path, [("no", "0.29", "0.71", "6.00", "2026-09-20T12:00:08+00:00")])
        v = check_fills(db)[0]
        assert v.supported and v.covered and v.prints == 1

    def test_a_print_above_our_price_or_the_wrong_taker_side_does_not(self, tmp_path):
        db = make_fill_db(tmp_path, [("no", "0.35", "0.65", "9.00", "2026-09-20T12:00:08+00:00"),
                                     ("yes", "0.29", "0.71", "9.00", "2026-09-20T12:00:08+00:00")])
        assert check_fills(db)[0].supported is False

    def test_too_little_volume_is_supported_but_not_covered(self, tmp_path):
        db = make_fill_db(tmp_path, [("no", "0.30", "0.70", "2.00", "2026-09-20T12:00:08+00:00")])
        v = check_fills(db)[0]
        assert v.supported and not v.covered

    def test_runs_against_a_tape_written_by_the_historical_backfill_pipeline(self, tmp_path):
        """A2/A3's `replace_ticker_tape` (the backfill's writer) must produce a table fillcheck can read with
        NO changes -- it's the same schema/writer path the live recorder's upsert_trades uses, just a
        different write pattern. Builds the tape via the real backfill function, not hand-written SQL."""
        from btcbot.trade_tape import init_trade_tape_schema, replace_ticker_tape

        path = tmp_path / "backfilled.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, ticker TEXT, side TEXT, size TEXT, entry_price TEXT, entry_ts TEXT,"
                     " fee_paid TEXT, p_side_at_entry REAL, result TEXT, pnl_usd TEXT, exit_reason TEXT, exit_price TEXT)")
        conn.execute("INSERT INTO trades (ticker, side, size, entry_price, entry_ts, fee_paid, p_side_at_entry) VALUES"
                     " ('W','yes','5','0.30','2026-09-20T12:00:10+00:00','0',0.5)")
        conn.commit()
        init_trade_tape_schema(conn)
        replace_ticker_tape(conn, "W", [
            Trade(ticker="W", trade_id="t1", count=D("6.00"), yes_price=D("0.29"), no_price=D("0.71"),
                 taker_side="no", created_time=datetime.fromisoformat("2026-09-20T12:00:08+00:00")),
        ])
        conn.close()

        v = check_fills(path)[0]
        assert v.supported and v.covered

    def test_a_recording_without_a_tape_is_an_error_and_the_cli_reports_it(self, tmp_path, capsys):
        path = tmp_path / "old.sqlite"
        sqlite3.connect(path).close()
        with pytest.raises(FillCheckError):
            check_fills(path)
        assert main(["fillcheck", "--db", str(path)]) == 1
        assert "no trade_tape" in capsys.readouterr().err

    def test_the_cli_prints_a_summary(self, tmp_path, capsys):
        db = make_fill_db(tmp_path, [("no", "0.29", "0.71", "6.00", "2026-09-20T12:00:08+00:00")])
        assert main(["fillcheck", "--db", str(db)]) == 0
        assert "1/1" in capsys.readouterr().out
