"""A small, dependency-free logistic regression for the ML entry/exit layers.

See docs/research/ml-layers-handoff.md for the design this plugs into. No numpy or scikit-learn: like
model.py's own normal_cdf (plain math.erf), gradient descent over a few thousand rows and a handful of
features does not need a matrix library, and it keeps the project on stdlib-only math. A model is JSON --
feature names, weights, bias, and the per-feature mean/scale used to standardize inputs -- so a saved model
is inspectable text, never a pickle that could execute arbitrary code when loaded.

This module never reads recorded data, calls a strategy function, or touches Kalshi/Coinbase; it only fits
and evaluates a model given feature/label pairs a caller has already built (see btcbot.ml_pipeline).
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class MLModelError(Exception):
    """Bad training data, or a saved model file that does not parse."""


def _sigmoid(z: float) -> float:
    # The naive exp(-z) overflows for very negative z; branch on sign to stay in float range either way.
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


@dataclass(frozen=True, slots=True)
class LogisticModel:
    """``predict_proba`` standardizes each named feature with the training mean/scale before applying the
    learned weights, so callers pass the same raw feature dict :func:`fit` was trained on; a feature missing
    from that dict -- or present with value ``None`` (btcbot.features' feature-store rows use ``None`` for a
    column that could not be computed yet, e.g. not enough spot history for a long lookback) -- is treated as
    its training-set mean (contributes 0 after standardization), not an error, so a caller need not thread
    every feature through every call site, and ``predict_proba`` itself is a valid
    ``btcbot.validation.Predictor``: it takes one row-like mapping and returns a float."""

    feature_names: tuple[str, ...]
    weights: tuple[float, ...]
    bias: float
    mean: tuple[float, ...]
    scale: tuple[float, ...]

    def __post_init__(self) -> None:
        n = len(self.feature_names)
        if not (len(self.weights) == len(self.mean) == len(self.scale) == n):
            raise MLModelError("feature_names, weights, mean and scale must all be the same length")

    def predict_proba(self, features: Mapping[str, float | None]) -> float:
        z = self.bias
        for name, w, mean, scale in zip(self.feature_names, self.weights, self.mean, self.scale):
            raw = features.get(name)
            x = ((mean if raw is None else raw) - mean) / scale
            z += w * x
        return _sigmoid(z)

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_names": list(self.feature_names), "weights": list(self.weights), "bias": self.bias,
            "mean": list(self.mean), "scale": list(self.scale),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LogisticModel:
        try:
            return cls(
                feature_names=tuple(data["feature_names"]), weights=tuple(float(w) for w in data["weights"]),
                bias=float(data["bias"]), mean=tuple(float(m) for m in data["mean"]),
                scale=tuple(float(s) for s in data["scale"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MLModelError(f"malformed model data: {exc}") from exc


def save_model(model: LogisticModel, path: Path) -> None:
    path.write_text(json.dumps(model.to_dict(), indent=2), encoding="ascii")


def load_model(path: Path) -> LogisticModel:
    try:
        data = json.loads(path.read_text(encoding="ascii"))
    except (OSError, ValueError) as exc:
        raise MLModelError(f"could not read model file {path}: {exc}") from exc
    return LogisticModel.from_dict(data)


def check_feature_coverage(model: LogisticModel, available_features: Iterable[str], *, min_coverage: float = 0.5) -> None:
    """Raise :class:`MLModelError` when fewer than ``min_coverage`` of ``model.feature_names`` appear in
    ``available_features``. This project now has more than one feature schema (see
    docs/research/ml-layers-handoff.md): a model trained for one (e.g. the feature-store CSV's columns) fed
    into a pipeline that only ever supplies another (e.g. btcbot.ml_features.ENTRY_FEATURES) would otherwise
    fail silently -- :meth:`LogisticModel.predict_proba` treats every unrecognized feature name as its
    training-set mean, so it would just degrade to a near-constant prediction instead of erroring. Call this
    once, when a model is loaded for a specific pipeline, not on every ``predict_proba`` call."""
    available = set(available_features)
    present = sum(1 for name in model.feature_names if name in available)
    coverage = present / len(model.feature_names)
    if coverage < min_coverage:
        raise MLModelError(
            f"model expects features {model.feature_names}, but only {present}/{len(model.feature_names)} "
            f"of those are available here ({sorted(available)}) -- this looks like a model trained for a "
            "different feature schema"
        )


def fit(
    rows: Sequence[Mapping[str, float]],
    labels: Sequence[bool],
    feature_names: Sequence[str],
    *,
    epochs: int = 300,
    learning_rate: float = 0.3,
    l2: float = 0.001,
) -> LogisticModel:
    """Batch gradient descent on standardized features (mean 0, unit variance -- a constant or missing
    feature gets ``scale=1`` rather than a division by zero) with L2 weight decay. ``rows``/``labels`` must be
    non-empty and the same length; every row may be missing keys (treated as that feature's mean, i.e. no
    contribution), matching :meth:`LogisticModel.predict_proba`."""
    n = len(rows)
    if n == 0 or n != len(labels):
        raise MLModelError("rows and labels must be the same non-empty length")
    names = tuple(feature_names)
    if not names:
        raise MLModelError("feature_names must not be empty")

    means = [sum(r.get(name, 0.0) for r in rows) / n for name in names]
    variances = [sum((r.get(name, 0.0) - m) ** 2 for r in rows) / n for name, m in zip(names, means)]
    scales = [math.sqrt(v) if v > 1e-12 else 1.0 for v in variances]
    xs = [[(r.get(name, 0.0) - m) / s for name, m, s in zip(names, means, scales)] for r in rows]
    ys = [1.0 if label else 0.0 for label in labels]

    weights = [0.0] * len(names)
    bias = 0.0
    for _ in range(epochs):
        grad_w = [0.0] * len(names)
        grad_b = 0.0
        for x_row, y in zip(xs, ys):
            p = _sigmoid(bias + sum(w * x for w, x in zip(weights, x_row)))
            err = p - y
            for i, x in enumerate(x_row):
                grad_w[i] += err * x
            grad_b += err
        weights = [w - learning_rate * (grad_w[i] / n + l2 * w) for i, w in enumerate(weights)]
        bias -= learning_rate * (grad_b / n)

    return LogisticModel(names, tuple(weights), bias, tuple(means), tuple(scales))


def brier_score(model: LogisticModel, rows: Sequence[Mapping[str, float]], labels: Sequence[bool]) -> float:
    """Mean squared error between the model's predicted probability and the 0/1 outcome, on held-out
    ``rows``/``labels`` -- the same metric ``btcbot.model.brier_score`` uses, so a learned model and the v1
    formula are judged on the same scale. Not a PnL or profitability number."""
    if not rows or len(rows) != len(labels):
        raise MLModelError("rows and labels must be the same non-empty length")
    total = sum((model.predict_proba(r) - (1.0 if y else 0.0)) ** 2 for r, y in zip(rows, labels))
    return total / len(rows)
