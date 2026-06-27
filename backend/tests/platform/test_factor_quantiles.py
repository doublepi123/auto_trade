from __future__ import annotations

import pytest

from app.platform.factor_quantiles import factor_quantile_report


def test_factor_quantiles_positive_spread_and_monotonicity():
    report = factor_quantile_report([1, 2, 3, 4, 5, 6], [0.01, 0.02, 0.03, 0.04, 0.05, 0.06], n_quantiles=3)
    body = report.to_dict()
    assert sum(bucket["count"] for bucket in body["quantiles"]) == 6
    assert body["top_bottom_spread"] > 0
    assert body["monotonicity_score"] == pytest.approx(1.0)


def test_factor_quantiles_rejects_invalid_quantile_count():
    with pytest.raises(ValueError):
        factor_quantile_report([1, 2, 3], [0.1, 0.2, 0.3], n_quantiles=1)
    with pytest.raises(ValueError):
        factor_quantile_report([1, 2, 3], [0.1, 0.2, 0.3], n_quantiles=2.5)  # type: ignore[arg-type]
