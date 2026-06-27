from __future__ import annotations

import math

import pytest

from app.platform.strategy_quality import strategy_quality_report


def test_strategy_quality_reports_sqn_and_sample_confidence():
    report = strategy_quality_report([1.0, 2.0, -0.5, 1.5, 0.5, -0.2, 1.2, 0.8, 1.1, -0.3])
    body = report.to_dict()
    assert body["expectancy"] > 0
    assert body["sqn"] > 0
    assert body["sample_confidence"] in {"low", "medium", "high"}
    assert body["win_rate"] == pytest.approx(0.7)


def test_strategy_quality_handles_zero_variance():
    body = strategy_quality_report([1.0, 1.0, 1.0]).to_dict()
    assert math.isinf(strategy_quality_report([1.0, 1.0, 1.0]).sqn)
    assert body["sqn"] is None
    assert body["payoff_ratio"] is None
    assert body["sample_confidence"] == "low"
