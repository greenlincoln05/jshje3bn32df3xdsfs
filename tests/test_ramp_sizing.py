import sqlite3
from decimal import Decimal as D

from btcbot.config import BotConfig
from btcbot.strategy import ramp_next_size
from test_live_paper import make_trader
from test_percent_sizing import run_windows

KW = dict(base=D(5), growth_pct=D(20), max_contracts=D(10))


def test_win_streak_grows_at_least_one_each_time():
    size, seen = D(5), []
    for _ in range(4):
        size = ramp_next_size(size, True, **KW)
        seen.append(size)
    assert seen == [6, 7, 8, 9]


def test_loss_resets_to_base_and_cap_holds():
    assert ramp_next_size(D(9), False, **KW) == 5
    assert ramp_next_size(D(10), True, **KW) == 10
    assert ramp_next_size(D(20), True, base=D(5), growth_pct=D(20), max_contracts=D(100)) == 24


def test_loss_resets_to_base_regardless_of_how_far_it_had_ramped():
    for streak_size in (D(6), D(9), D(50)):
        assert ramp_next_size(streak_size, False, **KW) == 5


class TestLiveTraderRampMode:
    """SizingMode.RAMP is the default, so this drives the actual live/paper/demo decision loop
    (LivePaperTrader), not just the pure ramp_next_size function -- confirming the wiring in
    live_paper.py's _resolve() (which calls ramp_next_size on every settlement) behaves the same way.

    fill_a_window's fixed order-book shrink pattern only ever fills 4 of whatever size is ordered, and
    only manages to fill every OTHER window (a known test-harness limit of chaining LivePaperTrader windows
    a fixed 1000s apart, unrelated to sizing mode -- see test_percent_sizing.py's own run_windows/one_window,
    reused here as-is), so results below are indexed by TRADED window, not by window number.
    """

    async def test_a_loss_resets_to_base_and_a_later_win_ramps_again(self):
        trader, buffer = make_trader(sqlite3.connect(":memory:"), config=BotConfig())  # default sizing: ramp

        # win, (skip), LOSS, (skip), win, (skip), win, (skip) -- results at odd indices are never used, since
        # no order is ever placed for those windows.
        rows = await run_windows(trader, buffer, ["yes", "yes", "no", "yes", "yes", "yes", "yes", "yes"])
        sizes = [r[1] for r in rows]

        assert len(rows) == 4
        assert sizes[0] == 5   # the very first order, at the configured base
        assert sizes[1] == 6   # grown by the settled win before the loss
        assert sizes[2] == 5   # the loss reset the NEXT order straight back to base, not part-way down
        assert sizes[3] == 6   # and a later settled win ramps it up again, exactly as the first win did

    async def test_two_losses_in_a_row_never_drop_below_base(self):
        trader, buffer = make_trader(sqlite3.connect(":memory:"), config=BotConfig())

        rows = await run_windows(trader, buffer, ["yes", "yes", "yes", "yes", "no", "yes", "no", "yes", "yes", "yes"])
        sizes = [r[1] for r in rows]

        assert len(rows) == 5
        assert sizes[-2] == 5 and sizes[-1] == 5  # both losses hold at base; a second loss cannot go lower
