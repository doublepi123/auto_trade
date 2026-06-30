"""P384: Higher Moment Forecast — rolling skewness/kurtosis + AR(1) prediction.

Computes rolling-window skewness and kurtosis time series, then fits an AR(1)
model to forecast future values. Reports persistence coefficients, forecasts,
and a predictability flag (persistence > 0.3).

Pure Python, deterministic. Frozen dataclass result with to_dict().
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = [
    "HigherMomentForecastResult",
    "higher_moment_forecast_report",
]

_MAX_SERIES = 5000
_MIN_WINDOW = 5


@dataclass(frozen=True)
class HigherMomentForecastResult:
    skewness_forecast: float
    kurtosis_forecast: float
    skewness_persistence: float
    kurtosis_persistence: float
    is_skewness_predictable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "skewness_forecast": self.skewness_forecast,
            "kurtosis_forecast": self.kurtosis_forecast,
            "skewness_persistence": self.skewness_persistence,
            "kurtosis_persistence": self.kurtosis_persistence,
            "is_skewness_predictable": self.is_skewness_predictable,
        }


def _validate_returns(values: Any, window: int) -> list[float]:
    if not isinstance(values, list):
        raise ValueError("returns must be a non-empty list of finite numbers")
    if not values:
        raise ValueError("returns must be a non-empty list of finite numbers")
    if len(values) > _MAX_SERIES:
        raise ValueError(f"returns must contain at most {_MAX_SERIES} values")
    if len(values) < window:
        raise ValueError(f"returns must contain at least {window} values")
    out: list[float] = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("returns entries must be finite numbers")
        number = float(v)
        if not math.isfinite(number):
            raise ValueError("returns entries must be finite numbers")
        out.append(number)
    return out


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: list[float], mu: float | None = None) -> float:
    if len(values) < 1:
        return 0.0
    m = mu if mu is not None else _mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / len(values))


def _skewness(values: list[float]) -> float:
    """Compute sample skewness of a window."""
    n = len(values)
    if n < 3:
        return 0.0
    mu = _mean(values)
    sd = _std(values, mu)
    if sd == 0:
        return 0.0
    m3 = sum((v - mu) ** 3 for v in values) / n
    return m3 / (sd ** 3)


def _kurtosis(values: list[float]) -> float:
    """Compute excess kurtosis of a window."""
    n = len(values)
    if n < 4:
        return 0.0
    mu = _mean(values)
    sd = _std(values, mu)
    if sd == 0:
        return 0.0
    m4 = sum((v - mu) ** 4 for v in values) / n
    return m4 / (sd ** 4) - 3.0


def _rolling_moments(
    returns: list[float], window: int
) -> tuple[list[float], list[float]]:
    """Compute rolling skewness and excess kurtosis series."""
    n = len(returns)
    skew_series: list[float] = []
    kurt_series: list[float] = []

    for i in range(n):
        if i + 1 < window:
            skew_series.append(0.0)
            kurt_series.append(0.0)
        else:
            window_data = returns[i + 1 - window : i + 1]
            skew_series.append(_skewness(window_data))
            kurt_series.append(_kurtosis(window_data))

    return skew_series, kurt_series


def _ar1_fit(series: list[float]) -> tuple[float, float, float]:
    """Fit AR(1): x_{t+1} = alpha + beta * x_t.

    Returns:
        (alpha, beta, r_squared)
    """
    n = len(series) - 1
    if n < 2:
        return 0.0, 0.0, 0.0

    x_lag = series[:-1]
    x_next = series[1:]

    mean_lag = _mean(x_lag)
    mean_next = _mean(x_next)

    # beta = Cov(x_t, x_{t+1}) / Var(x_t)
    cov = sum((x_lag[i] - mean_lag) * (x_next[i] - mean_next) for i in range(n))
    var_lag = sum((v - mean_lag) ** 2 for v in x_lag)

    if var_lag == 0:
        return mean_next, 0.0, 0.0

    beta = cov / var_lag
    alpha = mean_next - beta * mean_lag

    # R² for AR(1)
    ss_total = sum((v - mean_next) ** 2 for v in x_next)
    if ss_total == 0:
        r_squared = 1.0
    else:
        y_hat = [alpha + beta * x_lag[i] for i in range(n)]
        ss_res = sum((x_next[i] - y_hat[i]) ** 2 for i in range(n))
        r_squared = 1.0 - ss_res / ss_total

    return alpha, beta, max(r_squared, 0.0)


def _ar1_forecast(series: list[float]) -> tuple[float, float]:
    """Fit AR(1) to series, return (forecast_next, persistence_beta)."""
    alpha, beta, _r2 = _ar1_fit(series)
    last_value = series[-1] if series else 0.0
    forecast = alpha + beta * last_value
    return forecast, beta


def higher_moment_forecast_report(
    returns: list[float],
    *,
    window: int = 20,
    forecast_horizon: int = 1,
) -> HigherMomentForecastResult:
    """Forecast future skewness and kurtosis from rolling moments via AR(1).

    Args:
        returns: Return series.
        window: Rolling window size for moment estimation (default 20).
        forecast_horizon: Steps ahead to forecast (default 1, AR(1) single-step).

    Returns:
        HigherMomentForecastResult with forecasts, persistence coefficients,
        and predictability flag.

    Raises:
        ValueError: On invalid input.
    """
    # Validate window
    if isinstance(window, bool) or not isinstance(window, int):
        raise ValueError("window must be a positive int")
    if window < _MIN_WINDOW:
        raise ValueError(f"window must be at least {_MIN_WINDOW}")

    # Validate forecast_horizon
    if isinstance(forecast_horizon, bool) or not isinstance(forecast_horizon, int):
        raise ValueError("forecast_horizon must be a positive int")
    if forecast_horizon < 1:
        raise ValueError("forecast_horizon must be a positive int")

    returns_v = _validate_returns(returns, window)

    # Rolling moments
    skew_series, kurt_series = _rolling_moments(returns_v, window)

    # AR(1) forecast on the moment series (skip leading zeros)
    valid_skew = [s for s in skew_series if s != 0.0 or skew_series.index(s) < window - 1]
    # Actually, take the full series starting from when we have values
    skew_valid = skew_series[window - 1 :]
    kurt_valid = kurt_series[window - 1 :]

    if len(skew_valid) < 2:
        return HigherMomentForecastResult(
            skewness_forecast=0.0,
            kurtosis_forecast=0.0,
            skewness_persistence=0.0,
            kurtosis_persistence=0.0,
            is_skewness_predictable=False,
        )

    skew_forecast, skew_beta = _ar1_forecast(skew_valid)
    kurt_forecast, kurt_beta = _ar1_forecast(kurt_valid)

    return HigherMomentForecastResult(
        skewness_forecast=skew_forecast,
        kurtosis_forecast=kurt_forecast,
        skewness_persistence=max(skew_beta, 0.0),
        kurtosis_persistence=kurt_beta,
        is_skewness_predictable=abs(skew_beta) > 0.3,
    )
