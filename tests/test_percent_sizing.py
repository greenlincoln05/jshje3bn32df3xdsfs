"""Percent-of-account sizing: bets grow gradually after settled wins, follow the account down, and never grow after a
loss. Offline: pure function, the risk manager, the live trader on a fake book, and the lab replay."""

import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from test_lab import WIN, T0 as LAB_T0, load_replay_data, make_db, prepare_replay, replay_prepared, seed_window
from test_live_paper import NO_KILL_FILE, T0, fill_a_window, make_book, make_market, make_trader

from btcbot.backtest import EntryFilters
from btcbot.config import BotConfig, RiskLimits, Sizing, SizingMode
from btcbot.paper_broker import PaperBroker, QueueAssumption
from btcbot.risk import RiskManager, TradeOutcome
from btcbot.strategy import percent_size

D = Decimal


def size(price="0.30", cash="20", pct="10", prev=None, last=None, growth="20", cap="1000"):
    return percent_size(D(price), cash_usd=D(cash), risk_pct=D(pct), previous_size=None if prev is None else D(prev),
                        last_result=last, max_growth_pct=None if growth is None else D(growth), max_contracts=D(cap))


class TestPercentSizeRules:
    def test_the_target_is_a_percent_of_the_account_floored_to_whole_contracts(self):
        assert size(cash="20", pct="10") == 6        # $2.00 / 0.30 = 6.67 -> 6
        assert size(cash="1000", pct="2") == 66      # $20 / 0.30
        assert size(cash="20", pct="10", cap="4") == 4

    def test_an_account_too_small_for_one_contract_returns_zero(self):
        assert size(cash="1", pct="10") == 0         # $0.10 / 0.30 < 1
        assert size(price="0") == 0

    def test_after_a_win_the_next_order_grows_by_at_most_the_configured_percent(self):
        assert size(cash="60", pct="10", prev="6", last="win", growth="20") == 7   # target 20, capped at 6 + max(1, 1)
        assert size(cash="600", pct="10", prev="20", last="win", growth="25") == 25  # 20 + 25%
        assert size(cash="60", pct="10", prev="6", last="win", growth="0") == 7      # always at least one step

    def test_a_small_size_can_still_move_by_one_contract(self):
        assert size(cash="60", pct="10", prev="2", last="win", growth="10") == 3

    def test_growth_never_overshoots_the_target(self):
        assert size(cash="22.8", pct="10", prev="6", last="win", growth="500") == 7   # the target is 7, not 6 + 30

    def test_after_a_loss_the_size_can_never_exceed_the_previous_order(self):
        assert size(cash="600", pct="10", prev="6", last="loss", growth="20") == 6
        assert size(cash="600", pct="10", prev="6", last="loss", growth=None) == 6

    def test_a_smaller_account_shrinks_the_order_immediately_even_after_a_win(self):
        assert size(cash="10", pct="10", prev="20", last="win") == 3

    def test_with_no_ramp_configured_the_target_is_used_after_a_win(self):
        assert size(cash="60", pct="10", prev="6", last="win", growth=None) == 20

    def test_the_first_order_has_no_previous_size_to_ramp_from(self):
        assert size(cash="600", pct="10", prev=None) == 200


class TestAccountRelativeRiskLimits:
    def limits(self, **kw):
        return RiskLimits(max_contracts_per_trade=1000, max_open_exposure_usd=D("25"), daily_loss_limit_usd=D("20"), **kw)

    def manager(self, **kw):
        return RiskManager(self.limits(**kw), kill_file=NO_KILL_FILE, clock=lambda: T0)

    def test_the_dollar_limit_applies_when_no_percent_is_configured(self):
        r = self.manager(max_open_exposure_pct=None)
        r.set_account_value(D("10000"))  # ignored: no percent configured
        assert r.check_new_order(size=D(100), price=D("0.5"), now=T0).approved is False  # $50 > $25

    def test_the_exposure_cap_grows_and_shrinks_with_the_account(self):
        r = self.manager(max_open_exposure_pct=D(10))
        r.set_account_value(D("1000"))
        assert r.check_new_order(size=D(100), price=D("0.5"), now=T0).approved is True     # $50 <= 10% of 1000
        r.set_account_value(D("300"))
        assert r.check_new_order(size=D(100), price=D("0.5"), now=T0).approved is False    # $50 > 10% of 300

    def test_without_an_account_value_the_dollar_fallback_is_used(self):
        r = self.manager(max_open_exposure_pct=D(10))
        assert r.check_new_order(size=D(100), price=D("0.5"), now=T0).approved is False    # falls back to $25

    def test_the_daily_loss_limit_also_follows_the_account(self):
        r = self.manager(daily_loss_limit_pct=D(1))
        r.set_account_value(D("1000"))                                                      # limit $10
        r.record_trade_closed(TradeOutcome(ts=T0, size=D(1), pnl_usd=D("-11")), exposure_released_usd=D(0))
        assert r.check_new_order(size=D(1), price=D("0.1"), now=T0).approved is False

    def test_the_no_increase_after_a_loss_rule_is_untouched(self):
        r = self.manager(max_open_exposure_pct=D(50))
        r.set_account_value(D("10000"))
        r.record_order_opened(size=D(5), price=D("0.3"), now=T0)
        r.record_trade_closed(TradeOutcome(ts=T0, size=D(5), pnl_usd=D("-1")), exposure_released_usd=D("1.5"))
        assert r.check_new_order(size=D(6), price=D("0.3"), now=T0).approved is False


class TestConfig:
    def test_percent_mode_and_its_defaults_are_valid(self):
        cfg = BotConfig(sizing=Sizing(mode="percent"))
        assert cfg.sizing.mode is SizingMode.PERCENT and cfg.sizing.risk_pct_per_trade == Decimal("0.5")
        assert cfg.sizing.account_usd == 500 and cfg.sizing.max_growth_per_win_pct == 20

    @pytest.mark.parametrize("kw", [{"risk_pct_per_trade": 0}, {"risk_pct_per_trade": 11}, {"account_usd": 0},
                                    {"max_growth_per_win_pct": -1}, {"max_growth_per_win_pct": 101}])
    def test_dangerous_or_nonsense_values_are_rejected(self, kw):
        with pytest.raises(ValidationError):
            Sizing(mode="percent", **kw)

    def test_the_default_mode_is_percent(self):
        assert BotConfig().sizing.mode is SizingMode.PERCENT
        assert BotConfig().risk.max_open_exposure_pct == 5 and BotConfig().risk.daily_loss_limit_pct == 4


def percent_config():
    return BotConfig(
        sizing=Sizing(mode="percent", account_usd=D("20"), risk_pct_per_trade=D("10"), max_growth_per_win_pct=D("20")),
        risk=RiskLimits(max_contracts_per_trade=1000, max_open_exposure_pct=D(80), daily_loss_limit_pct=D(80),
                        max_trades_per_hour=100, max_consecutive_losses=100),
    )


async def one_window(trader, buffer, index, result):
    """Fill window ``index``, settle it before the next window's first snapshot, and report (traded, ordered size).
    The shared fill helper only manages to fill every other window (true in fixed mode too), so callers look at the
    windows that traded."""
    start = T0 + timedelta(seconds=index * 1000)
    ticker = f"KXBTC15M-PCT{index:03d}-00"
    await fill_a_window(trader, buffer, ticker=ticker, close_time=start + timedelta(seconds=500), start_ts=start)
    traded = trader._position is not None
    ordered = trader._last_order_size
    await trader.on_settlement(make_market(ticker, status="finalized", raw_extra={"result": result}))
    return traded, ordered


async def run_windows(trader, buffer, results):
    """Settled results per window; returns [(window, ordered size, bankroll after settlement)] for windows that traded."""
    rows = []
    for i, result in enumerate(results):
        traded, ordered = await one_window(trader, buffer, i, result)
        if traded:
            rows.append((i, ordered, trader.bankroll))
    return rows


class TestLiveTraderPercentMode:
    async def test_bets_start_at_the_percent_and_grow_gradually_after_settled_wins(self):
        trader, buffer = make_trader(sqlite3.connect(":memory:"), config=percent_config())
        rows = await run_windows(trader, buffer, ["yes"] * 8)
        sizes, banks = [r[1] for r in rows], [r[2] for r in rows]
        assert len(rows) == 4
        assert sizes[0] == 6                                     # 10% of $20 = $2 at 0.30 -> 6 contracts
        assert all(b > a for a, b in zip(sizes, sizes[1:]))      # every settled win made the next order bigger...
        assert all(b - a <= 2 for a, b in zip(sizes, sizes[1:]))  # ...but only slightly (20% ramp, one step minimum)
        assert banks[0] == D("20") + D("2.80") and banks == sorted(banks)  # 4 filled contracts won $0.70 each

    async def test_a_loss_never_grows_the_next_bet_and_shrinks_the_account(self):
        trader, buffer = make_trader(sqlite3.connect(":memory:"), config=percent_config())
        # traded windows are 0, 2, 4, 6: yes, yes, LOSS, yes
        rows = await run_windows(trader, buffer, ["yes", "yes", "yes", "yes", "no", "yes", "yes", "yes"])
        (_, s0, b0), (_, s1, b1), (_, s2, b2), (_, s3, b3) = rows
        assert s0 < s1 < s2                                      # grew after the two wins
        assert b2 < b1                                           # the loss shrank the account
        assert s3 <= s2                                          # the order after the loss is never larger than the loser
        assert b3 > b2                                           # and the win after it grows the account again

    async def test_the_account_only_moves_with_settled_results(self):
        trader, buffer = make_trader(sqlite3.connect(":memory:"), config=percent_config())
        start = T0
        await fill_a_window(trader, buffer, ticker="KXBTC15M-PCT900-00", close_time=start + timedelta(seconds=500), start_ts=start)
        assert trader._position is not None and trader.bankroll == D("20")   # a filled, unsettled position changes nothing
        await trader.on_settlement(make_market("KXBTC15M-PCT900-00", status="finalized", raw_extra={"result": "yes"}))
        assert trader.bankroll == D("22.80")

    async def test_an_account_too_small_for_one_contract_skips_the_trade(self):
        cfg = percent_config()
        tiny = BotConfig(sizing=Sizing(mode="percent", account_usd=D("1"), risk_pct_per_trade=D("1")), risk=cfg.risk)
        trader, buffer = make_trader(sqlite3.connect(":memory:"), config=tiny)
        await fill_a_window(trader, buffer)
        assert trader._resting_order_id is None and trader._position is None

    async def test_fixed_mode_is_untouched_by_all_of_this(self):
        trader, buffer = make_trader(sqlite3.connect(":memory:"), config=BotConfig(sizing=Sizing(mode="fixed")))
        rows = await run_windows(trader, buffer, ["yes"] * 4)
        assert [r[1] for r in rows] == [5, 5]                     # contracts_per_trade, wins or not


    async def test_ramp_mode_grows_after_wins(self):
        trader, buffer = make_trader(sqlite3.connect(":memory:"), config=BotConfig(sizing=Sizing(mode="ramp")))
        rows = await run_windows(trader, buffer, ["yes"] * 4)
        sizes = [r[1] for r in rows]
        assert sizes[0] == 5 and sizes[1] > sizes[0]


class TestLabReplay:
    def spied_sizes(self, tmp_path, results, filters, cfg):
        conn = make_db(tmp_path)
        for i, r in enumerate(results):
            seed_window(conn, i, r)
        data = load_replay_data(conn)
        sizes = []
        original = PaperBroker.place_resting_order

        def spy(self, side, price, size, *, ts, book):
            sizes.append(size)
            return original(self, side, price, size, ts=ts, book=book)

        PaperBroker.place_resting_order = spy
        try:
            prepared = prepare_replay(data, cfg)
            result = replay_prepared(prepared, cfg, queue_assumption=QueueAssumption.OPTIMISTIC, filters=filters)
        finally:
            PaperBroker.place_resting_order = original
        return sizes, result

    def test_wins_grow_the_ordered_size_gradually_and_a_loss_never_does(self, tmp_path):
        cfg = BotConfig(risk=RiskLimits(max_contracts_per_trade=1000, max_trades_per_hour=100, max_consecutive_losses=100,
                                        max_open_exposure_pct=D(80), daily_loss_limit_pct=D(80)))
        filters = EntryFilters(account_usd=D(20), risk_pct_per_trade=D("0.10"), max_growth_pct=D(20))
        sizes, result = self.spied_sizes(tmp_path, ["yes", "yes", "yes", "no", "yes", "yes"], filters, cfg)
        assert len(sizes) == 6 and sizes[0] == 6
        assert sizes[0] < sizes[1] < sizes[2] and all(b - a <= 2 for a, b in zip(sizes[:3], sizes[1:3]))
        assert sizes[4] <= sizes[3]                   # the order right after the loss is not larger than the one that lost
        assert result.final_bankroll == D(20) + sum((t.pnl_usd for t in result.trades), D(0))

    def test_without_the_ramp_the_lab_still_follows_the_account_as_before(self, tmp_path):
        cfg = BotConfig(risk=RiskLimits(max_contracts_per_trade=1000, max_trades_per_hour=100, max_open_exposure_pct=D(80)))
        filters = EntryFilters(account_usd=D(20), risk_pct_per_trade=D("0.10"))
        sizes, _ = self.spied_sizes(tmp_path, ["yes", "yes", "yes"], filters, cfg)
        assert sizes[0] == 6 and sizes[-1] >= sizes[0]


class TestMinimumStakeComposesWithPercentSizing:
    """The minimum order premium (a floor) and the percent rule (growth ramp, no increase after a loss) both exist
    now; the floor is applied AFTER the percent rule and the cash check still applies on top."""

    def cfg(self):
        return BotConfig(risk=RiskLimits(max_contracts_per_trade=1000, max_trades_per_hour=100, max_consecutive_losses=100,
                                         max_open_exposure_pct=D(80), daily_loss_limit_pct=D(80)))

    def test_a_percent_stake_too_small_for_one_contract_is_lifted_to_the_minimum_premium(self, tmp_path):
        # 1% of $20 = $0.20 buys 0 contracts at 0.30; a $3 minimum premium lifts it to ceil(3 / 0.30) = 10 contracts
        filters = EntryFilters(account_usd=D(20), risk_pct_per_trade=D("0.01"), min_stake_usd=D(3))
        sizes, result = TestLabReplay().spied_sizes(tmp_path, ["yes"], filters, self.cfg())
        assert sizes == [10] and result.filter_counts["too_small"] == 0

    def test_without_a_floor_that_same_stake_is_skipped_as_too_small(self, tmp_path):
        filters = EntryFilters(account_usd=D(20), risk_pct_per_trade=D("0.01"))
        sizes, result = TestLabReplay().spied_sizes(tmp_path, ["yes"], filters, self.cfg())
        assert sizes == [] and result.filter_counts["too_small"] > 0

    def test_the_floor_cannot_overdraw_the_account(self, tmp_path):
        filters = EntryFilters(account_usd=D(2), risk_pct_per_trade=D("0.10"), min_stake_usd=D(5))
        sizes, result = TestLabReplay().spied_sizes(tmp_path, ["yes"], filters, self.cfg())
        assert sizes == [] and result.filter_counts["too_small"] > 0

    def test_the_growth_ramp_still_governs_when_the_percent_size_is_above_the_floor(self, tmp_path):
        filters = EntryFilters(account_usd=D(20), risk_pct_per_trade=D("0.10"), max_growth_pct=D(20), min_stake_usd=D("0.5"))
        sizes, _ = TestLabReplay().spied_sizes(tmp_path, ["yes", "yes", "yes"], filters, self.cfg())
        assert sizes[0] == 6 and sizes[0] < sizes[1] <= sizes[0] + 2   # ramped, not jumped straight to the target

    def test_persistence_and_the_floor_work_together(self, tmp_path):
        filters = EntryFilters(account_usd=D(100), min_stake_usd=D(5), persist_steps=2)
        sizes, result = TestLabReplay().spied_sizes(tmp_path, ["yes", "yes"], filters, self.cfg())
        assert sizes and all(s * D("0.30") >= D(5) for s in sizes) and result.filter_counts["persistence"] > 0
