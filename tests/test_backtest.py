import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from btcbot.backtest import (
    BacktestError,
    Settlement,
    load_replay_data,
    load_settlements,
    load_snapshots,
    load_spot_ticks,
    load_windows,
    prepare_replay,
    replay_prepared,
    run_backtest,
)
from btcbot.config import BotConfig, ExitRules
from btcbot.models import ParseError
from btcbot.paper_broker import QueueAssumption, taker_fee
from btcbot.recorder import Recorder

T0 = datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc)
TICKER = "KXBTC15M-26SEP190000-00"


def make_db(tmp_path, name="bt.sqlite"):
    db_path = tmp_path / name
    Recorder(None, series_ticker="KXBTC15M", db_path=db_path).close()
    return sqlite3.connect(str(db_path))


def insert_market(conn, ticker, *, strike, close_time, open_time=None, status="active"):
    open_time = open_time or (close_time - timedelta(seconds=900))
    conn.execute(
        """INSERT INTO market_state
           (ticker, event_ticker, poll_ts, status, strike, open_time, close_time, volume, open_interest)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (ticker, ticker.rsplit("-", 1)[0], open_time.isoformat(), status, str(strike), open_time.isoformat(), close_time.isoformat(), "0", "0"),
    )
    conn.commit()


def insert_snapshot(conn, ticker, poll_ts, *, yes=(), no=()):
    payload = json.dumps({"yes": [[p, s] for p, s in yes], "no": [[p, s] for p, s in no]})
    yb = yes[0] if yes else (None, None)
    nb = no[0] if no else (None, None)
    conn.execute(
        """INSERT INTO orderbook_snapshots
           (ticker, request_started_ts, poll_ts, latency_ms, yes_bid_price, yes_bid_size, yes_ask_price,
            yes_ask_size, no_bid_price, no_bid_size, no_ask_price, no_ask_size, book_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ticker, poll_ts.isoformat(), poll_ts.isoformat(), 10.0, yb[0], yb[1], None, None, nb[0], nb[1], None, None, payload),
    )
    conn.commit()


def insert_spot_run(conn, start_ts, count, *, base_price=Decimal("80000")):
    price = base_price
    for i in range(count):
        ts = start_ts + timedelta(seconds=i)
        price = price + Decimal("1") if i % 2 == 0 else price - Decimal("0.5")
        conn.execute(
            "INSERT INTO spot_ticks (source, price, source_ts, receive_ts, monotonic_ts) VALUES (?,?,?,?,?)",
            ("coinbase-ws", str(price), ts.isoformat(), ts.isoformat(), float(i)),
        )
    conn.commit()


def insert_settlement(conn, ticker, result, *, strike, close_time, available_ts=None):
    conn.execute(
        """INSERT INTO settlements (ticker, event_ticker, result, settled_avg, strike, close_time, finalized_poll_ts)
           VALUES (?,?,?,?,?,?,?)""",
        (ticker, ticker.rsplit("-", 1)[0], result, str(strike), str(strike), close_time.isoformat(),
         (available_ts or close_time).isoformat()),
    )
    conn.commit()


def seed_fillable_window(conn, ticker, *, start_ts, strike=Decimal("80000"), close_time=None, sizes=None,
                          yes_price="0.30", no_price="0.68"):
    """A window whose YES book depth shrinks enough to trigger exactly one maker fill partway through, given
    an up-trending spot feed (so YES has a clear edge over the 0.30 bid)."""
    close_time = close_time or (start_ts + timedelta(seconds=500))
    sizes = sizes if sizes is not None else [15, 14, 13, 12, 11, 10, 14, 0]
    insert_spot_run(conn, start_ts - timedelta(seconds=90), 110)
    insert_market(conn, ticker, strike=strike, close_time=close_time, open_time=start_ts - timedelta(seconds=300))
    for offset, size in enumerate(sizes):
        insert_snapshot(
            conn, ticker, start_ts + timedelta(seconds=offset),
            yes=[(yes_price, str(size))], no=[(no_price, "15")],
        )
    return close_time


FULL_FILL_SIZES = [15, 14, 13, 12, 11, 10, 14, 0, 5, 0]  # seed_fillable_window's own sequence leaves 1 of 5
# contracts still resting (a deliberate partial fill for its own tests); the last two ticks here mop that
# remaining contract up too, so the order clears completely and decide()'s exit check (which only ever runs
# once has_resting_order is False) has something to act on.


def seed_window_with_price_drop(conn, ticker, *, start_ts, strike=Decimal("80000"), entry_price="0.30",
                                 drop_price="0.20", drop_depth="20", close_time=None):
    """A resting order that fills COMPLETELY (all 5 contracts, see FULL_FILL_SIZES), followed by the YES
    book resting at a materially lower ``drop_price`` with ``drop_depth`` contracts of depth -- enough to
    fill a stop-loss exit sale, partially if ``drop_depth`` is less than the position size."""
    close_time = seed_fillable_window(conn, ticker, start_ts=start_ts, strike=strike, close_time=close_time,
                                       sizes=FULL_FILL_SIZES, yes_price=entry_price)
    for offset in range(len(FULL_FILL_SIZES), len(FULL_FILL_SIZES) + 8):
        insert_snapshot(conn, ticker, start_ts + timedelta(seconds=offset), yes=[(drop_price, drop_depth)], no=[("0.79", "20")])
    return close_time


def replay(conn, config):
    return replay_prepared(prepare_replay(load_replay_data(conn), config), config)


class TestLoaders:
    def test_load_snapshots_round_trips_the_book(self, tmp_path):
        conn = make_db(tmp_path)
        insert_snapshot(conn, TICKER, T0, yes=[("0.5", "10")], no=[("0.4", "8")])
        snapshots = load_snapshots(conn)
        assert len(snapshots) == 1
        assert snapshots[0].book.best_bid("yes").price == Decimal("0.5")
        assert snapshots[0].book.best_bid("no").size == Decimal("8")

    def test_load_snapshots_rejects_malformed_json(self, tmp_path):
        conn = make_db(tmp_path)
        conn.execute(
            "INSERT INTO orderbook_snapshots (ticker, request_started_ts, poll_ts, latency_ms, book_json) VALUES (?,?,?,?,?)",
            (TICKER, T0.isoformat(), T0.isoformat(), 1.0, "not json"),
        )
        conn.commit()
        with pytest.raises(ParseError):
            load_snapshots(conn)

    def test_load_windows_takes_the_latest_strike_row(self, tmp_path):
        conn = make_db(tmp_path)
        insert_market(conn, TICKER, strike=Decimal("80000"), close_time=T0 + timedelta(seconds=900))
        windows = load_windows(conn)
        assert windows[TICKER][0] == Decimal("80000")

    def test_load_settlements_excludes_unresolved(self, tmp_path):
        conn = make_db(tmp_path)
        insert_settlement(conn, TICKER, "yes", strike=Decimal("80000"), close_time=T0)
        assert load_settlements(conn) == {TICKER: Settlement("yes", T0)}

    def test_load_spot_ticks_round_trips_price(self, tmp_path):
        conn = make_db(tmp_path)
        insert_spot_run(conn, T0, 3)
        ticks = load_spot_ticks(conn)
        assert len(ticks) == 3


class TestRunBacktestValidation:
    def test_raises_with_no_snapshots(self, tmp_path):
        conn = make_db(tmp_path)
        with pytest.raises(BacktestError):
            run_backtest(conn, BotConfig())


class TestHappyPath:
    def test_a_winning_trade_is_recorded_correctly(self, tmp_path):
        conn = make_db(tmp_path)
        close_time = seed_fillable_window(conn, TICKER, start_ts=T0)
        insert_settlement(conn, TICKER, "yes", strike=Decimal("80000"), close_time=close_time)

        report = run_backtest(conn, BotConfig(), queue_assumption=QueueAssumption.OPTIMISTIC, maker_fee_multiplier=Decimal("0"))

        assert report.trades == 1
        assert report.wins == 1 and report.losses == 0 and report.unresolved == 0
        assert report.win_rate == 1.0
        # 4 contracts (see seed_fillable_window's shrink-to-0-then-refill-to-14-then-0 sequence) at 0.30, no fees
        assert report.total_pnl_usd == Decimal("4") * (Decimal(1) - Decimal("0.30"))
        assert report.total_fees_usd == Decimal("0.000000")
        assert report.beats_trade_nothing is True
        assert report.windows_traded == 1 and report.windows_seen == 1

    def test_a_losing_trade_is_recorded_correctly(self, tmp_path):
        conn = make_db(tmp_path)
        close_time = seed_fillable_window(conn, TICKER, start_ts=T0)
        insert_settlement(conn, TICKER, "no", strike=Decimal("80000"), close_time=close_time)

        report = run_backtest(conn, BotConfig(), maker_fee_multiplier=Decimal("0"))

        assert report.trades == 1 and report.wins == 0 and report.losses == 1
        assert report.total_pnl_usd == Decimal("-4") * Decimal("0.30")
        assert report.beats_trade_nothing is False

    def test_maker_fee_multiplier_reduces_pnl(self, tmp_path):
        conn = make_db(tmp_path)
        close_time = seed_fillable_window(conn, TICKER, start_ts=T0)
        insert_settlement(conn, TICKER, "yes", strike=Decimal("80000"), close_time=close_time)

        free = run_backtest(conn, BotConfig(), maker_fee_multiplier=Decimal("0"))
        taxed = run_backtest(conn, BotConfig(), maker_fee_multiplier=Decimal("0.25"))

        assert taxed.total_fees_usd > 0
        assert taxed.total_pnl_usd < free.total_pnl_usd

    def test_pessimistic_queue_assumption_never_fills(self, tmp_path):
        conn = make_db(tmp_path)
        close_time = seed_fillable_window(conn, TICKER, start_ts=T0)
        insert_settlement(conn, TICKER, "yes", strike=Decimal("80000"), close_time=close_time)

        report = run_backtest(conn, BotConfig(), queue_assumption=QueueAssumption.PESSIMISTIC)

        assert report.trades == 0
        assert report.windows_traded == 0
        assert report.beats_trade_nothing is None  # nothing resolved to judge


class TestUnresolvedSettlement:
    def test_a_position_with_no_settlement_row_is_unresolved_not_a_loss(self, tmp_path):
        conn = make_db(tmp_path)
        seed_fillable_window(conn, TICKER, start_ts=T0)
        # no insert_settlement call: this window's result is unknown

        report = run_backtest(conn, BotConfig())

        assert report.trades == 1
        assert report.wins == 0 and report.losses == 0 and report.unresolved == 1
        assert report.total_pnl_usd == 0
        assert report.beats_trade_nothing is None


class TestRollover:
    def test_settlement_announcement_controls_when_exposure_is_released(self, tmp_path):
        conn = make_db(tmp_path)
        ticker_2 = "KXBTC15M-26SEP190015-15"
        close_1 = seed_fillable_window(conn, TICKER, start_ts=T0)
        start_2 = T0 + timedelta(seconds=10)
        close_2 = seed_fillable_window(conn, ticker_2, start_ts=start_2)
        # Window 1 is announced only after all window-2 snapshots. Its filled
        # exposure must still occupy the tight risk cap, blocking window 2.
        insert_settlement(
            conn, TICKER, "yes", strike=Decimal("80000"), close_time=close_1,
            available_ts=start_2 + timedelta(seconds=30),
        )
        insert_settlement(conn, ticker_2, "yes", strike=Decimal("80000"), close_time=close_2)
        config = BotConfig(risk={"max_contracts_per_trade": 10, "max_open_exposure_usd": Decimal("1.5"),
                                 "daily_loss_limit_usd": Decimal("20"), "max_consecutive_losses": 5,
                                 "max_trades_per_hour": 12})

        report = run_backtest(conn, config)

        assert report.trades == 1
        assert report.wins == 1

    def test_announcement_before_next_entry_releases_exposure(self, tmp_path):
        conn = make_db(tmp_path)
        ticker_2 = "KXBTC15M-26SEP190015-15"
        close_1 = seed_fillable_window(conn, TICKER, start_ts=T0)
        start_2 = T0 + timedelta(seconds=10)
        close_2 = seed_fillable_window(conn, ticker_2, start_ts=start_2)
        insert_settlement(
            conn, TICKER, "yes", strike=Decimal("80000"), close_time=close_1,
            available_ts=start_2,
        )
        insert_settlement(conn, ticker_2, "yes", strike=Decimal("80000"), close_time=close_2)
        config = BotConfig(risk={"max_contracts_per_trade": 10, "max_open_exposure_usd": Decimal("1.5"),
                                 "daily_loss_limit_usd": Decimal("20"), "max_consecutive_losses": 5,
                                 "max_trades_per_hour": 12})

        report = run_backtest(conn, config)

        assert report.trades == 2
        assert report.wins == 2

    def test_an_unfilled_resting_order_is_cancelled_at_rollover_and_frees_exposure(self, tmp_path):
        conn = make_db(tmp_path)
        # window 1: depth never shrinks, so the order rests but is never filled
        close_1 = T0 + timedelta(seconds=500)
        insert_spot_run(conn, T0 - timedelta(seconds=90), 110)
        insert_market(conn, TICKER, strike=Decimal("80000"), close_time=close_1, open_time=T0 - timedelta(seconds=300))
        for offset in range(5):
            insert_snapshot(conn, TICKER, T0 + timedelta(seconds=offset), yes=[("0.30", "20")], no=[("0.68", "15")])

        # window 2: a fresh ticker that fills and wins; if window 1's exposure was not released, a tight
        # max_open_exposure_usd would reject this order
        ticker_2 = "KXBTC15M-26SEP190015-15"
        start_2 = T0 + timedelta(seconds=10)
        close_2 = seed_fillable_window(conn, ticker_2, start_ts=start_2)
        insert_settlement(conn, ticker_2, "yes", strike=Decimal("80000"), close_time=close_2)

        config = BotConfig(risk={"max_contracts_per_trade": 10, "max_open_exposure_usd": Decimal("2"),
                                  "daily_loss_limit_usd": Decimal("20"), "max_consecutive_losses": 5,
                                  "max_trades_per_hour": 12})
        report = run_backtest(conn, config)

        assert report.windows_seen == 2
        assert report.trades == 1  # only window 2 ever fills
        assert report.wins == 1

    def test_windows_seen_counts_untraded_windows_too(self, tmp_path):
        conn = make_db(tmp_path)
        close_time = T0 + timedelta(seconds=500)
        insert_spot_run(conn, T0 - timedelta(seconds=90), 110)
        insert_market(conn, TICKER, strike=Decimal("80000"), close_time=close_time, open_time=T0 - timedelta(seconds=300))
        # depth never dips below min_depth=10, so nothing ever fills
        for offset in range(5):
            insert_snapshot(conn, TICKER, T0 + timedelta(seconds=offset), yes=[("0.30", "20")], no=[("0.68", "15")])

        report = run_backtest(conn, BotConfig())

        assert report.windows_seen == 1
        assert report.windows_traded == 0
        assert report.trades == 0


class TestStopLoss:
    def test_closes_the_position_early_without_waiting_for_settlement(self, tmp_path):
        conn = make_db(tmp_path)
        seed_window_with_price_drop(conn, TICKER, start_ts=T0)  # no settlement row inserted at all
        config = BotConfig(exit=ExitRules(stop_loss_pct=Decimal("20")))

        result = replay(conn, config)

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.exit_reason == "stop_loss"
        assert trade.result is None  # the MARKET never settled; only this position closed early
        assert trade.size == Decimal(5) and trade.exit_price == Decimal("0.20")
        expected_fee = taker_fee(Decimal(5), Decimal("0.20"))
        assert trade.fee_paid == expected_fee
        assert trade.pnl_usd == Decimal(5) * (Decimal("0.20") - Decimal("0.30")) - expected_fee

    def test_without_stop_loss_configured_holds_to_settlement_as_before(self, tmp_path):
        conn = make_db(tmp_path)
        close_time = seed_window_with_price_drop(conn, TICKER, start_ts=T0)
        insert_settlement(conn, TICKER, "no", strike=Decimal("80000"), close_time=close_time)

        result = replay(conn, BotConfig())  # exit rules default to off

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.exit_reason is None
        assert trade.result == "no"  # held all the way to settlement, exactly as before this feature existed

    def test_a_partial_exit_fill_leaves_the_remainder_tracked_to_settlement(self, tmp_path):
        conn = make_db(tmp_path)
        strike = Decimal("80000")
        close_time = seed_fillable_window(conn, TICKER, start_ts=T0, strike=strike, sizes=FULL_FILL_SIZES)
        # exactly one tick with a stop-triggering price and only 2 of the 5 held contracts' worth of resting
        # depth to sell into -- then nothing more for this ticker, so the unsold remainder rides to settlement
        # instead of a later, identical tick offering the same "fresh" depth and closing the rest too.
        insert_snapshot(conn, TICKER, T0 + timedelta(seconds=len(FULL_FILL_SIZES)), yes=[("0.20", "2")], no=[("0.79", "20")])
        insert_settlement(conn, TICKER, "no", strike=strike, close_time=close_time)
        config = BotConfig(exit=ExitRules(stop_loss_pct=Decimal("20")))

        result = replay(conn, config)

        assert len(result.trades) == 2  # the exit-closed part and the settlement-resolved remainder
        by_reason = {t.exit_reason: t for t in result.trades}
        exited, held = by_reason["stop_loss"], by_reason[None]
        assert exited.size == Decimal(2) and held.size == Decimal(3)
        assert exited.exit_price == Decimal("0.20")
        assert held.result == "no" and held.entry_price == Decimal("0.30")  # unchanged cost basis for the rest


class TestReportShape:
    def test_sample_size_note_flags_a_small_sample(self, tmp_path):
        conn = make_db(tmp_path)
        close_time = seed_fillable_window(conn, TICKER, start_ts=T0)
        insert_settlement(conn, TICKER, "yes", strike=Decimal("80000"), close_time=close_time)
        report = run_backtest(conn, BotConfig())
        assert "too small a sample" in report.sample_size_note

    def test_reports_carry_their_own_assumptions(self, tmp_path):
        conn = make_db(tmp_path)
        seed_fillable_window(conn, TICKER, start_ts=T0)
        report = run_backtest(conn, BotConfig(), queue_assumption=QueueAssumption.PESSIMISTIC, maker_fee_multiplier=Decimal("0.25"))
        assert report.queue_assumption == "pessimistic"
        assert report.maker_fee_multiplier == Decimal("0.25")
