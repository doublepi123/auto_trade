from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

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
    evaluate_opening_momentum,
    opening_path_efficiency,
    shadow_round_trip_return_bps,
)
from app.domain.opening_momentum_policy import (
    OPENING_POLICY_COHORT_DIAGNOSTIC_VERSION,
    OPENING_POLICY_DIAGNOSTIC_VERSION,
    PRODUCTION_MAXIMUM_MARKET_RETURN_BPS,
    PRODUCTION_MINIMUM_PATH_EFFICIENCY,
    PRODUCTION_POLICY_NAME,
    OpeningPolicyCohortReport,
    OpeningPolicyCohortSlice,
    OpeningPolicyDiagnosticReport,
    OpeningPolicyResult,
    OpeningPolicySession,
    OpeningPolicySlice,
    OpeningPolicySpec,
    evaluate_opening_policy_cohort,
    evaluate_opening_policy_grid,
    opening_execution_config,
)


OPENING_POLICY_CLI_VERSION = "opening-policy-research-cli-v2"
_DEFAULT_PATH_THRESHOLDS = (0.50, 0.60, 0.70, 0.80, 0.90)
_DEFAULT_MARKET_MAXIMUMS_BPS = (-10.0, -5.0, 0.0, 5.0, 10.0, 20.0)
_DEFAULT_MINIMUM_DATA_COVERAGE = 0.95


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
    config = opening_execution_config()
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
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))

    overlap = set(baseline_symbols).intersection(cohort_symbols)
    if overlap:
        parser.error(
            "cohort symbols already exist in baseline: "
            + ", ".join(sorted(overlap))
        )

    config = opening_execution_config()
    maximum_offset = (
        config.signal_minutes
        + config.execution_delay_minutes
        + config.holding_minutes
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
            for cost in (14.0, 20.0, 30.0)
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
            "signal_minutes": config.signal_minutes,
            "execution_delay_minutes": config.execution_delay_minutes,
            "holding_minutes": config.holding_minutes,
            "stop_loss_pct": config.stop_loss_pct,
            "round_trip_cost_bps": config.round_trip_cost_bps,
            "minimum_data_coverage": args.minimum_data_coverage,
            "discovery_ratio": args.discovery_ratio,
            "production_policy_precommitted": True,
            "sensitivity_grid_selection_allowed": False,
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
    print(json.dumps(
        full_payload if args.full else compact_payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
