from __future__ import annotations

from dataclasses import replace

from app.domain.universe_selection.tradeability import (
    TradeabilityInput,
    rank_tradeability,
)


def test_ranks_by_spread_then_dollar_volume() -> None:
    # Given: equal spreads, with optional metrics opposing the volume ranking.
    rows = [
        TradeabilityInput("AAA.US", "US", 10.0, 2_000_000.0, 5.0, 9.0, 90.0),
        TradeabilityInput("ZZZ.US", "US", 10.0, 3_000_000.0, 5.0, None, None),
        TradeabilityInput("BBB.US", "US", 10.0, 9_000_000.0, 6.0, 10.0, 100.0),
    ]
    # When
    result = rank_tradeability(rows, min_price=5.0, max_spread_bps=10.0, min_avg_dollar_volume=1_000_000.0)
    # Then
    assert [(row.symbol, row.rank) for row in result] == [("ZZZ.US", 1), ("AAA.US", 2), ("BBB.US", 3)]
    assert all(row.eligible and row.reasons == () for row in result)
    assert result[0].atr_pct_14d is None
    assert result[1].opportunity_to_cost_ratio == 90.0


def test_hard_filters_exclude_with_named_reasons() -> None:
    # Given: one row per filter, plus combined failures and a missing metric.
    rows = [
        TradeabilityInput("A", "US", 4.0, 1_000_000.0, 10.0, None, None),
        TradeabilityInput("B", "US", 5.0, 1_000_000.0, 11.0, None, None),
        TradeabilityInput("C", "US", 5.0, 999_999.0, 10.0, None, None),
        TradeabilityInput("D", "US", 4.0, 999_999.0, 11.0, None, None),
        TradeabilityInput("E", "US", 4.0, 999_999.0, None, None, None),
    ]
    expected = (
        ("PRICE_BELOW_MINIMUM",),
        ("SPREAD_ABOVE_MAXIMUM",),
        ("DOLLAR_VOLUME_BELOW_MINIMUM",),
        ("PRICE_BELOW_MINIMUM", "SPREAD_ABOVE_MAXIMUM", "DOLLAR_VOLUME_BELOW_MINIMUM"),
        ("METRIC_MISSING:relative_spread_bps", "PRICE_BELOW_MINIMUM", "DOLLAR_VOLUME_BELOW_MINIMUM"),
    )
    # When
    result = rank_tradeability(rows, min_price=5.0, max_spread_bps=10.0, min_avg_dollar_volume=1_000_000.0)
    # Then
    for row, reasons in zip(result, expected, strict=True):
        assert row.eligible is False
        assert row.rank is None
        assert row.reasons == reasons


def test_missing_metric_is_ineligible_with_metric_name() -> None:
    # Given: None and every non-finite form in each required metric.
    base = TradeabilityInput("A", "US", 5.0, 1_000_000.0, 10.0, None, None)
    cases = [
        (replace(base, relative_spread_bps=value), ("METRIC_MISSING:relative_spread_bps",))
        for value in (None, float("nan"), float("inf"), -float("inf"))
    ] + [
        (replace(base, price=value), ("METRIC_MISSING:price",))
        for value in (None, float("nan"), float("inf"), -float("inf"))
    ] + [
        (replace(base, avg_dollar_volume=value), ("METRIC_MISSING:avg_dollar_volume",))
        for value in (None, float("nan"), float("inf"), -float("inf"))
    ] + [
        (replace(base, price=None, relative_spread_bps=None, avg_dollar_volume=None), (
            "METRIC_MISSING:price", "METRIC_MISSING:relative_spread_bps", "METRIC_MISSING:avg_dollar_volume",
        )),
    ]
    for candidate, reasons in cases:
        # When
        result = rank_tradeability([candidate], min_price=5.0, max_spread_bps=10.0, min_avg_dollar_volume=1_000_000.0)
        # Then
        assert result[0].eligible is False
        assert result[0].rank is None
        assert result[0].reasons == reasons


def test_hk_symbol_is_flagged_not_excluded() -> None:
    # Given: either HK identifier suffices; equality at each threshold passes.
    rows = [
        TradeabilityInput("0005.HK", "US", 5.0, 1_000_000.0, 10.0, None, None),
        TradeabilityInput("0005", "HK", 5.0, 1_000_000.0, 10.0, None, None),
        TradeabilityInput("AAA.US", "US", 5.0, 1_000_000.0, 10.0, None, None),
    ]
    # When
    result = rank_tradeability(rows, min_price=5.0, max_spread_bps=10.0, min_avg_dollar_volume=1_000_000.0)
    # Then
    assert [row.symbol for row in result] == ["0005", "0005.HK", "AAA.US"]
    for row in result[:2]:
        assert row.board_lot_uncertain is True
        assert row.eligible is True
        assert row.rank is not None
    assert result[2].board_lot_uncertain is False


def test_ranking_is_deterministic_and_dense() -> None:
    # Given: ties, exclusions, and a fixed permutation of the same inputs.
    rows = [
        TradeabilityInput("Z", "US", 4.0, 2_000_000.0, 1.0, None, None),
        TradeabilityInput("C", "US", 5.0, 2_000_000.0, 5.0, 1.0, 1.0),
        TradeabilityInput("B", "US", 5.0, 2_000_000.0, 5.0, 9.0, 9.0),
        TradeabilityInput("A", "US", 5.0, 2_000_000.0, 2.0, None, None),
        TradeabilityInput("D", "US", None, 2_000_000.0, 1.0, None, None),
    ]
    # When
    outputs = tuple(
        rank_tradeability(candidates, min_price=5.0, max_spread_bps=10.0, min_avg_dollar_volume=1_000_000.0)
        for candidates in (rows, [rows[i] for i in (4, 2, 0, 3, 1)], [])
    )
    # Then
    assert outputs[0] == outputs[1]
    assert [row.symbol for row in outputs[0]] == ["A", "B", "C", "D", "Z"]
    assert [row.rank for row in outputs[0]] == [1, 2, 3, None, None]
    assert outputs[2] == ()
