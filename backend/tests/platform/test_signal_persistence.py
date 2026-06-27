from __future__ import annotations

import pytest

from app.platform.signal_persistence import signal_persistence_report


def test_signal_persistence_detects_persistence_and_turnover():
    persistent = signal_persistence_report([1, 1, 0.9, 0.8, 0.75, 0.7], max_lag=3)
    alternating = signal_persistence_report([1, -1, 1, -1, 1, -1], max_lag=3)
    assert persistent.to_dict()["autocorrelation"]["1"] > alternating.to_dict()["autocorrelation"]["1"]
    assert alternating.to_dict()["turnover_proxy"] > persistent.to_dict()["turnover_proxy"]


def test_signal_persistence_rejects_invalid_lag():
    with pytest.raises(ValueError):
        signal_persistence_report([1, 2, 3], max_lag=0)
    with pytest.raises(ValueError):
        signal_persistence_report([1, 2, 3], max_lag=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        signal_persistence_report([1, 1, 1], max_lag=1)
