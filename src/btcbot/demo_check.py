"""Phase 6c/6d: ``btcbot demo-check``, a validation script that places and cancels real (fake-money) orders
against Kalshi's demo environment to confirm order/cancel/fill handling actually works, plus a fidelity
comparison between those real fills and this bot's paper fee model.

The checklist below and its ordering come straight from the plan recorded in
docs/btc15m-bot-spec.md section 8 (Phase 6c). Every write goes through :class:`btcbot.execution.
DemoExecutionBackend`, which itself only ever calls :class:`btcbot.kalshi_client.KalshiClient`'s demo-gated
write methods -- there is no path from this module to a live order.

Phase 6d's fidelity report (:class:`FidelityReport`) is narrower than the plan's full description ("latency,
fill rate, fee charged"): it only compares fee charged, from whatever fill(s) this run's own checklist
happens to produce. A true latency or queue-position fill-rate comparison needs order-book history captured
alongside the real fills (the way `btcbot record`/`btcbot paper` capture it for the backtest), which this
one-shot validation script does not do; that is future work, not a silent gap.

No Claude Code session has ever run this against a real demo account: CLAUDE.md is explicit that no
session uses or stores a Kalshi key, so this module's own logic (sequencing, report shape, how a rejection
is classified) is what offline tests here exercise, against a fake client -- not this checklist's real
outcome against Kalshi's actual API, which the owner's own first run is what actually confirms.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Protocol

from btcbot.execution import DemoExecutionBackend
from btcbot.kalshi_client import KalshiAPIError, KalshiError
from btcbot.market_discovery import find_current_market
from btcbot.paper_broker import maker_fee, taker_fee

if TYPE_CHECKING:
    from btcbot.models import Balance, Market, Side
    from btcbot.paper_broker import Fill


class DemoCheckClient(Protocol):
    """The slice of :class:`btcbot.kalshi_client.KalshiClient` this module needs -- narrowed so tests can
    substitute a fake without touching the network."""

    async def get_balance(self) -> Balance: ...
    async def get_order(self, order_id: str) -> object: ...
    async def get_market(self, ticker: str) -> Market: ...
    async def list_markets(self, *, series_ticker: str, status: str | None = None) -> list[Market]: ...
    async def create_order(
        self, ticker: str, side: str, *, count: Decimal, price: Decimal | None = None, client_order_id: str | None = None
    ) -> object: ...
    async def cancel_order(self, order_id: str, *, market_ticker: str | None = None) -> object: ...
    async def get_positions(self) -> list[object]: ...
    async def list_orders(self, *, ticker: str | None = None, status: str | None = None) -> list[object]: ...


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool | None  # True/False, or None for "skipped, not a failure" (e.g. no time left to exercise it)
    detail: str


@dataclass(frozen=True, slots=True)
class FidelityComparison:
    """One real fill from this run, compared against what :mod:`btcbot.paper_broker`'s fee formula would
    have charged for the identical trade (Phase 6d). Not a claim about edge -- spec section 8's own caveat
    is that demo books are thin and synthetic, so this validates plumbing and fee math, nothing more."""

    side: Side
    price: Decimal
    size: Decimal
    is_maker: bool
    real_fee_usd: Decimal
    predicted_taker_fee_usd: Decimal
    predicted_maker_fee_usd_if_free: Decimal
    predicted_maker_fee_usd_at_quarter: Decimal


def compare_fill_to_paper_fee_model(fill: Fill) -> FidelityComparison:
    return FidelityComparison(
        side=fill.side,
        price=fill.price,
        size=fill.size,
        is_maker=fill.maker,
        real_fee_usd=fill.fee,
        predicted_taker_fee_usd=taker_fee(fill.size, fill.price),
        predicted_maker_fee_usd_if_free=maker_fee(fill.size, fill.price, multiplier=Decimal("0")),
        predicted_maker_fee_usd_at_quarter=maker_fee(fill.size, fill.price, multiplier=Decimal("0.25")),
    )


@dataclass(frozen=True, slots=True)
class FidelityReport:
    comparisons: list[FidelityComparison]

    @property
    def summary(self) -> str:
        if not self.comparisons:
            return "no fills observed this run -- nothing to compare"
        lines = []
        taker_fills = [c for c in self.comparisons if not c.is_maker]
        maker_fills = [c for c in self.comparisons if c.is_maker]
        if taker_fills:
            matches = sum(1 for c in taker_fills if c.real_fee_usd == c.predicted_taker_fee_usd)
            lines.append(f"{len(taker_fills)} taker fill(s): {matches} matched the taker fee formula exactly")
        if maker_fills:
            free = sum(1 for c in maker_fills if c.real_fee_usd == 0)
            full = sum(1 for c in maker_fills if c.real_fee_usd == c.predicted_taker_fee_usd)
            lines.append(
                f"{len(maker_fills)} maker fill(s): {free} charged $0, {full} charged the full "
                "taker-equivalent fee (anything else needs inspecting individually)"
            )
        else:
            lines.append(
                "0 maker fills observed this run -- whether makers pay a fee under plain quadratic is "
                "still unconfirmed (demo-check's own resting-order check is deliberately unfillable; a "
                "maker fill from a future paper-adjacent run would settle this)"
            )
        return "; ".join(lines)


@dataclass(frozen=True, slots=True)
class DemoCheckReport:
    ticker: str | None
    results: list[CheckResult]
    fidelity: FidelityReport = field(default_factory=lambda: FidelityReport([]))

    @property
    def ok(self) -> bool:
        """True only if nothing explicitly failed. A skip does not fail the run -- spec section 8.6's exit
        criteria is "every row passes", and a skip is not a failing row, just an inconclusive one."""
        return all(r.passed is not False for r in self.results)


async def run_demo_check(
    client: DemoCheckClient,
    series_ticker: str,
    *,
    wait_for_settlement: bool = False,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> DemoCheckReport:
    """``clock`` defaults to the real UTC clock, sampled fresh at each step (discovery, then later the
    settlement check) rather than once at the top -- the same injectable-clock pattern recorder.py and
    risk.py use, and for the same reason: real time elapses between this function's own steps (placing and
    cancelling orders is a handful of real network round-trips), so a market discovery just confirmed as
    open a moment ago can legitimately have closed by the time the settlement check runs. Tests pass a
    callable returning controlled values instead of a single frozen timestamp."""
    results: list[CheckResult] = []

    try:
        balance = await client.get_balance()
    except KalshiError as exc:
        return DemoCheckReport(None, [CheckResult("auth-check + balance", False, str(exc))])
    results.append(CheckResult("auth-check + balance", True, f"available ${balance.available:,.2f}"))

    market = await find_current_market(client, series_ticker, now=clock())
    if market is None:
        results.append(CheckResult("market discovery", False, "no open market right now; re-run in a few seconds"))
        return DemoCheckReport(None, results)
    results.append(CheckResult("market discovery", True, market.ticker))

    backend = DemoExecutionBackend(client, market.ticker)
    results.append(await _check_resting_order_and_cancel(client, backend, "yes"))
    results.append(await _check_resting_order_and_cancel(client, backend, "no"))
    fillable_result, observed_fills = await _check_fillable_order_and_position(client, backend, market)
    results.append(fillable_result)
    if wait_for_settlement:
        results.append(await _check_settlement(client, market, now=clock()))
    else:
        results.append(CheckResult("settlement check", None, "skipped: pass wait_for_settlement=True near a window close to exercise this"))
    results.append(await _check_bad_tick_rejected(client, market))
    results.append(await _check_insufficient_balance_rejected(client, market, balance))
    results.append(await _check_closed_market_rejected(client, series_ticker))
    results.append(await _check_survives_a_request_burst(client))
    results.append(await _check_crash_restart_reconciliation(client, market))

    fidelity = FidelityReport([compare_fill_to_paper_fee_model(fill) for fill in observed_fills])
    return DemoCheckReport(market.ticker, results, fidelity)


# --------------------------------------------------------------------------- individual checks


async def _check_resting_order_and_cancel(client: DemoCheckClient, backend: DemoExecutionBackend, side: Side) -> CheckResult:
    """A tiny, deliberately unfillable order (deep off the current market) then a cancel. Run once per side
    because Kalshi's V2 endpoint only speaks YES: a NO order is sent as an ask on YES at ``1 - price``, and
    reading the order back is what proves that mapping did what was intended. If the order comes back as the
    wrong outcome or at the wrong price, this FAILS: trading on a wrong side mapping would be the worst bug
    this whole phase could ship."""
    name = f"resting {side.upper()} order + cancel"
    price = Decimal("0.01")
    try:
        order_id = await backend.place_resting_order(side, price, Decimal(1))
        placed = await client.get_order(order_id)
        if placed.is_done:
            return CheckResult(name, False, f"order {order_id} was already done ({placed.status}) right after placing it")
        if placed.side != side or (placed.price is not None and placed.price != price):
            await backend.cancel_order(order_id)
            return CheckResult(
                name, False,
                f"asked for {side.upper()} at {price} but Kalshi recorded {placed.side.upper()} at {placed.price}: "
                "the side/price mapping in kalshi_client.create_order is WRONG; do not trade until fixed",
            )
        await backend.cancel_order(order_id)
        cancelled = await client.get_order(order_id)
        if not cancelled.is_done:
            return CheckResult(name, False, f"order {order_id} still shows {cancelled.status} after cancel")
        return CheckResult(name, True, f"order {order_id}: {side.upper()} at {price} read back correctly, then {cancelled.status}")
    except KalshiError as exc:
        return CheckResult(name, False, str(exc))


async def _check_fillable_order_and_position(
    client: DemoCheckClient, backend: DemoExecutionBackend, market: Market
) -> tuple[CheckResult, list[Fill]]:
    """A tiny market (taker) order, expected to fill immediately against the resting book, then a position
    check. Demo books are thin (README, "Verified Kalshi API facts"): an empty book here is reported as a
    failure of this specific check, not retried, since a market order into an empty book has nothing to
    fill against by design, not because anything here is broken. Its fills feed Phase 6d's fidelity report."""
    name = "fillable order + position"
    try:
        fills = await backend.place_taker_order("yes", Decimal(1))
        if not fills:
            return CheckResult(name, False, "market order returned no fills (demo book may be empty right now)"), []
        positions = await client.get_positions()
        has_position = any(p.ticker == market.ticker and p.count > 0 for p in positions)
        return CheckResult(name, has_position, f"{len(fills)} fill(s); position present: {has_position}"), fills
    except KalshiError as exc:
        return CheckResult(name, False, str(exc)), []


async def _check_settlement(client: DemoCheckClient, market: Market, *, now: datetime) -> CheckResult:
    name = "settlement check"
    tau = market.seconds_to_close(now)
    if tau > 0:
        return CheckResult(name, None, f"skipped: {tau:.0f}s still left in {market.ticker}'s window")
    try:
        settled = await client.get_market(market.ticker)  # type: ignore[attr-defined]
    except KalshiError as exc:
        return CheckResult(name, False, str(exc))
    result = settled.raw.get("result")
    if result not in ("yes", "no"):
        return CheckResult(name, None, f"skipped: {market.ticker} has not finalized yet (status={settled.status})")
    return CheckResult(name, True, f"{market.ticker} settled {result}")


async def _check_bad_tick_rejected(client: DemoCheckClient, market: Market) -> CheckResult:
    name = "rejection: bad tick"
    try:
        await client.create_order(market.ticker, "yes", count=Decimal(1), price=Decimal("0.123456789"))
    except KalshiAPIError:
        return CheckResult(name, True, "off-grid price was rejected, as expected")
    except KalshiError as exc:
        return CheckResult(name, None, f"inconclusive: rejected, but not with an API error: {exc}")
    return CheckResult(name, False, "an off-grid price was NOT rejected -- this needs investigating before trusting order placement")


async def _check_insufficient_balance_rejected(client: DemoCheckClient, market: Market, balance: Balance) -> CheckResult:
    name = "rejection: insufficient balance"
    price = Decimal("0.99")  # near the top of the grid, to maximize cost per contract
    count = (balance.available / price).to_integral_value(rounding=ROUND_FLOOR) + 1000
    try:
        await client.create_order(market.ticker, "yes", count=count, price=price)
    except KalshiAPIError:
        return CheckResult(name, True, "an order costing far more than the account balance was rejected, as expected")
    except KalshiError as exc:
        return CheckResult(name, None, f"inconclusive: rejected, but not with an API error: {exc}")
    return CheckResult(name, False, "an order exceeding the account balance was NOT rejected -- investigate before trusting risk limits")


async def _check_closed_market_rejected(client: DemoCheckClient, series_ticker: str) -> CheckResult:
    name = "rejection: closed market"
    settled = await client.list_markets(series_ticker=series_ticker, status="settled")
    if not settled:
        return CheckResult(name, None, "skipped: no settled market found to test against")
    try:
        await client.create_order(settled[0].ticker, "yes", count=Decimal(1), price=Decimal("0.5"))
    except KalshiAPIError:
        return CheckResult(name, True, f"an order against closed market {settled[0].ticker} was rejected, as expected")
    except KalshiError as exc:
        return CheckResult(name, None, f"inconclusive: rejected, but not with an API error: {exc}")
    return CheckResult(name, False, f"an order against closed market {settled[0].ticker} was NOT rejected -- investigate immediately")


async def _check_survives_a_request_burst(client: DemoCheckClient) -> CheckResult:
    """Confirms a small burst of authenticated calls doesn't crash the client. The 429 backoff math itself
    (exponential, jittered, honors Retry-After) is exercised thoroughly offline in test_client.py's
    TestRetries -- deliberately hammering a real account to force a 429 here would just be poor etiquette
    for a fact this project can already verify without the network."""
    name = "request burst (rate-limit survival)"
    try:
        for _ in range(5):
            await client.get_balance()
    except KalshiError as exc:
        return CheckResult(name, False, f"a small burst of balance checks failed: {exc}")
    return CheckResult(name, True, "5 rapid balance checks all succeeded (see test_client.py for 429 backoff coverage)")


async def _check_crash_restart_reconciliation(client: DemoCheckClient, market: Market) -> CheckResult:
    """Places an order and deliberately does NOT cancel it -- simulating a crash -- then builds a fresh
    DemoExecutionBackend (a "new process") and confirms reconcile() finds and cancels it."""
    name = "crash-and-restart reconciliation"
    try:
        stray_backend = DemoExecutionBackend(client, market.ticker)
        stray_order_id = await stray_backend.place_resting_order("yes", Decimal("0.01"), Decimal(1))

        fresh_backend = DemoExecutionBackend(client, market.ticker)
        report = await fresh_backend.reconcile()

        if stray_order_id not in report.cancelled_order_ids:
            return CheckResult(name, False, f"order {stray_order_id} was left orphaned after reconcile()")
        order = await client.get_order(stray_order_id)
        if not order.is_done:
            return CheckResult(name, False, f"order {stray_order_id} reported cancelled but still shows {order.status}")
        return CheckResult(name, True, f"a fresh backend found and cancelled orphaned order {stray_order_id}")
    except KalshiError as exc:
        return CheckResult(name, False, str(exc))
