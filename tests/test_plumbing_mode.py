from decimal import Decimal as D

from btcbot.models import OrderBook, PriceLevel
from btcbot.strategy import Action, decide


def wide_book():
    # YES bid 0.40 / ask 0.55 (NO bid 0.45): a 15-cent spread with tiny depth, like the demo exchange
    return OrderBook("T", yes_bids=(PriceLevel(D("0.40"), D("3")),), no_bids=(PriceLevel(D("0.45"), D("3")),))


def go(**kw):
    base = dict(book=wide_book(), tau_sec=400, p_yes=0.75, spot_is_stale=False, min_edge=D("0.04"), min_depth=D(1),
                max_spread=D(1), min_tau_sec=30, max_tau_sec=780, cancel_before_close_sec=20,
                contracts_per_trade=D(5), maker_fee_multiplier=D(0), has_resting_order=False, has_position=False)
    return decide(**{**base, **kw})


def test_normal_mode_bids_at_the_best_bid():
    d = go()
    assert d.action is Action.REST and d.price == D("0.40")


def test_plumbing_bids_inside_the_spread_but_never_onto_the_ask():
    d = go(bid_improve_ticks=5)
    assert d.action is Action.REST and d.price == D("0.45")        # 0.40 + 5 ticks
    d = go(bid_improve_ticks=20)
    assert d.price == D("0.54")                                    # capped one tick under the 0.55 ask


def test_the_edge_is_computed_at_the_improved_price():
    d = go(bid_improve_ticks=20, p_yes=0.56)                       # 0.56 - 0.54 = 0.02 < min_edge: no trade
    assert d.action is Action.SKIP



def test_the_flag_is_demo_only_and_defaults_off():
    from btcbot.cli import main
    from btcbot.config import BotConfig
    assert BotConfig().bid_improve_ticks == 0
    import pytest
    with pytest.raises(SystemExit):
        main(["paper", "--plumbing"])          # not a paper/prod option
