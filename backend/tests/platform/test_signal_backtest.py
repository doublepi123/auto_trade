from __future__ import annotations

import pytest

from app.platform.signal_backtest import signal_backtest_report


def test_signal_backtest_single_trade_pnl():
    body = signal_backtest_report([100, 101, 105], entries=[True, False, False], exits=[False, False, True], size=2, initial_cash=1000).to_dict()
    assert body["trades"][0]["pnl"] == pytest.approx(10.0)
    assert body["stats"]["num_trades"] == 1
    assert body["equity_curve"][-1] > 1000


def test_signal_backtest_rejects_length_mismatch():
    with pytest.raises(ValueError):
        signal_backtest_report([100, 101], entries=[True], exits=[False, True])
    with pytest.raises(ValueError):
        signal_backtest_report([100, 101], entries=[1, 0], exits=[False, True])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        signal_backtest_report([100, 101], entries=[True, False], exits=[False, True], initial_cash=0)
    with pytest.raises(ValueError):
        signal_backtest_report([100, 101], target_positions=[0.0, float("nan")])
    with pytest.raises(ValueError):
        signal_backtest_report([100, 101], entries=[True, False], exits=[False, True], fee_bps=float("inf"))
    with pytest.raises(ValueError):
        signal_backtest_report([0, 1], target_positions=[1.0, 1.0])
    with pytest.raises(ValueError):
        signal_backtest_report([0, 1], entries=[True, False], exits=[False, True])
