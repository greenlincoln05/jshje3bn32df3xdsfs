"""Offline checks for dashboard scheduling, recorded-data consistency, and caching."""

import json
import sqlite3
import threading
import time
from dataclasses import asdict
from decimal import Decimal

import pytest

from btcbot import dashboard_jobs
from btcbot.backtest import build_report, run_backtest
from btcbot.config import BotConfig
from btcbot.dashboard_jobs import BacktestJobs, BacktestQueueFull
from btcbot.paper_broker import QueueAssumption
from btcbot.recorder import Recorder
from test_backtest import T0, TICKER, insert_settlement, seed_fillable_window


@pytest.fixture
def inputs(tmp_path):
    db_path = tmp_path / "recording with spaces.sqlite"
    Recorder(None, series_ticker="KXBTC15M", db_path=db_path).close()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO orderbook_snapshots (ticker, request_started_ts, poll_ts, latency_ms, book_json) VALUES (?,?,?,?,?)",
        ("KXBTC15M-TEST", "2026-09-20T00:00:00+00:00", "2026-09-20T00:00:00+00:00", 1.0,
         json.dumps({"yes": [["0.5", "10"]], "no": [["0.4", "12"]]})),
    )
    conn.commit()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}", encoding="utf-8")
    yield db_path, config_path, conn
    conn.close()


def wait_finished(jobs, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snapshot = jobs.get(job_id)
        if snapshot["state"] in ("completed", "error"):
            return snapshot
        time.sleep(0.005)
    pytest.fail("background job did not finish")


def empty_report(conn, config, *, queue_assumption, maker_fee_multiplier):
    return build_report(
        [], windows_seen=1, windows_traded=set(), first_ts=None, last_ts=None,
        queue_assumption=queue_assumption, maker_fee_multiplier=maker_fee_multiplier,
    )


def test_real_replay_matches_existing_backtest_and_compares_all_scenarios(inputs):
    db_path, config_path, conn = inputs
    conn.execute("DELETE FROM orderbook_snapshots")
    close_time = seed_fillable_window(conn, TICKER, start_ts=T0)
    insert_settlement(conn, TICKER, "yes", strike=Decimal("80000"), close_time=close_time)
    jobs = BacktestJobs()
    try:
        accepted = jobs.submit(db_path, config_path)
        assert accepted["state"] == "queued"
        done = wait_finished(jobs, accepted["id"])
        assert done["state"] == "completed"
        assert done["done"] == done["total"] == 4
        expected = [
            asdict(run_backtest(conn, BotConfig(), queue_assumption=queue, maker_fee_multiplier=fee))
            for queue in QueueAssumption for fee in (Decimal(0), Decimal("0.25"))
        ]
        assert done["reports"] == expected
        assert done["reports"][0]["trades"] == 1
        assert done["reports"][1]["total_pnl_usd"] < done["reports"][0]["total_pnl_usd"]
        assert done["reports"][2]["trades"] == 0
        assert done["run_elapsed_sec"] >= 0
        assert done["elapsed_sec"] >= done["run_elapsed_sec"]
        assert jobs.get(accepted["id"])["elapsed_sec"] == done["elapsed_sec"]
        json.dumps(done, default=str)
        done["reports"][0]["trades"] = -99
        assert jobs.get(accepted["id"])["reports"][0]["trades"] == 1
    finally:
        jobs.close(wait=True)


def test_worker_bound_queue_limit_and_inflight_deduplication(inputs, monkeypatch):
    db_path, config_path, _ = inputs
    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocked(conn, config, **kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(5)
        return empty_report(conn, config, **kwargs)

    monkeypatch.setattr(dashboard_jobs, "run_backtest", blocked)
    jobs = BacktestJobs(max_workers=1, max_pending=1)
    try:
        first = jobs.submit(db_path, config_path, queue="optimistic", maker_fee_multiplier="0")
        assert entered.wait(2)
        assert jobs.get(first["id"])["state"] == "running"
        duplicate = jobs.submit(db_path, config_path, queue="optimistic", maker_fee_multiplier="0.00")
        assert duplicate["id"] == first["id"]
        assert not duplicate["cached"]
        queued = jobs.submit(db_path, config_path, queue="pessimistic", maker_fee_multiplier="0")
        assert queued["state"] == "queued"
        assert queued["run_elapsed_sec"] == 0
        assert len(calls) == 1
        with pytest.raises(BacktestQueueFull):
            jobs.submit(db_path, config_path, maker_fee_multiplier="0.25")
        release.set()
        assert wait_finished(jobs, first["id"])["state"] == "completed"
        assert wait_finished(jobs, queued["id"])["state"] == "completed"
        assert len(calls) == 2
    finally:
        release.set()
        jobs.close(wait=True)


def test_progress_counts_finished_scenarios_and_all_share_read_only_snapshot(inputs, monkeypatch):
    db_path, config_path, writer = inputs
    paused, release = threading.Event(), threading.Event()
    seen_rows = []

    def observed(conn, config, **kwargs):
        seen_rows.append(conn.execute("SELECT COUNT(*) FROM orderbook_snapshots").fetchone()[0])
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM orderbook_snapshots")
        if len(seen_rows) == 2:
            paused.set()
            assert release.wait(5)
        return empty_report(conn, config, **kwargs)

    monkeypatch.setattr(dashboard_jobs, "run_backtest", observed)
    jobs = BacktestJobs()
    try:
        accepted = jobs.submit(db_path, config_path)
        assert paused.wait(2)
        snapshot = jobs.get(accepted["id"])
        assert snapshot["state"] == "running"
        assert (snapshot["done"], snapshot["total"]) == (1, 4)
        assert "Scenario 2/4" in snapshot["label"]
        writer.execute("DELETE FROM orderbook_snapshots")
        writer.commit()
        release.set()
        assert wait_finished(jobs, accepted["id"])["state"] == "completed"
        assert seen_rows == [1, 1, 1, 1]
    finally:
        release.set()
        jobs.close(wait=True)


def test_completed_cache_invalidates_for_wal_commits_and_config_changes(inputs):
    db_path, config_path, writer = inputs
    jobs = BacktestJobs()
    try:
        first = jobs.submit(db_path, config_path)
        wait_finished(jobs, first["id"])
        cached = jobs.submit(db_path, config_path)
        assert cached["id"] == first["id"]
        assert cached["cached"] is True
        main_mtime = db_path.stat().st_mtime_ns
        writer.execute("UPDATE orderbook_snapshots SET latency_ms = 2")
        writer.commit()
        assert db_path.stat().st_mtime_ns == main_mtime
        after_wal = jobs.submit(db_path, config_path)
        assert after_wal["id"] != first["id"]
        assert not after_wal["cached"]
        wait_finished(jobs, after_wal["id"])
        config_path.write_text("min_edge: 0.05\n", encoding="utf-8")
        after_config = jobs.submit(db_path, config_path)
        assert after_config["id"] != after_wal["id"]
        assert wait_finished(jobs, after_config["id"])["state"] == "completed"
    finally:
        jobs.close(wait=True)


def test_failure_is_visible_releases_capacity_and_can_be_retried(inputs, monkeypatch):
    db_path, config_path, _ = inputs

    def broken(*args, **kwargs):
        raise RuntimeError("malformed recording")

    monkeypatch.setattr(dashboard_jobs, "run_backtest", broken)
    jobs = BacktestJobs(max_pending=0)
    try:
        first = jobs.submit(db_path, config_path)
        failed = wait_finished(jobs, first["id"])
        assert failed["state"] == "error"
        assert failed["error"] == "RuntimeError: malformed recording"
        assert failed["done"] == 0
        assert jobs.get(first["id"])["elapsed_sec"] == failed["elapsed_sec"]
        monkeypatch.setattr(dashboard_jobs, "run_backtest", empty_report)
        retried = jobs.submit(db_path, config_path)
        assert retried["id"] != first["id"]
        assert wait_finished(jobs, retried["id"])["state"] == "completed"
    finally:
        jobs.close(wait=True)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "-0.1", "hello"])
def test_invalid_fees_fail_before_starting_a_job(inputs, value):
    db_path, config_path, _ = inputs
    jobs = BacktestJobs()
    with pytest.raises(ValueError, match="finite non-negative"):
        jobs.submit(db_path, config_path, maker_fee_multiplier=value)
    jobs.close(wait=True)


def test_history_is_bounded_unknown_ids_return_none_and_closed_pool_rejects_work(inputs):
    db_path, config_path, _ = inputs
    jobs = BacktestJobs(max_history=1)
    try:
        first = jobs.submit(db_path, config_path, queue="optimistic", maker_fee_multiplier="0")
        wait_finished(jobs, first["id"])
        second = jobs.submit(db_path, config_path, queue="pessimistic", maker_fee_multiplier="0")
        wait_finished(jobs, second["id"])
        assert jobs.get(first["id"]) is None
        assert jobs.get("not-a-job") is None
        assert jobs.get(second["id"])["state"] == "completed"
    finally:
        jobs.close(wait=True)
    with pytest.raises(ValueError, match="closed"):
        jobs.submit(db_path, config_path)


def test_invalid_config_becomes_visible_error(inputs):
    db_path, config_path, _ = inputs
    config_path.write_text("not_a_setting: true\n", encoding="utf-8")
    jobs = BacktestJobs()
    try:
        accepted = jobs.submit(db_path, config_path)
        failed = wait_finished(jobs, accepted["id"])
        assert failed["state"] == "error"
        assert "ConfigError" in failed["error"]
        assert failed["reports"] == []
    finally:
        jobs.close(wait=True)
