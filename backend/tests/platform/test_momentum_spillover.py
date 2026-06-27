from __future__ import annotations

import pytest

from app.platform.momentum_spillover import momentum_spillover_report


def test_momentum_spillover_detects_lead_lag():
    leader = [0.01, 0.02, -0.01, 0.03, 0.0, -0.02, 0.04, 0.01, 0.02, -0.01]
    lagger = [0.0, 0.01, 0.02, -0.01, 0.03, 0.0, -0.02, 0.04, 0.01, 0.02]
    body = momentum_spillover_report(leader, lagger, max_lag=3).to_dict()
    assert body["best_lag"] >= 1
    assert "f_statistic" in body
    assert "r_squared" in body


def test_momentum_spillover_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        momentum_spillover_report([0.01, 0.02], [0.01], max_lag=1)
