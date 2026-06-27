from __future__ import annotations

import pytest

from app.platform.signal_information_ratio import signal_information_ratio_report


def test_signal_information_ratio_reports_positive_ir():
    body = signal_information_ratio_report([0.1, 0.2, 0.3, 0.4], [0.01, 0.02, 0.03, 0.04], periods_per_year=252, n_buckets=2).to_dict()
    assert body["information_ratio"] > 0
    assert body["bucket_quality"]["top_bottom_spread"] > 0


def test_signal_information_ratio_rejects_bad_bucket_count():
    with pytest.raises(ValueError):
        signal_information_ratio_report([1, 2], [0.1, 0.2], n_buckets=3)
    with pytest.raises(ValueError):
        signal_information_ratio_report([1, 2], [0.1, 0.2], periods_per_year="252")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        signal_information_ratio_report([1, 2], [0.1, 0.2], n_buckets="2")  # type: ignore[arg-type]
