from __future__ import annotations

import pytest

from app.platform.strategy_capacity import strategy_capacity_report


def test_strategy_capacity_reports_aum_threshold():
    body = strategy_capacity_report(signal_autocorr=0.5, adv=1_000_000.0, turnover=0.2, impact_threshold_bps=10).to_dict()
    assert body["max_aum"] > 0
    assert body["impact_at_max_aum_bps"] == pytest.approx(10, abs=1e-6)


def test_strategy_capacity_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        strategy_capacity_report(signal_autocorr=1.5, adv=1_000_000.0, turnover=0.2)
    with pytest.raises(ValueError):
        strategy_capacity_report(signal_autocorr=0.5, adv=0.0, turnover=0.2)
