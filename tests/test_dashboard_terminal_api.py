import time
import sqlite3
from decimal import Decimal

import pytest

from btcbot.coinbase_history import Candle
from btcbot.history_pipeline import init_history_schema, save_candles
from test_perp import random_walk_bars
from test_webui import dashboard, _get, _post, make_db, seed_fillable_window, TICKER, T0


def test_portfolio_http_reports_reference_capital_without_implying_exchange_balance(dashboard):
    db = make_db(dashboard.data_dir)
    status, body = _get(dashboard.base_url, f"/api/portfolio?db={db.name}&starting_balance=1000")
    assert status == 200
    assert body["equity_usd"] == "1000"
    assert body["growth_pct"] == "0"
    assert body["provenance"]["balance_basis"] == "supplied_assumption"
    assert body["active_orders"] == []


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "oops"])
def test_portfolio_rejects_invalid_capital(dashboard, value):
    db = make_db(dashboard.data_dir)
    status, _ = _get(dashboard.base_url, f"/api/portfolio?db={db.name}&starting_balance={value}")
    assert status == 400


def test_portfolio_history_http_merges_across_files_of_one_kind(dashboard):
    make_db(dashboard.data_dir, "paper-KXBTC15M-prod-20260919T000000Z.sqlite")
    make_db(dashboard.data_dir, "paper-KXBTC15M-prod-20260920T000000Z.sqlite")
    status, body = _get(dashboard.base_url, "/api/portfolio_history?kind=paper")
    assert status == 200
    assert body["kind"] == "paper"
    assert body["run_files"] == 2
    assert body["trades"] == [] and body["versions"] == [] and body["segments"] == []


def test_portfolio_history_http_rejects_prod_and_missing_kind(dashboard):
    status, body = _get(dashboard.base_url, "/api/portfolio_history?kind=prod")
    assert status == 400 and "kind must be" in body["error"]
    status, body = _get(dashboard.base_url, "/api/portfolio_history")
    assert status == 400 and "kind must be" in body["error"]


def test_backtest_http_job_completes_all_scenarios_and_has_stable_cached_result(dashboard):
    db = make_db(dashboard.data_dir)
    with sqlite3.connect(db) as conn:
        seed_fillable_window(conn, TICKER, start_ts=T0)
    payload = {"db": db.name, "queue": "both", "maker_fee_multiplier": "both"}
    status, job = _post(dashboard.base_url, "/api/backtest/start", payload)
    assert status == 202
    for _ in range(100):
        status, job = _get(dashboard.base_url, f"/api/backtest/status?id={job['id']}")
        if job["state"] in ("completed", "error"):
            break
        time.sleep(.01)
    assert status == 200
    assert job["state"] == "completed", job
    assert job["done"] == job["total"] == len(job["reports"]) == 4
    status, cached = _post(dashboard.base_url, "/api/backtest/start", payload)
    assert status == 202 and cached["id"] == job["id"] and cached["cached"]
    assert _get(dashboard.base_url, "/api/backtest/status?id=missing")[0] == 404


def test_backtest_http_rejects_path_escape_and_nonfinite_fee(dashboard):
    db = make_db(dashboard.data_dir)
    assert _post(dashboard.base_url, "/api/backtest/start", {"db": "../escape.sqlite"})[0] == 404
    assert _post(dashboard.base_url, "/api/backtest/start", {"db": db.name, "maker_fee_multiplier": "NaN"})[0] == 400


def _make_history_db(data_dir, name="history-KXBTC15M-prod-20260101T000000Z.sqlite", days=3):
    db_path = data_dir / name
    conn = sqlite3.connect(str(db_path))
    init_history_schema(conn)
    save_candles(conn, [Candle(b.ts, b.low, b.high, b.open, b.close, Decimal(1)) for b in random_walk_bars(days)])
    conn.close()
    return db_path


def test_perp_backtest_http_runs_a_job_and_the_saved_report_matches_a_reload(dashboard):
    db = _make_history_db(dashboard.data_dir)
    status, dbs = _get(dashboard.base_url, "/api/perp/databases")
    assert status == 200 and dbs[0]["name"] == db.name and "Coinbase" in dbs[0]["source"]

    status, reports = _get(dashboard.base_url, "/api/perp/reports")
    assert status == 200 and reports == []

    payload = {"db": db.name, "strategies": ["flat"], "leverages": ["1"], "fundings": ["0"]}
    status, job = _post(dashboard.base_url, "/api/perp/start", payload)
    assert status == 202
    for _ in range(200):
        status, job = _get(dashboard.base_url, f"/api/perp/status?id={job['id']}")
        if job["state"] in ("completed", "error"):
            break
        time.sleep(.01)
    assert status == 200 and job["state"] == "completed", job
    assert job["report"]["rows"]

    status, reports = _get(dashboard.base_url, "/api/perp/reports")
    assert status == 200 and len(reports) == 1
    status, full = _get(dashboard.base_url, f"/api/perp/report?name={reports[0]['name']}")
    assert status == 200 and full == job["report"]

    assert _get(dashboard.base_url, "/api/perp/report?name=../escape.json")[0] == 404
    assert _get(dashboard.base_url, "/api/perp/status?id=missing")[0] == 404


def test_perp_backtest_http_rejects_path_escape_bad_strategy_and_high_leverage(dashboard):
    db = dashboard.data_dir / "recorder.sqlite"
    sqlite3.connect(str(db)).close()
    assert _post(dashboard.base_url, "/api/perp/start", {"db": "../escape.sqlite"})[0] == 404
    assert _post(dashboard.base_url, "/api/perp/start", {"db": db.name, "strategies": ["bogus"]})[0] == 400
    assert _post(dashboard.base_url, "/api/perp/start", {"db": db.name, "leverages": ["10"]})[0] == 400
