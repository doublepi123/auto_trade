"""P376: Price discovery metrics — Hasbrouck/IS and Gonzalo-Granger PT.

Pure-Python implementation of approximate price discovery metrics for
multi-venue trading. Given synchronized prices from multiple venues,
computes Hasbrouck information shares (variance decomposition of the
common efficient price innovation) and the Gonzalo-Granger permanent
component metric via normalized first-venue variance contribution.

Reference: Hasbrouck, J. (1995). "One Security, Many Markets: Determining
the Contributions to Price Discovery".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PriceDiscoveryResult:
    """Frozen carrier for price discovery analysis results."""

    information_shares: dict[str, float]
    dominant_venue: str
    price_discovery_ratio: float
    n_venues: int
    n_observations: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "information_shares": self.information_shares,
            "dominant_venue": self.dominant_venue,
            "price_discovery_ratio": self.price_discovery_ratio,
            "n_venues": self.n_venues,
            "n_observations": self.n_observations,
        }


def _validate_venues(venues: dict[str, list[float]]) -> tuple[list[str], list[list[float]], int, int]:
    """Validate venue price data, returning sorted names, price matrix, m venues, n obs."""
    if not isinstance(venues, dict) or not venues:
        raise ValueError("venues must be a non-empty dict of {venue_name: [prices]}")
    if len(venues) < 2:
        raise ValueError("venues must contain at least 2 venues")
    if len(venues) > 50:
        raise ValueError("venues must contain at most 50 venues")

    m = len(venues)
    n: int | None = None
    names: list[str] = []
    matrix: list[list[float]] = []

    for name, series in venues.items():
        if isinstance(series, (str, dict)) or not isinstance(series, list):
            raise ValueError(f"venues['{name}'] must be a list of finite numbers")
        if not series:
            raise ValueError(f"venues['{name}'] must be non-empty")

        validated: list[float] = []
        for v in series:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"venues['{name}'] entries must be finite numbers")
            fv = float(v)
            if not math.isfinite(fv) or fv <= 0:
                raise ValueError(f"venues['{name}'] entries must be finite positive numbers")
            validated.append(fv)

        if n is None:
            n = len(validated)
        elif len(validated) != n:
            raise ValueError("venue price series must have equal length")
        names.append(str(name))
        matrix.append(validated)

    if n is None or n < 2:
        raise ValueError("venues must contain at least 2 observations per venue")

    return names, matrix, m, n


def _first_differences(prices: list[float]) -> list[float]:
    """Compute first differences (price innovations)."""
    diffs: list[float] = []
    for i in range(1, len(prices)):
        diffs.append(prices[i] - prices[i - 1])
    return diffs


def _variance(series: list[float]) -> float:
    """Sample variance."""
    if len(series) < 2:
        return 0.0
    mean = sum(series) / len(series)
    return sum((x - mean) ** 2 for x in series) / (len(series) - 1)


def _covariance(x: list[float], y: list[float]) -> float:
    """Sample covariance between two equal-length series."""
    if len(x) < 2 or len(x) != len(y):
        return 0.0
    mx = sum(x) / len(x)
    my = sum(y) / len(y)
    return sum((x[i] - mx) * (y[i] - my) for i in range(len(x))) / (len(x) - 1)


def price_discovery_report(venues: dict[str, list[float]]) -> PriceDiscoveryResult:
    """Compute price discovery metrics using Hasbrouck / Gonzalo-Granger approximations.

    Parameters
    ----------
    venues:
        Dict mapping venue name to list of synchronized prices.
        All venue series must be equal-length, non-empty.
        Minimum 2 venues, maximum 50.

    Returns
    -------
    PriceDiscoveryResult with information shares, dominant venue, and ratio.
    """
    names, matrix, m, n = _validate_venues(venues)

    # Compute first differences for each venue
    diffs: list[list[float]] = []
    for prices in matrix:
        diffs.append(_first_differences(prices))

    T = n - 1  # number of differences

    # Naive variance-share approximation (not true Hasbrouck IS):
    # Compute the variance of each venue's price innovations
    # and the sum of all variances.
    # IS_i = variance(diff_i) / sum_all(variance(diff_j))
    variances: list[float] = []
    total_var = 0.0
    for diff in diffs:
        v = _variance(diff)
        variances.append(v)
        total_var += v

    # If total variance is zero (all flat), use equal shares
    if total_var <= 0:
        information_shares = {name: 1.0 / m for name in names}
    else:
        information_shares = {}
        for i, name in enumerate(names):
            information_shares[name] = variances[i] / total_var

    # Dominant venue: max IS
    dominant_venue = max(information_shares, key=lambda k: information_shares[k])

    # Naive variance-share proxy (not true Gonzalo-Granger PT):
    # The permanent component of the price is approximated by
    # the common factor derived from the first venue's variance
    # contribution. Price discovery ratio = first_venue_variance / (sum of all variances).
    first_venue_share = variances[0] / total_var if total_var > 0 else 1.0 / m
    price_discovery_ratio = first_venue_share  # Note: this is a naive variance-share, not GG PT

    return PriceDiscoveryResult(
        information_shares=information_shares,
        dominant_venue=dominant_venue,
        price_discovery_ratio=price_discovery_ratio,
        n_venues=m,
        n_observations=n,
    )
