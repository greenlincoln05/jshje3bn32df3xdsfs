import itertools
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from btcbot.demo_check import FidelityReport, compare_fill_to_paper_fee_model, run_demo_check
from btcbot.kalshi_client import KalshiAPIError, KalshiConnectionError
from btcbot.models import Balance, KalshiFill, KalshiOrder, Market, Position
from btcbot.paper_broker import Fill, maker_fee, taker_fee

T0 = datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc)
TICKER = "KXBTC15M-26SEP182145-45"
SERIES = "KXBTC15M"


def make_market(ticker=TICKER, *, open_time=T0 - timedelta(seconds=60), close_time=T0 + timedelta(seconds=800), raw=None):
    return Market(
        ticker=ticker, event_ticker=ticker.rsplit("-", 1)[0], status="active", title="",
        open_time=open_time, close_time=close_time, strike=Decimal("80000"), strike_type="greater_or_equal",
        volume=Decimal(0), open_interest=Decimal(0), raw=raw or {},
    )


class FakeClient:
    """Everything run_demo_check needs from a real KalshiClient, fully scripted -- no network, no signing.
    Each rejection knob defaults to "behaves correctly" so TestHappyPath exercises every row passing."""

    def __init__(self):
        self.now = T0
        self.balance = Balance(available=Decimal("100"), portfolio_value=Decimal("100"))
        self.get_balance_raises: Exception | None = None
        self.open_markets: list[Market] = [make_market()]
        self.settled_markets: list[Market] = []
        self.fillable = True
        self.reject_bad_tick = True
        self.reject_insufficient_balance = True
        self.reject_closed_market = True
        self.cancel_actually_works = True
        self.get_order_raises: Exception | None = None  # e.g. Kalshi answering 404 for an order it just accepted
        self.sweep_raises: Exception | None = None
        self.sweeps = 0
        self.wrong_side_mapping = False  # Kalshi records a NO order as YES (a broken side mapping)
        self.orders: dict[str, KalshiOrder] = {}
        self.fills: dict[str, list[KalshiFill]] = {}
        self.positions: list[Position] = []
        self._ids = itertools.count(1)

    async def get_balance(self):
        if self.get_balance_raises:
            raise self.get_balance_raises
        return self.balance

    async def list_markets(self, *, series_ticker, status=None):
        return self.settled_markets if status == "settled" else self.open_markets

    async def get_market(self, ticker):
        for market in self.open_markets + self.settled_markets:
            if market.ticker == ticker:
                return market
        raise KalshiAPIError(404, "no such market")

    async def create_order(self, ticker, side, *, count, price=None, client_order_id=None):
        if price == Decimal("0.123456789") and self.reject_bad_tick:
            raise KalshiAPIError(400, "price is not on the tick grid")
        if price == Decimal("0.99") and count > 1000 and self.reject_insufficient_balance:
            raise KalshiAPIError(400, "insufficient balance")
        if any(m.ticker == ticker for m in self.settled_markets) and self.reject_closed_market:
            raise KalshiAPIError(400, "market is not open")
        order_id = f"ord-{next(self._ids)}"
        recorded_side = "yes" if self.wrong_side_mapping and side == "no" else side
        order = KalshiOrder(
            order_id=order_id, client_order_id=client_order_id, ticker=ticker, side=recorded_side, action="buy",
            order_type="limit" if price is not None else "market", status="resting", price=price,
            initial_count=Decimal(count), remaining_count=Decimal(count), created_time=self.now,
        )
        self.orders[order_id] = order
        if price is None and self.fillable:  # a market order that fills immediately
            self.fills[order_id] = [
                KalshiFill(
                    trade_id=f"t-{order_id}", order_id=order_id, ticker=ticker, side=side, action="buy",
                    price=Decimal("0.50"), count=Decimal(count), fee_usd=Decimal("0"), is_taker=True,
                    created_time=self.now,
                )
            ]
            self.orders[order_id] = replace(order, status="executed", remaining_count=Decimal(0))
            self.positions.append(Position(ticker=ticker, side=side, count=Decimal(count), market_exposure_usd=None))
        return order

    async def cancel_order(self, order_id, *, market_ticker=None):
        order = self.orders[order_id]
        if self.cancel_actually_works:
            self.orders[order_id] = replace(order, status="canceled", remaining_count=Decimal(0))
        return self.orders[order_id]

    async def get_order(self, order_id):
        if self.get_order_raises is not None:
            raise self.get_order_raises
        return self.orders[order_id]

    async def cancel_all_resting_orders(self):
        self.sweeps += 1
        if self.sweep_raises is not None:
            raise self.sweep_raises
        if self.cancel_actually_works:  # a broken exchange cancel is broken for cancel-all too
            for order_id, order in list(self.orders.items()):
                if not order.is_done:
                    self.orders[order_id] = replace(order, status="canceled", remaining_count=Decimal(0))
        return {}

    async def list_orders(self, *, ticker=None, status=None):
        return [o for o in self.orders.values() if ticker is None or o.ticker == ticker]

    async def list_fills(self, *, ticker=None, order_id=None, min_ts=None):
        all_fills = [f for fills in self.fills.values() for f in fills]
        return [f for f in all_fills if order_id is None or f.order_id == order_id]

    async def get_positions(self):
        return self.positions


def result_named(report, name):
    return next(r for r in report.results if r.name == name)


class TestHappyPath:
    async def test_every_check_passes_or_inconclusively_skips(self):
        client = FakeClient()
        client.settled_markets = [make_market("KXBTC15M-26SEP180000-00", raw={"result": "yes"})]

        report = await run_demo_check(client, SERIES, clock=lambda: T0)

        assert report.ticker == TICKER
        assert report.ok is True
        names = [r.name for r in report.results]
        assert "auth-check + balance" in names
        assert "market discovery" in names
        assert "crash-and-restart reconciliation" in names
        assert result_named(report, "resting YES order + cancel").passed is True
        assert result_named(report, "fillable order + position").passed is True
        assert result_named(report, "rejection: bad tick").passed is True
        assert result_named(report, "rejection: insufficient balance").passed is True
        assert result_named(report, "rejection: closed market").passed is True


class TestAuthAndDiscoveryGateEverythingElse:
    async def test_auth_failure_stops_before_anything_else(self):
        client = FakeClient()
        client.get_balance_raises = KalshiConnectionError("network down")

        report = await run_demo_check(client, SERIES, clock=lambda: T0)

        assert report.ok is False
        assert len(report.results) == 1
        assert report.results[0].name == "auth-check + balance" and report.results[0].passed is False

    async def test_no_open_market_stops_after_discovery(self):
        client = FakeClient()
        client.open_markets = []

        report = await run_demo_check(client, SERIES, clock=lambda: T0)

        assert report.ok is False
        assert [r.name for r in report.results] == ["auth-check + balance", "market discovery"]
        assert result_named(report, "market discovery").passed is False


class TestRestingOrderAndCancel:
    async def test_failure_when_cancel_does_not_actually_cancel(self):
        client = FakeClient()
        client.cancel_actually_works = False

        report = await run_demo_check(client, SERIES, clock=lambda: T0)

        assert result_named(report, "resting YES order + cancel").passed is False


class TestFillableOrder:
    async def test_failure_when_the_market_order_gets_no_fills(self):
        client = FakeClient()
        client.fillable = False

        report = await run_demo_check(client, SERIES, clock=lambda: T0)

        assert result_named(report, "fillable order + position").passed is False


class TestSettlementCheck:
    async def test_skipped_by_default(self):
        client = FakeClient()
        report = await run_demo_check(client, SERIES, clock=lambda: T0)
        result = result_named(report, "settlement check")
        assert result.passed is None and "skipped" in result.detail

    async def test_skipped_when_time_remains_even_if_asked_to_wait(self):
        client = FakeClient()  # the fixture market closes 800s from "now"
        report = await run_demo_check(client, SERIES, wait_for_settlement=True, clock=lambda: T0)
        result = result_named(report, "settlement check")
        assert result.passed is None

    async def test_passes_once_the_market_has_actually_finalized(self):
        # Discovery must see the window still open (tau > 0) for find_current_market to return it at all;
        # only the *later* settlement-check clock read sees it closed -- real elapsed time between this
        # function's own steps, not a single frozen instant, which is exactly why run_demo_check takes a
        # clock *callable* rather than one timestamp.
        client = FakeClient()
        client.open_markets = [make_market(close_time=T0 + timedelta(seconds=5), raw={"result": "yes"})]
        ticks = iter([T0, T0 + timedelta(seconds=10)])
        report = await run_demo_check(client, SERIES, wait_for_settlement=True, clock=lambda: next(ticks))
        result = result_named(report, "settlement check")
        assert result.passed is True and "settled yes" in result.detail

    async def test_inconclusive_when_closed_but_not_yet_finalized(self):
        client = FakeClient()
        client.open_markets = [make_market(close_time=T0 + timedelta(seconds=5), raw={})]  # no result yet
        ticks = iter([T0, T0 + timedelta(seconds=10)])
        report = await run_demo_check(client, SERIES, wait_for_settlement=True, clock=lambda: next(ticks))
        result = result_named(report, "settlement check")
        assert result.passed is None


class TestRejectionChecksCatchDangerousBehavior:
    async def test_bad_tick_not_rejected_is_a_failure_not_a_pass(self):
        client = FakeClient()
        client.reject_bad_tick = False
        report = await run_demo_check(client, SERIES, clock=lambda: T0)
        assert result_named(report, "rejection: bad tick").passed is False

    async def test_insufficient_balance_not_rejected_is_a_failure(self):
        client = FakeClient()
        client.reject_insufficient_balance = False
        report = await run_demo_check(client, SERIES, clock=lambda: T0)
        assert result_named(report, "rejection: insufficient balance").passed is False

    async def test_closed_market_not_rejected_is_a_failure(self):
        client = FakeClient()
        client.settled_markets = [make_market("KXBTC15M-26SEP180000-00", raw={"result": "yes"})]
        client.reject_closed_market = False
        report = await run_demo_check(client, SERIES, clock=lambda: T0)
        assert result_named(report, "rejection: closed market").passed is False

    async def test_closed_market_check_is_skipped_without_a_settled_market_to_use(self):
        client = FakeClient()  # no settled_markets configured
        report = await run_demo_check(client, SERIES, clock=lambda: T0)
        assert result_named(report, "rejection: closed market").passed is None


class TestReconciliation:
    async def test_a_stray_order_is_found_and_cancelled_by_a_fresh_backend(self):
        client = FakeClient()
        report = await run_demo_check(client, SERIES, clock=lambda: T0)
        result = result_named(report, "crash-and-restart reconciliation")
        assert result.passed is True

    async def test_failure_when_reconcile_cannot_actually_cancel_the_stray_order(self):
        client = FakeClient()
        client.cancel_actually_works = False
        report = await run_demo_check(client, SERIES, clock=lambda: T0)
        result = result_named(report, "crash-and-restart reconciliation")
        assert result.passed is False


class TestRequestBurst:
    async def test_passes_when_every_call_in_the_burst_succeeds(self):
        client = FakeClient()
        report = await run_demo_check(client, SERIES, clock=lambda: T0)
        assert result_named(report, "request burst (rate-limit survival)").passed is True


def make_fill(*, side="yes", price=Decimal("0.30"), size=Decimal(4), fee=Decimal("0"), maker=True):
    return Fill(side=side, price=price, size=size, fee=fee, maker=maker, ts=T0)


class TestCompareFillToPaperFeeModel:
    def test_predicted_fees_match_the_paper_broker_formulas(self):
        fill = make_fill(price=Decimal("0.30"), size=Decimal(4), fee=Decimal("0"), maker=True)

        comparison = compare_fill_to_paper_fee_model(fill)

        assert comparison.is_maker is True
        assert comparison.real_fee_usd == Decimal("0")
        assert comparison.predicted_taker_fee_usd == taker_fee(Decimal(4), Decimal("0.30"))
        assert comparison.predicted_maker_fee_usd_if_free == maker_fee(Decimal(4), Decimal("0.30"), multiplier=Decimal("0"))
        assert comparison.predicted_maker_fee_usd_at_quarter == maker_fee(Decimal(4), Decimal("0.30"), multiplier=Decimal("0.25"))


class TestFidelityReportSummary:
    def test_no_fills_is_explicit_about_having_nothing_to_compare(self):
        assert "nothing to compare" in FidelityReport([]).summary

    def test_taker_fills_report_how_many_matched_the_formula(self):
        report = FidelityReport([compare_fill_to_paper_fee_model(make_fill(maker=False, fee=taker_fee(Decimal(4), Decimal("0.30"))))])
        assert "1 taker fill(s): 1 matched" in report.summary

    def test_no_maker_fills_says_the_question_is_still_open(self):
        report = FidelityReport([compare_fill_to_paper_fee_model(make_fill(maker=False))])
        assert "unconfirmed" in report.summary

    def test_maker_fills_charged_zero_are_reported_as_such(self):
        report = FidelityReport([compare_fill_to_paper_fee_model(make_fill(maker=True, fee=Decimal("0")))])
        assert "1 maker fill(s): 1 charged $0" in report.summary


class TestRunDemoCheckPopulatesFidelity:
    async def test_the_happy_paths_taker_fill_is_captured_for_comparison(self):
        client = FakeClient()
        report = await run_demo_check(client, SERIES, clock=lambda: T0)
        assert len(report.fidelity.comparisons) == 1
        assert report.fidelity.comparisons[0].is_maker is False

    async def test_no_fill_means_an_empty_fidelity_report(self):
        client = FakeClient()
        client.fillable = False
        report = await run_demo_check(client, SERIES, clock=lambda: T0)
        assert report.fidelity.comparisons == []
        assert "nothing to compare" in report.fidelity.summary


class TestSideMappingIsVerifiedAgainstTheExchange:
    async def test_both_sides_are_checked_and_pass_when_kalshi_reads_them_back_correctly(self):
        report = await run_demo_check(FakeClient(), SERIES, clock=lambda: T0)
        assert result_named(report, "resting YES order + cancel").passed is True
        assert result_named(report, "resting NO order + cancel").passed is True
        assert "read back correctly" in result_named(report, "resting NO order + cancel").detail

    async def test_a_no_order_recorded_as_yes_fails_loudly_and_is_cancelled(self):
        client = FakeClient()
        client.wrong_side_mapping = True

        report = await run_demo_check(client, SERIES, clock=lambda: T0)

        result = result_named(report, "resting NO order + cancel")
        assert result.passed is False and "mapping" in result.detail and "WRONG" in result.detail
        assert report.ok is False
        assert all(order.is_done for order in client.orders.values() if order.price == Decimal("0.01"))  # nothing left resting


class TestShardCollateralPreflight:
    async def test_no_collateral_on_the_markets_shard_stops_with_one_clear_row(self):
        client = FakeClient()
        client.open_markets = [make_market(raw={"exchange_index": 2})]
        client.balance = Balance(available=Decimal("100"), portfolio_value=Decimal(0), by_exchange={0: Decimal("100"), 2: Decimal(0)})

        report = await run_demo_check(client, SERIES, clock=lambda: T0)

        row = result_named(report, "collateral on the market's exchange shard")
        assert row.passed is False and "shard 2" in row.detail and "demo-allocate" in row.detail
        assert report.ok is False
        assert [r.name for r in report.results][-1] == "collateral on the market's exchange shard"  # nothing else was tried
        assert client.orders == {}  # and no order was sent to fail the same way four times

    async def test_funded_shard_lets_the_checks_run(self):
        client = FakeClient()
        client.open_markets = [make_market(raw={"exchange_index": 2})]
        client.balance = Balance(available=Decimal("100"), portfolio_value=Decimal(0), by_exchange={2: Decimal("100")})
        report = await run_demo_check(client, SERIES, clock=lambda: T0)
        assert result_named(report, "collateral on the market's exchange shard").passed is True
        assert result_named(report, "resting YES order + cancel").passed is True

    async def test_an_unreported_shard_is_a_skip_not_a_failure(self):
        report = await run_demo_check(FakeClient(), SERIES, clock=lambda: T0)  # no exchange_index, no breakdown
        assert result_named(report, "collateral on the market's exchange shard").passed is None
        assert report.ok is True


class TestNothingIsLeftRestingOnTheAccount:
    """Regression: on the owner's real demo account, a failed read-back made demo-check return BEFORE its cancel
    step, leaving resting orders on the account ('rogue' orders, in the owner's words)."""

    async def test_an_order_that_cannot_be_read_back_is_still_cancelled(self):
        from btcbot.kalshi_client import KalshiAPIError

        client = FakeClient()
        client.get_order_raises = KalshiAPIError(404, "not found", code="not_found")

        report = await run_demo_check(client, SERIES, clock=lambda: T0)

        for side in ("YES", "NO"):
            row = result_named(report, f"resting {side} order + cancel")
            assert row.passed is False and "could not be read back" in row.detail and row.detail.endswith("; cancelled")
        assert all(o.is_done for o in client.orders.values() if o.price == Decimal("0.01"))  # none left resting

    async def test_a_failed_cancel_is_reported_not_hidden(self):
        client = FakeClient()
        client.cancel_actually_works = False
        report = await run_demo_check(client, SERIES, clock=lambda: T0)
        assert "cancel FAILED" in "\n".join(r.detail for r in report.results) or result_named(report, "resting YES order + cancel").passed is False

    async def test_the_last_row_sweeps_every_resting_order_and_passes(self):
        client = FakeClient()
        report = await run_demo_check(client, SERIES, clock=lambda: T0)
        last = report.results[-1]
        assert last.name == "cleanup: cancel any resting demo orders" and last.passed is True
        assert client.sweeps >= 1 and all(o.is_done for o in client.orders.values())  # reconcile sweeps once too

    async def test_a_sweep_that_fails_is_a_loud_failure_with_manual_instructions(self):
        from btcbot.kalshi_client import KalshiConnectionError

        client = FakeClient()
        client.sweep_raises = KalshiConnectionError("down")
        report = await run_demo_check(client, SERIES, clock=lambda: T0)
        last = report.results[-1]
        assert last.passed is False and "Orders tab" in last.detail and report.ok is False
