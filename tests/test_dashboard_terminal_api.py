import time
import sqlite3

import pytest

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
