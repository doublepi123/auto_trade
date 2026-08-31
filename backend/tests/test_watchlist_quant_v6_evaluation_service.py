from __future__ import annotations

import inspect
import logging
import threading
import time
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import cache
from types import SimpleNamespace
from typing import Any

import pytest

import app.domain.watchlist_quant_v6.assessment as assessment_module
import app.services.watchlist_quant_v6_historical_provider as provider_module
from app.domain.universe_selection import (
    INDEX_MEMBERSHIP_HISTORY,
    ROTATION_RESEARCH_CANDIDATE_CATALOG,
)
from app.domain.universe_selection.membership_history import MembershipInterval
from app.domain.watchlist_quant_v6 import (
    QUANT_V6_ACQUISITION_SPEC_DIGEST,
    BarNextOpenStressedEvent,
    QuantV6Bar,
    QuantV6SessionLeaf,
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
    QuantV6EvaluationDeadlineExceededError,
)
from app.services.watchlist_quant_v6_historical_provider import (
    QuantV6HistoricalBarProvider,
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


@cache
def _two_member_plan():
    observed = datetime(2026, 7, 31, 23, 0, tzinfo=timezone.utc)
    full_plan = build_latest_quant_v6_registration_plan(observed_at=observed)
    selected_symbols = {
        member.symbol for member in full_plan.members[:2]
    }
    selected = tuple(
        candidate
        for candidate in ROTATION_RESEARCH_CANDIDATE_CATALOG
        if candidate.symbol in selected_symbols
    )
    return _build_registration_plan(
        observed_at=observed,
        market="US",
        candidates=selected,
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

    assert manifest["manifest_version"] == 3
    assert isinstance(source_sha256, dict)
    assert set(source_sha256) == {
        "app.domain.universe_selection.catalog",
        "app.domain.universe_selection.membership_history",
        "app.services.watchlist_quant_v6_deadline",
        "app.services.watchlist_quant_v6_evaluation_service",
        "app.services.watchlist_quant_v6_historical_provider",
        "app.services.watchlist_quant_v6_publication_service",
        "app.services.watchlist_quant_v6_spawn_supervisor",
    }
    assert all(
        isinstance(digest, str) and len(digest) == 64
        for digest in source_sha256.values()
    )


def test_historical_evaluator_manifest_golden_digest() -> None:
    assert quant_v6_historical_evaluator_digest_sha256() == (
        "31fd7466dd5db58a493c1506d663c5c220d4d0aeb3ec48897966da28257de619"
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


def test_truncated_history_yields_missing_target_sessions_not_failure() -> None:
    # A halted symbol can end its exchange history before the frozen window
    # closes.  The evaluator must turn the truncated acquisition into honest
    # SESSION_MISSING leaves instead of failing the whole registration, so a
    # single halted member cannot deadlock the publication retry loop.
    plan = _one_member_plan()
    halted_target = plan.target_session_dates[-1]
    cutoff = quant_v6_expected_rth_bar_starts(plan.market, halted_target)[0]
    truncated = tuple(
        bar for bar in _all_bars(plan) if bar.start_at < cutoff
    )

    result = evaluate_quant_v6_candidate(
        registration=plan,
        member=plan.members[0],
        provider=_Provider(truncated),
    )

    assert result.covered_sessions == len(plan.target_session_dates) - 1
    roles = [binding.role for binding in result.bindings]
    assert roles.count(ASSESSMENT_ROLE) == 1
    # Session input artifacts are bound only for covered sessions, so the
    # halted day appears as MISSING coverage evidence rather than an artifact.
    assert roles.count(SESSION_INPUT_ROLE) == result.covered_sessions


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


def test_candidate_fused_child_artifacts_match_without_public_encoders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _one_member_plan()
    bars = list(_all_bars(plan))
    target_starts = quant_v6_expected_rth_bar_starts(
        plan.market,
        plan.target_session_dates[0],
    )
    by_start = {bar.start_at: index for index, bar in enumerate(bars)}
    signal_at = target_starts[1]
    exit_at = target_starts[8]
    bars[by_start[signal_at]] = QuantV6Bar(
        start_at=signal_at,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("89"),
        close=Decimal("90"),
        volume=Decimal("1000"),
    )
    bars[by_start[exit_at]] = QuantV6Bar(
        start_at=exit_at,
        open=Decimal("102"),
        high=Decimal("103"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1000"),
    )
    frozen_bars = tuple(bars)
    baseline = evaluate_quant_v6_candidate(
        registration=plan,
        member=plan.members[0],
        provider=_Provider(frozen_bars),
    )
    assert baseline.event_count > 0

    def _reject_session_encoder(
        _leaf: QuantV6SessionLeaf,
        *,
        symbol: str,
        market: str,
        checkpoint: Any = None,
    ) -> Any:
        del symbol, market, checkpoint
        raise AssertionError("evaluation called the public session encoder")

    def _reject_event_encoder(
        _event: BarNextOpenStressedEvent,
    ) -> Any:
        raise AssertionError("evaluation called the public event encoder")

    monkeypatch.setattr(
        service_module.QuantV6SessionLeaf,
        "encoded_replay_input",
        _reject_session_encoder,
    )
    monkeypatch.setattr(
        BarNextOpenStressedEvent,
        "encoded_artifact",
        _reject_event_encoder,
    )

    fused = evaluate_quant_v6_candidate(
        registration=plan,
        member=plan.members[0],
        provider=_Provider(frozen_bars),
    )

    assert fused == baseline
    assert [
        (binding.role, binding.artifact_ordinal, binding.session_date)
        for binding in fused.bindings
    ] == [
        (binding.role, binding.artifact_ordinal, binding.session_date)
        for binding in baseline.bindings
    ]


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


def test_registration_lookahead_overlaps_next_fetch_with_current_compute(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    plan = _two_member_plan()
    assert len(plan.members) == 2
    deadline = QuantV6EvaluationDeadline(120)
    second_fetch_started = threading.Event()
    active_lock = threading.Lock()
    active_fetches = 0
    max_active_fetches = 0

    class _TrackingProvider(_Provider):
        def fetch_five_minute_no_adjust(
            self,
            symbol: str,
            *,
            start_at: datetime,
            end_at: datetime,
        ) -> QuantV6HistoricalBarFetch:
            nonlocal active_fetches, max_active_fetches
            with active_lock:
                active_fetches += 1
                max_active_fetches = max(max_active_fetches, active_fetches)
            try:
                if symbol == plan.members[1].symbol:
                    second_fetch_started.set()
                return super().fetch_five_minute_no_adjust(
                    symbol,
                    start_at=start_at,
                    end_at=end_at,
                )
            finally:
                with active_lock:
                    active_fetches -= 1

    original_compute = service_module._evaluate_candidate_from_fetch

    def _require_lookahead_before_first_compute_completes(**kwargs):
        completed_fetch = kwargs["completed_fetch"]
        if completed_fetch.request.member.ordinal == 0:
            assert second_fetch_started.wait(2)
        return original_compute(**kwargs)

    monkeypatch.setattr(
        service_module,
        "_evaluate_candidate_from_fetch",
        _require_lookahead_before_first_compute_completes,
    )
    provider = _TrackingProvider(())
    with caplog.at_level(
        logging.INFO,
        logger="auto_trade.watchlist_quant_v6_evaluation_service",
    ):
        pipelined = evaluate_quant_v6_registration(
            registration=plan,
            provider=provider,
            evaluation_deadline=deadline,
        )

    sequential = evaluate_quant_v6_registration(
        registration=plan,
        provider=_Provider(()),
    )

    assert pipelined == sequential
    assert [item.member.ordinal for item in pipelined] == [0, 1]
    assert [call[0] for call in provider.calls] == [
        member.symbol for member in plan.members
    ]
    assert len({(call[1], call[2]) for call in provider.calls}) == 1
    assert max_active_fetches == 1
    assert not any(
        thread.name.startswith("quant-v6-prefetch")
        for thread in threading.enumerate()
    )
    messages = [record.getMessage() for record in caplog.records]
    first_complete = next(
        index for index, message in enumerate(messages)
        if "evaluation completed ordinal=0" in message
    )
    second_start = next(
        index for index, message in enumerate(messages)
        if "fetch started ordinal=1" in message
    )
    assert second_start < first_complete
    assert all(member.symbol not in " ".join(messages) for member in plan.members)
    assert any(
        "completed_count=2" in message
        and "fetch_ms=" in message
        and "compute_ms=" in message
        and "pages=" in message
        and "rows=" in message
        and "bars=" in message
        and "events=" in message
        for message in messages
    )


def test_registration_lookahead_propagates_provider_timeout_once() -> None:
    plan = _one_member_plan()
    deadline = QuantV6EvaluationDeadline(60)
    sentinel = TimeoutError("provider timeout sentinel")

    class _TimeoutProvider:
        calls = 0

        def fetch_five_minute_no_adjust(
            self,
            symbol: str,
            *,
            start_at: datetime,
            end_at: datetime,
        ) -> QuantV6HistoricalBarFetch:
            del symbol, start_at, end_at
            self.calls += 1
            raise sentinel

    provider = _TimeoutProvider()
    with pytest.raises(TimeoutError) as caught:
        evaluate_quant_v6_registration(
            registration=plan,
            provider=provider,
            evaluation_deadline=deadline,
        )

    assert caught.value is sentinel
    assert provider.calls == 1
    assert deadline.is_stopped() is False
    assert not any(
        thread.name.startswith("quant-v6-prefetch")
        for thread in threading.enumerate()
    )


def test_registration_second_fetch_failure_returns_no_partial_tuple() -> None:
    plan = _two_member_plan()
    deadline = QuantV6EvaluationDeadline(60)
    sentinel = RuntimeError("second provider fetch sentinel")

    class _SecondFailureProvider(_Provider):
        def fetch_five_minute_no_adjust(
            self,
            symbol: str,
            *,
            start_at: datetime,
            end_at: datetime,
        ) -> QuantV6HistoricalBarFetch:
            if symbol == plan.members[1].symbol:
                self.calls.append((symbol, start_at, end_at))
                raise sentinel
            return super().fetch_five_minute_no_adjust(
                symbol,
                start_at=start_at,
                end_at=end_at,
            )

    provider = _SecondFailureProvider(())
    result: object | None = None
    with pytest.raises(RuntimeError) as caught:
        result = evaluate_quant_v6_registration(
            registration=plan,
            provider=provider,
            evaluation_deadline=deadline,
        )

    assert caught.value is sentinel
    assert result is None
    assert [call[0] for call in provider.calls] == [
        member.symbol for member in plan.members
    ]
    assert deadline.is_stopped() is False
    assert not any(
        thread.name.startswith("quant-v6-prefetch")
        for thread in threading.enumerate()
    )


def test_single_member_compute_failure_does_not_cancel_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _one_member_plan()
    deadline = QuantV6EvaluationDeadline(60)
    sentinel = RuntimeError("single candidate compute sentinel")

    def _fail_compute(**_kwargs):
        raise sentinel

    monkeypatch.setattr(
        service_module,
        "_evaluate_candidate_from_fetch",
        _fail_compute,
    )
    with pytest.raises(RuntimeError) as caught:
        evaluate_quant_v6_registration(
            registration=plan,
            provider=_Provider(()),
            evaluation_deadline=deadline,
        )

    assert caught.value is sentinel
    assert deadline.is_stopped() is False
    assert not any(
        thread.name.startswith("quant-v6-prefetch")
        for thread in threading.enumerate()
    )


def test_registration_lookahead_deadline_does_not_return_partial_tuple() -> None:
    plan = _one_member_plan()
    deadline = QuantV6EvaluationDeadline(0.05)
    fetch_started = threading.Event()

    class _DeadlineProvider:
        def fetch_five_minute_no_adjust(
            self,
            symbol: str,
            *,
            start_at: datetime,
            end_at: datetime,
        ) -> QuantV6HistoricalBarFetch:
            del symbol, start_at, end_at
            fetch_started.set()
            while True:
                deadline.checkpoint()
                time.sleep(0.001)

    result: object | None = None
    with pytest.raises(QuantV6EvaluationDeadlineExceededError):
        result = evaluate_quant_v6_registration(
            registration=plan,
            provider=_DeadlineProvider(),
            evaluation_deadline=deadline,
        )

    assert fetch_started.is_set()
    assert result is None
    assert not any(
        thread.name.startswith("quant-v6-prefetch")
        for thread in threading.enumerate()
    )


def test_registration_compute_failure_drains_hanging_provider_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _two_member_plan()
    deadline = QuantV6EvaluationDeadline(60)
    second_page_started = threading.Event()
    release_page = threading.Event()
    page_finished = threading.Event()
    context_closed = threading.Event()
    sentinel = RuntimeError("candidate compute sentinel")

    class Config:
        @staticmethod
        def from_env() -> object:
            return object()

    class Period:
        Min_5 = "MIN_5_ENUM"

    class AdjustType:
        NoAdjust = "NO_ADJUST_ENUM"

    class QuoteContext:
        instances: list[QuoteContext] = []

        def __init__(self, _config: object) -> None:
            self.close_calls = 0
            QuoteContext.instances.append(self)

        def history_candlesticks_by_offset(
            self,
            *args: Any,
        ) -> list[object]:
            symbol = args[0]
            if symbol == plan.members[0].symbol:
                return []
            second_page_started.set()
            try:
                release_page.wait(5)
                return []
            finally:
                page_finished.set()

        def close(self) -> None:
            self.close_calls += 1
            context_closed.set()

    module = SimpleNamespace(
        Config=Config,
        Period=Period,
        AdjustType=AdjustType,
        QuoteContext=QuoteContext,
    )
    monkeypatch.setattr(
        provider_module,
        "_runtime_local_timezone_is_utc",
        lambda: True,
    )
    provider = QuantV6HistoricalBarProvider(
        module_loader=lambda: module,
        evaluation_deadline=deadline,
    )
    original_compute = service_module._evaluate_candidate_from_fetch

    def _fail_first_compute(**kwargs):
        completed_fetch = kwargs["completed_fetch"]
        if completed_fetch.request.member.ordinal == 0:
            assert second_page_started.wait(2)
            raise sentinel
        return original_compute(**kwargs)

    monkeypatch.setattr(
        service_module,
        "_evaluate_candidate_from_fetch",
        _fail_first_compute,
    )

    began_at = time.monotonic()
    try:
        with pytest.raises(RuntimeError) as caught:
            evaluate_quant_v6_registration(
                registration=plan,
                provider=provider,
                evaluation_deadline=deadline,
            )
        assert caught.value is sentinel
        assert time.monotonic() - began_at < 2
        assert deadline.is_stopped()
        assert second_page_started.is_set()
        context = QuoteContext.instances[0]
        provider.close()
        assert context.close_calls == 0
        assert context_closed.is_set() is False
        slot_was_available = provider_module._SDK_CALL_SLOT.acquire(
            blocking=False
        )
        if slot_was_available:
            provider_module._SDK_CALL_SLOT.release()
        assert slot_was_available is False
    finally:
        release_page.set()
        assert page_finished.wait(2)
        assert context_closed.wait(2)
        context = QuoteContext.instances[0]
        assert context.close_calls == 1
        assert provider_module._SDK_CALL_SLOT.acquire(timeout=2)
        provider_module._SDK_CALL_SLOT.release()
        provider.close()
    assert not any(
        thread.name.startswith("quant-v6-prefetch")
        for thread in threading.enumerate()
    )


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
