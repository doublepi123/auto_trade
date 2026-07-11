from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.domain.strategy_v2.features import (
    SessionFeatureEngine,
    StrategyBar,
    StrategyV2FeatureConfig,
    aggregate_complete_five_minute_bars,
    annualized_realized_vol,
    leave_one_out_zscore,
    session_vwap,
    wilder_adx,
)


_NEW_YORK = ZoneInfo("America/New_York")


def _at_open(day: int = 7) -> datetime:
    return datetime(2026, 7, day, 9, 30, tzinfo=_NEW_YORK).astimezone(timezone.utc)


def _bar(
    index: int,
    *,
    price: float = 100.0,
    volume: float = 100.0,
    day: int = 7,
    symbol: str = "NVDA.US",
) -> StrategyBar:
    return StrategyBar(
        timestamp=_at_open(day) + timedelta(minutes=index),
        open=price,
        high=price + 0.1,
        low=price - 0.1,
        close=price,
        volume=volume,
        symbol=symbol,
    )


def test_session_vwap_uses_typical_price_and_resets_with_feature_session() -> None:
    first = StrategyBar(
        timestamp=_at_open(), open=100, high=101, low=99, close=100,
        volume=100, symbol="NVDA.US",
    )
    second = StrategyBar(
        timestamp=_at_open() + timedelta(minutes=1), open=102, high=103, low=101,
        close=102, volume=300, symbol="NVDA.US",
    )
    assert session_vwap([first, second]) == pytest.approx(101.5)

    engine = SessionFeatureEngine(StrategyV2FeatureConfig(settlement_grace_seconds=0))
    engine.on_bar(first, observed_at=first.end_at)
    second_snapshot = engine.on_bar(second, observed_at=second.end_at)
    assert second_snapshot is not None
    assert second_snapshot.session_vwap_1m == pytest.approx(101.5)

    next_day = _bar(0, price=110, day=8)
    reset_snapshot = engine.on_bar(next_day, observed_at=next_day.end_at)
    assert reset_snapshot is not None
    assert reset_snapshot.session_vwap_1m == pytest.approx(110.0)


def test_leave_one_out_zscore_excludes_current_observation() -> None:
    mean, sigma, zscore = leave_one_out_zscore(
        [-0.01, 0.0, 0.01],
        -0.02,
        window=3,
    )
    assert mean == pytest.approx(0.0)
    assert sigma == pytest.approx(0.01)
    assert zscore == pytest.approx(-2.0)


def test_five_minute_aggregation_waits_for_complete_session_bucket() -> None:
    bars = [_bar(index, price=100 + index) for index in range(5)]
    before_close = bars[-1].end_at - timedelta(seconds=1)
    assert aggregate_complete_five_minute_bars(
        bars,
        market="US",
        observed_at=before_close,
    ) == []

    result = aggregate_complete_five_minute_bars(
        bars,
        market="US",
        observed_at=bars[-1].end_at,
    )
    assert len(result) == 1
    aggregate = result[0]
    assert aggregate.timestamp == _at_open()
    assert aggregate.duration_minutes == 5
    assert aggregate.open == 100
    assert aggregate.close == 104
    assert aggregate.high == pytest.approx(104.1)
    assert aggregate.low == pytest.approx(99.9)
    assert aggregate.volume == 500


def test_five_minute_aggregation_requires_one_symbol_and_all_five_minutes() -> None:
    incomplete = [_bar(index) for index in (0, 1, 3, 4)]
    assert aggregate_complete_five_minute_bars(incomplete, market="US") == []
    with pytest.raises(ValueError, match="one non-empty symbol"):
        aggregate_complete_five_minute_bars(
            [_bar(0), _bar(1, symbol="AAPL.US")],
            market="US",
        )


def test_hk_lunch_never_bridges_five_minute_buckets() -> None:
    hong_kong = ZoneInfo("Asia/Hong_Kong")

    def hk_bar(hour: int, minute: int, price: float) -> StrategyBar:
        return StrategyBar(
            timestamp=datetime(2026, 7, 7, hour, minute, tzinfo=hong_kong),
            open=price,
            high=price + 0.1,
            low=price - 0.1,
            close=price,
            volume=100,
            symbol="700.HK",
        )

    morning = [hk_bar(11, minute, 100 + minute / 100) for minute in range(55, 60)]
    afternoon = [hk_bar(13, minute, 101 + minute / 100) for minute in range(5)]
    result = aggregate_complete_five_minute_bars(
        morning + afternoon,
        market="HK",
        observed_at=datetime(2026, 7, 7, 13, 5, tzinfo=hong_kong),
    )
    assert len(result) == 2
    assert result[0].timestamp.astimezone(hong_kong).time().isoformat() == "11:55:00"
    assert result[1].timestamp.astimezone(hong_kong).time().isoformat() == "13:00:00"
    assert result[0].close == pytest.approx(100.59)
    assert result[1].open == pytest.approx(101.0)


def test_wilder_adx_has_standard_seed_and_warmup() -> None:
    bars = [
        StrategyBar(
            timestamp=_at_open() + timedelta(minutes=5 * index),
            open=100 + index,
            high=101 + index,
            low=99 + index,
            close=100.5 + index,
            volume=100,
            symbol="NVDA.US",
            duration_minutes=5,
        )
        for index in range(28)
    ]
    assert wilder_adx(bars[:-1], period=14) is None
    assert wilder_adx(bars, period=14) == pytest.approx(100.0)


def test_feature_engine_requires_settlement_grace_and_one_symbol() -> None:
    engine = SessionFeatureEngine(StrategyV2FeatureConfig(settlement_grace_seconds=5))
    bar = _bar(0)
    assert engine.on_bar(
        bar,
        observed_at=bar.end_at + timedelta(seconds=4),
    ) is None
    assert engine.on_bar(
        bar,
        observed_at=bar.end_at + timedelta(seconds=5),
    ) is not None
    with pytest.raises(ValueError, match="exactly one symbol"):
        engine.on_bar(_bar(1, symbol="AAPL.US"))


def test_dual_timeframe_features_become_available_from_complete_bars() -> None:
    engine = SessionFeatureEngine(StrategyV2FeatureConfig(settlement_grace_seconds=0))
    snapshot = None
    for index in range(145):
        price = 100.0 + 0.002 * index + 0.25 * math.sin(index / 3.0)
        bar = _bar(index, price=price, volume=1000 + index)
        snapshot = engine.on_bar(bar, observed_at=bar.end_at)
    assert snapshot is not None
    assert snapshot.ready
    assert snapshot.bar_timestamp_5m == _at_open() + timedelta(minutes=140)
    assert snapshot.session_vwap_1m is not None
    assert snapshot.zscore_1m is not None
    assert snapshot.session_vwap_5m is not None
    assert snapshot.zscore_5m is not None
    assert snapshot.adx_5m is not None
    assert snapshot.realized_vol_1m is not None


def test_zero_volume_current_bar_is_explicit_gate_failure() -> None:
    engine = SessionFeatureEngine(StrategyV2FeatureConfig(settlement_grace_seconds=0))
    snapshot = None
    for index in range(145):
        price = 100.0 + 0.2 * math.sin(index / 4.0)
        bar = _bar(index, price=price, volume=0 if index == 144 else 1000)
        snapshot = engine.on_bar(bar, observed_at=bar.end_at)
    assert snapshot is not None
    assert not snapshot.ready
    assert "NON_POSITIVE_VOLUME" in snapshot.gate_reasons


def test_realized_vol_default_is_market_aware_and_excludes_lunch_gap() -> None:
    us = StrategyV2FeatureConfig(market="US")
    hk = StrategyV2FeatureConfig(market="HK")
    assert us.realized_vol_periods_per_year == 252 * 390
    assert hk.realized_vol_periods_per_year == 252 * 330

    closes = [100.0 + index * 0.01 for index in range(31)]
    timestamps = [
        datetime(2026, 7, 7, 11, 31, tzinfo=ZoneInfo("Asia/Hong_Kong"))
        + timedelta(minutes=index)
        for index in range(29)
    ]
    timestamps.extend([
        datetime(2026, 7, 7, 13, 0, tzinfo=ZoneInfo("Asia/Hong_Kong")),
        datetime(2026, 7, 7, 13, 1, tzinfo=ZoneInfo("Asia/Hong_Kong")),
    ])
    assert annualized_realized_vol(
        closes,
        window=30,
        periods_per_year=252 * 330,
        timestamps=timestamps,
    ) is None
