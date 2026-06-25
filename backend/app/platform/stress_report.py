"""P221: Scenario Stress Report Aggregator.

Aggregate the macro stress scenarios from :mod:`app.platform.stress_scenarios`
applied to a portfolio snapshot into a single consolidated report: per-scenario
PnL, per-scenario VaR, worst-scenario drawdown, and a capital-adequacy ratio.
Reuses the existing stress scenario library, risk-metrics VaR, and drawdown
analysis rather than duplicating them.

The report answers: *"if any of these macro shocks hit right now, how much
would we lose, how concentrated is that loss, and is our capital buffer
sufficient?"* — the regulator-style (FRB CCAR / RiskMetrics) aggregate stress
summary most banks run.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.platform.drawdown_analysis import drawdown_summary
from app.platform.risk_metrics import historical_var
from app.platform.stress_scenarios import (
    ScenarioLibrary,
    StressInput,
    get_default_library,
)

__all__ = ["build_stress_report", "StressReportBuilder"]


def build_stress_report(
    positions: dict[str, tuple[int, Decimal]],
    betas: dict[str, Decimal] | None = None,
    base_nav: Decimal | None = None,
    scenarios: ScenarioLibrary | None = None,
    confidence_levels: list[float] | None = None,
    capital_buffer: Decimal | None = None,
) -> dict[str, Any]:
    """Aggregate per-scenario PnL / VaR / drawdown into a single report.

    ``positions`` maps symbol -> (quantity, last_price). ``betas`` optionally
    maps symbol -> equity beta (defaults to 1.0). ``base_nav`` is the pre-stress
    NAV (defaults to positions value). ``capital_buffer`` is the excess capital
    above the positions' market value (used for the adequacy ratio); if omitted
    it is taken as 20% of base NAV.

    Returns ``{"scenarios": [...], "worst_scenario", "worst_pnl",
    "worst_pnl_pct", "scenario_var", "max_drawdown", "capital_adequacy_ratio"}``.
    """
    lib = scenarios or get_default_library()
    confidence_levels = confidence_levels or [0.95, 0.99]
    inp = StressInput(positions=positions, betas=betas or {}, base_nav=base_nav)
    baseline = inp.baseline_nav()

    results = lib.run_all(inp)
    scenario_rows: list[dict[str, Any]] = []
    pnls: list[float] = []
    pnl_pcts: list[float] = []
    for name, res in results.items():
        pnl = float(res.pnl)
        pnl_pct = float(res.pnl_pct)
        pnls.append(pnl)
        pnl_pcts.append(pnl_pct)
        # per-scenario VaR: treat the single-scenario PnL as a 1-point loss
        # distribution; VaR95 = VaR99 = that loss magnitude (clamped ≥ 0).
        loss = max(-pnl, 0.0)
        scenario_rows.append({
            "scenario": name,
            "description": res.description,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "stressed_nav": float(res.stressed_nav),
            "scenario_var": loss,
            "per_symbol_pnl": {k: float(v) for k, v in res.per_symbol_pnl.items()},
        })

    worst_idx = min(range(len(pnls)), key=lambda i: pnls[i]) if pnls else -1
    worst_scenario = scenario_rows[worst_idx]["scenario"] if worst_idx >= 0 else None
    worst_pnl = pnls[worst_idx] if worst_idx >= 0 else 0.0
    worst_pnl_pct = pnl_pcts[worst_idx] if worst_idx >= 0 else 0.0

    # aggregate scenario VaR: historical VaR over the scenario PnL distribution
    # (losses are negative returns → VaR is the positive loss magnitude at the
    # confidence level; treat scenario PnLs as the return sample).
    scenario_returns = [p / float(baseline) for p in pnls] if float(baseline) > 0 else pnls
    var_report = {f"var_{int(c * 100)}": historical_var(scenario_returns, confidence=c)
                  for c in confidence_levels}

    # worst-scenario drawdown: build an "equity" path from baseline → stressed
    # for the worst scenario and measure the drawdown.
    worst_equity = [float(baseline), float(baseline) + worst_pnl]
    dd = drawdown_summary(worst_equity) if len(worst_equity) >= 2 else {"max_drawdown": 0.0}

    # capital adequacy: capital buffer / worst-scenario loss.
    if capital_buffer is None:
        capital_buffer = Decimal("0.20") * baseline
    adequacy = (float(capital_buffer) / abs(worst_pnl)) if worst_pnl < 0 and abs(worst_pnl) > 0 else float("inf")

    return {
        "scenarios": scenario_rows,
        "worst_scenario": worst_scenario,
        "worst_pnl": worst_pnl,
        "worst_pnl_pct": worst_pnl_pct,
        "scenario_var": var_report,
        "max_drawdown": dd.get("max_drawdown", 0.0),
        "capital_adequacy_ratio": adequacy,
        "baseline_nav": float(baseline),
        "capital_buffer": float(capital_buffer),
    }


class StressReportBuilder:
    """Convenience wrapper."""

    def __init__(self, scenarios: ScenarioLibrary | None = None) -> None:
        self.scenarios = scenarios or get_default_library()

    def build(
        self,
        positions: dict[str, tuple[int, Decimal]],
        betas: dict[str, Decimal] | None = None,
        base_nav: Decimal | None = None,
        confidence_levels: list[float] | None = None,
        capital_buffer: Decimal | None = None,
    ) -> dict[str, Any]:
        return build_stress_report(
            positions, betas=betas, base_nav=base_nav, scenarios=self.scenarios,
            confidence_levels=confidence_levels, capital_buffer=capital_buffer,
        )