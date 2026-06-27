from __future__ import annotations

import pytest

from app.platform.correlation_regime import correlation_regime_report


def test_correlation_regime_detects_high_correlation():
    panel = {"A": [0.01, 0.02, 0.03], "B": [0.02, 0.04, 0.06], "C": [-0.01, -0.02, -0.03]}
    body = correlation_regime_report(panel).to_dict()
    assert body["average_correlation"] != 0
    assert body["regime"] in {"diversified", "normal", "concentrated", "stress"}


def test_correlation_regime_rejects_small_panel():
    with pytest.raises(ValueError):
        correlation_regime_report({"A": [0.01, 0.02]})


def test_correlation_regime_handles_perfect_negative_correlation():
    body = correlation_regime_report({"A": [1, 2, 3], "B": [-1, -2, -3]}).to_dict()
    assert body["largest_eigenvalue"] == pytest.approx(2.0, abs=1e-6)
    assert body["concentration_ratio"] == pytest.approx(1.0, abs=1e-6)


def test_correlation_regime_rejects_large_panel():
    panel = {f"A{i}": [0.01, 0.02] for i in range(51)}
    with pytest.raises(ValueError):
        correlation_regime_report(panel)
