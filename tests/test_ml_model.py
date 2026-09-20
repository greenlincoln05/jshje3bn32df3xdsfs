"""Offline tests for the hand-rolled logistic regression: no recorded data, no network."""

import random

import pytest

from btcbot.ml_model import LogisticModel, MLModelError, brier_score, check_feature_coverage, fit, load_model, save_model


def separable_dataset(n=200, seed=7):
    """label = x1 > 0, with a second, irrelevant feature -- a model that actually fits should push most of
    its weight onto x1 and assign confident probabilities on either side of the boundary."""
    rng = random.Random(seed)
    rows, labels = [], []
    for _ in range(n):
        x1 = rng.uniform(-5, 5)
        x2 = rng.uniform(-5, 5)
        rows.append({"x1": x1, "x2": x2})
        labels.append(x1 > 0)
    return rows, labels


class TestFitAndPredict:
    def test_learns_a_linearly_separable_boundary(self):
        rows, labels = separable_dataset()
        model = fit(rows, labels, ["x1", "x2"], epochs=300, learning_rate=0.5)

        assert model.predict_proba({"x1": 4.0, "x2": 0.0}) > 0.9
        assert model.predict_proba({"x1": -4.0, "x2": 0.0}) < 0.1

    def test_missing_feature_falls_back_to_training_mean_not_an_error(self):
        rows, labels = separable_dataset()
        model = fit(rows, labels, ["x1", "x2"])
        # Omitting x2 (the irrelevant one) should barely move the prediction versus supplying its mean.
        with_x2 = model.predict_proba({"x1": 3.0, "x2": model.mean[1]})
        without_x2 = model.predict_proba({"x1": 3.0})
        assert without_x2 == pytest.approx(with_x2, abs=1e-9)

    def test_a_none_valued_feature_is_treated_the_same_as_a_missing_one(self):
        # btcbot.features' feature-store rows always carry every column, with None where a value could not
        # be computed (e.g. not enough spot history yet) -- predict_proba must not crash on that.
        rows, labels = separable_dataset()
        model = fit(rows, labels, ["x1", "x2"])
        missing_key = model.predict_proba({"x1": 3.0})
        none_value = model.predict_proba({"x1": 3.0, "x2": None})
        assert none_value == pytest.approx(missing_key, abs=1e-9)

    def test_rejects_mismatched_lengths(self):
        with pytest.raises(MLModelError):
            fit([{"x1": 1.0}], [True, False], ["x1"])

    def test_rejects_empty_inputs(self):
        with pytest.raises(MLModelError):
            fit([], [], ["x1"])
        with pytest.raises(MLModelError):
            fit([{"x1": 1.0}], [True], [])

    def test_constant_feature_does_not_divide_by_zero(self):
        rows = [{"x1": 1.0, "const": 5.0} for _ in range(10)]
        labels = [i % 2 == 0 for i in range(10)]
        model = fit(rows, labels, ["x1", "const"])
        assert model.scale[1] == 1.0  # a zero-variance feature falls back to scale 1, not inf/NaN
        assert 0.0 <= model.predict_proba({"x1": 1.0, "const": 5.0}) <= 1.0


class TestBrierScore:
    def test_perfect_predictions_score_zero(self):
        model = LogisticModel(("x",), (0.0,), 0.0, (0.0,), (1.0,))  # constant p=0.5 regardless of x
        # A constant-0.5 model on a balanced set scores 0.25, the well-known baseline.
        score = brier_score(model, [{"x": 0.0}, {"x": 0.0}], [True, False])
        assert score == pytest.approx(0.25)

    def test_rejects_mismatched_lengths(self):
        model = fit(*separable_dataset(), ["x1", "x2"])
        with pytest.raises(MLModelError):
            brier_score(model, [{"x1": 1.0}], [True, False])


class TestSerialization:
    def test_round_trips_through_json(self, tmp_path):
        model = fit(*separable_dataset(), ["x1", "x2"])
        path = tmp_path / "entry.json"

        save_model(model, path)
        loaded = load_model(path)

        assert loaded == model
        assert loaded.predict_proba({"x1": 2.0, "x2": -1.0}) == model.predict_proba({"x1": 2.0, "x2": -1.0})

    def test_bad_file_raises_ml_model_error(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json", encoding="ascii")
        with pytest.raises(MLModelError):
            load_model(path)

    def test_malformed_data_raises_ml_model_error(self):
        with pytest.raises(MLModelError):
            LogisticModel.from_dict({"feature_names": ["x"], "weights": []})

    def test_mismatched_field_lengths_are_rejected_at_construction(self):
        with pytest.raises(MLModelError):
            LogisticModel(("x", "y"), (1.0,), 0.0, (0.0, 0.0), (1.0, 1.0))


class TestFeatureCoverage:
    def test_full_overlap_passes(self):
        model = LogisticModel(("a", "b"), (1.0, 1.0), 0.0, (0.0, 0.0), (1.0, 1.0))
        check_feature_coverage(model, ["a", "b", "c"])  # does not raise

    def test_no_overlap_is_rejected(self):
        model = LogisticModel(("a", "b"), (1.0, 1.0), 0.0, (0.0, 0.0), (1.0, 1.0))
        with pytest.raises(MLModelError, match="different feature schema"):
            check_feature_coverage(model, ["x", "y", "z"])

    def test_partial_overlap_below_threshold_is_rejected(self):
        model = LogisticModel(("a", "b", "c", "d"), (1.0,) * 4, 0.0, (0.0,) * 4, (1.0,) * 4)
        with pytest.raises(MLModelError):
            check_feature_coverage(model, ["a"], min_coverage=0.5)  # 1/4 = 0.25 < 0.5

    def test_partial_overlap_meeting_threshold_passes(self):
        model = LogisticModel(("a", "b", "c", "d"), (1.0,) * 4, 0.0, (0.0,) * 4, (1.0,) * 4)
        check_feature_coverage(model, ["a", "b"], min_coverage=0.5)  # 2/4 = 0.5, does not raise
