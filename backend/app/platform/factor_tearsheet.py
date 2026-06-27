"""P284: compact factor tearsheet aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.platform.factor_data_quality import factor_data_quality_report
from app.platform.factor_ic import pearson_corr, spearman_corr
from app.platform.factor_quantiles import factor_quantile_report
from app.platform.factor_turnover import factor_turnover_report
from app.platform.factor_utils import mean


@dataclass(frozen=True)
class FactorTearsheetResult:
    summary: dict[str, float | int | None]
    ic_by_date: list[dict[str, float | str]]

    def to_dict(self) -> dict[str, Any]:
        return {"summary": self.summary, "ic_by_date": self.ic_by_date}


def factor_tearsheet_report(records: list[dict[str, Any]], *, n_quantiles: int = 5, bucket_fraction: float = 0.2) -> FactorTearsheetResult:
    if not isinstance(records, list) or not records:
        raise ValueError("records must be non-empty")
    by_date: dict[str, list[tuple[str, float, float]]] = {}
    panel: dict[str, list[float | None]] = {}
    for rec in records:
        if not isinstance(rec, dict) or not {"date", "symbol", "factor", "forward_return"}.issubset(rec):
            raise ValueError("records must contain date, symbol, factor, forward_return")
        date = str(rec["date"])
        symbol = str(rec["symbol"])
        factor = float(rec["factor"])
        ret = float(rec["forward_return"])
        by_date.setdefault(date, []).append((symbol, factor, ret))
        panel.setdefault(symbol, []).append(factor)
    ic_rows: list[dict[str, float | str]] = []
    all_factors: list[float] = []
    all_returns: list[float] = []
    snapshots: list[dict[str, float]] = []
    for date, rows in sorted(by_date.items()):
        if len(rows) < 2:
            continue
        factors = [row[1] for row in rows]
        returns = [row[2] for row in rows]
        ic_rows.append({"date": date, "ic": pearson_corr(factors, returns), "rank_ic": spearman_corr(factors, returns)})
        snapshots.append({row[0]: row[1] for row in rows})
        all_factors.extend(factors)
        all_returns.extend(returns)
    if len(all_factors) < 2:
        raise ValueError("records must contain at least one date with two assets")
    quant = factor_quantile_report(all_factors, all_returns, n_quantiles=min(n_quantiles, len(all_factors))).to_dict()
    quality = factor_data_quality_report(panel).to_dict()
    turnover = factor_turnover_report(snapshots, bucket_fraction=bucket_fraction).average_top_turnover if len(snapshots) >= 2 else None
    return FactorTearsheetResult({"mean_ic": mean([float(row["ic"]) for row in ic_rows]), "mean_rank_ic": mean([float(row["rank_ic"]) for row in ic_rows]), "top_bottom_spread": float(quant["top_bottom_spread"]), "turnover_avg": turnover, "quality_score": float(quality["quality_score"]), "n_records": len(records)}, ic_rows)


__all__ = ["FactorTearsheetResult", "factor_tearsheet_report"]
