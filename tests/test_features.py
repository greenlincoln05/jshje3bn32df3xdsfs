import csv
from datetime import timedelta

import pytest
from test_lab import T0, WIN, make_db, seed_window

from btcbot.cli import main
from btcbot.features import COLUMNS, FeatureError, build_rows, write_csv


def two_window_db(tmp_path, name="a.sqlite"):
    conn = make_db(tmp_path, name)
    seed_window(conn, 0, "yes")
    seed_window(conn, 1, "no")
    conn.commit()
    conn.close()
    return tmp_path / name


def test_rows_have_features_and_the_settled_label(tmp_path):
    rows = build_rows([two_window_db(tmp_path)], step_sec=1)
    assert rows and set(rows[0]) >= set(COLUMNS) - {"p_model"}
    by = {r["ticker"]: r for r in rows}
    assert by["KXBTC15M-LAB000-00"]["outcome_yes"] == 1
    assert by["KXBTC15M-LAB001-00"]["outcome_yes"] == 0
    assert all(r["tau_sec"] >= 0 for r in rows)
    assert rows[0]["yes_bid"] == 0.3 and rows[0]["no_bid"] == 0.68


def test_step_thins_rows_per_window(tmp_path):
    db = two_window_db(tmp_path)
    assert len(build_rows([db], step_sec=1)) > len(build_rows([db], step_sec=4))


def test_two_recordings_of_one_window_are_not_double_counted(tmp_path):
    a = two_window_db(tmp_path, "a.sqlite")
    b = two_window_db(tmp_path, "b.sqlite")
    rows = build_rows([a, b], step_sec=1)
    assert len(rows) == len(build_rows([a], step_sec=1))


def test_later_data_cannot_change_an_earlier_row(tmp_path):
    db = two_window_db(tmp_path)
    before = build_rows([db], step_sec=1)[0]
    import sqlite3
    conn = sqlite3.connect(db)
    late = (T0 + timedelta(days=1)).isoformat()
    conn.execute("INSERT INTO spot_ticks (source, price, source_ts, receive_ts, monotonic_ts) VALUES ('c','99999',?,?,1)", (late, late))
    conn.commit()
    conn.close()
    after = build_rows([db], step_sec=1)[0]
    assert before == after


def test_no_databases_is_an_error():
    with pytest.raises(FeatureError):
        build_rows([])


def test_a_non_order_book_database_is_skipped_not_a_crash(tmp_path):
    """A ``--data-dir`` glob commonly also matches e.g. a `btcbot download-history` database (market_outcomes/
    spot_candles schema, no order book at all) -- that must be skipped, not crash the whole run."""
    import sqlite3

    wrong_schema = tmp_path / "history-KXBTC15M-prod-x.sqlite"
    conn = sqlite3.connect(wrong_schema)
    conn.execute("CREATE TABLE market_outcomes (ticker TEXT)")
    conn.commit()
    conn.close()

    good = two_window_db(tmp_path)
    rows = build_rows([wrong_schema, good], step_sec=1)
    assert rows and all(r["source"] != wrong_schema.name for r in rows)


def test_only_wrong_schema_databases_is_still_a_clean_error(tmp_path):
    import sqlite3

    wrong_schema = tmp_path / "history-KXBTC15M-prod-x.sqlite"
    conn = sqlite3.connect(wrong_schema)
    conn.execute("CREATE TABLE market_outcomes (ticker TEXT)")
    conn.commit()
    conn.close()

    with pytest.raises(FeatureError, match="wrong schema"):
        build_rows([wrong_schema])


def test_cli_writes_csv(tmp_path, capsys):
    db = two_window_db(tmp_path)
    out = tmp_path / "f.csv"
    assert main(["features", "--db", str(db), "--output", str(out), "--step-sec", "1"]) == 0
    with open(out, newline="") as fh:
        got = list(csv.DictReader(fh))
    assert got and list(got[0]) == list(COLUMNS)
    assert "wrote" in capsys.readouterr().out
