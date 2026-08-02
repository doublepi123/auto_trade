from __future__ import annotations

import inspect
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import cache

import pytest

import app.domain.watchlist_quant_v6.assessment as assessment_module
from app.domain.universe_selection import (
    INDEX_MEMBERSHIP_HISTORY,
    ROTATION_RESEARCH_CANDIDATE_CATALOG,
)
from app.domain.universe_selection.membership_history import MembershipInterval
from app.domain.watchlist_quant_v6 import (
    QUANT_V6_ACQUISITION_SPEC_DIGEST,
    BarNextOpenStressedEvent,
    QuantV6Bar,
    QuantV6ThresholdEvidence,
    QuantV6TrainingSession,
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
    evaluate_quant_v6_registration,
    quant_v6_historical_evaluator_digest_sha256,
    quant_v6_historical_evaluator_manifest,
    quant_v6_registration_acquisition_spec,
    validate_quant_v6_registration_plan,
)
from app.services.watchlist_quant_v6_deadline import (
    QuantV6EvaluationCancelledError,
    QuantV6EvaluationDeadline,
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


def test_historical_evaluator_manifest_has_exact_source_closure() -> None:
    manifest = quant_v6_historical_evaluator_manifest()
    source_sha256 = manifest["source_sha256"]

    assert manifest["manifest_version"] == 2
    assert isinstance(source_sha256, dict)
    assert set(source_sha256) == {
        "app.domain.universe_selection.catalog",
        "app.domain.universe_selection.membership_history",
        "app.services.watchlist_quant_v6_deadline",
        "app.services.watchlist_quant_v6_evaluation_service",
        "app.services.watchlist_quant_v6_historical_provider",
    }
    assert all(
        isinstance(digest, str) and len(digest) == 64
        for digest in source_sha256.values()
    )


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


def test_candidate_fused_assessment_runs_one_complete_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _one_member_plan()
    replay_calls = 0
    original = assessment_module._assess_bar_next_open_stressed_window_core

    def _track_replay(**kwargs):
        nonlocal replay_calls
        replay_calls += 1
        return original(**kwargs)

    monkeypatch.setattr(
        assessment_module,
        "_assess_bar_next_open_stressed_window_core",
        _track_replay,
    )

    result = evaluate_quant_v6_candidate(
        registration=plan,
        member=plan.members[0],
        provider=_Provider(()),
    )

    assert result.covered_sessions == 0
    assert replay_calls == 1


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


def test_candidate_checkpoints_each_complete_session_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _one_member_plan()
    deadline = QuantV6EvaluationDeadline(60)
    visited_sessions: list[date] = []
    checkpoint_calls = 0
    sentinel = RuntimeError("complete-session checkpoint sentinel")
    original = service_module._complete_session_bars

    def _track_complete_session(
        fetched_bars: Sequence[QuantV6Bar],
        *,
        market: str,
        session_date: date,
    ) -> tuple[QuantV6Bar, ...] | None:
        visited_sessions.append(session_date)
        return original(
            fetched_bars,
            market=market,
            session_date=session_date,
        )

    def _checkpoint() -> None:
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        if len(visited_sessions) == 2:
            raise sentinel

    monkeypatch.setattr(
        service_module,
        "_complete_session_bars",
        _track_complete_session,
    )
    monkeypatch.setattr(deadline, "checkpoint", _checkpoint)

    with pytest.raises(RuntimeError) as caught:
        evaluate_quant_v6_candidate(
            registration=plan,
            member=plan.members[0],
            provider=_Provider(()),
            evaluation_deadline=deadline,
        )

    assert caught.value is sentinel
    assert visited_sessions == list(
        (*plan.training_session_dates, *plan.target_session_dates)[:2]
    )
    assert checkpoint_calls > 1


def test_candidate_checkpoints_each_training_session_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _one_member_plan()
    deadline = QuantV6EvaluationDeadline(60)
    built_sessions: list[date] = []
    checkpoint_calls = 0
    sentinel = RuntimeError("training-session checkpoint sentinel")
    original = service_module.QuantV6TrainingSession

    def _track_training_session(
        *,
        session_date: date,
        bars: tuple[QuantV6Bar, ...],
    ) -> QuantV6TrainingSession:
        built_sessions.append(session_date)
        return original(session_date=session_date, bars=bars)

    def _checkpoint() -> None:
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        if len(built_sessions) == 2:
            raise sentinel

    monkeypatch.setattr(
        service_module,
        "QuantV6TrainingSession",
        _track_training_session,
    )
    monkeypatch.setattr(deadline, "checkpoint", _checkpoint)

    with pytest.raises(RuntimeError) as caught:
        evaluate_quant_v6_candidate(
            registration=plan,
            member=plan.members[0],
            provider=_Provider(_all_bars(plan)),
            evaluation_deadline=deadline,
        )

    assert caught.value is sentinel
    assert built_sessions == list(plan.training_session_dates[:2])
    assert checkpoint_calls > 1


def test_candidate_observes_cancellation_before_event_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _one_member_plan()
    deadline = QuantV6EvaluationDeadline(60)
    threshold_calls = 0
    event_build_calls = 0
    original = service_module.build_quant_v6_threshold_evidence

    def _cancel_after_threshold(
        *,
        symbol: str,
        market: str,
        target_session_date: date,
        training_sessions: Sequence[QuantV6TrainingSession],
    ) -> QuantV6ThresholdEvidence:
        nonlocal threshold_calls
        threshold_calls += 1
        threshold = original(
            symbol=symbol,
            market=market,
            target_session_date=target_session_date,
            training_sessions=training_sessions,
        )
        deadline.cancel()
        return threshold

    def _track_event_build(
        *,
        symbol: str,
        market: str,
        session_date: date,
        bars: Sequence[QuantV6Bar],
        threshold_evidence: QuantV6ThresholdEvidence,
        fee_rate: Decimal | int | str,
    ) -> tuple[BarNextOpenStressedEvent, ...]:
        nonlocal event_build_calls
        del symbol, market, session_date, bars, threshold_evidence, fee_rate
        event_build_calls += 1
        return ()

    monkeypatch.setattr(
        service_module,
        "build_quant_v6_threshold_evidence",
        _cancel_after_threshold,
    )
    monkeypatch.setattr(
        service_module,
        "build_bar_next_open_stressed_session_events",
        _track_event_build,
    )

    with pytest.raises(QuantV6EvaluationCancelledError):
        evaluate_quant_v6_candidate(
            registration=plan,
            member=plan.members[0],
            provider=_Provider(_all_bars(plan)),
            evaluation_deadline=deadline,
        )

    assert threshold_calls == 1
    assert event_build_calls == 0


def test_registration_cancellation_raises_instead_of_returning_partial_tuple(
) -> None:
    plan = build_latest_quant_v6_registration_plan(
        observed_at=datetime(2026, 7, 31, 23, 0, tzinfo=timezone.utc),
    )
    assert len(plan.members) >= 2
    deadline = QuantV6EvaluationDeadline(60)

    class _CancelOnSecondFetchProvider(_Provider):
        def fetch_five_minute_no_adjust(
            self,
            symbol: str,
            *,
            start_at: datetime,
            end_at: datetime,
        ) -> QuantV6HistoricalBarFetch:
            fetched = super().fetch_five_minute_no_adjust(
                symbol,
                start_at=start_at,
                end_at=end_at,
            )
            if len(self.calls) == 2:
                deadline.cancel()
            return fetched

    provider = _CancelOnSecondFetchProvider(())
    result: object | None = None

    with pytest.raises(QuantV6EvaluationCancelledError):
        result = evaluate_quant_v6_registration(
            registration=plan,
            provider=provider,
            evaluation_deadline=deadline,
        )

    assert result is None
    assert len(provider.calls) == 2


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
