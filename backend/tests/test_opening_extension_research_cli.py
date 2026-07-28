from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.cli.opening_extension_research import (
    GridEvaluation,
    RawMinuteBar,
    _baseline_session_dates,
    _build_sessions,
    _execution_cohort_payload,
    _fetch_symbol_bars,
    _frozen_selection_grid,
    _grid_summary,
    _joint_exploration_shortlist_payload,
    _load_current_baseline_symbols,
    _load_cache,
    _load_seed_cache,
    _merge_bars,
    _parse_integer_grid,
    _parse_symbols,
    _save_cache,
    _select_discovery_winner,
    _selected_status,
)
from app.core.broker import BrokerCandle
from app.core.market_calendar import get_session
from app.database import SessionLocal, engine
from app.domain.opening_momentum import (
    OpeningMomentumConfig,
    OpeningMomentumObservation,
)
from app.domain.opening_momentum_extension import (
    OpeningExtensionExitPrice,
    OpeningExtensionSession,
    evaluate_opening_extension_candidates,
)
from app.models import Base, StrategyV2ShadowConfig


Base.metadata.create_all(bind=engine)


def _timestamp(
    session_date: date,
    minute_offset: int,
) -> datetime:
    session = get_session("US")
    local_open = datetime.combine(
        session_date,
        session.rth_open,
        tzinfo=session.timezone,
    )
    return (local_open + timedelta(minutes=minute_offset)).astimezone(
        timezone.utc
    )


def _raw_bars(
    symbol: str,
    session_date: date,
    *,
    missing_offset: int | None = None,
) -> tuple[RawMinuteBar, ...]:
    adjustment = {
        "AAA.US": 0.00,
        "BBB.US": 0.01,
        "EXT.US": 0.02,
    }[symbol]
    return tuple(
        RawMinuteBar(
            timestamp=_timestamp(session_date, offset),
            open=100.0 + adjustment + offset / 100,
            close=100.0 + adjustment + (offset + 1) / 100,
        )
        for offset in range(36)
        if offset != missing_offset
    )


def test_session_builder_matches_production_bar_timing() -> None:
    session_date = date(2026, 7, 6)
    bars = {
        symbol: _raw_bars(symbol, session_date)
        for symbol in ("AAA.US", "BBB.US", "EXT.US")
    }

    dates = _baseline_session_dates(
        bars,
        baseline_symbols=("AAA.US", "BBB.US"),
        minimum_universe_size=2,
        minimum_data_coverage=0.95,
    )
    sessions = _build_sessions(
        bars,
        symbols=("AAA.US", "BBB.US", "EXT.US"),
        session_dates=dates,
        signal_minutes=3,
        execution_delay_minutes=1,
        holding_minutes=30,
    )

    assert dates == (session_date,)
    assert len(sessions) == 1
    observations = {item.symbol: item for item in sessions[0].observations}
    exits = {item.symbol: item.price for item in sessions[0].exit_prices}
    assert observations["EXT.US"].session_open == pytest.approx(100.02)
    assert observations["EXT.US"].signal_close == pytest.approx(100.05)
    assert observations["EXT.US"].entry_open == pytest.approx(100.06)
    assert exits["EXT.US"] == pytest.approx(100.36)


def test_session_builder_excludes_incomplete_signal_path() -> None:
    session_date = date(2026, 7, 6)
    bars = {
        "AAA.US": _raw_bars("AAA.US", session_date),
        "BBB.US": _raw_bars("BBB.US", session_date),
        "EXT.US": _raw_bars(
            "EXT.US",
            session_date,
            missing_offset=1,
        ),
    }

    sessions = _build_sessions(
        bars,
        symbols=("AAA.US", "BBB.US", "EXT.US"),
        session_dates=(session_date,),
        signal_minutes=3,
        execution_delay_minutes=1,
        holding_minutes=30,
    )

    assert {item.symbol for item in sessions[0].observations} == {
        "AAA.US",
        "BBB.US",
    }


def test_session_builder_applies_intraday_stop_before_fixed_exit() -> None:
    session_date = date(2026, 7, 6)
    extension_bars = list(_raw_bars("EXT.US", session_date))
    extension_bars[10] = replace(
        extension_bars[10],
        open=100.12,
        high=100.20,
        low=98.00,
        close=99.50,
    )
    bars = {
        "AAA.US": _raw_bars("AAA.US", session_date),
        "BBB.US": _raw_bars("BBB.US", session_date),
        "EXT.US": tuple(extension_bars),
    }

    session = _build_sessions(
        bars,
        symbols=("AAA.US", "BBB.US", "EXT.US"),
        session_dates=(session_date,),
        signal_minutes=3,
        execution_delay_minutes=1,
        holding_minutes=30,
        stop_loss_pct=1.0,
    )[0]

    outcome = next(
        item for item in session.exit_prices if item.symbol == "EXT.US"
    )
    assert outcome.stop_triggered is True
    assert outcome.price == pytest.approx(100.06 * 0.99)


class _PagedProvider:
    def __init__(self, bars: tuple[BrokerCandle, ...]) -> None:
        self.bars = bars
        self.calls: list[datetime] = []

    def get_history_candlesticks_by_offset(
        self,
        symbol: str,
        period: str,
        count: int,
        after: datetime,
    ) -> list[BrokerCandle]:
        del symbol, period
        self.calls.append(after)
        return [bar for bar in self.bars if bar.timestamp >= after][:count]


class _ClampedProvider(_PagedProvider):
    def get_history_candlesticks_by_offset(
        self,
        symbol: str,
        period: str,
        count: int,
        after: datetime,
    ) -> list[BrokerCandle]:
        page = super().get_history_candlesticks_by_offset(
            symbol,
            period,
            count,
            after,
        )
        return page or [self.bars[-1]]


def _broker_bar(timestamp: datetime, price: float) -> BrokerCandle:
    return BrokerCandle(
        timestamp=timestamp,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=100.0,
    )


def test_history_fetch_pages_and_keeps_only_opening_window() -> None:
    session_date = date(2026, 7, 6)
    bars = tuple(
        _broker_bar(_timestamp(session_date, offset), 100.0 + offset)
        for offset in (0, 1, 2, 3, 200)
    )
    provider = _PagedProvider(bars)

    result = _fetch_symbol_bars(
        provider,
        "AAA.US",
        start_date=session_date,
        end_date=session_date,
        retained_minutes_after_open=3,
        page_size=2,
    )

    assert [bar.timestamp for bar in result] == [
        _timestamp(session_date, offset) for offset in range(4)
    ]
    assert len(provider.calls) == 4
    assert provider.calls[-1] == _timestamp(session_date, 201)


def test_history_fetch_bounds_repeated_terminal_page() -> None:
    session_date = date(2026, 7, 6)
    provider = _ClampedProvider(tuple(
        _broker_bar(_timestamp(session_date, offset), 100.0 + offset)
        for offset in (0, 1, 2, 3, 200)
    ))

    result = _fetch_symbol_bars(
        provider,
        "AAA.US",
        start_date=session_date,
        end_date=session_date,
        retained_minutes_after_open=3,
        page_size=2,
    )

    assert len(result) == 4
    assert len(provider.calls) == 5
    assert provider.calls[-2:] == [
        _timestamp(session_date, 201),
        _timestamp(session_date, 201),
    ]


def test_cache_round_trip_and_scope_mismatch(tmp_path: Path) -> None:
    session_date = date(2026, 7, 6)
    path = tmp_path / "opening.json.gz"
    bars = {"AAA.US": _raw_bars("AAA.US", session_date)}

    _save_cache(
        path,
        bars,
        start_date=session_date,
        end_date=session_date,
        retained_minutes_after_open=35,
    )

    assert _load_cache(
        path,
        start_date=session_date,
        end_date=session_date,
        retained_minutes_after_open=35,
    ) == bars
    assert _load_cache(
        path,
        start_date=session_date,
        end_date=session_date,
        retained_minutes_after_open=34,
    ) == bars
    with pytest.raises(ValueError, match="covers only 35 minutes"):
        _load_cache(
            path,
            start_date=session_date,
            end_date=session_date,
            retained_minutes_after_open=36,
        )
    with pytest.raises(ValueError, match="metadata does not match"):
        _load_cache(
            path,
            start_date=session_date - timedelta(days=1),
            end_date=session_date,
            retained_minutes_after_open=35,
        )


def test_seed_cache_loads_prior_scope_and_merges_new_bars(
    tmp_path: Path,
) -> None:
    first = date(2026, 7, 6)
    second = date(2026, 7, 7)
    third = date(2026, 7, 8)
    path = tmp_path / "opening-seed.json.gz"
    seed_bars = {
        "AAA.US": (
            *_raw_bars("AAA.US", first),
            *_raw_bars("AAA.US", second),
        )
    }
    _save_cache(
        path,
        seed_bars,
        start_date=first,
        end_date=second,
        retained_minutes_after_open=35,
    )

    loaded, seed_end = _load_seed_cache(
        path,
        start_date=first,
        end_date=third,
        retained_minutes_after_open=35,
    )

    assert loaded == seed_bars
    assert seed_end == second
    replacement = replace(
        _raw_bars("AAA.US", second)[0],
        close=101.0,
        high=101.0,
    )
    new_bar = _raw_bars("AAA.US", third)[0]
    merged = _merge_bars(
        loaded["AAA.US"],
        (new_bar, replacement),
    )
    assert len(merged) == len(seed_bars["AAA.US"]) + 1
    assert [bar.timestamp for bar in merged] == sorted(
        bar.timestamp for bar in merged
    )
    assert next(
        bar for bar in merged if bar.timestamp == replacement.timestamp
    ) == replacement


def test_seed_cache_rejects_incompatible_scope(tmp_path: Path) -> None:
    first = date(2026, 7, 6)
    second = date(2026, 7, 7)
    third = date(2026, 7, 8)
    path = tmp_path / "opening-seed.json.gz"
    _save_cache(
        path,
        {"AAA.US": _raw_bars("AAA.US", first)},
        start_date=first,
        end_date=second,
        retained_minutes_after_open=35,
    )

    with pytest.raises(ValueError, match="start date does not match"):
        _load_seed_cache(
            path,
            start_date=first - timedelta(days=1),
            end_date=third,
            retained_minutes_after_open=35,
        )
    with pytest.raises(ValueError, match="must precede"):
        _load_seed_cache(
            path,
            start_date=first,
            end_date=second,
            retained_minutes_after_open=35,
        )
    with pytest.raises(ValueError, match="covers only 35 minutes"):
        _load_seed_cache(
            path,
            start_date=first,
            end_date=third,
            retained_minutes_after_open=36,
        )


def _research_sessions(
    *,
    discovery_exit: float,
    holdout_exit: float,
) -> tuple[OpeningExtensionSession, ...]:
    return tuple(
        OpeningExtensionSession(
            session_date=date(2026, 1, 1) + timedelta(days=index),
            observations=(
                OpeningMomentumObservation(
                    "AAA.US",
                    100.0,
                    100.10,
                    100.0,
                ),
                OpeningMomentumObservation(
                    "BBB.US",
                    100.0,
                    100.80,
                    100.0,
                ),
                OpeningMomentumObservation(
                    "EXT.US",
                    100.0,
                    101.20,
                    100.0,
                ),
            ),
            exit_prices=(
                OpeningExtensionExitPrice("AAA.US", 100.0),
                OpeningExtensionExitPrice("BBB.US", 100.0),
                OpeningExtensionExitPrice(
                    "EXT.US",
                    discovery_exit if index < 30 else holdout_exit,
                ),
            ),
        )
        for index in range(50)
    )


def _grid(
    signal_minutes: int,
    *,
    discovery_exit: float,
    holdout_exit: float,
    holding_minutes: int = 30,
    stop_loss_pct: float | None = None,
) -> GridEvaluation:
    config = OpeningMomentumConfig(
        signal_minutes=signal_minutes,
        holding_minutes=holding_minutes,
        minimum_universe_size=2,
        minimum_market_return_bps=-50.0,
        minimum_candidate_return_bps=50.0,
        minimum_excess_return_bps=25.0,
        stop_loss_pct=stop_loss_pct,
    )
    report = evaluate_opening_extension_candidates(
        _research_sessions(
            discovery_exit=discovery_exit,
            holdout_exit=holdout_exit,
        ),
        baseline_symbols=("AAA.US", "BBB.US"),
        extension_symbols=("EXT.US",),
        config=config,
    )
    return GridEvaluation(
        signal_minutes=signal_minutes,
        holding_minutes=holding_minutes,
        report=report,
    )


def test_grid_selection_never_uses_holdout_performance() -> None:
    discovery_winner = _grid(
        2,
        discovery_exit=101.0,
        holdout_exit=98.0,
    )
    holdout_winner = _grid(
        3,
        discovery_exit=100.7,
        holdout_exit=105.0,
    )

    selected = _select_discovery_winner((
        discovery_winner,
        holdout_winner,
    ))
    status, blockers = _selected_status(selected)

    assert selected.grid.signal_minutes == 2
    assert status == "REJECTED"
    assert "HOLDOUT_DELTA_NOT_POSITIVE" in blockers


def test_formal_selection_is_frozen_to_production_grid() -> None:
    sensitivity = _grid(
        2,
        discovery_exit=105.0,
        holdout_exit=105.0,
    )
    production = GridEvaluation(
        signal_minutes=3,
        holding_minutes=60,
        report=_grid(
            3,
            discovery_exit=100.5,
            holdout_exit=100.5,
            holding_minutes=60,
            stop_loss_pct=1.0,
        ).report,
    )

    assert _frozen_selection_grid((sensitivity, production)) is production
    with pytest.raises(ValueError, match="frozen 3m/60m"):
        _frozen_selection_grid((sensitivity,))


def test_selected_status_requires_robust_holdout() -> None:
    grid = _grid(
        3,
        discovery_exit=101.0,
        holdout_exit=105.0,
    )
    selected = _select_discovery_winner((grid,))

    status, blockers = _selected_status(selected)
    summary = _grid_summary(grid)

    assert status == "HISTORICALLY_ROBUST"
    assert blockers == ()
    assert summary["discovery_winner"] == "EXT.US"
    assert "candidates" not in summary


def test_execution_cohort_is_frozen_from_discovery_only() -> None:
    grid = _grid(
        3,
        discovery_exit=101.0,
        holdout_exit=95.0,
        holding_minutes=60,
        stop_loss_pct=1.0,
    )

    payload = _execution_cohort_payload(grid)

    assert payload["selection_uses_holdout"] is False
    assert payload["selection_stage"] == "INDIVIDUAL_CANDIDATE_SHORTLIST"
    assert payload["joint_subset_selection_required"] is True
    assert payload["automatic_execution_cohort_allowed"] is False
    assert payload["symbols"] == ["EXT.US"]


def test_joint_exploration_shortlist_keeps_sparse_robust_candidate() -> None:
    grid = _grid(
        3,
        discovery_exit=101.0,
        holdout_exit=95.0,
        holding_minutes=60,
        stop_loss_pct=1.0,
    )
    candidate = grid.report.candidates[0]
    sparse_slices = tuple(
        replace(
            value,
            displaced_baseline_sessions=1,
            extension_signal_sessions=1,
        )
        if value.name == "DISCOVERY"
        else value
        for value in candidate.slices
    )
    sparse_grid = replace(
        grid,
        report=replace(
            grid.report,
            candidates=(replace(candidate, slices=sparse_slices),),
        ),
    )

    robust = _execution_cohort_payload(sparse_grid)
    exploratory = _joint_exploration_shortlist_payload(sparse_grid)

    assert robust["symbols"] == []
    assert exploratory["selection_uses_holdout"] is False
    assert exploratory["selection_stage"] == (
        "JOINT_EXPLORATION_CANDIDATE_SHORTLIST"
    )
    assert exploratory["joint_subset_selection_required"] is True
    assert exploratory["automatic_execution_cohort_allowed"] is False
    assert exploratory["minimum_displacement_sessions"] == 1
    assert exploratory["symbols"] == ["EXT.US"]


def test_cli_list_parsers_fail_closed() -> None:
    assert _parse_symbols(
        "sndk.us, lite.us",
        field_name="extensions",
    ) == ("SNDK.US", "LITE.US")
    assert _parse_integer_grid(
        "2,3,10",
        field_name="signals",
        minimum=1,
        maximum=120,
    ) == (2, 3, 10)
    with pytest.raises(ValueError, match="only supports"):
        _parse_symbols("0700.HK", field_name="extensions")
    with pytest.raises(ValueError, match="unique"):
        _parse_integer_grid(
            "3,3",
            field_name="signals",
            minimum=1,
            maximum=120,
        )


def test_default_baseline_excludes_observation_only_symbols() -> None:
    symbols = ("EXECUTION.US", "OBSERVATION.US", "DISABLED.US")
    db = SessionLocal()
    try:
        db.query(StrategyV2ShadowConfig).filter(
            StrategyV2ShadowConfig.symbol.in_(symbols)
        ).delete(synchronize_session=False)
        db.add_all([
            StrategyV2ShadowConfig(
                symbol="EXECUTION.US",
                enabled=True,
                opening_momentum_execution_eligible=True,
            ),
            StrategyV2ShadowConfig(
                symbol="OBSERVATION.US",
                enabled=True,
                opening_momentum_execution_eligible=False,
            ),
            StrategyV2ShadowConfig(
                symbol="DISABLED.US",
                enabled=False,
                opening_momentum_execution_eligible=True,
            ),
        ])
        db.commit()

        baseline = set(_load_current_baseline_symbols())

        assert "EXECUTION.US" in baseline
        assert "OBSERVATION.US" not in baseline
        assert "DISABLED.US" not in baseline
    finally:
        db.query(StrategyV2ShadowConfig).filter(
            StrategyV2ShadowConfig.symbol.in_(symbols)
        ).delete(synchronize_session=False)
        db.commit()
        db.close()
