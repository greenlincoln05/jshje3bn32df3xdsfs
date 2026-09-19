import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from btcbot import cli
from btcbot.config import KalshiEnv
from btcbot.kalshi_client import KalshiClient
from btcbot.model import ModelState, Prediction, init_predictions_schema, log_prediction, predict
from btcbot.models import Market, OrderBook, Series
from btcbot.recorder import Recorder

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

    def test_demo_check_needs_credentials(self, monkeypatch, tmp_path, capsys):
        monkeypatch.chdir(tmp_path)
        for var in ("KALSHI_ENV", "KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY_PATH"):
            monkeypatch.delenv(var, raising=False)
        assert cli.main(["demo-check"]) == 1
        assert "KALSHI_KEY_ID" in capsys.readouterr().err

    def test_demo_check_has_no_env_flag_at_all(self, capsys):
        # demo-check always targets the demo environment (see kalshi_client.py's write gate); there is no
        # --env flag to override that, and argparse itself is what enforces this (by exiting directly),
        # not a runtime check this test could otherwise bypass.
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["demo-check", "--env", "prod"])
        assert excinfo.value.code == 2
        assert "unrecognized arguments" in capsys.readouterr().err


class TestDemoCheckReportRendering:
    def test_marks_each_row_pass_fail_or_skip(self):
        from btcbot.demo_check import CheckResult, DemoCheckReport

        report = DemoCheckReport(
            "T",
            [
                CheckResult("a", True, "fine"),
                CheckResult("b", False, "broken"),
                CheckResult("c", None, "not attempted"),
            ],
        )
        text = cli.render_demo_check_report(report)
        assert "[PASS] a: fine" in text
        assert "[FAIL] b: broken" in text
        assert "[SKIP] c: not attempted" in text
        assert "Overall: FAILED" in text  # the one False row fails the whole run, regardless of the skip

    def test_all_pass_or_skip_is_an_overall_ok(self):
        from btcbot.demo_check import CheckResult, DemoCheckReport

        report = DemoCheckReport("T", [CheckResult("a", True, "fine"), CheckResult("b", None, "skipped")])
        assert "Overall: OK" in cli.render_demo_check_report(report)


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


def make_calibration_db(tmp_path):
    db_path = tmp_path / "cal.sqlite"
    Recorder(None, series_ticker="KXBTC15M", db_path=db_path).close()
    conn = sqlite3.connect(str(db_path))
    init_predictions_schema(conn)
    return db_path, conn


class TestCalibrateEndToEnd:
    def test_reports_a_missing_database(self, tmp_path, capsys):
        assert cli.main(["calibrate", "--db", str(tmp_path / "nope.sqlite")]) == 1
        assert "no such database" in capsys.readouterr().err

    def test_reports_a_non_recorder_database(self, tmp_path, capsys):
        empty_db = tmp_path / "empty.sqlite"
        sqlite3.connect(str(empty_db)).close()
        assert cli.main(["calibrate", "--db", str(empty_db)]) == 1
        assert "does not look like a recorder database" in capsys.readouterr().err

    def test_reports_no_resolved_predictions_yet(self, tmp_path, capsys):
        db_path, conn = make_calibration_db(tmp_path)
        conn.close()
        assert cli.main(["calibrate", "--db", str(db_path)]) == 0
        assert "nothing to score" in capsys.readouterr().out

    def test_prints_a_calibration_report(self, tmp_path, capsys):
        db_path, conn = make_calibration_db(tmp_path)
        conn.execute(
            """INSERT INTO settlements (ticker, event_ticker, result, settled_avg, strike, close_time, finalized_poll_ts)
               VALUES ('T-1', 'T', 'yes', '80100', '80000', '2026-09-19T00:00:00+00:00', '2026-09-19T00:00:00+00:00')"""
        )
        conn.commit()
        state = ModelState(spot=Decimal("80500"), strike=Decimal("80000"), tau_sec=300.0, sigma=0.0005)
        p_model, p_blend = predict(state)
        log_prediction(conn, Prediction("T-1", datetime.now(UTC), state, 0.5, p_model, p_blend))
        conn.close()

        assert cli.main(["calibrate", "--db", str(db_path)]) == 0
        out = capsys.readouterr().out
        assert "model " in out and "brier score" in out

    def test_rejects_non_positive_bins(self, tmp_path, capsys):
        db_path, conn = make_calibration_db(tmp_path)
        conn.close()
        assert cli.main(["calibrate", "--db", str(db_path), "--bins", "0"]) == 2
        assert "--bins" in capsys.readouterr().err


def make_backtest_db(tmp_path):
    import json

    db_path = tmp_path / "bt.sqlite"
    Recorder(None, series_ticker="KXBTC15M", db_path=db_path).close()
    conn = sqlite3.connect(str(db_path))

    ticker = "KXBTC15M-26SEP190000-00"
    start = datetime(2026, 9, 19, 0, 0, 0, tzinfo=UTC)
    close_time = start + timedelta(seconds=500)

    price = Decimal("80000")
    for i in range(110):
        ts = start - timedelta(seconds=90) + timedelta(seconds=i)
        price = price + Decimal("1") if i % 2 == 0 else price - Decimal("0.5")
        conn.execute(
            "INSERT INTO spot_ticks (source, price, source_ts, receive_ts, monotonic_ts) VALUES (?,?,?,?,?)",
            ("coinbase-ws", str(price), ts.isoformat(), ts.isoformat(), float(i)),
        )
    conn.execute(
        """INSERT INTO market_state
           (ticker, event_ticker, poll_ts, status, strike, open_time, close_time, volume, open_interest)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (ticker, "KXBTC15M-26SEP190000", start.isoformat(), "active", "80000",
         (start - timedelta(seconds=300)).isoformat(), close_time.isoformat(), "0", "0"),
    )
    for offset, size in enumerate([15, 14, 13, 12, 11, 10, 14, 0]):
        ts = start + timedelta(seconds=offset)
        payload = json.dumps({"yes": [["0.30", str(size)]], "no": [["0.68", "15"]]})
        conn.execute(
            """INSERT INTO orderbook_snapshots
               (ticker, request_started_ts, poll_ts, latency_ms, yes_bid_price, yes_bid_size, yes_ask_price,
                yes_ask_size, no_bid_price, no_bid_size, no_ask_price, no_ask_size, book_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticker, ts.isoformat(), ts.isoformat(), 10.0, "0.30", str(size), None, None, "0.68", "15", None, None, payload),
        )
    conn.execute(
        """INSERT INTO settlements (ticker, event_ticker, result, settled_avg, strike, close_time, finalized_poll_ts)
           VALUES (?,?,?,?,?,?,?)""",
        (ticker, "KXBTC15M-26SEP190000", "yes", "80500", "80000", close_time.isoformat(), close_time.isoformat()),
    )
    conn.commit()
    return db_path, conn


class TestBacktestEndToEnd:
    def test_reports_a_missing_database(self, tmp_path, capsys):
        assert cli.main(["backtest", "--db", str(tmp_path / "nope.sqlite")]) == 1
        assert "no such database" in capsys.readouterr().err

    def test_reports_a_non_recorder_database(self, tmp_path, capsys):
        empty_db = tmp_path / "empty.sqlite"
        sqlite3.connect(str(empty_db)).close()
        assert cli.main(["--config", CONFIG, "backtest", "--db", str(empty_db)]) == 1
        assert "error:" in capsys.readouterr().err

    def test_prints_all_four_combinations_by_default(self, tmp_path, capsys):
        db_path, conn = make_backtest_db(tmp_path)
        conn.close()
        assert cli.main(["--config", CONFIG, "backtest", "--db", str(db_path)]) == 0
        out = capsys.readouterr().out
        assert out.count("--- queue=") == 4  # optimistic/pessimistic x maker_fee_multiplier 0/0.25
        assert out.count("queue=optimistic") == 2
        assert out.count("queue=pessimistic") == 2
        assert "maker_fee_multiplier=0" in out
        assert "maker_fee_multiplier=0.25" in out
        assert "Beats trade-nothing after fees? yes" in out

    def test_single_combination_via_flags(self, tmp_path, capsys):
        db_path, conn = make_backtest_db(tmp_path)
        conn.close()
        assert cli.main(
            ["--config", CONFIG, "backtest", "--db", str(db_path), "--queue", "pessimistic", "--maker-fee-multiplier", "0"]
        ) == 0
        out = capsys.readouterr().out
        assert out.count("---") == 2  # exactly one report block
        assert "queue=pessimistic" in out

    def test_rejects_a_bad_maker_fee_multiplier(self, tmp_path, capsys):
        assert cli.main(["backtest", "--db", "whatever", "--maker-fee-multiplier", "not-a-number"]) == 2
        assert "--maker-fee-multiplier" in capsys.readouterr().err

    def test_rejects_a_negative_maker_fee_multiplier(self, tmp_path, capsys):
        assert cli.main(["backtest", "--db", "whatever", "--maker-fee-multiplier", "-1"]) == 2
        assert "--maker-fee-multiplier" in capsys.readouterr().err


class TestDemoAllocation:
    def test_the_markets_shard_gets_the_percent_and_the_rest_stays_on_shard_zero(self):
        assert cli.allocation_for(2, 100) == {2: 100}
        assert cli.allocation_for(2, 60) == {2: 60, 0: 40}
        assert cli.allocation_for(0, 50) == {0: 100}
        assert cli.allocation_for(None, 100) == {2: 100}  # unreported shard: assume crypto's

    def test_a_percent_outside_1_to_100_is_refused(self):
        import pytest

        for bad in (0, 101, -5):
            with pytest.raises(ValueError):
                cli.allocation_for(2, bad)


class TestAuthCheckDescribesWhatItUses:
    def test_it_shows_the_environment_key_suffix_and_file_but_never_the_key(self, tmp_path):
        from cryptography.hazmat.primitives.asymmetric import rsa

        from btcbot.kalshi_client import KalshiAuth

        key_file = tmp_path / "k.pem"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nSECRET-BODY\n", encoding="utf-8")
        auth = KalshiAuth("aaaaaaaa-bbbb-cccc-dddd-eeeeeeee57a5", rsa.generate_private_key(public_exponent=65537, key_size=2048))
        text = cli.describe_credentials(auth, key_file, KalshiEnv.DEMO)
        assert "demo" in text and "external-api.demo.kalshi.co" in text and "...57a5" in text and str(key_file) in text
        assert "SECRET-BODY" not in text and "aaaaaaaa" not in text

    def test_a_missing_key_file_is_reported_not_raised(self, tmp_path):
        from cryptography.hazmat.primitives.asymmetric import rsa

        from btcbot.kalshi_client import KalshiAuth

        auth = KalshiAuth("id-1234", rsa.generate_private_key(public_exponent=65537, key_size=2048))
        assert "missing" in cli.describe_credentials(auth, tmp_path / "nope.pem", KalshiEnv.DEMO)
