import pytest
from test_lab import make_db, seed_window

from btcbot.cli import main
from btcbot.compare import CompareError, compare, render


def db(tmp_path, name, yes_price):
    conn = make_db(tmp_path, name)
    seed_window(conn, 0, "yes", yes_price=yes_price)
    conn.commit()
    conn.close()
    return tmp_path / name


def test_windows_are_lined_up_and_gaps_measured(tmp_path):
    rep = compare(db(tmp_path, "d.sqlite", "0.30"), db(tmp_path, "p.sqlite", "0.34"))
    assert rep["n_windows"] == 1
    w = rep["windows"][0]
    assert w["demo_mid"] != w["prod_mid"] and w["median_abs_mid_gap"] > 0
    assert "windows compared: 1" in render(rep)


def test_no_shared_window_is_an_error(tmp_path):
    a = db(tmp_path, "a.sqlite", "0.30")
    import sqlite3
    conn = sqlite3.connect(a)
    conn.execute("UPDATE orderbook_snapshots SET ticker='OTHER'")
    conn.execute("UPDATE market_state SET ticker='OTHER'")
    conn.commit(); conn.close()
    with pytest.raises(CompareError):
        compare(a, db(tmp_path, "b.sqlite", "0.30"))


def test_cli_prints_report(tmp_path, capsys):
    assert main(["compare", "--demo-db", str(db(tmp_path, "d.sqlite", "0.30")), "--prod-db", str(db(tmp_path, "p.sqlite", "0.34"))]) == 0
    assert "Demo vs prod" in capsys.readouterr().out
