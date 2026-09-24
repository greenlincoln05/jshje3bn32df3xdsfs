"""Paper simulation of Kalshi's BTC perpetual future (BTCPERP): the account/fill/fee/funding/liquidation rules
only, never an order (docs/research/perps-paper.md).

PAPER ONLY. There is no network code in this module and no order-placing method anywhere for perps: Kalshi's
perps trade through a separate ``/margin`` API that ``kalshi_client.py`` does not implement at all, and adding
it -- even demo-gated, the way Phase 6 gated event-contract orders -- is a separate decision that needs the
owner's explicit go-ahead (CLAUDE.md). This module only answers "what would this position have done, under
Kalshi's rules, at these prices".

Contract rules, as published by Kalshi (help.kalshi.com "BTC Perpetual Futures -- Contract Specifications", "How
Funding Works", "Perps Fees Explained"; read via search snippets 2026-09-24 because this sandbox cannot load
them -- verify against docs.kalshi.com/margin before trusting any number here, the same way every Kalshi fact
in README's dated table was verified before use):

- One contract is 0.0001 BTC; fractional contracts allowed. Reference price is CF Benchmarks' BRTI.
- Fees are charged on NOTIONAL, not margin: taker 12.0 bps at the lowest 30-day-volume tier down to 2.6 bps,
  maker 5.0 down to 0.6. A small account starts at the top of that range, so the default here is 12 bps taker.
- Funding settles every 8 hours at 12:00 AM, 8:00 AM and 4:00 PM US Eastern. Rate = premium index (TWAP of the
  perp's 1-minute premium over BRTI across the period; Kalshi's interest component is zero), clamped to
  +/-2% per period, and set to zero when its absolute value is below 0.01%. Positive: longs pay shorts.
- Maximum leverage on BTC is about 5.7x. Initial margin = notional / chosen leverage; maintenance margin is
  about 90% of initial. Under that rule even a 1x ("no margin") position is liquidated after roughly a 10%
  adverse move, because 1x posts exactly the notional and maintenance sits 10% below it.

What this module does NOT know, and therefore makes explicit, adjustable assumptions about:
- The perp's own price. There is no public perp price history this code can read, so fills and marks use a
  BTC spot proxy (Coinbase or Binance bars) plus ``half_spread_bps`` of slippage on every fill.
- The funding rate. Without the perp's price there is no premium to average, so each period's rate is a
  supplied constant (``funding_rate_8h``, default 0, i.e. the perp trades at BRTI), still passed through
  Kalshi's cap and dead zone. Sweep it: a persistent positive premium is a real, recurring cost to longs.
- What liquidation actually costs. Modelled as a forced taker close at the liquidation price minus
  ``liq_slippage_bps``; Kalshi's own liquidation fee, if any, is not published in what could be read.

Money is ``Decimal`` throughout, per the repo convention; signals upstream may be float.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

CONTRACT_BTC = Decimal("0.0001")
BPS = Decimal("0.0001")
FUNDING_HOURS_ET = (0, 8, 16)


class PerpPaperError(Exception):
    """A parameter outside what the contract or this simulation allows."""


@dataclass(frozen=True, slots=True)
class PerpSpec:
    """Kalshi BTCPERP rules plus the simulation's explicit assumptions (see the module docstring)."""

    contract_btc: Decimal = CONTRACT_BTC
    taker_fee_bps: Decimal = Decimal("12.0")
    max_leverage: Decimal = Decimal("5.7")
    maintenance_frac: Decimal = Decimal("0.9")  # maintenance margin as a fraction of initial margin
    funding_cap: Decimal = Decimal("0.02")
    funding_deadband: Decimal = Decimal("0.0001")
    half_spread_bps: Decimal = Decimal("1.0")  # assumed slippage per fill vs the spot proxy
    liq_slippage_bps: Decimal = Decimal("25.0")  # assumed extra cost of a forced close

    def __post_init__(self) -> None:
        if not (Decimal(0) < self.maintenance_frac < Decimal(1)):
            raise PerpPaperError("maintenance_frac must be strictly between 0 and 1")
        for name in ("taker_fee_bps", "half_spread_bps", "liq_slippage_bps", "funding_cap", "funding_deadband"):
            if getattr(self, name) < 0:
                raise PerpPaperError(f"{name} must not be negative")


# --------------------------------------------------------------------------- funding schedule


def _nth_sunday(year: int, month: int, n: int) -> int:
    first = datetime(year, month, 1).weekday()  # Monday = 0
    return 1 + (6 - first) % 7 + 7 * (n - 1)


def eastern_utc_offset_hours(moment: datetime) -> int:
    """-4 during US daylight time (second Sunday of March, 2 AM local, to first Sunday of November, 2 AM local),
    else -5. Hand-rolled so the owner's Windows machine needs no ``tzdata`` package for ``zoneinfo``."""
    utc = moment.astimezone(timezone.utc)
    y = utc.year
    dst_start = datetime(y, 3, _nth_sunday(y, 3, 2), 7, tzinfo=timezone.utc)  # 2 AM EST = 07:00 UTC
    dst_end = datetime(y, 11, _nth_sunday(y, 11, 1), 6, tzinfo=timezone.utc)  # 2 AM EDT = 06:00 UTC
    return -4 if dst_start <= utc < dst_end else -5


def funding_times(start: datetime, end: datetime) -> list[datetime]:
    """Every funding settlement in ``(start, end]``, as UTC datetimes: 00:00, 08:00 and 16:00 US Eastern."""
    out: list[datetime] = []
    day = (start.astimezone(timezone.utc) - timedelta(days=1)).date()
    last = end.astimezone(timezone.utc).date() + timedelta(days=1)
    while day <= last:
        for hour in FUNDING_HOURS_ET:
            naive_local = datetime(day.year, day.month, day.day, hour)
            guess = naive_local.replace(tzinfo=timezone.utc) + timedelta(hours=5)
            offset = eastern_utc_offset_hours(guess)
            t = naive_local.replace(tzinfo=timezone.utc) - timedelta(hours=offset)
            if start < t <= end:
                out.append(t)
        day += timedelta(days=1)
    return sorted(out)


def effective_funding_rate(rate: Decimal, spec: PerpSpec) -> Decimal:
    """Kalshi's clamp to +/-cap and zeroing of anything smaller than the dead band."""
    clamped = max(-spec.funding_cap, min(spec.funding_cap, rate))
    return Decimal(0) if abs(clamped) < spec.funding_deadband else clamped


# --------------------------------------------------------------------------- account


@dataclass(frozen=True, slots=True)
class PerpEvent:
    ts: datetime
    kind: str  # "open" | "close" | "liquidation" | "funding"
    contracts: Decimal  # signed position change (0 for funding)
    price: Decimal  # fill price, or the mark for funding
    fee: Decimal
    cash_delta: Decimal  # realized PnL - fee (open/close), or the funding payment (negative = paid)
    reason: str = ""


@dataclass
class PerpAccount:
    """One isolated-margin position at a time. ``cash`` includes the collateral posted for the open position;
    equity marks the open position to the given price. Leverage is chosen per position (Kalshi's own framing:
    "initial margin is set by the leverage you choose against notional size")."""

    cash: Decimal
    spec: PerpSpec = field(default_factory=PerpSpec)
    position: Decimal = Decimal(0)  # signed contracts, + long
    entry_price: Decimal = Decimal(0)
    margin: Decimal = Decimal(0)  # initial margin posted for the open position
    leverage: Decimal = Decimal(0)
    fees_paid: Decimal = Decimal(0)
    funding_paid: Decimal = Decimal(0)  # net, positive = paid out
    liquidations: int = 0
    events: list[PerpEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.cash <= 0:
            raise PerpPaperError("starting cash must be positive")

    @property
    def is_open(self) -> bool:
        return self.position != 0

    def notional(self, mark: Decimal) -> Decimal:
        return abs(self.position) * self.spec.contract_btc * mark

    def unrealized(self, mark: Decimal) -> Decimal:
        return self.position * self.spec.contract_btc * (mark - self.entry_price)

    def equity(self, mark: Decimal) -> Decimal:
        return self.cash + (self.unrealized(mark) if self.is_open else Decimal(0))

    def liquidation_price(self) -> Decimal | None:
        """Where position equity (margin + unrealized PnL) falls to the maintenance margin."""
        if not self.is_open:
            return None
        move = (1 - self.spec.maintenance_frac) / self.leverage
        return self.entry_price * (1 - move) if self.position > 0 else self.entry_price * (1 + move)

    def _fill_price(self, mid: Decimal, buying: bool, extra_bps: Decimal = Decimal(0)) -> Decimal:
        slip = (self.spec.half_spread_bps + extra_bps) * BPS
        return mid * (1 + slip) if buying else mid * (1 - slip)

    def _fee(self, contracts: Decimal, price: Decimal) -> Decimal:
        return abs(contracts) * self.spec.contract_btc * price * self.spec.taker_fee_bps * BPS

    def open(self, ts: datetime, side: int, mid: Decimal, leverage: Decimal, *, reason: str = "") -> PerpEvent:
        """Open a new position worth ``leverage`` x current equity in notional, as a taker. Sizing off CURRENT
        equity is fixed-fractional: it shrinks after a loss and never grows to chase one."""
        if self.is_open:
            raise PerpPaperError("a position is already open; close it first")
        if side not in (1, -1):
            raise PerpPaperError("side must be +1 (long) or -1 (short)")
        if not (Decimal(0) < leverage <= self.spec.max_leverage):
            raise PerpPaperError(f"leverage must be in (0, {self.spec.max_leverage}]")
        price = self._fill_price(mid, buying=side > 0)
        # Size so that margin + the entry fee both fit inside current cash.
        per_contract = self.spec.contract_btc * price * (1 / leverage + self.spec.taker_fee_bps * BPS)
        contracts = (self.cash / per_contract).quantize(Decimal("0.01"), rounding="ROUND_DOWN")
        if contracts <= 0:
            raise PerpPaperError("not enough cash to open even 0.01 contracts")
        fee = self._fee(contracts, price)
        self.position = side * contracts
        self.entry_price = price
        self.leverage = leverage
        self.margin = contracts * self.spec.contract_btc * price / leverage
        self.cash -= fee
        self.fees_paid += fee
        event = PerpEvent(ts, "open", self.position, price, fee, -fee, reason)
        self.events.append(event)
        return event

    def close(self, ts: datetime, mid: Decimal, *, reason: str = "", liquidation: bool = False) -> PerpEvent:
        if not self.is_open:
            raise PerpPaperError("no position to close")
        extra = self.spec.liq_slippage_bps if liquidation else Decimal(0)
        price = mid if liquidation and extra == 0 else self._fill_price(mid, buying=self.position < 0, extra_bps=extra)
        pnl = self.position * self.spec.contract_btc * (price - self.entry_price)
        fee = self._fee(self.position, price)
        # Isolated margin: a position can never lose more than the collateral posted for it.
        pnl = max(pnl, -self.margin)
        self.cash += pnl - fee
        self.fees_paid += fee
        change = -self.position
        self.position = Decimal(0)
        self.entry_price = self.margin = self.leverage = Decimal(0)
        kind = "liquidation" if liquidation else "close"
        if liquidation:
            self.liquidations += 1
        event = PerpEvent(ts, kind, change, price, fee, pnl - fee, reason)
        self.events.append(event)
        return event

    def check_liquidation(self, ts: datetime, bar_low: Decimal, bar_high: Decimal) -> PerpEvent | None:
        """Force-close at the liquidation price if this bar's adverse extreme reached it."""
        liq = self.liquidation_price()
        if liq is None:
            return None
        hit = bar_low <= liq if self.position > 0 else bar_high >= liq
        return self.close(ts, liq, reason="maintenance margin breached", liquidation=True) if hit else None

    def apply_funding(self, ts: datetime, rate: Decimal, mark: Decimal) -> PerpEvent | None:
        """One settlement: a positive effective rate moves ``rate x notional`` from longs to shorts."""
        if not self.is_open:
            return None
        eff = effective_funding_rate(rate, self.spec)
        if eff == 0:
            return None
        payment = eff * self.notional(mark) * (1 if self.position > 0 else -1)  # positive = this account pays
        self.cash -= payment
        self.funding_paid += payment
        event = PerpEvent(ts, "funding", Decimal(0), mark, Decimal(0), -payment, f"rate {eff}")
        self.events.append(event)
        return event
