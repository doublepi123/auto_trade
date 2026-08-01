from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from statistics import stdev
from zoneinfo import ZoneInfo

import pytest

from app.domain.strategy_v2.features import (
    BOUNDARY_NEUTRAL_PREWARM_ALGORITHM_VERSION,
    BoundaryNeutralCausalTrendPrewarmFeatureEngine,
    CausalTrendPrewarmFeatureEngine,
    SessionFeatureEngine,
    StrategyBar,
    StrategyV2FeatureConfig,
    aggregate_complete_five_minute_bars,
    annualized_realized_vol,
    boundary_neutral_wilder_adx,
    leave_one_out_zscore,
    session_vwap,
    wilder_adx,
)
from app.domain.strategy_v2.engine import StrategyV2Engine, StrategyV2State


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


def _session_bars(day: int, *, price_shift: float = 0.0) -> list[StrategyBar]:
    bars: list[StrategyBar] = []
    for index in range(390):
        price = (
            100.0
            + price_shift
            + 0.002 * index
            + 0.25 * math.sin(index / 3.0)
        )
        bars.append(_bar(index, price=price, volume=1000 + index, day=day))
    return bars


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


def test_boundary_neutral_wilder_adx_changes_only_the_named_boundary() -> None:
    assert BOUNDARY_NEUTRAL_PREWARM_ALGORITHM_VERSION == (
        "strategy-v2-causal-trend-prewarm-boundary-neutral-v1"
    )
    bars = [
        StrategyBar(
            timestamp=_at_open() + timedelta(minutes=5 * index),
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=100,
            symbol="NVDA.US",
            duration_minutes=5,
        )
        for index, (open_price, high, low, close) in enumerate((
            (10.0, 11.0, 9.0, 10.0),
            (11.0, 12.0, 10.0, 11.0),
            (6.0, 7.0, 5.0, 6.0),
            (4.0, 6.0, 3.0, 4.0),
        ))
    ]

    assert wilder_adx(bars, period=2) == pytest.approx(73.33333333333333)
    assert boundary_neutral_wilder_adx(
        bars,
        boundary_at=bars[2].timestamp,
        period=2,
    ) == pytest.approx(80.0)
    assert boundary_neutral_wilder_adx(
        bars,
        boundary_at=bars[-1].end_at,
        period=2,
    ) == pytest.approx(wilder_adx(bars, period=2))


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


def test_causal_trend_prewarm_preserves_local_features_and_reduces_warmup() -> None:
    config = StrategyV2FeatureConfig(settlement_grace_seconds=0)
    seed = _session_bars(7)
    target = _session_bars(8, price_shift=2.0)
    baseline = SessionFeatureEngine(config)
    prewarmed = CausalTrendPrewarmFeatureEngine(config, seed)

    assert prewarmed.session_day is None
    strategy = StrategyV2Engine(features=prewarmed)
    strategy_snapshot = strategy.snapshot()
    assert strategy_snapshot.state == StrategyV2State.COLD
    assert strategy_snapshot.last_processed_at is None

    baseline_first_ready: int | None = None
    prewarmed_first_ready: int | None = None
    local_fields = (
        "bar_index",
        "bar_timestamp_5m",
        "session_vwap_1m",
        "residual_1m",
        "residual_mean_1m",
        "residual_sigma_1m",
        "zscore_1m",
        "session_vwap_5m",
        "residual_5m",
        "residual_mean_5m",
        "residual_sigma_5m",
        "zscore_5m",
    )
    for index, bar in enumerate(target[:145]):
        baseline_snapshot = baseline.on_bar(bar, observed_at=bar.end_at)
        prewarmed_snapshot = prewarmed.on_bar(bar, observed_at=bar.end_at)
        assert baseline_snapshot is not None
        assert prewarmed_snapshot is not None
        for field in local_fields:
            assert getattr(prewarmed_snapshot, field) == getattr(
                baseline_snapshot,
                field,
            )
        if baseline_snapshot.ready and baseline_first_ready is None:
            baseline_first_ready = index
        if prewarmed_snapshot.ready and prewarmed_first_ready is None:
            prewarmed_first_ready = index

    assert baseline_first_ready == 139
    assert prewarmed_first_ready == 64


def test_causal_trend_prewarm_excludes_overnight_return_but_keeps_adx_gap() -> None:
    config = StrategyV2FeatureConfig(settlement_grace_seconds=0)
    seed = _session_bars(7)
    target = _session_bars(8, price_shift=25.0)
    engine = CausalTrendPrewarmFeatureEngine(config, seed)

    seed_returns = [
        math.log(current.close / previous.close)
        for previous, current in zip(seed, seed[1:])
    ]
    expected_seed_vol = stdev(seed_returns[-config.realized_vol_window_1m :]) * math.sqrt(
        int(config.realized_vol_periods_per_year or 0)
    )
    first = engine.on_bar(target[0], observed_at=target[0].end_at)
    assert first is not None
    assert first.realized_vol_1m == pytest.approx(expected_seed_vol)

    snapshot = first
    for bar in target[1:5]:
        snapshot = engine.on_bar(bar, observed_at=bar.end_at)
    assert snapshot is not None
    seed_5m = aggregate_complete_five_minute_bars(seed, market="US")
    target_5m = aggregate_complete_five_minute_bars(target[:5], market="US")
    expected_adx = wilder_adx(
        [*seed_5m, *target_5m],
        period=config.adx_period,
    )
    assert snapshot.adx_5m == pytest.approx(expected_adx)


def test_boundary_neutral_prewarm_ignores_positive_and_negative_overnight_gap() -> None:
    config = StrategyV2FeatureConfig(settlement_grace_seconds=0)
    seed = _session_bars(7)
    positive_gap = _session_bars(8, price_shift=25.0)
    negative_gap = _session_bars(8, price_shift=-20.0)
    positive_v1 = CausalTrendPrewarmFeatureEngine(config, seed)
    negative_v1 = CausalTrendPrewarmFeatureEngine(config, seed)
    positive_neutral = BoundaryNeutralCausalTrendPrewarmFeatureEngine(
        config,
        seed,
    )
    negative_neutral = BoundaryNeutralCausalTrendPrewarmFeatureEngine(
        config,
        seed,
    )

    v1_pairs: list[tuple[float, float]] = []
    neutral_pairs: list[tuple[float, float]] = []
    positive_snapshot = None
    negative_snapshot = None
    for positive_bar, negative_bar in zip(
        positive_gap[:90],
        negative_gap[:90],
    ):
        positive_v1_snapshot = positive_v1.on_bar(
            positive_bar,
            observed_at=positive_bar.end_at,
        )
        negative_v1_snapshot = negative_v1.on_bar(
            negative_bar,
            observed_at=negative_bar.end_at,
        )
        positive_snapshot = positive_neutral.on_bar(
            positive_bar,
            observed_at=positive_bar.end_at,
        )
        negative_snapshot = negative_neutral.on_bar(
            negative_bar,
            observed_at=negative_bar.end_at,
        )
        assert positive_v1_snapshot is not None
        assert negative_v1_snapshot is not None
        assert positive_snapshot is not None
        assert negative_snapshot is not None
        if (
            positive_v1_snapshot.adx_5m is not None
            and negative_v1_snapshot.adx_5m is not None
            and positive_snapshot.adx_5m is not None
            and negative_snapshot.adx_5m is not None
        ):
            v1_pairs.append((
                positive_v1_snapshot.adx_5m,
                negative_v1_snapshot.adx_5m,
            ))
            neutral_pairs.append((
                positive_snapshot.adx_5m,
                negative_snapshot.adx_5m,
            ))

    assert any(left != pytest.approx(right) for left, right in v1_pairs[5:])
    assert all(left == pytest.approx(right) for left, right in neutral_pairs)
    assert positive_snapshot is not None
    assert negative_snapshot is not None
    seed_5m = aggregate_complete_five_minute_bars(seed, market="US")
    positive_5m = aggregate_complete_five_minute_bars(
        positive_gap[:90],
        market="US",
    )
    negative_5m = aggregate_complete_five_minute_bars(
        negative_gap[:90],
        market="US",
    )
    assert positive_snapshot.adx_5m == pytest.approx(
        boundary_neutral_wilder_adx(
            [*seed_5m, *positive_5m],
            boundary_at=positive_5m[0].timestamp,
            period=config.adx_period,
        )
    )
    assert negative_snapshot.adx_5m == pytest.approx(
        boundary_neutral_wilder_adx(
            [*seed_5m, *negative_5m],
            boundary_at=negative_5m[0].timestamp,
            period=config.adx_period,
        )
    )


def test_boundary_neutral_prewarm_is_prefix_invariant() -> None:
    config = StrategyV2FeatureConfig(settlement_grace_seconds=0)
    seed = _session_bars(7)
    unchanged = _session_bars(8, price_shift=2.0)
    changed = [
        bar
        if index < 90
        else _bar(
            index,
            price=bar.close + 10.0,
            volume=bar.volume,
            day=8,
        )
        for index, bar in enumerate(unchanged)
    ]
    first = BoundaryNeutralCausalTrendPrewarmFeatureEngine(config, seed)
    second = BoundaryNeutralCausalTrendPrewarmFeatureEngine(config, seed)

    for left_bar, right_bar in zip(unchanged[:90], changed[:90]):
        left = first.on_bar(left_bar, observed_at=left_bar.end_at)
        right = second.on_bar(right_bar, observed_at=right_bar.end_at)
        assert left == right

    left_future = first.on_bar(unchanged[90], observed_at=unchanged[90].end_at)
    right_future = second.on_bar(changed[90], observed_at=changed[90].end_at)
    assert left_future is not None
    assert right_future is not None
    assert left_future.bar.close != right_future.bar.close


def test_boundary_neutral_prewarm_preserves_v1_session_local_features() -> None:
    config = StrategyV2FeatureConfig(settlement_grace_seconds=0)
    seed = _session_bars(7)
    target = _session_bars(8, price_shift=25.0)
    current = CausalTrendPrewarmFeatureEngine(config, seed)
    neutral = BoundaryNeutralCausalTrendPrewarmFeatureEngine(config, seed)
    local_fields = (
        "session_day",
        "bar_index",
        "bar_timestamp_5m",
        "session_vwap_1m",
        "residual_1m",
        "residual_mean_1m",
        "residual_sigma_1m",
        "zscore_1m",
        "session_vwap_5m",
        "residual_5m",
        "residual_mean_5m",
        "residual_sigma_5m",
        "zscore_5m",
        "realized_vol_1m",
        "ready",
        "gate_reasons",
    )
    adx_changed = False
    for bar in target[:145]:
        current_snapshot = current.on_bar(bar, observed_at=bar.end_at)
        neutral_snapshot = neutral.on_bar(bar, observed_at=bar.end_at)
        assert current_snapshot is not None
        assert neutral_snapshot is not None
        for field in local_fields:
            assert getattr(neutral_snapshot, field) == getattr(
                current_snapshot,
                field,
            )
        adx_changed = adx_changed or (
            neutral_snapshot.adx_5m != pytest.approx(current_snapshot.adx_5m)
        )
    assert adx_changed


def test_causal_trend_prewarm_retains_valid_returns_across_hk_lunch() -> None:
    hong_kong = ZoneInfo("Asia/Hong_Kong")

    def hk_session_bars(day: int) -> list[StrategyBar]:
        timestamps = [
            datetime(2026, 7, day, 9, 30, tzinfo=hong_kong)
            + timedelta(minutes=index)
            for index in range(150)
        ]
        timestamps.extend(
            datetime(2026, 7, day, 13, 0, tzinfo=hong_kong)
            + timedelta(minutes=index)
            for index in range(180)
        )
        result: list[StrategyBar] = []
        for index, timestamp in enumerate(timestamps):
            price = 100.0 + 0.002 * index + 0.15 * math.sin(index / 4.0)
            result.append(StrategyBar(
                timestamp=timestamp,
                open=price,
                high=price + 0.1,
                low=price - 0.1,
                close=price,
                volume=1000 + index,
                symbol="700.HK",
            ))
        return result

    config = StrategyV2FeatureConfig(market="HK", settlement_grace_seconds=0)
    seed = hk_session_bars(7)
    target = hk_session_bars(8)
    baseline = SessionFeatureEngine(config)
    prewarmed = CausalTrendPrewarmFeatureEngine(config, seed)
    baseline_snapshot = None
    prewarmed_snapshot = None
    for bar in target[:151]:
        baseline_snapshot = baseline.on_bar(bar, observed_at=bar.end_at)
        prewarmed_snapshot = prewarmed.on_bar(bar, observed_at=bar.end_at)

    assert baseline_snapshot is not None
    assert prewarmed_snapshot is not None
    assert baseline_snapshot.realized_vol_1m is None
    assert prewarmed_snapshot.realized_vol_1m is not None


def test_causal_trend_prewarm_is_prefix_invariant() -> None:
    config = StrategyV2FeatureConfig(settlement_grace_seconds=0)
    seed = _session_bars(7)
    unchanged = _session_bars(8, price_shift=2.0)
    changed = [
        bar
        if index < 90
        else _bar(
            index,
            price=bar.close + 10.0,
            volume=bar.volume,
            day=8,
        )
        for index, bar in enumerate(unchanged)
    ]
    first = CausalTrendPrewarmFeatureEngine(config, seed)
    second = CausalTrendPrewarmFeatureEngine(config, seed)

    for left_bar, right_bar in zip(unchanged[:90], changed[:90]):
        left = first.on_bar(left_bar, observed_at=left_bar.end_at)
        right = second.on_bar(right_bar, observed_at=right_bar.end_at)
        assert left == right

    left_future = first.on_bar(unchanged[90], observed_at=unchanged[90].end_at)
    right_future = second.on_bar(changed[90], observed_at=changed[90].end_at)
    assert left_future is not None
    assert right_future is not None
    assert left_future.bar.close != right_future.bar.close


def test_causal_trend_prewarm_validates_seed_and_preserves_gap_failure() -> None:
    config = StrategyV2FeatureConfig(settlement_grace_seconds=0)
    seed = _session_bars(7)
    with pytest.raises(ValueError, match="complete RTH session"):
        CausalTrendPrewarmFeatureEngine(config, seed[:-1])

    engine = CausalTrendPrewarmFeatureEngine(config, seed)
    target = _session_bars(8, price_shift=2.0)
    snapshot = None
    for index, bar in enumerate(target[:145]):
        if index == 20:
            continue
        snapshot = engine.on_bar(bar, observed_at=bar.end_at)
    assert snapshot is not None
    assert snapshot.ready is False
    assert "SESSION_DATA_INCOMPLETE" in snapshot.gate_reasons
