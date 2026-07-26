from __future__ import annotations

from datetime import date, timedelta
from typing import cast

import pytest

from app.domain.opening_momentum import (
    OpeningMomentumConfig,
    OpeningMomentumObservation,
)
from app.domain.opening_momentum_extension import (
    OPENING_EXTENSION_RESEARCH_VERSION,
    OpeningExtensionCandidateReport,
    OpeningExtensionExitPrice,
    OpeningExtensionSession,
    OpeningExtensionSlice,
    evaluate_opening_extension_candidates,
)


_BASELINE = ("AAA.US", "BBB.US")


def _observation(
    symbol: str,
    opening_return_bps: float,
    *,
    entry_open: float | None = 100.0,
) -> OpeningMomentumObservation:
    return OpeningMomentumObservation(
        symbol=symbol,
        session_open=100.0,
        signal_close=100.0 * (1 + opening_return_bps / 10_000),
        entry_open=entry_open,
    )


def _session(
    offset: int,
    *,
    extension_return_bps: float = 120.0,
    baseline_exit: float = 100.20,
    extension_exit: float = 101.00,
    include_extension: bool = True,
    extension_entry: float | None = 100.0,
    include_extension_exit: bool = True,
) -> OpeningExtensionSession:
    observations = [
        _observation("AAA.US", 10.0),
        _observation("BBB.US", 80.0),
    ]
    exits = [
        OpeningExtensionExitPrice("AAA.US", 100.0),
        OpeningExtensionExitPrice("BBB.US", baseline_exit),
    ]
    if include_extension:
        observations.append(_observation(
            "EXT.US",
            extension_return_bps,
            entry_open=extension_entry,
        ))
    if include_extension_exit:
        exits.append(OpeningExtensionExitPrice(
            "EXT.US",
            extension_exit,
        ))
    return OpeningExtensionSession(
        session_date=date(2026, 1, 2) + timedelta(days=offset),
        observations=tuple(observations),
        exit_prices=tuple(exits),
    )


def _config(
    *,
    minimum_candidate_return_bps: float = 50.0,
) -> OpeningMomentumConfig:
    return OpeningMomentumConfig(
        minimum_universe_size=2,
        minimum_market_return_bps=-50.0,
        minimum_candidate_return_bps=(
            minimum_candidate_return_bps
        ),
        minimum_excess_return_bps=25.0,
        one_side_fee_rate=0.0005,
        one_side_slippage_bps=2.0,
    )


def _slice(
    report: OpeningExtensionCandidateReport,
    name: str,
) -> OpeningExtensionSlice:
    return next(
        item
        for item in report.slices
        if item.name == name
    )


def test_extension_report_uses_chronological_holdout_and_paired_returns(
) -> None:
    sessions = tuple(_session(index) for index in range(10))

    result = evaluate_opening_extension_candidates(
        sessions,
        baseline_symbols=_BASELINE,
        extension_symbols=("EXT.US",),
        config=_config(),
    )

    candidate = result.candidates[0]
    discovery = _slice(candidate, "DISCOVERY")
    holdout = _slice(candidate, "HOLDOUT")
    assert result.algorithm_version == OPENING_EXTENSION_RESEARCH_VERSION
    assert result.discovery_sessions == 6
    assert result.holdout_sessions == 4
    assert candidate.comparable_sessions == 10
    assert candidate.displaced_baseline_sessions == 10
    assert candidate.candidate_signal_sessions == 10
    assert discovery.start_date == sessions[0].session_date
    assert discovery.end_date == sessions[5].session_date
    assert holdout.start_date == sessions[6].session_date
    assert holdout.end_date == sessions[9].session_date
    assert discovery.displaced_baseline_sessions == 6
    assert discovery.extension_signal_sessions == 6
    assert holdout.displaced_baseline_sessions == 4
    assert holdout.extension_signal_sessions == 4
    assert holdout.baseline.cumulative_return_bps == pytest.approx(24.0)
    assert holdout.challenger.cumulative_return_bps == pytest.approx(344.0)
    assert holdout.comparison.cumulative_delta_bps == pytest.approx(320.0)
    assert result.automatic_promotion_allowed is False


def test_missing_required_candidate_data_is_excluded_after_fixed_split(
) -> None:
    sessions = tuple(
        _session(index, include_extension=index != 8)
        for index in range(10)
    )

    result = evaluate_opening_extension_candidates(
        sessions,
        baseline_symbols=_BASELINE,
        extension_symbols=("EXT.US",),
        config=_config(),
    )

    candidate = result.candidates[0]
    discovery = _slice(candidate, "DISCOVERY")
    holdout = _slice(candidate, "HOLDOUT")
    assert result.discovery_sessions == 6
    assert result.holdout_sessions == 4
    assert candidate.comparable_sessions == 9
    assert discovery.resolved_sessions == 6
    assert holdout.resolved_sessions == 3


@pytest.mark.parametrize(
    "session",
    [
        _session(8, extension_entry=None),
        _session(8, include_extension_exit=False),
    ],
)
def test_missing_entry_or_exit_is_not_counted_as_zero_return(
    session: OpeningExtensionSession,
) -> None:
    sessions = tuple(
        session if index == 8 else _session(index)
        for index in range(10)
    )

    result = evaluate_opening_extension_candidates(
        sessions,
        baseline_symbols=_BASELINE,
        extension_symbols=("EXT.US",),
        config=_config(),
    )

    holdout = _slice(result.candidates[0], "HOLDOUT")
    assert holdout.resolved_sessions == 3


def test_legitimate_gate_skip_is_a_resolved_zero_return_session() -> None:
    sessions = tuple(
        _session(index, extension_return_bps=40.0)
        for index in range(10)
    )

    result = evaluate_opening_extension_candidates(
        sessions,
        baseline_symbols=_BASELINE,
        extension_symbols=("EXT.US",),
        config=_config(minimum_candidate_return_bps=200.0),
    )

    holdout = _slice(result.candidates[0], "HOLDOUT")
    assert holdout.resolved_sessions == 4
    assert holdout.baseline.signals == 0
    assert holdout.challenger.signals == 0
    assert holdout.comparison.mean_delta_bps == 0.0


def test_cost_stress_charges_only_the_policy_that_signals() -> None:
    sessions = tuple(
        OpeningExtensionSession(
            session_date=date(2026, 1, 2) + timedelta(days=index),
            observations=(
                _observation("AAA.US", 10.0),
                _observation("BBB.US", 40.0),
                _observation("EXT.US", 100.0),
            ),
            exit_prices=(
                OpeningExtensionExitPrice("AAA.US", 100.0),
                OpeningExtensionExitPrice("BBB.US", 100.0),
                OpeningExtensionExitPrice("EXT.US", 101.0),
            ),
        )
        for index in range(10)
    )

    result = evaluate_opening_extension_candidates(
        sessions,
        baseline_symbols=_BASELINE,
        extension_symbols=("EXT.US",),
        config=_config(),
    )

    stress = result.candidates[0].cost_stress
    assert [item.round_trip_cost_bps for item in stress] == [
        14.0,
        20.0,
        30.0,
    ]
    assert stress[0].cumulative_delta_bps > stress[-1].cumulative_delta_bps
    assert stress[0].baseline_cumulative_return_bps == 0.0


def test_tail_dependency_and_json_payload_are_reported() -> None:
    sessions = tuple(
            _session(
                index,
                extension_exit=110.0 if index == 9 else 100.10,
            )
        for index in range(10)
    )

    result = evaluate_opening_extension_candidates(
        sessions,
        baseline_symbols=_BASELINE,
        extension_symbols=("EXT.US",),
        config=_config(),
    )
    payload = result.to_dict()
    holdout = _slice(result.candidates[0], "HOLDOUT")
    candidate_payloads = cast(
        list[dict[str, object]],
        payload["candidates"],
    )
    slice_payloads = cast(
        list[dict[str, object]],
        candidate_payloads[0]["slices"],
    )

    assert holdout.challenger.cumulative_return_bps > 0
    assert holdout.challenger.cumulative_without_best_3_bps <= 0
    assert payload["automatic_promotion_allowed"] is False
    assert slice_payloads[0]["start_date"] == (
        sessions[0].session_date.isoformat()
    )


def test_extension_research_rejects_invalid_inputs() -> None:
    sessions = (_session(0), _session(1))
    with pytest.raises(ValueError, match="already exist"):
        evaluate_opening_extension_candidates(
            sessions,
            baseline_symbols=_BASELINE,
            extension_symbols=("AAA.US",),
            config=_config(),
        )
    with pytest.raises(ValueError, match="unique"):
        evaluate_opening_extension_candidates(
            (sessions[0], sessions[0]),
            baseline_symbols=_BASELINE,
            extension_symbols=("EXT.US",),
            config=_config(),
        )
    with pytest.raises(ValueError, match="cost scenarios"):
        evaluate_opening_extension_candidates(
            sessions,
            baseline_symbols=_BASELINE,
            extension_symbols=("EXT.US",),
            config=_config(),
            round_trip_cost_scenarios_bps=(14.0, 14.0),
        )
