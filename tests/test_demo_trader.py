"""Offline tests for the demo-environment trader: a fake demo client scripts what the exchange returns, so no
network, no signing and no credentials are involved (the real client's own gates are covered in test_client.py)."""

import sqlite3
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_execution import make_fill
from test_live_paper import NO_KILL_FILE, T0, TICKER, feed_fresh_spot, make_book, make_market

from btcbot.config import BotConfig, KalshiEnv
from btcbot.demo_trader import DemoTrader, compute_fill_gap, render_demo_report
from btcbot.kalshi_client import KalshiAPIError, KalshiConnectionError, KalshiWriteNotAllowedError
from btcbot.models import ParseError
from btcbot.spot_feed import SpotBuffer
from btcbot.strategy import Action, Decision

SIZES = [15, 14, 13, 12, 11, 10, 14, 0]  # the YES bid queue shrinks then refills: the paper shadow fills partway


class FakeDemoClient:
    env = KalshiEnv.DEMO

    def __init__(self):
        self.created: list[dict] = []
        self.cancelled: list[tuple[str, str | None]] = []
        self.fills: list = []
        self.resting_orders: list = []
        self.create_error: Exception | None = None
        self.cancel_error: Exception | None = None
        self._n = 0

    async def create_order(self, ticker, side, *, count, price=None, client_order_id=None):
        if self.create_error is not None:
            raise self.create_error
        self._n += 1
        self.created.append({"ticker": ticker, "side": side, "count": count, "price": price})
        return SimpleNamespace(order_id=f"ord-{self._n}")

    async def cancel_order(self, order_id, *, market_ticker=None):
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled.append((order_id, market_ticker))

    async def list_orders(self, *, ticker=None, status=None):
        return self.resting_orders

    async def list_fills(self, *, ticker=None, order_id=None, min_ts=None):
        return [f for f in self.fills if order_id is None or f.order_id == order_id]

    async def get_positions(self):
        return []


def make_trader(client=None):
    conn = sqlite3.connect(":memory:")
    buffer = SpotBuffer(window_sec=5.0, stale_after_sec=3.0)
    client = client or FakeDemoClient()
    return DemoTrader(conn, BotConfig(), buffer, client, kill_file=NO_KILL_FILE), buffer, client, conn


async def drive(trader, buffer, client, *, real_fill=None, ticker=TICKER, start_ts=T0, upto=len(SIZES)):
    """The same window test_live_paper's fill_a_window drives. ``real_fill`` (a KalshiFill) appears on the
    exchange one snapshot after the order is placed."""
    for i in range(4):
        feed_fresh_spot(trader, buffer, Decimal("80000") + i, ts=start_ts + timedelta(seconds=i))
    market = make_market(ticker)
    placed_at = None
    for offset, size in enumerate(SIZES[:upto]):
        await trader.on_orderbook_snapshot(market, make_book(yes_size=str(size)), start_ts + timedelta(seconds=offset))
        if client.created and placed_at is None:
            placed_at = offset
        if real_fill is not None and placed_at is not None and offset == placed_at + 1 and real_fill not in client.fills:
            client.fills.append(real_fill)
    return market


class TestHardLimits:
    def test_refuses_a_client_that_is_not_the_demo_environment(self):
        client = FakeDemoClient()
        client.env = KalshiEnv.PROD
        with pytest.raises(KalshiWriteNotAllowedError):
            DemoTrader(sqlite3.connect(":memory:"), BotConfig(), SpotBuffer(), client, kill_file=NO_KILL_FILE)

    def test_a_client_without_an_env_is_refused_too(self):
        with pytest.raises(KalshiWriteNotAllowedError):
            DemoTrader(sqlite3.connect(":memory:"), BotConfig(), SpotBuffer(), SimpleNamespace(), kill_file=NO_KILL_FILE)


class TestRealOrdersFollowThePaperDecisions:
    async def test_the_strategys_order_becomes_a_real_order_and_a_real_fill_becomes_the_position(self):
        trader, buffer, client, conn = make_trader()
        fill = make_fill("t-1", "ord-1", price=Decimal("0.30"), count=Decimal(4), fee=Decimal("0.0100"), fill_id="f-1")
        await drive(trader, buffer, client, real_fill=fill)

        assert client.created == [{"ticker": TICKER, "side": "yes", "count": Decimal(5), "price": Decimal("0.30")}]
        assert trader._position is not None and trader._position.size == Decimal(4)
        assert trader._position.entry_price == Decimal("0.30") and trader._position.fee_paid == Decimal("0.0100")
        row = conn.execute("SELECT side, price, size, demo_filled, demo_fee, paper_filled FROM demo_orders").fetchone()
        assert row == ("yes", "0.30", "5", "4", "0.0100", "4")  # real and shadow-paper both filled 4
        assert trader.stats.orders_placed == 1

    async def test_the_real_fee_not_the_papers_is_what_enters_the_pnl(self):
        trader, buffer, client, conn = make_trader()
        await drive(trader, buffer, client, real_fill=make_fill("t-1", "ord-1", price=Decimal("0.30"), count=Decimal(4),
                                                                  fee=Decimal("0.5000"), fill_id="f-1"))
        await trader.on_settlement(make_market(TICKER, status="finalized", raw_extra={"result": "yes"}))

        assert trader.trades[0].pnl_usd == Decimal(4) * Decimal("0.70") - Decimal("0.5000")
        demo_pnl, paper_pnl, result = conn.execute("SELECT demo_pnl, paper_pnl, result FROM demo_orders").fetchone()
        assert result == "yes" and Decimal(demo_pnl) == Decimal("2.3000")
        assert Decimal(paper_pnl) == Decimal("2.8000")  # the shadow charged its own (zero) maker fee

    async def test_a_fill_the_exchange_already_held_before_the_order_is_never_booked(self):
        trader, buffer, client, _ = make_trader()
        client.fills.append(make_fill("t-old", "ord-old", price=Decimal("0.30"), count=Decimal(9), fill_id="f-old"))
        await drive(trader, buffer, client)
        assert trader._position is None or trader._position.size != Decimal(9)


class TestWhenTheExchangeDisagreesWithTheSimulation:
    async def test_paper_fills_but_the_real_order_does_not(self):
        trader, buffer, client, conn = make_trader()
        await drive(trader, buffer, client)  # the exchange never reports a fill
        assert trader._position is None
        paper_filled, demo_filled = conn.execute("SELECT paper_filled, demo_filled FROM demo_orders").fetchone()
        assert Decimal(paper_filled) > 0 and Decimal(demo_filled) == 0

        report = render_demo_report(conn, trader.stats)
        assert "paper filled but demo did not: 1" in report

    async def test_a_rejected_order_leaves_nothing_resting_and_no_exposure(self):
        trader, buffer, client, conn = make_trader()
        client.create_error = KalshiAPIError(400, "post only cross", code="post_only_cross")
        await drive(trader, buffer, client)

        assert trader._resting_order_id is None
        assert trader.risk.open_exposure_usd == Decimal(0)
        assert trader.stats.orders_rejected >= 1 and trader.stats.orders_placed == 0
        assert conn.execute("SELECT COUNT(*) FROM demo_orders").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM demo_events WHERE event='order_rejected'").fetchone()[0] >= 1
        assert trader._shadow_order_id is None  # the paper twin was cancelled too, so it cannot fill on its own
        assert trader.trades == [] and trader._position is None

    async def test_the_clients_own_local_precondition_check_is_handled_like_any_other_rejection(self):
        # create_order() raises plain ValueError for its own local checks (count <= 0, price outside (0, 1))
        # rather than a KalshiError -- the strategy should never trigger this, but it must not crash the run.
        trader, buffer, client, conn = make_trader()
        client.create_error = ValueError("count must be positive")
        await drive(trader, buffer, client)

        assert trader._resting_order_id is None
        assert trader.risk.open_exposure_usd == Decimal(0)
        assert trader.stats.orders_rejected >= 1 and trader.stats.orders_placed == 0
        assert trader._shadow_order_id is None

    async def test_an_unreadable_acknowledgement_cancels_whatever_may_be_resting(self):
        trader, buffer, client, conn = make_trader()
        client.create_error = ParseError("create-order response payload has no 'order_id' field")
        client.resting_orders = [SimpleNamespace(order_id="ghost-1")]
        await drive(trader, buffer, client, upto=6)

        assert ("ghost-1", TICKER) in client.cancelled  # found and cancelled rather than left untracked
        assert conn.execute("SELECT COUNT(*) FROM demo_events WHERE event='order_ack_unreadable'").fetchone()[0] >= 1

    async def test_a_failed_cancel_is_remembered_and_does_not_stop_the_run(self):
        trader, buffer, client, conn = make_trader()
        await drive(trader, buffer, client, upto=3)  # an order is resting, unfilled
        assert trader._resting_order_id == "ord-1"
        client.cancel_error = KalshiConnectionError("network down")
        await trader._cancel_resting()

        assert trader._resting_order_id is None and trader.risk.open_exposure_usd == Decimal(0)
        assert trader.stats.uncancelled_order_ids == ["ord-1"]
        client.cancel_error = None
        await trader.shutdown(T0 + timedelta(seconds=60))  # one last try clears it
        assert trader.stats.uncancelled_order_ids == []
        assert ("ord-1", TICKER) in client.cancelled

    async def test_an_unfilled_orders_unrealized_exposure_is_released_on_cancel(self):
        trader, buffer, client, _ = make_trader()
        await drive(trader, buffer, client, upto=3)
        assert trader.risk.open_exposure_usd == Decimal("1.5")  # 5 contracts at 0.30
        await trader._cancel_resting()
        assert trader.risk.open_exposure_usd == Decimal(0)

    async def test_a_fill_that_lands_between_the_last_poll_and_the_cancel_is_not_double_released(self):
        # 5 ordered, 4 known filled as of the last regular poll; the 5th fills on the exchange in the gap
        # before the cancel is sent, so this trader has not polled for it yet when _cancel_resting starts.
        trader, buffer, client, _ = make_trader()
        known = make_fill("t-1", "ord-1", price=Decimal("0.30"), count=Decimal(4), fill_id="f-1")
        await drive(trader, buffer, client, real_fill=known, upto=3)
        assert trader.risk.open_exposure_usd == Decimal("1.5")  # 5 contracts at 0.30 taken when the order was placed
        assert trader._position.size == Decimal(4)
        client.fills.append(make_fill("t-1", "ord-1", price=Decimal("0.30"), count=Decimal(1), fill_id="f-2"))

        await trader._cancel_resting()

        rec = trader._latest(TICKER)
        assert rec.demo_filled == Decimal(5)  # the late fill was picked up before "unfilled" was computed
        assert trader._position.size == Decimal(5)  # ...and folded into the position, not lost
        # Nothing was actually unfilled, so cancel must release nothing yet -- only settlement releases a
        # filled contract's exposure. A stale "remaining" would wrongly release 1 contract's worth here.
        assert trader.risk.open_exposure_usd == Decimal("1.5")

        await trader.on_settlement(make_market(TICKER, status="finalized", raw_extra={"result": "yes"}))
        assert trader.risk.open_exposure_usd == Decimal(0)  # released exactly once, for exactly what was filled


class TestFillAttribution:
    async def test_a_late_fill_is_attributed_to_its_own_order_not_just_the_latest(self):
        # Two orders can rest on the same ticker within one window (place, cancel, place again). A fill for
        # the FIRST one that arrives only after the SECOND already exists must still be booked onto the
        # first order's row, not silently misattributed to "whichever order is most recent".
        trader, buffer, client, _ = make_trader()
        trader._current_ticker = TICKER
        trader._backend.sync_market(make_book(), T0)  # prime the shadow paper backend for _place_resting
        decision = Decision(Action.REST, side="yes", price=Decimal("0.30"), size=Decimal(5))
        order_a = await trader._place_resting(decision, T0)
        order_b = await trader._place_resting(decision, T0 + timedelta(seconds=1))
        assert order_a != order_b and len(trader._records[TICKER]) == 2

        client.fills.append(make_fill("t-1", order_a, ticker=TICKER, price=Decimal("0.30"), count=Decimal(3), fill_id="f-a"))
        await trader._poll_real(TICKER)

        rec_a, rec_b = trader._records[TICKER]
        assert rec_a.order_id == order_a and rec_b.order_id == order_b
        assert rec_a.demo_filled == Decimal(3)
        assert rec_b.demo_filled == Decimal(0)  # not misattributed to the most recently placed order


class TestSettlementAndReport:
    async def test_settlement_before_the_rollover_still_resolves_the_real_position(self):
        trader, buffer, client, conn = make_trader()
        await drive(trader, buffer, client, real_fill=make_fill("t-1", "ord-1", price=Decimal("0.30"), count=Decimal(4),
                                                                  fill_id="f-1"))
        await trader.on_settlement(make_market(TICKER, status="finalized", raw_extra={"result": "no"}))

        assert len(trader.trades) == 1 and trader.trades[0].pnl_usd == Decimal("-1.2")
        assert trader.risk.consecutive_losses == 1

    async def test_the_report_lists_both_columns_and_the_caveats(self):
        trader, buffer, client, conn = make_trader()
        await drive(trader, buffer, client, real_fill=make_fill("t-1", "ord-1", price=Decimal("0.30"), count=Decimal(4),
                                                                  fill_id="f-1"))
        await trader.on_settlement(make_market(TICKER, status="finalized", raw_extra={"result": "yes"}))
        report = render_demo_report(conn, trader.stats)

        assert "demo fill" in report and "paper fill" in report and "settled PnL: demo $2.8000" in report
        assert "thin and largely synthetic" in report and "not " in report
        assert "profitab" not in report.lower()

    def test_an_empty_run_reports_no_orders(self):
        conn = sqlite3.connect(":memory:")
        trader = DemoTrader(conn, BotConfig(), SpotBuffer(), FakeDemoClient(), kill_file=NO_KILL_FILE)
        assert "no orders were placed" in render_demo_report(conn, trader.stats)


class TestFillGapSummary:
    """`compute_fill_gap` is the milestone this project's roadmap calls "a paper-vs-demo fill gap we
    understand": a pure function of `demo_orders` rows, tested independently of a live DemoTrader run."""

    def test_empty_rows_report_no_data_not_a_zero_gap(self):
        gap = compute_fill_gap([])
        assert gap.orders == 0
        assert gap.mean_fill_rate_gap is None
        assert gap.mean_price_gap is None
        assert gap.mean_pnl_gap is None

    def test_counts_both_demo_only_paper_only_and_neither(self):
        rows = [
            ("T0", "yes", "0.30", "5", "5", "1.50", "0", None, "5", "1.50", "0", None, None),  # both filled
            ("T1", "yes", "0.30", "5", "5", "1.50", "0", None, "0", "0", "0", None, None),  # demo only
            ("T2", "yes", "0.30", "5", "0", "0", "0", None, "5", "1.50", "0", None, None),  # paper only
            ("T3", "yes", "0.30", "5", "0", "0", "0", None, "0", "0", "0", None, None),  # neither
        ]
        gap = compute_fill_gap(rows)
        assert (gap.orders, gap.both_filled, gap.demo_only, gap.paper_only, gap.neither) == (4, 1, 1, 1, 1)

    def test_mean_fill_rate_gap_is_positive_when_the_paper_twin_overfills(self):
        rows = [("T0", "yes", "0.30", "10", "2", "0.60", "0", None, "10", "3.00", "0", None, None)]
        gap = compute_fill_gap(rows)
        assert gap.mean_fill_rate_gap == pytest.approx(0.8)  # paper filled 100%, demo only 20%

    def test_mean_price_gap_only_counts_orders_where_both_sides_filled(self):
        rows = [
            ("T0", "yes", "0.30", "5", "5", "1.75", "0", None, "5", "1.50", "0", None, None),  # demo avg 0.35, paper 0.30
            ("T1", "yes", "0.30", "5", "0", "0", "0", None, "5", "1.50", "0", None, None),  # paper-only, excluded
        ]
        gap = compute_fill_gap(rows)
        assert gap.both_filled == 1
        assert gap.mean_price_gap == pytest.approx(0.30 - 0.35)

    def test_mean_pnl_gap_only_counts_settled_orders(self):
        rows = [
            ("T0", "yes", "0.30", "5", "5", "1.5", "0", "1.0", "5", "1.5", "0", "1.5", "yes"),  # settled: paper +0.5
            ("T1", "yes", "0.30", "5", "5", "1.5", "0", None, "5", "1.5", "0", None, None),  # unsettled, excluded
        ]
        gap = compute_fill_gap(rows)
        assert gap.settled == 1
        assert gap.mean_pnl_gap == pytest.approx(0.5)

    async def test_render_demo_report_includes_the_fill_gap_line(self):
        trader, buffer, client, conn = make_trader()
        await drive(trader, buffer, client)  # the exchange never reports a fill: a paper-only case
        report = render_demo_report(conn, trader.stats)
        assert "fill gap: mean fill-rate gap (paper - demo)" in report
