from __future__ import annotations
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas import (
    BacktestParams,
    StrategyExperimentCreate,
    StrategyExperimentGridItem,
    StrategyExperimentGridRange,
)
from app.services.experiment_grid_service import ExperimentGridService

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_request(
    *,
    base_overrides: dict[str, Any] | None = None,
    grid: dict[str, Any] | None = None,
    symbol: str = "AAPL.US",
    name: str = "test-exp",
) -> StrategyExperimentCreate:
    defaults: dict[str, Any] = {
        "symbol": symbol,
        "buy_low": 100.0,
        "sell_high": 110.0,
    }
    if base_overrides:
        defaults.update(base_overrides)
    base = BacktestParams(**defaults)
    return StrategyExperimentCreate(
        name=name,
        symbol=base.symbol,
        base_params=base,
        parameter_grid=grid or {},
    )


def _grid_value(v: float) -> StrategyExperimentGridItem:
    return StrategyExperimentGridItem(value=v)


def _grid_values(*vs: float) -> StrategyExperimentGridItem:
    return StrategyExperimentGridItem(values=list(vs))


def _grid_range(start: float, end: float, step: float) -> StrategyExperimentGridItem:
    return StrategyExperimentGridItem(
        range=StrategyExperimentGridRange(start=start, end=end, step=step)
    )


# ── _expand_item ─────────────────────────────────────────────────────────────


class TestExpandItem:
    def test_value(self) -> None:
        assert ExperimentGridService._expand_item(_grid_value(42.0)) == [42.0]

    def test_values(self) -> None:
        assert ExperimentGridService._expand_item(_grid_values(1.0, 2.0, 3.0)) == [1.0, 2.0, 3.0]

    def test_range_inclusive_end(self) -> None:
        result = ExperimentGridService._expand_item(_grid_range(0.0, 1.0, 0.5))
        assert result == [0.0, 0.5, 1.0]

    def test_range_single_value(self) -> None:
        result = ExperimentGridService._expand_item(_grid_range(5.0, 5.0, 1.0))
        assert result == [5.0]


# ── expand: deterministic order ──────────────────────────────────────────────


class TestExpandDeterministic:
    def test_key_order_preserved(self) -> None:
        """Combinations follow insertion order of parameter_grid keys."""
        req = _make_request(
            grid={
                "buy_low": _grid_values(100.0, 101.0),
                "sell_high": _grid_values(200.0, 201.0),
                "quantity": _grid_value(1.0),
            },
        )
        svc = ExperimentGridService()
        results = svc.expand(req)
        assert len(results) == 4
        # buy_low varies first (outermost), sell_high second, quantity fixed
        assert results[0].buy_low == 100.0 and results[0].sell_high == 200.0
        assert results[1].buy_low == 100.0 and results[1].sell_high == 201.0
        assert results[2].buy_low == 101.0 and results[2].sell_high == 200.0
        assert results[3].buy_low == 101.0 and results[3].sell_high == 201.0

    def test_mixed_value_types(self) -> None:
        """Single value, list, and range all contribute."""
        req = _make_request(
            grid={
                "buy_low": _grid_value(100.0),
                "quantity": _grid_values(1.0, 2.0),
                "slippage_pct": _grid_range(0.0, 1.0, 1.0),
            },
        )
        svc = ExperimentGridService()
        results = svc.expand(req)
        # 1 × 2 × 2 = 4 combos
        assert len(results) == 4
        quantities = sorted({r.quantity for r in results})
        slippages = sorted({r.slippage_pct for r in results})
        assert quantities == [1.0, 2.0]
        assert slippages == [0.0, 1.0]


# ── expand: buy_low >= sell_high rejection ───────────────────────────────────


class TestExpandBuyLowSellHighRejection:
    def test_skips_invalid_keeps_valid(self) -> None:
        """Combinations with buy_low >= sell_high are silently dropped."""
        req = _make_request(
            grid={
                "buy_low": _grid_values(150.0, 200.0),
                "sell_high": _grid_values(155.0, 180.0),
            },
        )
        svc = ExperimentGridService()
        results = svc.expand(req)
        # 150/155 → OK (150 < 155)
        # 150/180 → OK (150 < 180)
        # 200/155 → skip (200 > 155)
        # 200/180 → skip (200 > 180)
        assert len(results) == 2
        assert all(r.buy_low < r.sell_high for r in results)
        assert {r.buy_low for r in results} == {150.0}

    def test_equal_values_rejected(self) -> None:
        """buy_low == sell_high is also invalid."""
        req = _make_request(
            grid={
                "buy_low": _grid_value(100.0),
                "sell_high": _grid_value(100.0),
            },
        )
        svc = ExperimentGridService()
        with pytest.raises(ValueError, match="no valid combinations"):
            svc.expand(req)

    def test_all_invalid_raises(self) -> None:
        req = _make_request(
            grid={
                "buy_low": _grid_values(200.0, 300.0),
                "sell_high": _grid_values(100.0, 150.0),
            },
        )
        svc = ExperimentGridService()
        with pytest.raises(ValueError, match="parameter grid produced no valid combinations"):
            svc.expand(req)

    def test_other_validation_errors_propagate(self) -> None:
        """A non-sell_high validation error must not be swallowed."""
        req = _make_request(
            base_overrides={"buy_low": 100.0, "sell_high": 200.0},
            grid={
                "fee_rate": _grid_value(99.0),  # fee_rate le=0.1
            },
        )
        svc = ExperimentGridService()
        with pytest.raises(ValidationError):
            svc.expand(req)


# ── estimate_count ───────────────────────────────────────────────────────────


class TestEstimateCount:
    def test_simple_product(self) -> None:
        req = _make_request(
            grid={
                "buy_low": _grid_values(1.0, 2.0),
                "quantity": _grid_values(10.0, 20.0, 30.0),
            },
        )
        svc = ExperimentGridService()
        assert svc.estimate_count(req) == 6

    def test_limits_at_500(self) -> None:
        """501 candidates must raise."""
        req = _make_request(
            grid={
                "buy_low": _grid_values(*range(501)),
            },
        )
        svc = ExperimentGridService()
        with pytest.raises(ValueError, match="parameter grid produced 501 combinations"):
            svc.estimate_count(req)

    def test_exact_500_passes(self) -> None:
        req = _make_request(
            grid={
                "buy_low": _grid_values(*range(500)),
            },
        )
        svc = ExperimentGridService()
        assert svc.estimate_count(req) == 500


# ── expand: limit enforcement ────────────────────────────────────────────────


class TestExpandLimit:
    def test_over_500_raises(self) -> None:
        req = _make_request(
            grid={
                "buy_low": _grid_values(*range(501)),
            },
        )
        svc = ExperimentGridService()
        with pytest.raises(ValueError, match="parameter grid produced 501 combinations"):
            svc.expand(req)


# ── decimal precision ────────────────────────────────────────────────────────


class TestDecimalPrecision:
    def test_range_avoids_float_drift(self) -> None:
        """Step values that cause IEEE 754 drift must be rounded away."""
        result = ExperimentGridService._expand_item(_grid_range(0.0, 1.0, 0.1))
        expected = [round(i * 0.1, 10) for i in range(11)]  # 0.0 through 1.0
        assert result == expected
        # Concrete spot-check
        assert result[3] == 0.3
        # Without rounding, 0.1*3 can be 0.30000000000000004
        assert result[3] != 0.30000000000000004  # type: ignore[comparison-overlap]

    def test_negative_range_precision(self) -> None:
        result = ExperimentGridService._expand_item(_grid_range(-1.0, 1.0, 0.3))
        # With new non-overshoot logic: values up to 0.8 (7 values),
        # 1.1 exceeds 1.0 + epsilon and is excluded.
        assert len(result) == 7
        assert result[0] == -1.0
        assert result[-1] == round(-1.0 + 6 * 0.3, 10)  # 0.8

    def test_no_drift_in_expand_output(self) -> None:
        """End-to-end: expanded BacktestParams have clean float values."""
        req = _make_request(
            grid={
                "slippage_pct": _grid_range(0.0, 0.5, 0.1),
            },
        )
        svc = ExperimentGridService()
        results = svc.expand(req)
        values = [r.slippage_pct for r in results]
        assert values == [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]
        for v in values:
            # Round-tripping through rounding must not reveal hidden digits
            assert round(v, 10) == v


# ── edge cases ───────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_single_item_single_value(self) -> None:
        req = _make_request(grid={"buy_low": _grid_value(42.0)})
        svc = ExperimentGridService()
        results = svc.expand(req)
        assert len(results) == 1
        assert results[0].buy_low == 42.0

    def test_base_params_preserved_for_non_grid_fields(self) -> None:
        """Fields not in parameter_grid keep their base_params values."""
        req = _make_request(
            base_overrides={
                "symbol": "TSLA.US",
                "buy_low": 50.0,
                "sell_high": 80.0,
                "max_daily_loss": 999.0,
                "stop_loss_pct": 3.0,
            },
            grid={"buy_low": _grid_values(50.0, 55.0)},
        )
        svc = ExperimentGridService()
        results = svc.expand(req)
        assert len(results) == 2
        for r in results:
            assert r.symbol == "TSLA.US"
            assert r.sell_high == 80.0
            assert r.max_daily_loss == 999.0
            assert r.stop_loss_pct == 3.0
            assert r.buy_low in (50.0, 55.0)


# ── acceptance: range boundary (non‑divisible step) ──────────────────────────


class TestRangeBoundaryNonDivisible:
    """Verify that range expansion never overshoots *end* when step
    does not divide the span evenly, as required by P16 Task 2 QA."""

    def test_non_divisible_excludes_overshoot(self) -> None:
        """range(0, 1, 0.3) → [0, 0.3, 0.6, 0.9]; 1.2 is excluded."""
        req = _make_request(
            grid={"slippage_pct": _grid_range(0.0, 1.0, 0.3)},
        )
        svc = ExperimentGridService()
        results = svc.expand(req)
        values = sorted(r.slippage_pct for r in results)
        assert values == [0.0, 0.3, 0.6, 0.9]

    def test_exact_end_included(self) -> None:
        """range(0, 1, 0.25) includes 1.0 (evenly divisible)."""
        req = _make_request(
            grid={"slippage_pct": _grid_range(0.0, 1.0, 0.25)},
        )
        svc = ExperimentGridService()
        results = svc.expand(req)
        values = sorted(r.slippage_pct for r in results)
        assert values == [0.0, 0.25, 0.5, 0.75, 1.0]

    def test_large_non_divisible_does_not_crash(self) -> None:
        """slippage_pct 0..5 step 0.3 must succeed without overshoot ValueError."""
        req = _make_request(
            grid={"slippage_pct": _grid_range(0.0, 5.0, 0.3)},
        )
        svc = ExperimentGridService()
        results = svc.expand(req)
        values = sorted(r.slippage_pct for r in results)
        # 0, 0.3, …, 4.8 (17 values); 5.1 is excluded
        expected = [round(i * 0.3, 10) for i in range(17)]
        assert values == expected
        assert values[-1] == 4.8

    def test_estimate_count_matches_expand(self) -> None:
        """estimate_count must agree with expand for non‑divisible ranges."""
        req = _make_request(
            grid={
                "slippage_pct": _grid_range(0.0, 1.0, 0.3),
                "buy_low": _grid_value(50.0),
            },
        )
        svc = ExperimentGridService()
        assert svc.estimate_count(req) == 4
        assert len(svc.expand(req)) == 4
