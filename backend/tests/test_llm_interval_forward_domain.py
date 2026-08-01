from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Literal, cast

import pytest

import app.domain.llm_interval_forward.contract as contract_module
import app.domain.llm_interval_forward.replay as replay_module
from app.domain.llm_interval_forward import (
    BBO_COVERAGE,
    DATA_FIDELITY,
    ENTRY_CROSSING_SEMANTICS,
    FIXED_HORIZON_SESSIONS,
    PERMANENT_LIMITATIONS,
    ForwardBar,
    FrozenExecutionPolicy,
    FrozenIntervalBand,
    IntervalForwardContractError,
    IntervalForwardReplayError,
    PairedSessionLeaf,
    PairedLeafVerificationInput,
    ProposalObservation,
    ReplayRoundTrip,
    absent_session_leaf,
    assess_paired_sessions,
    bars_from_source_artifact_payload,
    canonical_json_bytes,
    canonical_decimal_text,
    canonical_sha256,
    content_sha256,
    counterfactual_policy_without_confidence,
    encode_interval_forward_artifact,
    evaluator_digest_sha256,
    evaluator_manifest,
    fixed_assessment_session_dates,
    freeze_proposal_observation,
    freeze_session_slot,
    full_session_observation_schedule,
    replay_paired_session,
    replay_paired_session_bundle,
    resolve_session_slot,
    select_first_session_proposal,
    source_artifact_payload,
    strict_next_full_session_date,
)


def _d(value: str) -> Decimal:
    return Decimal(value)


def _digest(label: str) -> str:
    return content_sha256(label)


def _policy(
    *,
    symbol: str = "NVDA.US",
    market: Literal["US", "HK"] = "US",
    max_holding_minutes: int = 60,
    flatten_minutes_before_close: int = 15,
) -> FrozenExecutionPolicy:
    return FrozenExecutionPolicy(
        symbol=symbol,
        market=market,
        reference_quantity=_d("10"),
        one_side_fee_rate=_d("0.001"),
        fixed_fee_per_order=_d("0"),
        entry_round_trip_slippage_bps=_d("4"),
        minimum_profit_amount=_d("1"),
        minimum_profit_pct=_d("0"),
        minimum_edge_cost_ratio=_d("2"),
        max_interval_width_pct=_d("8"),
        max_bound_deviation_pct=_d("5"),
        stop_loss_pct=_d("1"),
        trailing_stop_pct=_d("0"),
        max_daily_loss_amount=_d("5000"),
        max_drawdown_amount=_d("500"),
        max_consecutive_losses=3,
        max_entries_per_symbol_per_day=1,
        max_holding_minutes=max_holding_minutes,
        opening_warmup_minutes=0,
        entry_cutoff_minutes_before_close=45,
        flatten_minutes_before_close=flatten_minutes_before_close,
    )


def _proposal(
    *,
    interaction_id: int = 101,
    started_at: datetime = datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc),
    completed_at: datetime = datetime(2026, 7, 31, 14, 1, tzinfo=timezone.utc),
    registered_at: datetime = datetime(2026, 7, 31, 14, 2, tzinfo=timezone.utc),
    confidence: Decimal = Decimal("0.5"),
    baseline_band: FrozenIntervalBand | None = None,
    candidate_band: FrozenIntervalBand | None = None,
    policy: FrozenExecutionPolicy | None = None,
) -> ProposalObservation:
    return freeze_proposal_observation(
        interaction_id=interaction_id,
        analysis_started_at=started_at,
        analysis_completed_at=completed_at,
        registered_at=registered_at,
        confidence=confidence,
        minimum_confidence=_d("0.7"),
        reference_price=_d("102"),
        baseline_band=baseline_band or FrozenIntervalBand(_d("95"), _d("110")),
        raw_proposed_band=candidate_band
        or FrozenIntervalBand(_d("100"), _d("104")),
        execution_policy=policy or _policy(),
        prompt_sha256=_digest("prompt"),
        raw_response_sha256=_digest("raw-response"),
        parsed_response_sha256=_digest("parsed-response"),
        context_sha256=_digest("context"),
        quote_source_sha256=_digest("quote-source"),
        config_sha256=_digest("config"),
        eligibility_snapshot_sha256=_digest("eligibility-snapshot"),
        evaluator_digest_sha256=evaluator_digest_sha256(),
        is_primary=True,
        analysis_started_flat=True,
        registration_flat=True,
        broker_position_zero=True,
        tracked_entry_absent=True,
        pending_order_absent=True,
    )


def _bars(
    proposal: ProposalObservation,
    *,
    candidate_trade: bool = False,
    leave_candidate_open: bool = False,
) -> tuple[ForwardBar, ...]:
    schedule = full_session_observation_schedule(
        proposal.execution_policy.market,
        proposal.target_session_date,
    )
    output: list[ForwardBar] = []
    for index, timestamp in enumerate(schedule):
        opened = _d("105")
        high = _d("105.2")
        low = _d("104.8")
        close = _d("105")
        if candidate_trade and index == 0:
            opened = _d("101")
            high = _d("101.5")
            low = _d("99.5")
            close = _d("100.5")
        elif candidate_trade and index == 1 and not leave_candidate_open:
            opened = _d("103")
            high = _d("104.5")
            low = _d("102.5")
            close = _d("104")
        elif leave_candidate_open and index > 0:
            opened = _d("100.5")
            high = _d("100.8")
            low = _d("99.5")
            close = _d("100.5")
        output.append(ForwardBar(
            timestamp=timestamp,
            observed_at=timestamp + timedelta(seconds=5),
            open=opened,
            high=high,
            low=low,
            close=close,
            volume=_d("1000"),
            source_sha256=_digest(f"bar-{timestamp.isoformat()}-{opened}-{high}-{low}"),
        ))
    return tuple(output)


def _finalized_at(proposal: ProposalObservation) -> datetime:
    schedule = full_session_observation_schedule(
        proposal.execution_policy.market,
        proposal.target_session_date,
    )
    return schedule[-1] + timedelta(hours=1)


def _verification_input(
    proposal: ProposalObservation,
    *,
    bars: tuple[ForwardBar, ...] | None,
) -> PairedLeafVerificationInput:
    slot = freeze_session_slot(
        (proposal,),
        symbol=proposal.execution_policy.symbol,
        target_session_date=proposal.target_session_date,
        occupied_at=proposal.registered_at + timedelta(seconds=1),
    )
    assert slot is not None
    artifact = (
        None
        if bars is None
        else encode_interval_forward_artifact(
            source_artifact_payload(proposal, bars)
        )
    )
    return PairedLeafVerificationInput(
        slot=slot,
        proposal=proposal,
        causal_proposals=(proposal,),
        source_artifact=artifact,
    )


class _LeafComparisonBypass(PairedSessionLeaf):
    def __ne__(self, other: object) -> bool:
        return False


class _FakeVerificationProposal:
    def __init__(self, target_session_date: date) -> None:
        self.target_session_date = target_session_date


class _FakeVerificationInput:
    def __init__(self, target_session_date: date) -> None:
        self.proposal = _FakeVerificationProposal(target_session_date)

    def verify(self, leaf: PairedSessionLeaf) -> PairedSessionLeaf:
        return leaf


class TestFrozenContract:
    def test_policy_normalizes_absolute_amounts_to_virtual_one_share(self) -> None:
        policy = _policy()

        assert policy.per_side_slippage_bps == _d("2")
        assert policy.minimum_profit_per_share == _d("0.1")
        assert policy.max_daily_loss_per_share == _d("500")
        assert policy.max_drawdown_per_share == _d("50")
        assert policy.virtual_quantity == 1
        assert policy.short_entries_allowed is False
        assert policy.position_addons_allowed is False
        assert policy.order_submission_allowed is False
        assert policy.live_config_mutation_allowed is False
        assert policy.automatic_promotion_allowed is False

    @pytest.mark.parametrize(
        "field_name",
        [
            "short_entries_allowed",
            "position_addons_allowed",
            "llm_order_execution_allowed",
            "order_submission_allowed",
            "live_config_mutation_allowed",
            "automatic_promotion_allowed",
        ],
    )
    def test_policy_rejects_every_dangerous_switch(self, field_name: str) -> None:
        policy = _policy()
        with pytest.raises(IntervalForwardContractError, match="must remain false"):
            replace(policy, **{field_name: True})

    def test_policy_requires_one_daily_entry_and_rth(self) -> None:
        with pytest.raises(IntervalForwardContractError, match="exactly one entry"):
            replace(_policy(), max_entries_per_symbol_per_day=2)
        with pytest.raises(IntervalForwardContractError, match="exactly one entry"):
            replace(
                _policy(),
                max_entries_per_symbol_per_day=cast(int, True),
            )
        with pytest.raises(IntervalForwardContractError, match="virtual quantity"):
            replace(
                _policy(),
                virtual_quantity=cast(Literal[1], True),
            )
        with pytest.raises(IntervalForwardContractError, match="RTH_ONLY"):
            replace(
                _policy(),
                trading_session_mode=cast(Literal["RTH_ONLY"], "ANY"),
            )

    def test_counterfactual_rechecks_all_non_confidence_gates(self) -> None:
        policy = _policy()
        allowed = counterfactual_policy_without_confidence(
            reference_price=_d("102"),
            band=FrozenIntervalBand(_d("100"), _d("104")),
            policy=policy,
        )
        narrow = counterfactual_policy_without_confidence(
            reference_price=_d("102"),
            band=FrozenIntervalBand(_d("100"), _d("100.01")),
            policy=policy,
        )
        wide = counterfactual_policy_without_confidence(
            reference_price=_d("102"),
            band=FrozenIntervalBand(_d("96"), _d("106")),
            policy=policy,
        )
        deviating = counterfactual_policy_without_confidence(
            reference_price=_d("102"),
            band=FrozenIntervalBand(_d("96.8"), _d("103")),
            policy=policy,
        )

        assert allowed.allowed is True
        assert allowed.code == "ALLOW_WITHOUT_CONFIDENCE"
        assert narrow.code == "INTERVAL_TOO_NARROW"
        assert wide.code == "INTERVAL_TOO_WIDE"
        assert deviating.code == "INTERVAL_BOUND_DEVIATION"

    def test_only_server_frozen_low_confidence_primary_flat_proposal_is_valid(
        self,
    ) -> None:
        proposal = _proposal()

        assert proposal.origin == "AUTO_CRON"
        assert proposal.reject_code == "LOW_CONFIDENCE"
        assert proposal.is_primary is True
        assert proposal.analysis_started_flat is True
        assert proposal.registration_flat is True
        assert proposal.broker_position_zero is True
        assert proposal.tracked_entry_absent is True
        assert proposal.pending_order_absent is True
        assert proposal.counterfactual_decision.allowed is True
        assert proposal.raw_proposed_band == proposal.effective_candidate_band

    @pytest.mark.parametrize(
        "field_name",
        [
            "is_primary",
            "analysis_started_flat",
            "registration_flat",
            "broker_position_zero",
            "tracked_entry_absent",
            "pending_order_absent",
        ],
    )
    def test_registration_rejects_missing_eligibility_proof(
        self,
        field_name: str,
    ) -> None:
        with pytest.raises(IntervalForwardContractError, match="must remain true"):
            replace(_proposal(), **{field_name: False})

    def test_confidence_boundary_is_strict_and_unrounded(self) -> None:
        with pytest.raises(IntervalForwardContractError, match="below threshold"):
            _proposal(confidence=_d("0.7000000000"))
        assert _proposal(confidence=_d("0.6999999999")).confidence == _d(
            "0.6999999999"
        )

    def test_non_confidence_gate_failure_prevents_registration(self) -> None:
        with pytest.raises(
            IntervalForwardContractError,
            match="does not pass all non-confidence",
        ):
            _proposal(
                candidate_band=FrozenIntervalBand(_d("100"), _d("100.01"))
            )

    def test_registration_digest_is_deterministic_and_tamper_evident(self) -> None:
        first = _proposal()
        second = _proposal()

        assert first.to_payload() == second.to_payload()
        assert len(first.registration_digest_sha256) == 64
        with pytest.raises(IntervalForwardContractError, match="digest mismatch"):
            replace(
                first,
                registration_digest_sha256="0" * 64,
            )

    def test_frozen_objects_are_immutable(self) -> None:
        proposal = _proposal()
        with pytest.raises(FrozenInstanceError):
            setattr(proposal, "confidence", _d("0.1"))

    def test_canonical_json_rejects_float_and_decimal_ambiguity(self) -> None:
        with pytest.raises(IntervalForwardContractError, match="unsupported"):
            canonical_json_bytes({"value": 1.5})
        with pytest.raises(IntervalForwardContractError, match="unsupported"):
            canonical_json_bytes({"value": _d("1.5")})

    def test_decimal_canonicalization_is_exact_and_context_independent(self) -> None:
        first = _d("1.123456789012345678901234567890123456789")
        second = _d("1.123456789012345678901234567890123456788")

        with localcontext() as context:
            context.prec = 5
            first_text = canonical_decimal_text(first)
            second_text = canonical_decimal_text(second)

        assert first_text != second_text
        assert FrozenIntervalBand(first, _d("2")).digest_sha256 != (
            FrozenIntervalBand(second, _d("2")).digest_sha256
        )

    def test_decimal_and_canonical_json_resource_limits_fail_closed(self) -> None:
        with pytest.raises(IntervalForwardContractError, match="exponent"):
            FrozenIntervalBand(_d("1E+1000"), _d("2E+1000"))
        with pytest.raises(IntervalForwardContractError, match="digit"):
            canonical_decimal_text(Decimal("1." + "1" * 65))
        with pytest.raises(IntervalForwardContractError, match="integer"):
            canonical_json_bytes({"value": 10**37})
        with pytest.raises(IntervalForwardContractError, match="string"):
            canonical_json_bytes({"value": "x" * (512 * 1024 + 1)})
        nested: object = None
        for _ in range(34):
            nested = [nested]
        with pytest.raises(IntervalForwardContractError, match="nesting"):
            canonical_json_bytes({"value": nested})
        shared_chunk = "x" * 2048
        with pytest.raises(IntervalForwardContractError, match="byte limit"):
            canonical_json_bytes({"values": [shared_chunk] * 2_000})
        with pytest.raises(IntervalForwardContractError, match="byte limit"):
            canonical_json_bytes({"value": "\x00" * 400_000})

    def test_aggregate_string_budget_fails_before_json_encoding(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _UnexpectedEncoder:
            def __init__(self, **kwargs: object) -> None:
                raise AssertionError("oversized payload reached the JSON encoder")

        monkeypatch.setattr(
            contract_module.json,
            "JSONEncoder",
            _UnexpectedEncoder,
        )
        shared_chunk = "x" * 2048
        with pytest.raises(IntervalForwardContractError, match="byte limit"):
            canonical_json_bytes({"values": [shared_chunk] * 2_000})

    def test_all_evidence_digests_ignore_ambient_decimal_context(self) -> None:
        policy = replace(_policy(), reference_quantity=_d("3"))
        proposal = _proposal(policy=policy)
        leaf = replay_paired_session(
            proposal,
            _bars(proposal, candidate_trade=True),
            finalized_at=_finalized_at(proposal),
        )
        dates = fixed_assessment_session_dates("US", date(2026, 8, 3))
        leaves = _positive_horizon_leaves(dates)
        assessment = replay_module._assess_verified_paired_leaves(
            symbol="NVDA.US",
            expected_session_dates=dates,
            leaves=leaves,
            assessment_cutoff=leaves[-1].finalized_at + timedelta(hours=1),
        )

        with localcontext() as context:
            context.prec = 5
            low_precision_policy = replace(
                _policy(),
                reference_quantity=_d("3"),
            )
            low_precision_proposal = _proposal(policy=low_precision_policy)
            low_precision_leaf = replay_paired_session(
                low_precision_proposal,
                _bars(low_precision_proposal, candidate_trade=True),
                finalized_at=_finalized_at(low_precision_proposal),
            )
            low_precision_assessment = (
                replay_module._assess_verified_paired_leaves(
                    symbol="NVDA.US",
                    expected_session_dates=dates,
                    leaves=leaves,
                    assessment_cutoff=(
                        leaves[-1].finalized_at + timedelta(hours=1)
                    ),
                )
            )

        assert low_precision_policy.digest_sha256 == policy.digest_sha256
        assert (
            low_precision_proposal.registration_digest_sha256
            == proposal.registration_digest_sha256
        )
        assert low_precision_leaf.leaf_digest_sha256 == leaf.leaf_digest_sha256
        assert (
            low_precision_assessment.report_digest_sha256
            == assessment.report_digest_sha256
        )
        for oversized_metric in (
            _d("1000000000000000000.000000001"),
            _d("-1000000000000000000.000000001"),
        ):
            with localcontext() as context:
                context.prec = 5
                with pytest.raises(
                    IntervalForwardReplayError,
                    match="frozen maximum",
                ):
                    replay_module._metric(oversized_metric)

    def test_registration_requires_one_bounded_open_rth_session(self) -> None:
        with pytest.raises(IntervalForwardContractError, match="one open RTH"):
            _proposal(
                started_at=datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc),
                completed_at=datetime(2026, 8, 1, 14, 1, tzinfo=timezone.utc),
                registered_at=datetime(2026, 8, 1, 14, 2, tzinfo=timezone.utc),
            )
        with pytest.raises(IntervalForwardContractError, match="one open RTH"):
            _proposal(
                started_at=datetime(2026, 7, 31, 20, 58, tzinfo=timezone.utc),
                completed_at=datetime(2026, 7, 31, 20, 59, tzinfo=timezone.utc),
                registered_at=datetime(2026, 7, 31, 21, 0, tzinfo=timezone.utc),
            )
        with pytest.raises(IntervalForwardContractError, match="duration"):
            _proposal(
                started_at=datetime(2026, 7, 31, 13, 31, tzinfo=timezone.utc),
                completed_at=datetime(2026, 7, 31, 14, 2, tzinfo=timezone.utc),
                registered_at=datetime(2026, 7, 31, 14, 3, tzinfo=timezone.utc),
            )
        with pytest.raises(IntervalForwardContractError, match="registration"):
            _proposal(
                registered_at=datetime(2026, 7, 31, 14, 7, tzinfo=timezone.utc),
            )

    def test_confidence_only_gate_matches_live_cost_scope(self) -> None:
        zero_fixed = counterfactual_policy_without_confidence(
            reference_price=_d("102"),
            band=FrozenIntervalBand(_d("100"), _d("104")),
            policy=_policy(),
        )
        large_fixed = counterfactual_policy_without_confidence(
            reference_price=_d("102"),
            band=FrozenIntervalBand(_d("100"), _d("104")),
            policy=replace(_policy(), fixed_fee_per_order=_d("1000")),
        )

        assert zero_fixed == large_fixed


class TestCausalSessionSelection:
    def test_strict_next_session_never_uses_same_day_at_exact_open(self) -> None:
        monday_open = datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc)
        assert strict_next_full_session_date("US", monday_open) == date(2026, 8, 4)

    def test_weekend_holiday_hk_lunch_and_dst_are_exchange_aware(self) -> None:
        friday = datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc)
        before_independence_observed = datetime(
            2026,
            7,
            2,
            14,
            0,
            tzinfo=timezone.utc,
        )
        hk_lunch = datetime(2026, 8, 3, 4, 30, tzinfo=timezone.utc)

        assert strict_next_full_session_date("US", friday) == date(2026, 8, 3)
        assert strict_next_full_session_date(
            "US",
            before_independence_observed,
        ) == date(2026, 7, 6)
        assert strict_next_full_session_date("HK", hk_lunch) == date(2026, 8, 4)
        assert full_session_observation_schedule("US", date(2026, 3, 6))[0].hour == 14
        assert full_session_observation_schedule("US", date(2026, 3, 9))[0].hour == 13

    def test_full_session_schedule_handles_us_hk_and_half_day(self) -> None:
        assert len(full_session_observation_schedule("US", date(2026, 8, 3))) == 390
        assert len(full_session_observation_schedule("HK", date(2026, 8, 3))) == 330
        assert len(full_session_observation_schedule("US", date(2026, 11, 27))) == 210

    def test_calendar_coverage_overflow_fails_closed(self) -> None:
        with pytest.raises(IntervalForwardContractError, match="coverage"):
            strict_next_full_session_date(
                "US",
                datetime(2027, 12, 31, 15, 0, tzinfo=timezone.utc),
            )

    def test_selection_is_first_only_and_independent_of_input_order(self) -> None:
        first = _proposal()
        second = _proposal(
            interaction_id=102,
            started_at=datetime(2026, 7, 31, 14, 3, tzinfo=timezone.utc),
            completed_at=datetime(2026, 7, 31, 14, 4, tzinfo=timezone.utc),
            registered_at=datetime(2026, 7, 31, 14, 5, tzinfo=timezone.utc),
            confidence=_d("0.69"),
        )

        selected = select_first_session_proposal(
            [second, first],
            symbol="NVDA.US",
            target_session_date=first.target_session_date,
        )
        assert selected == first

        slot = freeze_session_slot(
            [second, first],
            symbol="NVDA.US",
            target_session_date=first.target_session_date,
            occupied_at=second.registered_at,
        )
        assert slot is not None
        assert resolve_session_slot(slot, [second]) is None
        assert resolve_session_slot(slot, [second, first]) == first
        assert slot.replacement_allowed is False
        assert slot.causal_registration_count == 2

    def test_session_slot_cannot_be_frozen_after_target_open(self) -> None:
        proposal = _proposal()
        with pytest.raises(IntervalForwardContractError, match="before the target open"):
            freeze_session_slot(
                (proposal,),
                symbol=proposal.execution_policy.symbol,
                target_session_date=proposal.target_session_date,
                occupied_at=proposal.target_open_at,
            )

    def test_leaf_verifier_replays_slot_cutoff_root_and_winner(self) -> None:
        first = _proposal(interaction_id=201)
        second = _proposal(
            interaction_id=202,
            completed_at=first.analysis_completed_at + timedelta(minutes=1),
            registered_at=first.registered_at + timedelta(minutes=1),
        )
        occupied_at = second.registered_at + timedelta(seconds=1)
        slot = freeze_session_slot(
            (second, first),
            symbol=first.execution_policy.symbol,
            target_session_date=first.target_session_date,
            occupied_at=occupied_at,
        )
        assert slot is not None
        verification_input = PairedLeafVerificationInput(
            slot=slot,
            proposal=first,
            causal_proposals=(second, first),
            source_artifact=None,
        )
        assert verification_input.proposal == first

        post_cutoff_slot = replace(
            slot,
            occupied_at=first.registered_at - timedelta(seconds=1),
            slot_digest_sha256="",
        )
        with pytest.raises(IntervalForwardReplayError, match="slot cutoff"):
            PairedLeafVerificationInput(
                slot=post_cutoff_slot,
                proposal=first,
                causal_proposals=(first,),
                source_artifact=None,
            )

        forged_root_slot = replace(
            slot,
            causal_registration_set_sha256=_digest("forged-causal-root"),
            slot_digest_sha256="",
        )
        with pytest.raises(IntervalForwardReplayError, match="causal registration"):
            PairedLeafVerificationInput(
                slot=forged_root_slot,
                proposal=first,
                causal_proposals=(second, first),
                source_artifact=None,
            )

        forged_winner_slot = replace(
            slot,
            selected_interaction_id=second.interaction_id,
            selected_registration_digest_sha256=(
                second.registration_digest_sha256
            ),
            slot_digest_sha256="",
        )
        with pytest.raises(IntervalForwardReplayError, match="causal registration"):
            PairedLeafVerificationInput(
                slot=forged_winner_slot,
                proposal=second,
                causal_proposals=(second, first),
                source_artifact=None,
            )


class TestPairedReplay:
    def test_complete_paired_replay_uses_one_common_environment(self) -> None:
        proposal = _proposal()
        leaf = replay_paired_session(
            proposal,
            _bars(proposal, candidate_trade=True),
            finalized_at=_finalized_at(proposal),
        )

        assert leaf.disposition == "INCLUDED"
        assert leaf.baseline is not None
        assert leaf.candidate is not None
        assert leaf.baseline.closed_round_trips == 0
        assert leaf.candidate.closed_round_trips == 1
        assert leaf.delta_net_bps is not None and leaf.delta_net_bps > 0
        assert (
            leaf.baseline.common_environment_sha256
            == leaf.candidate.common_environment_sha256
            == leaf.common_environment_sha256
        )
        assert leaf.baseline.arm_input_sha256 != leaf.candidate.arm_input_sha256
        assert leaf.order_submission_allowed is False
        assert leaf.live_config_mutation_allowed is False
        assert leaf.automatic_promotion_allowed is False
        assert leaf.promotion_eligible is False
        assert leaf.permanent_limitations == PERMANENT_LIMITATIONS

    def test_slippage_is_split_per_fill_and_fees_are_rebuilt(self) -> None:
        proposal = _proposal()
        leaf = replay_paired_session(
            proposal,
            _bars(proposal, candidate_trade=True),
            finalized_at=_finalized_at(proposal),
        )
        candidate = leaf.candidate
        assert candidate is not None
        trade = candidate.round_trips[0]

        assert trade.entry_price == _d("100.02000000")
        assert trade.exit_price == _d("103.97920000")
        assert trade.gross_pnl == _d("3.95920000")
        assert trade.modeled_fees == _d("0.20399920")
        assert trade.net_pnl == _d("3.75520080")
        assert candidate.modeled_fee_bps > 0

    def test_complete_no_trade_day_is_included_as_zero(self) -> None:
        proposal = _proposal()
        leaf = replay_paired_session(
            proposal,
            _bars(proposal),
            finalized_at=_finalized_at(proposal),
        )

        assert leaf.disposition == "INCLUDED"
        assert leaf.baseline is not None and leaf.baseline.net_bps == 0
        assert leaf.candidate is not None and leaf.candidate.net_bps == 0
        assert leaf.delta_net_bps == 0

    def test_missing_or_reordered_minutes_invalidate_the_whole_pair(self) -> None:
        proposal = _proposal()
        bars = _bars(proposal, candidate_trade=True)

        missing = replay_paired_session(
            proposal,
            bars[:-1],
            finalized_at=_finalized_at(proposal),
        )
        reordered = replay_paired_session(
            proposal,
            (bars[1], bars[0], *bars[2:]),
            finalized_at=_finalized_at(proposal),
        )

        assert missing.disposition == "INVALID"
        assert missing.reason == "INCOMPLETE_OBSERVATION_SCHEDULE"
        assert reordered.disposition == "INVALID"
        assert reordered.reason == "OBSERVATION_SCHEDULE_MISMATCH"
        assert missing.baseline is None and missing.candidate is None

    def test_open_final_position_invalidates_both_arms(self) -> None:
        policy = _policy(max_holding_minutes=0, flatten_minutes_before_close=0)
        proposal = _proposal(policy=policy)
        leaf = replay_paired_session(
            proposal,
            _bars(
                proposal,
                candidate_trade=True,
                leave_candidate_open=True,
            ),
            finalized_at=_finalized_at(proposal),
        )

        assert leaf.disposition == "INVALID"
        assert leaf.reason == "FINAL_STATE_NOT_FLAT"
        assert leaf.baseline is None and leaf.candidate is None

    def test_evaluator_drift_fails_closed_before_replay(self) -> None:
        proposal = replace(
            _proposal(),
            evaluator_digest_sha256="0" * 64,
            registration_digest_sha256="",
        )
        leaf = replay_paired_session(
            proposal,
            _bars(proposal),
            finalized_at=_finalized_at(proposal),
        )

        assert leaf.disposition == "INVALID"
        assert leaf.reason == "EVALUATOR_DRIFT"

    def test_absent_session_uses_fixed_pending_then_missing_deadline(self) -> None:
        proposal = _proposal()
        schedule = full_session_observation_schedule(
            proposal.execution_policy.market,
            proposal.target_session_date,
        )

        pending = absent_session_leaf(
            proposal,
            as_of=schedule[-1] + timedelta(hours=1),
        )
        missing = absent_session_leaf(
            proposal,
            as_of=schedule[-1] + timedelta(hours=7),
        )

        assert pending.disposition == "PENDING"
        assert missing.disposition == "MISSING"
        assert pending.expected_observation_count == missing.expected_observation_count

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("open", _d("NaN")),
            ("high", _d("0")),
            ("low", _d("106")),
            ("volume", _d("-1")),
            ("open", _d("10000000000000")),
            ("volume", _d("10000000000000000000")),
        ],
    )
    def test_forward_bar_rejects_untrusted_values(
        self,
        field_name: str,
        value: Decimal,
    ) -> None:
        timestamp = datetime(2026, 8, 3, 13, 31, tzinfo=timezone.utc)
        bar = ForwardBar(
            timestamp=timestamp,
            observed_at=timestamp + timedelta(seconds=1),
            open=_d("105"),
            high=_d("105.2"),
            low=_d("104.8"),
            close=_d("105"),
            volume=_d("100"),
            source_sha256=_digest("bar"),
        )
        with pytest.raises(IntervalForwardReplayError):
            if field_name == "open":
                replace(bar, open=value)
            elif field_name == "high":
                replace(bar, high=value)
            elif field_name == "low":
                replace(bar, low=value)
            else:
                replace(bar, volume=value)

    def test_bar_source_revision_changes_artifact_and_leaf_digest(self) -> None:
        proposal = _proposal()
        bars = _bars(proposal)
        revised_first = replace(
            bars[0],
            source_sha256=_digest("revised-source"),
        )

        first = replay_paired_session(
            proposal,
            bars,
            finalized_at=_finalized_at(proposal),
        )
        second = replay_paired_session(
            proposal,
            (revised_first, *bars[1:]),
            finalized_at=_finalized_at(proposal),
        )

        assert first.source_artifact_sha256 != second.source_artifact_sha256
        assert first.common_environment_sha256 != second.common_environment_sha256
        assert first.leaf_digest_sha256 != second.leaf_digest_sha256

    def test_encoded_source_artifact_is_required_for_leaf_verification(self) -> None:
        proposal = _proposal()
        bars = _bars(proposal, candidate_trade=True)
        bundle = replay_paired_session_bundle(
            proposal,
            bars,
            finalized_at=_finalized_at(proposal),
        )
        leaf = bundle.leaf
        slot = freeze_session_slot(
            (proposal,),
            symbol=proposal.execution_policy.symbol,
            target_session_date=proposal.target_session_date,
            occupied_at=proposal.registered_at + timedelta(seconds=1),
        )
        assert slot is not None
        verification_input = PairedLeafVerificationInput(
            slot=slot,
            proposal=proposal,
            causal_proposals=(proposal,),
            source_artifact=bundle.source_artifact,
        )

        assert verification_input.verify(leaf) == leaf
        tampered = replace(
            leaf,
            reason="CALLER_ASSERTED_INCLUDED",
            leaf_digest_sha256="",
        )
        with pytest.raises(IntervalForwardReplayError, match="source replay"):
            verification_input.verify(tampered)
        without_artifact = PairedLeafVerificationInput(
            slot=slot,
            proposal=proposal,
            causal_proposals=(proposal,),
            source_artifact=None,
        )
        with pytest.raises(IntervalForwardReplayError, match="encoded source"):
            without_artifact.verify(leaf)
        pending = absent_session_leaf(
            proposal,
            as_of=_finalized_at(proposal),
        )
        with pytest.raises(IntervalForwardReplayError, match="cannot carry"):
            verification_input.verify(pending)
        comparison_bypass = _LeafComparisonBypass(**leaf.__dict__)
        with pytest.raises(IntervalForwardReplayError, match="exact frozen type"):
            verification_input.verify(comparison_bypass)

    def test_round_trip_rejects_arbitrary_actions_and_derived_metrics(self) -> None:
        valid = _round_trip(date(2026, 8, 3), net_bps=_d("5"))
        with pytest.raises(IntervalForwardReplayError, match="action"):
            replace(valid, exit_action="ARBITRARY_SELL")
        with pytest.raises(IntervalForwardReplayError, match="derived metrics"):
            replace(valid, net_pnl=valid.net_pnl + _d("1"))

    def test_typed_source_decoder_rejects_identity_and_scalar_drift(self) -> None:
        proposal = _proposal()
        payload = source_artifact_payload(proposal, _bars(proposal))

        wrong_symbol = {**payload, "symbol": "AAPL.US"}
        with pytest.raises(IntervalForwardReplayError, match="identity"):
            bars_from_source_artifact_payload(proposal, wrong_symbol)

        extra_field = {**payload, "unexpected": True}
        with pytest.raises(IntervalForwardReplayError, match="fields"):
            bars_from_source_artifact_payload(proposal, extra_field)

        raw_bars = cast(list[dict[str, object]], payload["bars"])
        long_timestamp_bars = [dict(item) for item in raw_bars]
        long_timestamp_bars[0]["timestamp"] = "2" * 512 + "Z"
        with pytest.raises(IntervalForwardReplayError, match="canonical UTC"):
            bars_from_source_artifact_payload(
                proposal,
                {**payload, "bars": long_timestamp_bars},
            )

        long_decimal_bars = [dict(item) for item in raw_bars]
        long_decimal_bars[0]["open"] = "1" * 512
        with pytest.raises(IntervalForwardReplayError, match="decimal text"):
            bars_from_source_artifact_payload(
                proposal,
                {**payload, "bars": long_decimal_bars},
            )


def _round_trip(
    session_date: date,
    *,
    net_bps: Decimal,
    fee_bps: Decimal = Decimal("2"),
) -> ReplayRoundTrip:
    entry_price = _d("100")
    gross_bps = net_bps + fee_bps
    gross_pnl = gross_bps / _d("100")
    modeled_fees = fee_bps / _d("100")
    net_pnl = net_bps / _d("100")
    entry_at = datetime(
        session_date.year,
        session_date.month,
        session_date.day,
        14,
        0,
        tzinfo=timezone.utc,
    )
    return ReplayRoundTrip(
        entry_at=entry_at,
        exit_at=entry_at + timedelta(minutes=30),
        exit_action="SELL",
        entry_price=entry_price,
        exit_price=entry_price + gross_pnl,
        gross_pnl=gross_pnl,
        modeled_fees=modeled_fees,
        net_pnl=net_pnl,
        gross_bps=gross_bps,
        fee_bps=fee_bps,
        net_bps=net_bps,
        holding_minutes=_d("30"),
    )


def _included_assessment_leaf(
    session_date: date,
    *,
    baseline_bps: Decimal,
    candidate_bps: Decimal,
    baseline_trade: bool,
    candidate_trade: bool,
) -> PairedSessionLeaf:
    common = _digest(f"common-{session_date.isoformat()}")
    baseline = replay_module._build_variant_session_result(
        arm="baseline",
        band=FrozenIntervalBand(_d("95"), _d("110")),
        common_environment_sha256=common,
        round_trips=(
            (_round_trip(session_date, net_bps=baseline_bps),)
            if baseline_trade
            else ()
        ),
    )
    candidate = replay_module._build_variant_session_result(
        arm="candidate",
        band=FrozenIntervalBand(_d("100"), _d("104")),
        common_environment_sha256=common,
        round_trips=(
            (_round_trip(session_date, net_bps=candidate_bps),)
            if candidate_trade
            else ()
        ),
    )
    schedule = full_session_observation_schedule("US", session_date)
    return PairedSessionLeaf(
        symbol="NVDA.US",
        target_session_date=session_date,
        disposition="INCLUDED",
        reason="COMPLETE_PAIRED_DIAGNOSTIC_REPLAY",
        registration_digest_sha256=_digest(f"registration-{session_date}"),
        evaluator_digest_sha256=evaluator_digest_sha256(),
        expected_observation_count=len(schedule),
        observed_observation_count=len(schedule),
        finalized_at=schedule[-1] + timedelta(hours=1),
        source_artifact_sha256=_digest(f"artifact-{session_date}"),
        common_environment_sha256=common,
        baseline=baseline,
        candidate=candidate,
        delta_net_bps=candidate.net_bps - baseline.net_bps,
    )


def _missing_assessment_leaf(session_date: date) -> PairedSessionLeaf:
    schedule = full_session_observation_schedule("US", session_date)
    return PairedSessionLeaf(
        symbol="NVDA.US",
        target_session_date=session_date,
        disposition="MISSING",
        reason="SOURCE_ARTIFACT_MISSING_AT_FIXED_DEADLINE",
        registration_digest_sha256=_digest(f"registration-{session_date}"),
        evaluator_digest_sha256=evaluator_digest_sha256(),
        expected_observation_count=len(schedule),
        observed_observation_count=0,
        finalized_at=schedule[-1] + timedelta(hours=7),
    )


def _positive_horizon_leaves(
    dates: tuple[date, ...],
    *,
    baseline_trade_sessions: int = 50,
    candidate_trade_sessions: int = 50,
) -> tuple[PairedSessionLeaf, ...]:
    return tuple(
        _included_assessment_leaf(
            session_date,
            baseline_bps=_d("5") if index < baseline_trade_sessions else _d("0"),
            candidate_bps=_d("10") if index < candidate_trade_sessions else _d("0"),
            baseline_trade=index < baseline_trade_sessions,
            candidate_trade=index < candidate_trade_sessions,
        )
        for index, session_date in enumerate(dates)
    )


def _proposal_for_target_session(
    target_session_date: date,
    *,
    interaction_id: int,
) -> ProposalObservation:
    cursor = target_session_date - timedelta(days=1)
    for _ in range(14):
        try:
            prior_schedule = full_session_observation_schedule("US", cursor)
        except IntervalForwardContractError:
            cursor -= timedelta(days=1)
            continue
        started_at = prior_schedule[10]
        proposal = _proposal(
            interaction_id=interaction_id,
            started_at=started_at,
            completed_at=started_at + timedelta(minutes=1),
            registered_at=started_at + timedelta(minutes=2),
        )
        assert proposal.target_session_date == target_session_date
        return proposal
    raise AssertionError("previous source session was not found")


class TestFixedHorizonAssessment:
    def test_public_assessment_reverifies_every_fixed_denominator_leaf(self) -> None:
        dates = fixed_assessment_session_dates("US", date(2026, 8, 3))
        leaves: list[PairedSessionLeaf] = []
        verification_inputs: list[PairedLeafVerificationInput] = []
        for index, session_date in enumerate(dates):
            proposal = _proposal_for_target_session(
                session_date,
                interaction_id=10_000 + index,
            )
            schedule = full_session_observation_schedule("US", session_date)
            leaf = absent_session_leaf(
                proposal,
                as_of=schedule[-1] + timedelta(hours=7),
            )
            leaves.append(leaf)
            verification_inputs.append(
                _verification_input(proposal, bars=None)
            )

        with pytest.raises(IntervalForwardReplayError, match="every daily leaf"):
            assess_paired_sessions(
                symbol="NVDA.US",
                expected_session_dates=dates,
                leaves=leaves,
                verification_inputs=verification_inputs[:-1],
                assessment_cutoff=max(item.finalized_at for item in leaves),
            )
        with pytest.raises(IntervalForwardReplayError, match="duplicate"):
            assess_paired_sessions(
                symbol="NVDA.US",
                expected_session_dates=dates,
                leaves=leaves,
                verification_inputs=(
                    *verification_inputs[:-1],
                    verification_inputs[0],
                ),
                assessment_cutoff=max(item.finalized_at for item in leaves),
            )

        first_proposal = verification_inputs[0].proposal
        first_bundle = replay_paired_session_bundle(
            first_proposal,
            _bars(first_proposal, candidate_trade=True),
            finalized_at=_finalized_at(first_proposal),
        )
        leaves_with_included = (first_bundle.leaf, *leaves[1:])
        with pytest.raises(IntervalForwardReplayError, match="encoded source"):
            assess_paired_sessions(
                symbol="NVDA.US",
                expected_session_dates=dates,
                leaves=leaves_with_included,
                verification_inputs=verification_inputs,
                assessment_cutoff=max(
                    item.finalized_at for item in leaves_with_included
                ),
            )
        verified_inputs = (
            replace(
                verification_inputs[0],
                source_artifact=first_bundle.source_artifact,
            ),
            *verification_inputs[1:],
        )
        verified_assessment = assess_paired_sessions(
            symbol="NVDA.US",
            expected_session_dates=dates,
            leaves=leaves_with_included,
            verification_inputs=verified_inputs,
            assessment_cutoff=max(
                item.finalized_at for item in leaves_with_included
            ),
        )
        assert verified_assessment.included_sessions == 1

        assessment = assess_paired_sessions(
            symbol="NVDA.US",
            expected_session_dates=dates,
            leaves=leaves,
            verification_inputs=verification_inputs,
            assessment_cutoff=max(item.finalized_at for item in leaves),
        )

        assert assessment.missing_sessions == FIXED_HORIZON_SESSIONS
        assert assessment.human_review_discussion_eligible is False
        assert "CANDIDATE_NET_NOT_POSITIVE" in assessment.blockers

    def test_public_assessment_rejects_duck_typed_verifier_bypass(self) -> None:
        dates = fixed_assessment_session_dates("US", date(2026, 8, 3))
        leaves = _positive_horizon_leaves(dates)
        fake_inputs = cast(
            tuple[PairedLeafVerificationInput, ...],
            tuple(_FakeVerificationInput(item) for item in dates),
        )

        with pytest.raises(IntervalForwardReplayError, match="exact frozen type"):
            assess_paired_sessions(
                symbol="NVDA.US",
                expected_session_dates=dates,
                leaves=leaves,
                verification_inputs=fake_inputs,
                assessment_cutoff=leaves[-1].finalized_at + timedelta(hours=1),
            )

    def test_fixed_horizon_includes_zero_trade_days_in_paired_ci(self) -> None:
        dates = fixed_assessment_session_dates("US", date(2026, 8, 3))
        leaves = _positive_horizon_leaves(dates)
        cutoff = leaves[-1].finalized_at + timedelta(hours=1)

        assessment = replay_module._assess_verified_paired_leaves(
            symbol="NVDA.US",
            expected_session_dates=dates,
            leaves=leaves,
            assessment_cutoff=cutoff,
        )

        assert len(dates) == FIXED_HORIZON_SESSIONS
        assert assessment.included_sessions == 60
        assert assessment.baseline_closed_round_trips == 50
        assert assessment.candidate_closed_round_trips == 50
        assert assessment.cumulative_delta_bps == _d("250.00000000")
        assert assessment.mean_session_delta_bps == _d("4.16666667")
        assert assessment.confidence_lower_bps is not None
        assert assessment.confidence_lower_bps > 0
        assert assessment.human_review_discussion_eligible is True
        assert assessment.blockers == ()
        assert assessment.promotion_eligible is False
        assert assessment.automatic_promotion_allowed is False

    def test_candidate_must_be_profitable_not_merely_better_than_baseline(
        self,
    ) -> None:
        dates = fixed_assessment_session_dates("US", date(2026, 8, 3))
        leaves = tuple(
            _included_assessment_leaf(
                session_date,
                baseline_bps=_d("-10"),
                candidate_bps=_d("-5"),
                baseline_trade=True,
                candidate_trade=True,
            )
            for session_date in dates
        )

        assessment = replay_module._assess_verified_paired_leaves(
            symbol="NVDA.US",
            expected_session_dates=dates,
            leaves=leaves,
            assessment_cutoff=leaves[-1].finalized_at,
        )

        assert assessment.confidence_lower_bps is not None
        assert assessment.confidence_lower_bps > 0
        assert assessment.candidate_net_bps < 0
        assert "CANDIDATE_NET_NOT_POSITIVE" in assessment.blockers

    def test_assessment_rejects_mixed_evaluators_and_future_leaves(self) -> None:
        dates = fixed_assessment_session_dates("US", date(2026, 8, 3))
        leaves = list(_positive_horizon_leaves(dates))
        leaves[0] = replace(
            leaves[0],
            evaluator_digest_sha256="0" * 64,
            leaf_digest_sha256="",
        )
        with pytest.raises(IntervalForwardReplayError, match="current frozen evaluator"):
            replay_module._assess_verified_paired_leaves(
                symbol="NVDA.US",
                expected_session_dates=dates,
                leaves=leaves,
                assessment_cutoff=leaves[-1].finalized_at,
            )

        leaves = list(_positive_horizon_leaves(dates))
        cutoff = leaves[-1].finalized_at
        leaves[0] = replace(
            leaves[0],
            finalized_at=cutoff + timedelta(minutes=1),
            leaf_digest_sha256="",
        )
        with pytest.raises(IntervalForwardReplayError, match="cutoff"):
            replay_module._assess_verified_paired_leaves(
                symbol="NVDA.US",
                expected_session_dates=dates,
                leaves=leaves,
                assessment_cutoff=cutoff,
            )

    def test_each_arm_must_independently_reach_fifty_round_trips(self) -> None:
        dates = fixed_assessment_session_dates("US", date(2026, 8, 3))
        leaves = _positive_horizon_leaves(
            dates,
            baseline_trade_sessions=49,
            candidate_trade_sessions=50,
        )

        assessment = replay_module._assess_verified_paired_leaves(
            symbol="NVDA.US",
            expected_session_dates=dates,
            leaves=leaves,
            assessment_cutoff=leaves[-1].finalized_at + timedelta(hours=1),
        )

        assert assessment.baseline_closed_round_trips == 49
        assert assessment.candidate_closed_round_trips == 50
        assert "INSUFFICIENT_BASELINE_CLOSED_ROUND_TRIPS" in assessment.blockers
        assert assessment.human_review_discussion_eligible is False

    def test_missing_leaf_stays_in_fixed_denominator_and_blocks_discussion(
        self,
    ) -> None:
        dates = fixed_assessment_session_dates("US", date(2026, 8, 3))
        leaves = list(_positive_horizon_leaves(dates))
        leaves[-1] = _missing_assessment_leaf(dates[-1])

        assessment = replay_module._assess_verified_paired_leaves(
            symbol="NVDA.US",
            expected_session_dates=dates,
            leaves=leaves,
            assessment_cutoff=leaves[-1].finalized_at + timedelta(hours=1),
        )

        assert assessment.expected_sessions == 60
        assert assessment.included_sessions == 59
        assert assessment.missing_sessions == 1
        assert assessment.paired_coverage_ratio < 1
        assert "MISSING_SESSIONS" in assessment.blockers
        assert "PAIRED_COVERAGE_INCOMPLETE" in assessment.blockers

    def test_optional_stopping_and_cherry_picked_dates_are_rejected(self) -> None:
        dates = fixed_assessment_session_dates("US", date(2026, 8, 3))
        leaves = _positive_horizon_leaves(dates)

        with pytest.raises(IntervalForwardReplayError, match="requires 60"):
            replay_module._assess_verified_paired_leaves(
                symbol="NVDA.US",
                expected_session_dates=dates[:-1],
                leaves=leaves[:-1],
                assessment_cutoff=leaves[-2].finalized_at,
            )
        cherry_picked = (*dates[:-1], dates[-1] + timedelta(days=1))
        with pytest.raises(IntervalForwardReplayError, match="consecutive"):
            replay_module._assess_verified_paired_leaves(
                symbol="NVDA.US",
                expected_session_dates=cherry_picked,
                leaves=leaves,
                assessment_cutoff=leaves[-1].finalized_at,
            )

    def test_duplicate_leaf_cannot_inflate_session_or_trade_counts(self) -> None:
        dates = fixed_assessment_session_dates("US", date(2026, 8, 3))
        leaves = _positive_horizon_leaves(dates)
        with pytest.raises(IntervalForwardReplayError, match="duplicate"):
            replay_module._assess_verified_paired_leaves(
                symbol="NVDA.US",
                expected_session_dates=dates,
                leaves=(*leaves[:-1], leaves[0]),
                assessment_cutoff=leaves[-1].finalized_at,
            )

    def test_assessment_and_leaf_derived_metrics_are_tamper_evident(self) -> None:
        dates = fixed_assessment_session_dates("US", date(2026, 8, 3))
        leaves = _positive_horizon_leaves(dates)
        first = leaves[0]
        assert first.candidate is not None

        with pytest.raises(IntervalForwardReplayError, match="delta"):
            replace(first, delta_net_bps=_d("999"), leaf_digest_sha256="")
        with pytest.raises(IntervalForwardReplayError, match="aggregate metrics"):
            replace(
                first.candidate,
                net_bps=_d("999"),
                result_digest_sha256="",
            )

    def test_cross_symbol_leaf_is_rejected(self) -> None:
        dates = fixed_assessment_session_dates("US", date(2026, 8, 3))
        leaves = list(_positive_horizon_leaves(dates))
        leaves[0] = replace(
            leaves[0],
            symbol="AAPL.US",
            leaf_digest_sha256="",
        )
        with pytest.raises(IntervalForwardReplayError, match="symbol"):
            replay_module._assess_verified_paired_leaves(
                symbol="NVDA.US",
                expected_session_dates=dates,
                leaves=leaves,
                assessment_cutoff=leaves[-1].finalized_at,
            )


class TestSemanticAndSafetyGoldens:
    def test_domain_has_no_io_or_live_execution_dependencies(self) -> None:
        sources = (
            inspect.getsource(contract_module),
            inspect.getsource(replay_module),
        )
        forbidden = (
            "app.services",
            "sqlalchemy",
            "SessionLocal",
            "TradeExecutionService",
            "BrokerGateway",
        )
        for source in sources:
            assert all(token not in source for token in forbidden)

    def test_fidelity_and_p0_limitations_are_permanent(self) -> None:
        assert DATA_FIDELITY == "ONE_MINUTE_OHLCV"
        assert BBO_COVERAGE == "NONE"
        assert ENTRY_CROSSING_SEMANTICS == "BAR_LOCAL_CROSSING_APPROXIMATION"
        assert "LIVE_PARITY_UNPROVEN" in PERMANENT_LIMITATIONS
        assert "SERVER_SOURCE_AUTHORITY_REQUIRED" in PERMANENT_LIMITATIONS

    def test_evaluator_manifest_covers_every_transitive_local_module(self) -> None:
        source_sha256 = evaluator_manifest()["source_sha256"]
        assert isinstance(source_sha256, dict)
        assert set(source_sha256) == {
            "artifact_module",
            "backtest_module",
            "fees_module",
            "holiday_calendar_module",
            "interval_contract_module",
            "interval_replay_module",
            "market_calendar_module",
        }
        assert all(
            isinstance(value, str) and len(value) == 64
            for value in source_sha256.values()
        )

    def test_golden_digests(self) -> None:
        proposal = _proposal()
        leaf = replay_paired_session(
            proposal,
            _bars(proposal, candidate_trade=True),
            finalized_at=_finalized_at(proposal),
        )
        dates = fixed_assessment_session_dates("US", date(2026, 8, 3))
        leaves = _positive_horizon_leaves(dates)
        assessment = replay_module._assess_verified_paired_leaves(
            symbol="NVDA.US",
            expected_session_dates=dates,
            leaves=leaves,
            assessment_cutoff=leaves[-1].finalized_at + timedelta(hours=1),
        )

        assert evaluator_digest_sha256() == (
            "09917a773dd10f0409ab00cc68f21285e2df4cbfb6d249fb712af07ad6356566"
        )
        assert proposal.registration_digest_sha256 == (
            "dfdc4569e3d1abca86502aaf259e2ad5f2454a780c3e366efcef189913c581fe"
        )
        assert leaf.leaf_digest_sha256 == (
            "a4bc00de5caa8bc41dcbcd6eaef00b067f383420f6562ea9f94b9d18e559f838"
        )
        assert assessment.report_digest_sha256 == (
            "a2c7f7d07ae59ef8facb806d687f23c128188073d7975c3dae1d8d902c6edee6"
        )
        assert canonical_sha256(assessment.to_payload()) != assessment.report_digest_sha256
