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


def _log(conn, event, *, ts=None, detail="detail"):
    ts = ts or datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO run_log (ts, level, event, detail) VALUES (?, 'warning', ?, ?)",
        (ts.isoformat(), event, detail),
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


def test_a_kill_file_pause_gets_kill_specific_advice_not_the_resume_file_hint(tmp_path):
    # A resume-file only clears RiskManager's consecutive-loss pause -- it does nothing for a KILL-file
    # block, so the two must not share the same "create a resume-file" guidance.
    conn = make_db(tmp_path, "paper-x.sqlite")
    _log(conn, "risk_blocked_order", detail="KILL file present")
    got = {h.kind: h for h in summarize(tmp_path, stale_min=5)}
    rendered = render(list(got.values()))
    assert "delete it" in rendered
    assert "create a resume-file" not in rendered


def test_a_consecutive_loss_pause_still_gets_the_resume_file_hint(tmp_path):
    conn = make_db(tmp_path, "paper-x.sqlite")
    _log(conn, "risk_blocked_order", detail="paused: 5 consecutive losses")
    got = {h.kind: h for h in summarize(tmp_path, stale_min=5)}
    rendered = render(list(got.values()))
    assert "create a resume-file" in rendered
    assert "delete it" not in rendered


def test_a_stale_and_risk_paused_db_gets_a_non_contradictory_message(tmp_path):
    # A process that paused and then also stopped writing entirely must not claim it's "still
    # polling/predicting" (the message the not-stale RISK-PAUSED branch uses) while also being flagged STALE.
    conn = make_db(tmp_path, "paper-x.sqlite")
    _log(conn, "risk_blocked_order")
    conn.close()  # checkpoint WAL back into the main file so no -wal file is left with a fresh mtime
    old = time.time() - 3600
    for suffix in ("", "-wal", "-journal", "-shm"):
        p = tmp_path / f"paper-x.sqlite{suffix}"
        if p.exists():
            os.utime(p, (old, old))
    got = {h.kind: h for h in summarize(tmp_path, stale_min=5)}
    h = got["paper(prod)"]
    assert h.stale is True and h.risk_paused is True
    rendered = render([h])
    assert "STALE" in rendered and "RISK-PAUSED" in rendered
    assert "still polling/predicting" not in rendered
