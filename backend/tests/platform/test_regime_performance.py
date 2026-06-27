from __future__ import annotations

import pytest

from app.platform.regime_performance import regime_performance_report


def test_regime_performance_groups_returns_by_label():
    report = regime_performance_report([0.02, 0.03, -0.01, -0.02, 0.01], ["bull", "bull", "bear", "bear", "flat"])
    body = report.to_dict()
    assert body["regimes"]["bull"]["mean_return"] > 0
    assert body["regimes"]["bear"]["mean_return"] < 0
    contribution_sum = sum(abs(item["contribution_share"]) for item in body["regimes"].values())
    assert contribution_sum == pytest.approx(1.0)


def test_regime_performance_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        regime_performance_report([0.1, 0.2], ["bull"])
