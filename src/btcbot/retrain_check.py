"""One-shot weekly retrain-and-validate (``btcbot retrain-check``): features -> train -> validate, chained.

Wires three separately-useful steps (``btcbot features``, ``btcbot ml-train --features``, ``btcbot validate
--model``) into one call so re-checking "does a model trained on this week's recorded prod paper data beat what
is live" is a single command instead of three. Offline: no network, no key, no order code, nothing wired into
``live_paper.py``/``btcbot paper``/``btcbot demo`` -- this only ever writes a features CSV and a model JSON file
and prints a report, the same as running the three commands by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from btcbot.features import FeatureError, build_rows, write_csv
from btcbot.ml_model import MLModelError, save_model
from btcbot.ml_pipeline import FEATURE_STORE_ENTRY_FEATURES, MLPipelineError, train_and_validate_from_features
from btcbot.validation import Evaluation, ValidationError, blend_predictor, validate


class RetrainCheckError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RetrainCheckResult:
    rows_written: int
    rows_labeled: int
    features_path: Path
    model_path: Path
    train_brier: float
    validate_brier: float
    baseline_validate_brier: float | None
    beats_baseline_brier: bool | None
    current: dict[str, Evaluation]
    model: dict[str, Evaluation]


def run_retrain_check(
    db_paths: Sequence[str | Path],
    *,
    features_path: str | Path,
    model_path: str | Path,
    step_sec: float = 5.0,
    split: float = 0.7,
    embargo: int = 1,
    min_test_trades: int = 30,
) -> RetrainCheckResult:
    try:
        rows = build_rows(db_paths, step_sec=step_sec)
    except FeatureError as exc:
        raise RetrainCheckError(str(exc)) from exc
    features_path, model_path = Path(features_path), Path(model_path)
    features_path.parent.mkdir(parents=True, exist_ok=True)
    n_written = write_csv(rows, features_path)
    n_labeled = sum(1 for r in rows if r["outcome_yes"] is not None)

    try:
        model, report = train_and_validate_from_features(
            rows, FEATURE_STORE_ENTRY_FEATURES, train_fraction=split, embargo=embargo
        )
    except MLPipelineError as exc:
        raise RetrainCheckError(str(exc)) from exc
    model_path.parent.mkdir(parents=True, exist_ok=True)
    save_model(model, model_path)

    try:
        # The SAME split function/args train_and_validate_from_features just used internally (it imports
        # btcbot.validation.split_windows directly), so "current" and "model" are judged on the identical
        # held-out windows -- not two independently-computed splits that could quietly disagree.
        current = validate(rows, blend_predictor, train_frac=split, embargo=embargo, min_test_trades=min_test_trades)
        trained = validate(rows, model.predict_proba, train_frac=split, embargo=embargo, min_test_trades=min_test_trades)
    except ValidationError as exc:
        raise RetrainCheckError(str(exc)) from exc

    return RetrainCheckResult(
        rows_written=n_written, rows_labeled=n_labeled, features_path=features_path, model_path=model_path,
        train_brier=report.train_brier, validate_brier=report.validate_brier,
        baseline_validate_brier=report.baseline_validate_brier, beats_baseline_brier=report.beats_baseline,
        current=current, model=trained,
    )


def render(result: RetrainCheckResult) -> str:
    lines = [
        f"Wrote {result.rows_written} feature rows ({result.rows_labeled} settled) to {result.features_path}",
        f"Trained {result.model_path}: train brier {result.train_brier:.4f}, validate brier "
        f"{result.validate_brier:.4f}"
        + (
            f" vs current model's {result.baseline_validate_brier:.4f} on the same held-out rows -- "
            f"beats baseline: {'YES' if result.beats_baseline_brier else 'no'}"
            if result.baseline_validate_brier is not None else ""
        ),
        "",
    ]

    def block(label: str, out: dict[str, Evaluation]) -> None:
        lines.append(f"=== {label} ===")
        for name, ev in out.items():
            rate = "-" if ev.win_rate is None else f"{ev.win_rate:.0%} (95% {ev.ci95[0]:.0%}-{ev.ci95[1]:.0%})"
            brier = "-" if ev.brier is None else f"{ev.brier:.4f}"
            lines.append(f"{name}: windows {ev.windows}, trades {ev.trades}, wins {ev.wins}, win rate {rate}, "
                        f"pnl/contract ${ev.pnl_per_contract:.2f}, brier {brier}\n  verdict: {ev.verdict}")

    block("current model (p_blend)", result.current)
    block(f"trained model ({result.model_path})", result.model)
    base_test, model_test = result.current["test"], result.model["test"]
    lines.append(
        f"\nTest-window pnl/contract: current model ${base_test.pnl_per_contract:.2f} vs trained model "
        f"${model_test.pnl_per_contract:.2f} -- not a paired test between the two; read each verdict above on "
        "its own terms, not just which number is larger."
    )
    return "\n".join(lines)
