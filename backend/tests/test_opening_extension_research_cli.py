from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.cli.opening_extension_research import (
    GridEvaluation,
    RawMinuteBar,
    _baseline_session_dates,
    _build_sessions,
    _fetch_symbol_bars,
    _grid_summary,
    _load_cache,
    _parse_integer_grid,
    _parse_symbols,
    _save_cache,
    _select_discovery_winner,
    _selected_status,
)
from app.core.broker import BrokerCandle
from app.core.market_calendar import get_session
from app.domain.opening_momentum import (
    OpeningMomentumConfig,
    OpeningMomentumObservation,
)
from app.domain.opening_momentum_extension import (
    OpeningExtensionExitPrice,
    OpeningExtensionSession,
    evaluate_opening_extension_candidates,
)


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
    assert len(provider.calls) == 3


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
    ) == {}


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
) -> GridEvaluation:
    config = OpeningMomentumConfig(
        signal_minutes=signal_minutes,
        holding_minutes=30,
        minimum_universe_size=2,
        minimum_market_return_bps=-50.0,
        minimum_candidate_return_bps=50.0,
        minimum_excess_return_bps=25.0,
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
        holding_minutes=30,
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
