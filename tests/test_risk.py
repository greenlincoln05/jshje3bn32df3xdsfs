from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from btcbot.config import RiskLimits
from btcbot.risk import RiskManager, TradeOutcome

T0 = datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc)
NO_KILL_FILE = "/tmp/btcbot-test-should-never-exist-kill-file"


def make_limits(**overrides):
    defaults = dict(
        max_contracts_per_trade=10,
        max_open_exposure_usd=Decimal("25"),
        daily_loss_limit_usd=Decimal("20"),
        max_consecutive_losses=3,
        max_trades_per_hour=5,
    )
    defaults.update(overrides)
    return RiskLimits(**defaults)


def make_manager(**overrides):
    return RiskManager(make_limits(**overrides), kill_file=NO_KILL_FILE, clock=lambda: T0)


class TestBasicApproval:
    def test_approves_a_reasonable_order(self):
        r = make_manager()
        assert r.check_new_order(size=Decimal(5), price=Decimal("0.5"), now=T0).approved is True

    def test_rejects_non_positive_size(self):
        r = make_manager()
        assert r.check_new_order(size=Decimal(0), price=Decimal("0.5"), now=T0).approved is False

    def test_rejects_size_over_max_contracts_per_trade(self):
        r = make_manager(max_contracts_per_trade=10)
        decision = r.check_new_order(size=Decimal(11), price=Decimal("0.1"), now=T0)
        assert decision.approved is False and "max_contracts_per_trade" in decision.reason


class TestExposure:
    def test_tracks_open_exposure_across_orders(self):
        r = make_manager(max_open_exposure_usd=Decimal("10"))
        r.record_order_opened(size=Decimal(5), price=Decimal("1"), now=T0)
        assert r.open_exposure_usd == 5
        decision = r.check_new_order(size=Decimal(6), price=Decimal("1"), now=T0)
        assert decision.approved is False and "max_open_exposure_usd" in decision.reason

    def test_exactly_at_the_cap_is_allowed(self):
        r = make_manager(max_open_exposure_usd=Decimal("10"))
        assert r.check_new_order(size=Decimal(10), price=Decimal("1"), now=T0).approved is True

    def test_release_exposure_frees_capital_for_a_new_order(self):
        r = make_manager(max_open_exposure_usd=Decimal("10"))
        r.record_order_opened(size=Decimal(10), price=Decimal("1"), now=T0)
        assert r.check_new_order(size=Decimal(1), price=Decimal("1"), now=T0).approved is False
        r.release_exposure(Decimal("5"))
        assert r.open_exposure_usd == 5
        assert r.check_new_order(size=Decimal(5), price=Decimal("1"), now=T0).approved is True

    def test_release_exposure_never_goes_negative(self):
        r = make_manager()
        r.release_exposure(Decimal("100"))
        assert r.open_exposure_usd == 0


class TestTradesPerHour:
    def test_rejects_beyond_the_hourly_cap(self):
        r = make_manager(max_trades_per_hour=3)
        for i in range(3):
            r.record_order_opened(size=Decimal(1), price=Decimal("0.1"), now=T0 + timedelta(minutes=i))
        decision = r.check_new_order(size=Decimal(1), price=Decimal("0.1"), now=T0 + timedelta(minutes=4))
        assert decision.approved is False and "max_trades_per_hour" in decision.reason

    def test_a_trade_older_than_an_hour_rolls_off(self):
        r = make_manager(max_trades_per_hour=1)
        r.record_order_opened(size=Decimal(1), price=Decimal("0.1"), now=T0)
        assert r.check_new_order(size=Decimal(1), price=Decimal("0.1"), now=T0 + timedelta(minutes=30)).approved is False
        assert r.check_new_order(size=Decimal(1), price=Decimal("0.1"), now=T0 + timedelta(hours=1, seconds=1)).approved is True

    def test_exactly_one_hour_old_has_rolled_off(self):
        r = make_manager(max_trades_per_hour=1)
        r.record_order_opened(size=Decimal(1), price=Decimal("0.1"), now=T0)
        # strictly more than an hour is required to roll off; exactly one hour is still within the window
        decision = r.check_new_order(size=Decimal(1), price=Decimal("0.1"), now=T0 + timedelta(hours=1))
        assert decision.approved is False


class TestDailyLossLimit:
    def test_rejects_once_the_daily_limit_is_reached(self):
        r = make_manager(daily_loss_limit_usd=Decimal("10"))
        r.record_trade_closed(TradeOutcome(ts=T0, size=Decimal(1), pnl_usd=Decimal("-10")), exposure_released_usd=Decimal(0))
        decision = r.check_new_order(size=Decimal(1), price=Decimal("0.1"), now=T0)
        assert decision.approved is False and "daily loss limit" in decision.reason

    def test_wins_do_not_count_toward_the_daily_loss(self):
        r = make_manager(daily_loss_limit_usd=Decimal("10"))
        r.record_trade_closed(TradeOutcome(ts=T0, size=Decimal(1), pnl_usd=Decimal("100")), exposure_released_usd=Decimal(0))
        assert r.daily_loss_usd == 0

    def test_resets_at_utc_midnight(self):
        r = make_manager(daily_loss_limit_usd=Decimal("10"))
        r.record_trade_closed(TradeOutcome(ts=T0, size=Decimal(1), pnl_usd=Decimal("-10")), exposure_released_usd=Decimal(0))
        assert r.check_new_order(size=Decimal(1), price=Decimal("0.1"), now=T0).approved is False
        next_day = T0 + timedelta(days=1)
        assert r.check_new_order(size=Decimal(1), price=Decimal("0.1"), now=next_day).approved is True
        assert r.daily_loss_usd == 0


class TestSizeNeverIncreasesAfterALoss:
    def test_a_loss_caps_the_next_order_at_its_own_size(self):
        r = make_manager()
        r.record_trade_closed(TradeOutcome(ts=T0, size=Decimal(5), pnl_usd=Decimal("-1")), exposure_released_usd=Decimal(0))
        assert r.check_new_order(size=Decimal(6), price=Decimal("0.1"), now=T0).approved is False
        assert r.check_new_order(size=Decimal(5), price=Decimal("0.1"), now=T0).approved is True
        assert r.check_new_order(size=Decimal(4), price=Decimal("0.1"), now=T0).approved is True

    def test_consecutive_losses_ratchet_the_cap_down_never_up(self):
        r = make_manager(max_consecutive_losses=10)
        r.record_trade_closed(TradeOutcome(ts=T0, size=Decimal(5), pnl_usd=Decimal("-1")), exposure_released_usd=Decimal(0))
        r.record_trade_closed(TradeOutcome(ts=T0, size=Decimal(3), pnl_usd=Decimal("-1")), exposure_released_usd=Decimal(0))
        assert r.check_new_order(size=Decimal(4), price=Decimal("0.1"), now=T0).approved is False
        assert r.check_new_order(size=Decimal(3), price=Decimal("0.1"), now=T0).approved is True

    def test_a_win_clears_the_cap(self):
        r = make_manager()
        r.record_trade_closed(TradeOutcome(ts=T0, size=Decimal(2), pnl_usd=Decimal("-1")), exposure_released_usd=Decimal(0))
        r.record_trade_closed(TradeOutcome(ts=T0, size=Decimal(2), pnl_usd=Decimal("1")), exposure_released_usd=Decimal(0))
        assert r.check_new_order(size=Decimal(10), price=Decimal("0.1"), now=T0).approved is True

    def test_a_breakeven_trade_is_not_treated_as_a_loss(self):
        r = make_manager()
        r.record_trade_closed(TradeOutcome(ts=T0, size=Decimal(2), pnl_usd=Decimal("0")), exposure_released_usd=Decimal(0))
        assert r.consecutive_losses == 0
        assert r.check_new_order(size=Decimal(10), price=Decimal("0.1"), now=T0).approved is True


class TestConsecutiveLossPause:
    def test_pauses_after_max_consecutive_losses(self):
        r = make_manager(max_consecutive_losses=3)
        for _ in range(3):
            r.record_trade_closed(TradeOutcome(ts=T0, size=Decimal(1), pnl_usd=Decimal("-1")), exposure_released_usd=Decimal(0))
        assert r.is_paused is True
        decision = r.check_new_order(size=Decimal(1), price=Decimal("0.1"), now=T0)
        assert decision.approved is False and "paused" in decision.reason

    def test_does_not_pause_before_the_threshold(self):
        r = make_manager(max_consecutive_losses=3)
        for _ in range(2):
            r.record_trade_closed(TradeOutcome(ts=T0, size=Decimal(1), pnl_usd=Decimal("-1")), exposure_released_usd=Decimal(0))
        assert r.is_paused is False

    def test_a_win_resets_the_consecutive_loss_counter(self):
        r = make_manager(max_consecutive_losses=3)
        r.record_trade_closed(TradeOutcome(ts=T0, size=Decimal(1), pnl_usd=Decimal("-1")), exposure_released_usd=Decimal(0))
        r.record_trade_closed(TradeOutcome(ts=T0, size=Decimal(1), pnl_usd=Decimal("1")), exposure_released_usd=Decimal(0))
        assert r.consecutive_losses == 0

    def test_resume_clears_the_pause_and_the_streak(self):
        r = make_manager(max_consecutive_losses=2)
        for _ in range(2):
            r.record_trade_closed(TradeOutcome(ts=T0, size=Decimal(1), pnl_usd=Decimal("-1")), exposure_released_usd=Decimal(0))
        assert r.is_paused is True
        r.resume()
        assert r.is_paused is False
        assert r.consecutive_losses == 0
        assert r.check_new_order(size=Decimal(1), price=Decimal("0.1"), now=T0).approved is True

    def test_pause_does_not_auto_clear_on_a_new_day(self):
        r = make_manager(max_consecutive_losses=2)
        for _ in range(2):
            r.record_trade_closed(TradeOutcome(ts=T0, size=Decimal(1), pnl_usd=Decimal("-1")), exposure_released_usd=Decimal(0))
        decision = r.check_new_order(size=Decimal(1), price=Decimal("0.1"), now=T0 + timedelta(days=1))
        assert decision.approved is False  # "require manual restart": only resume() clears it


class TestKillSwitch:
    def test_no_kill_file_present_is_not_active(self, tmp_path):
        r = RiskManager(make_limits(), kill_file=tmp_path / "KILL", clock=lambda: T0)
        assert r.kill_switch_active() is False
        assert r.check_new_order(size=Decimal(1), price=Decimal("0.1"), now=T0).approved is True

    def test_kill_file_blocks_new_orders(self, tmp_path):
        kill_file = tmp_path / "KILL"
        r = RiskManager(make_limits(), kill_file=kill_file, clock=lambda: T0)
        kill_file.write_text("stop")
        decision = r.check_new_order(size=Decimal(1), price=Decimal("0.1"), now=T0)
        assert decision.approved is False and "KILL" in decision.reason


class TestPrecedence:
    def test_kill_switch_wins_over_every_other_check(self, tmp_path):
        kill_file = tmp_path / "KILL"
        kill_file.write_text("stop")
        r = RiskManager(make_limits(max_consecutive_losses=1), kill_file=kill_file, clock=lambda: T0)
        r.record_trade_closed(TradeOutcome(ts=T0, size=Decimal(1), pnl_usd=Decimal("-1")), exposure_released_usd=Decimal(0))
        decision = r.check_new_order(size=Decimal(1), price=Decimal("0.1"), now=T0)
        assert decision.approved is False and "KILL" in decision.reason


class TestPartialFillDoesNotLockOutTrading:
    def test_a_partly_filled_loss_caps_the_next_order_at_the_ordered_size(self):
        r = RiskManager(RiskLimits(), kill_file=NO_KILL_FILE, clock=lambda: T0)
        r.record_order_opened(size=Decimal(5), price=Decimal("0.3"), now=T0)
        r.record_trade_closed(TradeOutcome(ts=T0, size=Decimal(4), pnl_usd=Decimal("-1.2")), exposure_released_usd=Decimal("1.2"))
        assert r.check_new_order(size=Decimal(5), price=Decimal("0.3"), now=T0).approved is True
        assert r.check_new_order(size=Decimal(6), price=Decimal("0.3"), now=T0).approved is False
