"""Offline tests for dashboard_history.py: one merged, chronological trade history across every
paper-*.sqlite (or demo-*.sqlite) run in a data directory, segmented by which PR was live for each trade."""

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from btcbot.backtest import TRADES_SCHEMA
from btcbot.code_version import CodeVersion, init_code_version_schema, record_code_version
from btcbot.dashboard_history import portfolio_history
from btcbot.demo_trader import DEMO_SCHEMA

D = Decimal
T0 = datetime(2026, 9, 20, tzinfo=timezone.utc)


def ts(minutes=0):
    return (T0 + timedelta(minutes=minutes)).isoformat()


def make_run_db(tmp_path, name, *, demo=False):
    path = tmp_path / name
    conn = sqlite3.connect(path)
    conn.executescript(TRADES_SCHEMA)
    if demo:
        conn.executescript(DEMO_SCHEMA)
    conn.commit()
    conn.close()
    return path


def add_trade(path, *, ticker="A", size="4", price="0.30", fee="0.01", pnl="2.79", result="yes", minute=0):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO trades (ticker,side,size,entry_price,entry_ts,fee_paid,p_side_at_entry,result,pnl_usd) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ticker, "yes", size, price, ts(minute), fee, 0.5, result, pnl),
        )


def add_unresolved_trade(path, *, ticker="B", minute=0):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO trades (ticker,side,size,entry_price,entry_ts,fee_paid,p_side_at_entry,result,pnl_usd) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ticker, "yes", "4", "0.30", ts(minute), "0.01", 0.5, None, None),
        )


def add_demo_order(path, *, ticker="A", filled="4", price="0.30", fee="0.01", pnl="2.79", result="yes", minute=0):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO demo_orders (ticker,side,price,size,placed_ts,order_id,demo_filled,demo_cost,demo_fee,
               demo_first_fill_ts,closed_ts,result,demo_pnl,paper_filled,paper_pnl)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticker, "yes", price, filled, ts(minute), "order-" + ticker, filled, "1.20", fee,
             ts(minute), ts(minute + 1), result, pnl, filled, "0"),
        )


def add_unfilled_demo_order(path, *, ticker="B", minute=0):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO demo_orders (ticker,side,price,size,placed_ts,order_id,demo_filled,demo_cost,demo_fee,
               demo_first_fill_ts,closed_ts,result,demo_pnl,paper_filled,paper_pnl)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticker, "yes", "0.30", "4", ts(minute), "order-" + ticker, "0", "0", "0", None, None, None, None, "0", None),
        )


def add_version(path, *, pr_number, subject=None, minute=0, source="auto"):
    subject = subject or f"Merge pull request #{pr_number} from x/y"
    with sqlite3.connect(path) as conn:
        init_code_version_schema(conn)
        record_code_version(
            conn,
            CodeVersion(commit_hash=f"{pr_number:040x}", commit_subject=subject, pr_title=f"Change #{pr_number}",
                        commit_ts=T0 + timedelta(minutes=minute), pr_number=pr_number),
            source=source, recorded_ts=T0 + timedelta(minutes=minute),
        )


class TestKindValidation:
    def test_rejects_an_unknown_kind(self, tmp_path):
        with pytest.raises(ValueError, match="kind must be"):
            portfolio_history(tmp_path, "prod")


class TestEmptyAndMissingData:
    def test_an_empty_data_dir_has_no_trades_or_versions(self, tmp_path):
        result = portfolio_history(tmp_path, "paper")
        assert result == {"kind": "paper", "run_files": 0, "trades": [], "versions": [], "segments": []}

    def test_unresolved_trades_are_excluded(self, tmp_path):
        path = make_run_db(tmp_path, "paper-X-prod-20260920T000000Z.sqlite")
        add_unresolved_trade(path)
        result = portfolio_history(tmp_path, "paper")
        assert result["trades"] == []

    def test_never_filled_demo_orders_are_excluded(self, tmp_path):
        path = make_run_db(tmp_path, "demo-X-demo-20260920T000000Z.sqlite", demo=True)
        add_unfilled_demo_order(path)
        result = portfolio_history(tmp_path, "demo")
        assert result["trades"] == []


class TestMergingAcrossFiles:
    def test_trades_from_two_runs_are_merged_in_chronological_order(self, tmp_path):
        older = make_run_db(tmp_path, "paper-X-prod-20260920T000000Z.sqlite")
        newer = make_run_db(tmp_path, "paper-X-prod-20260920T010000Z.sqlite")
        add_trade(newer, ticker="LATER", minute=100)
        add_trade(older, ticker="EARLIER", minute=0)
        result = portfolio_history(tmp_path, "paper")
        assert [t["ticker"] for t in result["trades"]] == ["EARLIER", "LATER"]
        assert result["run_files"] == 2

    def test_paper_and_demo_are_independent_kinds(self, tmp_path):
        paper_path = make_run_db(tmp_path, "paper-X-prod-20260920T000000Z.sqlite")
        demo_path = make_run_db(tmp_path, "demo-X-demo-20260920T000000Z.sqlite", demo=True)
        add_trade(paper_path, ticker="PAPER-TRADE")
        add_demo_order(demo_path, ticker="DEMO-TRADE")
        paper_result = portfolio_history(tmp_path, "paper")
        demo_result = portfolio_history(tmp_path, "demo")
        assert [t["ticker"] for t in paper_result["trades"]] == ["PAPER-TRADE"]
        assert [t["ticker"] for t in demo_result["trades"]] == ["DEMO-TRADE"]

    def test_demo_uses_the_real_exchange_fill_not_the_shadow_paper_twin(self, tmp_path):
        path = make_run_db(tmp_path, "demo-X-demo-20260920T000000Z.sqlite", demo=True)
        add_demo_order(path, filled="3", pnl="1.11")
        result = portfolio_history(tmp_path, "demo")
        assert len(result["trades"]) == 1
        assert result["trades"][0]["size"] == D("3")
        assert result["trades"][0]["pnl_usd"] == D("1.11")


class TestVersionSegmentation:
    def test_trades_before_any_recorded_version_form_an_unlabeled_leading_segment(self, tmp_path):
        path = make_run_db(tmp_path, "paper-X-prod-20260920T000000Z.sqlite")
        add_trade(path, minute=0, pnl="5.00")
        add_version(path, pr_number=10, minute=50)
        add_trade(path, minute=100, pnl="-2.00")
        result = portfolio_history(tmp_path, "paper")
        assert len(result["segments"]) == 2
        assert result["segments"][0]["version"] is None
        assert result["segments"][0]["trade_count"] == 1
        assert result["segments"][0]["pnl_usd"] == D("5.00")
        assert result["segments"][1]["version"]["pr_number"] == 10
        assert result["segments"][1]["trade_count"] == 1
        assert result["segments"][1]["pnl_usd"] == D("-2.00")

    def test_a_trade_belongs_to_the_version_active_at_or_before_its_own_timestamp(self, tmp_path):
        path = make_run_db(tmp_path, "paper-X-prod-20260920T000000Z.sqlite")
        add_version(path, pr_number=1, minute=0)
        add_trade(path, minute=10, pnl="1.00")  # still under PR #1
        add_version(path, pr_number=2, minute=20)
        add_trade(path, minute=30, pnl="2.00")  # now under PR #2
        result = portfolio_history(tmp_path, "paper")
        by_pr = {s["version"]["pr_number"]: s for s in result["segments"]}
        assert by_pr[1]["trade_count"] == 1 and by_pr[1]["pnl_usd"] == D("1.00")
        assert by_pr[2]["trade_count"] == 1 and by_pr[2]["pnl_usd"] == D("2.00")
        # each segment carries its OWN slice of trades, not just aggregate counts -- the dashboard renders
        # straight from this rather than re-deriving time-range membership on the client.
        assert [t["pnl_usd"] for t in by_pr[1]["trades"]] == [D("1.00")]
        assert [t["pnl_usd"] for t in by_pr[2]["trades"]] == [D("2.00")]

    def test_win_rate_and_win_loss_counts(self, tmp_path):
        path = make_run_db(tmp_path, "paper-X-prod-20260920T000000Z.sqlite")
        add_version(path, pr_number=1, minute=0)
        add_trade(path, ticker="W1", minute=1, pnl="1.00")
        add_trade(path, ticker="W2", minute=2, pnl="2.00")
        add_trade(path, ticker="L1", minute=3, pnl="-1.00")
        result = portfolio_history(tmp_path, "paper")
        segment = result["segments"][0]
        assert segment["wins"] == 2 and segment["losses"] == 1
        assert segment["win_rate"] == pytest.approx(2 / 3)
        assert segment["pnl_usd"] == D("2.00")

    def test_a_version_recorded_with_no_trades_yet_is_still_a_visible_empty_segment(self, tmp_path):
        path = make_run_db(tmp_path, "paper-X-prod-20260920T000000Z.sqlite")
        add_version(path, pr_number=1, minute=0)
        result = portfolio_history(tmp_path, "paper")
        assert len(result["segments"]) == 1
        assert result["segments"][0]["trade_count"] == 0
        assert result["segments"][0]["win_rate"] is None

    def test_versions_from_multiple_runs_are_all_reported(self, tmp_path):
        run1 = make_run_db(tmp_path, "paper-X-prod-20260920T000000Z.sqlite")
        run2 = make_run_db(tmp_path, "paper-X-prod-20260921T000000Z.sqlite")
        add_version(run1, pr_number=1, minute=0)
        add_version(run2, pr_number=2, minute=0)
        result = portfolio_history(tmp_path, "paper")
        assert {v["pr_number"] for v in result["versions"]} == {1, 2}

    def test_label_and_commit_hash_are_exposed_on_each_version(self, tmp_path):
        path = make_run_db(tmp_path, "paper-X-prod-20260920T000000Z.sqlite")
        add_version(path, pr_number=42, minute=0)
        result = portfolio_history(tmp_path, "paper")
        v = result["versions"][0]
        assert v["pr_number"] == 42 and "PR #42" in v["label"] and len(v["commit_hash"]) == 40
        assert v["source"] == "auto"

    def test_two_restarts_on_the_same_still_current_pr_collapse_into_one_segment(self, tmp_path):
        # A restart that lands while the SAME commit is still the latest merge (no code change happened)
        # must not read as a second, confusingly-identical "PR #N" divider back to back.
        run1 = make_run_db(tmp_path, "paper-X-prod-20260920T000000Z.sqlite")
        run2 = make_run_db(tmp_path, "paper-X-prod-20260920T020000Z.sqlite")
        add_version(run1, pr_number=10, minute=0)
        add_trade(run1, ticker="BEFORE-RESTART", minute=5, pnl="1.00")
        add_version(run2, pr_number=10, minute=120)  # same PR, a later restart
        add_trade(run2, ticker="AFTER-RESTART", minute=125, pnl="2.00")
        result = portfolio_history(tmp_path, "paper")
        pr10_segments = [s for s in result["segments"] if s["version"] and s["version"]["pr_number"] == 10]
        assert len(pr10_segments) == 1
        assert [t["ticker"] for t in pr10_segments[0]["trades"]] == ["BEFORE-RESTART", "AFTER-RESTART"]
        assert pr10_segments[0]["pnl_usd"] == D("3.00")

    def test_a_genuinely_different_pr_after_a_repeat_still_starts_a_new_segment(self, tmp_path):
        run1 = make_run_db(tmp_path, "paper-X-prod-20260920T000000Z.sqlite")
        run2 = make_run_db(tmp_path, "paper-X-prod-20260920T020000Z.sqlite")
        run3 = make_run_db(tmp_path, "paper-X-prod-20260920T040000Z.sqlite")
        add_version(run1, pr_number=10, minute=0)
        add_version(run2, pr_number=10, minute=120)  # repeat: collapses into the PR #10 segment
        add_version(run3, pr_number=11, minute=240)  # genuinely new: its own segment
        result = portfolio_history(tmp_path, "paper")
        assert [s["version"]["pr_number"] for s in result["segments"]] == [10, 11]
