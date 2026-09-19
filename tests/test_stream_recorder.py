"""Offline tests for the WebSocket stream recorder: fake sockets and hand-written frames only, no network and
no credentials (the RSA key below is generated in-process and never leaves the test)."""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import websockets
import websockets.datastructures
import websockets.exceptions
import websockets.http11
from cryptography.hazmat.primitives.asymmetric import rsa

from btcbot.config import KalshiEnv
from btcbot.kalshi_client import KalshiAuth
from btcbot.recorder import Recorder
from btcbot.stream_recorder import (
    CH_BOOK,
    CH_BRTI,
    CH_BRTI_5HZ,
    WS_PATH,
    WS_URLS,
    BookDelta,
    BookSnapshot,
    BrtiTick,
    Control,
    LiveBook,
    StreamError,
    StreamParseError,
    StreamRecorder,
    Unknown,
    _Resync,
    _Session,
    compare_brti_to_spot,
    parse_message,
)

T0 = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)

BRTI_1HZ = {
    "type": "cfbenchmarks_value", "sid": 1, "seq": 42,
    "msg": {
        "index_id": "BRTI", "received_at": 1710000000123,
        "data": json.dumps({"type": "value", "id": "BRTI", "time": 1710000000000, "value": "68000.12"}),
        "avg_60s_data": {"value": "68000.12000000", "window_size": 3, "window_start_ts_ms": 1, "window_end_ts_exclusive": 2},
    },
}
BRTI_5HZ = {
    "type": "cfbenchmarks_value_5hz", "sid": 2, "seq": 7,
    "msg": {"index_id": "BRTI", "value_usd": "68001.50000000", "source_ts_ms": 1710000000323, "received_at": 1710000000341},
}
SNAPSHOT = {
    "type": "orderbook_snapshot", "sid": 3, "seq": 2,
    "msg": {"market_ticker": "KXBTC15M-A", "yes_dollars_fp": [["0.4000", "10.00"], ["0.4100", "5.00"]],
            "no_dollars_fp": [["0.5500", "20.00"]]},
}


def delta(seq, price="0.4100", change="-2.00", side="yes", ticker="KXBTC15M-A", sid=3):
    return {"type": "orderbook_delta", "sid": sid, "seq": seq,
            "msg": {"market_ticker": ticker, "price_dollars": price, "delta_fp": change, "side": side, "ts_ms": 1710000000000}}


def j(payload) -> str:
    return json.dumps(payload)


@pytest.fixture(scope="module")
def auth():
    return KalshiAuth("test-key-id-abcd", rsa.generate_private_key(public_exponent=65537, key_size=2048))


# --------------------------------------------------------------------------- parsing


class TestParse:
    def test_brti_one_hz(self):
        tick = parse_message(j(BRTI_1HZ), receive_ts=T0)
        assert isinstance(tick, BrtiTick)
        assert tick.channel == CH_BRTI and tick.index_id == "BRTI"
        assert tick.value == Decimal("68000.12") and tick.source_ts_ms == 1710000000000
        assert tick.received_at_ms == 1710000000123 and tick.avg60 == Decimal("68000.12000000")
        assert tick.avg60_window_size == 3

    def test_brti_five_hz(self):
        tick = parse_message(j(BRTI_5HZ), receive_ts=T0)
        assert isinstance(tick, BrtiTick) and tick.channel == CH_BRTI_5HZ
        assert tick.value == Decimal("68001.5") and tick.source_ts_ms == 1710000000323

    def test_snapshot_and_delta(self):
        snap = parse_message(j(SNAPSHOT), receive_ts=T0)
        assert isinstance(snap, BookSnapshot) and snap.ticker == "KXBTC15M-A"
        assert snap.yes == ((Decimal("0.4"), Decimal("10")), (Decimal("0.41"), Decimal("5")))
        d = parse_message(j(delta(3)), receive_ts=T0)
        assert isinstance(d, BookDelta) and d.side == "yes" and d.delta == Decimal("-2") and d.seq == 3

    def test_control_and_unknown(self):
        ctl = parse_message(j({"id": 5, "type": "subscribed", "msg": {"channel": CH_BOOK, "sid": 9}}), receive_ts=T0)
        assert isinstance(ctl, Control) and ctl.command_id == 5 and ctl.sid == 9 and ctl.channel == CH_BOOK
        assert isinstance(parse_message(j({"type": "something_new"}), receive_ts=T0), Unknown)

    @pytest.mark.parametrize("raw", [
        "not json", "[]", j({"type": "orderbook_delta", "msg": {"market_ticker": "X", "side": "up",
                                                                "price_dollars": "0.5", "delta_fp": "1"}}),
        j({"type": "orderbook_snapshot", "msg": {"market_ticker": "X", "yes_dollars_fp": [["1.5", "1"]]}}),
        j({"type": "cfbenchmarks_value", "msg": {"index_id": "BRTI"}}),
        j({"type": "cfbenchmarks_value", "msg": {"index_id": "BRTI", "value_usd": "abc"}}),
    ])
    def test_malformed_frames_raise(self, raw):
        with pytest.raises(StreamParseError):
            parse_message(raw, receive_ts=T0)


class TestLiveBook:
    def test_snapshot_then_deltas(self):
        book = LiveBook("KXBTC15M-A")
        book.apply_snapshot(parse_message(j(SNAPSHOT), receive_ts=T0))
        book.apply_delta(parse_message(j(delta(3, "0.4100", "-5.00")), receive_ts=T0))   # removes the level
        book.apply_delta(parse_message(j(delta(4, "0.4200", "7.00")), receive_ts=T0))    # adds one
        ob = book.to_orderbook()
        assert [(lv.price, lv.size) for lv in ob.yes_bids] == [(Decimal("0.4"), Decimal("10")), (Decimal("0.42"), Decimal("7"))]
        assert ob.best_bid("no").price == Decimal("0.55")

    def test_negative_level_demands_resync(self):
        book = LiveBook("KXBTC15M-A")
        book.apply_snapshot(parse_message(j(SNAPSHOT), receive_ts=T0))
        with pytest.raises(_Resync):
            book.apply_delta(parse_message(j(delta(3, "0.4100", "-99.00")), receive_ts=T0))

    def test_deltas_before_snapshot_are_ignored(self):
        book = LiveBook("KXBTC15M-A")
        book.apply_delta(parse_message(j(delta(3)), receive_ts=T0))
        assert not book.ready and not book.levels["yes"]


# --------------------------------------------------------------------------- handling and storage


def make_stream(tmp_path, auth, **kwargs):
    db_path = tmp_path / "stream.sqlite"
    Recorder(None, series_ticker="KXBTC15M", db_path=db_path).close()  # creates the base schema, as the CLI does

    async def provider():
        return None

    kwargs.setdefault("ticker_provider", provider)
    return StreamRecorder(db_path, auth, KalshiEnv.PROD, **kwargs), db_path


class TestHandle:
    def test_writes_brti_and_book_rows(self, tmp_path, auth):
        stream, db_path = make_stream(tmp_path, auth)
        session = _Session()
        for frame in (BRTI_1HZ, BRTI_5HZ, SNAPSHOT, delta(3), delta(4, "0.4000", "1.00")):
            stream.handle(j(frame), T0, session)
        stream.close()
        conn = sqlite3.connect(str(db_path))
        assert conn.execute("SELECT COUNT(*) FROM brti_ticks").fetchone()[0] == 2
        assert conn.execute("SELECT value FROM brti_ticks WHERE channel=?", (CH_BRTI,)).fetchone()[0] == "68000.12"
        kinds = [r[0] for r in conn.execute("SELECT kind FROM ws_book_events ORDER BY id")]
        assert kinds == ["snapshot", "delta", "delta"]
        assert stream.stats.brti_ticks == 2 and stream.stats.book_deltas == 2

    def test_seq_gap_forces_resync(self, tmp_path, auth):
        stream, _ = make_stream(tmp_path, auth)
        session = _Session()
        stream.handle(j(SNAPSHOT), T0, session)
        stream.handle(j(delta(3)), T0, session)
        with pytest.raises(_Resync):
            stream.handle(j(delta(5)), T0, session)  # seq 4 was missed

    def test_bad_and_unknown_frames_are_counted_not_fatal(self, tmp_path, auth):
        stream, db_path = make_stream(tmp_path, auth)
        session = _Session()
        stream.handle("garbage", T0, session)
        stream.handle(j({"type": "brand_new"}), T0, session)
        stream.handle(j({"type": "brand_new"}), T0, session)
        assert stream.stats.malformed_messages == 1 and stream.stats.unknown_messages == 2
        conn = sqlite3.connect(str(db_path))
        stream._db.commit()
        logged = [r[0] for r in conn.execute("SELECT event FROM run_log")]
        assert logged.count("unknown_message_type") == 1  # each unknown type is reported once, not per frame


# --------------------------------------------------------------------------- the connection loop


class _Stop(Exception):
    pass


class FakeConn:
    """Answers each order-book subscribe with a "subscribed" frame (sid = 10 + command id), like the server."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, message):
        command = json.loads(message)
        self.sent.append(command)
        if command["cmd"] == "subscribe" and CH_BOOK in command["params"].get("channels", []):
            self.frames.append(json.dumps(
                {"id": command["id"], "type": "subscribed", "msg": {"channel": CH_BOOK, "sid": 10 + command["id"]}}))

    async def recv(self):
        for _ in range(6):
            if self.frames:
                return self.frames.pop(0)
            await asyncio.sleep(0)  # let the ticker watcher run
        raise websockets.exceptions.ConnectionClosedOK(None, None)

    async def close(self):
        self.closed = True


def make_connect(conns, seen):
    async def connect(url, headers):
        seen.append((url, headers))
        if not conns:
            raise _Stop()
        return conns.pop(0)
    return connect


async def _no_sleep(_):
    await asyncio.sleep(0)


class TestRunForever:
    async def test_handshake_headers_subscribe_commands_and_rollover(self, tmp_path, auth):
        tickers = iter(["KXBTC15M-A", "KXBTC15M-A", "KXBTC15M-B", "KXBTC15M-B"])
        last = ["KXBTC15M-B"]

        async def provider():
            last[0] = next(tickers, last[0])
            return last[0]

        conn = FakeConn([j(BRTI_1HZ)])
        seen: list = []
        stream, db_path = make_stream(tmp_path, auth, ticker_provider=provider,
                                      connect=make_connect([conn], seen), sleep=_no_sleep)
        with pytest.raises(_Stop):
            await stream.run_forever()
        stream.close()

        url, headers = seen[0]
        assert url == WS_URLS[KalshiEnv.PROD] and url.endswith(WS_PATH)
        assert set(headers) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP", "KALSHI-ACCESS-SIGNATURE"}

        subs = [(m["cmd"], m["params"]) for m in conn.sent]
        assert ("subscribe", {"channels": [CH_BRTI], "index_ids": ["BRTI"]}) in subs
        assert ("subscribe", {"channels": [CH_BRTI_5HZ], "index_ids": ["BRTI"]}) in subs
        assert ("subscribe", {"channels": [CH_BOOK], "market_tickers": ["KXBTC15M-A"]}) in subs
        assert ("subscribe", {"channels": [CH_BOOK], "market_tickers": ["KXBTC15M-B"]}) in subs
        assert ("unsubscribe", {"sids": [13]}) in subs  # the old window's subscription is dropped on rollover
        assert conn.closed and stream.stats.brti_ticks == 1

    async def test_only_read_only_market_data_commands_are_ever_sent(self, tmp_path, auth):
        async def provider():
            return "KXBTC15M-A"

        conn = FakeConn([j(BRTI_1HZ)])
        stream, _ = make_stream(tmp_path, auth, ticker_provider=provider, connect=make_connect([conn], []), sleep=_no_sleep)
        with pytest.raises(_Stop):
            await stream.run_forever()
        stream.close()
        assert conn.sent, "expected subscribe commands"
        for message in conn.sent:
            assert message["cmd"] in {"subscribe", "unsubscribe"}
            assert set(message["params"].get("channels", [])) <= {CH_BRTI, CH_BRTI_5HZ, CH_BOOK}

    async def test_gap_reconnects_and_resubscribes(self, tmp_path, auth):
        first = FakeConn([j(SNAPSHOT), j(delta(3)), j(delta(9))])   # gap at 9
        second = FakeConn([j(SNAPSHOT), j(BRTI_1HZ)])
        seen: list = []
        stream, _ = make_stream(tmp_path, auth, connect=make_connect([first, second], seen), sleep=_no_sleep)
        with pytest.raises(_Stop):
            await stream.run_forever()
        stream.close()
        assert stream.stats.resyncs == 1 and stream.stats.reconnects == 2
        assert len(seen) == 3 and stream.stats.brti_ticks == 1
        assert first.closed and any(m["cmd"] == "subscribe" for m in second.sent)

    async def test_rejected_credentials_stop_instead_of_retrying(self, tmp_path, auth):
        attempts = []

        async def connect(url, headers):
            attempts.append(1)
            response = websockets.http11.Response(401, "Unauthorized", websockets.datastructures.Headers(), b"")
            raise websockets.exceptions.InvalidStatus(response)

        stream, _ = make_stream(tmp_path, auth, connect=connect, sleep=_no_sleep)
        with pytest.raises(StreamError, match="401"):
            await stream.run_forever()
        stream.close()
        assert len(attempts) == 1


# --------------------------------------------------------------------------- BRTI vs Coinbase


class TestCompare:
    def test_pairs_each_brti_tick_with_the_latest_spot_tick(self, tmp_path, auth):
        stream, db_path = make_stream(tmp_path, auth)
        conn = sqlite3.connect(str(db_path))
        for i, price in enumerate(["80000.00", "80010.00"]):
            conn.execute("INSERT INTO spot_ticks (source, price, source_ts, receive_ts, monotonic_ts) VALUES (?,?,?,?,?)",
                         ("coinbase", price, None, (T0 + timedelta(seconds=i * 10)).isoformat(), float(i)))
        conn.commit()
        for offset, value in ((1, "80004.00"), (11, "80006.00"), (60, "99999.00")):  # last one has no fresh spot tick
            stream._write_brti(BrtiTick(CH_BRTI, "BRTI", Decimal(value), 1000, 1500, T0 + timedelta(seconds=offset)))
        stream._db.commit()
        result = compare_brti_to_spot(conn)
        stream.close()
        assert result.n == 2
        assert result.mean_diff == Decimal("0") and result.mean_abs_diff == Decimal("4")   # +4 and -4
        assert result.max_abs_diff == Decimal("4") and result.mean_feed_lag_ms == 500

    def test_empty_database(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "x.sqlite"))
        assert compare_brti_to_spot(conn).n == 0
