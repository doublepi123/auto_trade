"""Pure summaries of complete, independently replayed range arms."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from typing import Any

from app.domain.strategy_v2.clustered_returns import clustered_t_test, day_cluster_t_critical

COST_SCENARIOS = ("measured", "confirmation", "legacy_stress")
ARMS = ("crossing_baseline", "reclaim_candidate")


def _statistics(rows: list[tuple[date, Decimal]]) -> dict[str, Any]:
    # The last fixed table value is conservative for more than 121 clusters.
    count = len({day for day, _ in rows})
    critical = day_cluster_t_critical(min(count, 121)) if count >= 2 else None
    return asdict(clustered_t_test(
        [(day, float(value)) for day, value in rows], t_critical=critical,
    ))


def _arm_summary(report: dict[str, Any], sessions: Sequence[date]) -> dict[str, Any]:
    trades = report["closed_trades"]
    coverage = report["daily_coverage"]
    daily = {
        day.isoformat(): {
            "closed_trades": 0, "coverage": coverage.get(day.isoformat(), {
                "fraction": 0, "valid_minutes": 0, "missing_session": True,
            }),
            "costs": {s: {k: Decimal(0) for k in ("gross", "fees", "extra_costs", "net")}
                      for s in COST_SCENARIOS},
        } for day in sessions
    }
    observations: dict[str, list[tuple[date, Decimal]]] = {s: [] for s in COST_SCENARIOS}
    for trade in trades:
        day = trade["day"]
        if day not in daily:
            raise ValueError("trade outside requested session window")
        daily[day]["closed_trades"] += 1
        for scenario in COST_SCENARIOS:
            for field in ("gross", "fees", "extra_costs", "net"):
                daily[day]["costs"][scenario][field] += Decimal(trade["costs"][scenario][field])
            observations[scenario].append((date.fromisoformat(day), Decimal(trade["costs"][scenario]["net"])))
    totals = {
        s: {field: str(sum((day["costs"][s][field] for day in daily.values()), Decimal(0)))
            for field in ("gross", "fees", "extra_costs", "net")}
        for s in COST_SCENARIOS
    }
    trade_days = sum(day["closed_trades"] > 0 for day in daily.values())
    quality = (
        bool(daily)
        and all(day["coverage"]["fraction"] >= .8 for day in daily.values())
        and not any(report[key] for key in ("position_open", "pending_entry", "pending_exit"))
        and report.get("cross_session_carries", 0) == 0
    )
    for day in daily.values():
        day["costs"] = {
            scenario: {key: str(value) for key, value in fields.items()}
            for scenario, fields in day["costs"].items()
        }
    return {
        "daily": daily, "cost_totals": totals,
        "trade_statistics": {s: _statistics(observations[s]) for s in COST_SCENARIOS},
        "closed_trade_days": trade_days, "closed_trades_count": len(trades),
        "quality_complete": quality, "sample_complete": trade_days >= 20 and len(trades) >= 30,
    }


def summarize_comparison(
    reports: dict[str, dict[str, Any]], sessions: Sequence[date], *,
    forward: bool = False, arms: tuple[str, str] = ARMS,
) -> dict[str, Any]:
    if list(sessions) != sorted(set(sessions)) or len(set(arms)) != 2 or set(reports) != set(arms):
        raise ValueError("comparison requires ordered unique sessions and both registered arms")
    summaries = {arm: _arm_summary(reports[arm], sessions) for arm in arms}
    baseline, candidate = (summaries[arm] for arm in arms)
    paired = {}
    for scenario in COST_SCENARIOS:
        rows = [
            (day, Decimal(candidate["daily"][day.isoformat()]["costs"][scenario]["net"])
             - Decimal(baseline["daily"][day.isoformat()]["costs"][scenario]["net"]))
            for day in sessions
        ]
        paired[scenario] = {
            **_statistics(rows), "total_difference": str(sum((v for _, v in rows), Decimal(0))),
            "estimand": "mean candidate-minus-baseline USD per scheduled trading day, including zeros",
        }
    # Frozen before scoring: whole-session 2/3 versus 1/3 chronology. Replay
    # state (inventory, risk latches, band and cooldown) is NEVER restarted here.
    boundary = len(sessions)*2//3
    early, later = list(sessions[:boundary]), list(sessions[boundary:])
    slices = {}
    for label, days in (("early", early), ("later", later)):
        slices[label] = {
            arm: {
                scenario: str(sum((
                    Decimal(summaries[arm]["daily"][day.isoformat()]["costs"][scenario]["net"])
                    for day in days
                ), Decimal(0)))
                for scenario in COST_SCENARIOS
            } for arm in arms
        }
    complete = all(row["quality_complete"] and row["sample_complete"] for row in summaries.values())
    return {
        "evidence_class": "FORWARD_SHADOW" if forward else "RETROSPECTIVE_DIAGNOSTIC",
        "status": "REVIEW_REQUIRED" if complete else "INSUFFICIENT_DATA",
        "automatic_promotion": False, "live_equivalent": False,
        "arms": summaries, "paired_days": paired,
        "split": {
            "early_days": [d.isoformat() for d in early],
            "later_days": [d.isoformat() for d in later],
            "unseen_holdout": False, "state_reset": False, "cost_totals": slices,
        },
        "adoption_authorized": False,
    }
