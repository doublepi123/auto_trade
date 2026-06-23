from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.events import BarEvent, EventSource
from app.platform.factors import (
    MeanReversionFactor,
    MomentumFactor,
    VolatilityFactor,
    get_default_registry,
    information_coefficient,
)


def _bar(close: str, minute: int, symbol: str = "A") -> BarEvent:
    return BarEvent(timestamp=datetime(2026, 6, 23, 10, minute, tzinfo=timezone.utc), source=EventSource.MARKET, symbol=symbol, open=Decimal(close), high=Decimal(close), low=Decimal(close), close=Decimal(close), volume=100)


def test_momentum_factor_series():
    bars = [_bar("100", 0), _bar("101", 1), _bar("103", 2), _bar("106", 3)]
    vals = MomentumFactor(period=2).compute(bars)
    assert len(vals) == 4
    assert vals[0] == 0.0 and vals[1] == 0.0
    # at i=2: 103/100 - 1 = 0.03
    assert abs(vals[2] - 0.03) < 1e-9
    # at i=3: 106/101 - 1
    assert abs(vals[3] - (106 / 101 - 1)) < 1e-9


def test_volatility_factor_nonneg():
    bars = [_bar(str(100 + i), i) for i in range(15)]
    vals = VolatilityFactor(period=10).compute(bars)
    assert len(vals) == 15
    assert all(v >= 0 for v in vals)
    assert vals[0] == 0.0


def test_mean_reversion_above_sma_negative():
    # rising price -> close above sma -> positive deviation (price - sma)/sma
    bars = [_bar("100", 0), _bar("100", 1), _bar("100", 2), _bar("200", 3)]
    vals = MeanReversionFactor(period=3).compute(bars)
    assert vals[3] > 0


def test_registry_defaults():
    reg = get_default_registry()
    names = {f["name"] for f in reg.list()}
    assert "momentum_10" in names and "volatility_10" in names and "meanrev_10" in names


def test_information_coefficient_positive_when_factor_predicts():
    # 3 symbols; factor ranks match forward return ranks each period -> IC ~ 1
    factor_values = {
        "A": [1.0, 2.0, 3.0],
        "B": [2.0, 3.0, 1.0],
        "C": [3.0, 1.0, 2.0],
    }
    forward_returns = {
        "A": [0.01, 0.02, 0.03],
        "B": [0.02, 0.03, 0.01],
        "C": [0.03, 0.01, 0.02],
    }
    result = information_coefficient(factor_values, forward_returns)
    assert result["num_periods"] == 3
    assert result["mean_ic"] > 0.99


def test_information_coefficient_single_symbol_returns_zero():
    assert information_coefficient({"A": [1.0]}, {"A": [0.1]})["mean_ic"] == 0.0
