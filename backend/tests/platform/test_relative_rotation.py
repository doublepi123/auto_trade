"""Tests for P353 relative rotation graph (RRG) analysis.

RRG classifies assets into 4 quadrants (leading/improving/lagging/weakening)
based on their relative strength ratio and momentum vs. a benchmark.
"""

from __future__ import annotations

import math

import pytest

from app.platform.relative_rotation import RelativeRotationResult, relative_rotation_report


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _two_asset_bench() -> tuple[dict[str, list[float]], list[float]]:
    """Asset A outperforms benchmark (rising RS), B underperforms (falling RS)."""
    benchmark = [100.0 + i for i in range(40)]  # steady uptrend
    # Asset A: rising 2x faster than benchmark → positive RS_ratio and momentum
    asset_a = [100.0 + 2.0 * i for i in range(40)]
    # Asset B: rising 0.5x slower → negative RS momentum
    asset_b = [100.0 + 0.5 * i for i in range(40)]
    assets = {"A": asset_a, "B": asset_b}
    return assets, benchmark


# ---------------------------------------------------------------------------
# relative_rotation_report
# ---------------------------------------------------------------------------


class TestRelativeRotationReport:
    def test_two_assets_produces_quadrants(self):
        """Two assets + benchmark should produce per-asset quadrant classification."""
        assets, benchmark = _two_asset_bench()
        result = relative_rotation_report(assets, benchmark, tail=10)
        assert isinstance(result, RelativeRotationResult)
        assert "A" in result.per_asset
        assert "B" in result.per_asset
        assert "A" in result.latest_quadrants
        assert "B" in result.latest_quadrants
        for sym in ("A", "B"):
            info = result.per_asset[sym]
            assert "rs_ratio" in info
            assert "rs_momentum" in info
            assert "quadrant" in info
            assert info["quadrant"] in {"leading", "improving", "lagging", "weakening"}

    def test_leading_asset_classified_correctly(self):
        """Asset A beats benchmark → RS_ratio > 0, RS_momentum > 0 → leading."""
        assets, benchmark = _two_asset_bench()
        result = relative_rotation_report(assets, benchmark, tail=10)
        a_info = result.per_asset["A"]
        # A outperforms → RS_ratio should be positive, momentum positive
        assert a_info["rs_ratio"] > 0
        assert a_info["rs_momentum"] > 0
        assert a_info["quadrant"] == "leading"

    def test_lagging_asset_classified_correctly(self):
        """Asset B underperforms → RS_ratio < 0, RS_momentum < 0 → lagging."""
        assets, benchmark = _two_asset_bench()
        result = relative_rotation_report(assets, benchmark, tail=10)
        b_info = result.per_asset["B"]
        assert b_info["rs_ratio"] < 0
        assert b_info["rs_momentum"] < 0
        assert b_info["quadrant"] == "lagging"

    def test_empty_assets_raises(self):
        with pytest.raises(ValueError):
            relative_rotation_report({}, [100.0, 101.0])

    def test_benchmark_too_short_raises(self):
        with pytest.raises(ValueError):
            relative_rotation_report({"A": [1.0, 2.0, 3.0]}, [100.0])

    def test_mismatched_lengths_raises(self):
        with pytest.raises(ValueError):
            relative_rotation_report(
                {"A": [1.0, 2.0, 3.0]}, [100.0, 101.0, 102.0, 103.0]
            )

    def test_non_finite_entries_raises(self):
        with pytest.raises(ValueError):
            relative_rotation_report(
                {"A": [1.0, float("nan")]}, [100.0, 101.0]
            )

    def test_custom_tail(self):
        assets, benchmark = _two_asset_bench()
        result = relative_rotation_report(assets, benchmark, tail=5)
        assert isinstance(result, RelativeRotationResult)

    def test_to_dict(self):
        assets, benchmark = _two_asset_bench()
        result = relative_rotation_report(assets, benchmark, tail=10)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "per_asset" in d
        assert "latest_quadrants" in d
        assert isinstance(d["per_asset"], dict)
        assert isinstance(d["latest_quadrants"], dict)

    def test_max_50_assets(self):
        """Panel must have at most 50 assets."""
        assets = {f"S{i}": [100.0 + i + j for j in range(30)] for i in range(51)}
        bench = [100.0 + j for j in range(30)]
        with pytest.raises(ValueError):
            relative_rotation_report(assets, bench)
