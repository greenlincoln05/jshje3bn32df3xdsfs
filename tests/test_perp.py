"""Offline tests for the BTC perpetual PAPER engine (btcbot.perp_paper) and its backtest (btcbot.perp_backtest).
Synthetic bars only; no network, no key, and no perps order code exists to test."""

import ast
import math
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import btcbot.perp_backtest as perp_backtest_module
import btcbot.perp_paper as perp_paper_module
from btcbot.cli import main
from btcbot.coinbase_history import Candle
from btcbot.history_pipeline import init_history_schema, save_candles
from btcbot.perp_backtest import (
    Bar,
    PerpBacktestError,
    describe_source,
    excess_t,
    load_bars,
    render_report,
    run,
    run_backtest,
    verdict,
)
from btcbot.perp_paper import PerpAccount, PerpPaperError, PerpSpec, effective_funding_rate, funding_times
from btcbot.pm_history import init_pm_history_schema

UTC = timezone.utc
T0 = datetime(2026, 7, 1, tzinfo=UTC)
NO_SLIP = PerpSpec(half_spread_bps=Decimal(0), liq_slippage_bps=Decimal(0))


def d(x) -> Decimal:
    return Decimal(str(x))


def bars_from(prices, *, start=T0, wiggle=0.0):
    out = []
    prev = prices[0]
    for i, p in enumerate(prices):
        o = prev
        hi, lo = max(o, p) * (1 + wiggle), min(o, p) * (1 - wiggle)
        out.append(Bar(start + timedelta(minutes=i), d(round(o, 2)), d(round(hi, 2)), d(round(lo, 2)), d(round(p, 2))))
        prev = p
    return out


class TestFundingSchedule:
    @pytest.mark.parametrize("day,expected", [
        ("2026-07-01", ["04:00", "12:00", "20:00"]),  # EDT
        ("2026-12-01", ["05:00", "13:00", "21:00"]),  # EST
        ("2026-03-08", ["05:00", "12:00", "20:00"]),  # DST starts at 2 AM: midnight is still EST
        ("2026-11-01", ["04:00", "13:00", "21:00"]),  # DST ends at 2 AM: midnight is still EDT
    ])
    def test_midnight_8am_4pm_eastern_in_utc(self, day, expected):
        start = datetime.fromisoformat(day + "T00:00:00+00:00")
        got = [t.strftime("%H:%M") for t in funding_times(start, start + timedelta(days=1))]
        assert got == expected

    def test_cap_and_dead_band(self):
        spec = PerpSpec()
        assert effective_funding_rate(d("0.05"), spec) == d("0.02")
        assert effective_funding_rate(d("-0.05"), spec) == d("-0.02")
        assert effective_funding_rate(d("0.00009"), spec) == 0
        assert effective_funding_rate(d("0.0001"), spec) == d("0.0001")


class TestAccount:
    def test_round_trip_costs_two_taker_fees_on_notional(self):
        acct = PerpAccount(cash=d(1000), spec=NO_SLIP)
        acct.open(T0, 1, d(100000), d(1))
        contracts = acct.position
        acct.close(T0, d(100000))
        fee_each = contracts * d("0.0001") * d(100000) * d("0.0012")
        assert acct.cash == d(1000) - 2 * fee_each
        assert acct.fees_paid == 2 * fee_each

    def test_sizing_uses_current_equity_so_it_shrinks_after_a_loss(self):
        acct = PerpAccount(cash=d(1000), spec=NO_SLIP)
        acct.open(T0, 1, d(100000), d(1))
        first = acct.position
        acct.close(T0, d(90000))
        acct.open(T0, 1, d(100000), d(1))
        assert acct.position < first

    def test_liquidation_price_one_x_long_and_two_x_short(self):
        long_ = PerpAccount(cash=d(1000), spec=NO_SLIP)
        long_.open(T0, 1, d(100000), d(1))
        assert long_.liquidation_price() == d(90000)  # maintenance 90% of initial: a 10% move at 1x
        short = PerpAccount(cash=d(1000), spec=NO_SLIP)
        short.open(T0, -1, d(100000), d(2))
        assert short.liquidation_price() == d(105000)

    def test_liquidation_triggers_on_the_bar_extreme_and_loss_is_capped_at_margin(self):
        acct = PerpAccount(cash=d(1000), spec=NO_SLIP)
        acct.open(T0, 1, d(100000), d(2))
        assert acct.check_liquidation(T0, bar_low=d(95100), bar_high=d(99000)) is None
        event = acct.check_liquidation(T0, bar_low=d(80000), bar_high=d(99000))
        assert event is not None and event.kind == "liquidation" and acct.liquidations == 1
        assert not acct.is_open and acct.cash > 0  # isolated: lost the posted margin's cushion, not everything

    def test_funding_sign(self):
        long_ = PerpAccount(cash=d(1000), spec=NO_SLIP)
        long_.open(T0, 1, d(100000), d(1))
        short = PerpAccount(cash=d(1000), spec=NO_SLIP)
        short.open(T0, -1, d(100000), d(1))
        before_l, before_s = long_.cash, short.cash
        long_.apply_funding(T0, d("0.001"), d(100000))
        short.apply_funding(T0, d("0.001"), d(100000))
        assert long_.cash < before_l and short.cash > before_s  # positive rate: longs pay shorts
        assert long_.apply_funding(T0, d("0.00001"), d(100000)) is None  # inside the dead band

    @pytest.mark.parametrize("lev", ["0", "6"])
    def test_leverage_bounds(self, lev):
        with pytest.raises(PerpPaperError):
            PerpAccount(cash=d(1000)).open(T0, 1, d(100000), d(lev))

    def test_no_network_or_order_code_in_the_perp_modules(self):
        # By construction, not convention: parse the real imports and definitions (docstrings may NAME these).
        for module in (perp_paper_module, perp_backtest_module):
            tree = ast.parse(Path(module.__file__).read_text(encoding="ascii"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            assert not imported & {"httpx", "websockets", "btcbot.kalshi_client", "socket", "urllib.request"}, module.__name__
            defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            assert not {name for name in defined if "order" in name.lower()}, f"{module.__name__} defines an order function"


class TestRun:
    def test_flat_never_trades(self):
        r = run(bars_from([100000.0] * 3000), "flat", account_usd=d(500), leverage=d(1), funding_8h=d(0), spec=NO_SLIP)
        assert (r.trades, r.return_pct, r.end_equity) == (0, 0.0, "500.00")

    def test_hold_tracks_the_price_minus_one_entry_fee(self):
        prices = [100000.0 + 5 * i for i in range(3000)]
        r = run(bars_from(prices), "hold", account_usd=d(500), leverage=d(1), funding_8h=d(0), spec=NO_SLIP)
        assert r.trades == 1
        entry = prices[0]  # decided on bar 0's close, filled at bar 1's open (= bar 0's close here)
        assert r.return_pct == pytest.approx((prices[-1] / entry - 1) * 100 - 0.12, abs=0.05)

    def test_orders_fill_at_the_next_bars_open_not_the_decision_bars_close(self):
        bars = [Bar(T0, d(100), d(100), d(100), d(100)), Bar(T0 + timedelta(minutes=1), d(110), d(110), d(110), d(110))]
        acct_run = run(bars * 1, "hold", account_usd=d(500), leverage=d(1), funding_8h=d(0), spec=NO_SLIP)
        assert acct_run.trades == 1 and acct_run.return_pct == pytest.approx(-0.12, abs=0.01)  # bought at 110, marked 110

    def test_trend_goes_long_in_an_uptrend_and_short_in_a_downtrend(self):
        up = [100000.0 * math.exp(0.00002 * i) for i in range(4000)]
        down = [100000.0 * math.exp(-0.00002 * i) for i in range(4000)]
        r_up = run(bars_from(up), "trend_24h", account_usd=d(500), leverage=d(1), funding_8h=d(0), spec=NO_SLIP)
        r_dn = run(bars_from(down), "trend_24h", account_usd=d(500), leverage=d(1), funding_8h=d(0), spec=NO_SLIP)
        assert r_up.trades >= 1 and r_up.return_pct > 0
        assert r_dn.trades >= 1 and r_dn.return_pct > 0  # short side profits

    def test_window_15m_trades_once_per_directional_window(self):
        rng = random.Random(1)
        prices = [100000.0]
        for _ in range(15 * 40 - 1):
            prices.append(prices[-1] * math.exp(rng.gauss(0, 0.0005)))
        r = run(bars_from(prices), "window_15m", account_usd=d(500), leverage=d(1), funding_8h=d(0), spec=NO_SLIP)
        assert 35 <= r.trades <= 40

    def test_funding_stamp_inside_a_data_gap_is_still_charged(self):
        # 03:50-03:59 UTC then a gap to 04:10: the 04:00 UTC (midnight EDT) stamp falls in the gap.
        start = datetime(2026, 7, 1, 3, 50, tzinfo=UTC)
        before = [Bar(start + timedelta(minutes=i), d(100000), d(100000), d(100000), d(100000)) for i in range(10)]
        after = [Bar(start + timedelta(minutes=20 + i), d(100000), d(100000), d(100000), d(100000)) for i in range(10)]
        r = run(before + after, "hold", account_usd=d(500), leverage=d(1), funding_8h=d("0.001"), spec=NO_SLIP)
        assert Decimal(r.funding_paid) > 0

    def test_a_one_x_long_through_a_crash_is_liquidated(self):
        prices = [100000.0] * 10 + [100000.0 - 2000 * i for i in range(1, 10)] + [82000.0] * 10
        r = run(bars_from(prices), "hold", account_usd=d(500), leverage=d(1), funding_8h=d(0), spec=NO_SLIP)
        assert r.liquidations >= 1


class TestVerdict:
    def test_refuses_below_thirty_days(self):
        assert verdict(29, 5.0, 5.0).startswith("insufficient data")

    def test_wording_never_says_profitable(self):
        for t in (3.0, -3.0, 0.5, None):
            text = verdict(60, t, t).lower()
            assert "profitable" not in text
        assert "not proof of an edge" in verdict(60, 3.0, 0.0)

    def test_excess_t_sign(self):
        assert excess_t([0.01, 0.02, 0.015, 0.012], [0, 0, 0, 0]) > 2
        assert excess_t([0.0, 0.0, 0.0], [0.0, 0.0, 0.0]) is None


def random_walk_bars(days, seed=3):
    rng = random.Random(seed)
    p = 100000.0
    prices = []
    for _ in range(days * 1440):
        p *= math.exp(rng.gauss(0, 0.0006))
        prices.append(p)
    return bars_from(prices, wiggle=0.0002)


class TestBacktest:
    def test_needs_at_least_two_days(self):
        with pytest.raises(PerpBacktestError):
            run_backtest(bars_from([100000.0] * 100))

    def test_leverage_is_capped_for_paper(self):
        with pytest.raises(PerpBacktestError):
            run_backtest(random_walk_bars(3), leverages=[d(4)])

    def test_report_shape_and_no_profit_wording(self):
        report = run_backtest(random_walk_bars(45), leverages=[d(1)], fundings=[d(0)])
        assert {r["strategy"] for r in report.rows} == {"flat", "hold", "trend_24h", "window_15m"}
        assert {v["strategy"] for v in report.verdicts} == {"trend_24h", "window_15m"}
        text = render_report(report).lower()
        assert "not a profitability claim" in text
        assert "profitable" not in text.replace("not a profitability claim", "")

    def test_fees_sink_the_fifteen_minute_copy_on_a_no_edge_walk(self):
        report = run_backtest(random_walk_bars(45), leverages=[d(1)], fundings=[d(0)], strategies=["flat", "window_15m"])
        test_row = next(r for r in report.rows if r["segment"] == "test" and r["strategy"] == "window_15m")
        assert test_row["return_pct"] < -50 and Decimal(test_row["fees_paid"]) > 100


class TestLoadBars:
    def test_from_a_download_history_database(self):
        conn = sqlite3.connect(":memory:")
        init_history_schema(conn)
        save_candles(conn, [Candle(T0 + timedelta(minutes=i), d(99), d(102), d(100), d(101), d(1)) for i in range(3)])
        bars, source = load_bars(conn)
        assert len(bars) == 3 and "Coinbase" in source and bars[0].close == d(101)

    def test_from_binance_seconds_aggregated_to_minutes(self):
        conn = sqlite3.connect(":memory:")
        init_pm_history_schema(conn)
        base = int(T0.timestamp())
        rows = [(base + s, str(100 + s), str(100 + s + 0.5), str(100 + s - 0.5), str(100 + s + 0.25), "1", 1, "0.5") for s in range(120)]
        conn.executemany("INSERT INTO btc_klines_1s VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        bars, source = load_bars(conn)
        assert len(bars) == 2 and "Binance" in source
        assert (bars[0].open, bars[0].high, bars[0].low, bars[0].close) == (d(100), d("159.5"), d("99.5"), d("159.25"))

    def test_an_unrelated_database_is_an_error(self):
        with pytest.raises(PerpBacktestError):
            load_bars(sqlite3.connect(":memory:"))

    def test_describe_source_matches_load_bars_and_none_for_unrelated_or_empty(self):
        history_conn = sqlite3.connect(":memory:")
        init_history_schema(history_conn)
        save_candles(history_conn, [Candle(T0, d(99), d(102), d(100), d(101), d(1))])
        assert describe_source(history_conn) == load_bars(history_conn)[1]

        pm_conn = sqlite3.connect(":memory:")
        init_pm_history_schema(pm_conn)
        base = int(T0.timestamp())
        pm_conn.execute(
            "INSERT INTO btc_klines_1s VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (base, "100", "100.5", "99.5", "100.25", "1", 1, "0.5")
        )
        assert describe_source(pm_conn) == load_bars(pm_conn)[1]

        assert describe_source(sqlite3.connect(":memory:")) is None
        empty_history = sqlite3.connect(":memory:")
        init_history_schema(empty_history)
        assert describe_source(empty_history) is None  # spot_candles exists but has no rows
        empty_pm = sqlite3.connect(":memory:")
        init_pm_history_schema(empty_pm)
        assert describe_source(empty_pm) is None  # btc_klines_1s exists but has no rows


class TestCli:
    def _db(self, tmp_path, days=3):
        conn = sqlite3.connect(str(tmp_path / "history.sqlite"))
        init_history_schema(conn)
        save_candles(conn, [Candle(b.ts, b.low, b.high, b.open, b.close, d(1)) for b in random_walk_bars(days)])
        conn.close()
        return tmp_path / "history.sqlite"

    def test_end_to_end(self, tmp_path, capsys):
        db = self._db(tmp_path)
        report = tmp_path / "perp.json"
        assert main(["perp-backtest", "--db", str(db), "--leverage", "1", "--funding-8h", "0", "--report", str(report)]) == 0
        out = capsys.readouterr().out
        assert "PAPER backtest" in out and "insufficient data" in out and report.is_file()

    def test_leverage_above_three_is_refused(self, tmp_path):
        assert main(["perp-backtest", "--db", "x", "--leverage", "5"]) == 2

    def test_bad_funding_value(self, tmp_path):
        assert main(["perp-backtest", "--db", "x", "--funding-8h", "abc"]) == 2
