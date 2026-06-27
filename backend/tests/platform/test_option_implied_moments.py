from __future__ import annotations

import pytest

from app.platform.option_implied_moments import option_implied_moments_report


def test_option_implied_moments_reports_skew_and_term_structure():
    options = [
        {"strike": 90, "iv": 0.30, "expiry": 30},
        {"strike": 100, "iv": 0.22, "expiry": 30},
        {"strike": 110, "iv": 0.20, "expiry": 30},
        {"strike": 100, "iv": 0.25, "expiry": 60},
    ]
    body = option_implied_moments_report(options, spot=100).to_dict()
    assert body["smile"]["skew"] < 0
    assert body["term_structure"]["slope"] > 0


def test_option_implied_moments_rejects_invalid_option():
    with pytest.raises(ValueError):
        option_implied_moments_report([{"strike": 0, "iv": 0.2, "expiry": 30}], spot=100)
    with pytest.raises(ValueError):
        option_implied_moments_report(None, spot=100)  # type: ignore[arg-type]
