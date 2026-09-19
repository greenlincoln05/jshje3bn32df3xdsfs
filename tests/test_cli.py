import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from btcbot import cli
from btcbot.config import KalshiEnv
from btcbot.kalshi_client import KalshiClient
from btcbot.models import Market, OrderBook, Series

UTC = timezone.utc
CONFIG = str(Path(__file__).resolve().parents[1] / "config.yaml")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("0.5400"), "0.54"),
        (Decimal("0.0010"), "0.001"),
        (Decimal("0.0545"), "0.0545"),
        (Decimal("1"), "1.00"),
        (Decimal("0"), "0.00"),
        (Decimal("-0.0100"), "-0.01"),
        (None, "-"),
    ],
)
def test_fmt_price_shows_two_to_four_decimals(value, expected):
    assert cli.fmt_price(value) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(219.1, "3m 39.1s"), (900, "15m 00.0s"), (59.94, "0m 59.9s"), (59.96, "1m 00.0s"), (0, "closed"), (-5, "closed")],
)
def test_fmt_remaining(seconds, expected):
    assert cli.fmt_remaining(seconds) == expected


class TestRendering:
    @pytest.fixture
    def parts(self, load_fixture):
        market = Market.from_api(load_fixture("market_active.json")["market"])
        book = OrderBook.from_api(market.ticker, load_fixture("orderbook_prod.json"))
        series = Series.from_api(load_fixture("series_kxbtc15m.json")["series"])
        return market, book, series

    def test_discovery_shows_strike_close_time_and_top_of_book(self, parts):
        market, book, series = parts
        text = cli.render_discovery(KalshiEnv.PROD, series, market, book, market.close_time - timedelta(seconds=220))

        for expected in (
            "KXBTC15M-26SEP182130-30",
            "81,263.65",
            "2026-09-19 01:30:00 UTC",
            "3m 40.0s",
            "quadratic x1",
            "greater_or_equal",
        ):
            assert expected in text
        lines = {line.split()[0]: " ".join(line.split()) for line in text.splitlines() if line.startswith("  ")}
        assert lines["YES"] == "YES bid 0.54 x 1,670.79 ask 0.55 x 6,445.03 spread 0.01 mid 0.545"
        assert lines["NO"] == "NO bid 0.45 x 6,445.03 ask 0.46 x 1,670.79 spread 0.01 mid 0.455"

    def test_missing_strike_is_reported_not_crashed_on(self, parts, load_fixture):
        _, book, series = parts
        payload = {**load_fixture("market_active.json")["market"], "floor_strike": None}
        market = Market.from_api(payload)

        text = cli.render_discovery(KalshiEnv.DEMO, series, market, book, market.open_time)

        assert "not published" in text

    def test_empty_book_renders_dashes(self, parts):
        market, _, series = parts
        empty = OrderBook.from_api(market.ticker, {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}})

        text = cli.render_discovery(KalshiEnv.PROD, series, market, empty, market.open_time)

        assert "bid      -" in text
        assert "0 YES bid levels, 0 NO bid levels" in text

    def test_watch_line_is_one_compact_line(self, parts):
        market, book, _ = parts
        line = cli.render_watch_line(market, book, market.close_time - timedelta(seconds=220))
        assert "\n" not in line
        assert line == "01:26:20Z KXBTC15M-26SEP182130-30 strike=81,263.65 tau= 220.0s YES 0.54/0.55 NO 0.45/0.46"


class TestMain:
    def test_rejects_a_non_positive_watch_interval(self, capsys):
        assert cli.main(["discover", "--watch", "0"]) == 2
        assert "--watch" in capsys.readouterr().err

    def test_reports_a_missing_config_file(self, tmp_path, capsys):
        assert cli.main(["--config", str(tmp_path / "nope.yaml"), "discover", "--env", "prod"]) == 1
        assert "config file not found" in capsys.readouterr().err

    def test_auth_check_needs_credentials(self, monkeypatch, tmp_path, capsys):
        monkeypatch.chdir(tmp_path)  # no .env here
        for var in ("KALSHI_ENV", "KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY_PATH"):
            monkeypatch.delenv(var, raising=False)
        assert cli.main(["auth-check"]) == 1
        assert "KALSHI_KEY_ID" in capsys.readouterr().err

    def test_auth_check_reports_a_missing_key_file(self, monkeypatch, tmp_path, capsys):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("KALSHI_KEY_ID", "abc")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(tmp_path / "absent.key"))
        assert cli.main(["auth-check"]) == 1
        assert "absent.key" in capsys.readouterr().err


def install_mock_api(monkeypatch, handler):
    real_client = KalshiClient
    monkeypatch.setattr(cli, "KalshiClient", lambda env, **kw: real_client(env, transport=httpx.MockTransport(handler), **kw))


def install_fake_spot_feed(monkeypatch):
    """record always starts a background spot feed; give it a connector that fails instantly instead of
    letting it reach the real network in a test."""
    real_feed_cls = cli.CoinbaseSpotFeed

    async def refuse(url):
        raise OSError("no network access in tests")

    monkeypatch.setattr(cli, "CoinbaseSpotFeed", lambda buffer, **kw: real_feed_cls(buffer, connect=refuse, **kw))


class TestDiscoverEndToEnd:
    def test_prints_the_open_market_found_by_walking_series_markets_orderbook(self, monkeypatch, capsys, load_fixture):
        now = datetime.now(UTC)
        markets = load_fixture("markets_open.json")
        market = markets["markets"][0]
        market["open_time"] = (now - timedelta(minutes=2)).isoformat()
        market["close_time"] = (now + timedelta(minutes=13)).isoformat()
        ticker = market["ticker"]
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/trade-api/v2/series/KXBTC15M":
                return httpx.Response(200, json=load_fixture("series_kxbtc15m.json"))
            if request.url.path == "/trade-api/v2/markets":
                return httpx.Response(200, json=markets)
            if request.url.path == f"/trade-api/v2/markets/{ticker}/orderbook":
                return httpx.Response(200, json=load_fixture("orderbook_prod.json"))
            return httpx.Response(404, json={"error": {"code": "not_found", "message": request.url.path}})

        install_mock_api(monkeypatch, handler)

        assert cli.main(["--config", CONFIG, "discover", "--env", "prod"]) == 0

        out = capsys.readouterr().out
        assert ticker in out and "81,263.65" in out and "Top of book" in out
        assert [r.url.path for r in seen] == [
            "/trade-api/v2/series/KXBTC15M",
            "/trade-api/v2/markets",
            f"/trade-api/v2/markets/{ticker}/orderbook",  # ticker came from discovery, not from config
        ]
        assert not [name for r in seen for name in r.headers if name.lower().startswith("kalshi-access")]

    def test_exits_nonzero_when_no_market_is_open(self, monkeypatch, capsys, load_fixture):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/trade-api/v2/series/"):
                return httpx.Response(200, json=load_fixture("series_kxbtc15m.json"))
            return httpx.Response(200, json={"markets": [], "cursor": ""})

        install_mock_api(monkeypatch, handler)

        assert cli.main(["--config", CONFIG, "discover", "--env", "prod"]) == 1
        assert "No open KXBTC15M market" in capsys.readouterr().err

    def test_series_flag_overrides_the_config(self, monkeypatch, capsys, load_fixture):
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path.startswith("/trade-api/v2/series/"):
                return httpx.Response(200, json=load_fixture("series_kxbtc15m.json"))
            return httpx.Response(200, json={"markets": []})

        install_mock_api(monkeypatch, handler)

        cli.main(["--config", CONFIG, "discover", "--env", "demo", "--series", "KXOTHER"])

        assert "/trade-api/v2/series/KXOTHER" in paths

    def test_api_errors_become_a_clean_message_and_exit_code_1(self, monkeypatch, capsys):
        install_mock_api(monkeypatch, lambda request: httpx.Response(400, json={"error": {"code": "bad", "message": "nope"}}))

        assert cli.main(["--config", CONFIG, "discover", "--env", "prod"]) == 1
        assert "nope" in capsys.readouterr().err


class TestRecordEndToEnd:
    def test_stops_via_kill_file_and_prints_a_summary(self, monkeypatch, tmp_path, capsys):
        install_mock_api(monkeypatch, lambda request: httpx.Response(200, json={"markets": []}))
        install_fake_spot_feed(monkeypatch)
        kill_file = tmp_path / "KILL"
        kill_file.write_text("stop")
        data_dir = tmp_path / "data"

        exit_code = cli.main(
            [
                "--config",
                CONFIG,
                "record",
                "--env",
                "prod",
                "--data-dir",
                str(data_dir),
                "--kill-file",
                str(kill_file),
                "--hours",
                "1",
            ]
        )

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "Recording KXBTC15M (prod, public data only)" in out
        assert "Stopped     : kill_file" in out
        assert list(data_dir.glob("recorder-*.sqlite"))

    def test_rejects_a_non_positive_hours(self, capsys):
        assert cli.main(["record", "--hours", "0"]) == 2
        assert "--hours" in capsys.readouterr().err

    def test_rejects_a_non_positive_poll_interval(self, capsys):
        assert cli.main(["record", "--poll-interval", "0"]) == 2
        assert "--poll-interval" in capsys.readouterr().err


class TestWatch:
    async def test_survives_bad_payloads_and_api_errors_and_keeps_printing(self, capsys, load_fixture):
        now = datetime.now(UTC)
        markets = load_fixture("markets_open.json")
        market = markets["markets"][0]
        market["open_time"] = (now - timedelta(minutes=2)).isoformat()
        market["close_time"] = (now + timedelta(minutes=13)).isoformat()
        market_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal market_calls
            if request.url.path == "/trade-api/v2/markets":
                market_calls += 1
                if market_calls == 1:
                    return httpx.Response(200, json={"markets": [{"title": "no ticker"}]})  # malformed payload
                if market_calls == 2:
                    return httpx.Response(400, json={"error": {"code": "bad", "message": "transient"}})
                return httpx.Response(200, json=markets)
            return httpx.Response(200, json=load_fixture("orderbook_prod.json"))

        async with KalshiClient(KalshiEnv.PROD, transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(TimeoutError):  # _watch runs until cancelled
                await asyncio.wait_for(cli._watch(client, "KXBTC15M", 0.01), timeout=0.5)

        lines = capsys.readouterr().out.splitlines()
        assert "error:" in lines[0] and "'ticker'" in lines[0]
        assert "error:" in lines[1] and "transient" in lines[1]
        assert any(market["ticker"] in line and "YES 0.54/0.55" in line for line in lines[2:])
