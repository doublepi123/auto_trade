from __future__ import annotations

import importlib.util
import json
import sys
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from app.core.holiday_calendar import is_market_closed
from app.domain.strategy_v2.frozen_disproof_queue import (
    ASSESSMENT_ALGORITHM_VERSION,
    ASSESSMENT_POLICY_VERSION,
    ASSESSMENT_REPORT_SCHEMA_VERSION,
    FROZEN_DISPROOF_INPUT_SCHEMA_VERSION,
    FROZEN_EVALUATOR_DIGEST,
    FROZEN_QUEUE_AS_OF_DATE,
    FROZEN_QUEUE_DIGEST,
    MINIMUM_CLOSED_TRADES,
    MINIMUM_EXPECTED_SESSION_COVERAGE_RATIO,
    MINIMUM_FUTURE_TRADE_DAYS,
    canonical_frozen_queue_manifest,
    evaluate_frozen_forward_disproof_queue,
    frozen_queue_digest,
)


_EVALUATOR_DIGEST = (
    "e5ae9ea3e68dcc47d5131c21d8ba223824aecabf59da1f4b592df72cb9aa0294"
)
_CONFIG_HASHES = {
    "NVDA.US": (
        "9afed570d67d2394f01d40d6706ad7b5eefea5627c7813b4ae762d46a4eeddd9"
    ),
    "AAPL.US": (
        "11583728616a7d5f17328d38f024fa10644c29e8b5691106ea961ba75d160a32"
    ),
    "AVGO.US": (
        "f2e0b1fcfb832c1887a093abda8a146fc03741b2deb47bb29c3132542a4393e2"
    ),
    "GOOGL.US": (
        "5afa45fb95bda68262818c43bbc4f01f62cb81a5e6457d68b5c64dbd11be5bdc"
    ),
    "META.US": (
        "cf97ba201b97e8040e0ff113ebb4197268f7709ef6c801979f27eea0a6e9fcd3"
    ),
    "TER.US": (
        "e9f094c87d6342ea6a8663b04584c0aae16455060e0ad3a903a18620c5411435"
    ),
}


def _queue() -> dict[str, object]:
    return canonical_frozen_queue_manifest()


def _trading_days(
    first: date,
    count: int,
) -> list[date]:
    result: list[date] = []
    current = first
    while len(result) < count:
        if current.weekday() < 5 and not is_market_closed("US", current):
            result.append(current)
        current += timedelta(days=1)
    return result


def _daily_row(
    session_date: date,
    *,
    closed_trades: int,
    net_pnl: float,
    baseline_net_pnl: float | None = None,
    disposition: str = "INCLUDED",
    exclusion_reason: str | None = None,
    structural_failure: bool = False,
) -> dict[str, object]:
    fees = float(closed_trades) * 0.1
    baseline_net = net_pnl if baseline_net_pnl is None else baseline_net_pnl
    candidate_metrics: dict[str, object] = {
        "bars": 390,
        "closed_trades": closed_trades,
        "gross_pnl": net_pnl + fees,
        "fees": fees,
        "net_pnl": net_pnl,
    }
    baseline_metrics: dict[str, object] = {
        "bars": 390,
        "closed_trades": closed_trades,
        "gross_pnl": baseline_net + fees,
        "fees": fees,
        "net_pnl": baseline_net,
    }
    return {
        "target_session_date": session_date.isoformat(),
        "disposition": disposition,
        "exclusion_reason": (
            ""
            if disposition == "INCLUDED"
            else exclusion_reason or "NO_DATA"
        ),
        "structural_failure": structural_failure,
        "target_bars": 390,
        "seed_bars_sha256": "0" * 64,
        "target_bars_sha256": "1" * 64,
        "baseline_input_sha256": "2" * 64,
        "candidate_input_sha256": "2" * 64,
        "same_target_bars": True,
        "baseline_replay_match": True,
        "session_local_invariant": True,
        "baseline_result_sha256": "6" * 64,
        "candidate_result_sha256": "3" * 64,
        "evidence_digest_sha256": "4" * 64,
        "baseline_metrics": baseline_metrics,
        "candidate_metrics": candidate_metrics,
    }


def _forward_report(
    symbol: str,
    config_hash: str,
    daily: list[dict[str, object]],
    *,
    blockers: list[str] | None = None,
) -> dict[str, object]:
    return {
        "status": "COLLECTING",
        "mode": "SHADOW",
        "order_submission_allowed": False,
        "automatic_promotion_allowed": False,
        "historical_target_backfill_allowed": False,
        "evaluation_scope": "FORWARD_OUT_OF_SAMPLE",
        "blockers": blockers or [],
        "registration": {
            "symbol": symbol,
            "source_config_version": config_hash,
            "evaluator_digest": _EVALUATOR_DIGEST,
            "candidate_algorithm_version": (
                "strategy-v2-causal-trend-prewarm-v1"
            ),
        },
        "daily": deepcopy(daily),
    }


def _payload(
    control_daily: list[dict[str, object]],
    candidate_daily: list[dict[str, object]],
    *,
    precommit: bool,
    assessment_as_of_date: date | None = None,
) -> dict[str, object]:
    queue = _queue()
    source_dates = [
        date.fromisoformat(cast(str, row["target_session_date"]))
        for row in [*control_daily, *candidate_daily]
    ]
    assessment_date = assessment_as_of_date or max(
        [FROZEN_QUEUE_AS_OF_DATE, *source_dates]
    )
    payload: dict[str, object] = {
        "schema_version": FROZEN_DISPROOF_INPUT_SCHEMA_VERSION,
        "frozen_queue": queue,
        "assessment_policy": {
            "version": ASSESSMENT_POLICY_VERSION,
            "assessment_as_of_date": assessment_date.isoformat(),
        },
        "forward_reports": {
            "NVDA.US": _forward_report(
                "NVDA.US",
                _CONFIG_HASHES["NVDA.US"],
                control_daily,
            ),
            **{
                symbol: _forward_report(
                    symbol,
                    _CONFIG_HASHES[symbol],
                    candidate_daily,
                )
                for symbol in _CONFIG_HASHES
                if symbol != "NVDA.US"
            },
        },
    }
    if precommit:
        payload["precommitted_freeze_digest"] = frozen_queue_digest(queue)
    return payload


def _candidate(
    report: dict[str, object],
    symbol: str = "AAPL.US",
) -> dict[str, object]:
    rows = cast(list[dict[str, object]], report["candidates"])
    assert len(rows) == 5
    return next(row for row in rows if row["symbol"] == symbol)


def test_short_history_is_insufficient_and_cannot_promote() -> None:
    future = date(2026, 8, 3)
    report = evaluate_frozen_forward_disproof_queue(_payload(
        [_daily_row(future, closed_trades=1, net_pnl=-0.5)],
        [_daily_row(future, closed_trades=2, net_pnl=1.0)],
        precommit=False,
    ))

    assert report["status"] == "INSUFFICIENT_EVIDENCE"
    assert report["research_only"] is True
    assert report["live_equivalent"] is False
    assert report["order_submission_allowed"] is False
    assert report["automatic_promotion_allowed"] is False
    assert report["promotion_recommendation"] == "NONE"
    assert report["parameter_tuning_performed"] is False
    row = _candidate(report)
    assert row["common_future_trade_days"] == 1
    assert row["status"] == "INSUFFICIENT_EVIDENCE"
    assert row["blockers"] == [
        "ASSESSMENT_CUTOFF_PROVENANCE_UNVERIFIED",
        "FREEZE_PRECOMMITMENT_MISSING",
        "FUTURE_TRADE_DAYS_INSUFFICIENT",
        "CANDIDATE_CLOSED_TRADES_INSUFFICIENT",
        "WITHIN_SYMBOL_RETURN_BPS_PREIMAGE_MISSING",
    ]
    freeze = cast(dict[str, object], report["freeze"])
    assert freeze["precommit_verified"] is False
    assert freeze["freeze_digest"] == FROZEN_QUEUE_DIGEST
    assert freeze["entries"] == sorted(
        cast(list[dict[str, object]], freeze["entries"]),
        key=lambda item: cast(str, item["symbol"]),
    )


def test_complete_raw_evidence_stays_blocked_without_bound_preimage() -> None:
    first = date(2026, 8, 3)
    common_days = _trading_days(first, 20)
    control_daily = [
        _daily_row(day, closed_trades=3, net_pnl=0.5)
        for day in common_days
    ]
    candidate_daily = [
        _daily_row(
            day,
            closed_trades=3,
            net_pnl=1.0,
            baseline_net_pnl=0.25,
        )
        for day in common_days
    ]

    report = evaluate_frozen_forward_disproof_queue(_payload(
        control_daily,
        candidate_daily,
        precommit=True,
        assessment_as_of_date=common_days[-1],
    ))

    assert report["status"] == "INSUFFICIENT_EVIDENCE"
    assert report["evidence_review_ready"] is False
    assert report["promotion_eligible"] is False
    assert report["promotion_blockers"] == [
        "EVIDENCE_REVIEW_NOT_READY",
        "QUANT_CANDIDATE_VETO_NOT_VERIFIED",
        "MANUAL_PROMOTION_REQUIRED",
    ]
    thresholds = cast(dict[str, object], report["evidence_thresholds"])
    assert thresholds["minimum_future_trade_days"] == MINIMUM_FUTURE_TRADE_DAYS
    assert thresholds["minimum_closed_trades_per_candidate"] == (
        MINIMUM_CLOSED_TRADES
    )
    assert thresholds["minimum_expected_session_coverage_ratio"] == (
        MINIMUM_EXPECTED_SESSION_COVERAGE_RATIO
    )
    assert thresholds["nvda_performance_control"] is False
    assert thresholds["quant_candidate_required_for_promotion"] is True
    assert thresholds["thresholds_tunable"] is False
    row = _candidate(report)
    assert row["common_future_trade_days"] == 20
    assert row["candidate_source_future_days"] == 20
    assert row["expected_session_count"] == 20
    assert row["paired_expected_session_coverage_ratio"] == 1.0
    assert row["evidence_review_ready"] is False
    assert row["manual_disproof_review_ready"] is False
    assert row["promotion_eligible"] is False
    assert row["blockers"] == [
        "ASSESSMENT_CUTOFF_PROVENANCE_UNVERIFIED",
        "WITHIN_SYMBOL_RETURN_BPS_PREIMAGE_MISSING",
    ]
    candidate = cast(dict[str, object], row["candidate"])
    control = cast(dict[str, object], row["nvda_same_window_control"])
    assert candidate["closed_trades"] == 60
    assert control["closed_trades"] == 60
    assert candidate["net_pnl"] == 20.0
    assert control["net_pnl"] == 10.0
    assert row["observed_net_pnl_delta_vs_nvda"] == 10.0
    assert row["observed_net_pnl_delta_vs_nvda_descriptive_only"] is True
    within_symbol = cast(
        dict[str, object],
        row["within_symbol_return_bps_comparison"],
    )
    assert within_symbol == {
        "available": False,
        "comparison_design": "SAME_SYMBOL_SAME_SESSION_BASELINE_PAIRED",
        "normalization": (
            "SUM_NET_PNL_DIVIDED_BY_SUM_CLOSED_TRADE_ENTRY_NOTIONAL"
        ),
        "paired_session_count": 20,
        "baseline_net_return_bps": None,
        "candidate_net_return_bps": None,
        "candidate_delta_bps": None,
        "missing_preimage_blocker": (
            "WITHIN_SYMBOL_RETURN_BPS_PREIMAGE_MISSING"
        ),
    }
    nvda_control = cast(dict[str, object], row["nvda_control"])
    assert nvda_control["role"] == "DATA_OPERATIONAL_CONTROL_ONLY"
    assert nvda_control["performance_control"] is False
    assert nvda_control["closed_trades_gate_required"] is False
    assert row["automatic_disproof_decision_allowed"] is False


def test_nvda_operational_control_has_no_trade_count_gate() -> None:
    days = _trading_days(date(2026, 8, 3), 20)
    control = [
        _daily_row(day, closed_trades=0, net_pnl=0.0)
        for day in days
    ]
    candidate = [
        _daily_row(day, closed_trades=3, net_pnl=1.0)
        for day in days
    ]

    row = _candidate(evaluate_frozen_forward_disproof_queue(_payload(
        control,
        candidate,
        precommit=True,
        assessment_as_of_date=days[-1],
    )))

    control_summary = cast(
        dict[str, object],
        row["nvda_same_window_control"],
    )
    blockers = cast(list[str], row["blockers"])
    assert control_summary["closed_trades"] == 0
    assert blockers == [
        "ASSESSMENT_CUTOFF_PROVENANCE_UNVERIFIED",
        "WITHIN_SYMBOL_RETURN_BPS_PREIMAGE_MISSING",
    ]
    assert "NVDA_CONTROL_CLOSED_TRADES_INSUFFICIENT" not in blockers


def test_assessment_policy_does_not_mutate_frozen_identity() -> None:
    queue = _queue()

    assert FROZEN_EVALUATOR_DIGEST == _EVALUATOR_DIGEST
    assert FROZEN_QUEUE_DIGEST == (
        "d2005f023cc9e1874609008a55c2b0d21d1d30647175ca607e60225e4f7ea69f"
    )
    assert frozen_queue_digest(queue) == FROZEN_QUEUE_DIGEST
    payload = _payload([], [], precommit=True)
    policy = cast(dict[str, object], payload["assessment_policy"])
    policy["assessment_as_of_date"] = "2026-08-10"
    assert frozen_queue_digest(payload["frozen_queue"]) == FROZEN_QUEUE_DIGEST

    report = evaluate_frozen_forward_disproof_queue(payload)
    assert report["assessment_report_schema_version"] == (
        ASSESSMENT_REPORT_SCHEMA_VERSION
    )
    assert report["assessment_algorithm_version"] == (
        ASSESSMENT_ALGORITHM_VERSION
    )
    report_policy = cast(dict[str, object], report["assessment_policy"])
    assert report_policy["legacy_frozen_metric_track"] == "candidate_metrics"
    assert report_policy["assessment_evidence_tracks"] == [
        "baseline_metrics",
        "candidate_metrics",
        "daily_dispositions",
    ]
    assert report_policy["frozen_manifest_notes_are_identity_only"] is True


def test_asymmetric_non_structural_exclusion_fails_closed() -> None:
    days = _trading_days(date(2026, 8, 3), 21)
    control = [
        _daily_row(day, closed_trades=3, net_pnl=1.0)
        for day in days
    ]
    candidate = [
        _daily_row(
            days[0],
            closed_trades=0,
            net_pnl=0.0,
            disposition="EXCLUDED",
            exclusion_reason="NO_DATA",
        ),
        *[
            _daily_row(day, closed_trades=3, net_pnl=1.0)
            for day in days[1:]
        ],
    ]

    row = _candidate(evaluate_frozen_forward_disproof_queue(_payload(
        control,
        candidate,
        precommit=True,
        assessment_as_of_date=days[-1],
    )))

    assert row["paired_expected_session_coverage_ratio"] == 0.952381
    assert row["common_future_trade_days"] == 20
    assert row["candidate_excluded_targets"] == 1
    assert row["blockers"] == [
        "ASSESSMENT_CUTOFF_PROVENANCE_UNVERIFIED",
        "EXPECTED_SESSION_DISPOSITION_MISMATCH",
        "WITHIN_SYMBOL_RETURN_BPS_PREIMAGE_MISSING",
    ]
    dispositions = cast(
        list[dict[str, object]],
        row["expected_session_dispositions"],
    )
    assert dispositions[0] == {
        "session_date": days[0].isoformat(),
        "candidate_disposition": "EXCLUDED_NON_STRUCTURAL",
        "candidate_exclusion_reason": "NO_DATA",
        "nvda_control_disposition": "INCLUDED",
        "nvda_control_exclusion_reason": "",
        "paired_included": False,
    }


def test_non_structural_exclusion_reason_mismatch_fails_closed() -> None:
    days = _trading_days(date(2026, 8, 3), 21)
    control = [
        _daily_row(
            days[0],
            closed_trades=0,
            net_pnl=0.0,
            disposition="EXCLUDED",
            exclusion_reason="TARGET_SESSION_INCOMPLETE",
        ),
        *[
            _daily_row(day, closed_trades=3, net_pnl=1.0)
            for day in days[1:]
        ],
    ]
    candidate = [
        _daily_row(
            days[0],
            closed_trades=0,
            net_pnl=0.0,
            disposition="EXCLUDED",
            exclusion_reason="COLLECTION_DISABLED",
        ),
        *[
            _daily_row(day, closed_trades=3, net_pnl=2.0)
            for day in days[1:]
        ],
    ]

    row = _candidate(evaluate_frozen_forward_disproof_queue(_payload(
        control,
        candidate,
        precommit=True,
        assessment_as_of_date=days[-1],
    )))

    assert row["paired_expected_session_coverage_ratio"] == 0.952381
    assert row["blockers"] == [
        "ASSESSMENT_CUTOFF_PROVENANCE_UNVERIFIED",
        "EXPECTED_SESSION_EXCLUSION_REASON_MISMATCH",
        "WITHIN_SYMBOL_RETURN_BPS_PREIMAGE_MISSING",
    ]


def test_structural_exclusion_is_an_explicit_evidence_blocker() -> None:
    days = _trading_days(date(2026, 8, 3), 21)
    control = [
        _daily_row(day, closed_trades=3, net_pnl=1.0)
        for day in days
    ]
    candidate = [
        _daily_row(
            days[0],
            closed_trades=0,
            net_pnl=0.0,
            disposition="EXCLUDED",
            exclusion_reason="SEED_SESSION_MISSING",
            structural_failure=True,
        ),
        *[
            _daily_row(day, closed_trades=3, net_pnl=1.0)
            for day in days[1:]
        ],
    ]

    row = _candidate(evaluate_frozen_forward_disproof_queue(_payload(
        control,
        candidate,
        precommit=True,
        assessment_as_of_date=days[-1],
    )))

    assert row["evidence_review_ready"] is False
    assert row["blockers"] == [
        "ASSESSMENT_CUTOFF_PROVENANCE_UNVERIFIED",
        "CANDIDATE_STRUCTURAL_EXCLUSION",
        "EXPECTED_SESSION_DISPOSITION_MISMATCH",
        "WITHIN_SYMBOL_RETURN_BPS_PREIMAGE_MISSING",
    ]
    dispositions = cast(
        list[dict[str, object]],
        row["expected_session_dispositions"],
    )
    assert dispositions[0]["candidate_disposition"] == (
        "EXCLUDED_STRUCTURAL"
    )
    assert dispositions[0]["candidate_exclusion_reason"] == (
        "SEED_SESSION_MISSING"
    )


def test_all_symbol_missing_sessions_remain_in_coverage_denominator() -> None:
    days = _trading_days(date(2026, 8, 3), 40)
    observed_days = days[3:]
    control = [
        _daily_row(day, closed_trades=3, net_pnl=1.0)
        for day in observed_days
    ]
    candidate = [
        _daily_row(day, closed_trades=3, net_pnl=2.0)
        for day in observed_days
    ]

    row = _candidate(evaluate_frozen_forward_disproof_queue(_payload(
        control,
        candidate,
        precommit=True,
        assessment_as_of_date=days[-1],
    )))

    assert row["expected_session_count"] == 40
    assert row["common_future_trade_days"] == 37
    assert row["paired_expected_session_coverage_ratio"] == 0.925
    assert row["blockers"] == [
        "ASSESSMENT_CUTOFF_PROVENANCE_UNVERIFIED",
        "EXPECTED_SESSION_COVERAGE_INSUFFICIENT",
        "WITHIN_SYMBOL_RETURN_BPS_PREIMAGE_MISSING",
    ]
    dispositions = cast(
        list[dict[str, object]],
        row["expected_session_dispositions"],
    )
    assert [item["candidate_disposition"] for item in dispositions[:3]] == [
        "MISSING",
        "MISSING",
        "MISSING",
    ]


def test_unbound_caller_notional_is_ignored_and_cannot_change_return() -> None:
    days = _trading_days(date(2026, 8, 3), 20)
    control = [
        _daily_row(day, closed_trades=3, net_pnl=1.0)
        for day in days
    ]
    candidate = [
        _daily_row(
            day,
            closed_trades=3,
            net_pnl=2.0,
            baseline_net_pnl=1.0,
        )
        for day in days
    ]
    payload = _payload(
        control,
        candidate,
        precommit=True,
        assessment_as_of_date=days[-1],
    )
    reports = cast(dict[str, dict[str, object]], payload["forward_reports"])
    for report in reports.values():
        daily = cast(list[dict[str, object]], report["daily"])
        for row in daily:
            baseline = cast(dict[str, object], row["baseline_metrics"])
            candidate_metrics = cast(
                dict[str, object],
                row["candidate_metrics"],
            )
            baseline["closed_trade_entry_notional"] = 1.0
            candidate_metrics["closed_trade_entry_notional"] = 1_000_000.0

    row = _candidate(evaluate_frozen_forward_disproof_queue(payload))

    assert row["blockers"] == [
        "ASSESSMENT_CUTOFF_PROVENANCE_UNVERIFIED",
        "WITHIN_SYMBOL_RETURN_BPS_PREIMAGE_MISSING",
    ]
    comparison = cast(
        dict[str, object],
        row["within_symbol_return_bps_comparison"],
    )
    assert comparison["available"] is False
    assert comparison["baseline_net_return_bps"] is None
    assert comparison["candidate_net_return_bps"] is None
    assert comparison["candidate_delta_bps"] is None
    assert comparison["missing_preimage_blocker"] == (
        "WITHIN_SYMBOL_RETURN_BPS_PREIMAGE_MISSING"
    )


def test_evidence_after_assessment_cutoff_fails_closed() -> None:
    days = _trading_days(date(2026, 8, 3), 21)
    control = [
        _daily_row(day, closed_trades=3, net_pnl=1.0)
        for day in days[:20]
    ]
    candidate = [
        _daily_row(day, closed_trades=3, net_pnl=2.0)
        for day in days
    ]

    row = _candidate(evaluate_frozen_forward_disproof_queue(_payload(
        control,
        candidate,
        precommit=True,
        assessment_as_of_date=days[19],
    )))

    assert row["candidate_post_assessment_rows_excluded"] == 1
    assert row["blockers"] == [
        "ASSESSMENT_CUTOFF_PROVENANCE_UNVERIFIED",
        "SOURCE_EVIDENCE_AFTER_ASSESSMENT_WINDOW",
        "WITHIN_SYMBOL_RETURN_BPS_PREIMAGE_MISSING",
    ]


def test_pre_freeze_history_never_satisfies_future_gates() -> None:
    old_days = [date(2026, 6, 1) + timedelta(days=index) for index in range(30)]
    future = date(2026, 8, 3)
    control = [
        _daily_row(day, closed_trades=10, net_pnl=5.0) for day in old_days
    ] + [_daily_row(future, closed_trades=1, net_pnl=0.0)]
    candidate = [
        _daily_row(day, closed_trades=10, net_pnl=8.0) for day in old_days
    ] + [_daily_row(future, closed_trades=1, net_pnl=0.0)]

    row = _candidate(evaluate_frozen_forward_disproof_queue(_payload(
        control,
        candidate,
        precommit=True,
    )))

    assert row["candidate_pre_freeze_rows_excluded"] == 30
    assert row["nvda_pre_freeze_rows_excluded"] == 30
    assert row["common_future_trade_days"] == 1
    assert row["status"] == "INSUFFICIENT_EVIDENCE"


def test_config_drift_and_source_blockers_fail_closed() -> None:
    first = date(2026, 8, 3)
    days = _trading_days(first, 20)
    payload = _payload(
        [_daily_row(day, closed_trades=3, net_pnl=1.0) for day in days],
        [_daily_row(day, closed_trades=3, net_pnl=2.0) for day in days],
        precommit=True,
    )
    reports = cast(dict[str, dict[str, object]], payload["forward_reports"])
    candidate_report = reports["AAPL.US"]
    registration = cast(dict[str, object], candidate_report["registration"])
    registration["source_config_version"] = "9" * 64
    candidate_report["blockers"] = ["EVIDENCE_DIGEST_MISMATCH"]

    row = _candidate(evaluate_frozen_forward_disproof_queue(payload))

    assert row["status"] == "INSUFFICIENT_EVIDENCE"
    assert row["blockers"] == [
        "ASSESSMENT_CUTOFF_PROVENANCE_UNVERIFIED",
        "SOURCE_FORWARD_BLOCKER:EVIDENCE_DIGEST_MISMATCH",
        "FROZEN_CONFIG_HASH_MISMATCH",
        "WITHIN_SYMBOL_RETURN_BPS_PREIMAGE_MISSING",
    ]


def test_frozen_cohort_rejects_role_reason_config_or_as_of_drift() -> None:
    queue = _queue()
    variants: list[dict[str, object]] = []
    reason_variant = deepcopy(queue)
    reason_entries = cast(list[dict[str, object]], reason_variant["entries"])
    reason_entries[1]["reason"] = "changed reason"
    variants.append(reason_variant)
    config_variant = deepcopy(queue)
    config_entries = cast(list[dict[str, object]], config_variant["entries"])
    config_entries[1]["config_hash"] = "c" * 64
    variants.append(config_variant)

    for variant in variants:
        with pytest.raises(ValueError, match="canonical precommitment"):
            frozen_queue_digest(variant)

    as_of_variant = deepcopy(queue)
    as_of_variant["as_of_date"] = "2026-08-01"
    with pytest.raises(ValueError, match="must remain frozen"):
        frozen_queue_digest(as_of_variant)

    role_variant = deepcopy(queue)
    role_entries = cast(list[dict[str, object]], role_variant["entries"])
    role_entries[1]["role"] = "SELECTED"
    with pytest.raises(ValueError, match="must match the frozen"):
        frozen_queue_digest(role_variant)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "changed-name"),
        ("evaluator_digest", "7" * 64),
    ],
)
def test_frozen_cohort_rejects_name_or_evaluator_drift(
    field: str,
    value: str,
) -> None:
    queue = _queue()
    queue[field] = value

    with pytest.raises(ValueError, match="canonical precommitment"):
        frozen_queue_digest(queue)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ohlc_source", "changed source"),
        ("bbo_coverage", "PARTIAL"),
        ("cost_model", "changed costs"),
        ("notes", ["changed notes"]),
    ],
)
def test_frozen_cohort_rejects_evidence_context_drift(
    field: str,
    value: object,
) -> None:
    queue = _queue()
    context = cast(dict[str, object], queue["evidence_context"])
    context[field] = value

    with pytest.raises(ValueError, match="canonical precommitment"):
        frozen_queue_digest(queue)


@pytest.mark.parametrize("mutation", ["missing", "extra", "swapped"])
def test_frozen_cohort_rejects_symbol_set_drift(mutation: str) -> None:
    queue = _queue()
    entries = cast(list[dict[str, object]], queue["entries"])
    if mutation == "missing":
        entries.pop(1)
    elif mutation == "extra":
        entries.append({
            "symbol": "MSFT.US",
            "role": "EXPLORATION",
            "reason": "must not enter the frozen cohort",
            "config_hash": "6" * 64,
        })
    else:
        entries[1]["symbol"] = "MSFT.US"

    with pytest.raises(ValueError, match="must match the frozen"):
        frozen_queue_digest(queue)


def test_mismatched_precommit_is_rejected() -> None:
    payload = _payload([], [], precommit=False)
    payload["precommitted_freeze_digest"] = "f" * 64

    with pytest.raises(ValueError, match="does not match"):
        evaluate_frozen_forward_disproof_queue(payload)


def test_legacy_v1_payload_without_policy_fails_closed_compatibly() -> None:
    payload = _payload([], [], precommit=False)
    payload.pop("assessment_policy")

    report = evaluate_frozen_forward_disproof_queue(payload)

    assert report["status"] == "INSUFFICIENT_EVIDENCE"
    policy = cast(dict[str, object], report["assessment_policy"])
    assert policy["assessment_as_of_date_provenance"] == "MISSING"
    row = _candidate(report)
    blockers = cast(list[str], row["blockers"])
    assert blockers[0] == "ASSESSMENT_POLICY_MISSING"


@pytest.mark.parametrize(
    "field_name",
    ["seed_bars_sha256", "baseline_result_sha256"],
)
def test_source_seed_and_baseline_result_hashes_require_sha256_format(
    field_name: str,
) -> None:
    future = date(2026, 8, 3)
    payload = _payload(
        [_daily_row(future, closed_trades=1, net_pnl=0.0)],
        [_daily_row(future, closed_trades=1, net_pnl=0.0)],
        precommit=True,
    )
    reports = cast(dict[str, dict[str, object]], payload["forward_reports"])
    daily = cast(list[dict[str, object]], reports["AAPL.US"]["daily"])
    daily[0][field_name] = "not-a-digest"

    row = _candidate(evaluate_frozen_forward_disproof_queue(payload))

    assert "SOURCE_INCLUDED_EVIDENCE_INVALID" in cast(
        list[str],
        row["blockers"],
    )


def _load_cli_module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "evaluate_frozen_disproof_queue.py"
    )
    spec = importlib.util.spec_from_file_location(
        "frozen_disproof_queue_cli_under_test",
        path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cli_reads_only_explicit_json_and_emits_json(
    tmp_path: Path,
) -> None:
    cli = _load_cli_module()
    source = tmp_path / "input.json"
    output = tmp_path / "output.json"
    source.write_text(
        json.dumps(_payload([], [], precommit=False)),
        encoding="utf-8",
    )

    assert cli.main(["--input", str(source), "--output", str(output)]) == 0

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "INSUFFICIENT_EVIDENCE"
    assert report["research_only"] is True
    assert report["order_submission_allowed"] is False
    assert report["automatic_promotion_allowed"] is False
    assert any("Historical BBO" in value for value in report["limitations"])
    assert any(
        "caller-supplied and unsigned" in value
        for value in report["limitations"]
    )


def test_cli_can_supply_separate_assessment_cutoff(tmp_path: Path) -> None:
    cli = _load_cli_module()
    source = tmp_path / "input.json"
    output = tmp_path / "output.json"
    payload = _payload([], [], precommit=False)
    payload.pop("assessment_policy")
    source.write_text(json.dumps(payload), encoding="utf-8")

    assert cli.main([
        "--input",
        str(source),
        "--output",
        str(output),
        "--assessment-as-of-date",
        "2026-08-03",
    ]) == 0

    report = json.loads(output.read_text(encoding="utf-8"))
    policy = cast(dict[str, object], report["assessment_policy"])
    assert policy["version"] == ASSESSMENT_POLICY_VERSION
    assert policy["assessment_as_of_date"] == "2026-08-03"
