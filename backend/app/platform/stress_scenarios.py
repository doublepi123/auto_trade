"""P200: macro stress scenario library.

Deterministic, predefined macro shock scenarios applied to a portfolio snapshot
(positions + prices) to project stressed NAV and per-asset PnL. Each scenario
is a pure function of the inputs — no randomness, no I/O — so stress runs are
reproducible and cheap. Mirrors the scenario-based stress testing in RiskMetrics
and the regulator-style macro scenarios most banks run (equity crash, vol spike,
correlation breakdown, liquidity discount).

Scenarios operate on a simple ``StressInput`` (symbol -> (quantity, price,
optional beta to equity)) and produce a ``StressResult`` with shocked prices,
stressed NAV vs baseline, and per-symbol PnL. New scenarios are registered into
a :class:`ScenarioLibrary` so callers can run one or all scenarios by name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

__all__ = [
    "StressInput",
    "StressResult",
    "Scenario",
    "ScenarioLibrary",
    "equity_crash_scenario",
    "volatility_spike_scenario",
    "correlation_breakdown_scenario",
    "liquidity_discount_scenario",
    "get_default_library",
]


@dataclass
class StressInput:
    """Portfolio snapshot to stress.

    ``positions`` maps symbol -> (quantity, last_price). ``betas`` optionally
    maps symbol -> equity beta (defaults to 1.0). ``base_nav`` is the
    pre-stress NAV including cash; if omitted it is derived from positions.
    """

    positions: dict[str, tuple[int, Decimal]]
    betas: dict[str, Decimal] = field(default_factory=dict)
    base_nav: Decimal | None = None

    def beta(self, symbol: str) -> Decimal:
        return self.betas.get(symbol, Decimal("1"))

    def positions_value(self, prices: dict[str, Decimal]) -> Decimal:
        return sum(
            (Decimal(qty) * prices.get(sym, Decimal("0")) for sym, (qty, _) in self.positions.items()),
            Decimal("0"),
        )

    def baseline_nav(self) -> Decimal:
        if self.base_nav is not None:
            return self.base_nav
        prices = {sym: price for sym, (_qty, price) in self.positions.items()}
        return self.positions_value(prices)


@dataclass
class StressResult:
    scenario: str
    shocked_prices: dict[str, Decimal]
    stressed_nav: Decimal
    baseline_nav: Decimal
    pnl: Decimal
    pnl_pct: Decimal
    per_symbol_pnl: dict[str, Decimal]
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "description": self.description,
            "baseline_nav": float(self.baseline_nav),
            "stressed_nav": float(self.stressed_nav),
            "pnl": float(self.pnl),
            "pnl_pct": float(self.pnl_pct),
            "shocked_prices": {k: float(v) for k, v in self.shocked_prices.items()},
            "per_symbol_pnl": {k: float(v) for k, v in self.per_symbol_pnl.items()},
        }


Scenario = Callable[[StressInput], StressResult]


def _pnl_pct(pnl: Decimal, baseline: Decimal) -> Decimal:
    if baseline == 0:
        return Decimal("0")
    return (pnl / baseline) * Decimal("100")


def equity_crash_scenario(shock_pct: Decimal = Decimal("-20")) -> Scenario:
    """Equity bear market: every position shocked down by ``shock_pct * beta``.

    Negative ``shock_pct`` (e.g. -20) means a 20% market drop; higher-beta names
    fall further.
    """

    def run(inp: StressInput) -> StressResult:
        shocked: dict[str, Decimal] = {}
        per_symbol: dict[str, Decimal] = {}
        for sym, (qty, price) in inp.positions.items():
            move = shock_pct * inp.beta(sym) / Decimal("100")
            new_price = price * (Decimal("1") + move)
            shocked[sym] = new_price
            per_symbol[sym] = (new_price - price) * Decimal(qty)
        stressed_nav = inp.positions_value(shocked)
        # If base_nav included cash, preserve the cash component.
        baseline = inp.baseline_nav()
        positions_baseline = inp.positions_value({s: p for s, (_q, p) in inp.positions.items()})
        cash = baseline - positions_baseline
        stressed_nav_with_cash = stressed_nav + cash
        pnl = stressed_nav_with_cash - baseline
        return StressResult(
            scenario="equity_crash",
            shocked_prices=shocked,
            stressed_nav=stressed_nav_with_cash,
            baseline_nav=baseline,
            pnl=pnl,
            pnl_pct=_pnl_pct(pnl, baseline),
            per_symbol_pnl=per_symbol,
            description=f"equity market {shock_pct}% (beta-scaled)",
        )

    return run


def volatility_spike_scenario(vol_shock_pct: Decimal = Decimal("50")) -> Scenario:
    """Volatility spike modeled as a price haircut proportional to inverse price.

    Higher-priced / lower-vol names absorb less; this is a coarse proxy: shock
    each price down by ``vol_shock_pct / 10`` of its value (a 50% vol spike → 5%
    price drag). Kept deliberately simple and deterministic.
    """

    drag = vol_shock_pct / Decimal("1000")

    def run(inp: StressInput) -> StressResult:
        shocked: dict[str, Decimal] = {}
        per_symbol: dict[str, Decimal] = {}
        for sym, (qty, price) in inp.positions.items():
            new_price = price * (Decimal("1") - drag)
            shocked[sym] = new_price
            per_symbol[sym] = (new_price - price) * Decimal(qty)
        baseline = inp.baseline_nav()
        positions_baseline = inp.positions_value({s: p for s, (_q, p) in inp.positions.items()})
        cash = baseline - positions_baseline
        stressed_nav = inp.positions_value(shocked) + cash
        pnl = stressed_nav - baseline
        return StressResult(
            scenario="volatility_spike",
            shocked_prices=shocked,
            stressed_nav=stressed_nav,
            baseline_nav=baseline,
            pnl=pnl,
            pnl_pct=_pnl_pct(pnl, baseline),
            per_symbol_pnl=per_symbol,
            description=f"volatility +{vol_shock_pct}% (price drag proxy)",
        )

    return run


def correlation_breakdown_scenario(diversifier_loss_pct: Decimal = Decimal("-15")) -> Scenario:
    """Correlation breakdown: assets that typically diversify (beta < 1) fall.

    Low-beta / defensive names lose the diversification benefit and move with
    the market. The shock scales inversely with beta: a beta-0.5 name takes
    twice the hit of a beta-2 name (its diversification just vanished).
    """

    def run(inp: StressInput) -> StressResult:
        shocked: dict[str, Decimal] = {}
        per_symbol: dict[str, Decimal] = {}
        for sym, (qty, price) in inp.positions.items():
            beta = inp.beta(sym)
            # diversification value ∝ (1 - beta); losing it hurts low-beta names more
            intensity = (Decimal("1") - beta) if beta < Decimal("1") else Decimal("0.5")
            move = diversifier_loss_pct * intensity / Decimal("100")
            new_price = price * (Decimal("1") + move)
            shocked[sym] = new_price
            per_symbol[sym] = (new_price - price) * Decimal(qty)
        baseline = inp.baseline_nav()
        positions_baseline = inp.positions_value({s: p for s, (_q, p) in inp.positions.items()})
        cash = baseline - positions_baseline
        stressed_nav = inp.positions_value(shocked) + cash
        pnl = stressed_nav - baseline
        return StressResult(
            scenario="correlation_breakdown",
            shocked_prices=shocked,
            stressed_nav=stressed_nav,
            baseline_nav=baseline,
            pnl=pnl,
            pnl_pct=_pnl_pct(pnl, baseline),
            per_symbol_pnl=per_symbol,
            description=f"correlation breakdown (diversifier {diversifier_loss_pct}%)",
        )

    return run


def liquidity_discount_scenario(discount_bps: Decimal = Decimal("200")) -> Scenario:
    """Liquidity discount: every position marked down by a fixed spread.

    Models a fire-sale haircut independent of fundamentals — ``discount_bps``
    (e.g. 200 = 2%) applied uniformly.
    """

    factor = Decimal("1") - discount_bps / Decimal("10000")

    def run(inp: StressInput) -> StressResult:
        shocked: dict[str, Decimal] = {}
        per_symbol: dict[str, Decimal] = {}
        for sym, (qty, price) in inp.positions.items():
            new_price = price * factor
            shocked[sym] = new_price
            per_symbol[sym] = (new_price - price) * Decimal(qty)
        baseline = inp.baseline_nav()
        positions_baseline = inp.positions_value({s: p for s, (_q, p) in inp.positions.items()})
        cash = baseline - positions_baseline
        stressed_nav = inp.positions_value(shocked) + cash
        pnl = stressed_nav - baseline
        return StressResult(
            scenario="liquidity_discount",
            shocked_prices=shocked,
            stressed_nav=stressed_nav,
            baseline_nav=baseline,
            pnl=pnl,
            pnl_pct=_pnl_pct(pnl, baseline),
            per_symbol_pnl=per_symbol,
            description=f"liquidity discount {discount_bps} bps",
        )

    return run


class ScenarioLibrary:
    """Registry of named macro stress scenarios."""

    def __init__(self) -> None:
        self._scenarios: dict[str, Scenario] = {}

    def register(self, name: str, scenario: Scenario) -> None:
        if name in self._scenarios:
            raise ValueError(f"scenario '{name}' already registered")
        self._scenarios[name] = scenario

    def get(self, name: str) -> Scenario:
        if name not in self._scenarios:
            raise KeyError(f"scenario '{name}' not found")
        return self._scenarios[name]

    def names(self) -> list[str]:
        return list(self._scenarios.keys())

    def run(self, name: str, inp: StressInput) -> StressResult:
        return self.get(name)(inp)

    def run_all(self, inp: StressInput) -> dict[str, StressResult]:
        return {name: scenario(inp) for name, scenario in self._scenarios.items()}

    def summary(self, inp: StressInput) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.run_all(inp).values()]


def get_default_library() -> ScenarioLibrary:
    lib = ScenarioLibrary()
    lib.register("equity_crash", equity_crash_scenario())
    lib.register("volatility_spike", volatility_spike_scenario())
    lib.register("correlation_breakdown", correlation_breakdown_scenario())
    lib.register("liquidity_discount", liquidity_discount_scenario())
    return lib
