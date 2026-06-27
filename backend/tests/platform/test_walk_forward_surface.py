"""P328: walk-forward surface tests."""

from __future__ import annotations

import pytest

from app.platform.walk_forward_surface import (
    WalkForwardSurfaceResult,
    walk_forward_surface_report,
)


def test_walk_forward_surface_basic():
    """Basic call returns valid structure with segments > 0."""
    returns = [0.01, -0.02, 0.03, -0.01, 0.02] * 20
    result = walk_forward_surface_report(returns, train_window=20, test_window=10)
    d = result.to_dict()
    assert "segments" in d
    assert len(d["segments"]) > 0
    for seg in d["segments"]:
        assert "is_sharpe" in seg
        assert "oos_sharpe" in seg
        assert "degradation" in seg
        assert "start_idx" in seg
        assert "end_idx" in seg


def test_walk_forward_surface_degradation_field():
    """Each segment has degradation = is_sharpe - oos_sharpe."""
    returns = [0.02, 0.01, -0.01, 0.03, -0.02, 0.01, 0.02, -0.01] * 15
    result = walk_forward_surface_report(returns, train_window=10, test_window=5)
    d = result.to_dict()
    for seg in d["segments"]:
        assert seg["degradation"] == pytest.approx(
            seg["is_sharpe"] - seg["oos_sharpe"], abs=1e-10
        )


def test_walk_forward_surface_summary():
    """Summary includes mean_degradation."""
    returns = [0.01, -0.02, 0.03, -0.01, 0.02] * 20
    result = walk_forward_surface_report(returns, train_window=20, test_window=10)
    d = result.to_dict()
    assert "summary" in d
    assert "mean_degradation" in d["summary"]
    assert isinstance(d["summary"]["mean_degradation"], float)
    # mean_degradation should equal mean of segment degradations
    expected = (
        sum(s["degradation"] for s in d["segments"]) / len(d["segments"])
        if d["segments"]
        else 0.0
    )
    assert d["summary"]["mean_degradation"] == pytest.approx(expected, abs=1e-10)


def test_walk_forward_surface_constant_returns():
    """Constant returns → all Sharpes are 0 (no volatility)."""
    returns = [0.01] * 60
    result = walk_forward_surface_report(returns, train_window=20, test_window=10)
    d = result.to_dict()
    assert len(d["segments"]) > 0
    for seg in d["segments"]:
        assert seg["is_sharpe"] == 0.0
        assert seg["oos_sharpe"] == 0.0
        assert seg["degradation"] == 0.0


def test_walk_forward_surface_rejects_invalid():
    """Invalid inputs raise ValueError."""
    with pytest.raises(ValueError):
        walk_forward_surface_report([], train_window=10, test_window=5)
    with pytest.raises(ValueError):
        walk_forward_surface_report([0.01, 0.02], train_window=0, test_window=5)
    with pytest.raises(ValueError):
        walk_forward_surface_report([0.01, 0.02], train_window=10, test_window=0)
    with pytest.raises(ValueError):
        walk_forward_surface_report([0.01, float("nan")], train_window=5, test_window=3)


def test_walk_forward_surface_to_dict_keys():
    """to_dict contains expected top-level keys."""
    returns = [0.01, -0.02] * 30
    result = walk_forward_surface_report(returns, train_window=10, test_window=5)
    d = result.to_dict()
    for key in ("n_observations", "train_window", "test_window", "segments", "summary"):
        assert key in d, f"missing key {key}"
