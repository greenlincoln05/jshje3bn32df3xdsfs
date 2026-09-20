from btcbot.calibration_report import build_report, one_row_per_window, render


def row(t, tau, p, mid, y):
    return {"ticker": t, "tau_sec": tau, "p_model": p, "p_blend": p, "yes_mid": mid, "outcome_yes": y}


def test_one_row_per_window_picks_the_nearest_tau_and_skips_unsettled():
    rows = [row("A", 100, .5, .5, 1), row("A", 310, .6, .5, 1), row("B", 300, .5, .5, None)]
    got = one_row_per_window(rows)
    assert [(r["ticker"], r["tau_sec"]) for r in got] == [("A", 310)]


def test_disagreement_bucket_shows_a_model_that_loses_when_it_disagrees():
    rows = [row(f"W{i}", 300, 0.55, 0.18, 0) for i in range(12)] + [row(f"X{i}", 300, 0.5, 0.5, i % 2) for i in range(12)]
    rep = build_report(rows)
    big = next(b for b in rep["by_disagreement"] if b.label.startswith(">= +0.30"))
    assert big.n == 12 and big.yes_rate == 0 and not big.small
    assert "Model minus market" in render(rep)


def test_small_buckets_are_flagged():
    rep = build_report([row("A", 300, .5, .5, 1)])
    assert all(b.small for b in rep["reliability"])
