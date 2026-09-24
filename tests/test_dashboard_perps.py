"""Offline tests for the dashboard's Perps (paper) tab support (btcbot.dashboard_perps). No network, no key,
same guarantees as btcbot.perp_backtest itself; see test_perp.py for the engine's own tests."""

import json
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

import pytest

from btcbot.coinbase_history import Candle
from btcbot.dashboard_perps import PerpJobs, list_perp_databases, list_perp_reports, read_perp_report
from btcbot.history_pipeline import init_history_schema, save_candles
from test_perp import random_walk_bars


def d(x):
    return Decimal(str(x))


def _history_db(data_dir, name="history-KXBTC15M-prod-20260101T000000Z.sqlite", days=3):
    path = data_dir / name
    conn = sqlite3.connect(str(path))
    init_history_schema(conn)
    save_candles(conn, [Candle(b.ts, b.low, b.high, b.open, b.close, d(1)) for b in random_walk_bars(days)])
    conn.close()
    return path


def _wait(jobs, job_id, *, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = jobs.get(job_id)
        if job["state"] in ("completed", "error"):
            return job
        time.sleep(0.01)
    pytest.fail("perp backtest job did not finish")


class TestListPerpDatabases:
    def test_finds_a_download_history_database_and_skips_an_unrelated_file(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = _history_db(data_dir)
        sqlite3.connect(str(data_dir / "unrelated.sqlite")).close()  # a real sqlite file, but no usable tables
        entries = list_perp_databases(data_dir)
        assert [e["name"] for e in entries] == [db_path.name]
        assert "Coinbase" in entries[0]["source"]

    def test_missing_data_dir_is_empty(self, tmp_path):
        assert list_perp_databases(tmp_path / "nope") == []


class TestListAndReadPerpReports:
    def test_lists_only_perp_backtest_reports_and_skips_broken_json(self, tmp_path):
        data_dir = tmp_path / "data"
        research = data_dir / "research"
        research.mkdir(parents=True)
        (research / "perp-backtest-20260101T000000Z.json").write_text(
            json.dumps({"source": "x", "bars": 10, "train_end": "2026-01-01T00:00:00"}), encoding="utf-8"
        )
        (research / "perp-backtest-broken.json").write_text("{not json", encoding="utf-8")
        (research / "pm-reaction-20260101T000000Z.json").write_text(json.dumps({"source": "y"}), encoding="utf-8")
        entries = list_perp_reports(data_dir)
        assert [e["name"] for e in entries] == ["perp-backtest-20260101T000000Z.json"]
        assert entries[0]["bars"] == 10

    def test_missing_research_dir_is_empty(self, tmp_path):
        assert list_perp_reports(tmp_path / "data") == []

    def test_read_rejects_path_traversal_and_reports_from_other_commands(self, tmp_path):
        data_dir = tmp_path / "data"
        research = data_dir / "research"
        research.mkdir(parents=True)
        (research / "perp-backtest-ok.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
        (research / "pm-reaction-x.json").write_text(json.dumps({"ok": False}), encoding="utf-8")
        with pytest.raises(FileNotFoundError):
            read_perp_report(data_dir, "../escape.json")
        with pytest.raises(FileNotFoundError):
            read_perp_report(data_dir, "pm-reaction-x.json")
        assert read_perp_report(data_dir, "perp-backtest-ok.json") == {"ok": True}


class TestPerpJobs:
    def test_end_to_end_writes_a_report_that_shows_up_in_the_listing(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = _history_db(data_dir)
        jobs = PerpJobs()
        try:
            job = jobs.submit(db_path, data_dir, {"strategies": ["flat"], "leverages": ["1"], "fundings": ["0"]})
            assert job["state"] == "queued"
            job = _wait(jobs, job["id"])
            assert job["state"] == "completed", job
            assert job["report"]["rows"] and job["report"]["bars"] > 0
            assert Path(job["report_path"]).is_file()
            reports = list_perp_reports(data_dir)
            assert len(reports) == 1 and reports[0]["name"] == Path(job["report_path"]).name
            assert read_perp_report(data_dir, reports[0]["name"]) == job["report"]
        finally:
            jobs.close(wait=True)

    def test_unknown_strategy_is_rejected_before_a_worker_slot_is_used(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = _history_db(data_dir)
        jobs = PerpJobs()
        try:
            with pytest.raises(ValueError):
                jobs.submit(db_path, data_dir, {"strategies": ["not_a_strategy"]})
            assert list_perp_reports(data_dir) == []  # no job was ever created, so nothing ran or was written
        finally:
            jobs.close(wait=True)

    def test_leverage_above_the_paper_cap_is_rejected(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = _history_db(data_dir)
        jobs = PerpJobs()
        try:
            with pytest.raises(ValueError):
                jobs.submit(db_path, data_dir, {"leverages": ["5"]})
        finally:
            jobs.close(wait=True)

    def test_a_database_with_no_usable_bars_fails_the_job_not_the_submit(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        empty_db = data_dir / "empty.sqlite"
        sqlite3.connect(str(empty_db)).close()
        jobs = PerpJobs()
        try:
            job = jobs.submit(empty_db, data_dir, {"strategies": ["flat"], "leverages": ["1"], "fundings": ["0"]})
            job = _wait(jobs, job["id"])
            assert job["state"] == "error" and "PerpBacktestError" in job["error"]
            assert list_perp_reports(data_dir) == []
        finally:
            jobs.close(wait=True)
