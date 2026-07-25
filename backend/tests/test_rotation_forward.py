from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from app.core.broker import BrokerCandle
from app.core.holiday_calendar import is_market_closed
from app.domain.universe_selection import (
    DIVERSIFIED_INVERSE_VOLATILITY_VARIANT,
    IndexCandidate,
    RotationCohortRegistration,
    UniverseSelectionConfig,
    build_rotation_cohort_registration,
    evaluate_rotation_forward,
    is_last_us_session_of_month,
    next_cohort_month,
)


def _sessions(start: date, end: date) -> list[datetime]:
    current = start
    result: list[datetime] = []
    while current <= end:
        if (
            current.weekday() < 5
            and not is_market_closed("US", current)
        ):
            result.append(
                datetime(
                    current.year,
                    current.month,
                    current.day,
                    20,
                    tzinfo=timezone.utc,
                )
            )
        current += timedelta(days=1)
    return result


_DATES = _sessions(date(2023, 1, 3), date(2025, 7, 18))


def _bars(
    *,
    drift: float,
    dates: list[datetime] | None = None,
    shock_from: date | None = None,
    volatility_scale: float = 1.0,
) -> list[BrokerCandle]:
    price = 100.0
    result: list[BrokerCandle] = []
    for index, timestamp in enumerate(dates or _DATES):
        move = drift + (
            0.009 if index % 2 == 0 else -0.007
        ) * volatility_scale
        if shock_from is not None and timestamp.date() >= shock_from:
            move = -0.025
        open_price = price
        close = open_price * (1.0 + move)
        result.append(
            BrokerCandle(
                timestamp=timestamp,
                open=open_price,
                high=max(open_price, close) * 1.005,
                low=min(open_price, close) * 0.995,
                close=close,
                volume=20_000_000,
                turnover=open_price * 20_000_000,
            )
        )
        price = close
    return result


def _candidate(
    symbol: str,
    sector: str,
) -> IndexCandidate:
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


def _inputs(
    *,
    fast_bars: list[BrokerCandle] | None = None,
) -> tuple[
    tuple[IndexCandidate, ...],
    dict[str, list[BrokerCandle]],
    dict[str, list[BrokerCandle]],
]:
    candidates = (
        _candidate("FAST.US", "Semiconductors"),
        _candidate("SLOW.US", "Financials"),
        _candidate("STEADY.US", "Healthcare"),
    )
    return (
        candidates,
        {
            "FAST.US": fast_bars or _bars(drift=0.0025),
            "SLOW.US": _bars(drift=0.0012),
            "STEADY.US": _bars(drift=0.0008),
        },
        {
            "QQQ.US": _bars(drift=0.0005),
            "DIA.US": _bars(drift=0.0003),
        },
    )


def test_rotation_forward_uses_prior_signal_and_month_open() -> None:
    candidates, bars, benchmarks = _inputs()

    result = evaluate_rotation_forward(
        candidates=candidates,
        bars_by_symbol=bars,
        benchmark_bars_by_symbol=benchmarks,
        base_config=_config(),
        as_of_date=date(2025, 7, 18),
    )

    registration = result.registration
    assert registration is not None
    assert registration.cohort_month == date(2025, 7, 1)
    assert registration.signal_date == date(2025, 6, 30)
    assert registration.target_symbols == (
        "FAST.US",
        "SLOW.US",
        "STEADY.US",
    )
    assert registration.forward_eligible is False
    assert abs(
        sum(
            signal.target_weight_pct
            for signal in registration.target_signals
        )
        - 100.0
    ) < 1e-9
    assert result.snapshot.entry_date == date(2025, 7, 1)
    assert result.snapshot.mark_date == date(2025, 7, 18)
    assert result.snapshot.evidence_mode == "BACKFILLED_AFTER_ENTRY"
    assert result.snapshot.forward_observation_sessions == 0
    assert result.snapshot.net_liquidation_return_pct is not None
    assert result.snapshot.gross_return_pct is not None
    assert (
        result.snapshot.net_liquidation_return_pct
        < result.snapshot.gross_return_pct
    )
    assert result.snapshot.total_estimated_cost_pct is not None
    assert result.snapshot.total_estimated_cost_pct > 0
    selected = [
        row.candidate.symbol
        for row in result.selections
        if row.rotation.selected
    ]
    assert selected == list(registration.target_symbols)


def test_precommitted_registration_counts_forward_sessions() -> None:
    candidates, bars, benchmarks = _inputs()
    registration = build_rotation_cohort_registration(
        candidates=candidates,
        bars_by_symbol=bars,
        base_config=_config(),
        cohort_month=date(2025, 7, 1),
        signal_date=date(2025, 6, 30),
        registered_as_of_date=date(2025, 6, 30),
    )

    result = evaluate_rotation_forward(
        candidates=candidates,
        bars_by_symbol=bars,
        benchmark_bars_by_symbol=benchmarks,
        base_config=_config(),
        as_of_date=date(2025, 7, 18),
        frozen_registration=registration,
    )

    assert result.snapshot.status == "FORWARD_OPEN"
    assert result.snapshot.evidence_mode == "FORWARD_PRECOMMITTED"
    assert result.snapshot.forward_eligible is True
    assert result.snapshot.forward_observation_sessions == (
        result.snapshot.elapsed_sessions
    )
    assert "COHORT_REGISTERED_AFTER_SIGNAL" not in (
        result.snapshot.blockers
    )


def test_midmonth_shock_changes_return_not_frozen_targets() -> None:
    candidates, bars, benchmarks = _inputs()
    registration = build_rotation_cohort_registration(
        candidates=candidates,
        bars_by_symbol=bars,
        base_config=_config(),
        cohort_month=date(2025, 7, 1),
        signal_date=date(2025, 6, 30),
        registered_as_of_date=date(2025, 6, 30),
    )
    baseline = evaluate_rotation_forward(
        candidates=candidates,
        bars_by_symbol=bars,
        benchmark_bars_by_symbol=benchmarks,
        base_config=_config(),
        as_of_date=date(2025, 7, 18),
        frozen_registration=registration,
    )
    shocked_candidates, shocked_bars, shocked_benchmarks = _inputs(
        fast_bars=_bars(
            drift=0.0025,
            shock_from=date(2025, 7, 7),
        )
    )

    shocked = evaluate_rotation_forward(
        candidates=shocked_candidates,
        bars_by_symbol=shocked_bars,
        benchmark_bars_by_symbol=shocked_benchmarks,
        base_config=_config(),
        as_of_date=date(2025, 7, 18),
        frozen_registration=registration,
    )

    assert shocked.snapshot.target_symbols == (
        baseline.snapshot.target_symbols
    )
    assert shocked.snapshot.net_liquidation_return_pct is not None
    assert baseline.snapshot.net_liquidation_return_pct is not None
    assert (
        shocked.snapshot.net_liquidation_return_pct
        < baseline.snapshot.net_liquidation_return_pct
    )
    assert shocked.snapshot.selection_drift_detected is False


def test_frozen_target_drift_is_reported_without_reselection() -> None:
    candidates, bars, benchmarks = _inputs()
    registration = build_rotation_cohort_registration(
        candidates=candidates,
        bars_by_symbol=bars,
        base_config=_config(),
        cohort_month=date(2025, 7, 1),
        signal_date=date(2025, 6, 30),
        registered_as_of_date=date(2025, 6, 30),
    )
    reduced = replace(
        registration,
        target_signals=(
            replace(registration.target_signals[1], rank=1),
        ),
    )

    result = evaluate_rotation_forward(
        candidates=candidates,
        bars_by_symbol=bars,
        benchmark_bars_by_symbol=benchmarks,
        base_config=_config(),
        as_of_date=date(2025, 7, 18),
        frozen_registration=reduced,
    )

    assert result.snapshot.selection_drift_detected is True
    assert result.snapshot.target_symbols == (
        registration.target_signals[1].symbol,
    )
    assert "PRECOMMITTED_COHORT_SELECTION_DRIFT" in (
        result.snapshot.blockers
    )
    assert [
        row.candidate.symbol
        for row in result.selections
        if row.rotation.selected
    ] == list(result.snapshot.target_symbols)


def test_registration_round_trip_and_strict_boolean() -> None:
    candidates, bars, _ = _inputs()
    registration = build_rotation_cohort_registration(
        candidates=candidates,
        bars_by_symbol=bars,
        base_config=_config(),
        cohort_month=date(2025, 7, 1),
        signal_date=date(2025, 6, 30),
        registered_as_of_date=date(2025, 6, 30),
    )

    assert RotationCohortRegistration.from_dict(
        registration.to_dict()
    ) == registration
    legacy = registration.to_dict()
    legacy_signals = legacy["target_signals"]
    assert isinstance(legacy_signals, list)
    for signal in legacy_signals:
        assert isinstance(signal, dict)
        signal.pop("target_weight_pct")
    legacy_registration = RotationCohortRegistration.from_dict(legacy)
    assert legacy_registration.target_symbols == (
        registration.target_symbols
    )
    assert all(
        abs(
            legacy_signal.target_weight_pct
            - current_signal.target_weight_pct
        )
        < 1e-9
        for legacy_signal, current_signal in zip(
            legacy_registration.target_signals,
            registration.target_signals,
        )
    )
    invalid = registration.to_dict()
    invalid["forward_eligible"] = "false"
    try:
        RotationCohortRegistration.from_dict(invalid)
    except ValueError as exc:
        assert "forward_eligible" in str(exc)
    else:
        raise AssertionError("string boolean was accepted")
    invalid = registration.to_dict()
    invalid["registered_as_of_date"] = "2025-06-27"
    try:
        RotationCohortRegistration.from_dict(invalid)
    except ValueError as exc:
        assert "cannot precede" in str(exc)
    else:
        raise AssertionError("pre-signal registration was accepted")


def test_rotation_forward_fails_closed_without_both_benchmarks() -> None:
    candidates, bars, benchmarks = _inputs()

    result = evaluate_rotation_forward(
        candidates=candidates,
        bars_by_symbol=bars,
        benchmark_bars_by_symbol={
            "QQQ.US": benchmarks["QQQ.US"],
        },
        base_config=_config(),
        as_of_date=date(2025, 7, 18),
    )

    assert result.registration is None
    assert result.selections == ()
    assert result.snapshot.status == "BENCHMARK_HISTORY_UNAVAILABLE"
    assert result.snapshot.order_execution_allowed is False
    assert result.snapshot.automatic_promotion_allowed is False


def test_month_end_registration_helpers_are_holiday_aware() -> None:
    assert is_last_us_session_of_month(date(2025, 7, 31)) is True
    assert is_last_us_session_of_month(date(2025, 7, 30)) is False
    assert is_last_us_session_of_month(date(2025, 8, 31)) is False
    assert next_cohort_month(date(2025, 12, 31)) == date(2026, 1, 1)


def test_inverse_volatility_forward_registration_freezes_weights() -> None:
    candidates, bars, benchmarks = _inputs(
        fast_bars=_bars(
            drift=0.0025,
            volatility_scale=2.5,
        )
    )
    variant = replace(
        DIVERSIFIED_INVERSE_VOLATILITY_VARIANT,
        max_selected=3,
        max_position_weight_pct=60.0,
    )
    registration = build_rotation_cohort_registration(
        candidates=candidates,
        bars_by_symbol=bars,
        base_config=_config(),
        cohort_month=date(2025, 7, 1),
        signal_date=date(2025, 6, 30),
        registered_as_of_date=date(2025, 6, 30),
        variant=variant,
    )

    result = evaluate_rotation_forward(
        candidates=candidates,
        bars_by_symbol=bars,
        benchmark_bars_by_symbol=benchmarks,
        base_config=_config(),
        as_of_date=date(2025, 7, 18),
        frozen_registration=registration,
        variant=variant,
    )

    registered_weights = {
        signal.symbol: signal.target_weight_pct
        for signal in registration.target_signals
    }
    snapshot_weights = {
        holding.symbol: holding.weight_pct
        for holding in result.snapshot.holdings
    }
    assert registered_weights == snapshot_weights
    assert registered_weights["FAST.US"] < (
        registered_weights["SLOW.US"]
    )
    assert max(registered_weights.values()) <= 60.0


def test_unavailable_forward_snapshot_keeps_requested_variant() -> None:
    candidates, bars, _ = _inputs()

    result = evaluate_rotation_forward(
        candidates=candidates,
        bars_by_symbol=bars,
        benchmark_bars_by_symbol={},
        base_config=_config(),
        as_of_date=date(2025, 7, 18),
        variant=DIVERSIFIED_INVERSE_VOLATILITY_VARIANT,
    )

    assert result.snapshot.status == "BENCHMARK_HISTORY_UNAVAILABLE"
    assert result.snapshot.variant_name == (
        DIVERSIFIED_INVERSE_VOLATILITY_VARIANT.name
    )
