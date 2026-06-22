from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

__all__ = ["PortfolioConfig"]


@dataclass
class PortfolioConfig:
    name: str
    symbols: list[str]
    allocations: dict[str, Decimal]
    per_symbol_risk_budget: dict[str, Decimal] | None = None
    rebalance_threshold_pct: Decimal = Decimal("5")
    max_gross_exposure: Decimal = Decimal("1.0")
    max_net_exposure: Decimal = Decimal("1.0")
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.symbols:
            raise ValueError("symbols must not be empty")
        if set(self.allocations.keys()) != set(self.symbols):
            raise ValueError("allocations keys must match symbols")
        total = sum(self.allocations.values(), Decimal("0"))
        if total != Decimal("1"):
            raise ValueError("allocations must sum to 1")
        if self.per_symbol_risk_budget is None:
            self.per_symbol_risk_budget = {s: Decimal("0.05") for s in self.symbols}
        elif set(self.per_symbol_risk_budget.keys()) != set(self.symbols):
            raise ValueError("per_symbol_risk_budget keys must match symbols")
        if self.rebalance_threshold_pct <= Decimal("0"):
            raise ValueError("rebalance_threshold_pct must be positive")
        if self.max_gross_exposure <= Decimal("0"):
            raise ValueError("max_gross_exposure must be positive")
        if self.max_net_exposure <= Decimal("0"):
            raise ValueError("max_net_exposure must be positive")

    def to_json(self) -> str:
        return json.dumps(
            {
                "name": self.name,
                "symbols": self.symbols,
                "allocations": {k: str(v) for k, v in self.allocations.items()},
                "per_symbol_risk_budget": {k: str(v) for k, v in self.per_symbol_risk_budget.items()},
                "rebalance_threshold_pct": str(self.rebalance_threshold_pct),
                "max_gross_exposure": str(self.max_gross_exposure),
                "max_net_exposure": str(self.max_net_exposure),
                "enabled": self.enabled,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> PortfolioConfig:
        data = json.loads(raw)
        required = {"name", "symbols", "allocations"}
        missing = required - set(data.keys())
        if missing:
            raise ValueError(f"Missing required fields: {missing}")
        return cls(
            name=data["name"],
            symbols=data["symbols"],
            allocations={k: Decimal(v) for k, v in data["allocations"].items()},
            per_symbol_risk_budget={k: Decimal(v) for k, v in data["per_symbol_risk_budget"].items()} if "per_symbol_risk_budget" in data else None,
            rebalance_threshold_pct=Decimal(data.get("rebalance_threshold_pct", "5")),
            max_gross_exposure=Decimal(data.get("max_gross_exposure", "1.0")),
            max_net_exposure=Decimal(data.get("max_net_exposure", "1.0")),
            enabled=data.get("enabled", True),
        )
