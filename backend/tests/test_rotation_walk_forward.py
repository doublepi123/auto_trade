from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, cast

from app.core.broker import BrokerCandle
from app.domain.universe_selection import (
    DIVERSIFIED_INVERSE_VOLATILITY_VARIANT,
    DIVERSIFIED_SHRINKAGE_ROTATION_VARIANT,
    RETURN_TO_VARIANCE_ROTATION_VARIANT,
    IndexMembershipHistory,
    IndexCandidate,
    MembershipInterval,
    RotationVariant,
    UniverseSelectionConfig,
    evaluate_rotation_walk_forward,
)


def _sessions(count: int) -> list[datetime]:
    current = datetime(2021, 1, 4, 5, tzinfo=timezone.utc)
    result: list[datetime] = []
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


def _month_starts(
    *,
    year: int,
    month: int,
    count: int,
) -> list[datetime]:
    result: list[datetime] = []
    for offset in range(count):
        absolute_month = year * 12 + month - 1 + offset
        result.append(
            datetime(
                absolute_month // 12,
                absolute_month % 12 + 1,
                1,
                17,
                tzinfo=timezone.utc,
            )
        )
    return result


_DATES = _sessions(1100)


def _bars(
    *,
    drift: float,
    dates: list[datetime] | None = None,
    shock_after: int | None = None,
    volatility_scale: float = 1.0,
) -> list[BrokerCandle]:
    price = 100.0
    result: list[BrokerCandle] = []
    for index, timestamp in enumerate(dates or _DATES):
        noise = (
            0.008 if index % 2 == 0 else -0.006
        ) * volatility_scale
        move = drift + noise
        if shock_after is not None and index >= shock_after:
            move = -0.03
        open_price = price
        close = open_price * (1.0 + move)
        result.append(
            BrokerCandle(
                timestamp=timestamp,
                open=open_price,
                high=max(open_price, close) * 1.006,
                low=min(open_price, close) * 0.994,
                close=close,
                volume=20_000_000,
                turnover=open_price * 20_000_000,
            )
        )
        price = close
    return result


def _candidate(symbol: str, sector: str) -> IndexCandidate:
    return IndexCandidate(
        symbol=symbol,
        alias=symbol,
        sector=sector,
        memberships=("NASDAQ_100",),
    )


def _config() -> UniverseSelectionConfig:
    return UniverseSelectionConfig(
        max_selected=3,
        max_per_sector=2,
        min_price=1.0,
        min_avg_dollar_volume=1_000_000,
        max_relative_spread_bps=20.0,
        min_realized_vol_20d=0.01,
        max_realized_vol_20d=3.0,
        min_atr_pct_14d=0.1,
        max_atr_pct_14d=20.0,
        round_trip_fee_bps=16.0,
        round_trip_slippage_bps=4.0,
    )


def _evaluate(
    *,
    fast_bars: list[BrokerCandle] | None = None,
    membership_history: IndexMembershipHistory | None = None,
):
    candidates = (
        _candidate("FAST.US", "Semiconductors"),
        _candidate("SLOW.US", "Financials"),
        _candidate("STEADY.US", "Healthcare"),
    )
    variant = RotationVariant(
        name="baseline",
        lookback_bars=252,
        skip_bars=21,
        sma_bars=200,
        max_selected=2,
        max_per_risk_group=1,
    )
    return evaluate_rotation_walk_forward(
        candidates=candidates,
        bars_by_symbol={
            "FAST.US": fast_bars or _bars(drift=0.0025),
            "SLOW.US": _bars(drift=0.0012),
            "STEADY.US": _bars(drift=0.0008),
        },
        benchmark_bars_by_symbol={
            "QQQ.US": _bars(drift=0.0005),
            "DIA.US": _bars(drift=0.0003),
        },
        base_config=_config(),
        variants=(variant,),
        validation_periods=12,
        membership_history=membership_history,
    )


def test_rotation_walk_forward_uses_prior_close_and_next_month_open() -> None:
    result = _evaluate()

    assert result.algorithm_version == (
        "rotation-monthly-open-walk-forward-v7"
    )
    assert result.evaluation_warmup_bars == 253
    assert result.status == "COMPLETE"
    assert result.selected_variant == "baseline"
    assert result.validated_challenger_variant == "baseline"
    assert result.variants[0].expanding_validation_passed is True
    assert result.variants[0].expanding_folds_passed >= 2
    assert result.variants[0].expanding_folds_total >= 2
    assert result.automatic_promotion_allowed is False
    assert "CURRENT_CONSTITUENTS_SURVIVORSHIP_BIAS" in (
        result.promotion_blockers
    )
    evaluation = result.variants[0]
    assert evaluation.training.periods >= 12
    assert evaluation.validation.periods == 12
    assert evaluation.full.total_return_pct > (
        evaluation.full.qqq_total_return_pct
    )
    assert result.selected_variant_periods
    first = result.selected_variant_periods[0]
    assert first.signal_date < first.entry_date < first.exit_date
    assert first.entry_date.month != first.signal_date.month
    assert 0.1 < first.transaction_cost_pct < 0.2
    assert first.turnover_pct == 100.0
    assert first.selected_symbols == ("FAST.US", "SLOW.US")
    assert first.target_weights_pct == (
        ("FAST.US", 50.0),
        ("SLOW.US", 50.0),
    )


def test_point_in_time_membership_filters_future_periods() -> None:
    history = IndexMembershipHistory(
        source_version="test-history",
        effective_start_date=date(2021, 1, 1),
        catalog_snapshot_date=date(2021, 1, 1),
        sources=(),
        intervals={
            "NASDAQ_100": {
                "FAST": (
                    MembershipInterval(
                        date(2021, 1, 1),
                        date(2024, 1, 1),
                    ),
                ),
                "SLOW": (
                    MembershipInterval(
                        date(2021, 1, 1),
                        None,
                    ),
                ),
                "STEADY": (
                    MembershipInterval(
                        date(2021, 1, 1),
                        None,
                    ),
                ),
            },
        },
        snapshot_overrides={},
    )

    result = _evaluate(membership_history=history)

    assert result.data_scope == "POINT_IN_TIME_RESEARCH_CATALOG"
    assert "HISTORICAL_CONSTITUENTS_OMITTED" not in (
        result.promotion_blockers
    )
    assert "POINT_IN_TIME_MEMBER_DATA_PARTIAL" not in (
        result.promotion_blockers
    )
    assert result.point_in_time_data_missing_symbols == ()
    assert result.point_in_time_required_missing_symbols == ()
    assert result.point_in_time_out_of_window_missing_symbols == ()
    assert "CURRENT_CONSTITUENTS_SURVIVORSHIP_BIAS" not in (
        result.promotion_blockers
    )
    assert all(
        "FAST.US" not in period.selected_symbols
        for period in result.selected_variant_periods
        if period.signal_date >= date(2024, 1, 1)
    )


def test_point_in_time_reports_member_without_overlapping_data() -> None:
    available = _candidate("FAST.US", "Semiconductors")
    unavailable = _candidate("MISSING.US", "Healthcare")
    history = IndexMembershipHistory(
        source_version="test-history",
        effective_start_date=date(2021, 1, 1),
        catalog_snapshot_date=date(2021, 1, 1),
        sources=(),
        intervals={
            "NASDAQ_100": {
                "FAST": (MembershipInterval(date(2021, 1, 1), None),),
                "MISSING": (
                    MembershipInterval(date(2021, 1, 1), None),
                ),
            },
        },
        snapshot_overrides={},
    )

    result = evaluate_rotation_walk_forward(
        candidates=(available, unavailable),
        bars_by_symbol={
            "FAST.US": _bars(drift=0.0025),
            "MISSING.US": (),
        },
        benchmark_bars_by_symbol={
            "QQQ.US": _bars(drift=0.0005),
            "DIA.US": _bars(drift=0.0003),
        },
        base_config=_config(),
        variants=(
            RotationVariant(
                name="baseline",
                lookback_bars=252,
                skip_bars=21,
                sma_bars=200,
                max_selected=2,
                max_per_risk_group=1,
            ),
        ),
        validation_periods=12,
        membership_history=history,
    )

    assert result.point_in_time_data_missing_symbols == (
        "MISSING.US",
    )
    assert result.point_in_time_required_missing_symbols == (
        "MISSING.US",
    )
    assert result.point_in_time_out_of_window_missing_symbols == ()
    assert result.evaluation_first_signal_date == (
        result.selected_variant_periods[0].signal_date
    )
    assert result.evaluation_last_signal_date == (
        result.selected_variant_periods[-1].signal_date
    )
    assert "POINT_IN_TIME_MEMBER_DATA_PARTIAL" in (
        result.promotion_blockers
    )
    assert "HISTORICAL_CONSTITUENTS_OMITTED" not in (
        result.promotion_blockers
    )


def test_point_in_time_all_missing_still_uses_canonical_signal_window(
) -> None:
    missing = _candidate("MISSING.US", "Healthcare")
    history = IndexMembershipHistory(
        source_version="test-history",
        effective_start_date=date(2021, 1, 1),
        catalog_snapshot_date=date(2021, 1, 1),
        sources=(),
        intervals={
            "NASDAQ_100": {
                "MISSING": (
                    MembershipInterval(date(2021, 1, 1), None),
                ),
            },
        },
        snapshot_overrides={},
    )

    result = evaluate_rotation_walk_forward(
        candidates=(missing,),
        bars_by_symbol={"MISSING.US": ()},
        benchmark_bars_by_symbol={
            "QQQ.US": _bars(drift=0.0005),
            "DIA.US": _bars(drift=0.0003),
        },
        base_config=_config(),
        variants=(
            RotationVariant(
                name="baseline",
                lookback_bars=252,
                skip_bars=21,
                sma_bars=200,
                max_selected=2,
                max_per_risk_group=1,
            ),
        ),
        validation_periods=12,
        membership_history=history,
    )

    assert result.evaluation_first_signal_date is not None
    assert result.evaluation_last_signal_date is not None
    assert result.point_in_time_required_missing_symbols == (
        "MISSING.US",
    )
    assert result.point_in_time_out_of_window_missing_symbols == ()
    assert "POINT_IN_TIME_MEMBER_DATA_PARTIAL" in (
        result.promotion_blockers
    )
    assert result.variants[0].full.periods > 0
    assert result.selected_variant_periods
    assert all(
        not period.selected_symbols
        and not period.target_weights_pct
        and period.gross_return_pct == 0
        and period.net_return_pct == 0
        for period in result.selected_variant_periods
    )


def test_point_in_time_reports_out_of_window_missing_member_without_blocking(
) -> None:
    available = _candidate("FAST.US", "Semiconductors")
    historical = _candidate("OLD.US", "Healthcare")
    history = IndexMembershipHistory(
        source_version="test-history",
        effective_start_date=date(2019, 1, 1),
        catalog_snapshot_date=date(2021, 1, 1),
        sources=(),
        intervals={
            "NASDAQ_100": {
                "FAST": (
                    MembershipInterval(date(2021, 1, 1), None),
                ),
                "OLD": (
                    MembershipInterval(
                        date(2019, 1, 1),
                        date(2020, 1, 1),
                    ),
                ),
            },
        },
        snapshot_overrides={},
    )

    result = evaluate_rotation_walk_forward(
        candidates=(available, historical),
        bars_by_symbol={
            "FAST.US": _bars(drift=0.0025),
            "OLD.US": (),
        },
        benchmark_bars_by_symbol={
            "QQQ.US": _bars(drift=0.0005),
            "DIA.US": _bars(drift=0.0003),
        },
        base_config=_config(),
        variants=(
            RotationVariant(
                name="baseline",
                lookback_bars=252,
                skip_bars=21,
                sma_bars=200,
                max_selected=2,
                max_per_risk_group=1,
            ),
        ),
        validation_periods=12,
        membership_history=history,
    )

    assert result.evaluation_first_signal_date is not None
    assert result.evaluation_last_signal_date is not None
    assert result.evaluation_first_signal_date > date(2020, 1, 1)
    assert result.point_in_time_data_missing_symbols == ("OLD.US",)
    assert result.point_in_time_required_missing_symbols == ()
    assert result.point_in_time_out_of_window_missing_symbols == (
        "OLD.US",
    )
    assert "POINT_IN_TIME_MEMBER_DATA_PARTIAL" not in (
        result.promotion_blockers
    )


def test_point_in_time_reentry_stale_signal_is_required_missing() -> None:
    reentered = _candidate("REENTRY.US", "Healthcare")
    history = IndexMembershipHistory(
        source_version="test-history",
        effective_start_date=date(2020, 1, 1),
        catalog_snapshot_date=date(2021, 1, 1),
        sources=(),
        intervals={
            "NASDAQ_100": {
                "REENTRY": (
                    MembershipInterval(
                        date(2020, 1, 1),
                        date(2020, 2, 1),
                    ),
                    MembershipInterval(date(2022, 1, 1), None),
                ),
            },
        },
        snapshot_overrides={},
    )

    result = evaluate_rotation_walk_forward(
        candidates=(reentered,),
        bars_by_symbol={
            "REENTRY.US": _bars(
                drift=0.0025,
                dates=[
                    datetime(
                        2020,
                        1,
                        6,
                        5,
                        tzinfo=timezone.utc,
                    ),
                ],
            ),
        },
        benchmark_bars_by_symbol={
            "QQQ.US": _bars(drift=0.0005),
            "DIA.US": _bars(drift=0.0003),
        },
        base_config=_config(),
        variants=(
            RotationVariant(
                name="baseline",
                lookback_bars=252,
                skip_bars=21,
                sma_bars=200,
                max_selected=2,
                max_per_risk_group=1,
            ),
        ),
        membership_history=history,
    )

    assert result.evaluation_first_signal_date is not None
    assert result.evaluation_last_signal_date is not None
    assert result.point_in_time_data_missing_symbols == (
        "REENTRY.US",
    )
    assert result.point_in_time_required_missing_symbols == (
        "REENTRY.US",
    )
    assert result.point_in_time_out_of_window_missing_symbols == ()
    assert "POINT_IN_TIME_MEMBER_DATA_PARTIAL" in (
        result.promotion_blockers
    )


def test_rotation_walk_forward_does_not_rewrite_earlier_periods() -> None:
    baseline = _evaluate()
    shocked = _evaluate(
        fast_bars=_bars(drift=0.0025, shock_after=len(_DATES) - 40),
    )
    cutoff = _DATES[-40].date()

    baseline_earlier = [
        period
        for period in baseline.selected_variant_periods
        if period.exit_date < cutoff
    ]
    shocked_earlier = [
        period
        for period in shocked.selected_variant_periods
        if period.exit_date < cutoff
    ]
    assert baseline_earlier == shocked_earlier


def test_rotation_walk_forward_does_not_drop_future_missing_exit() -> None:
    baseline = _evaluate()
    first = baseline.selected_variant_periods[0]
    fast_bars = [
        bar
        for bar in _bars(drift=0.0025)
        if bar.timestamp.date() != first.exit_date
    ]

    missing_exit = _evaluate(fast_bars=fast_bars)
    corresponding = next(
        period
        for period in missing_exit.selected_variant_periods
        if period.entry_date == first.entry_date
    )

    assert "FAST.US" in corresponding.selected_symbols
    assert corresponding.gross_return_pct < -40.0


def test_rotation_walk_forward_aligns_variant_evaluation_periods() -> None:
    candidates = (
        _candidate("FAST.US", "Semiconductors"),
        _candidate("SLOW.US", "Financials"),
    )
    result = evaluate_rotation_walk_forward(
        candidates=candidates,
        bars_by_symbol={
            "FAST.US": _bars(drift=0.0025),
            "SLOW.US": _bars(drift=0.0012),
        },
        benchmark_bars_by_symbol={
            "QQQ.US": _bars(drift=0.0005),
            "DIA.US": _bars(drift=0.0003),
        },
        base_config=_config(),
        variants=(
            RotationVariant(
                name="slow",
                lookback_bars=252,
                skip_bars=21,
                sma_bars=200,
                max_selected=2,
                max_per_risk_group=1,
            ),
            RotationVariant(
                name="fast",
                lookback_bars=126,
                skip_bars=21,
                sma_bars=126,
                max_selected=2,
                max_per_risk_group=1,
            ),
        ),
        validation_periods=12,
    )

    period_counts = {
        evaluation.full.periods
        for evaluation in result.variants
    }
    assert len(period_counts) == 1


def test_rotation_walk_forward_rejects_entry_one_bar_before_warmup(
) -> None:
    dates = [
        datetime(2021, 1, day, 17, tzinfo=timezone.utc)
        for day in range(1, 22)
    ] + _month_starts(year=2021, month=2, count=2)
    missing = _candidate("MISSING.US", "Healthcare")
    history = IndexMembershipHistory(
        source_version="test-history",
        effective_start_date=date(2021, 1, 1),
        catalog_snapshot_date=date(2021, 1, 1),
        sources=(),
        intervals={
            "NASDAQ_100": {
                "MISSING": (
                    MembershipInterval(date(2021, 1, 1), None),
                ),
            },
        },
        snapshot_overrides={},
    )
    variant = RotationVariant(
        name="needs_22_bars",
        lookback_bars=21,
        skip_bars=1,
        sma_bars=2,
        max_selected=1,
        max_per_risk_group=1,
    )

    result = evaluate_rotation_walk_forward(
        candidates=(missing,),
        bars_by_symbol={"MISSING.US": ()},
        benchmark_bars_by_symbol={
            "QQQ.US": _bars(drift=0.0005, dates=dates),
            "DIA.US": _bars(drift=0.0003, dates=dates),
        },
        base_config=_config(),
        variants=(variant,),
        membership_history=history,
    )

    assert result.evaluation_warmup_bars == 22
    assert result.status == "HISTORY_INSUFFICIENT"
    assert result.variants[0].full.periods == 0
    assert result.evaluation_first_signal_date is None
    assert result.evaluation_last_signal_date is None
    assert result.point_in_time_required_missing_symbols == ()
    assert result.point_in_time_out_of_window_missing_symbols == (
        "MISSING.US",
    )
    assert "POINT_IN_TIME_MEMBER_DATA_PARTIAL" not in (
        result.promotion_blockers
    )


def test_rotation_walk_forward_accepts_entry_exactly_at_warmup() -> None:
    dates = [
        datetime(2021, 1, day, 17, tzinfo=timezone.utc)
        for day in range(1, 23)
    ] + _month_starts(year=2021, month=2, count=2)
    variant = RotationVariant(
        name="needs_22_bars",
        lookback_bars=21,
        skip_bars=1,
        sma_bars=2,
        max_selected=1,
        max_per_risk_group=1,
    )

    result = evaluate_rotation_walk_forward(
        candidates=(_candidate("FAST.US", "Semiconductors"),),
        bars_by_symbol={
            "FAST.US": _bars(drift=0.0025, dates=dates),
        },
        benchmark_bars_by_symbol={
            "QQQ.US": _bars(drift=0.0005, dates=dates),
            "DIA.US": _bars(drift=0.0003, dates=dates),
        },
        base_config=_config(),
        variants=(variant,),
    )

    assert result.evaluation_warmup_bars == 22
    assert result.variants[0].full.periods == 1
    assert result.evaluation_first_signal_date == date(2021, 1, 22)
    assert result.evaluation_last_signal_date == date(2021, 1, 22)


def test_rotation_walk_forward_uses_largest_variant_warmup_for_all(
) -> None:
    dates = [
        datetime(2021, 1, day, 17, tzinfo=timezone.utc)
        for day in range(1, 23)
    ]
    dates.extend(
        datetime(2021, 2, day, 17, tzinfo=timezone.utc)
        for day in range(1, 5)
    )
    dates.extend(_month_starts(year=2021, month=3, count=30))
    fast = RotationVariant(
        name="needs_22_bars",
        lookback_bars=21,
        skip_bars=1,
        sma_bars=2,
        max_selected=1,
        max_per_risk_group=1,
    )
    slow = replace(
        fast,
        name="needs_25_bars",
        lookback_bars=24,
    )
    common: dict[str, Any] = {
        "candidates": (_candidate("FAST.US", "Semiconductors"),),
        "bars_by_symbol": {
            "FAST.US": _bars(drift=0.0025, dates=dates),
        },
        "benchmark_bars_by_symbol": {
            "QQQ.US": _bars(drift=0.0005, dates=dates),
            "DIA.US": _bars(drift=0.0003, dates=dates),
        },
        "base_config": _config(),
        "validation_periods": 12,
    }

    fast_only = evaluate_rotation_walk_forward(
        **common,
        variants=(fast,),
    )
    aligned = evaluate_rotation_walk_forward(
        **common,
        variants=(fast, slow),
    )

    assert fast_only.evaluation_warmup_bars == 22
    assert fast_only.evaluation_first_signal_date == date(2021, 1, 22)
    assert aligned.evaluation_warmup_bars == 25
    assert aligned.evaluation_first_signal_date == date(2021, 2, 4)
    assert {
        evaluation.full.periods
        for evaluation in aligned.variants
    } == {29}
    assert aligned.variants[0].full.periods == (
        fast_only.variants[0].full.periods - 1
    )


def test_rotation_walk_forward_reports_missing_benchmarks() -> None:
    result = evaluate_rotation_walk_forward(
        candidates=(_candidate("FAST.US", "Semiconductors"),),
        bars_by_symbol={"FAST.US": _bars(drift=0.0025)},
        benchmark_bars_by_symbol={
            "QQQ.US": _bars(drift=0.0005),
        },
        base_config=_config(),
    )

    assert result.status == "BENCHMARK_HISTORY_UNAVAILABLE"
    assert result.selected_variant is None
    assert result.variants == ()


def test_rotation_walk_forward_validates_variant_ranges() -> None:
    try:
        RotationVariant(
            name="invalid",
            lookback_bars=21,
            skip_bars=21,
            sma_bars=20,
            max_selected=1,
            max_per_risk_group=1,
        )
    except ValueError as exc:
        assert "lookback_bars" in str(exc)
    else:
        raise AssertionError("invalid variant was accepted")

    duplicate = RotationVariant(
        name="duplicate",
        lookback_bars=252,
        skip_bars=21,
        sma_bars=200,
        max_selected=1,
        max_per_risk_group=1,
    )
    try:
        evaluate_rotation_walk_forward(
            candidates=(_candidate("FAST.US", "Semiconductors"),),
            bars_by_symbol={"FAST.US": _bars(drift=0.0025)},
            benchmark_bars_by_symbol={
                "QQQ.US": _bars(drift=0.0005),
                "DIA.US": _bars(drift=0.0003),
            },
            base_config=_config(),
            variants=(duplicate, duplicate),
        )
    except ValueError as exc:
        assert "unique" in str(exc)
    else:
        raise AssertionError("duplicate variant names were accepted")


def test_rotation_cost_assumption_changes_net_but_not_gross_return() -> None:
    result = _evaluate()
    no_cost_config = replace(
        _config(),
        round_trip_fee_bps=0.0,
        round_trip_slippage_bps=0.0,
    )
    variant = result.variants[0].variant
    no_cost = evaluate_rotation_walk_forward(
        candidates=(
            _candidate("FAST.US", "Semiconductors"),
            _candidate("SLOW.US", "Financials"),
            _candidate("STEADY.US", "Healthcare"),
        ),
        bars_by_symbol={
            "FAST.US": _bars(drift=0.0025),
            "SLOW.US": _bars(drift=0.0012),
            "STEADY.US": _bars(drift=0.0008),
        },
        benchmark_bars_by_symbol={
            "QQQ.US": _bars(drift=0.0005),
            "DIA.US": _bars(drift=0.0003),
        },
        base_config=no_cost_config,
        variants=(variant,),
        validation_periods=12,
    )

    with_cost_period = result.selected_variant_periods[0]
    no_cost_period = no_cost.selected_variant_periods[0]
    assert with_cost_period.gross_return_pct == no_cost_period.gross_return_pct
    assert with_cost_period.net_return_pct < no_cost_period.net_return_pct


def test_inverse_volatility_weighting_caps_and_reduces_risk_weight() -> None:
    candidates = (
        _candidate("CALM.US", "Healthcare"),
        _candidate("VOLATILE.US", "Semiconductors"),
    )
    result = evaluate_rotation_walk_forward(
        candidates=candidates,
        bars_by_symbol={
            "CALM.US": _bars(
                drift=0.0012,
                volatility_scale=0.5,
            ),
            "VOLATILE.US": _bars(
                drift=0.0025,
                volatility_scale=2.5,
            ),
        },
        benchmark_bars_by_symbol={
            "QQQ.US": _bars(drift=0.0005),
            "DIA.US": _bars(drift=0.0003),
        },
        base_config=_config(),
        variants=(
            replace(
                DIVERSIFIED_INVERSE_VOLATILITY_VARIANT,
                max_selected=2,
                max_position_weight_pct=60.0,
            ),
        ),
        validation_periods=12,
    )

    first = result.selected_variant_periods[0]
    weights = dict(first.target_weights_pct)
    assert 0 < weights["VOLATILE.US"] < weights["CALM.US"]
    assert weights["CALM.US"] <= 60.0
    assert sum(weights.values()) <= 100.0


def test_shrinkage_weighting_blends_equal_and_inverse_volatility() -> None:
    candidates = (
        _candidate("CALM.US", "Healthcare"),
        _candidate("VOLATILE.US", "Semiconductors"),
    )
    common: dict[str, Any] = {
        "candidates": candidates,
        "bars_by_symbol": {
            "CALM.US": _bars(
                drift=0.0012,
                volatility_scale=0.5,
            ),
            "VOLATILE.US": _bars(
                drift=0.0025,
                volatility_scale=2.5,
            ),
        },
        "benchmark_bars_by_symbol": {
            "QQQ.US": _bars(drift=0.0005),
            "DIA.US": _bars(drift=0.0003),
        },
        "base_config": _config(),
        "validation_periods": 12,
    }
    inverse = evaluate_rotation_walk_forward(
        **common,
        variants=(
            replace(
                DIVERSIFIED_INVERSE_VOLATILITY_VARIANT,
                max_selected=2,
                max_position_weight_pct=60.0,
            ),
        ),
    )
    shrinkage = evaluate_rotation_walk_forward(
        **common,
        variants=(
            replace(
                DIVERSIFIED_SHRINKAGE_ROTATION_VARIANT,
                max_selected=2,
                max_position_weight_pct=60.0,
            ),
        ),
    )

    inverse_weights = dict(
        inverse.selected_variant_periods[0].target_weights_pct
    )
    shrinkage_weights = dict(
        shrinkage.selected_variant_periods[0].target_weights_pct
    )
    assert inverse_weights["VOLATILE.US"] < (
        shrinkage_weights["VOLATILE.US"]
    ) < 50.0
    assert 50.0 < shrinkage_weights["CALM.US"] < (
        inverse_weights["CALM.US"]
    )
    assert round(sum(shrinkage_weights.values()), 10) == 100.0


def test_return_to_variance_variant_prefers_steadier_formation_period() -> None:
    candidates = (
        _candidate("CALM.US", "Healthcare"),
        _candidate("VOLATILE.US", "Semiconductors"),
    )
    result = evaluate_rotation_walk_forward(
        candidates=candidates,
        bars_by_symbol={
            "CALM.US": _bars(
                drift=0.0012,
                volatility_scale=0.5,
            ),
            "VOLATILE.US": _bars(
                drift=0.0025,
                volatility_scale=2.5,
            ),
        },
        benchmark_bars_by_symbol={
            "QQQ.US": _bars(drift=0.0005),
            "DIA.US": _bars(drift=0.0003),
        },
        base_config=_config(),
        variants=(
            replace(
                RETURN_TO_VARIANCE_ROTATION_VARIANT,
                max_selected=1,
            ),
        ),
        validation_periods=12,
    )

    assert result.selected_variant_periods
    assert all(
        period.selected_symbols == ("CALM.US",)
        for period in result.selected_variant_periods
    )


def test_rotation_walk_forward_serializes_expanding_fold_dates() -> None:
    result = _evaluate()
    payload = result.to_dict()

    serialized = json.dumps(payload)

    assert "training_end_date" in serialized
    assert "validation_start_date" in serialized
    assert payload["evaluation_warmup_bars"] == (
        result.evaluation_warmup_bars
    )
    assert payload["evaluation_first_signal_date"] == (
        result.evaluation_first_signal_date.isoformat()
        if result.evaluation_first_signal_date is not None
        else None
    )
    assert payload["evaluation_last_signal_date"] == (
        result.evaluation_last_signal_date.isoformat()
        if result.evaluation_last_signal_date is not None
        else None
    )


def test_rotation_variant_rejects_invalid_weighting_controls() -> None:
    base = RotationVariant(
        name="valid",
        lookback_bars=252,
        skip_bars=21,
        sma_bars=200,
        max_selected=8,
        max_per_risk_group=1,
    )

    try:
        replace(
            base,
            weighting=cast(Any, "unsupported"),
        )
    except ValueError as exc:
        assert "weighting" in str(exc)
    else:
        raise AssertionError("invalid weighting was accepted")

    try:
        replace(base, ranking=cast(Any, "unsupported"))
    except ValueError as exc:
        assert "ranking" in str(exc)
    else:
        raise AssertionError("invalid ranking was accepted")

    try:
        replace(base, max_position_weight_pct=0)
    except ValueError as exc:
        assert "max_position_weight_pct" in str(exc)
    else:
        raise AssertionError("invalid position cap was accepted")

    try:
        replace(
            base,
            weighting="equal_inverse_volatility_blend",
        )
    except ValueError as exc:
        assert "inverse-volatility share" in str(exc)
    else:
        raise AssertionError("empty blend share was accepted")

    try:
        replace(base, inverse_volatility_blend_pct=25)
    except ValueError as exc:
        assert "only valid" in str(exc)
    else:
        raise AssertionError("ignored blend share was accepted")
