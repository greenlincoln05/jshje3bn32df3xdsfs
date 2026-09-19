"""Risk limits, kill switch and position/PnL tracking (Phase 4), per docs/btc15m-bot-spec.md section 5.

Every limit here is checked by :meth:`RiskManager.check_new_order` before an order is placed, never after.
"Size never increases after a loss" is enforced as an active gate, not merely an absence of a feature: a
loss of size S caps the next order at S until a win clears the cap. That is narrower than a permanent
one-way ratchet -- it stops martingale-style doubling right after a loss, which is what section 5 and
CLAUDE.md's "no size increase after a loss" are about, not a rule against ever sizing normally again.

"Pause after consecutive losses ... require manual restart" has no human in the loop during a backtest.
:meth:`RiskManager.resume` is that manual step; a backtest that never calls it after a pause is faithfully
simulating what would happen live, including however much of the run that leaves untraded.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btcbot.config import RiskLimits


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class TradeOutcome:
    ts: datetime
    size: Decimal
    pnl_usd: Decimal  # net of fees; negative is a loss


class RiskManager:
    def __init__(
        self,
        limits: RiskLimits,
        *,
        kill_file: str | Path = "KILL",
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._limits = limits
        self._kill_file = Path(kill_file)
        self._clock = clock
        self._open_exposure_usd = Decimal("0")
        self._trade_times: deque[datetime] = deque()
        self._consecutive_losses = 0
        self._paused = False
        self._pause_reason = ""
        self._daily_loss_usd = Decimal("0")
        self._daily_reset_date = None
        self._max_size_since_loss: Decimal | None = None
        self._last_order_size: Decimal | None = None
        self._account_usd: Decimal | None = None

    def set_account_value(self, usd: Decimal) -> None:
        """Tell the manager what the account is worth now (start plus SETTLED profit and loss). Only matters when
        ``max_open_exposure_pct`` / ``daily_loss_limit_pct`` are configured."""
        self._account_usd = usd

    def _max_open_exposure(self) -> Decimal:
        pct = self._limits.max_open_exposure_pct
        if pct is not None and self._account_usd is not None:
            return self._account_usd * pct / 100
        return self._limits.max_open_exposure_usd

    def _daily_loss_limit(self) -> Decimal:
        pct = self._limits.daily_loss_limit_pct
        if pct is not None and self._account_usd is not None:
            return self._account_usd * pct / 100
        return self._limits.daily_loss_limit_usd

    # ---- read-only state, useful for reporting

    @property
    def open_exposure_usd(self) -> Decimal:
        return self._open_exposure_usd

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def daily_loss_usd(self) -> Decimal:
        return self._daily_loss_usd

    @property
    def is_paused(self) -> bool:
        return self._paused

    def kill_switch_active(self) -> bool:
        return self._kill_file.exists()

    def _roll_daily_window(self, now: datetime) -> None:
        today = now.astimezone(timezone.utc).date()
        if today != self._daily_reset_date:
            self._daily_reset_date = today
            self._daily_loss_usd = Decimal("0")

    # ---- the gate

    def check_new_order(self, *, size: Decimal, price: Decimal, now: datetime | None = None) -> RiskDecision:
        now = now if now is not None else self._clock()
        self._roll_daily_window(now)
        if self.kill_switch_active():
            return RiskDecision(False, "KILL file present")
        if self._paused:
            return RiskDecision(False, f"paused: {self._pause_reason}")
        if self._daily_loss_usd >= self._daily_loss_limit():
            return RiskDecision(False, "daily loss limit reached")
        if size <= 0:
            return RiskDecision(False, "size must be positive")
        if size > self._limits.max_contracts_per_trade:
            return RiskDecision(False, "exceeds max_contracts_per_trade")
        if self._max_size_since_loss is not None and size > self._max_size_since_loss:
            return RiskDecision(False, "size increase after a loss is not allowed")
        exposure = price * size
        if self._open_exposure_usd + exposure > self._max_open_exposure():
            return RiskDecision(False, "exceeds max_open_exposure_usd")
        self._drop_trades_older_than_an_hour(now)
        if len(self._trade_times) >= self._limits.max_trades_per_hour:
            return RiskDecision(False, "exceeds max_trades_per_hour")
        return RiskDecision(True)

    def _drop_trades_older_than_an_hour(self, now: datetime) -> None:
        while self._trade_times and (now - self._trade_times[0]) > timedelta(hours=1):
            self._trade_times.popleft()

    # ---- lifecycle: call these once check_new_order has approved and the order is acted on

    def record_order_opened(self, *, size: Decimal, price: Decimal, now: datetime | None = None) -> None:
        now = now if now is not None else self._clock()
        self._trade_times.append(now)
        self._open_exposure_usd += price * size
        self._last_order_size = size

    def release_exposure(self, usd: Decimal) -> None:
        """For a position closed with an unknown outcome (e.g. replay data ends before settlement):
        release the capital without touching the win/loss streak, since the outcome is genuinely unknown."""
        self._open_exposure_usd = max(Decimal("0"), self._open_exposure_usd - usd)

    def record_trade_closed(self, outcome: TradeOutcome, *, exposure_released_usd: Decimal) -> None:
        self.release_exposure(exposure_released_usd)
        self._roll_daily_window(outcome.ts)
        if outcome.pnl_usd < 0:
            self._daily_loss_usd += -outcome.pnl_usd
            self._consecutive_losses += 1
            # The cap is the size of the ORDER that lost, not the (possibly smaller) filled amount. A partial fill
            # (ordered 5, filled 4) must not lock out every later 5-contract order: that would stop trading
            # for good, since only a win clears the cap and no win can happen without a trade.
            lost_size = max(outcome.size, self._last_order_size or Decimal(0))
            self._max_size_since_loss = (
                lost_size if self._max_size_since_loss is None else min(self._max_size_since_loss, lost_size)
            )
            if self._consecutive_losses >= self._limits.max_consecutive_losses:
                self._paused = True
                self._pause_reason = f"{self._consecutive_losses} consecutive losses"
        else:
            self._consecutive_losses = 0
            self._max_size_since_loss = None

    def resume(self) -> None:
        """The manual restart spec section 5 asks for after a consecutive-loss pause."""
        self._paused = False
        self._pause_reason = ""
        self._consecutive_losses = 0
