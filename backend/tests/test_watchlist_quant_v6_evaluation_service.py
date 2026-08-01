from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import cache

import pytest

from app.domain.universe_selection import (
    INDEX_MEMBERSHIP_HISTORY,
    ROTATION_RESEARCH_CANDIDATE_CATALOG,
)
from app.domain.universe_selection.membership_history import MembershipInterval
from app.domain.watchlist_quant_v6 import (
    QUANT_V6_ACQUISITION_SPEC_DIGEST,
    QuantV6Bar,
    quant_v6_expected_rth_bar_starts,
)
from app.services import watchlist_quant_v6_evaluation_service as service_module
from app.services.watchlist_quant_v6_evaluation_service import (
    ASSESSMENT_ROLE,
    EVENT_ROLE,
    SESSION_INPUT_ROLE,
    QuantV6HistoricalEvaluationError,
    _build_registration_plan,
    build_latest_quant_v6_registration_plan,
    evaluate_quant_v6_candidate,
    quant_v6_historical_evaluator_digest_sha256,
    quant_v6_registration_acquisition_spec,
    validate_quant_v6_registration_plan,
)
from app.services.watchlist_quant_v6_historical_provider import (
    QuantV6HistoricalBarFetch,
)


class _Provider:
    def __init__(self, bars: tuple[QuantV6Bar, ...]) -> None:
        self.bars = bars
        self.calls: list[tuple[str, datetime, datetime]] = []

    def fetch_five_minute_no_adjust(
        self,
        symbol: str,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> QuantV6HistoricalBarFetch:
        self.calls.append((symbol, start_at, end_at))
        return QuantV6HistoricalBarFetch(
            bars=self.bars,
            pages=4,
            raw_rows=len(self.bars),
            rejected_rows=0,
        )


def _bar(start_at: datetime, index: int) -> QuantV6Bar:
    closed = Decimal("100") + Decimal(index) / Decimal("100")
    return QuantV6Bar(
        start_at=start_at,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=closed,
        volume=Decimal("1000"),
    )


def _all_bars(plan) -> tuple[QuantV6Bar, ...]:
    values: list[QuantV6Bar] = []
    for session_date in (*plan.training_session_dates, *plan.target_session_dates):
        values.extend(
            _bar(start, index)
            for index, start in enumerate(
                quant_v6_expected_rth_bar_starts("US", session_date)
            )
        )
    return tuple(values)


@cache
def _one_member_plan():
    observed = datetime(2026, 7, 31, 23, 0, tzinfo=timezone.utc)
    full_plan = build_latest_quant_v6_registration_plan(observed_at=observed)
    first = next(
        candidate
        for candidate in ROTATION_RESEARCH_CANDIDATE_CATALOG
        if candidate.symbol == full_plan.members[0].symbol
    )
    return _build_registration_plan(
        observed_at=observed,
        market="US",
        candidates=(first,),
        membership_history=INDEX_MEMBERSHIP_HISTORY,
    )


def test_registration_is_server_owned_pit_fixed_30_plus_10() -> None:
    plan = build_latest_quant_v6_registration_plan(
        observed_at=datetime(2026, 7, 31, 23, 0, tzinfo=timezone.utc),
    )
    assert len(plan.training_session_dates) == 10
    assert len(plan.target_session_dates) == 30
    assert plan.training_session_dates[-1] < plan.target_session_dates[0]
    assert [member.symbol for member in plan.members] == sorted(
        member.symbol for member in plan.members
    )
    assert all(
        INDEX_MEMBERSHIP_HISTORY.is_active(
            candidate,
            plan.target_session_dates[0],
        )
        for candidate in ROTATION_RESEARCH_CANDIDATE_CATALOG
        if candidate.symbol in {member.symbol for member in plan.members}
    )
    assert len(plan.identity_sha256) == 64
    assert plan.registration_json.startswith("{")


def test_registration_identity_is_stable_for_same_completed_window() -> None:
    first = build_latest_quant_v6_registration_plan(
        observed_at=datetime(2026, 7, 31, 23, 0, tzinfo=timezone.utc),
    )
    retried = build_latest_quant_v6_registration_plan(
        observed_at=datetime(2026, 7, 31, 23, 30, tzinfo=timezone.utc),
    )

    assert retried.identity_sha256 == first.identity_sha256
    assert retried.registration_json == first.registration_json
    assert retried.cohort_observed_at == first.cohort_observed_at


def test_acquisition_and_runtime_source_are_bound_to_registration() -> None:
    acquisition = quant_v6_registration_acquisition_spec()
    assert acquisition["domain_acquisition_spec_sha256"] == (
        QUANT_V6_ACQUISITION_SPEC_DIGEST
    )
    provider_contract = acquisition["provider_contract"]
    assert isinstance(provider_contract, dict)
    assert provider_contract["adjustment_mode"] == "NO_ADJUST"
    assert provider_contract["fallback_allowed"] is False
    assert len(quant_v6_historical_evaluator_digest_sha256()) == 64


def test_membership_interval_data_is_bound_even_when_cohort_is_unchanged() -> None:
    observed = datetime(2026, 7, 31, 23, 0, tzinfo=timezone.utc)
    baseline = _one_member_plan()
    original_djia = INDEX_MEMBERSHIP_HISTORY.intervals["DJIA"]
    original_aapl = original_djia["AAPL"]
    drifted_history = replace(
        INDEX_MEMBERSHIP_HISTORY,
        intervals={
            **INDEX_MEMBERSHIP_HISTORY.intervals,
            "DJIA": {
                **original_djia,
                "AAPL": (
                    MembershipInterval(
                        start=date(1900, 1, 1),
                        end=date(1900, 1, 2),
                    ),
                    *original_aapl,
                ),
            },
        },
    )
    selected = next(
        candidate
        for candidate in ROTATION_RESEARCH_CANDIDATE_CATALOG
        if candidate.symbol == baseline.members[0].symbol
    )
    drifted = _build_registration_plan(
        observed_at=observed,
        market="US",
        candidates=(selected,),
        membership_history=drifted_history,
    )

    assert drifted.members == baseline.members
    assert drifted.source_snapshot_sha256 != baseline.source_snapshot_sha256
    assert drifted.identity_sha256 != baseline.identity_sha256


def test_complete_zero_event_sessions_still_bind_every_session_input() -> None:
    plan = _one_member_plan()
    provider = _Provider(_all_bars(plan))
    result = evaluate_quant_v6_candidate(
        registration=plan,
        member=plan.members[0],
        provider=provider,
    )
    roles = [binding.role for binding in result.bindings]
    assert result.covered_sessions == 30
    assert result.event_count == 0
    assert roles.count(ASSESSMENT_ROLE) == 1
    assert roles.count(SESSION_INPUT_ROLE) == 30
    assert roles.count(EVENT_ROLE) == 0
    assert len({binding.binding_sha256 for binding in result.bindings}) == 31
    assert len(provider.calls) == 1


def test_one_missing_training_bar_keeps_denominator_and_omits_inputs() -> None:
    plan = _one_member_plan()
    bars = list(_all_bars(plan))
    missing_at = quant_v6_expected_rth_bar_starts(
        "US",
        plan.target_session_dates[0],
    )[0]
    bars = [bar for bar in bars if bar.start_at != missing_at]
    result = evaluate_quant_v6_candidate(
        registration=plan,
        member=plan.members[0],
        provider=_Provider(tuple(bars)),
    )
    assert result.covered_sessions < 30
    assert len([
        binding
        for binding in result.bindings
        if binding.role == SESSION_INPUT_ROLE
    ]) == result.covered_sessions
    assert result.recommended_action == "AVOID"


def test_service_has_no_live_or_current_watchlist_dependency() -> None:
    source = inspect.getsource(service_module)
    forbidden = (
        "WatchlistItem",
        "WatchlistScore",
        "BrokerGateway",
        "get_runner",
        "place_order",
        "submit_order",
        "UniverseSelectionRun",
    )
    assert all(token not in source for token in forbidden)


def test_registration_rejects_naive_observation_time() -> None:
    with pytest.raises(
        QuantV6HistoricalEvaluationError,
        match="timezone-aware",
    ):
        build_latest_quant_v6_registration_plan(
            observed_at=datetime(2026, 7, 31, 23, 0),
        )


def test_registration_tamper_is_rejected_before_provider_call() -> None:
    plan = _one_member_plan()
    provider = _Provider(_all_bars(plan))
    tampered = replace(plan, identity_sha256="f" * 64)
    with pytest.raises(
        QuantV6HistoricalEvaluationError,
        match="identity digest mismatch",
    ):
        evaluate_quant_v6_candidate(
            registration=tampered,
            member=tampered.members[0],
            provider=provider,
        )
    assert provider.calls == []
    validate_quant_v6_registration_plan(plan)


def test_registration_schedule_tamper_is_rejected_before_provider_call() -> None:
    plan = _one_member_plan()
    provider = _Provider(_all_bars(plan))
    tampered = replace(
        plan,
        target_session_dates=(
            plan.target_session_dates[1],
            plan.target_session_dates[0],
            *plan.target_session_dates[2:],
        ),
    )

    with pytest.raises(
        QuantV6HistoricalEvaluationError,
        match="schedule failed canonical replay",
    ):
        evaluate_quant_v6_candidate(
            registration=tampered,
            member=tampered.members[0],
            provider=provider,
        )

    assert provider.calls == []
