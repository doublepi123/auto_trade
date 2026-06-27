"""Tests for P265 feature orthogonalization (Gram-Schmidt / residualization).

Pure-Python standard-library module: Gram-Schmidt orthogonalization of a
feature panel, OLS residualization of a target against exposure features,
correlation-based feature pruning, simplified VIF scores, and a combined
``orthogonalization_report`` aggregator.

The error-handling contract mirrors the other platform modules (see
``rolling_features.py``): invalid arguments raise ``ValueError`` uniformly —
including empty panels, length mismatch, non-sequence feature values, dict
values, bool entries, non-finite numbers, and invalid thresholds. The
platform HTTP layer translates this single exception family (plus
``TypeError``) into HTTP 422.
"""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError

import pytest

from app.platform.feature_orthogonalization import (
    OrthogonalizationResult,
    correlation_prune,
    dot,
    gram_schmidt,
    norm,
    orthogonalization_report,
    residualize,
    vif_scores,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _approx_zero(value: float, tol: float = 1e-9) -> bool:
    return abs(value) < tol


# ---------------------------------------------------------------------------
# dot / norm
# ---------------------------------------------------------------------------


class TestDotNorm:
    def test_dot_orthogonal_vectors(self):
        assert dot([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_dot_basic(self):
        assert dot([1.0, 2.0, 3.0], [4.0, 5.0, 6.0]) == pytest.approx(32.0)

    def test_dot_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            dot([1.0, 2.0], [1.0])

    def test_dot_bool_entry_raises(self):
        with pytest.raises(ValueError):
            dot([True, 0.0], [1.0, 1.0])

    def test_dot_non_finite_raises(self):
        with pytest.raises(ValueError):
            dot([float("nan"), 0.0], [1.0, 1.0])

    def test_dot_non_sequence_raises(self):
        with pytest.raises(ValueError):
            dot("ab", [1.0, 1.0])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# gram_schmidt
# ---------------------------------------------------------------------------


class TestGramSchmidt:
    def test_two_vectors_orthogonal(self):
        panel = {"A": [1.0, 0.0, 0.0], "B": [1.0, 1.0, 0.0]}
        out = gram_schmidt(panel)
        assert set(out.keys()) == {"A", "B"}
        # Output vectors mutually near-orthogonal.
        assert _approx_zero(dot(out["A"], out["B"]))

    def test_preserves_order(self):
        panel = {"A": [1.0, 2.0, 3.0], "B": [2.0, 1.0, 0.0], "C": [0.0, 1.0, 1.0]}
        out = gram_schmidt(panel)
        assert list(out.keys()) == ["A", "B", "C"]
        # Pairwise dot products ~0.
        assert _approx_zero(dot(out["A"], out["B"]))
        assert _approx_zero(dot(out["A"], out["C"]))
        assert _approx_zero(dot(out["B"], out["C"]))

    def test_linearly_dependent_yields_zero_vector(self):
        # B is a scalar multiple of A → its projection removes everything,
        # leaving a zero (or near-zero) vector.
        panel = {"A": [1.0, 2.0, 3.0], "B": [2.0, 4.0, 6.0]}
        out = gram_schmidt(panel)
        assert all(abs(v) < 1e-9 for v in out["B"])

    def test_empty_panel_raises(self):
        with pytest.raises(ValueError):
            gram_schmidt({})

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            gram_schmidt({"A": [1.0, 2.0], "B": [1.0, 2.0, 3.0]})

    def test_bool_entry_raises(self):
        with pytest.raises(ValueError):
            gram_schmidt({"A": [True, 0.0], "B": [1.0, 1.0]})

    def test_non_sequence_value_raises(self):
        with pytest.raises(ValueError):
            gram_schmidt({"A": "abc", "B": [1.0, 1.0]})  # type: ignore[dict-item]

    def test_dict_value_raises(self):
        with pytest.raises(ValueError):
            gram_schmidt({"A": {"x": 1.0}, "B": [1.0, 1.0]})  # type: ignore[dict-item]

    def test_non_finite_raises(self):
        with pytest.raises(ValueError):
            gram_schmidt({"A": [float("inf"), 0.0], "B": [1.0, 1.0]})


# ---------------------------------------------------------------------------
# residualize
# ---------------------------------------------------------------------------


class TestResidualize:
    def test_no_exposures_returns_target(self):
        target = [1.0, 2.0, 3.0, 4.0]
        out = residualize(target, [])
        assert out == pytest.approx(target)

    def test_removes_linear_exposure_single(self):
        # target = 2 * exposure + residual. residualize projects onto the
        # exposure *without an intercept*, so the residual's mean component
        # may leak into the fit — but the linear exposure is fully removed,
        # leaving a residual orthogonal to the exposure. That orthogonality
        # is the documented contract.
        exposure = [1.0, 2.0, 3.0, 4.0, 5.0]
        residual = [0.1, -0.2, 0.05, 0.0, 0.15]
        target = [2.0 * e + r for e, r in zip(exposure, residual)]
        out = residualize(target, [exposure])
        # residual is orthogonal to exposure (dot ~0).
        assert _approx_zero(dot(out, exposure), tol=1e-9)
        # removing the linear exposure strictly shrinks the residual norm.
        assert norm(out) < norm(target)

    def test_removes_two_exposures(self):
        e1 = [1.0, 0.0, 1.0, 0.0, 1.0]
        e2 = [0.0, 1.0, 0.0, 1.0, 0.0]
        target = [3.0 * a + 5.0 * b for a, b in zip(e1, e2)]
        out = residualize(target, [e1, e2])
        assert _approx_zero(dot(out, e1), tol=1e-9)
        assert _approx_zero(dot(out, e2), tol=1e-9)

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            residualize([1.0, 2.0], [[1.0, 2.0, 3.0]])

    def test_empty_target_raises(self):
        with pytest.raises(ValueError):
            residualize([], [])

    def test_bool_entry_raises(self):
        with pytest.raises(ValueError):
            residualize([True, 1.0], [[1.0, 1.0]])

    def test_non_finite_raises(self):
        with pytest.raises(ValueError):
            residualize([float("nan"), 1.0], [[1.0, 1.0]])


# ---------------------------------------------------------------------------
# correlation_prune
# ---------------------------------------------------------------------------


class TestCorrelationPrune:
    def test_keeps_first_drops_duplicate(self):
        panel = {"A": [1.0, 2.0, 3.0, 4.0], "B": [2.0, 4.0, 6.0, 8.0]}  # B = 2A, corr=1
        kept, dropped = correlation_prune(panel, threshold=0.95)
        assert kept == ["A"]
        assert dropped == ["B"]

    def test_independent_features_all_kept(self):
        panel = {
            "A": [1.0, -1.0, 1.0, -1.0],
            "B": [1.0, 1.0, -1.0, -1.0],
        }  # corr ~ 0
        kept, dropped = correlation_prune(panel, threshold=0.95)
        assert kept == ["A", "B"]
        assert dropped == []

    def test_preserves_input_order(self):
        panel = {"A": [1.0, 2.0, 3.0], "B": [10.0, 20.0, 30.0], "C": [1.0, -1.0, 1.0]}
        kept, dropped = correlation_prune(panel, threshold=0.95)
        # B corr=1 with A → dropped; C independent → kept.
        assert kept == ["A", "C"]
        assert dropped == ["B"]

    def test_invalid_threshold_raises(self):
        with pytest.raises(ValueError):
            correlation_prune({"A": [1.0, 2.0]}, threshold=1.5)
        with pytest.raises(ValueError):
            correlation_prune({"A": [1.0, 2.0]}, threshold=-0.1)

    def test_empty_panel_raises(self):
        with pytest.raises(ValueError):
            correlation_prune({})

    def test_bool_entry_raises(self):
        with pytest.raises(ValueError):
            correlation_prune({"A": [True, 1.0], "B": [1.0, 2.0]})


# ---------------------------------------------------------------------------
# vif_scores
# ---------------------------------------------------------------------------


class TestVifScores:
    def test_duplicate_higher_than_independent(self):
        panel = {
            "A": [1.0, 2.0, 3.0, 4.0, 5.0],
            "B": [2.0, 4.0, 6.0, 8.0, 10.0],  # dup of A
            "C": [1.0, -1.0, 1.0, -1.0, 1.0],  # independent of A
        }
        scores = vif_scores(panel)
        assert set(scores.keys()) == {"A", "B", "C"}
        # All finite.
        for v in scores.values():
            assert math.isfinite(v)
        # Duplicate feature's VIF should exceed the independent feature's.
        assert scores["B"] > scores["C"]

    def test_single_feature_finite(self):
        scores = vif_scores({"A": [1.0, 2.0, 3.0]})
        assert math.isfinite(scores["A"])

    def test_empty_panel_raises(self):
        with pytest.raises(ValueError):
            vif_scores({})

    def test_bool_entry_raises(self):
        with pytest.raises(ValueError):
            vif_scores({"A": [True, 1.0], "B": [1.0, 2.0]})


# ---------------------------------------------------------------------------
# orthogonalization_report
# ---------------------------------------------------------------------------


class TestOrthogonalizationReport:
    def test_report_without_target(self):
        panel = {
            "A": [1.0, 2.0, 3.0, 4.0],
            "B": [2.0, 4.0, 6.0, 8.0],  # dup of A → dropped by corr prune
            "C": [1.0, -1.0, 1.0, -1.0],
        }
        result = orthogonalization_report(panel, threshold=0.95)
        assert isinstance(result, OrthogonalizationResult)
        assert result.kept_features == ["A", "C"]
        assert result.dropped_features == ["B"]
        assert "A" in result.vif_scores
        assert "C" in result.vif_scores
        # correlations dict has pair labels like "A|B".
        assert any("|" in label for label in result.correlations.keys())
        # orthogonal_features comes from gram_schmidt on kept features.
        assert set(result.orthogonal_features.keys()) <= {"A", "C"}
        # residualized is None when no target.
        assert result.residualized is None

    def test_report_with_target(self):
        exposure = [1.0, 2.0, 3.0, 4.0, 5.0]
        residual = [0.1, -0.2, 0.05, 0.0, 0.15]
        target = [2.0 * e + r for e, r in zip(exposure, residual)]
        panel = {"A": exposure, "B": [1.0, -1.0, 1.0, -1.0, 1.0]}
        result = orthogonalization_report(panel, target=target, threshold=0.95)
        assert result.residualized is not None
        # residualized ~ orthogonal to exposure A.
        assert _approx_zero(dot(result.residualized, exposure), tol=1e-9)

    def test_report_empty_panel_raises(self):
        with pytest.raises(ValueError):
            orthogonalization_report({})

    def test_report_invalid_threshold_raises(self):
        with pytest.raises(ValueError):
            orthogonalization_report({"A": [1.0, 2.0]}, threshold=2.0)

    def test_report_target_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            orthogonalization_report(
                {"A": [1.0, 2.0, 3.0]}, target=[1.0, 2.0], threshold=0.95
            )


# ---------------------------------------------------------------------------
# dataclass: frozen + to_dict
# ---------------------------------------------------------------------------


class TestOrthogonalizationResult:
    def test_to_dict_keys(self):
        result = OrthogonalizationResult(
            orthogonal_features={"A": [1.0, 0.0]},
            residualized=[0.5, 0.5],
            kept_features=["A"],
            dropped_features=[],
            vif_scores={"A": 1.0},
            correlations={"A|A": 1.0},
        )
        d = result.to_dict()
        assert set(d.keys()) == {
            "orthogonal_features",
            "residualized",
            "kept_features",
            "dropped_features",
            "vif_scores",
            "correlations",
        }
        assert d["kept_features"] == ["A"]
        assert d["residualized"] == [0.5, 0.5]

    def test_to_dict_residualized_none(self):
        result = OrthogonalizationResult(
            orthogonal_features={},
            residualized=None,
            kept_features=[],
            dropped_features=[],
            vif_scores={},
            correlations={},
        )
        assert result.to_dict()["residualized"] is None

    def test_frozen_immutable(self):
        result = OrthogonalizationResult(
            orthogonal_features={},
            residualized=None,
            kept_features=[],
            dropped_features=[],
            vif_scores={},
            correlations={},
        )
        with pytest.raises(FrozenInstanceError):
            result.kept_features = ["X"]  # type: ignore[misc]
