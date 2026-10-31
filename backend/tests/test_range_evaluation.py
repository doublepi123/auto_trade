from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from app.domain.range_evaluation import summarize_comparison


def arm(rows: list[tuple[str, str]], coverage: dict[str, float]) -> dict[str, Any]:
    return {
        "closed_trades": [
            {"day": day, "costs": {
                scenario: {"net": value, "gross": value, "fees": "0", "extra_costs": "0"}
                for scenario in ("measured", "confirmation", "legacy_stress")
            }} for day, value in rows
        ],
        "daily_coverage": {
            day: {"fraction": value, "valid_minutes": int(value*390), "expected_minutes": 390}
            for day, value in coverage.items()
        },
        "position_open": False, "pending_entry": False, "pending_exit": False,
    }


def test_missing_days_and_zero_trade_days_are_kept_in_paired_comparison() -> None:
    days = [date(2026, 10, d) for d in (28, 29, 30)]
    reports = {
        "crossing_baseline": arm([("2026-10-28", "10")], {"2026-10-28": 1}),
        "reclaim_candidate": arm([("2026-10-29", "-2")], {"2026-10-29": 1}),
    }
    result = summarize_comparison(reports, days)
    assert result["evidence_class"] == "RETROSPECTIVE_DIAGNOSTIC"
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["paired_days"]["confirmation"]["observations"] == 3
    assert result["paired_days"]["confirmation"]["total_difference"] == "-12"
    assert result["arms"]["crossing_baseline"]["daily"]["2026-10-30"]["closed_trades"] == 0
    assert result["arms"]["crossing_baseline"]["quality_complete"] is False
    assert result["automatic_promotion"] is False


def test_chronological_diagnostics_keep_full_run_state_and_all_losses() -> None:
    days = [date(2026, 10, d) for d in (26, 27, 28, 29, 30)]
    baseline = arm([(d.isoformat(), str(i-4)) for i, d in enumerate(days)], {
        d.isoformat(): 1 for d in days
    })
    result = summarize_comparison({
        "crossing_baseline": baseline, "reclaim_candidate": baseline,
    }, days)
    assert result["split"]["early_days"] == [d.isoformat() for d in days[:3]]
    assert result["split"]["later_days"] == [d.isoformat() for d in days[3:]]
    assert result["split"]["unseen_holdout"] is False
    summary = result["arms"]["crossing_baseline"]
    assert summary["cost_totals"]["confirmation"]["net"] == "-10"
    assert Decimal(summary["trade_statistics"]["confirmation"]["naive_mean"]) == -2
    assert summary["trade_statistics"]["confirmation"]["ci_lower"] < -2
    assert summary["closed_trade_days"] == 5


def test_pending_inventory_prevents_quality_completion_even_with_full_coverage() -> None:
    days = [date(2026, 10, 30)]
    report = arm([], {"2026-10-30": 1})
    report["pending_exit"] = True
    report["position_open"] = True
    result = summarize_comparison({
        "crossing_baseline": report, "reclaim_candidate": report,
    }, days, forward=True)
    assert result["arms"]["reclaim_candidate"]["quality_complete"] is False
    assert result["evidence_class"] == "FORWARD_SHADOW"
    assert result["status"] == "INSUFFICIENT_DATA"
