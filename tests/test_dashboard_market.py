import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from btcbot.dashboard_market import market_quote, market_view, order_book_depth
from btcbot.recorder import Recorder

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def make_db(tmp_path):
    path = tmp_path / "market.sqlite"
    Recorder(None, series_ticker="KXBTC15M", db_path=path).close()
    return path


def snapshot(conn, ticker, stamp, book=None):
    conn.execute(
        "INSERT INTO orderbook_snapshots(ticker,request_started_ts,poll_ts,latency_ms,"
        "yes_bid_price,yes_ask_price,book_json) VALUES(?,?,?,?,?,?,?)",
        (ticker, stamp, stamp, 12.5, "0.40", "0.45", json.dumps(book or {
            "yes": [["0.40", "10"]], "no": [["0.55", "20"]]})),
    )


def market(conn, ticker, start, end):
    conn.execute(
        "INSERT INTO market_state(ticker,event_ticker,poll_ts,status,strike,open_time,close_time) "
        "VALUES(?,?,?,?,?,?,?)", (ticker, ticker, start.isoformat(), "active", "80000", start.isoformat(), end.isoformat()),
    )


def test_depth_uses_implied_asks_and_cumulative_executable_liquidity():
    depth = order_book_depth({
        "yes": [["0.35", "10"], ["0.40", "5"], ["0.99", "0"]],
        "no": [["0.50", "12"], ["0.55", "8"]],
    })
    assert [r["price"] for r in depth["bids"]] == [0.40, 0.35]
    assert [r["price"] for r in depth["asks"]] == [0.45, 0.50]
    assert [r["cumulative_size"] for r in depth["asks"]] == [8, 20]
    assert depth["asks"][-1]["cumulative_notional"] == 9.6
    assert depth["best_bid"] == 0.40 and depth["best_ask"] == 0.45
    assert depth["spread"] == 0.05 and depth["spread_cents"] == 5
    assert depth["mid"] == 0.425
    assert depth["imbalance"] == pytest.approx(-5 / 35)
    assert depth["microprice"] == pytest.approx((0.45 * 5 + 0.40 * 8) / 13)
    assert depth["ask_source"] == "implied_from_no_bids"


def test_depth_handles_one_sided_empty_and_invalid_levels():
    depth = order_book_depth({"yes": [["0.4", "2"], ["0.4", "3"], ["NaN", "1"],
                                             ["0.2", "-1"], ["bad"], ["1.1", "2"]], "no": []})
    assert depth["bid_size"] == 5 and len(depth["bids"]) == 1
    assert depth["invalid_levels"] == 4
    assert depth["best_ask"] is None and depth["spread"] is None and depth["mid"] is None
    assert depth["imbalance"] == 1
    empty = order_book_depth({})
    assert empty["imbalance"] is None and empty["bid_size"] == 0
    assert order_book_depth({"yes": [[".8", "2"]], "no": [[".4", "3"]]})["crossed"]


def test_quote_reports_receipt_age_and_keeps_spot_in_selected_window(tmp_path):
    path = make_db(tmp_path)
    with sqlite3.connect(path) as conn:
        market(conn, "A", NOW - timedelta(minutes=15), NOW)
        snapshot(conn, "A", (NOW - timedelta(milliseconds=123)).isoformat())
        # Late insertion of an older response must not replace the newest quote.
        snapshot(conn, "A", (NOW - timedelta(seconds=3)).isoformat())
        for offset in (-100, 1000):
            stamp = (NOW + timedelta(milliseconds=offset)).isoformat()
            conn.execute("INSERT INTO spot_ticks(source,price,receive_ts,monotonic_ts) VALUES(?,?,?,?)",
                         ("coinbase-ws", str(80000 + offset), stamp, offset))
    quote = market_quote(path, "A", now=NOW)
    assert quote["book_age_ms"] == 123
    assert quote["spot_age_ms"] == 100
    assert quote["book_latency_ms"] == 12.5
    assert quote["spot"] == "79900"
    assert not quote["book_stale"]
    assert quote["timing"]["exchange_clock_synchronized"] is False
    assert "spot_series" not in quote and "trades" not in quote
    assert market_quote(path, "A", now=NOW + timedelta(seconds=5))["book_stale"]
    assert market_quote(path, "A", now=NOW - timedelta(seconds=5))["book_age_ms"] < 0


def test_empty_and_legacy_recordings_return_safe_payloads(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as conn:
        pass
    assert market_quote(path, "unknown", now=NOW)["book_ts"] is None
    assert market_view(path)["ticker"] is None
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE orderbook_snapshots(ticker TEXT,poll_ts TEXT,book_json TEXT)")
        conn.execute("INSERT INTO orderbook_snapshots VALUES(?,?,?)", ("A", NOW.isoformat(), "not json"))
    quote = market_quote(path, "A", now=NOW)
    assert quote["book"] == {"yes": [], "no": []}
    assert quote["book_error"] and quote["book_latency_ms"] is None
    view = market_view(path, "A", now=NOW)
    assert view["status"] is None and view["settlement"] is None and view["trades"] == []
    assert view["mid_series"] == [[NOW.isoformat(), None, None]]


def test_reader_does_not_create_missing_database(tmp_path):
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        market_quote(missing, "A")
    assert not missing.exists()


def test_history_samples_entire_window_with_hard_sql_bound(tmp_path):
    path = make_db(tmp_path)
    end = NOW + timedelta(seconds=1999)
    with sqlite3.connect(path) as conn:
        market(conn, "A", NOW, end)
        for i in range(2000):
            stamp = (NOW + timedelta(seconds=i)).isoformat()
            snapshot(conn, "A", stamp)
            conn.execute("INSERT INTO spot_ticks(source,price,receive_ts,monotonic_ts) VALUES(?,?,?,?)",
                         ("coinbase-ws", str(80000 + i), stamp, i))
    view = market_view(path, "A", now=end, history_limit=40)
    assert len(view["mid_series"]) == 40 and len(view["spot_series"]) == 40
    assert view["mid_series"][0][0] == NOW.isoformat()
    assert view["mid_series"][-1][0] == end.isoformat()
    assert view["spot_series"][0] == [NOW.isoformat(), "80000"]
    assert view["spot_series"][-1] == [end.isoformat(), "81999"]
    assert view["history"]["mid_sampled"] and view["history"]["spot_sampled"]
    assert len(market_view(path, "A", history_limit=100000)["mid_series"]) <= 400


def test_fast_quote_has_constant_query_work_as_history_grows(tmp_path, monkeypatch):
    path = make_db(tmp_path)
    with sqlite3.connect(path) as conn:
        market(conn, "A", NOW, NOW + timedelta(minutes=15))
        row = ("A", NOW.isoformat(), NOW.isoformat(), 10, '{"yes":[],"no":[]}')
        conn.executemany("INSERT INTO orderbook_snapshots(ticker,request_started_ts,poll_ts,latency_ms,book_json) "
                         "VALUES(?,?,?,?,?)", [row] * 25000)
    original_connect = sqlite3.connect
    callbacks = 0

    def bounded_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)

        def budget():
            nonlocal callbacks
            callbacks += 1
            # A scan/sort of 25,000 snapshots exceeds this VM budget; three
            # current-record index seeks and schema reads finish comfortably.
            return int(callbacks > 500)

        connection.set_progress_handler(budget, 100)
        return connection

    monkeypatch.setattr(sqlite3, "connect", bounded_connect)
    assert market_quote(path, "A", now=NOW)["book_ts"] == NOW.isoformat()
    assert callbacks < 500


def test_window_list_and_selected_trade_limit(tmp_path):
    path = make_db(tmp_path)
    with sqlite3.connect(path) as conn:
        from btcbot.backtest import init_trades_schema
        init_trades_schema(conn)
        for name, offset in (("A", 0), ("B", 1)):
            start = NOW + timedelta(minutes=15 * offset)
            market(conn, name, start, start + timedelta(minutes=15))
            snapshot(conn, name, start.isoformat())
        conn.executemany(
            "INSERT INTO trades(ticker,side,size,entry_price,entry_ts,fee_paid,p_side_at_entry) "
            "VALUES(?,?,?,?,?,?,?)", [("A", "yes", "1", ".4", NOW.isoformat(), "0", .5)] * 205)
    view = market_view(path, "A", now=NOW)
    assert view["tickers"] == ["B", "A"]
    assert len(view["trades"]) == 200 and view["trades_truncated"]
    assert all(row["ticker"] == "A" for row in view["trades"])
    assert view["windows"][1]["trades"] == 205
    assert market_view(path, "unknown")["ticker"] == "B"
