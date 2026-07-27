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
    _load_current_baseline_symbols,
    _parse_date,
    _parse_symbols,
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


OPENING_POLICY_CLI_VERSION = "opening-policy-research-cli-v4"
_DEFAULT_PATH_THRESHOLDS = (0.50, 0.60, 0.70, 0.80, 0.90)
_DEFAULT_MARKET_MAXIMUMS_BPS = (-10.0, -5.0, 0.0, 5.0, 10.0, 20.0)
_DEFAULT_MINIMUM_DATA_COVERAGE = 0.95
_COHORT_COST_STRESS_BPS = (14.0, 20.0, 30.0)
_COHORT_SUBSET_MAX_CANDIDATES = 6
_COHORT_SUBSET_MINIMUM_EXECUTION_DISPLACEMENTS = 4
_COHORT_SUBSET_SELECTION_VERSION = (
    "discovery-exhaustive-joint-subset-cost30-drawdown-v1"
)


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
    return tuple(policies)


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
        "discovery": discovery.to_dict(),
        "holdout": holdout.to_dict(),
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


def _cohort_subset_selection_payload(
    reports_by_symbols: Mapping[
        tuple[str, ...],
        Sequence[OpeningPolicyCohortReport],
    ],
) -> dict[str, object]:
    """Select a joint cohort using discovery data and cost stress only."""

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
            "symbols": list(symbols),
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
            len(cast(list[str], value["symbols"])),
            cast(list[str], value["symbols"]),
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
        "selection_version": _COHORT_SUBSET_SELECTION_VERSION,
        "selection_uses_holdout": False,
        "maximum_candidate_symbols": _COHORT_SUBSET_MAX_CANDIDATES,
        "minimum_execution_displacements": (
            _COHORT_SUBSET_MINIMUM_EXECUTION_DISPLACEMENTS
        ),
        "evaluated_subset_count": len(evaluated),
        "eligible_subset_count": len(eligible),
        "status": "SHADOW_CANDIDATE" if selected else "REJECTED",
        "selected_symbols": (
            cast(list[str], selected["symbols"]) if selected else []
        ),
        "selection_blockers": (
            [] if selected else ["NO_DISCOVERY_ROBUST_SUBSET"]
        ),
        "selected": selected,
        "subsets": evaluated,
        "diagnostic_only": True,
        "automatic_promotion_allowed": False,
    }


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
            "against the baseline under the production policy"
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
        "--holding-horizons",
        help=(
            "optional comma-separated fixed holding-minute challengers; "
            "paired against the 60-minute production baseline with the "
            "same signal, gates, and stop"
        ),
    )
    parser.add_argument("--discovery-ratio", type=float, default=0.60)
    parser.add_argument(
        "--minimum-data-coverage",
        type=float,
        default=_DEFAULT_MINIMUM_DATA_COVERAGE,
    )
    parser.add_argument("--cache-path")
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
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))

    overlap = set(baseline_symbols).intersection(cohort_symbols)
    if overlap:
        parser.error(
            "cohort symbols already exist in baseline: "
            + ", ".join(sorted(overlap))
        )

    config = opening_execution_config()
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
    try:
        bars_by_symbol = _load_cache(
            cache_path,
            start_date=start_date,
            end_date=end_date,
            retained_minutes_after_open=maximum_offset,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"failed to load research cache: {exc}")

    all_symbols = tuple(dict.fromkeys(
        (*baseline_symbols, *cohort_symbols)
    ))
    missing_symbols = tuple(
        symbol
        for symbol in all_symbols
        if not bars_by_symbol.get(symbol)
    )
    broker: BrokerGateway | None = None
    if missing_symbols:
        try:
            _configure_longport_environment()
            broker = BrokerGateway()
            for index, symbol in enumerate(missing_symbols, start=1):
                print(
                    f"minute history {index}/{len(missing_symbols)} {symbol}",
                    file=sys.stderr,
                    flush=True,
                )
                bars_by_symbol[symbol] = _fetch_symbol_bars(
                    broker,
                    symbol,
                    start_date=start_date,
                    end_date=end_date,
                    retained_minutes_after_open=maximum_offset,
                )
                if index % 5 == 0 or index == len(missing_symbols):
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
        production_policy = next(
            value
            for value in policies
            if value.name == PRODUCTION_POLICY_NAME
        )
        cohort_reports = tuple(
            evaluate_opening_policy_cohort(
                policy_sessions,
                cohort_policy_sessions,
                policy=production_policy,
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
                            policy=production_policy,
                            cohort_symbols=subset,
                            round_trip_cost_bps=cost,
                            discovery_ratio=args.discovery_ratio,
                        )
                        for cost in _COHORT_COST_STRESS_BPS
                    )
            cohort_subset_selection = (
                _cohort_subset_selection_payload(reports_by_symbols)
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
        production_policy = next(
            value
            for value in policies
            if value.name == PRODUCTION_POLICY_NAME
        )
        horizon_reports = tuple(
            evaluate_opening_policy_horizons(
                sessions_by_horizon,
                baseline_holding_minutes=config.holding_minutes,
                policy=production_policy,
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
            "holding_horizon_paired_session_count": (
                horizon_reports[0].paired_sessions
                if horizon_reports
                else None
            ),
            "cache_path": str(cache_path),
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
            "cohort_symbols": list(cohort_symbols),
            "cohort_subset_selection_requested": bool(
                args.select_cohort_subset
            ),
            "cohort_subset_selection_uses_holdout": False,
            "holding_horizons": list(holding_horizons),
            "signal_minutes": config.signal_minutes,
            "execution_delay_minutes": config.execution_delay_minutes,
            "holding_minutes": config.holding_minutes,
            "stop_loss_pct": config.stop_loss_pct,
            "round_trip_cost_bps": config.round_trip_cost_bps,
            "minimum_data_coverage": args.minimum_data_coverage,
            "discovery_ratio": args.discovery_ratio,
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
