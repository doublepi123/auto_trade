from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.platform.events import BarEvent, EventSource
from app.platform.momentum_indicators import (
    MACDLine,
    bollinger,
    macd,
    obv,
    stochastic,
    williams_r,
)


def _bars(closes: list[float]) -> list[BarEvent]:
    start = datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc)
    return [
        BarEvent(
            timestamp=start + timedelta(minutes=index),
            source=EventSource.MARKET,
            symbol="AAPL.US",
            open=Decimal(str(close)),
            high=Decimal(str(close + 1.0)),
            low=Decimal(str(close - 1.0)),
            close=Decimal(str(close)),
            volume=100 + index,
        )
        for index, close in enumerate(closes)
    ]


def test_macd_uses_sma_seeded_emas_without_reseeding_fast_line() -> None:
    value = macd([1.0, 2.0, 10.0, 4.0], fast=2, slow=3, signal=2)

    assert value is not None
    assert value.to_dict() == pytest.approx(
        {
            "macd": 0.8888888888888893,
            "signal": 1.8611111111111114,
            "histogram": -0.9722222222222221,
        }
    )


def test_macd_returns_none_until_signal_warmup_completes() -> None:
    closes = [float(index) for index in range(34)]

    assert macd(closes[:-1]) is None
    assert macd(closes) is not None


def test_bollinger_uses_population_standard_deviation() -> None:
    value = bollinger([1.0, 2.0, 3.0, 4.0], period=4, nbdev=2.0)

    assert value is not None
    assert value.to_dict() == pytest.approx(
        {
            "upper": 4.73606797749979,
            "middle": 2.5,
            "lower": 0.2639320225002102,
        }
    )
    assert bollinger([1.0, 2.0, 3.0], period=4) is None


def test_stochastic_flat_range_returns_zero() -> None:
    value = stochastic(
        [5.0] * 5,
        [5.0] * 5,
        [5.0] * 5,
        fastk=3,
        slowk=2,
        slowd=2,
    )

    assert value is not None
    assert value.to_dict() == {"k": 0.0, "d": 0.0}


def test_stochastic_applies_slowk_then_slowd_sma() -> None:
    value = stochastic(
        [3.0, 4.0, 5.0, 6.0, 7.0],
        [1.0, 2.0, 3.0, 4.0, 5.0],
        [2.0, 3.0, 4.0, 5.0, 6.0],
        fastk=3,
        slowk=2,
        slowd=2,
    )

    assert value is not None
    assert value.to_dict() == pytest.approx({"k": 75.0, "d": 75.0})


def test_williams_r_flat_range_returns_zero() -> None:
    assert williams_r([5.0] * 3, [5.0] * 3, [5.0] * 3, period=3) == 0.0


def test_obv_starts_at_first_volume_and_applies_price_sign() -> None:
    result = obv(
        [10.0, 11.0, 11.0, 9.0, 10.0],
        [100.0, 200.0, 300.0, 400.0, 500.0],
    )

    assert result == [100.0, 300.0, 300.0, -100.0, 400.0]
    assert obv([], []) == []


def test_invalid_periods_and_nbdev_raise_value_error() -> None:
    with pytest.raises(ValueError):
        macd([1.0], fast=0)
    with pytest.raises(ValueError):
        bollinger([1.0], period=0)
    with pytest.raises(ValueError):
        bollinger([1.0], nbdev=-1.0)
    with pytest.raises(ValueError):
        stochastic([1.0], [1.0], [1.0], slowd=0)
    with pytest.raises(ValueError):
        williams_r([1.0], [1.0], [1.0], period=0)


def test_mismatched_input_lengths_raise_value_error() -> None:
    with pytest.raises(ValueError):
        stochastic([1.0], [1.0, 2.0], [1.0])
    with pytest.raises(ValueError):
        williams_r([1.0], [1.0], [])
    with pytest.raises(ValueError):
        obv([1.0], [])


def test_macd_wrapper_returns_decimal_and_none_during_warmup() -> None:
    indicator = MACDLine(fast=2, slow=3, signal=2)
    bars = _bars([1.0, 2.0, 10.0, 4.0])

    assert indicator.compute(bars[:-1]) is None
    result = indicator.compute(bars)
    assert isinstance(result, Decimal)
    assert result == Decimal("0.8888888888888893")
