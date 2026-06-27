"""P287: rolling performance tearsheet metrics."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from app.platform.factor_utils import mean, pearson, std, validate_pair, validate_series


@dataclass(frozen=True)
class RollingTearsheetResult:
    windows: dict[str, dict[str, list[float | None]]]
    summary: dict[str, float | int | None]

    def to_dict(self) -> dict[str, Any]:
        return {"windows": self.windows, "summary": self.summary}


def rolling_tearsheet_report(returns: list[float], *, benchmark: list[float] | None = None, windows: list[int] | None = None, periods_per_year: int = 252) -> RollingTearsheetResult:
    rets = validate_series(returns, name="returns", min_len=2)
    if isinstance(periods_per_year, bool) or periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    bench = None
    if benchmark is not None:
        _, bench = validate_pair(rets, benchmark, x_name="returns", y_name="benchmark")
    win_list = windows or [20]
    out: dict[str, dict[str, list[float | None]]] = {}
    best = worst = None
    for window in win_list:
        if isinstance(window, bool) or not isinstance(window, int) or window < 2 or window > len(rets):
            raise ValueError("windows must be ints in [2, len(returns)]")
        sharpe: list[float | None] = [None] * len(rets)
        mdd: list[float | None] = [None] * len(rets)
        beta: list[float | None] = [None] * len(rets)
        alpha: list[float | None] = [None] * len(rets)
        for i in range(window - 1, len(rets)):
            vals = rets[i - window + 1 : i + 1]
            sigma = std(vals, sample=True)
            sr = 0.0 if sigma == 0 else mean(vals) / sigma * math.sqrt(periods_per_year)
            sharpe[i] = sr
            mdd[i] = _max_drawdown(vals)
            if bench is not None:
                bvals = bench[i - window + 1 : i + 1]
                bs = std(bvals)
                beta_value = 0.0 if bs == 0 else pearson(vals, bvals) * (std(vals) / bs)
                beta[i] = beta_value
                alpha[i] = (mean(vals) - beta_value * mean(bvals)) * periods_per_year
            best = sr if best is None or sr > best else best
            worst = sr if worst is None or sr < worst else worst
        out[str(window)] = {"rolling_sharpe": sharpe, "rolling_max_drawdown": mdd, "rolling_beta": beta, "rolling_alpha": alpha}
    return RollingTearsheetResult(out, {"best_sharpe": best, "worst_sharpe": worst})


def _max_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    dd = 0.0
    for ret in returns:
        equity *= 1.0 + ret
        peak = max(peak, equity)
        dd = min(dd, equity / peak - 1.0)
    return dd


__all__ = ["RollingTearsheetResult", "rolling_tearsheet_report"]
