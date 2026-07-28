from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path
from typing import cast

from app.cli.opening_extension_research import (
    RawMinuteBar,
    _baseline_session_dates,
    _build_sessions,
    _configure_longport_environment,
    _fetch_symbol_bars,
    _load_cache,
    _load_seed_cache,
    _load_current_baseline_symbols,
    _merge_bars,
    _parse_date,
    _parse_symbols,
    _replace_bar_date_range,
    _save_cache,
)
from app.core.broker import BrokerGateway
from app.core.market_calendar import get_session
from app.domain.opening_momentum import (
    OpeningMomentumConfig,
    evaluate_opening_momentum,
    opening_path_efficiency,
    shadow_round_trip_return_bps,
)
from app.domain.opening_momentum_policy import (
    EXCEPTIONAL_MAXIMUM_MARKET_RETURN_BPS,
    EXCEPTIONAL_MINIMUM_PATH_EFFICIENCY,
    EXCEPTIONAL_PATH_POLICY_NAME,
    OPENING_POLICY_COHORT_DIAGNOSTIC_VERSION,
    OPENING_POLICY_DIAGNOSTIC_VERSION,
    OPENING_POLICY_HORIZON_DIAGNOSTIC_VERSION,
    PRODUCTION_MAXIMUM_MARKET_RETURN_BPS,
    PRODUCTION_MINIMUM_PATH_EFFICIENCY,
    PRODUCTION_POLICY_NAME,
    OpeningPolicyCohortReport,
    OpeningPolicyCohortSlice,
    OpeningPolicyDiagnosticReport,
    OpeningPolicyHorizonReport,
    OpeningPolicyHorizonResult,
    OpeningPolicyHorizonSlice,
    OpeningPolicyResult,
    OpeningPolicySession,
    OpeningPolicySlice,
    OpeningPolicySpec,
    evaluate_opening_policy_cohort,
    evaluate_opening_policy_grid,
    evaluate_opening_policy_horizons,
    opening_execution_config,
)


OPENING_POLICY_CLI_VERSION = "opening-policy-research-cli-v11"
_DEFAULT_PATH_THRESHOLDS = (0.50, 0.60, 0.70, 0.80, 0.90)
_DEFAULT_MARKET_MAXIMUMS_BPS = (-10.0, -5.0, 0.0, 5.0, 10.0, 20.0)
_DEFAULT_MINIMUM_DATA_COVERAGE = 0.95
_CACHE_REFRESH_OVERLAP_DAYS = 7
_COHORT_COST_STRESS_BPS = (14.0, 20.0, 30.0)
_COHORT_SUBSET_MAX_CANDIDATES = 6
_COHORT_SUBSET_MINIMUM_EXECUTION_DISPLACEMENTS = 4
_COHORT_SUBSET_SELECTION_VERSION = (
    "discovery-exhaustive-joint-subset-cost30-drawdown-v1"
)
_COHORT_INDIVIDUAL_SCREEN_VERSION = (
    "discovery-individual-addition-cost30-drawdown-v1"
)
_EXCLUSION_DIAGNOSTIC_VERSION = (
    "opening-policy-exclusion-paired-baseline-anchored-holdout-v1"
)
_EXCLUSION_SUBSET_SELECTION_VERSION = (
    "discovery-exhaustive-joint-exclusion-cost30-drawdown-v1"
)
_PAIRED_POLICY_NAMES = {
    "production": PRODUCTION_POLICY_NAME,
    "exceptional": EXCEPTIONAL_PATH_POLICY_NAME,
}


def _default_policy_specs() -> tuple[OpeningPolicySpec, ...]:
    policies = [OpeningPolicySpec("BROAD")]
    policies.extend(
        OpeningPolicySpec(
            f"PATH_EFFICIENCY_{int(value * 100):03d}",
            minimum_path_efficiency=value,
        )
        for value in _DEFAULT_PATH_THRESHOLDS
    )
    for path_threshold in _DEFAULT_PATH_THRESHOLDS:
        for market_maximum in _DEFAULT_MARKET_MAXIMUMS_BPS:
            is_production = (
                math.isclose(
                    path_threshold,
                    PRODUCTION_MINIMUM_PATH_EFFICIENCY,
                )
                and math.isclose(
                    market_maximum,
                    PRODUCTION_MAXIMUM_MARKET_RETURN_BPS,
                )
            )
            policies.append(OpeningPolicySpec(
                (
                    PRODUCTION_POLICY_NAME
                    if is_production
                    else _sensitivity_policy_name(
                        path_threshold,
                        market_maximum,
                    )
                ),
                minimum_path_efficiency=path_threshold,
                maximum_market_return_bps=market_maximum,
            ))
    policies.append(OpeningPolicySpec(
        EXCEPTIONAL_PATH_POLICY_NAME,
        minimum_path_efficiency=(
            PRODUCTION_MINIMUM_PATH_EFFICIENCY
        ),
        maximum_market_return_bps=(
            PRODUCTION_MAXIMUM_MARKET_RETURN_BPS
        ),
        exceptional_minimum_path_efficiency=(
            EXCEPTIONAL_MINIMUM_PATH_EFFICIENCY
        ),
        exceptional_maximum_market_return_bps=(
            EXCEPTIONAL_MAXIMUM_MARKET_RETURN_BPS
        ),
    ))
    return tuple(policies)


def _resolve_paired_policy(
    policies: Sequence[OpeningPolicySpec],
    paired_policy: str,
) -> OpeningPolicySpec:
    policy_name = _PAIRED_POLICY_NAMES.get(paired_policy)
    if policy_name is None:
        raise ValueError(f"unsupported paired policy: {paired_policy}")
    return next(value for value in policies if value.name == policy_name)


def _sensitivity_policy_name(
    path_threshold: float,
    market_maximum_bps: float,
) -> str:
    market_token = (
        f"NEG{abs(int(market_maximum_bps)):03d}"
        if market_maximum_bps < 0
        else f"POS{int(market_maximum_bps):03d}"
    )
    return (
        f"PATH_{int(path_threshold * 100):03d}_"
        f"MARKET_MAX_{market_token}"
    )


def _build_policy_sessions(
    bars_by_symbol: dict[str, tuple[RawMinuteBar, ...]],
    *,
    symbols: Sequence[str],
    session_dates: Sequence[date],
    minimum_data_coverage: float,
    required_symbols: Sequence[str] = (),
    decision_config: OpeningMomentumConfig | None = None,
) -> tuple[OpeningPolicySession, ...]:
    if not 0 < minimum_data_coverage <= 1:
        raise ValueError("minimum_data_coverage must be in (0, 1]")
    normalized_symbols = tuple(
        dict.fromkeys(value.strip().upper() for value in symbols)
    )
    normalized_required = tuple(
        dict.fromkeys(value.strip().upper() for value in required_symbols)
    )
    if any(not value for value in normalized_required):
        raise ValueError("required_symbols must contain non-empty symbols")
    missing_required = set(normalized_required).difference(
        normalized_symbols
    )
    if missing_required:
        raise ValueError(
            "required_symbols are outside the policy universe: "
            + ", ".join(sorted(missing_required))
        )
    config = decision_config or opening_execution_config()
    source_sessions = _build_sessions(
        bars_by_symbol,
        symbols=normalized_symbols,
        session_dates=session_dates,
        signal_minutes=config.signal_minutes,
        execution_delay_minutes=config.execution_delay_minutes,
        holding_minutes=config.holding_minutes,
        stop_loss_pct=config.stop_loss_pct,
    )
    required_observations = max(
        config.minimum_universe_size,
        math.ceil(len(normalized_symbols) * minimum_data_coverage),
    )
    bars_index = {
        symbol: {
            value.timestamp: value
            for value in bars_by_symbol.get(symbol, ())
        }
        for symbol in normalized_symbols
    }
    market_session = get_session("US")
    result: list[OpeningPolicySession] = []
    for source in source_sessions:
        observed_symbols = {
            value.symbol for value in source.observations
        }
        if (
            len(source.observations) < required_observations
            or not set(normalized_required).issubset(observed_symbols)
        ):
            continue
        decision = evaluate_opening_momentum(source.observations, config)
        if decision.reason in {
            "ENTRY_BAR_MISSING",
            "INSUFFICIENT_UNIVERSE",
        }:
            continue
        candidate_symbol = decision.candidate_symbol
        candidate_path_efficiency: float | None = None
        if candidate_symbol is not None:
            session_open = datetime.combine(
                source.session_date,
                market_session.rth_open,
                tzinfo=market_session.timezone,
            ).astimezone(timezone.utc)
            indexed = bars_index.get(candidate_symbol, {})
            path_bars = tuple(
                indexed.get(session_open + timedelta(minutes=index))
                for index in range(config.signal_minutes)
            )
            if all(value is not None for value in path_bars):
                complete_path = tuple(
                    value for value in path_bars if value is not None
                )
                candidate_path_efficiency = opening_path_efficiency(
                    opening_price=complete_path[0].open,
                    closing_prices=tuple(
                        value.close for value in complete_path
                    ),
                )

        if decision.action != "ENTER_LONG":
            result.append(OpeningPolicySession(
                session_date=source.session_date,
                baseline_signal=False,
                gross_return_bps=0.0,
                market_return_bps=decision.market_return_bps,
                candidate_path_efficiency=candidate_path_efficiency,
                candidate_symbol=candidate_symbol,
            ))
            continue

        exit_by_symbol = {
            value.symbol: value for value in source.exit_prices
        }
        exit_outcome = exit_by_symbol.get(candidate_symbol or "")
        if (
            candidate_symbol is None
            or candidate_path_efficiency is None
            or decision.entry_price is None
            or decision.market_return_bps is None
            or exit_outcome is None
        ):
            continue
        gross_return_bps, _ = shadow_round_trip_return_bps(
            entry_price=decision.entry_price,
            exit_price=exit_outcome.price,
            config=config,
        )
        result.append(OpeningPolicySession(
            session_date=source.session_date,
            baseline_signal=True,
            gross_return_bps=gross_return_bps,
            market_return_bps=decision.market_return_bps,
            candidate_path_efficiency=candidate_path_efficiency,
            candidate_symbol=candidate_symbol,
            stop_triggered=exit_outcome.stop_triggered,
        ))
    return tuple(result)


def _result(
    report: OpeningPolicyDiagnosticReport,
    policy_name: str,
) -> OpeningPolicyResult:
    return next(
        value
        for value in report.policies
        if value.policy.name == policy_name
    )


def _slice(
    result: OpeningPolicyResult,
    name: str,
) -> OpeningPolicySlice:
    return next(value for value in result.slices if value.name == name)


def _compact_policy_payload(
    result: OpeningPolicyResult,
) -> dict[str, object]:
    discovery = _slice(result, "DISCOVERY")
    holdout = _slice(result, "HOLDOUT")
    return {
        "policy": asdict(result.policy),
        "discovery": {
            "metrics": asdict(discovery.metrics),
            "displacement": asdict(discovery.displacement),
        },
        "holdout": {
            "metrics": asdict(holdout.metrics),
            "comparison_to_baseline": asdict(
                holdout.comparison_to_baseline
            ),
            "displacement": asdict(holdout.displacement),
        },
    }


def _sensitivity_summary_payload(
    result: OpeningPolicyResult,
) -> dict[str, object]:
    discovery = _slice(result, "DISCOVERY")
    holdout = _slice(result, "HOLDOUT")
    return {
        "policy": asdict(result.policy),
        "discovery": {
            "entries": discovery.metrics.entries,
            "cumulative_return_bps": (
                discovery.metrics.cumulative_return_bps
            ),
            "max_drawdown_bps": discovery.metrics.max_drawdown_bps,
            "cumulative_without_best_3_bps": (
                discovery.metrics.cumulative_without_best_3_bps
            ),
        },
        "holdout": {
            "entries": holdout.metrics.entries,
            "cumulative_return_bps": holdout.metrics.cumulative_return_bps,
            "max_drawdown_bps": holdout.metrics.max_drawdown_bps,
            "cumulative_without_best_3_bps": (
                holdout.metrics.cumulative_without_best_3_bps
            ),
            "cumulative_delta_bps": (
                holdout.comparison_to_baseline.cumulative_delta_bps
            ),
        },
    }


def _cohort_slice(
    report: OpeningPolicyCohortReport,
    name: str,
) -> OpeningPolicyCohortSlice:
    return next(value for value in report.slices if value.name == name)


def _compact_cohort_payload(
    report: OpeningPolicyCohortReport,
) -> dict[str, object]:
    discovery = _cohort_slice(report, "DISCOVERY")
    holdout = _cohort_slice(report, "HOLDOUT")
    return {
        "algorithm_version": report.algorithm_version,
        "policy": asdict(report.policy),
        "cohort_symbols": list(report.cohort_symbols),
        "round_trip_cost_bps": report.round_trip_cost_bps,
        "paired_sessions": report.paired_sessions,
        "discovery_end_date": report.discovery_end_date.isoformat(),
        "discovery": discovery.to_dict(),
        "holdout": holdout.to_dict(),
        "diagnostic_only": report.diagnostic_only,
        "automatic_promotion_allowed": (
            report.automatic_promotion_allowed
        ),
    }


def _exclusion_slice_payload(
    value: OpeningPolicyCohortSlice,
) -> dict[str, object]:
    return {
        "name": value.name,
        "start_date": (
            value.start_date.isoformat() if value.start_date else None
        ),
        "end_date": value.end_date.isoformat() if value.end_date else None,
        "resolved_sessions": value.resolved_sessions,
        "candidate_displacement_sessions": (
            value.candidate_displacement_sessions
        ),
        "execution_displacement_sessions": (
            value.execution_displacement_sessions
        ),
        "baseline_only_entry_sessions": (
            value.baseline_only_entry_sessions
        ),
        "reduced_only_entry_sessions": value.cohort_only_entry_sessions,
        "baseline": asdict(value.baseline),
        "reduced": asdict(value.cohort),
        "comparison": asdict(value.comparison),
        "displacements": [
            {
                "session_date": item.session_date.isoformat(),
                "baseline_candidate_symbol": (
                    item.baseline_candidate_symbol
                ),
                "reduced_candidate_symbol": item.cohort_candidate_symbol,
                "baseline_entered": item.baseline_entered,
                "reduced_entered": item.cohort_entered,
                "baseline_return_bps": item.baseline_return_bps,
                "reduced_return_bps": item.cohort_return_bps,
                "delta_bps": item.delta_bps,
            }
            for item in value.displacements
        ],
        "tail_robustness_available": value.tail_robustness_available,
        "tail_robustness_passed": value.tail_robustness_passed,
    }


def _exclusion_report_payload(
    report: OpeningPolicyCohortReport,
) -> dict[str, object]:
    return {
        "algorithm_version": _EXCLUSION_DIAGNOSTIC_VERSION,
        "comparison_engine_version": report.algorithm_version,
        "comparison_mode": "BASELINE_MINUS_EXCLUSIONS",
        "policy": asdict(report.policy),
        "discovery_ratio": report.discovery_ratio,
        "round_trip_cost_bps": report.round_trip_cost_bps,
        "baseline_source_sessions": report.baseline_source_sessions,
        "reduced_source_sessions": report.cohort_source_sessions,
        "paired_sessions": report.paired_sessions,
        "discovery_sessions": report.discovery_sessions,
        "holdout_sessions": report.holdout_sessions,
        "discovery_end_date": report.discovery_end_date.isoformat(),
        "excluded_symbols": list(report.cohort_symbols),
        "slices": [
            _exclusion_slice_payload(value) for value in report.slices
        ],
        "diagnostic_only": report.diagnostic_only,
        "automatic_promotion_allowed": (
            report.automatic_promotion_allowed
        ),
    }


def _compact_exclusion_payload(
    report: OpeningPolicyCohortReport,
) -> dict[str, object]:
    discovery = _cohort_slice(report, "DISCOVERY")
    holdout = _cohort_slice(report, "HOLDOUT")
    return {
        "algorithm_version": _EXCLUSION_DIAGNOSTIC_VERSION,
        "comparison_engine_version": report.algorithm_version,
        "policy": asdict(report.policy),
        "excluded_symbols": list(report.cohort_symbols),
        "round_trip_cost_bps": report.round_trip_cost_bps,
        "paired_sessions": report.paired_sessions,
        "discovery_end_date": report.discovery_end_date.isoformat(),
        "discovery": _exclusion_slice_payload(discovery),
        "holdout": _exclusion_slice_payload(holdout),
        "diagnostic_only": report.diagnostic_only,
        "automatic_promotion_allowed": (
            report.automatic_promotion_allowed
        ),
    }


def _cohort_subset_blockers(
    primary: OpeningPolicyCohortReport,
    conservative: OpeningPolicyCohortReport,
) -> tuple[str, ...]:
    discovery = _cohort_slice(primary, "DISCOVERY")
    conservative_discovery = _cohort_slice(
        conservative,
        "DISCOVERY",
    )
    blockers: list[str] = []
    if (
        discovery.execution_displacement_sessions
        < _COHORT_SUBSET_MINIMUM_EXECUTION_DISPLACEMENTS
    ):
        blockers.append("DISCOVERY_EXECUTION_DISPLACEMENTS_BELOW_4")
    if discovery.comparison.cumulative_delta_bps <= 0:
        blockers.append("DISCOVERY_DELTA_NOT_POSITIVE")
    if not discovery.tail_robustness_passed:
        blockers.append("DISCOVERY_TAIL_ROBUSTNESS_FAILED")
    if not discovery.comparison.risk_guard_passed:
        blockers.append("DISCOVERY_DRAWDOWN_GUARD_FAILED")
    if conservative_discovery.comparison.cumulative_delta_bps <= 0:
        blockers.append("DISCOVERY_30BP_DELTA_NOT_POSITIVE")
    if not conservative_discovery.tail_robustness_passed:
        blockers.append("DISCOVERY_30BP_TAIL_ROBUSTNESS_FAILED")
    if not conservative_discovery.comparison.risk_guard_passed:
        blockers.append("DISCOVERY_30BP_DRAWDOWN_GUARD_FAILED")
    return tuple(blockers)


def _cohort_slice_summary(
    value: OpeningPolicyCohortSlice,
) -> dict[str, object]:
    return {
        "resolved_sessions": value.resolved_sessions,
        "execution_displacement_sessions": (
            value.execution_displacement_sessions
        ),
        "cumulative_delta_bps": value.comparison.cumulative_delta_bps,
        "max_drawdown_delta_bps": (
            value.comparison.max_drawdown_delta_bps
        ),
        "risk_guard_passed": value.comparison.risk_guard_passed,
        "tail_robustness_available": value.tail_robustness_available,
        "tail_robustness_passed": value.tail_robustness_passed,
        "tail_delta_bps": (
            value.cohort.cumulative_without_best_3_bps
            - value.baseline.cumulative_without_best_3_bps
        ),
    }


def _universe_subset_selection_payload(
    reports_by_symbols: Mapping[
        tuple[str, ...],
        Sequence[OpeningPolicyCohortReport],
    ],
    *,
    selection_version: str,
    symbols_key: str,
    selected_symbols_key: str,
) -> dict[str, object]:
    """Select a universe change using discovery data and cost stress only."""

    if not reports_by_symbols:
        raise ValueError("at least one cohort subset report is required")
    evaluated: list[dict[str, object]] = []
    eligible: list[
        tuple[
            float,
            float,
            float,
            tuple[str, ...],
            dict[str, object],
        ]
    ] = []
    seen: set[tuple[str, ...]] = set()
    for raw_symbols, raw_reports in reports_by_symbols.items():
        symbols = tuple(value.strip().upper() for value in raw_symbols)
        if (
            not symbols
            or any(not value for value in symbols)
            or len(symbols) != len(set(symbols))
        ):
            raise ValueError(
                "cohort subset keys must contain unique non-empty symbols"
            )
        if symbols in seen:
            raise ValueError("cohort subset keys must be unique")
        seen.add(symbols)
        reports = tuple(raw_reports)
        if not reports:
            raise ValueError("each cohort subset requires cost reports")
        if any(report.cohort_symbols != symbols for report in reports):
            raise ValueError(
                "cohort subset report symbols must match the mapping key"
            )
        costs = [report.round_trip_cost_bps for report in reports]
        if len(costs) != len(set(costs)):
            raise ValueError("cohort subset report costs must be unique")
        by_cost = {report.round_trip_cost_bps: report for report in reports}
        if 14.0 not in by_cost or 30.0 not in by_cost:
            raise ValueError(
                "cohort subset selection requires 14bp and 30bp reports"
            )
        primary = by_cost[14.0]
        conservative = by_cost[30.0]
        discovery = _cohort_slice(primary, "DISCOVERY")
        holdout = _cohort_slice(primary, "HOLDOUT")
        conservative_discovery = _cohort_slice(
            conservative,
            "DISCOVERY",
        )
        blockers = _cohort_subset_blockers(primary, conservative)
        payload: dict[str, object] = {
            symbols_key: list(symbols),
            "status": "ELIGIBLE" if not blockers else "REJECTED",
            "selection_blockers": list(blockers),
            "primary_cost_bps": primary.round_trip_cost_bps,
            "conservative_cost_bps": conservative.round_trip_cost_bps,
            "discovery": _cohort_slice_summary(discovery),
            "conservative_discovery": _cohort_slice_summary(
                conservative_discovery
            ),
            # Holdout is disclosed for audit, but never enters blockers or rank.
            "holdout_diagnostic": _cohort_slice_summary(holdout),
        }
        evaluated.append(payload)
        if not blockers:
            eligible.append((
                conservative_discovery.comparison.cumulative_delta_bps,
                discovery.comparison.cumulative_delta_bps,
                (
                    conservative_discovery.cohort
                    .cumulative_without_best_3_bps
                    - conservative_discovery.baseline
                    .cumulative_without_best_3_bps
                ),
                symbols,
                payload,
            ))

    evaluated.sort(
        key=lambda value: (
            len(cast(list[str], value[symbols_key])),
            cast(list[str], value[symbols_key]),
        )
    )
    eligible.sort(
        key=lambda value: (
            -value[0],
            -value[1],
            -value[2],
            len(value[3]),
            value[3],
        )
    )
    selected = eligible[0][4] if eligible else None
    return {
        "selection_version": selection_version,
        "selection_uses_holdout": False,
        "maximum_candidate_symbols": _COHORT_SUBSET_MAX_CANDIDATES,
        "minimum_execution_displacements": (
            _COHORT_SUBSET_MINIMUM_EXECUTION_DISPLACEMENTS
        ),
        "evaluated_subset_count": len(evaluated),
        "eligible_subset_count": len(eligible),
        "status": "SHADOW_CANDIDATE" if selected else "REJECTED",
        selected_symbols_key: (
            cast(list[str], selected[symbols_key]) if selected else []
        ),
        "selection_blockers": (
            [] if selected else ["NO_DISCOVERY_ROBUST_SUBSET"]
        ),
        "selected": selected,
        "subsets": evaluated,
        "diagnostic_only": True,
        "automatic_promotion_allowed": False,
    }


def _cohort_subset_selection_payload(
    reports_by_symbols: Mapping[
        tuple[str, ...],
        Sequence[OpeningPolicyCohortReport],
    ],
) -> dict[str, object]:
    """Select a joint addition cohort using discovery data only."""

    return _universe_subset_selection_payload(
        reports_by_symbols,
        selection_version=_COHORT_SUBSET_SELECTION_VERSION,
        symbols_key="symbols",
        selected_symbols_key="selected_symbols",
    )


def _cohort_individual_screen_payload(
    reports_by_symbol: Mapping[
        str,
        Sequence[OpeningPolicyCohortReport],
    ],
    *,
    coverage_failures: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Rank individually robust additions without consulting holdout returns."""

    normalized_reports: dict[
        str,
        tuple[OpeningPolicyCohortReport, ...],
    ] = {}
    for raw_symbol, raw_reports in reports_by_symbol.items():
        symbol = raw_symbol.strip().upper()
        if not symbol or not symbol.endswith(".US"):
            raise ValueError(
                "individual screen symbols must be non-empty .US symbols"
            )
        if symbol in normalized_reports:
            raise ValueError("individual screen symbols must be unique")
        normalized_reports[symbol] = tuple(raw_reports)

    normalized_failures: dict[str, str] = {}
    for raw_symbol, raw_reason in (coverage_failures or {}).items():
        symbol = raw_symbol.strip().upper()
        reason = raw_reason.strip().upper()
        if not symbol or not symbol.endswith(".US"):
            raise ValueError(
                "individual screen failure symbols must be non-empty .US symbols"
            )
        if not reason:
            raise ValueError(
                "individual screen coverage failure reasons must be non-empty"
            )
        if symbol in normalized_failures:
            raise ValueError("individual screen failure symbols must be unique")
        normalized_failures[symbol] = reason

    overlap = set(normalized_reports).intersection(normalized_failures)
    if overlap:
        raise ValueError(
            "individual screen symbols cannot be both evaluated and uncovered"
        )
    if not normalized_reports and not normalized_failures:
        raise ValueError("at least one individual screen symbol is required")

    evaluated_by_symbol: dict[str, dict[str, object]] = {}
    if normalized_reports:
        selection = _cohort_subset_selection_payload({
            (symbol,): reports
            for symbol, reports in normalized_reports.items()
        })
        for raw_candidate in cast(
            list[dict[str, object]],
            selection["subsets"],
        ):
            symbols = cast(list[str], raw_candidate["symbols"])
            if len(symbols) != 1:
                raise ValueError(
                    "individual screen reports must contain singleton cohorts"
                )
            symbol = symbols[0]
            candidate = {
                key: value
                for key, value in raw_candidate.items()
                if key != "symbols"
            }
            candidate.update({
                "symbol": symbol,
                "coverage_status": "PAIRED_DISCOVERY_AND_HOLDOUT",
                "coverage_reason": None,
                "discovery_rank": None,
                "shortlisted": False,
            })
            evaluated_by_symbol[symbol] = candidate

    eligible_symbols = [
        symbol
        for symbol, candidate in evaluated_by_symbol.items()
        if candidate["status"] == "ELIGIBLE"
    ]

    def discovery_rank_key(
        symbol: str,
    ) -> tuple[float, float, float, str]:
        by_cost = {
            value.round_trip_cost_bps: value
            for value in normalized_reports[symbol]
        }
        primary_discovery = _cohort_slice(by_cost[14.0], "DISCOVERY")
        conservative_discovery = _cohort_slice(
            by_cost[30.0],
            "DISCOVERY",
        )
        conservative_tail_delta = (
            conservative_discovery.cohort.cumulative_without_best_3_bps
            - conservative_discovery.baseline.cumulative_without_best_3_bps
        )
        return (
            -conservative_discovery.comparison.cumulative_delta_bps,
            -primary_discovery.comparison.cumulative_delta_bps,
            -conservative_tail_delta,
            symbol,
        )

    eligible_symbols.sort(key=discovery_rank_key)
    discovery_shortlist = eligible_symbols[
        :_COHORT_SUBSET_MAX_CANDIDATES
    ]
    shortlisted = set(discovery_shortlist)
    for rank, symbol in enumerate(eligible_symbols, start=1):
        candidate = evaluated_by_symbol[symbol]
        candidate["discovery_rank"] = rank
        candidate["shortlisted"] = symbol in shortlisted

    candidates = list(evaluated_by_symbol.values())
    candidates.extend(
        {
            "symbol": symbol,
            "coverage_status": "NO_PAIRED_COVERAGE",
            "coverage_reason": reason,
            "status": "REJECTED",
            "selection_blockers": ["NO_PAIRED_COVERAGE"],
            "discovery_rank": None,
            "shortlisted": False,
        }
        for symbol, reason in normalized_failures.items()
    )

    def candidate_sort_key(
        candidate: dict[str, object],
    ) -> tuple[int, int, str]:
        rank = candidate["discovery_rank"]
        symbol = cast(str, candidate["symbol"])
        if isinstance(rank, int):
            return (0, rank, symbol)
        return (1, 0, symbol)

    candidates.sort(key=candidate_sort_key)
    return {
        "screen_version": _COHORT_INDIVIDUAL_SCREEN_VERSION,
        "selection_uses_holdout": False,
        "ranking_fields": [
            "DISCOVERY_30BP_CUMULATIVE_DELTA_BPS",
            "DISCOVERY_14BP_CUMULATIVE_DELTA_BPS",
            "DISCOVERY_30BP_TAIL_DELTA_BPS",
            "SYMBOL",
        ],
        "minimum_execution_displacements": (
            _COHORT_SUBSET_MINIMUM_EXECUTION_DISPLACEMENTS
        ),
        "shortlist_limit": _COHORT_SUBSET_MAX_CANDIDATES,
        "screened_symbol_count": len(candidates),
        "evaluable_symbol_count": len(normalized_reports),
        "no_paired_coverage_count": len(normalized_failures),
        "eligible_symbol_count": len(eligible_symbols),
        "status": (
            "DISCOVERY_SHORTLIST_AVAILABLE"
            if discovery_shortlist
            else "REJECTED"
        ),
        "discovery_shortlist": discovery_shortlist,
        "selection_blockers": (
            []
            if discovery_shortlist
            else ["NO_DISCOVERY_ROBUST_SYMBOL"]
        ),
        "candidates": candidates,
        "diagnostic_only": True,
        "automatic_promotion_allowed": False,
    }


def _exclusion_subset_selection_payload(
    reports_by_symbols: Mapping[
        tuple[str, ...],
        Sequence[OpeningPolicyCohortReport],
    ],
) -> dict[str, object]:
    """Select a joint exclusion set using discovery data only."""

    return _universe_subset_selection_payload(
        reports_by_symbols,
        selection_version=_EXCLUSION_SUBSET_SELECTION_VERSION,
        symbols_key="excluded_symbols",
        selected_symbols_key="selected_excluded_symbols",
    )


def _horizon_slice(
    result: OpeningPolicyHorizonResult,
    name: str,
) -> OpeningPolicyHorizonSlice:
    return next(value for value in result.slices if value.name == name)


def _horizon_result(
    report: OpeningPolicyHorizonReport,
    holding_minutes: int,
) -> OpeningPolicyHorizonResult:
    return next(
        value
        for value in report.results
        if value.holding_minutes == holding_minutes
    )


def _horizon_blockers(
    result: OpeningPolicyHorizonResult,
    *,
    conservative_result: OpeningPolicyHorizonResult,
) -> tuple[str, ...]:
    discovery = _horizon_slice(result, "DISCOVERY")
    holdout = _horizon_slice(result, "HOLDOUT")
    conservative_holdout = _horizon_slice(
        conservative_result,
        "HOLDOUT",
    )
    blockers: list[str] = []
    if discovery.resolved_sessions < 20:
        blockers.append("DISCOVERY_SESSIONS_BELOW_20")
    if discovery.comparison.cumulative_delta_bps <= 0:
        blockers.append("DISCOVERY_DELTA_NOT_POSITIVE")
    if not discovery.tail_robustness_passed:
        blockers.append("DISCOVERY_TAIL_ROBUSTNESS_FAILED")
    if not discovery.comparison.risk_guard_passed:
        blockers.append("DISCOVERY_DRAWDOWN_GUARD_FAILED")
    if holdout.resolved_sessions < 20:
        blockers.append("HOLDOUT_SESSIONS_BELOW_20")
    if holdout.comparison.cumulative_delta_bps <= 0:
        blockers.append("HOLDOUT_DELTA_NOT_POSITIVE")
    if not holdout.tail_robustness_passed:
        blockers.append("HOLDOUT_TAIL_ROBUSTNESS_FAILED")
    if not holdout.comparison.risk_guard_passed:
        blockers.append("HOLDOUT_DRAWDOWN_GUARD_FAILED")
    if conservative_holdout.comparison.cumulative_delta_bps <= 0:
        blockers.append("HOLDOUT_30BP_COST_STRESS_FAILED")
    if not conservative_holdout.tail_robustness_passed:
        blockers.append("HOLDOUT_30BP_TAIL_ROBUSTNESS_FAILED")
    if not conservative_holdout.comparison.risk_guard_passed:
        blockers.append("HOLDOUT_30BP_DRAWDOWN_GUARD_FAILED")
    return tuple(blockers)


def _compact_horizon_payload(
    report: OpeningPolicyHorizonReport,
    *,
    cost_stress_reports: Sequence[OpeningPolicyHorizonReport],
) -> dict[str, object]:
    conservative = max(
        cost_stress_reports,
        key=lambda value: value.round_trip_cost_bps,
    )
    results: list[dict[str, object]] = []
    for result in report.results:
        discovery = _horizon_slice(result, "DISCOVERY")
        holdout = _horizon_slice(result, "HOLDOUT")
        conservative_result = _horizon_result(
            conservative,
            result.holding_minutes,
        )
        blockers = _horizon_blockers(
            result,
            conservative_result=conservative_result,
        )
        status = "REJECTED"
        if not blockers:
            confidence_lower = holdout.comparison.confidence_lower_bps
            status = (
                "HISTORICALLY_ROBUST"
                if confidence_lower is not None and confidence_lower > 0
                else "SHADOW_CANDIDATE"
            )
        results.append({
            "holding_minutes": result.holding_minutes,
            "status": status,
            "promotion_blockers": list(blockers),
            "discovery": discovery.to_dict(),
            "holdout": holdout.to_dict(),
        })
    return {
        "algorithm_version": report.algorithm_version,
        "policy": asdict(report.policy),
        "baseline_holding_minutes": report.baseline_holding_minutes,
        "sources": [asdict(value) for value in report.sources],
        "paired_sessions": report.paired_sessions,
        "discovery_sessions": report.discovery_sessions,
        "holdout_sessions": report.holdout_sessions,
        "discovery_end_date": report.discovery_end_date.isoformat(),
        "results": results,
        "diagnostic_only": report.diagnostic_only,
        "automatic_promotion_allowed": (
            report.automatic_promotion_allowed
        ),
    }


def _horizon_cost_stress_payload(
    reports: Sequence[OpeningPolicyHorizonReport],
) -> list[dict[str, object]]:
    return [
        {
            "round_trip_cost_bps": report.round_trip_cost_bps,
            "holdout": [
                {
                    "holding_minutes": result.holding_minutes,
                    "slice": _horizon_slice(
                        result,
                        "HOLDOUT",
                    ).to_dict(),
                }
                for result in report.results
            ],
        }
        for report in reports
    ]


def _parse_holding_horizons(value: str) -> tuple[int, ...]:
    raw_values = tuple(part.strip() for part in value.split(","))
    if not raw_values or any(not part for part in raw_values):
        raise ValueError(
            "holding_horizons must contain comma-separated integers"
        )
    try:
        values = tuple(int(part) for part in raw_values)
    except ValueError as exc:
        raise ValueError(
            "holding_horizons must contain comma-separated integers"
        ) from exc
    if len(values) != len(set(values)):
        raise ValueError("holding_horizons must contain unique values")
    if any(not 1 <= value <= 120 for value in values):
        raise ValueError("holding_horizons must be in [1, 120]")
    baseline = opening_execution_config().holding_minutes
    if baseline in values:
        raise ValueError(
            "holding_horizons must not include the production baseline"
        )
    return tuple(sorted(values))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Diagnose frozen opening-momentum post-signal gates on a "
            "chronological discovery/holdout split."
        )
    )
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument(
        "--baseline-symbols",
        help=(
            "optional frozen comma-separated baseline; defaults to current "
            "opening-execution eligible DB rows"
        ),
    )
    parser.add_argument(
        "--cohort-symbols",
        help=(
            "optional frozen comma-separated additions evaluated jointly "
            "against the baseline under --paired-policy"
        ),
    )
    parser.add_argument(
        "--select-cohort-subset",
        action="store_true",
        help=(
            "exhaustively select a discovery-only joint subset from up to "
            "six --cohort-symbols with 30bp cost and drawdown guards"
        ),
    )
    parser.add_argument(
        "--screen-cohort-symbols",
        help=(
            "optional comma-separated additions screened one at a time under "
            "--paired-policy; discovery-only ranking supports a full index "
            "catalog and never promotes automatically"
        ),
    )
    parser.add_argument(
        "--exclusion-symbols",
        help=(
            "optional frozen comma-separated baseline symbols removed "
            "jointly under --paired-policy"
        ),
    )
    parser.add_argument(
        "--select-exclusion-subset",
        action="store_true",
        help=(
            "exhaustively select a discovery-only exclusion subset from "
            "up to six --exclusion-symbols with 30bp cost and drawdown guards"
        ),
    )
    parser.add_argument(
        "--holding-horizons",
        help=(
            "optional comma-separated fixed holding-minute challengers; "
            "paired against the 60-minute baseline under --paired-policy "
            "with the same signal timing and stop"
        ),
    )
    parser.add_argument(
        "--paired-policy",
        choices=tuple(_PAIRED_POLICY_NAMES),
        default="production",
        help=(
            "policy used for cohort, individual screen, exclusion, and "
            "holding-horizon paired diagnostics (default: production)"
        ),
    )
    parser.add_argument("--discovery-ratio", type=float, default=0.60)
    parser.add_argument(
        "--minimum-data-coverage",
        type=float,
        default=_DEFAULT_MINIMUM_DATA_COVERAGE,
    )
    parser.add_argument("--cache-path")
    parser.add_argument(
        "--seed-cache-path",
        help=(
            "optional immutable cache with the same start date and an earlier "
            "end date; used only when --cache-path does not yet exist, with "
            "the final seven calendar days refreshed to absorb vendor data "
            "revisions before fetching the missing range"
        ),
    )
    parser.add_argument("--output")
    parser.add_argument(
        "--full",
        action="store_true",
        help="print the complete policy grid instead of the compact summary",
    )
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        start_date = _parse_date(args.start_date, field_name="start_date")
        end_date = _parse_date(args.end_date, field_name="end_date")
        if end_date < start_date:
            raise ValueError("end_date must not precede start_date")
        if not 0 < args.discovery_ratio < 1:
            raise ValueError("discovery_ratio must be in (0, 1)")
        if not 0 < args.minimum_data_coverage <= 1:
            raise ValueError("minimum_data_coverage must be in (0, 1]")
        baseline_symbols = (
            _parse_symbols(
                args.baseline_symbols,
                field_name="baseline_symbols",
            )
            if args.baseline_symbols
            else _load_current_baseline_symbols()
        )
        cohort_symbols = (
            _parse_symbols(
                args.cohort_symbols,
                field_name="cohort_symbols",
            )
            if args.cohort_symbols
            else ()
        )
        screen_cohort_symbols = (
            _parse_symbols(
                args.screen_cohort_symbols,
                field_name="screen_cohort_symbols",
            )
            if args.screen_cohort_symbols
            else ()
        )
        exclusion_symbols = (
            _parse_symbols(
                args.exclusion_symbols,
                field_name="exclusion_symbols",
            )
            if args.exclusion_symbols
            else ()
        )
        holding_horizons = (
            _parse_holding_horizons(args.holding_horizons)
            if args.holding_horizons
            else ()
        )
        if args.select_cohort_subset and not cohort_symbols:
            raise ValueError(
                "select_cohort_subset requires cohort_symbols"
            )
        if (
            args.select_cohort_subset
            and len(cohort_symbols) > _COHORT_SUBSET_MAX_CANDIDATES
        ):
            raise ValueError(
                "select_cohort_subset supports at most "
                f"{_COHORT_SUBSET_MAX_CANDIDATES} cohort symbols"
            )
        if args.select_exclusion_subset and not exclusion_symbols:
            raise ValueError(
                "select_exclusion_subset requires exclusion_symbols"
            )
        if (
            args.select_exclusion_subset
            and len(exclusion_symbols) > _COHORT_SUBSET_MAX_CANDIDATES
        ):
            raise ValueError(
                "select_exclusion_subset supports at most "
                f"{_COHORT_SUBSET_MAX_CANDIDATES} exclusion symbols"
            )
        if sum((
            bool(cohort_symbols),
            bool(screen_cohort_symbols),
            bool(exclusion_symbols),
        )) > 1:
            raise ValueError(
                "cohort_symbols, screen_cohort_symbols, and exclusion_symbols "
                "are mutually exclusive"
            )
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))

    config = opening_execution_config()
    overlap = set(baseline_symbols).intersection(cohort_symbols)
    if overlap:
        parser.error(
            "cohort symbols already exist in baseline: "
            + ", ".join(sorted(overlap))
        )
    screen_overlap = set(baseline_symbols).intersection(
        screen_cohort_symbols
    )
    if screen_overlap:
        parser.error(
            "screen cohort symbols already exist in baseline: "
            + ", ".join(sorted(screen_overlap))
        )
    unknown_exclusions = set(exclusion_symbols).difference(
        baseline_symbols
    )
    if unknown_exclusions:
        parser.error(
            "exclusion symbols are outside the baseline: "
            + ", ".join(sorted(unknown_exclusions))
        )
    if exclusion_symbols and (
        len(baseline_symbols) - len(exclusion_symbols)
        < config.minimum_universe_size
    ):
        parser.error(
            "exclusions leave fewer symbols than the minimum universe size"
        )

    maximum_holding_minutes = max(
        (config.holding_minutes, *holding_horizons)
    )
    maximum_offset = (
        config.signal_minutes
        + config.execution_delay_minutes
        + maximum_holding_minutes
    )
    cache_path = Path(args.cache_path) if args.cache_path else Path(
        "data/research/"
        f"opening-extension-{start_date.isoformat()}-{end_date.isoformat()}"
        "-ohlc-v3.json.gz"
    )
    seed_cache_path = (
        Path(args.seed_cache_path) if args.seed_cache_path else None
    )
    if (
        seed_cache_path is not None
        and seed_cache_path.resolve() == cache_path.resolve()
    ):
        parser.error("seed_cache_path must differ from cache_path")
    cache_preexisting = cache_path.exists()
    seed_cache_used: Path | None = None
    incremental_fetch_start_date: date | None = None
    try:
        if cache_preexisting:
            bars_by_symbol = _load_cache(
                cache_path,
                start_date=start_date,
                end_date=end_date,
                retained_minutes_after_open=maximum_offset,
            )
        elif seed_cache_path is not None:
            bars_by_symbol, seed_end_date = _load_seed_cache(
                seed_cache_path,
                start_date=start_date,
                end_date=end_date,
                retained_minutes_after_open=maximum_offset,
            )
            seed_cache_used = seed_cache_path
            incremental_fetch_start_date = max(
                start_date,
                seed_end_date - timedelta(days=_CACHE_REFRESH_OVERLAP_DAYS),
            )
        else:
            bars_by_symbol = {}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"failed to load research cache: {exc}")

    all_symbols = tuple(dict.fromkeys((
        *baseline_symbols,
        *cohort_symbols,
        *screen_cohort_symbols,
    )))
    if seed_cache_used is not None:
        bars_by_symbol = {
            symbol: bars_by_symbol[symbol]
            for symbol in all_symbols
            if bars_by_symbol.get(symbol)
        }
    if incremental_fetch_start_date is not None:
        fetch_plan = tuple(
            (
                symbol,
                incremental_fetch_start_date
                if bars_by_symbol.get(symbol)
                else start_date,
            )
            for symbol in all_symbols
        )
    else:
        fetch_plan = tuple(
            (symbol, start_date)
            for symbol in all_symbols
            if not bars_by_symbol.get(symbol)
        )
    broker: BrokerGateway | None = None
    if fetch_plan:
        try:
            _configure_longport_environment()
            broker = BrokerGateway()
            for index, (symbol, fetch_start_date) in enumerate(
                fetch_plan,
                start=1,
            ):
                print(
                    f"minute history {index}/{len(fetch_plan)} {symbol}",
                    file=sys.stderr,
                    flush=True,
                )
                fetched_bars = _fetch_symbol_bars(
                    broker,
                    symbol,
                    start_date=fetch_start_date,
                    end_date=end_date,
                    retained_minutes_after_open=maximum_offset,
                )
                existing_bars = bars_by_symbol.get(symbol, ())
                bars_by_symbol[symbol] = (
                    _replace_bar_date_range(
                        existing_bars,
                        fetched_bars,
                        start_date=fetch_start_date,
                        end_date=end_date,
                    )
                    if incremental_fetch_start_date is not None
                    and existing_bars
                    else _merge_bars(existing_bars, fetched_bars)
                )
                if (
                    incremental_fetch_start_date is None
                    and (index % 5 == 0 or index == len(fetch_plan))
                ):
                    _save_cache(
                        cache_path,
                        bars_by_symbol,
                        start_date=start_date,
                        end_date=end_date,
                        retained_minutes_after_open=maximum_offset,
                    )
            if incremental_fetch_start_date is not None:
                _save_cache(
                    cache_path,
                    bars_by_symbol,
                    start_date=start_date,
                    end_date=end_date,
                    retained_minutes_after_open=maximum_offset,
                )
        finally:
            if broker is not None:
                broker.close()

    cache_update_mode = (
        "SEEDED_INCREMENTAL_WITH_OVERLAP"
        if seed_cache_used is not None
        else "EXACT_OR_FILL_MISSING"
        if cache_preexisting
        else "FULL_RANGE_FETCH"
    )

    session_dates = _baseline_session_dates(
        bars_by_symbol,
        baseline_symbols=baseline_symbols,
        minimum_universe_size=config.minimum_universe_size,
        minimum_data_coverage=args.minimum_data_coverage,
    )
    policy_sessions = _build_policy_sessions(
        bars_by_symbol,
        symbols=baseline_symbols,
        session_dates=session_dates,
        minimum_data_coverage=args.minimum_data_coverage,
    )
    if len(policy_sessions) < 2:
        parser.error("fewer than two causally resolved sessions were found")

    policies = _default_policy_specs()
    paired_policy = _resolve_paired_policy(policies, args.paired_policy)
    report = evaluate_opening_policy_grid(
        policy_sessions,
        policies=policies,
        round_trip_cost_bps=config.round_trip_cost_bps,
        discovery_ratio=args.discovery_ratio,
    )
    cohort_report: OpeningPolicyCohortReport | None = None
    cohort_cost_stress: list[dict[str, object]] = []
    cohort_subset_selection: dict[str, object] | None = None
    if cohort_symbols:
        cohort_policy_sessions = _build_policy_sessions(
            bars_by_symbol,
            symbols=all_symbols,
            required_symbols=cohort_symbols,
            session_dates=session_dates,
            minimum_data_coverage=args.minimum_data_coverage,
        )
        if len(cohort_policy_sessions) < 2:
            parser.error(
                "fewer than two causally resolved cohort sessions were found"
            )
        cohort_reports = tuple(
            evaluate_opening_policy_cohort(
                policy_sessions,
                cohort_policy_sessions,
                policy=paired_policy,
                cohort_symbols=cohort_symbols,
                round_trip_cost_bps=cost,
                discovery_ratio=args.discovery_ratio,
            )
            for cost in _COHORT_COST_STRESS_BPS
        )
        cohort_report = cohort_reports[0]
        cohort_cost_stress = [
            {
                "round_trip_cost_bps": value.round_trip_cost_bps,
                "holdout": _cohort_slice(
                    value,
                    "HOLDOUT",
                ).to_dict(),
            }
            for value in cohort_reports
        ]
        if args.select_cohort_subset:
            reports_by_symbols: dict[
                tuple[str, ...],
                tuple[OpeningPolicyCohortReport, ...],
            ] = {}
            for subset_size in range(1, len(cohort_symbols) + 1):
                for subset in combinations(cohort_symbols, subset_size):
                    subset_sessions = (
                        cohort_policy_sessions
                        if subset == cohort_symbols
                        else _build_policy_sessions(
                            bars_by_symbol,
                            symbols=tuple(dict.fromkeys(
                                (*baseline_symbols, *subset)
                            )),
                            required_symbols=subset,
                            session_dates=session_dates,
                            minimum_data_coverage=(
                                args.minimum_data_coverage
                            ),
                        )
                    )
                    if len(subset_sessions) < 2:
                        parser.error(
                            "fewer than two causally resolved sessions were "
                            "found for cohort subset " + ",".join(subset)
                        )
                    reports_by_symbols[subset] = tuple(
                        evaluate_opening_policy_cohort(
                            policy_sessions,
                            subset_sessions,
                            policy=paired_policy,
                            cohort_symbols=subset,
                            round_trip_cost_bps=cost,
                            discovery_ratio=args.discovery_ratio,
                        )
                        for cost in _COHORT_COST_STRESS_BPS
                    )
            cohort_subset_selection = (
                _cohort_subset_selection_payload(reports_by_symbols)
            )
    cohort_individual_screen: dict[str, object] | None = None
    if screen_cohort_symbols:
        baseline_dates = {
            value.session_date for value in policy_sessions
        }
        screen_reports: dict[
            str,
            tuple[OpeningPolicyCohortReport, ...],
        ] = {}
        coverage_failures: dict[str, str] = {}
        for symbol in screen_cohort_symbols:
            candidate_sessions = _build_policy_sessions(
                bars_by_symbol,
                symbols=tuple(dict.fromkeys((*baseline_symbols, symbol))),
                required_symbols=(symbol,),
                session_dates=session_dates,
                minimum_data_coverage=args.minimum_data_coverage,
            )
            paired_dates = sorted(
                baseline_dates.intersection(
                    value.session_date for value in candidate_sessions
                )
            )
            if len(paired_dates) < 2:
                coverage_failures[symbol] = (
                    "FEWER_THAN_TWO_PAIRED_SESSIONS"
                )
                continue
            if not any(
                value <= report.discovery_end_date
                for value in paired_dates
            ):
                coverage_failures[symbol] = (
                    "MISSING_BASELINE_DISCOVERY_COVERAGE"
                )
                continue
            if not any(
                value > report.discovery_end_date
                for value in paired_dates
            ):
                coverage_failures[symbol] = (
                    "MISSING_BASELINE_HOLDOUT_COVERAGE"
                )
                continue
            screen_reports[symbol] = tuple(
                evaluate_opening_policy_cohort(
                    policy_sessions,
                    candidate_sessions,
                    policy=paired_policy,
                    cohort_symbols=(symbol,),
                    round_trip_cost_bps=cost,
                    discovery_ratio=args.discovery_ratio,
                )
                for cost in _COHORT_COST_STRESS_BPS
            )
        cohort_individual_screen = _cohort_individual_screen_payload(
            screen_reports,
            coverage_failures=coverage_failures,
        )
    exclusion_report: OpeningPolicyCohortReport | None = None
    exclusion_cost_stress: list[dict[str, object]] = []
    exclusion_subset_selection: dict[str, object] | None = None
    if exclusion_symbols:
        exclusion_set = set(exclusion_symbols)
        reduced_symbols = tuple(
            symbol
            for symbol in baseline_symbols
            if symbol not in exclusion_set
        )
        reduced_policy_sessions = _build_policy_sessions(
            bars_by_symbol,
            symbols=reduced_symbols,
            session_dates=session_dates,
            minimum_data_coverage=args.minimum_data_coverage,
        )
        if len(reduced_policy_sessions) < 2:
            parser.error(
                "fewer than two causally resolved exclusion sessions were found"
            )
        exclusion_reports = tuple(
            evaluate_opening_policy_cohort(
                policy_sessions,
                reduced_policy_sessions,
                policy=paired_policy,
                cohort_symbols=exclusion_symbols,
                round_trip_cost_bps=cost,
                discovery_ratio=args.discovery_ratio,
            )
            for cost in _COHORT_COST_STRESS_BPS
        )
        exclusion_report = exclusion_reports[0]
        exclusion_cost_stress = [
            {
                "round_trip_cost_bps": value.round_trip_cost_bps,
                "discovery": _exclusion_slice_payload(
                    _cohort_slice(value, "DISCOVERY")
                ),
                "holdout": _exclusion_slice_payload(
                    _cohort_slice(value, "HOLDOUT")
                ),
            }
            for value in exclusion_reports
        ]
        if args.select_exclusion_subset:
            exclusion_reports_by_symbols: dict[
                tuple[str, ...],
                tuple[OpeningPolicyCohortReport, ...],
            ] = {}
            for subset_size in range(1, len(exclusion_symbols) + 1):
                for subset in combinations(
                    exclusion_symbols,
                    subset_size,
                ):
                    subset_set = set(subset)
                    subset_reduced_symbols = tuple(
                        symbol
                        for symbol in baseline_symbols
                        if symbol not in subset_set
                    )
                    subset_sessions = (
                        reduced_policy_sessions
                        if subset == exclusion_symbols
                        else _build_policy_sessions(
                            bars_by_symbol,
                            symbols=subset_reduced_symbols,
                            session_dates=session_dates,
                            minimum_data_coverage=(
                                args.minimum_data_coverage
                            ),
                        )
                    )
                    if len(subset_sessions) < 2:
                        parser.error(
                            "fewer than two causally resolved sessions were "
                            "found for exclusion subset " + ",".join(subset)
                        )
                    exclusion_reports_by_symbols[subset] = tuple(
                        evaluate_opening_policy_cohort(
                            policy_sessions,
                            subset_sessions,
                            policy=paired_policy,
                            cohort_symbols=subset,
                            round_trip_cost_bps=cost,
                            discovery_ratio=args.discovery_ratio,
                        )
                        for cost in _COHORT_COST_STRESS_BPS
                    )
            exclusion_subset_selection = (
                _exclusion_subset_selection_payload(
                    exclusion_reports_by_symbols
                )
            )
    horizon_reports: tuple[OpeningPolicyHorizonReport, ...] = ()
    horizon_cost_stress: list[dict[str, object]] = []
    horizon_decisions: dict[str, object] | None = None
    if holding_horizons:
        sessions_by_horizon = {config.holding_minutes: policy_sessions}
        for holding_minutes in holding_horizons:
            horizon_config = replace(
                config,
                holding_minutes=holding_minutes,
            )
            horizon_sessions = _build_policy_sessions(
                bars_by_symbol,
                symbols=baseline_symbols,
                session_dates=session_dates,
                minimum_data_coverage=args.minimum_data_coverage,
                decision_config=horizon_config,
            )
            if len(horizon_sessions) < 2:
                parser.error(
                    "fewer than two causally resolved sessions were found "
                    f"for the {holding_minutes}-minute horizon"
                )
            sessions_by_horizon[holding_minutes] = horizon_sessions
        horizon_reports = tuple(
            evaluate_opening_policy_horizons(
                sessions_by_horizon,
                baseline_holding_minutes=config.holding_minutes,
                policy=paired_policy,
                round_trip_cost_bps=cost,
                discovery_ratio=args.discovery_ratio,
            )
            for cost in (14.0, 20.0, 30.0)
        )
        horizon_cost_stress = _horizon_cost_stress_payload(
            horizon_reports
        )
        horizon_decisions = _compact_horizon_payload(
            horizon_reports[0],
            cost_stress_reports=horizon_reports,
        )
    generated = datetime.now(timezone.utc)
    generated_at = generated.isoformat()
    full_payload: dict[str, object] = {
        "cli_version": OPENING_POLICY_CLI_VERSION,
        "algorithm_version": OPENING_POLICY_DIAGNOSTIC_VERSION,
        "generated_at": generated_at,
        "data_scope": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "period": "MIN_1",
            "adjustment": "NO_ADJUST",
            "candidate_session_count": len(session_dates),
            "resolved_session_count": len(policy_sessions),
            "cohort_resolved_session_count": (
                cohort_report.cohort_source_sessions
                if cohort_report is not None
                else None
            ),
            "cohort_subset_evaluated_count": (
                cohort_subset_selection["evaluated_subset_count"]
                if cohort_subset_selection is not None
                else None
            ),
            "cohort_individual_screen_symbol_count": (
                cohort_individual_screen["screened_symbol_count"]
                if cohort_individual_screen is not None
                else None
            ),
            "cohort_individual_screen_evaluable_count": (
                cohort_individual_screen["evaluable_symbol_count"]
                if cohort_individual_screen is not None
                else None
            ),
            "cohort_individual_screen_no_coverage_count": (
                cohort_individual_screen["no_paired_coverage_count"]
                if cohort_individual_screen is not None
                else None
            ),
            "exclusion_resolved_session_count": (
                exclusion_report.cohort_source_sessions
                if exclusion_report is not None
                else None
            ),
            "exclusion_subset_evaluated_count": (
                exclusion_subset_selection["evaluated_subset_count"]
                if exclusion_subset_selection is not None
                else None
            ),
            "holding_horizon_paired_session_count": (
                horizon_reports[0].paired_sessions
                if horizon_reports
                else None
            ),
            "cache_path": str(cache_path),
            "seed_cache_path": (
                str(seed_cache_used) if seed_cache_used is not None else None
            ),
            "incremental_fetch_start_date": (
                incremental_fetch_start_date.isoformat()
                if incremental_fetch_start_date is not None
                else None
            ),
            "cache_update_mode": cache_update_mode,
            "cache_refresh_overlap_days": (
                _CACHE_REFRESH_OVERLAP_DAYS
                if seed_cache_used is not None
                else 0
            ),
            "bar_counts": {
                symbol: len(bars_by_symbol.get(symbol, ()))
                for symbol in all_symbols
            },
        },
        "research_design": {
            "baseline_source": (
                "CLI_FROZEN_SYMBOLS"
                if args.baseline_symbols
                else "CURRENT_OPENING_EXECUTION_ELIGIBLE_STRATEGY_V2_CONFIG"
            ),
            "baseline_symbols": list(baseline_symbols),
            "cache_update_mode": cache_update_mode,
            "cache_refresh_overlap_days": (
                _CACHE_REFRESH_OVERLAP_DAYS
                if seed_cache_used is not None
                else 0
            ),
            "paired_policy_name": paired_policy.name,
            "cohort_symbols": list(cohort_symbols),
            "cohort_subset_selection_requested": bool(
                args.select_cohort_subset
            ),
            "cohort_subset_selection_uses_holdout": False,
            "cohort_individual_screen_symbols": list(
                screen_cohort_symbols
            ),
            "cohort_individual_screen_requested": bool(
                screen_cohort_symbols
            ),
            "cohort_individual_screen_uses_holdout": False,
            "exclusion_symbols": list(exclusion_symbols),
            "exclusion_subset_selection_requested": bool(
                args.select_exclusion_subset
            ),
            "exclusion_subset_selection_uses_holdout": False,
            "holding_horizons": list(holding_horizons),
            "signal_minutes": config.signal_minutes,
            "execution_delay_minutes": config.execution_delay_minutes,
            "holding_minutes": config.holding_minutes,
            "stop_loss_pct": config.stop_loss_pct,
            "round_trip_cost_bps": config.round_trip_cost_bps,
            "minimum_data_coverage": args.minimum_data_coverage,
            "discovery_ratio": args.discovery_ratio,
            "chronological_split": "BASELINE_DATE_ANCHORED",
            "discovery_end_date": report.discovery_end_date.isoformat(),
            "production_policy_precommitted": True,
            "sensitivity_grid_selection_allowed": False,
            "holding_horizon_selection_allowed": False,
            "automatic_promotion_allowed": False,
            "survivorship_bias": "CURRENT_BASELINE_SYMBOLS",
            "execution_price_approximation": (
                "DELAYED_MINUTE_BAR_OPEN_NOT_ACTUAL_BBO_OR_FILL"
            ),
        },
        "report": report.to_dict(),
        "cohort_diagnostic_version": (
            OPENING_POLICY_COHORT_DIAGNOSTIC_VERSION
            if cohort_report is not None
            else None
        ),
        "cohort_diagnostic": (
            cohort_report.to_dict()
            if cohort_report is not None
            else None
        ),
        "cohort_cost_stress": cohort_cost_stress,
        "cohort_subset_selection_version": (
            _COHORT_SUBSET_SELECTION_VERSION
            if cohort_subset_selection is not None
            else None
        ),
        "cohort_subset_selection": cohort_subset_selection,
        "cohort_individual_screen_version": (
            _COHORT_INDIVIDUAL_SCREEN_VERSION
            if cohort_individual_screen is not None
            else None
        ),
        "cohort_individual_screen": cohort_individual_screen,
        "exclusion_diagnostic_version": (
            _EXCLUSION_DIAGNOSTIC_VERSION
            if exclusion_report is not None
            else None
        ),
        "exclusion_diagnostic": (
            _exclusion_report_payload(exclusion_report)
            if exclusion_report is not None
            else None
        ),
        "exclusion_cost_stress": exclusion_cost_stress,
        "exclusion_subset_selection_version": (
            _EXCLUSION_SUBSET_SELECTION_VERSION
            if exclusion_subset_selection is not None
            else None
        ),
        "exclusion_subset_selection": exclusion_subset_selection,
        "holding_horizon_diagnostic_version": (
            OPENING_POLICY_HORIZON_DIAGNOSTIC_VERSION
            if horizon_reports
            else None
        ),
        "holding_horizon_diagnostic": (
            horizon_reports[0].to_dict()
            if horizon_reports
            else None
        ),
        "holding_horizon_cost_stress": horizon_cost_stress,
        "holding_horizon_decisions": horizon_decisions,
    }
    output_path = Path(args.output) if args.output else Path(
        "data/research/"
        "opening-policy-report-"
        f"{generated.strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output_path.with_name(f".{output_path.name}.tmp")
    temporary_output.write_text(
        json.dumps(
            full_payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary_output.replace(output_path)

    production = _result(report, PRODUCTION_POLICY_NAME)
    compact_payload = {
        "cli_version": OPENING_POLICY_CLI_VERSION,
        "generated_at": generated_at,
        "data_scope": full_payload["data_scope"],
        "production": _compact_policy_payload(production),
        "sensitivity": [
            _sensitivity_summary_payload(value)
            for value in report.policies
            if value.policy.name not in {
                "BROAD",
                PRODUCTION_POLICY_NAME,
            }
        ],
        "automatic_promotion_allowed": False,
        "full_report_path": str(output_path),
    }
    if cohort_report is not None:
        compact_payload["cohort"] = _compact_cohort_payload(
            cohort_report
        )
        compact_payload["cohort_cost_stress"] = cohort_cost_stress
    if cohort_subset_selection is not None:
        compact_payload["cohort_subset_selection"] = (
            cohort_subset_selection
        )
    if cohort_individual_screen is not None:
        compact_payload["cohort_individual_screen"] = (
            cohort_individual_screen
        )
    if exclusion_report is not None:
        compact_payload["exclusion"] = _compact_exclusion_payload(
            exclusion_report
        )
        compact_payload["exclusion_cost_stress"] = (
            exclusion_cost_stress
        )
    if exclusion_subset_selection is not None:
        compact_payload["exclusion_subset_selection"] = (
            exclusion_subset_selection
        )
    if horizon_reports:
        compact_payload["holding_horizons"] = horizon_decisions
        compact_payload["holding_horizon_cost_stress"] = (
            horizon_cost_stress
        )
    print(json.dumps(
        full_payload if args.full else compact_payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
