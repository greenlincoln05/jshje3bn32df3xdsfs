import pytest

from btcbot.validation import Policy, ValidationError, evaluate, split_windows, validate, wilson


def rows(n_windows, *, label, yes_bid=0.30, no_bid=0.60, per=3):
    out = []
    for w in range(n_windows):
        for i in range(per):
            out.append({"ticker": f"W{w:03d}", "ts": f"2026-01-01T{w // 60:02d}:{w % 60:02d}:{i:02d}+00:00",
                        "tau_sec": 300 - i, "yes_bid": yes_bid, "no_bid": no_bid, "outcome_yes": label(w),
                        "p_blend": 0.5})
    return out


def test_split_is_by_whole_market_in_time_order_with_embargo():
    r = rows(10, label=lambda w: 1)
    train, test = split_windows(r, 0.7, embargo=1)
    assert train == [f"W{i:03d}" for i in range(7)] and test == ["W008", "W009"]
    assert not set(train) & set(test)


def test_too_few_windows_is_an_error():
    with pytest.raises(ValidationError):
        split_windows(rows(2, label=lambda w: 1), 0.7, 1)


def test_wilson_interval_contains_the_rate():
    lo, hi = wilson(7, 9)
    assert lo < 7 / 9 < hi and 0 <= lo and hi <= 1


def test_refuses_a_verdict_below_the_trade_minimum():
    r = rows(10, label=lambda w: 1)
    ev = evaluate(r, [f"W{i:03d}" for i in range(10)], lambda row: 0.9)  # 10 trades < 30
    assert ev.trades == 10 and ev.verdict.startswith("insufficient")


def test_a_perfect_model_on_enough_windows_reads_as_consistent_never_profitable():
    r = rows(40, label=lambda w: 1)
    ev = evaluate(r, [f"W{i:03d}" for i in range(40)], lambda row: 0.95)
    assert ev.trades == 40 and ev.wins == 40
    assert "profitable" not in ev.verdict and "consistent" in ev.verdict


def test_a_wrong_model_reads_as_no_evidence():
    r = rows(40, label=lambda w: 0)
    ev = evaluate(r, [f"W{i:03d}" for i in range(40)], lambda row: 0.95)  # bets YES, all settle NO
    assert ev.wins == 0 and ev.verdict.startswith("no evidence")


def test_unsettled_windows_neither_score_nor_trade():
    r = rows(3, label=lambda w: None)
    ev = evaluate(r, ["W000", "W001", "W002"], lambda row: 0.9)
    assert ev.trades == 0 and ev.brier is None


def test_validate_scores_train_and_test_separately():
    out = validate(rows(20, label=lambda w: 1), lambda row: 0.95)
    assert out["train"].windows == 14 and out["test"].windows == 5


def test_price_band_and_edge_gate_the_entry():
    r = rows(40, label=lambda w: 1, yes_bid=0.05)
    ev = evaluate(r, [f"W{i:03d}" for i in range(40)], lambda row: 0.95, Policy(min_price=0.15))
    assert ev.trades == 0


def test_features_csv_roundtrip_and_cli(tmp_path, capsys):
    from btcbot.cli import main
    from btcbot.features import COLUMNS, read_csv, write_csv
    r = [{c: None for c in COLUMNS} | x for x in rows(12, label=lambda w: w % 2)]
    for x in r:
        x["source"] = "s"
    path = tmp_path / "f.csv"
    write_csv(r, path)
    back = read_csv(path)
    assert back[0]["outcome_yes"] in (0, 1) and back[0]["yes_bid"] == 0.3
    assert main(["validate", "--features", str(path)]) == 0
    assert "verdict:" in capsys.readouterr().out
