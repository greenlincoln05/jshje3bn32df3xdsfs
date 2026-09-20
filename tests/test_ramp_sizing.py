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


def test_ramp_max_price_and_stop_tighten_with_level_and_respect_floors():
    from btcbot.strategy import ramp_max_price, ramp_stop_loss_pct
    kw = dict(step=D("0.03"), floor=D("0.60"))
    assert ramp_max_price(D("0.85"), 0, **kw) == D("0.85")
    assert ramp_max_price(D("0.85"), 3, **kw) == D("0.76")
    assert ramp_max_price(D("0.85"), 20, **kw) == D("0.60")  # floor
    assert ramp_max_price(None, 3, **kw) is None
    sk = dict(tighten_per_level=D(8), floor_pct=D(10))
    assert ramp_stop_loss_pct(D(50), 0, **sk) == 50
    assert ramp_stop_loss_pct(D(50), 2, **sk) == 34
    assert ramp_stop_loss_pct(D(50), 9, **sk) == 10  # floor
    assert ramp_stop_loss_pct(None, 2, **sk) is None


def _trader():
    import sqlite3
    from btcbot.config import BotConfig, Sizing
    from test_live_paper import make_trader
    return make_trader(sqlite3.connect(":memory:"), config=BotConfig(sizing=Sizing(mode="ramp")))[0]


def test_the_ramp_resets_after_two_windows_without_a_position():
    t = _trader()
    t._ramp_level, t._ramp_size = 3, D(8)
    t._current_ticker = "A"
    t._note_finished_window()
    assert t._ramp_level == 3 and t._idle_windows == 1           # one idle window: not yet
    t._current_ticker = "B"
    t._note_finished_window()
    assert t._ramp_level == 0 and t._ramp_size is None and t._idle_windows == 0


def test_a_traded_window_clears_the_idle_count():
    t = _trader()
    t._ramp_level, t._idle_windows = 2, 1
    t._current_ticker = "A"
    t.windows_traded.add("A")
    t._note_finished_window()
    assert t._idle_windows == 0 and t._ramp_level == 2


def test_the_price_cap_drops_as_the_ramp_climbs():
    t = _trader()
    base = t._effective_max_price()
    t._ramp_level = 4
    assert t._effective_max_price() < base


def test_idle_count_does_not_run_at_base_size():
    t = _trader()
    t._current_ticker = "A"
    t._note_finished_window()
    assert t._idle_windows == 0
