"""Exit efficiency analysis service.

Measures how well exits capture the favorable excursion of each closed
trade: for winners, the share of MFE actually realized (capture rate)
and the giveback left on the table; for all trades, how deep the
adverse excursion went.  Read-only.

Inspired by Edgewonk's exit-efficiency journal metrics and TraderVue's
MFE/MAE execution reports.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math
from statistics import median
from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    mixed_currency_error,
)
from app.services.daily_pnl_service import ClosedRoundTrip

__all__ = ["ExitEfficiencyService"]


class ExitEfficiencyService:
    """MFE capture / MAE tolerance analytics over closed trades."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def summary(self, days: int = 90) -> dict[str, Any]:
        sample = load_analytics_trade_sample(
            self._db,
            lookback_days=days,
            include_excursions=True,
        )
        rows, excursion_quality = _select_excursion_evidence(sample.trades)
        base_payload = {
            "days": days,
            "sample_size": len(rows),
            "closed_trade_count": len(sample.trades),
            "eligible_excursion_count": len(rows),
            "excursion_quality": excursion_quality,
        }
        mixed_error = mixed_currency_error(
            sample,
            payload=base_payload,
        )
        if mixed_error is not None:
            return mixed_error
        if len(rows) < 3:
            return analytics_response(
                sample,
                {
                    **base_payload,
                    "capture_sample_size": 0,
                    "mae_sample_size": len(rows),
                    "error": (
                        "Need at least 3 closed trades with verified interior "
                        "snapshot excursion evidence."
                    ),
                },
            )

        captures: list[float] = []
        givebacks: list[float] = []
        maes: list[float] = []
        winner_maes: list[float] = []
        left_on_table = 0
        winners = 0
        by_cause: dict[str, dict[str, float]] = defaultdict(
            lambda: {"trades": 0.0, "capture_sum": 0.0, "capture_n": 0.0, "net_sum": 0.0}
        )

        for row in rows:
            net = float(row.net_pnl or 0.0)
            gross = float(row.gross_pnl)
            mfe = float(row.mfe_amount) if row.mfe_amount is not None else None
            mae = float(row.mae_amount) if row.mae_amount is not None else None
            cause = row.exit_cause or "UNKNOWN"

            bucket = by_cause[cause]
            bucket["trades"] += 1
            bucket["net_sum"] += net

            if mae is not None:
                # mae_amount is stored as adverse magnitude (<= 0 expected)
                depth = abs(mae)
                maes.append(depth)
                if gross > 0:
                    winner_maes.append(depth)

            if gross > 0 and mfe is not None and mfe > 0:
                winners += 1
                capture = min(gross / mfe, 1.0)
                captures.append(capture)
                giveback = max(mfe - gross, 0.0)
                givebacks.append(giveback)
                bucket["capture_sum"] += capture
                bucket["capture_n"] += 1
                if mfe > 2 * gross:
                    left_on_table += 1

        cause_rows = [
            {
                "exit_cause": cause,
                "trades": int(v["trades"]),
                "net_pnl": round(v["net_sum"], 2),
                "avg_capture": round(v["capture_sum"] / v["capture_n"], 4) if v["capture_n"] else None,
            }
            for cause, v in sorted(by_cause.items(), key=lambda kv: kv[1]["trades"], reverse=True)
        ]

        return analytics_response(
            sample,
            {
                **base_payload,
                "capture_sample_size": len(captures),
                "mae_sample_size": len(maes),
                "winners_with_mfe": winners,
                "avg_capture_rate": round(sum(captures) / len(captures), 4) if captures else None,
                "median_capture_rate": round(median(captures), 4) if captures else None,
                "avg_giveback": round(sum(givebacks) / len(givebacks), 2) if givebacks else None,
                "median_giveback": round(median(givebacks), 2) if givebacks else None,
                "left_on_table_count": left_on_table,
                "left_on_table_pct": round(left_on_table / winners, 4) if winners else None,
                "avg_mae_depth": round(sum(maes) / len(maes), 2) if maes else None,
                "avg_winner_mae_depth": (
                    round(sum(winner_maes) / len(winner_maes), 2) if winner_maes else None
                ),
                "by_exit_cause": cause_rows,
            },
        )


def _select_excursion_evidence(
    trades: list[ClosedRoundTrip],
) -> tuple[list[ClosedRoundTrip], dict[str, Any]]:
    """Keep only path-observed, internally consistent excursion records."""

    eligible: list[ClosedRoundTrip] = []
    excluded = Counter[str]()
    for trade in trades:
        reason = _excursion_exclusion_reason(trade)
        if reason is None:
            eligible.append(trade)
        else:
            excluded[reason] += 1

    status = (
        "INSUFFICIENT"
        if len(eligible) < 3
        else "PARTIAL"
        if excluded
        else "COMPLETE"
    )
    observed_gaps = [
        float(trade.excursion_max_gap_seconds)
        for trade in eligible
        if trade.excursion_max_gap_seconds is not None
    ]
    quality = {
        "status": status,
        "closed_trade_count": len(trades),
        "eligible_excursion_count": len(eligible),
        "excluded_excursion_count": sum(excluded.values()),
        "excluded_by_reason": dict(sorted(excluded.items())),
        "interior_observation_count": sum(
            int(trade.excursion_interior_observation_count)
            for trade in eligible
        ),
        "max_gap_seconds": max(observed_gaps) if observed_gaps else None,
    }
    return eligible, quality


def _excursion_exclusion_reason(trade: ClosedRoundTrip) -> str | None:
    source = str(trade.excursion_source or "").upper()
    if source not in {
        "SNAPSHOT_OBSERVED",
        "PERSISTED_WITH_SNAPSHOT_EVIDENCE",
    }:
        return source or "LEGACY_UNKNOWN"
    observation_count = trade.excursion_interior_observation_count
    if not isinstance(observation_count, int) or observation_count <= 0:
        return "NO_INTERIOR_OBSERVATIONS"

    values = (
        trade.mfe_amount,
        trade.mae_amount,
        trade.mfe_pct,
        trade.mae_pct,
        trade.gross_pnl,
    )
    numeric_values: list[float] = []
    for value in values:
        if value is None:
            return "INVALID_VALUES"
        try:
            numeric_value = float(value)
        except (TypeError, ValueError, OverflowError):
            return "INVALID_VALUES"
        if not math.isfinite(numeric_value):
            return "INVALID_VALUES"
        numeric_values.append(numeric_value)
    mfe, mae, mfe_pct, mae_pct, gross = numeric_values
    tolerance = max(1e-6, abs(gross) * 1e-6)
    if (
        mfe < -tolerance
        or mae > tolerance
        or mfe_pct < -tolerance
        or mae_pct > tolerance
        or mfe + tolerance < max(gross, 0.0)
        or mae - tolerance > min(gross, 0.0)
    ):
        return "INCONSISTENT_VALUES"
    max_gap = trade.excursion_max_gap_seconds
    if max_gap is not None and (
        not math.isfinite(float(max_gap)) or float(max_gap) < 0
    ):
        return "INVALID_COVERAGE"
    return None
