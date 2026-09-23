"""Offline tests for btcbot.trade_tape's two writers (A3 of the history-backfill handoff): upsert_trades
(the live recorder's additive shape) and replace_ticker_tape (the backfill's idempotent bulk-replace shape)."""

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from btcbot.models import Trade
from btcbot.trade_tape import init_trade_tape_schema, replace_ticker_tape, upsert_trades

T0 = datetime(2026, 7, 17, 23, 45, 0, tzinfo=timezone.utc)


def db():
    conn = sqlite3.connect(":memory:")
    init_trade_tape_schema(conn)
    return conn


def trade(tid, *, ticker="T", ts=T0, count="5.00", yes="0.30", no="0.70", side="yes"):
    return Trade(ticker=ticker, trade_id=tid, count=Decimal(count), yes_price=Decimal(yes), no_price=Decimal(no),
                taker_side=side, created_time=ts)


def rows(conn, ticker=None):
    sql = "SELECT ticker, second_ts, yes_price, taker_side, contracts, prints FROM trade_tape"
    if ticker:
        sql += " WHERE ticker = ?"
        return conn.execute(sql, (ticker,)).fetchall()
    return conn.execute(sql).fetchall()


class TestUpsertTrades:
    def test_two_trades_same_second_price_side_aggregate(self):
        conn = db()
        upsert_trades(conn, [trade("a", count="2.00"), trade("b", count="3.00")])
        got = rows(conn)
        assert len(got) == 1 and got[0][4] == 5.0 and got[0][5] == 2

    def test_a_different_second_is_a_separate_row(self):
        conn = db()
        upsert_trades(conn, [trade("a", ts=T0), trade("b", ts=T0 + timedelta(seconds=1))])
        assert len(rows(conn)) == 2

    def test_two_separate_calls_add_not_replace(self):
        conn = db()
        upsert_trades(conn, [trade("a", count="2.00")])
        upsert_trades(conn, [trade("b", count="3.00")])
        got = rows(conn)
        assert got[0][4] == 5.0 and got[0][5] == 2  # additive across calls -- this is the live-poll contract

    def test_repeating_the_same_trade_id_across_calls_double_counts(self):
        # This is the documented, intentional risk of upsert_trades when the caller doesn't dedupe by id --
        # the live recorder itself tracks seen ids so it never actually does this; this test pins the
        # behavior so a future change here is a deliberate decision, not an accident.
        conn = db()
        upsert_trades(conn, [trade("a", count="2.00")])
        upsert_trades(conn, [trade("a", count="2.00")])
        assert rows(conn)[0][4] == 4.0


class TestReplaceTickerTape:
    def test_dedupes_within_one_call_by_trade_id(self):
        conn = db()
        n = replace_ticker_tape(conn, "T", [trade("a", count="2.00"), trade("a", count="2.00")])
        assert n == 1
        assert rows(conn)[0][4] == 2.0  # NOT 4.0: a duplicate id is the same print, counted once

    def test_running_twice_over_the_same_trades_is_idempotent(self):
        conn = db()
        trades = [trade("a", count="2.00"), trade("b", count="3.00", ts=T0 + timedelta(seconds=1))]
        replace_ticker_tape(conn, "T", trades)
        first = sorted(rows(conn))
        replace_ticker_tape(conn, "T", trades)
        second = sorted(rows(conn))
        assert first == second

    def test_running_over_an_overlapping_later_fetch_does_not_double_count(self):
        conn = db()
        replace_ticker_tape(conn, "T", [trade("a", count="2.00"), trade("b", count="3.00")])
        # a later backfill run re-fetches the same window plus one more trade
        replace_ticker_tape(conn, "T", [trade("a", count="2.00"), trade("b", count="3.00"), trade("c", count="1.00")])
        assert rows(conn)[0][4] == 6.0  # 2+3+1, not 2+3+2+3+1

    def test_replacing_one_ticker_never_touches_another(self):
        conn = db()
        replace_ticker_tape(conn, "A", [trade("a", ticker="A")])
        replace_ticker_tape(conn, "B", [trade("b", ticker="B")])
        replace_ticker_tape(conn, "A", [trade("a2", ticker="A", count="9.00")])
        assert len(rows(conn, "B")) == 1 and rows(conn, "A")[0][4] == 9.0

    def test_a_trade_for_the_wrong_ticker_is_a_value_error(self):
        conn = db()
        with pytest.raises(ValueError):
            replace_ticker_tape(conn, "A", [trade("a", ticker="B")])

    def test_a_validation_failure_before_any_write_leaves_the_prior_tape_untouched(self):
        # The wrong-ticker check runs before the DELETE/INSERT transaction even starts, so this proves
        # "reject first, touch nothing" -- true rollback-after-DELETE relies on sqlite3's own `with conn:`
        # commit/rollback guarantee, which is standard library behavior this project doesn't re-verify.
        conn = db()
        replace_ticker_tape(conn, "T", [trade("a", count="2.00")])
        with pytest.raises(ValueError):
            replace_ticker_tape(conn, "T", [trade("a", count="1.00"), trade("bad", ticker="OTHER")])
        assert rows(conn, "T")[0][4] == 2.0

    @given(st.data())
    def test_property_summed_contracts_equals_summed_deduped_trade_counts(self, data):
        n = data.draw(st.integers(min_value=1, max_value=8))
        ids = data.draw(st.lists(st.sampled_from([f"id{i}" for i in range(n)]), min_size=1, max_size=20))
        counts = {tid: data.draw(st.decimals(min_value="0.01", max_value="1000", places=2)) for tid in set(ids)}
        trades = [trade(tid, count=str(counts[tid]), ts=T0 + timedelta(seconds=hash(tid) % 5)) for tid in ids]
        conn = db()
        replace_ticker_tape(conn, "T", trades)
        total_contracts = sum(r[4] for r in rows(conn, "T"))
        total_deduped_count = float(sum(counts.values()))
        assert total_contracts == pytest.approx(total_deduped_count)
