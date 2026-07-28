from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import pytest

import app.cli.opening_policy_research as research_cli
from app.cli.opening_extension_research import (
    RawMinuteBar,
    _load_cache,
    _save_cache,
)
from app.cli.opening_policy_research import (
    _build_policy_sessions,
    _cohort_individual_screen_payload,
    _cohort_subset_selection_payload,
    _default_policy_specs,
    _exclusion_subset_selection_payload,
    main,
)
from app.core.market_calendar import get_session
from app.domain.opening_momentum_policy import (
    EXCEPTIONAL_MAXIMUM_MARKET_RETURN_BPS,
    EXCEPTIONAL_MINIMUM_PATH_EFFICIENCY,
    EXCEPTIONAL_PATH_POLICY_NAME,
    PRODUCTION_MAXIMUM_MARKET_RETURN_BPS,
    PRODUCTION_MINIMUM_PATH_EFFICIENCY,
    PRODUCTION_POLICY_NAME,
    OpeningPolicyCohortReport,
    OpeningPolicySession,
    OpeningPolicySpec,
    evaluate_opening_policy_cohort,
)


_SYMBOLS = tuple(f"S{index}.US" for index in range(8))
_COHORT_SYMBOL = "C0.US"
_UNCOVERED_COHORT_SYMBOL = "C1.US"


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


def _bars(
    symbol: str,
    session_date: date,
    *,
    retained_minutes: int = 64,
) -> tuple[RawMinuteBar, ...]:
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
    for offset in range(retained_minutes + 1):
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
    retained_minutes: int = 64,
) -> dict[str, tuple[RawMinuteBar, ...]]:
    return {
        symbol: tuple(
            bar
            for session_date in session_dates
            for bar in _bars(
                symbol,
                session_date,
                retained_minutes=retained_minutes,
            )
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


def _selection_reports(
    symbol: str,
    *,
    discovery_delta_bps: float,
    holdout_delta_bps: float,
) -> tuple[OpeningPolicyCohortReport, ...]:
    first = date(2026, 1, 2)
    baseline = tuple(
        OpeningPolicySession(
            session_date=first + timedelta(days=index),
            baseline_signal=True,
            gross_return_bps=14.0,
            market_return_bps=-1.0,
            candidate_path_efficiency=0.8,
            candidate_symbol="BASE.US",
        )
        for index in range(10)
    )
    cohort = tuple(
        OpeningPolicySession(
            session_date=first + timedelta(days=index),
            baseline_signal=True,
            gross_return_bps=(
                14.0 + discovery_delta_bps
                if index < 4
                else (
                    14.0 + holdout_delta_bps
                    if index >= 6
                    else 14.0
                )
            ),
            market_return_bps=-1.0,
            candidate_path_efficiency=0.8,
            candidate_symbol=(
                symbol if index < 4 or index >= 6 else "BASE.US"
            ),
        )
        for index in range(10)
    )
    policy = OpeningPolicySpec(
        PRODUCTION_POLICY_NAME,
        minimum_path_efficiency=0.7,
        maximum_market_return_bps=0.0,
    )
    return tuple(
        evaluate_opening_policy_cohort(
            baseline,
            cohort,
            policy=policy,
            cohort_symbols=(symbol,),
            round_trip_cost_bps=cost,
        )
        for cost in (14.0, 20.0, 30.0)
    )


def test_default_grid_contains_unique_production_and_neighbors() -> None:
    policies = _default_policy_specs()
    names = [value.name for value in policies]
    production = next(
        value for value in policies if value.name == PRODUCTION_POLICY_NAME
    )
    exceptional = next(
        value
        for value in policies
        if value.name == EXCEPTIONAL_PATH_POLICY_NAME
    )

    assert len(policies) == 37
    assert len(names) == len(set(names))
    assert policies[0].name == "BROAD"
    assert production.minimum_path_efficiency == (
        PRODUCTION_MINIMUM_PATH_EFFICIENCY
    )
    assert production.maximum_market_return_bps == (
        PRODUCTION_MAXIMUM_MARKET_RETURN_BPS
    )
    assert exceptional.minimum_path_efficiency == (
        PRODUCTION_MINIMUM_PATH_EFFICIENCY
    )
    assert exceptional.maximum_market_return_bps == (
        PRODUCTION_MAXIMUM_MARKET_RETURN_BPS
    )
    assert exceptional.exceptional_minimum_path_efficiency == (
        EXCEPTIONAL_MINIMUM_PATH_EFFICIENCY
    )
    assert exceptional.exceptional_maximum_market_return_bps == (
        EXCEPTIONAL_MAXIMUM_MARKET_RETURN_BPS
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


def test_cohort_subset_selection_uses_discovery_not_holdout() -> None:
    payload = _cohort_subset_selection_payload({
        ("A.US",): _selection_reports(
            "A.US",
            discovery_delta_bps=100.0,
            holdout_delta_bps=-100.0,
        ),
        ("B.US",): _selection_reports(
            "B.US",
            discovery_delta_bps=50.0,
            holdout_delta_bps=500.0,
        ),
    })

    assert payload["selection_uses_holdout"] is False
    assert payload["status"] == "SHADOW_CANDIDATE"
    assert payload["selected_symbols"] == ["A.US"]
    assert payload["eligible_subset_count"] == 2
    selected = cast(dict[str, object], payload["selected"])
    discovery = cast(dict[str, object], selected["discovery"])
    holdout = cast(dict[str, object], selected["holdout_diagnostic"])
    assert discovery["cumulative_delta_bps"] == 400.0
    assert holdout["cumulative_delta_bps"] == -400.0


def test_cohort_individual_screen_uses_discovery_and_keeps_no_coverage() -> None:
    payload = _cohort_individual_screen_payload(
        {
            "A.US": _selection_reports(
                "A.US",
                discovery_delta_bps=100.0,
                holdout_delta_bps=-100.0,
            ),
            "B.US": _selection_reports(
                "B.US",
                discovery_delta_bps=50.0,
                holdout_delta_bps=500.0,
            ),
        },
        coverage_failures={
            "HONA.US": "MISSING_BASELINE_DISCOVERY_COVERAGE",
        },
    )

    assert payload["selection_uses_holdout"] is False
    assert payload["automatic_promotion_allowed"] is False
    assert payload["screened_symbol_count"] == 3
    assert payload["evaluable_symbol_count"] == 2
    assert payload["no_paired_coverage_count"] == 1
    assert payload["eligible_symbol_count"] == 2
    assert payload["discovery_shortlist"] == ["A.US", "B.US"]
    candidates = {
        cast(dict[str, object], value)["symbol"]: value
        for value in cast(list[object], payload["candidates"])
    }
    first = cast(dict[str, object], candidates["A.US"])
    second = cast(dict[str, object], candidates["B.US"])
    uncovered = cast(dict[str, object], candidates["HONA.US"])
    assert first["discovery_rank"] == 1
    assert second["discovery_rank"] == 2
    assert cast(dict[str, object], first["holdout_diagnostic"])[
        "cumulative_delta_bps"
    ] == -400.0
    assert cast(dict[str, object], second["holdout_diagnostic"])[
        "cumulative_delta_bps"
    ] == 2000.0
    assert uncovered["coverage_status"] == "NO_PAIRED_COVERAGE"
    assert uncovered["selection_blockers"] == ["NO_PAIRED_COVERAGE"]


def test_exclusion_subset_selection_uses_explicit_exclusion_keys() -> None:
    first = date(2026, 1, 2)
    baseline = tuple(
        OpeningPolicySession(
            session_date=first + timedelta(days=index),
            baseline_signal=True,
            gross_return_bps=14.0,
            market_return_bps=-1.0,
            candidate_path_efficiency=0.8,
            candidate_symbol="REMOVE.US",
        )
        for index in range(10)
    )
    reduced = tuple(
        OpeningPolicySession(
            session_date=first + timedelta(days=index),
            baseline_signal=True,
            gross_return_bps=(
                114.0 if index < 4 else (-86.0 if index >= 6 else 14.0)
            ),
            market_return_bps=-1.0,
            candidate_path_efficiency=0.8,
            candidate_symbol="NEXT.US",
        )
        for index in range(10)
    )
    policy = OpeningPolicySpec(
        PRODUCTION_POLICY_NAME,
        minimum_path_efficiency=0.7,
        maximum_market_return_bps=0.0,
    )
    reports = tuple(
        evaluate_opening_policy_cohort(
            baseline,
            reduced,
            policy=policy,
            cohort_symbols=("REMOVE.US",),
            round_trip_cost_bps=cost,
        )
        for cost in (14.0, 20.0, 30.0)
    )

    payload = _exclusion_subset_selection_payload({
        ("REMOVE.US",): reports,
    })

    assert payload["selection_uses_holdout"] is False
    assert payload["status"] == "SHADOW_CANDIDATE"
    assert payload["selected_excluded_symbols"] == ["REMOVE.US"]
    assert "selected_symbols" not in payload
    selected = cast(dict[str, object], payload["selected"])
    assert selected["excluded_symbols"] == ["REMOVE.US"]
    assert "symbols" not in selected


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
    assert stored["research_design"]["paired_policy_name"] == (
        PRODUCTION_POLICY_NAME
    )
    assert stored["research_design"]["automatic_promotion_allowed"] is False
    assert stored["research_design"]["chronological_split"] == (
        "BASELINE_DATE_ANCHORED"
    )
    assert stored["research_design"]["discovery_end_date"] == (
        stored["report"]["discovery_end_date"]
    )


def test_cli_extends_seed_cache_with_revision_overlap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = date(2026, 7, 1)
    second = date(2026, 7, 10)
    third = date(2026, 7, 13)
    seed_path = tmp_path / "opening-seed.json.gz"
    cache_path = tmp_path / "opening-target.json.gz"
    output_path = tmp_path / "report.json"
    seed_bars = _bars_by_symbol_with_cohort(first, second)
    seed_bars["EXTRA.US"] = tuple(
        bar
        for session_date in (first, second)
        for bar in _bars("EXTRA.US", session_date)
    )
    _save_cache(
        seed_path,
        seed_bars,
        start_date=first,
        end_date=second,
        retained_minutes_after_open=64,
    )
    calls: list[tuple[str, date, date, int]] = []
    closed: list[bool] = []

    class _FakeBroker:
        def close(self) -> None:
            closed.append(True)

    def fake_fetch(
        provider: object,
        symbol: str,
        *,
        start_date: date,
        end_date: date,
        retained_minutes_after_open: int,
        page_size: int = 1000,
    ) -> tuple[RawMinuteBar, ...]:
        del provider, page_size
        calls.append((
            symbol,
            start_date,
            end_date,
            retained_minutes_after_open,
        ))
        refreshed = list(_bars(
            symbol,
            second,
            retained_minutes=retained_minutes_after_open,
        ))
        if symbol == _COHORT_SYMBOL:
            refreshed[0] = replace(
                refreshed[0],
                open=99.5,
                low=99.5,
            )
        return (
            *refreshed,
            *_bars(
                symbol,
                third,
                retained_minutes=retained_minutes_after_open,
            ),
        )

    monkeypatch.setattr(research_cli, "BrokerGateway", _FakeBroker)
    monkeypatch.setattr(
        research_cli,
        "_configure_longport_environment",
        lambda: None,
    )
    monkeypatch.setattr(research_cli, "_fetch_symbol_bars", fake_fetch)
    monkeypatch.setattr(sys, "argv", [
        "opening_policy_research",
        "--start-date",
        first.isoformat(),
        "--end-date",
        third.isoformat(),
        "--baseline-symbols",
        ",".join(_SYMBOLS),
        "--screen-cohort-symbols",
        _COHORT_SYMBOL,
        "--cache-path",
        str(cache_path),
        "--seed-cache-path",
        str(seed_path),
        "--output",
        str(output_path),
    ])

    assert main() == 0

    stdout = json.loads(capsys.readouterr().out)
    refresh_start = second - timedelta(days=7)
    assert len(calls) == len(_SYMBOLS) + 1
    assert {value[0] for value in calls} == {*_SYMBOLS, _COHORT_SYMBOL}
    assert all(value[1:] == (refresh_start, third, 64) for value in calls)
    assert closed == [True]
    loaded = _load_cache(
        cache_path,
        start_date=first,
        end_date=third,
        retained_minutes_after_open=64,
    )
    assert all(
        len(loaded[symbol]) == 65 * 3
        for symbol in (*_SYMBOLS, _COHORT_SYMBOL)
    )
    assert next(
        bar
        for bar in loaded[_COHORT_SYMBOL]
        if bar.timestamp == _timestamp(second, 0)
    ).open == 99.5
    assert "EXTRA.US" not in loaded
    assert stdout["data_scope"]["cache_update_mode"] == (
        "SEEDED_INCREMENTAL_WITH_OVERLAP"
    )
    assert stdout["data_scope"]["cache_refresh_overlap_days"] == 7
    assert stdout["data_scope"]["seed_cache_path"] == str(seed_path)
    assert stdout["data_scope"]["incremental_fetch_start_date"] == (
        refresh_start.isoformat()
    )


def test_cli_seed_extension_failure_leaves_target_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = date(2026, 7, 6)
    second = date(2026, 7, 7)
    third = date(2026, 7, 8)
    seed_path = tmp_path / "opening-seed.json.gz"
    cache_path = tmp_path / "opening-target.json.gz"
    output_path = tmp_path / "report.json"
    _save_cache(
        seed_path,
        _bars_by_symbol_with_cohort(first, second),
        start_date=first,
        end_date=second,
        retained_minutes_after_open=64,
    )
    original_seed = seed_path.read_bytes()
    calls: list[str] = []
    closed: list[bool] = []

    class _FakeBroker:
        def close(self) -> None:
            closed.append(True)

    def failing_fetch(
        provider: object,
        symbol: str,
        *,
        start_date: date,
        end_date: date,
        retained_minutes_after_open: int,
        page_size: int = 1000,
    ) -> tuple[RawMinuteBar, ...]:
        del provider, start_date, end_date, page_size
        calls.append(symbol)
        if len(calls) == 2:
            raise RuntimeError("synthetic incremental fetch failure")
        return tuple(
            bar
            for session_date in (first, second, third)
            for bar in _bars(
                symbol,
                session_date,
                retained_minutes=retained_minutes_after_open,
            )
        )

    monkeypatch.setattr(research_cli, "BrokerGateway", _FakeBroker)
    monkeypatch.setattr(
        research_cli,
        "_configure_longport_environment",
        lambda: None,
    )
    monkeypatch.setattr(research_cli, "_fetch_symbol_bars", failing_fetch)
    monkeypatch.setattr(sys, "argv", [
        "opening_policy_research",
        "--start-date",
        first.isoformat(),
        "--end-date",
        third.isoformat(),
        "--baseline-symbols",
        ",".join(_SYMBOLS),
        "--screen-cohort-symbols",
        _COHORT_SYMBOL,
        "--cache-path",
        str(cache_path),
        "--seed-cache-path",
        str(seed_path),
        "--output",
        str(output_path),
    ])

    with pytest.raises(RuntimeError, match="synthetic incremental"):
        main()

    assert calls == ["S0.US", "S1.US"]
    assert closed == [True]
    assert seed_path.read_bytes() == original_seed
    assert not cache_path.exists()
    assert not output_path.exists()


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
        "--paired-policy",
        "exceptional",
        "--select-cohort-subset",
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
    assert stdout["cohort"]["discovery_end_date"] == first.isoformat()
    assert len(stdout["cohort_cost_stress"]) == 3
    assert stored["cohort_diagnostic"]["diagnostic_only"] is True
    assert stored["research_design"]["paired_policy_name"] == (
        EXCEPTIONAL_PATH_POLICY_NAME
    )
    assert stored["cohort_diagnostic"]["policy"]["name"] == (
        EXCEPTIONAL_PATH_POLICY_NAME
    )
    assert stored["cohort_diagnostic"]["policy"][
        "exceptional_minimum_path_efficiency"
    ] == EXCEPTIONAL_MINIMUM_PATH_EFFICIENCY
    assert stored["cohort_diagnostic"]["policy"][
        "exceptional_maximum_market_return_bps"
    ] == EXCEPTIONAL_MAXIMUM_MARKET_RETURN_BPS
    assert stored["cohort_diagnostic"][
        "automatic_promotion_allowed"
    ] is False
    assert stored["data_scope"]["cohort_resolved_session_count"] == 2
    subset = stdout["cohort_subset_selection"]
    assert subset["selection_uses_holdout"] is False
    assert subset["evaluated_subset_count"] == 1
    assert subset["status"] == "REJECTED"
    assert stored["data_scope"]["cohort_subset_evaluated_count"] == 1


def test_cli_emits_full_catalog_individual_screen_with_coverage_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = date(2026, 7, 6)
    second = date(2026, 7, 7)
    cache_path = tmp_path / "opening.json.gz"
    output_path = tmp_path / "individual-screen-report.json"
    bars_by_symbol = _bars_by_symbol_with_cohort(first, second)
    bars_by_symbol[_UNCOVERED_COHORT_SYMBOL] = _bars(
        _UNCOVERED_COHORT_SYMBOL,
        second,
    )
    _save_cache(
        cache_path,
        bars_by_symbol,
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
        "--screen-cohort-symbols",
        f"{_COHORT_SYMBOL},{_UNCOVERED_COHORT_SYMBOL}",
        "--paired-policy",
        "exceptional",
        "--cache-path",
        str(cache_path),
        "--output",
        str(output_path),
    ])

    assert main() == 0

    stdout = json.loads(capsys.readouterr().out)
    stored = json.loads(output_path.read_text(encoding="utf-8"))
    screen = stdout["cohort_individual_screen"]
    assert screen["selection_uses_holdout"] is False
    assert screen["automatic_promotion_allowed"] is False
    assert screen["screened_symbol_count"] == 2
    assert screen["evaluable_symbol_count"] == 1
    assert screen["no_paired_coverage_count"] == 1
    candidates = {
        value["symbol"]: value for value in screen["candidates"]
    }
    assert candidates[_COHORT_SYMBOL]["coverage_status"] == (
        "PAIRED_DISCOVERY_AND_HOLDOUT"
    )
    assert candidates[_UNCOVERED_COHORT_SYMBOL]["coverage_status"] == (
        "NO_PAIRED_COVERAGE"
    )
    assert candidates[_UNCOVERED_COHORT_SYMBOL]["coverage_reason"] == (
        "FEWER_THAN_TWO_PAIRED_SESSIONS"
    )
    assert stored["cohort_individual_screen_version"] == (
        screen["screen_version"]
    )
    assert stored["research_design"][
        "cohort_individual_screen_uses_holdout"
    ] is False
    assert stored["data_scope"][
        "cohort_individual_screen_evaluable_count"
    ] == 1
    assert "cohort" not in stdout


def test_cli_emits_joint_exclusion_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = date(2026, 7, 6)
    second = date(2026, 7, 7)
    cache_path = tmp_path / "opening.json.gz"
    output_path = tmp_path / "exclusion-report.json"
    baseline_symbols = (*_SYMBOLS, _COHORT_SYMBOL)
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
        ",".join(baseline_symbols),
        "--exclusion-symbols",
        _COHORT_SYMBOL,
        "--select-exclusion-subset",
        "--cache-path",
        str(cache_path),
        "--output",
        str(output_path),
    ])

    assert main() == 0

    stdout = json.loads(capsys.readouterr().out)
    stored = json.loads(output_path.read_text(encoding="utf-8"))
    assert stdout["exclusion"]["excluded_symbols"] == [_COHORT_SYMBOL]
    assert stdout["exclusion"]["paired_sessions"] == 2
    assert stdout["exclusion"]["discovery_end_date"] == first.isoformat()
    assert len(stdout["exclusion_cost_stress"]) == 3
    subset = stdout["exclusion_subset_selection"]
    assert subset["selection_uses_holdout"] is False
    assert subset["evaluated_subset_count"] == 1
    assert stored["exclusion_diagnostic"]["comparison_mode"] == (
        "BASELINE_MINUS_EXCLUSIONS"
    )
    assert stored["exclusion_diagnostic"]["diagnostic_only"] is True
    assert stored["data_scope"]["exclusion_resolved_session_count"] == 2
    assert stored["data_scope"]["exclusion_subset_evaluated_count"] == 1
    assert stored["research_design"][
        "exclusion_subset_selection_uses_holdout"
    ] is False


def test_cli_emits_paired_holding_horizon_rejections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = date(2026, 7, 6)
    second = date(2026, 7, 7)
    cache_path = tmp_path / "opening.json.gz"
    output_path = tmp_path / "horizon-report.json"
    _save_cache(
        cache_path,
        _bars_by_symbol(
            first,
            second,
            retained_minutes=124,
        ),
        start_date=first,
        end_date=second,
        retained_minutes_after_open=124,
    )
    monkeypatch.setattr(sys, "argv", [
        "opening_policy_research",
        "--start-date",
        first.isoformat(),
        "--end-date",
        second.isoformat(),
        "--baseline-symbols",
        ",".join(_SYMBOLS),
        "--holding-horizons",
        "90,120",
        "--cache-path",
        str(cache_path),
        "--output",
        str(output_path),
    ])

    assert main() == 0

    stdout = json.loads(capsys.readouterr().out)
    stored = json.loads(output_path.read_text(encoding="utf-8"))
    horizon = stdout["holding_horizons"]
    assert horizon["baseline_holding_minutes"] == 60
    assert horizon["paired_sessions"] == 2
    assert horizon["discovery_end_date"] == first.isoformat()
    assert [value["holding_minutes"] for value in horizon["results"]] == [
        90,
        120,
    ]
    assert all(
        value["status"] == "REJECTED"
        for value in horizon["results"]
    )
    assert len(stdout["holding_horizon_cost_stress"]) == 3
    assert stored["holding_horizon_diagnostic"]["diagnostic_only"] is True
    assert stored["holding_horizon_diagnostic"][
        "automatic_promotion_allowed"
    ] is False
    assert stored["holding_horizon_decisions"] == horizon
    assert stored["data_scope"][
        "holding_horizon_paired_session_count"
    ] == 2
    assert stored["research_design"][
        "holding_horizon_selection_allowed"
    ] is False


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
