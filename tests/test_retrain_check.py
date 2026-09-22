"""Offline tests for `btcbot retrain-check` (features + ml-train --features + validate --model chained)."""

from btcbot.cli import main
from btcbot.features import COLUMNS
from btcbot.retrain_check import RetrainCheckError, render, run_retrain_check


def separable_rows(n_windows=60):
    """Same synthetic construction as test_validation.py's trained-model test: a perfectly separable signal
    via model_minus_market, enough windows to satisfy both train_and_validate_from_features' MIN_VALIDATE_EXAMPLES
    and validate()'s train/test window minimums."""
    rows = []
    for w in range(n_windows):
        row = {c: None for c in COLUMNS}
        signal = 3.0 if w % 2 == 0 else -3.0
        row.update(
            source="s", ticker=f"W{w:03d}", ts=f"2026-01-01T{w // 60:02d}:{w % 60:02d}:00+00:00",
            tau_sec=300.0, spot_minus_strike=0.0, spot_move_60s=0.0, spot_move_300s=0.0, spot_move_900s=0.0,
            yes_spread=0.02, yes_bid_size=10.0, no_bid_size=10.0, yes_depth3=20.0, no_depth3=20.0,
            book_imbalance=0.0, p_model=0.5, sigma=0.0004, model_minus_market=signal, p_blend=0.5,
            yes_bid=0.30, no_bid=0.60, outcome_yes=1 if w % 2 == 0 else 0,
        )
        rows.append(row)
    return rows


def test_chains_features_train_and_validate_into_one_result(tmp_path, monkeypatch):
    monkeypatch.setattr("btcbot.retrain_check.build_rows", lambda paths, step_sec: separable_rows())
    features_out, model_out = tmp_path / "f.csv", tmp_path / "m.json"

    result = run_retrain_check(["ignored.sqlite"], features_path=features_out, model_path=model_out, min_test_trades=1)

    assert features_out.is_file() and model_out.is_file()
    assert result.rows_written == 60 and result.rows_labeled == 60
    assert set(result.current) == {"train", "test"} and set(result.model) == {"train", "test"}
    # The separable signal is what the model was built to exploit; the current model (fixed p_blend=0.5) is not.
    assert result.model["test"].win_rate is not None and result.model["test"].win_rate > result.current["test"].win_rate


def test_render_shows_both_models_and_the_model_path(tmp_path, monkeypatch):
    monkeypatch.setattr("btcbot.retrain_check.build_rows", lambda paths, step_sec: separable_rows())
    model_out = tmp_path / "m.json"
    result = run_retrain_check(["ignored.sqlite"], features_path=tmp_path / "f.csv", model_path=model_out, min_test_trades=1)

    out = render(result)
    assert "current model (p_blend)" in out
    assert f"trained model ({model_out})" in out
    assert "beats baseline" in out
    assert "Test-window pnl/contract" in out


def test_too_few_windows_is_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.setattr("btcbot.retrain_check.build_rows", lambda paths, step_sec: separable_rows(4))
    try:
        run_retrain_check(["ignored.sqlite"], features_path=tmp_path / "unused.csv", model_path=tmp_path / "unused.json")
    except RetrainCheckError:
        pass
    else:
        raise AssertionError("expected RetrainCheckError")


def test_cli_wires_the_three_steps_and_prints_a_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("btcbot.retrain_check.build_rows", lambda paths, step_sec: separable_rows())
    features_out, model_out = tmp_path / "f.csv", tmp_path / "m.json"

    code = main([
        "retrain-check", "--db", "ignored.sqlite",
        "--features-out", str(features_out), "--model-out", str(model_out), "--min-test-trades", "1",
    ])

    assert code == 0
    assert features_out.is_file() and model_out.is_file()
    out = capsys.readouterr().out
    assert "Wrote 60 feature rows" in out and "=== current model (p_blend) ===" in out


def test_cli_reports_a_pipeline_error_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("btcbot.retrain_check.build_rows", lambda paths, step_sec: separable_rows(4))

    code = main([
        "retrain-check", "--db", "ignored.sqlite",
        "--features-out", str(tmp_path / "f.csv"), "--model-out", str(tmp_path / "m.json"),
    ])

    assert code == 1
    assert "error:" in capsys.readouterr().err
