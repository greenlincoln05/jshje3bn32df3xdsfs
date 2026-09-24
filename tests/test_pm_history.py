"""Offline tests for the Polymarket trade tape reader, the Binance 1s kline fetchers and the resumable
``download-polymarket-history`` backfill. httpx.MockTransport stands in for every host; nothing touches the
network."""

import hashlib
import io
import json
import sqlite3
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from btcbot import binance_history
from btcbot.binance_history import (
    BinanceHistoryError,
    BinanceNotPublished,
    _to_unix_seconds,
    days_covering,
    fetch_daily_klines,
    fetch_klines_rest,
    parse_kline_csv,
)
from btcbot.cli import main
from btcbot.models import ParseError
from btcbot.pm_history import (
    PmHistoryError,
    backfill_btc,
    backfill_polymarket,
    init_pm_history_schema,
    load_trades,
    load_windows,
    parse_slug,
    slug_for,
    window_starts,
)
from btcbot.polymarket_client import PmTrade, PolymarketClient

UTC = timezone.utc


def trade_payload(ts=1_790_000_000, outcome="Up", side="BUY", price="0.55", size="10", **extra):
    base = {
        "proxyWallet": "0xWALLET", "name": "someone", "pseudonym": "Nick", "side": side, "asset": "tok",
        "conditionId": "0xcond", "size": size, "price": price, "timestamp": ts, "outcome": outcome,
        "transactionHash": f"0xtx{ts}",
    }
    base.update(extra)
    return base


class TestPmTrade:
    def test_parses_the_fields_a_series_needs(self):
        t = PmTrade.from_api(trade_payload())
        assert (t.outcome, t.side, t.price, t.size) == ("up", "BUY", Decimal("0.55"), Decimal("10"))
        assert t.timestamp == datetime.fromtimestamp(1_790_000_000, tz=UTC)

    def test_outcome_index_is_a_fallback_for_a_missing_outcome_name(self):
        payload = trade_payload(outcome=None, outcomeIndex=1)
        assert PmTrade.from_api(payload).outcome == "down"

    def test_down_prints_convert_to_up_terms(self):
        # Selling Down at 0.30 is bullish and says Up is worth about 0.70.
        t = PmTrade.from_api(trade_payload(outcome="Down", side="SELL", price="0.30"))
        assert t.up_price() == Decimal("0.70") and t.up_flow() == Decimal("10")
        # Buying Down is bearish.
        assert PmTrade.from_api(trade_payload(outcome="Down", side="BUY")).up_flow() == Decimal("-10")
        assert PmTrade.from_api(trade_payload(outcome="Up", side="SELL")).up_flow() == Decimal("-10")

    @pytest.mark.parametrize("bad", [
        {"side": "HOLD"}, {"outcome": "Maybe", "outcomeIndex": None}, {"price": "1.5"}, {"timestamp": "yesterday"},
    ])
    def test_malformed_trades_are_parse_errors_not_guesses(self, bad):
        with pytest.raises(ParseError):
            PmTrade.from_api(trade_payload(**bad))


class TestGetMarketTrades:
    async def test_pages_newest_first_api_into_an_oldest_first_taker_tape(self):
        pages = {0: [trade_payload(ts=30), trade_payload(ts=20)], 2: [trade_payload(ts=10)]}
        seen = []

        def handler(request):
            assert request.url.host == "data-api.polymarket.com" and request.url.path == "/trades"
            assert request.url.params["market"] == "0xcond" and request.url.params["takerOnly"] == "true"
            offset = int(request.url.params["offset"])
            seen.append(offset)
            return httpx.Response(200, json=pages.get(offset, []))

        async with PolymarketClient(transport=httpx.MockTransport(handler)) as client:
            trades, truncated = await client.get_market_trades("0xcond", page_size=2)
        assert seen == [0, 2] and truncated is False
        assert [int(t.timestamp.timestamp()) for t in trades] == [10, 20, 30]

    async def test_hitting_the_offset_cap_reports_a_truncated_tape(self):
        def handler(request):
            offset = int(request.url.params["offset"])
            return httpx.Response(200, json=[trade_payload(ts=1000 - offset), trade_payload(ts=999 - offset)])

        async with PolymarketClient(transport=httpx.MockTransport(handler)) as client:
            trades, truncated = await client.get_market_trades("0xcond", page_size=2, max_offset=4)
        assert truncated is True and len(trades) == 6  # offsets 0, 2, 4 -- then 6 would be past the cap


class TestBinanceParsing:
    def test_timestamp_unit_is_detected_per_value(self):
        assert _to_unix_seconds(1_735_689_600) == 1_735_689_600
        assert _to_unix_seconds(1_735_689_600_000) == 1_735_689_600  # ms (archives before 2025)
        assert _to_unix_seconds(1_735_689_600_000_000) == 1_735_689_600  # us (archives from 2025-01-01)

    def test_csv_with_or_without_header(self):
        row = "1735689600000000,93000.1,93001,92999,93000.5,1.25,1735689600999999,116250,42,0.75,69750,0"
        header = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore"
        for text in (row, header + "\n" + row):
            (k,) = parse_kline_csv(text)
            assert (k.ts, k.close, k.n_trades, k.taker_buy_volume) == (1_735_689_600, Decimal("93000.5"), 42, Decimal("0.75"))

    def test_a_short_row_is_an_error(self):
        with pytest.raises(BinanceHistoryError):
            parse_kline_csv("1735689600000,1,2,3")

    def test_days_covering(self):
        start = datetime(2026, 1, 1, 23, 0, tzinfo=UTC)
        assert days_covering(start, start + timedelta(hours=2)) == [date(2026, 1, 1), date(2026, 1, 2)]
        assert days_covering(start, datetime(2026, 1, 2, tzinfo=UTC)) == [date(2026, 1, 1)]


def _zip_csv(rows: list[str], name="BTCUSDT-1s-2026-01-01.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, "\n".join(rows) + "\n")
    return buf.getvalue()


def _kline_rows(first_ts: int, n: int, *, unit=1_000_000) -> list[str]:
    return [f"{(first_ts + i) * unit},100,101,99,{100 + i},2,0,0,5,1,0,0" for i in range(n)]


class TestFetchDailyKlines:
    async def test_verified_archive_day(self):
        content = _zip_csv(_kline_rows(1_767_225_600, 3))
        digest = hashlib.sha256(content).hexdigest()

        def handler(request):
            assert request.url.host == "data.binance.vision"
            if request.url.path.endswith(".CHECKSUM"):
                return httpx.Response(200, text=f"{digest}  BTCUSDT-1s-2026-01-01.zip\n")
            assert request.url.path == "/data/spot/daily/klines/BTCUSDT/1s/BTCUSDT-1s-2026-01-01.zip"
            return httpx.Response(200, content=content)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            klines = await fetch_daily_klines(http, date(2026, 1, 1))
        assert [k.ts for k in klines] == [1_767_225_600, 1_767_225_601, 1_767_225_602]

    async def test_a_checksum_mismatch_is_refused(self):
        def handler(request):
            if request.url.path.endswith(".CHECKSUM"):
                return httpx.Response(200, text="00" * 32 + "  x.zip")
            return httpx.Response(200, content=_zip_csv(_kline_rows(1_767_225_600, 1)))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(BinanceHistoryError, match="SHA-256"):
                await fetch_daily_klines(http, date(2026, 1, 1))

    async def test_an_unpublished_day_is_its_own_error(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as http:
            with pytest.raises(BinanceNotPublished):
                await fetch_daily_klines(http, date(2026, 1, 1))


class TestFetchKlinesRest:
    async def test_pages_until_a_short_page(self, monkeypatch):
        monkeypatch.setattr(binance_history, "REST_MAX_ROWS", 2)
        start = datetime.fromtimestamp(1_767_225_600, tz=UTC)
        calls = []

        def handler(request):
            assert request.url.host == "data-api.binance.vision" and request.url.params["interval"] == "1s"
            first = int(request.url.params["startTime"]) // 1000
            calls.append(first)
            n = 2 if first < 1_767_225_604 else 1
            return httpx.Response(200, json=[[(first + i) * 1000, "1", "1", "1", "1", "1", 0, "0", 1, "0", "0", "0"] for i in range(n)])

        async def no_sleep(_):
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            klines = await fetch_klines_rest(http, start=start, end=start + timedelta(seconds=10), sleep=no_sleep)
        assert calls == [1_767_225_600, 1_767_225_602, 1_767_225_604]
        assert [k.ts for k in klines] == [1_767_225_600 + i for i in range(5)]


class TestWindows:
    def test_window_starts_are_aligned_and_fully_inside_the_range(self):
        since = datetime.fromtimestamp(1_790_000_100 + 60, tz=UTC)  # a minute past a boundary
        until = since + timedelta(minutes=46)
        starts = window_starts("15m", since, until)
        assert all(s % 900 == 0 for s in starts) and starts[0] >= since.timestamp()
        assert starts[-1] + 900 <= until.timestamp() and len(starts) == 2

    def test_slug_round_trip(self):
        assert parse_slug(slug_for("15m", 1_765_548_000)) == ("15m", 1_765_548_000)
        assert parse_slug("eth-updown-15m-1") is None

    def test_unknown_horizon(self):
        with pytest.raises(PmHistoryError):
            window_starts("1h", datetime.now(UTC), datetime.now(UTC))


W0 = 1_790_000_100 - (1_790_000_100 % 900)


def event_json(start, *, closed=True, prices=("1", "0"), end=None):
    return {
        "slug": f"btc-updown-15m-{start}",
        "startDate": datetime.fromtimestamp(start - 86_400, tz=UTC).isoformat(),  # Gamma's startDate is NOT the window
        "endDate": datetime.fromtimestamp(end if end is not None else start + 900, tz=UTC).isoformat(),
        "markets": [{
            "question": "Bitcoin Up or Down", "conditionId": f"0xcond{start}",
            "clobTokenIds": json.dumps([f"up{start}", f"down{start}"]), "outcomes": json.dumps(["Up", "Down"]),
            "closed": closed, "outcomePrices": json.dumps(list(prices)) if closed else None,
        }],
    }


def pm_handler(script, trade_calls=None):
    """script: window start -> "resolved" | "missing" | "unresolved" | "mismatch"."""
    def handler(request):
        if request.url.host == "gamma-api.polymarket.com":
            start = int(request.url.path.rsplit("-", 1)[1])
            kind = script[start]
            if kind == "missing":
                return httpx.Response(404, json={"error": "not found"})
            if kind == "unresolved":
                return httpx.Response(200, json=event_json(start, closed=False))
            if kind == "mismatch":
                return httpx.Response(200, json=event_json(start, end=start + 300))
            return httpx.Response(200, json=event_json(start))
        if request.url.host == "data-api.polymarket.com":
            cond = request.url.params["market"]
            if trade_calls is not None:
                trade_calls.append(cond)
            start = int(cond.removeprefix("0xcond"))
            return httpx.Response(200, json=[trade_payload(ts=start + 5, proxyWallet="0xSECRET"), trade_payload(ts=start + 1, outcome="Down", price="0.4")])
        raise AssertionError(f"unexpected host {request.url}")
    return handler


class TestBackfillPolymarket:
    async def test_resolved_missing_unresolved_and_mismatched_windows(self):
        script = {W0: "resolved", W0 + 900: "missing", W0 + 1800: "unresolved", W0 + 2700: "mismatch"}
        conn = sqlite3.connect(":memory:")
        init_pm_history_schema(conn)
        since, until = datetime.fromtimestamp(W0, tz=UTC), datetime.fromtimestamp(W0 + 3600, tz=UTC)
        async with PolymarketClient(transport=httpx.MockTransport(pm_handler(script))) as client:
            s = await backfill_polymarket(client, conn, horizon="15m", since=since, until=until, sleep_sec=0)
        assert (s.windows, s.done, s.missing, s.unresolved, s.window_mismatch, s.failed, s.trades) == (4, 1, 1, 1, 1, 0, 2)
        (w,) = load_windows(conn)
        assert (w.start, w.end, w.result_up) == (W0, W0 + 900, True)  # window from the slug, not Gamma's startDate
        trades = load_trades(conn, w.slug)
        assert [t.ts for t in trades] == [W0 + 1, W0 + 5]
        assert trades[0].up_price == pytest.approx(0.6)  # the Down print, in Up terms
        columns = {r[1] for r in conn.execute("PRAGMA table_info(pm_hist_trades)")}
        assert columns == {"slug", "ts", "outcome", "side", "price", "size", "tx_hash"}  # no wallet/profile data

    async def test_a_rerun_skips_done_and_missing_but_retries_unresolved(self):
        script = {W0: "resolved", W0 + 900: "missing", W0 + 1800: "unresolved"}
        conn = sqlite3.connect(":memory:")
        init_pm_history_schema(conn)
        since, until = datetime.fromtimestamp(W0, tz=UTC), datetime.fromtimestamp(W0 + 2700, tz=UTC)
        calls: list[str] = []
        async with PolymarketClient(transport=httpx.MockTransport(pm_handler(script, calls))) as client:
            await backfill_polymarket(client, conn, horizon="15m", since=since, until=until, sleep_sec=0)
            script[W0 + 1800] = "resolved"
            again = await backfill_polymarket(client, conn, horizon="15m", since=since, until=until, sleep_sec=0)
            assert (again.skipped, again.done, again.missing) == (2, 1, 0)
            retried = await backfill_polymarket(client, conn, horizon="15m", since=since, until=until, sleep_sec=0, retry_missing=True)
            assert retried.missing == 1 and retried.skipped == 2
        assert calls == [f"0xcond{W0}", f"0xcond{W0 + 1800}"]  # each resolved window's tape fetched exactly once


class TestBackfillBtc:
    async def test_archive_then_rest_fallback_and_a_partial_day_is_not_marked_done(self):
        day1 = datetime(2026, 1, 1, tzinfo=UTC)
        content = _zip_csv(_kline_rows(int(day1.timestamp()), 2))
        digest = hashlib.sha256(content).hexdigest()

        def handler(request):
            if request.url.host == "data.binance.vision":
                if "2026-01-01" not in request.url.path:
                    return httpx.Response(404)
                if request.url.path.endswith(".CHECKSUM"):
                    return httpx.Response(200, text=digest)
                return httpx.Response(200, content=content)
            first = int(request.url.params["startTime"]) // 1000
            return httpx.Response(200, json=[[first * 1000, "1", "1", "1", "5", "1", 0, "0", 1, "0", "0", "0"]])

        conn = sqlite3.connect(":memory:")
        init_pm_history_schema(conn)
        now = day1 + timedelta(days=1, hours=6)  # day 2 is still in progress
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            s = await backfill_btc(http, conn, start=day1, end=now, now=lambda: now)
        assert (s.days, s.archive_days, s.rest_days, s.failed) == (2, 1, 1, [])
        assert [r[0] for r in conn.execute("SELECT day FROM btc_days_done")] == ["2026-01-01"]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            again = await backfill_btc(http, conn, start=day1, end=now, now=lambda: now)
        assert (again.skipped, again.rest_days) == (1, 1)  # the finished day is skipped, the partial one refreshed


class TestDownloadCli:
    def test_btc_only_and_no_btc_conflict(self, capsys):
        assert main(["download-polymarket-history", "--since", "2026-01-01T00:00:00Z", "--btc-only", "--no-btc"]) == 2

    def test_until_before_since(self, tmp_path, capsys):
        code = main(["download-polymarket-history", "--since", "2026-01-02T00:00:00Z", "--until", "2026-01-01T00:00:00Z",
                     "--data-dir", str(tmp_path)])
        assert code == 2
