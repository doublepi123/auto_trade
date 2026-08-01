#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.core.broker import BrokerGateway
from app.domain.universe_selection import (
    INDEX_MEMBERSHIP_HISTORY,
    INDEX_CANDIDATE_CATALOG,
    ROTATION_RESEARCH_CANDIDATE_CATALOG,
    ROTATION_BENCHMARK_SYMBOLS,
    DailyBar,
    IndexCandidate,
    completed_daily_bars,
    evaluate_rotation_walk_forward,
)
from app.services.universe_selection_service import (
    historical_membership_end,
    historical_research_before,
    historical_research_candlesticks,
    research_candidate_uses_recent_candlesticks,
    selection_config_from_settings,
)


_CLI_SCHEMA_VERSION = "rotation-walk-forward-research-cli-v2"
_PERFORMANCE_SUMMARY_FIELDS = (
    "periods",
    "annualized_return_pct",
    "sharpe",
    "max_drawdown_pct",
    "win_rate_pct",
    "average_turnover_pct",
    "total_cost_pct",
    "average_holdings",
    "qqq_annualized_return_pct",
    "qqq_sharpe",
    "qqq_max_drawdown_pct",
    "dia_annualized_return_pct",
    "dia_sharpe",
    "dia_max_drawdown_pct",
    "excess_annualized_return_vs_qqq_pct",
    "excess_annualized_return_vs_dia_pct",
)


def _configure_longport_environment() -> None:
    credentials = {
        "LONGPORT_APP_KEY": settings.longbridge_app_key,
        "LONGPORT_APP_SECRET": settings.longbridge_app_secret,
        "LONGPORT_ACCESS_TOKEN": settings.longbridge_access_token,
    }
    missing = [
        name for name, value in credentials.items() if not value
    ]
    if missing:
        raise RuntimeError(
            "Longport credentials are unavailable: "
            + ", ".join(missing)
        )
    for name, value in credentials.items():
        os.environ[name] = value


def _performance_summary(raw: object) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        return {}
    return {
        field: raw.get(field)
        for field in _PERFORMANCE_SUMMARY_FIELDS
    }


def _summary(payload: Mapping[str, object]) -> dict[str, object]:
    variants = payload.get("variants")
    compact_variants: list[dict[str, object]] = []
    if isinstance(variants, (list, tuple)):
        for raw in variants:
            if not isinstance(raw, dict):
                continue
            compact_variants.append(
                {
                    "name": (
                        raw.get("variant", {}).get("name")
                        if isinstance(raw.get("variant"), dict)
                        else None
                    ),
                    "variant": raw.get("variant"),
                    "training_score": raw.get("training_score"),
                    "validation_passed": raw.get(
                        "validation_passed"
                    ),
                    "validation_blockers": raw.get(
                        "validation_blockers"
                    ),
                    "expanding_validation_passed": raw.get(
                        "expanding_validation_passed"
                    ),
                    "expanding_validation_blockers": raw.get(
                        "expanding_validation_blockers"
                    ),
                    "expanding_folds_passed": raw.get(
                        "expanding_folds_passed"
                    ),
                    "expanding_folds_total": raw.get(
                        "expanding_folds_total"
                    ),
                    "expanding_validation": _performance_summary(
                        raw.get("expanding_validation")
                    ),
                    "full": _performance_summary(
                        raw.get("full")
                    ),
                    "training": _performance_summary(
                        raw.get("training")
                    ),
                    "validation": _performance_summary(
                        raw.get("validation")
                    ),
                }
            )
    return {
        "algorithm_version": payload.get("algorithm_version"),
        "status": payload.get("status"),
        "benchmark_symbols": payload.get("benchmark_symbols"),
        "data_scope": payload.get("data_scope"),
        "survivorship_bias": payload.get("survivorship_bias"),
        "validation_periods": payload.get("validation_periods"),
        "expanding_validation_min_training_periods": (
            payload.get(
                "expanding_validation_min_training_periods"
            )
        ),
        "expanding_validation_fold_periods": payload.get(
            "expanding_validation_fold_periods"
        ),
        "selected_variant": payload.get("selected_variant"),
        "selected_variant_validation_passed": payload.get(
            "selected_variant_validation_passed"
        ),
        "validated_challenger_variant": payload.get(
            "validated_challenger_variant"
        ),
        "automatic_promotion_allowed": payload.get(
            "automatic_promotion_allowed"
        ),
        "promotion_blockers": payload.get("promotion_blockers"),
        "point_in_time_data_missing_symbols": payload.get(
            "point_in_time_data_missing_symbols"
        ),
        "variants": compact_variants,
    }


def _acquisition_error(
    *,
    symbol: str,
    catalog_scope: str,
    status: str,
    requested_bars: int,
    bars_received: int | None,
    completed_bars: int,
    membership_end: str | None = None,
    requested_before: str | None = None,
    error_type: str | None = None,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "catalog_scope": catalog_scope,
        "status": status,
        "error_type": error_type,
        "requested_bars": requested_bars,
        "bars_received": bars_received,
        "completed_bars": completed_bars,
        "membership_end": membership_end,
        "requested_before": requested_before,
    }


def _collect_candidate_bars(
    broker: BrokerGateway,
    *,
    candidates: Sequence[IndexCandidate],
    current_symbols: frozenset[str],
    history_bars: int,
    now: datetime,
) -> tuple[dict[str, Sequence[DailyBar]], list[dict[str, object]]]:
    bars_by_symbol: dict[str, Sequence[DailyBar]] = {}
    errors: list[dict[str, object]] = []
    for index, candidate in enumerate(candidates, start=1):
        symbol = candidate.symbol
        is_current = symbol in current_symbols
        catalog_scope = "CURRENT_CANDIDATE" if is_current else (
            "HISTORICAL_CANDIDATE"
        )
        membership_end = (
            None if is_current else historical_membership_end(candidate)
        )
        requested_before = (
            None if is_current else historical_research_before(candidate)
        )
        membership_end_text = (
            membership_end.isoformat()
            if membership_end is not None
            else None
        )
        requested_before_text = (
            requested_before.isoformat()
            if requested_before is not None
            else None
        )
        print(
            f"candidate {index}/{len(candidates)} {symbol}",
            file=sys.stderr,
        )
        try:
            if is_current:
                raw = broker.get_forward_adjusted_candlesticks(
                    symbol,
                    "DAY",
                    history_bars,
                )
            else:
                raw = historical_research_candlesticks(
                    broker,
                    candidate,
                    count=history_bars,
                )
                if raw is None:
                    if research_candidate_uses_recent_candlesticks(
                        candidate
                    ):
                        raw = broker.get_forward_adjusted_candlesticks(
                            symbol,
                            "DAY",
                            history_bars,
                        )
                    else:
                        errors.append(
                            _acquisition_error(
                                symbol=symbol,
                                catalog_scope=catalog_scope,
                                status=(
                                    "HISTORICAL_CURSOR_UNAVAILABLE"
                                ),
                                requested_bars=history_bars,
                                bars_received=None,
                                completed_bars=0,
                                membership_end=membership_end_text,
                                requested_before=(
                                    requested_before_text
                                ),
                            )
                        )
                        bars_by_symbol[symbol] = ()
                        print(
                            "candidate history unavailable for "
                            f"{symbol}: "
                            "HISTORICAL_CURSOR_UNAVAILABLE",
                            file=sys.stderr,
                        )
                        continue
            completed = completed_daily_bars(
                raw,
                market=candidate.market,
                now=now,
            )
        except Exception as exc:
            errors.append(
                _acquisition_error(
                    symbol=symbol,
                    catalog_scope=catalog_scope,
                    status="FETCH_ERROR",
                    error_type=type(exc).__name__,
                    requested_bars=history_bars,
                    bars_received=None,
                    completed_bars=0,
                    membership_end=membership_end_text,
                    requested_before=requested_before_text,
                )
            )
            bars_by_symbol[symbol] = ()
            print(
                f"candidate history unavailable for {symbol}: "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )
            continue
        bars_by_symbol[symbol] = completed
        if not raw or not completed:
            errors.append(
                _acquisition_error(
                    symbol=symbol,
                    catalog_scope=catalog_scope,
                    status=(
                        "EMPTY_RESPONSE"
                        if not raw
                        else "NO_COMPLETED_BARS"
                    ),
                    requested_bars=history_bars,
                    bars_received=len(raw),
                    completed_bars=len(completed),
                    membership_end=membership_end_text,
                    requested_before=requested_before_text,
                )
            )
    return bars_by_symbol, errors


def _collect_benchmark_bars(
    broker: BrokerGateway,
    *,
    history_bars: int,
    now: datetime,
) -> tuple[dict[str, Sequence[DailyBar]], list[dict[str, object]]]:
    benchmark_bars: dict[str, Sequence[DailyBar]] = {}
    errors: list[dict[str, object]] = []
    for symbol in ROTATION_BENCHMARK_SYMBOLS:
        print(f"benchmark {symbol}", file=sys.stderr)
        try:
            raw = broker.get_forward_adjusted_candlesticks(
                symbol,
                "DAY",
                history_bars,
            )
            completed = completed_daily_bars(
                raw,
                market="US",
                now=now,
            )
        except Exception as exc:
            errors.append(
                _acquisition_error(
                    symbol=symbol,
                    catalog_scope="BENCHMARK",
                    status="FETCH_ERROR",
                    error_type=type(exc).__name__,
                    requested_bars=history_bars,
                    bars_received=None,
                    completed_bars=0,
                )
            )
            benchmark_bars[symbol] = ()
            print(
                f"benchmark history unavailable for {symbol}: "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )
            continue
        benchmark_bars[symbol] = completed
        if not raw or not completed:
            errors.append(
                _acquisition_error(
                    symbol=symbol,
                    catalog_scope="BENCHMARK",
                    status=(
                        "EMPTY_RESPONSE"
                        if not raw
                        else "NO_COMPLETED_BARS"
                    ),
                    requested_bars=history_bars,
                    bars_received=len(raw),
                    completed_bars=len(completed),
                )
            )
    return benchmark_bars, errors


def _acquisition_blockers(
    errors: Sequence[Mapping[str, object]],
) -> list[str]:
    scopes = {error.get("catalog_scope") for error in errors}
    blockers: list[str] = []
    if "CURRENT_CANDIDATE" in scopes:
        blockers.append(
            "ROTATION_CURRENT_CONSTITUENT_DATA_ACQUISITION_PARTIAL"
        )
    if "HISTORICAL_CANDIDATE" in scopes:
        blockers.append(
            "ROTATION_HISTORICAL_MEMBER_DATA_ACQUISITION_PARTIAL"
        )
    if "BENCHMARK" in scopes:
        blockers.append(
            "ROTATION_BENCHMARK_DATA_ACQUISITION_PARTIAL"
        )
    return blockers


def _report(
    *,
    current_payload: Mapping[str, object],
    point_in_time_payload: Mapping[str, object],
    membership_history: Mapping[str, object],
    acquisition_errors: Sequence[Mapping[str, object]],
    history_bars: int,
    current_candidate_count: int,
    historical_candidate_count: int,
    full: bool,
) -> dict[str, object]:
    raw_primary_blockers = point_in_time_payload.get(
        "promotion_blockers"
    )
    primary_blockers = (
        [
            blocker
            for blocker in raw_primary_blockers
            if isinstance(blocker, str)
        ]
        if isinstance(raw_primary_blockers, (list, tuple))
        else []
    )
    fail_closed_blockers = list(dict.fromkeys([
        *primary_blockers,
        *_acquisition_blockers(acquisition_errors),
        "ROTATION_FORWARD_OBSERVATIONS_REQUIRED",
    ]))
    current_evidence = (
        dict(current_payload)
        if full
        else _summary(current_payload)
    )
    point_in_time_evidence = (
        dict(point_in_time_payload)
        if full
        else _summary(point_in_time_payload)
    )
    return {
        "schema_version": _CLI_SCHEMA_VERSION,
        "evidence_mode": "READ_ONLY_RESEARCH",
        "research_only": True,
        "order_submission_allowed": False,
        "automatic_promotion_allowed": False,
        "primary_evidence": "point_in_time_primary",
        "sensitivity_evidence": (
            "current_constituents_sensitivity"
        ),
        "history_bars_requested": history_bars,
        "membership_history": dict(membership_history),
        "acquisition": {
            "current_candidate_count": current_candidate_count,
            "historical_candidate_count": historical_candidate_count,
            "total_candidate_count": (
                current_candidate_count + historical_candidate_count
            ),
            "benchmark_symbols": list(
                ROTATION_BENCHMARK_SYMBOLS
            ),
            "error_count": len(acquisition_errors),
            "errors": [dict(error) for error in acquisition_errors],
        },
        "fail_closed_blockers": fail_closed_blockers,
        "point_in_time_primary": point_in_time_evidence,
        "current_constituents_sensitivity": current_evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate the read-only monthly index momentum rotation "
            "with point-in-time membership as primary evidence and a "
            "current-constituent survivorship-bias sensitivity."
        )
    )
    parser.add_argument(
        "--history-bars",
        type=int,
        default=1000,
        help="recent completed daily bars requested per symbol",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help=(
            "include period-level evidence for both evaluations instead "
            "of the compact summary"
        ),
    )
    args = parser.parse_args()
    if args.history_bars < 300:
        parser.error("--history-bars must be at least 300")
    if args.history_bars > 1000:
        parser.error("--history-bars must not exceed 1000")

    _configure_longport_environment()
    now = datetime.now(timezone.utc)
    broker = BrokerGateway()
    try:
        candidates = ROTATION_RESEARCH_CANDIDATE_CATALOG
        current_symbols = frozenset(
            candidate.symbol for candidate in INDEX_CANDIDATE_CATALOG
        )
        bars_by_symbol, candidate_errors = (
            _collect_candidate_bars(
                broker,
                candidates=candidates,
                current_symbols=current_symbols,
                history_bars=args.history_bars,
                now=now,
            )
        )
        benchmark_bars, benchmark_errors = (
            _collect_benchmark_bars(
                broker,
                history_bars=args.history_bars,
                now=now,
            )
        )
        config = selection_config_from_settings()
        current_payload = evaluate_rotation_walk_forward(
            candidates=INDEX_CANDIDATE_CATALOG,
            bars_by_symbol=bars_by_symbol,
            benchmark_bars_by_symbol=benchmark_bars,
            base_config=config,
        ).to_dict()
        point_in_time_payload = evaluate_rotation_walk_forward(
            candidates=candidates,
            bars_by_symbol=bars_by_symbol,
            benchmark_bars_by_symbol=benchmark_bars,
            base_config=config,
            membership_history=INDEX_MEMBERSHIP_HISTORY,
        ).to_dict()
        membership_metadata = INDEX_MEMBERSHIP_HISTORY.metadata(
            candidates
        )
        report = _report(
            current_payload=current_payload,
            point_in_time_payload=point_in_time_payload,
            membership_history=membership_metadata,
            acquisition_errors=[
                *candidate_errors,
                *benchmark_errors,
            ],
            history_bars=args.history_bars,
            current_candidate_count=len(INDEX_CANDIDATE_CATALOG),
            historical_candidate_count=(
                len(candidates) - len(INDEX_CANDIDATE_CATALOG)
            ),
            full=args.full,
        )
        print(
            json.dumps(
                report,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            flush=True,
        )
    finally:
        broker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
