import os
import time
from datetime import datetime, timezone

from test_lab import make_db

from btcbot.cli import main
from btcbot.watch import render, summarize


def test_fresh_and_stale_databases(tmp_path):
    make_db(tmp_path, "paper-x.sqlite").close()
    make_db(tmp_path, "demo-y.sqlite").close()
    old = time.time() - 3600
    os.utime(tmp_path / "demo-y.sqlite", (old, old))
    got = {h.kind: h for h in summarize(tmp_path, stale_min=5)}
    assert not got["paper(prod)"].stale and got["demo"].stale


def test_cli_exit_code_flags_a_stale_database(tmp_path, capsys):
    make_db(tmp_path, "demo-y.sqlite").close()
    old = time.time() - 3600
    os.utime(tmp_path / "demo-y.sqlite", (old, old))
    assert main(["watch", "--data-dir", str(tmp_path)]) == 2
    assert "STALE" in capsys.readouterr().out


def test_empty_directory(tmp_path, capsys):
    assert main(["watch", "--data-dir", str(tmp_path)]) == 0
    assert "no paper" in capsys.readouterr().out


def _log(conn, event, *, ts=None):
    ts = ts or datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO run_log (ts, level, event, detail) VALUES (?, 'warning', ?, ?)",
        (ts.isoformat(), event, "detail"),
    )
    conn.commit()


def test_a_risk_pause_is_flagged_even_though_the_db_is_freshly_written(tmp_path):
    # A risk-manager pause keeps polling/predicting normally -- nothing else here would distinguish it from
    # a perfectly healthy run, which is exactly what let a 6+ hour production halt go unnoticed.
    conn = make_db(tmp_path, "paper-x.sqlite")
    _log(conn, "risk_blocked_order")
    got = {h.kind: h for h in summarize(tmp_path, stale_min=5)}
    assert got["paper(prod)"].risk_paused is True
    assert not got["paper(prod)"].stale
    assert "RISK-PAUSED" in render(list(got.values()))


def test_a_resume_event_after_the_pause_clears_the_flag(tmp_path):
    conn = make_db(tmp_path, "paper-x.sqlite")
    _log(conn, "risk_blocked_order")
    _log(conn, "risk_resumed")
    got = {h.kind: h for h in summarize(tmp_path, stale_min=5)}
    assert got["paper(prod)"].risk_paused is False


def test_no_risk_events_is_not_flagged(tmp_path):
    make_db(tmp_path, "paper-x.sqlite")
    got = {h.kind: h for h in summarize(tmp_path, stale_min=5)}
    assert got["paper(prod)"].risk_paused is False
    assert "RISK-PAUSED" not in render(list(got.values()))
