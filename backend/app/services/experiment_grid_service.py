from __future__ import annotations

from itertools import product

from pydantic import ValidationError

from app.schemas import BacktestParams, StrategyExperimentCreate, StrategyExperimentGridItem

GRID_LIMIT = 500
GRID_VALUE_PRECISION = 10
_EPS = 1e-12


class ExperimentGridService:
    """Generates parameter combinations from a strategy experiment grid."""

    @staticmethod
    def _expand_item(item: StrategyExperimentGridItem) -> list[float]:
        """Return the flat list of candidate values for one grid item."""
        if item.value is not None:
            return [item.value]
        if item.values is not None:
            return list(item.values)
        # range — iterate until exceeding end (with epsilon for float tolerance)
        r = item.range
        values: list[float] = []
        i = 0
        while True:
            v = round(r.start + i * r.step, GRID_VALUE_PRECISION)
            if v > r.end + _EPS:
                break
            values.append(v)
            i += 1
        return values

    def estimate_count(self, request: StrategyExperimentCreate) -> int:
        """Return the number of combinations *before* validation filtering."""
        total = 1
        for key in request.parameter_grid:
            total *= len(self._expand_item(request.parameter_grid[key]))
            if total > GRID_LIMIT:
                raise ValueError(
                    f"parameter grid produced {total} combinations, limit is {GRID_LIMIT}"
                )
        return total

    def expand(self, request: StrategyExperimentCreate) -> list[BacktestParams]:
        """Produce validated :class:`BacktestParams` for every grid combination.

        Combinations whose only validation failure is ``buy_low >= sell_high``
        are silently skipped; any other :class:`ValidationError` propagates.

        Raises :class:`ValueError` if the raw Cartesian product exceeds
        *GRID_LIMIT* or if every combination is invalid.
        """
        keys = list(request.parameter_grid.keys())
        candidates_lists = [self._expand_item(request.parameter_grid[k]) for k in keys]

        # Guard against runaway grids (defence in depth — estimate_count is
        # the primary check, but callers may skip it).
        total = 1
        for cl in candidates_lists:
            total *= len(cl)
        if total > GRID_LIMIT:
            raise ValueError(
                f"parameter grid produced {total} combinations, limit is {GRID_LIMIT}"
            )

        base = request.base_params.model_dump()
        results: list[BacktestParams] = []

        for combo in product(*candidates_lists):
            raw = {**base}
            for key, val in zip(keys, combo):
                raw[key] = val
            try:
                results.append(BacktestParams(**raw))
            except ValidationError as exc:
                if _is_buy_low_vs_sell_high_error(exc):
                    continue
                raise

        if not results:
            raise ValueError("parameter grid produced no valid combinations")

        return results


def _is_buy_low_vs_sell_high_error(exc: ValidationError) -> bool:
    """Return ``True`` when *every* error in the exception is the known
    ``sell_high must be greater than buy_low`` validator failure."""
    errors = exc.errors()
    if not errors:
        return False
    for err in errors:
        loc = err.get("loc", ())
        if not loc or loc[-1] != "sell_high":
            return False
        if err.get("type") != "value_error":
            return False
    return True
