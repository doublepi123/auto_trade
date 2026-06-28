"""P357: Pair Screening — unit tests.

Compute pairwise dependence metrics (mutual information or distance correlation)
over a returns panel, and rank the top-N most dependent asset pairs.
"""

from __future__ import annotations

import pytest

from app.platform.pair_screening import (
    PairScreeningResult,
    pair_screening_report,
)


class TestPairScreening:
    """Pair screening unit tests."""

    def test_basic_pair_screening(self) -> None:
        """3-asset panel yields non-empty ranked pairs."""
        panel: dict[str, list[float]] = {
            "A": [0.01, 0.02, -0.01, 0.00, 0.01],
            "B": [0.02, 0.03, -0.02, 0.01, 0.02],
            "C": [-0.02, -0.01, 0.03, -0.01, -0.02],
        }
        result = pair_screening_report(panel, top_n=3)
        assert len(result.pairs) >= 1
        assert result.total_pairs_screened == 3  # 3 choose 2

    def test_top_pair_has_highest_score(self) -> None:
        """The first pair in the result has the highest score."""
        panel: dict[str, list[float]] = {
            "A": [0.01, 0.02, -0.01, 0.00, 0.01],
            "B": [0.02, 0.03, -0.02, 0.01, 0.02],
            "C": [-0.02, -0.01, 0.03, -0.01, -0.02],
        }
        result = pair_screening_report(panel, top_n=3)
        scores = [p["score"] for p in result.pairs]
        assert scores == sorted(scores, reverse=True)

    def test_top_n_limits(self) -> None:
        """top_n limits the result length."""
        panel: dict[str, list[float]] = {
            "A": [0.01, -0.01, 0.02],
            "B": [0.02, -0.02, 0.01],
            "C": [-0.01, 0.01, -0.02],
            "D": [0.00, 0.01, -0.01],
        }
        result = pair_screening_report(panel, top_n=2)
        assert len(result.pairs) <= 2

    def test_distance_corr_method(self) -> None:
        """distance_corr method yields non-empty results."""
        panel: dict[str, list[float]] = {
            "A": [0.01, 0.02, -0.01, 0.00, 0.01],
            "B": [0.02, 0.03, -0.02, 0.01, 0.02],
            "C": [-0.02, -0.01, 0.03, -0.01, -0.02],
        }
        result = pair_screening_report(panel, top_n=3, method="distance_corr")
        assert len(result.pairs) >= 1
        assert result.method == "distance_corr"

    def test_invalid_method_raises_value_error(self) -> None:
        """Invalid method raises ValueError."""
        panel: dict[str, list[float]] = {"A": [0.01, -0.01], "B": [-0.01, 0.01]}
        with pytest.raises(ValueError):
            pair_screening_report(panel, method="invalid_method")

    def test_single_asset_raises_value_error(self) -> None:
        """Panel with only 1 asset raises ValueError (need at least 2)."""
        panel: dict[str, list[float]] = {"A": [0.01, -0.01]}
        with pytest.raises(ValueError):
            pair_screening_report(panel)

    def test_unequal_lengths_raises_value_error(self) -> None:
        """Assets with different series lengths raise ValueError."""
        panel: dict[str, list[float]] = {
            "A": [0.01, -0.01, 0.02],
            "B": [0.02, -0.02],
        }
        with pytest.raises(ValueError):
            pair_screening_report(panel)

    def test_non_numeric_raises_type_error(self) -> None:
        """Non-numeric entries raise TypeError."""
        panel: dict[str, list[float]] = {
            "A": ["x", "y"],  # type: ignore[dict-item]
            "B": [0.02, -0.02],
        }
        with pytest.raises(TypeError):
            pair_screening_report(panel)

    def test_to_dict_roundtrip(self) -> None:
        """to_dict returns expected keys."""
        panel: dict[str, list[float]] = {
            "A": [0.01, -0.01, 0.02],
            "B": [0.02, -0.02, 0.01],
            "C": [-0.01, 0.01, -0.02],
        }
        result = pair_screening_report(panel)
        d = result.to_dict()
        assert "pairs" in d
        assert "method" in d
        assert "total_pairs_screened" in d
        assert isinstance(d["pairs"], list)
        if d["pairs"]:
            pair0 = d["pairs"][0]
            assert "asset_a" in pair0
            assert "asset_b" in pair0
            assert "score" in pair0

    def test_too_many_assets_raises_value_error(self) -> None:
        """Panel with >50 assets raises ValueError."""
        panel = {str(i): [0.01, -0.01] for i in range(51)}
        with pytest.raises(ValueError):
            pair_screening_report(panel)
