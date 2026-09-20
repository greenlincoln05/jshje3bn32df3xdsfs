import json
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from btcbot.backtest import TradeRecord, init_trades_schema, log_trade
from btcbot.recorder import Recorder
from btcbot.webui import (
    _resolve_db,
    create_dashboard_server,
    list_databases,
    paper_summary,
    read_env_settings,
    settings_status,
    write_env_settings,
)

T0 = datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc)
TICKER = "KXBTC15M-26SEP190000-00"


def make_db(tmp_path, name="paper-KXBTC15M-demo-20260919T000000Z.sqlite"):
    db_path = tmp_path / name
    Recorder(None, series_ticker="KXBTC15M", db_path=db_path).close()
    return db_path


def insert_market(conn, ticker, *, strike, close_time, open_time=None, status="active"):
    open_time = open_time or (close_time - timedelta(seconds=900))
    conn.execute(
        """INSERT INTO market_state
           (ticker, event_ticker, poll_ts, status, strike, open_time, close_time, volume, open_interest)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (ticker, ticker.rsplit("-", 1)[0], open_time.isoformat(), status, str(strike), open_time.isoformat(), close_time.isoformat(), "0", "0"),
    )
    conn.commit()


def insert_snapshot(conn, ticker, poll_ts, *, yes=(), no=()):
    payload = json.dumps({"yes": [[p, s] for p, s in yes], "no": [[p, s] for p, s in no]})
    yb = yes[0] if yes else (None, None)
    nb = no[0] if no else (None, None)
    conn.execute(
        """INSERT INTO orderbook_snapshots
           (ticker, request_started_ts, poll_ts, latency_ms, yes_bid_price, yes_bid_size, yes_ask_price,
            yes_ask_size, no_bid_price, no_bid_size, no_ask_price, no_ask_size, book_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ticker, poll_ts.isoformat(), poll_ts.isoformat(), 10.0, yb[0], yb[1], None, None, nb[0], nb[1], None, None, payload),
    )
    conn.commit()


def insert_spot_run(conn, start_ts, count, *, base_price=Decimal("80000")):
    price = base_price
    for i in range(count):
        ts = start_ts + timedelta(seconds=i)
        price = price + Decimal("1") if i % 2 == 0 else price - Decimal("0.5")
        conn.execute(
            "INSERT INTO spot_ticks (source, price, source_ts, receive_ts, monotonic_ts) VALUES (?,?,?,?,?)",
            ("coinbase-ws", str(price), ts.isoformat(), ts.isoformat(), float(i)),
        )
    conn.commit()


def insert_settlement(conn, ticker, result, *, strike, close_time):
    conn.execute(
        """INSERT INTO settlements (ticker, event_ticker, result, settled_avg, strike, close_time, finalized_poll_ts)
           VALUES (?,?,?,?,?,?,?)""",
        (ticker, ticker.rsplit("-", 1)[0], result, str(strike), str(strike), close_time.isoformat(), close_time.isoformat()),
    )
    conn.commit()


def seed_fillable_window(conn, ticker, *, start_ts, strike=Decimal("80000"), close_time=None):
    """Same shrinking-depth trick test_backtest.py uses: triggers exactly one maker fill of 4 contracts at
    0.30 given an up-trending spot feed. The 90s/110-tick spot warm-up (matching test_backtest.py's own
    fixture) is what it takes for TimedVolatility.ready to flip true before the window under test starts."""
    close_time = close_time or (start_ts + timedelta(seconds=500))
    insert_spot_run(conn, start_ts - timedelta(seconds=90), 110)
    insert_market(conn, ticker, strike=strike, close_time=close_time, open_time=start_ts - timedelta(seconds=300))
    for offset, size in enumerate([15, 14, 13, 12, 11, 10, 14, 0]):
        insert_snapshot(conn, ticker, start_ts + timedelta(seconds=offset), yes=[("0.30", str(size))], no=[("0.68", "15")])
    return close_time


def make_trade(ticker="A", *, result="yes", pnl_usd=Decimal("2.79"), entry_ts=T0):
    return TradeRecord(
        ticker=ticker, side="yes", size=Decimal("4"), entry_price=Decimal("0.30"), entry_ts=entry_ts,
        fee_paid=Decimal("0.01"), p_side_at_entry=0.5, result=result, pnl_usd=pnl_usd,
    )


# --------------------------------------------------------------------------- pure functions, no server


class TestEnvSettings:
    def test_missing_file_reads_as_empty(self, tmp_path):
        assert read_env_settings(tmp_path / "missing.env") == {}
        status = settings_status(tmp_path / "missing.env")
        assert status["env_file_exists"] is False
        assert status["kalshi_env"] == "demo"
        assert status["key_id_set"] is False

    def test_write_then_read_round_trips(self, tmp_path):
        env_path = tmp_path / ".env"
        write_env_settings(env_path, {"KALSHI_ENV": "prod", "KALSHI_KEY_ID": "abcd1234", "KALSHI_PRIVATE_KEY_PATH": "/k.pem"})
        assert read_env_settings(env_path) == {
            "KALSHI_ENV": "prod", "KALSHI_KEY_ID": "abcd1234", "KALSHI_PRIVATE_KEY_PATH": "/k.pem",
        }
        status = settings_status(env_path)
        assert status["key_id_set"] is True
        assert status["key_id_last4"] == "1234"
        # never the full key id anywhere in the status payload
        assert "abcd1234" not in json.dumps(status)

    def test_partial_update_preserves_untouched_keys_and_unrelated_lines(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("# a comment\nKALSHI_ENV=demo\nKALSHI_KEY_ID=keep-me\nOTHER_VAR=untouched\n", encoding="utf-8")
        write_env_settings(env_path, {"KALSHI_ENV": "prod"})
        text = env_path.read_text(encoding="utf-8")
        assert "KALSHI_ENV=prod" in text
        assert "KALSHI_KEY_ID=keep-me" in text  # untouched by a partial update
        assert "OTHER_VAR=untouched" in text  # unrelated lines survive
        assert "# a comment" in text

    def test_empty_string_clears_a_key(self, tmp_path):
        env_path = tmp_path / ".env"
        write_env_settings(env_path, {"KALSHI_KEY_ID": "something"})
        write_env_settings(env_path, {"KALSHI_KEY_ID": ""})
        assert read_env_settings(env_path).get("KALSHI_KEY_ID", "") == ""
        assert settings_status(env_path)["key_id_set"] is False


class TestListDatabases:
    def test_lists_only_sqlite_files_with_a_kind_guess(self, tmp_path):
        (tmp_path / "paper-x.sqlite").touch()
        (tmp_path / "recorder-y.sqlite").touch()
        (tmp_path / "notes.txt").touch()
        names = {e["name"]: e["kind"] for e in list_databases(tmp_path)}
        assert names == {"paper-x.sqlite": "paper", "recorder-y.sqlite": "recorder"}

    def test_missing_data_dir_is_an_empty_list(self, tmp_path):
        assert list_databases(tmp_path / "does-not-exist") == []

    def test_resolve_db_rejects_traversal_and_unknown_names(self, tmp_path):
        (tmp_path / "real.sqlite").touch()
        assert _resolve_db(tmp_path, "real.sqlite") is not None
        assert _resolve_db(tmp_path, "../real.sqlite") is None
        assert _resolve_db(tmp_path, "sub/real.sqlite") is None
        assert _resolve_db(tmp_path, "no-such-file.sqlite") is None


class TestPaperSummaryFunction:
    def test_summarizes_resolved_and_unresolved_trades(self, tmp_path):
        db_path = make_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        init_trades_schema(conn)
        insert_snapshot(conn, "A", T0)
        insert_snapshot(conn, "B", T0 + timedelta(minutes=15))
        log_trade(conn, make_trade("A", result="yes", pnl_usd=Decimal("2.79")))
        log_trade(conn, make_trade("B", result=None, pnl_usd=None, entry_ts=T0 + timedelta(minutes=15)))
        conn.close()

        summary = paper_summary(db_path)

        assert summary["trade_count"] == 2
        assert summary["resolved_count"] == 1
        assert summary["unresolved_count"] == 1
        assert summary["wins"] == 1
        assert summary["win_rate"] == 1.0
        assert summary["total_pnl_usd"] == Decimal("2.79")
        assert summary["windows_seen"] == 2
        assert summary["windows_traded"] == 2
        assert len(summary["cumulative_pnl"]) == 1  # only the resolved trade contributes


# --------------------------------------------------------------------------- full HTTP server


@pytest.fixture
def dashboard(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mode: paper\n", encoding="utf-8")
    server = create_dashboard_server(data_dir=data_dir, env_path=tmp_path / ".env", config_path=config_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            data_dir=data_dir,
            env_path=tmp_path / ".env",
            config_path=config_path,
            base_url=f"http://127.0.0.1:{server.server_address[1]}",
            server=server,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _get(base_url, path):
    try:
        with urllib.request.urlopen(f"{base_url}{path}") as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _post(base_url, path, payload):
    body = json.dumps(payload).encode("utf-8") if not isinstance(payload, (bytes, str)) else (
        payload.encode("utf-8") if isinstance(payload, str) else payload
    )
    request = urllib.request.Request(f"{base_url}{path}", data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class TestServerBinding:
    def test_binds_to_localhost_only(self, dashboard):
        assert dashboard.server.server_address[0] == "127.0.0.1"


class TestIndexAndRouting:
    def test_index_serves_html(self, dashboard):
        with urllib.request.urlopen(dashboard.base_url + "/") as response:
            assert response.status == 200
            assert "text/html" in response.headers["Content-Type"]
            assert b"btc15m-bot dashboard" in response.read()

    def test_unknown_path_is_404(self, dashboard):
        status, body = _get(dashboard.base_url, "/api/nope")
        assert status == 404
        assert "error" in body


class TestDatabasesAndSummaryEndpoints:
    def test_databases_lists_files_in_data_dir(self, dashboard):
        make_db(dashboard.data_dir, "paper-KXBTC15M-demo-20260919T000000Z.sqlite")
        status, body = _get(dashboard.base_url, "/api/databases")
        assert status == 200
        assert [e["name"] for e in body["databases"]] == ["paper-KXBTC15M-demo-20260919T000000Z.sqlite"]

    def test_paper_summary_requires_db_param(self, dashboard):
        status, body = _get(dashboard.base_url, "/api/paper_summary")
        assert status == 400
        assert "db" in body["error"]

    def test_paper_summary_rejects_unknown_db(self, dashboard):
        status, _ = _get(dashboard.base_url, "/api/paper_summary?db=missing.sqlite")
        assert status == 404

    def test_paper_summary_round_trips_a_real_db_over_http(self, dashboard):
        name = "paper-KXBTC15M-demo-20260919T000000Z.sqlite"
        db_path = make_db(dashboard.data_dir, name)
        conn = sqlite3.connect(str(db_path))
        init_trades_schema(conn)
        insert_snapshot(conn, "A", T0)
        log_trade(conn, make_trade("A"))
        conn.close()

        status, body = _get(dashboard.base_url, f"/api/paper_summary?db={name}")

        assert status == 200
        assert body["trade_count"] == 1
        assert body["total_pnl_usd"] == "2.79"  # Decimal serialized as a string, not a float
        assert body["trades"][0]["ticker"] == "A"


class TestBacktestEndpoint:
    def test_missing_config_is_a_clean_400(self, dashboard):
        name = "paper-KXBTC15M-demo-20260919T000000Z.sqlite"
        make_db(dashboard.data_dir, name)
        dashboard.config_path.unlink()
        status, body = _get(dashboard.base_url, f"/api/backtest?db={name}")
        assert status == 400
        assert "config" in body["error"]

    def test_invalid_maker_fee_multiplier_is_rejected(self, dashboard):
        name = "paper-KXBTC15M-demo-20260919T000000Z.sqlite"
        make_db(dashboard.data_dir, name)
        status, body = _get(dashboard.base_url, f"/api/backtest?db={name}&maker_fee_multiplier=-1")
        assert status == 400

    def test_a_winning_window_produces_a_report_over_http(self, dashboard):
        name = "paper-KXBTC15M-demo-20260919T000000Z.sqlite"
        db_path = make_db(dashboard.data_dir, name)
        conn = sqlite3.connect(str(db_path))
        close_time = seed_fillable_window(conn, TICKER, start_ts=T0)
        insert_settlement(conn, TICKER, "yes", strike=Decimal("80000"), close_time=close_time)
        conn.close()

        status, body = _get(
            dashboard.base_url, f"/api/backtest?db={name}&queue=optimistic&maker_fee_multiplier=0"
        )

        assert status == 200
        [report] = body["reports"]
        assert report["trades"] == 1
        assert report["wins"] == 1
        # a JSON-serialized Decimal string, not a float -- compare by value, since the report's own
        # precision (trailing zeros from the fee math) need not match this test's literal
        assert Decimal(report["total_pnl_usd"]) == Decimal("4") * (Decimal(1) - Decimal("0.30"))


class TestSettingsEndpoint:
    def test_get_defaults_when_no_file_exists(self, dashboard):
        status, body = _get(dashboard.base_url, "/api/settings")
        assert status == 200
        assert body["env_file_exists"] is False
        assert body["kalshi_env"] == "demo"

    def test_post_writes_and_masks_on_read_back(self, dashboard):
        status, body = _post(dashboard.base_url, "/api/settings", {
            "kalshi_env": "prod", "key_id": "dummy-key-id-1234", "private_key_path": "/home/user/key.pem",
        })
        assert status == 200
        assert body["key_id_last4"] == "1234"
        raw = dashboard.env_path.read_text(encoding="utf-8")
        assert "KALSHI_KEY_ID=dummy-key-id-1234" in raw  # only on disk, never in the HTTP response
        assert "dummy-key-id-1234" not in json.dumps(body)

    def test_partial_post_does_not_clobber_other_fields(self, dashboard):
        _post(dashboard.base_url, "/api/settings", {"kalshi_env": "prod", "key_id": "keep-me-1234"})
        status, body = _post(dashboard.base_url, "/api/settings", {"kalshi_env": "demo"})
        assert status == 200
        assert body["kalshi_env"] == "demo"
        assert body["key_id_last4"] == "1234"  # untouched by the env-only update

    def test_invalid_kalshi_env_is_rejected(self, dashboard):
        status, body = _post(dashboard.base_url, "/api/settings", {"kalshi_env": "not-a-real-env"})
        assert status == 400
        assert dashboard.env_path.exists() is False  # rejected before anything was written

    def test_malformed_json_body_is_a_clean_400(self, dashboard):
        status, body = _post(dashboard.base_url, "/api/settings", "{not json")
        assert status == 400

    def test_non_object_json_body_is_a_clean_400(self, dashboard):
        status, body = _post(dashboard.base_url, "/api/settings", [1, 2, 3])
        assert status == 400


class TestMarketEndpointAndNoTradesTable:
    def test_paper_summary_ok_when_database_has_no_trades_table(self, dashboard):
        db_path = make_db(dashboard.data_dir)
        status, body = _get(dashboard.base_url, f"/api/paper_summary?db={db_path.name}")
        assert status == 200
        assert body["trade_count"] == 0 and body["trades"] == []

    def test_market_view_returns_book_and_history(self, dashboard):
        db_path = make_db(dashboard.data_dir)
        conn = sqlite3.connect(str(db_path))
        insert_market(conn, TICKER, strike=Decimal("80000"), close_time=T0 + timedelta(minutes=15))
        insert_snapshot(conn, TICKER, T0 + timedelta(seconds=1), yes=[("0.40", "10.00")], no=[("0.55", "5.00")])
        insert_snapshot(conn, TICKER, T0 + timedelta(seconds=2), yes=[("0.41", "10.00")], no=[("0.55", "5.00")])
        conn.close()
        status, body = _get(dashboard.base_url, f"/api/market?db={db_path.name}")
        assert status == 200
        assert body["ticker"] == TICKER and body["strike"] == "80000"
        assert body["book"]["yes"] == [["0.41", "10.00"]]
        assert len(body["mid_series"]) == 2 and body["trades"] == []

    def test_market_view_empty_database(self, dashboard):
        db_path = make_db(dashboard.data_dir)
        status, body = _get(dashboard.base_url, f"/api/market?db={db_path.name}")
        assert status == 200
        assert body["ticker"] is None and body["tickers"] == []

    def test_market_requires_known_db(self, dashboard):
        status, _ = _get(dashboard.base_url, "/api/market?db=missing.sqlite")
        assert status == 404

    def test_market_view_lists_windows_with_close_time_and_trade_counts(self, dashboard):
        db_path = make_db(dashboard.data_dir)
        conn = sqlite3.connect(str(db_path))
        close = T0 + timedelta(minutes=15)
        insert_market(conn, TICKER, strike=Decimal("80000"), close_time=close)
        insert_snapshot(conn, TICKER, T0 + timedelta(seconds=1), yes=[("0.40", "10.00")], no=[("0.55", "5.00")])
        conn.close()
        status, body = _get(dashboard.base_url, f"/api/market?db={db_path.name}")
        assert status == 200
        assert body["windows"] == [{"ticker": TICKER, "close_time": close.isoformat(), "result": None, "trades": 0}]

    def test_list_databases_labels_stream_files(self, tmp_path):
        (tmp_path / "stream-KXBTC15M-prod-20260919T000000Z.sqlite").write_bytes(b"")
        (tmp_path / "recorder-KXBTC15M-prod-20260919T000000Z.sqlite").write_bytes(b"")
        kinds = {e["name"].split("-")[0]: e["kind"] for e in list_databases(tmp_path)}
        assert kinds == {"stream": "stream", "recorder": "recorder"}


class TestStrategyLabEndpoints:
    def _seed(self, dashboard, n=12):
        from test_lab import alternating, seed_window

        db_path = make_db(dashboard.data_dir, "paper-KXBTC15M-prod-20260919T000000Z.sqlite")
        conn = sqlite3.connect(str(db_path))
        for i, result in enumerate(alternating(n)):
            seed_window(conn, i, result)
        conn.close()
        return db_path.name

    def _wait(self, dashboard, job_id, timeout=30):
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            status, body = _get(dashboard.base_url, f"/api/lab/status?id={job_id}")
            assert status == 200
            if body["state"] != "running":
                return body
            time.sleep(0.05)
        raise AssertionError("lab run did not finish")

    def test_defaults_list_the_tunable_parameters(self, dashboard):
        status, body = _get(dashboard.base_url, "/api/lab/defaults")
        assert status == 200 and "min_edge" in body["tunable"] and "risk_pct" in body["tunable"]

    def test_defaults_account_matches_the_configured_shipped_defaults(self, dashboard):
        # dashboard's own config.yaml is just "mode: paper", so this is BotConfig()'s own code defaults --
        # the dashboard's "account size" field must never just be a hardcoded guess of its own.
        status, body = _get(dashboard.base_url, "/api/lab/defaults")
        assert status == 200
        assert body["account"] == {"account_usd": "500", "max_exposure_pct": "5", "daily_loss_pct": "4"}

    def test_defaults_account_follows_a_custom_configured_account_size(self, dashboard):
        dashboard.config_path.write_text(
            "mode: paper\nsizing:\n  account_usd: 1234\nrisk:\n  max_open_exposure_pct: 12\n  daily_loss_limit_pct: 8\n",
            encoding="utf-8",
        )
        status, body = _get(dashboard.base_url, "/api/lab/defaults")
        assert status == 200
        assert body["account"] == {"account_usd": "1234", "max_exposure_pct": "12", "daily_loss_pct": "8"}

    def test_defaults_account_falls_back_when_the_percent_caps_are_unset(self, dashboard):
        # sizing.mode "fixed"/"ramp"/"kelly" may leave the two account-relative risk caps off (null) since
        # they are inert there; lab_defaults() then has no live percentage to mirror and uses the lab's own
        # AccountSettings research defaults instead of crashing or showing a blank/None field.
        dashboard.config_path.write_text(
            "mode: paper\nrisk:\n  max_open_exposure_pct: null\n  daily_loss_limit_pct: null\n", encoding="utf-8",
        )
        status, body = _get(dashboard.base_url, "/api/lab/defaults")
        assert status == 200
        assert body["account"] == {"account_usd": "500", "max_exposure_pct": "25", "daily_loss_pct": "10"}

    def test_preview_counts_combinations_and_windows(self, dashboard):
        name = self._seed(dashboard)
        status, body = _post(dashboard.base_url, "/api/lab/preview",
                             {"dbs": [name], "grid": {"min_edge": "0.02, 0.04", "max_price": "none, 0.6", "trend_mode": ""}})
        assert status == 200 and body == {"combinations": 4, "windows": 12}

    def test_bad_grid_values_are_a_400_not_a_crash(self, dashboard):
        name = self._seed(dashboard)
        for grid in ({"min_edge": "banana"}, {"bogus": "1"}, {}, {"min_edge": "5"}):
            status, body = _post(dashboard.base_url, "/api/lab/start", {"dbs": [name], "grid": grid})
            assert status == 400 and "error" in body

    def test_start_requires_a_real_data_file(self, dashboard):
        assert _post(dashboard.base_url, "/api/lab/start", {"dbs": [], "grid": {"min_edge": "0.02"}})[0] == 400
        assert _post(dashboard.base_url, "/api/lab/start", {"dbs": ["../evil.sqlite"], "grid": {"min_edge": "0.02"}})[0] == 404

    def test_a_run_completes_with_a_report(self, dashboard):
        name = self._seed(dashboard)
        status, job = _post(dashboard.base_url, "/api/lab/start", {
            "dbs": [name], "grid": {"min_edge": "0.02, 0.04", "max_price": "none, 0.2"}, "min_train_trades": 3,
            "account_usd": "1000", "split": "0.7",
        })
        assert status == 200 and job["state"] == "running"
        done = self._wait(dashboard, job["id"])
        assert done["state"] == "done", done
        report = done["report"]
        assert report["combinations"] == 4 and report["account_usd"] == "1000"
        assert report["verdict_level"] in {"insufficient", "not_supported", "weak_signal"}
        assert report["rows"] and {"train", "test", "description"} <= set(report["rows"][0])

    def test_too_little_data_is_reported_as_an_error_state(self, dashboard):
        from test_lab import seed_window

        db_path = make_db(dashboard.data_dir, "paper-KXBTC15M-prod-20260919T010000Z.sqlite")
        conn = sqlite3.connect(str(db_path))
        seed_window(conn, 0, "yes")
        conn.close()
        _, job = _post(dashboard.base_url, "/api/lab/start", {"dbs": [db_path.name], "grid": {"min_edge": "0.02"}})
        done = self._wait(dashboard, job["id"])
        assert done["state"] == "error" and "at least 6" in done["error"]

    def test_unknown_run_id_is_404(self, dashboard):
        assert _get(dashboard.base_url, "/api/lab/status?id=nope")[0] == 404


class TestDemoOrdersView:
    def _demo_db(self, dashboard, rows):
        from btcbot.demo_trader import DEMO_SCHEMA

        db_path = make_db(dashboard.data_dir, "demo-KXBTC15M-demo-20260919T000000Z.sqlite")
        conn = sqlite3.connect(str(db_path))
        conn.executescript(DEMO_SCHEMA)
        for r in rows:
            conn.execute(
                """INSERT INTO demo_orders (ticker, side, price, size, placed_ts, order_id, demo_filled, demo_cost, demo_fee,
                   paper_filled, paper_cost, paper_fee, closed_ts, result, demo_pnl, paper_pnl)
                   VALUES (:ticker,:side,:price,:size,:placed_ts,:order_id,:demo_filled,:demo_cost,:demo_fee,:paper_filled,
                   :paper_cost,:paper_fee,:closed_ts,:result,:demo_pnl,:paper_pnl)""", r)
        conn.execute("INSERT INTO demo_events (ts, ticker, event, detail) VALUES (?,?,?,?)",
                     (T0.isoformat(), TICKER, "order_rejected", "yes 5@0.30: post only cross"))
        conn.commit()
        conn.close()
        return db_path.name

    def row(self, oid, **over):
        base = {"ticker": TICKER, "side": "yes", "price": "0.30", "size": "5", "placed_ts": T0.isoformat(), "order_id": oid,
                "demo_filled": "0", "demo_cost": "0", "demo_fee": "0", "paper_filled": "0", "paper_cost": "0", "paper_fee": "0",
                "closed_ts": None, "result": None, "demo_pnl": None, "paper_pnl": None}
        return {**base, **over}

    def test_states_summary_and_events(self, dashboard):
        name = self._demo_db(dashboard, [
            self.row("o1"),                                                                        # resting
            self.row("o2", demo_filled="5", demo_cost="1.50", paper_filled="4", paper_cost="1.20"),  # filled
            self.row("o3", closed_ts=T0.isoformat()),                                              # cancelled unfilled
            self.row("o4", demo_filled="4", demo_cost="1.20", demo_fee="0.01", paper_filled="4", paper_cost="1.20",
                     closed_ts=T0.isoformat(), result="yes", demo_pnl="2.7900", paper_pnl="2.8000"),  # settled
        ])
        status, body = _get(dashboard.base_url, f"/api/demo?db={name}")
        assert status == 200
        states = {o["order_id"]: o["state"] for o in body["orders"]}
        assert states == {"o1": "resting", "o2": "filled", "o3": "cancelled, unfilled", "o4": "settled YES"}
        assert next(o for o in body["orders"] if o["order_id"] == "o2")["demo_avg_price"] == "0.30"
        s = body["summary"]
        assert s["placed"] == 4 and s["filled_on_demo"] == 2 and s["filled_in_paper"] == 2 and s["rejected"] == 1
        assert s["demo_pnl"] == "2.7900" and s["paper_pnl"] == "2.8000" and s["demo_fees"] == "0.01"
        assert body["events"][0]["event"] == "order_rejected"

    def test_the_ledger_tail_is_newest_first_and_skips_garbage(self, dashboard):
        name = self._demo_db(dashboard, [])
        lines = [json.dumps({"ts": f"2026-09-19T00:00:0{i}+00:00", "event": "create_order", "side": "yes"}) for i in range(3)]
        (dashboard.data_dir / "order-audit.jsonl").write_text("\n".join([lines[0], "not json {", lines[1], lines[2]]) + "\n", encoding="utf-8")
        _, body = _get(dashboard.base_url, f"/api/demo?db={name}")
        assert [a["ts"][17:19] for a in body["audit"]] == ["02", "01", "00"]

    def test_a_database_without_demo_tables_and_a_missing_ledger_are_just_empty(self, dashboard):
        db_path = make_db(dashboard.data_dir)  # a plain paper/recorder database
        status, body = _get(dashboard.base_url, f"/api/demo?db={db_path.name}")
        assert status == 200 and body["orders"] == [] and body["events"] == [] and body["audit"] == []
        assert body["summary"]["placed"] == 0

    def test_unknown_database_is_a_404(self, dashboard):
        assert _get(dashboard.base_url, "/api/demo?db=missing.sqlite")[0] == 404


def test_market_spot_is_bounded_to_selected_window(tmp_path):
    from btcbot.webui import market_view
    db = make_db(tmp_path)
    with sqlite3.connect(db) as conn:
        insert_market(conn, TICKER, strike=80000, close_time=T0 + timedelta(seconds=3), open_time=T0)
        insert_snapshot(conn, TICKER, T0)
        insert_spot_run(conn, T0, 10)
    view = market_view(db, TICKER)
    assert len(view['spot_series']) == 4
    assert view['spot_ts'] == (T0 + timedelta(seconds=3)).isoformat()
    assert view['book_latency_ms'] == 10.0

def test_fast_quote_matches_window_and_excludes_history(tmp_path):
    from btcbot.webui import market_quote
    db = make_db(tmp_path)
    with sqlite3.connect(db) as conn:
        insert_market(conn, TICKER, strike=80000, close_time=T0 + timedelta(seconds=3), open_time=T0)
        insert_snapshot(conn, TICKER, T0, yes=[('0.55', '8')])
        insert_spot_run(conn, T0, 10)
    q = market_quote(db, TICKER)
    assert q['book']['yes'] == [['0.55', '8']]
    assert q['spot_ts'] == (T0 + timedelta(seconds=3)).isoformat()
    assert 'spot_series' not in q
    assert market_quote(db, 'unknown')['book_ts'] is None
