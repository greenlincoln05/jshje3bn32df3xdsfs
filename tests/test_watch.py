import os
import time

from test_lab import make_db

from btcbot.cli import main
from btcbot.watch import summarize


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
