"""P229: Historical Scenario Generator.

Rather than parametric stress shocks (P200/P221), replay *actual* historical
market episodes against the current book to get scenario P&L grounded in
realized co-movements. Given a ``{symbol: (qty, price)}`` book and a library of
historical episodes (each a ``{symbol: return}`` map), apply each episode's
returns to the book and rank by realized P&L.

* **HistoricalScenarioLibrary** — pluggable library of named episodes (e.g.
  ``2020-03 COVID crash``, ``2022-06 rate shock``, ``2024-08 carry unwind``).
  Ships with a small built-in deterministic set; users extend via
  ``add_episode``.
* **apply_scenario** — multiply each holding's notional by ``1 + r`` and net
  against current notional to get per-symbol P&L.
* **historical_stress_report** — apply every episode, rank worst-first,
  return the worst case, the 95th-percentile loss, and capital adequacy
  against a buffer.

This complements P200/P221 (parametric) by anchoring stress to *realized*
factor moves — the standard FRB CCAR / Basel ICAAP historical-scenario method.

Reference: FRB CCAR, Basel ICAAP, RiskMetrics historical scenario module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "HistoricalEpisode",
    "HistoricalScenarioLibrary",
    "apply_scenario",
    "historical_stress_report",
    "default_episodes",
]


@dataclass(frozen=True)
class HistoricalEpisode:
    name: str
    returns: dict[str, float]  # {symbol: return} e.g. {"AAPL.US": -0.12, ...}
    description: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "returns": dict(self.returns), "description": self.description}


def default_episodes() -> list[HistoricalEpisode]:
    """A small deterministic built-in library of stylized historical episodes."""
    return [
        HistoricalEpisode("2020-03 COVID crash", {"A.US": -0.34, "B.US": -0.28, "C.US": -0.40},
                           "Mar-2020 risk-off"),
        HistoricalEpisode("2022-06 rate shock", {"A.US": -0.14, "B.US": -0.22, "C.US": -0.08},
                           "Jun-2022 duration/credit repricing"),
        HistoricalEpisode("2024-08 carry unwind", {"A.US": -0.09, "B.US": -0.18, "C.US": -0.31},
                           "Aug-2024 carry-trade unwind"),
        HistoricalEpisode("2018-02 vol-mageddon", {"A.US": -0.10, "B.US": -0.12, "C.US": -0.07},
                           "Feb-2018 short-vol unwind"),
        HistoricalEpisode("2023-03 bank stress", {"A.US": -0.18, "B.US": -0.05, "C.US": -0.22},
                           "Mar-2023 regional-bank stress"),
    ]


@dataclass
class HistoricalScenarioLibrary:
    episodes: list[HistoricalEpisode] = field(default_factory=list)

    @classmethod
    def with_defaults(cls) -> "HistoricalScenarioLibrary":
        return cls(episodes=default_episodes())

    def add_episode(self, episode: HistoricalEpisode) -> None:
        self.episodes.append(episode)

    def names(self) -> list[str]:
        return [e.name for e in self.episodes]


def apply_scenario(positions: Mapping[str, tuple[float, float]], episode: HistoricalEpisode) -> dict[str, float]:
    """Apply one episode's returns to a book.

    ``positions`` is ``{symbol: (qty, current_price)}``. Per-symbol P&L =
    ``qty * price * r``. Symbols absent from the episode return 0.
    """
    pnl: dict[str, float] = {}
    for sym, (qty, price) in positions.items():
        r = episode.returns.get(sym, 0.0)
        pnl[sym] = qty * price * r
    return pnl


@dataclass(frozen=True)
class HistoricalStressReport:
    per_episode: list[dict[str, Any]]
    worst_episode: str
    worst_pnl: float
    best_episode: str
    best_pnl: float
    percentile_95_loss: float
    capital_adequate: bool

    def to_dict(self) -> dict:
        return {
            "per_episode": self.per_episode,
            "worst_episode": self.worst_episode,
            "worst_pnl": self.worst_pnl,
            "best_episode": self.best_episode,
            "best_pnl": self.best_pnl,
            "percentile_95_loss": self.percentile_95_loss,
            "capital_adequate": self.capital_adequate,
        }


def historical_stress_report(
    positions: Mapping[str, tuple[float, float]],
    library: HistoricalScenarioLibrary | None = None,
    capital_buffer: float = 0.0,
    confidence: float = 0.95,
) -> HistoricalStressReport:
    """Run every episode in the library and rank by P&L."""
    if not positions:
        raise ValueError("positions must be non-empty")
    lib = library or HistoricalScenarioLibrary.with_defaults()
    if not lib.episodes:
        raise ValueError("library has no episodes")
    per_ep: list[dict[str, Any]] = []
    pnls: list[float] = []
    for ep in lib.episodes:
        per_sym = apply_scenario(positions, ep)
        total = sum(per_sym.values())
        pnls.append(total)
        per_ep.append({"name": ep.name, "total_pnl": total, "per_symbol": per_sym})
    # rank worst first
    sorted_pnls = sorted(pnls)
    worst = sorted_pnls[0]
    best = sorted_pnls[-1]
    # 95th-percentile loss (5th percentile of PnL → most-negative tail we keep at conf 0.95)
    # index into the sorted PnL distribution
    idx = int((1.0 - confidence) * (len(sorted_pnls) - 1))
    p95_loss = sorted_pnls[idx]
    worst_name = next(p["name"] for p in per_ep if p["total_pnl"] == worst)
    best_name = next(p["name"] for p in per_ep if p["total_pnl"] == best)
    capital_ok = (worst + capital_buffer) >= 0.0
    return HistoricalStressReport(
        per_episode=per_ep,
        worst_episode=worst_name,
        worst_pnl=worst,
        best_episode=best_name,
        best_pnl=best,
        percentile_95_loss=p95_loss,
        capital_adequate=capital_ok,
    )