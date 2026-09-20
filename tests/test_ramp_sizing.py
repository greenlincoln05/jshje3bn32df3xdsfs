from decimal import Decimal as D

from btcbot.strategy import ramp_next_size

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


def test_price_band_boost_doubles_only_inside_band_and_never_after_loss():
    import asyncio, sqlite3
    from btcbot.config import BotConfig, Sizing
    from test_live_paper import make_trader
    from test_percent_sizing import run_windows
    cfg = BotConfig(sizing=Sizing(mode="fixed", boost_multiplier=D(2), boost_min_price=D("0.01"), boost_max_price=D("0.99")))
    trader, buffer = make_trader(sqlite3.connect(":memory:"), config=cfg)
    rows = asyncio.run(run_windows(trader, buffer, ["yes"] * 2))
    assert rows[0][1] == 10  # 5 doubled, at the cap


def test_boost_band_must_be_ordered():
    import pytest
    from btcbot.config import Sizing
    with pytest.raises(ValueError):
        Sizing(boost_min_price=D("0.5"), boost_max_price=D("0.3"))
