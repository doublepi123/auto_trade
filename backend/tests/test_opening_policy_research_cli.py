from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.cli.opening_extension_research import RawMinuteBar, _save_cache
from app.cli.opening_policy_research import (
    _build_policy_sessions,
    _default_policy_specs,
    main,
)
from app.core.market_calendar import get_session
from app.domain.opening_momentum_policy import (
    PRODUCTION_MAXIMUM_MARKET_RETURN_BPS,
    PRODUCTION_MINIMUM_PATH_EFFICIENCY,
    PRODUCTION_POLICY_NAME,
)


_SYMBOLS = tuple(f"S{index}.US" for index in range(8))
_COHORT_SYMBOL = "C0.US"


def _timestamp(session_date: date, minute_offset: int) -> datetime:
    session = get_session("US")
    local_open = datetime.combine(
        session_date,
        session.rth_open,
        tzinfo=session.timezone,
    )
    return (local_open + timedelta(minutes=minute_offset)).astimezone(
        timezone.utc
    )


def _bars(symbol: str, session_date: date) -> tuple[RawMinuteBar, ...]:
    symbol_index = (
        _SYMBOLS.index(symbol) if symbol in _SYMBOLS else -1
    )
    final_signal_closes = (
        99.30,
        99.40,
        99.50,
        99.60,
        99.70,
        99.80,
        99.90,
        101.00,
    )
    result: list[RawMinuteBar] = []
    for offset in range(65):
        open_price = 100.0
        close_price = 100.0
        if symbol == _COHORT_SYMBOL and offset < 3:
            close_price = (101.00, 101.10, 101.20)[offset]
        elif symbol == "S7.US" and offset < 3:
            close_price = (101.50, 100.50, 101.00)[offset]
        elif offset < 3:
            close_price = final_signal_closes[symbol_index]
        if offset == 64 and symbol == _COHORT_SYMBOL:
            open_price = 102.00
            close_price = 102.00
        elif offset == 64 and symbol == "S7.US":
            open_price = 101.50
            close_price = 101.50
        result.append(RawMinuteBar(
            timestamp=_timestamp(session_date, offset),
            open=open_price,
            close=close_price,
        ))
    return tuple(result)


def _bars_by_symbol(
    *session_dates: date,
) -> dict[str, tuple[RawMinuteBar, ...]]:
    return {
        symbol: tuple(
            bar
            for session_date in session_dates
            for bar in _bars(symbol, session_date)
        )
        for symbol in _SYMBOLS
    }


def _bars_by_symbol_with_cohort(
    *session_dates: date,
) -> dict[str, tuple[RawMinuteBar, ...]]:
    result = _bars_by_symbol(*session_dates)
    result[_COHORT_SYMBOL] = tuple(
        bar
        for session_date in session_dates
        for bar in _bars(_COHORT_SYMBOL, session_date)
    )
    return result


def test_default_grid_contains_unique_production_and_neighbors() -> None:
    policies = _default_policy_specs()
    names = [value.name for value in policies]
    production = next(
        value for value in policies if value.name == PRODUCTION_POLICY_NAME
    )

    assert len(policies) == 36
    assert len(names) == len(set(names))
    assert policies[0].name == "BROAD"
    assert production.minimum_path_efficiency == (
        PRODUCTION_MINIMUM_PATH_EFFICIENCY
    )
    assert production.maximum_market_return_bps == (
        PRODUCTION_MAXIMUM_MARKET_RETURN_BPS
    )


def test_policy_session_builder_matches_frozen_timing_and_path() -> None:
    session_date = date(2026, 7, 6)

    sessions = _build_policy_sessions(
        _bars_by_symbol(session_date),
        symbols=_SYMBOLS,
        session_dates=(session_date,),
        minimum_data_coverage=0.95,
    )

    assert len(sessions) == 1
    value = sessions[0]
    assert value.baseline_signal is True
    assert value.candidate_symbol == "S7.US"
    assert value.market_return_bps == pytest.approx(-35.0)
    assert value.candidate_path_efficiency == pytest.approx(1 / 3)
    assert value.gross_return_bps == pytest.approx(150.0)
    assert value.stop_triggered is False


def test_policy_session_builder_requires_every_cohort_symbol() -> None:
    session_date = date(2026, 7, 6)

    sessions = _build_policy_sessions(
        _bars_by_symbol(session_date),
        symbols=(*_SYMBOLS, _COHORT_SYMBOL),
        required_symbols=(_COHORT_SYMBOL,),
        session_dates=(session_date,),
        minimum_data_coverage=0.95,
    )

    assert sessions == ()
    with pytest.raises(ValueError, match="outside the policy universe"):
        _build_policy_sessions(
            _bars_by_symbol(session_date),
            symbols=_SYMBOLS,
            required_symbols=(_COHORT_SYMBOL,),
            session_dates=(session_date,),
            minimum_data_coverage=0.95,
        )


def test_cli_replays_existing_cache_without_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = date(2026, 7, 6)
    second = date(2026, 7, 7)
    cache_path = tmp_path / "opening.json.gz"
    output_path = tmp_path / "report.json"
    _save_cache(
        cache_path,
        _bars_by_symbol(first, second),
        start_date=first,
        end_date=second,
        retained_minutes_after_open=64,
    )
    monkeypatch.setattr(sys, "argv", [
        "opening_policy_research",
        "--start-date",
        first.isoformat(),
        "--end-date",
        second.isoformat(),
        "--baseline-symbols",
        ",".join(_SYMBOLS),
        "--cache-path",
        str(cache_path),
        "--output",
        str(output_path),
    ])

    assert main() == 0

    stdout = json.loads(capsys.readouterr().out)
    stored = json.loads(output_path.read_text(encoding="utf-8"))
    assert stdout["production"]["policy"]["name"] == (
        PRODUCTION_POLICY_NAME
    )
    assert stored["data_scope"]["resolved_session_count"] == 2
    assert stored["report"]["source_sessions"] == 2
    assert stored["research_design"]["automatic_promotion_allowed"] is False


def test_cli_emits_joint_cohort_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = date(2026, 7, 6)
    second = date(2026, 7, 7)
    cache_path = tmp_path / "opening.json.gz"
    output_path = tmp_path / "cohort-report.json"
    _save_cache(
        cache_path,
        _bars_by_symbol_with_cohort(first, second),
        start_date=first,
        end_date=second,
        retained_minutes_after_open=64,
    )
    monkeypatch.setattr(sys, "argv", [
        "opening_policy_research",
        "--start-date",
        first.isoformat(),
        "--end-date",
        second.isoformat(),
        "--baseline-symbols",
        ",".join(_SYMBOLS),
        "--cohort-symbols",
        _COHORT_SYMBOL,
        "--cache-path",
        str(cache_path),
        "--output",
        str(output_path),
    ])

    assert main() == 0

    stdout = json.loads(capsys.readouterr().out)
    stored = json.loads(output_path.read_text(encoding="utf-8"))
    assert stdout["cohort"]["cohort_symbols"] == [_COHORT_SYMBOL]
    assert stdout["cohort"]["paired_sessions"] == 2
    assert len(stdout["cohort_cost_stress"]) == 3
    assert stored["cohort_diagnostic"]["diagnostic_only"] is True
    assert stored["cohort_diagnostic"][
        "automatic_promotion_allowed"
    ] is False
    assert stored["data_scope"]["cohort_resolved_session_count"] == 2


def test_cli_refuses_to_overwrite_incompatible_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = date(2026, 7, 6)
    second = date(2026, 7, 7)
    cache_path = tmp_path / "opening.json.gz"
    output_path = tmp_path / "report.json"
    _save_cache(
        cache_path,
        _bars_by_symbol(first, second),
        start_date=first,
        end_date=second,
        retained_minutes_after_open=63,
    )
    original_cache = cache_path.read_bytes()
    monkeypatch.setattr(sys, "argv", [
        "opening_policy_research",
        "--start-date",
        first.isoformat(),
        "--end-date",
        second.isoformat(),
        "--baseline-symbols",
        ",".join(_SYMBOLS),
        "--cache-path",
        str(cache_path),
        "--output",
        str(output_path),
    ])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    assert cache_path.read_bytes() == original_cache
    assert not output_path.exists()
