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
