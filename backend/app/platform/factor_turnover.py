"""P269: factor rank turnover diagnostics."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import pearson


@dataclass(frozen=True)
class FactorTurnoverResult:
    n_snapshots: int
    bucket_size: int
    average_top_turnover: float
    average_bottom_turnover: float
    average_rank_autocorrelation: float
    transitions: list[dict[str, float]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_snapshots": self.n_snapshots,
            "bucket_size": self.bucket_size,
            "average_top_turnover": self.average_top_turnover,
            "average_bottom_turnover": self.average_bottom_turnover,
            "average_rank_autocorrelation": self.average_rank_autocorrelation,
            "transitions": self.transitions,
        }


def _validate_snapshots(snapshots: list[dict[str, float]]) -> list[dict[str, float]]:
    if not isinstance(snapshots, list) or len(snapshots) < 2:
        raise ValueError("snapshots must contain at least two factor snapshots")
    names = set(snapshots[0])
    if len(names) < 2:
        raise ValueError("each snapshot must contain at least two assets")
    out: list[dict[str, float]] = []
    for snapshot in snapshots:
        if set(snapshot) != names:
            raise ValueError("all snapshots must contain the same assets")
        values: dict[str, float] = {}
        for name, raw in snapshot.items():
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
                raise ValueError("snapshot values must be finite numbers")
            values[str(name)] = float(raw)
        if len(set(values.values())) < 2:
            raise ValueError("factor snapshot must not be constant")
        out.append(values)
    return out


def _rank_map(snapshot: dict[str, float]) -> dict[str, float]:
    ordered = sorted(snapshot.items(), key=lambda item: (-item[1], item[0]))
    return {name: float(rank + 1) for rank, (name, _) in enumerate(ordered)}


def factor_turnover_report(snapshots: list[dict[str, float]], *, bucket_fraction: float = 0.2) -> FactorTurnoverResult:
    checked = _validate_snapshots(snapshots)
    if isinstance(bucket_fraction, bool) or not isinstance(bucket_fraction, (int, float)) or not math.isfinite(float(bucket_fraction)) or not 0 < float(bucket_fraction) <= 1:
        raise ValueError("bucket_fraction must be in (0, 1]")
    n_assets = len(checked[0])
    bucket_size = max(1, int(math.ceil(n_assets * float(bucket_fraction))))
    transitions: list[dict[str, float]] = []
    top_turnovers: list[float] = []
    bottom_turnovers: list[float] = []
    autocorrs: list[float] = []
    for prev, cur in zip(checked, checked[1:]):
        prev_order = [name for name, _ in sorted(prev.items(), key=lambda item: (-item[1], item[0]))]
        cur_order = [name for name, _ in sorted(cur.items(), key=lambda item: (-item[1], item[0]))]
        prev_top = set(prev_order[:bucket_size])
        cur_top = set(cur_order[:bucket_size])
        prev_bottom = set(prev_order[-bucket_size:])
        cur_bottom = set(cur_order[-bucket_size:])
        top_turnover = 1.0 - len(prev_top & cur_top) / bucket_size
        bottom_turnover = 1.0 - len(prev_bottom & cur_bottom) / bucket_size
        prev_rank = _rank_map(prev)
        cur_rank = _rank_map(cur)
        corr = pearson([prev_rank[name] for name in sorted(prev_rank)], [cur_rank[name] for name in sorted(cur_rank)])
        top_turnovers.append(top_turnover)
        bottom_turnovers.append(bottom_turnover)
        autocorrs.append(corr)
        transitions.append({"top_turnover": top_turnover, "bottom_turnover": bottom_turnover, "rank_autocorrelation": corr})
    return FactorTurnoverResult(
        n_snapshots=len(checked),
        bucket_size=bucket_size,
        average_top_turnover=sum(top_turnovers) / len(top_turnovers),
        average_bottom_turnover=sum(bottom_turnovers) / len(bottom_turnovers),
        average_rank_autocorrelation=sum(autocorrs) / len(autocorrs),
        transitions=transitions,
    )


__all__ = ["FactorTurnoverResult", "factor_turnover_report"]
