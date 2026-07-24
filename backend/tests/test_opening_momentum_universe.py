from __future__ import annotations

from app.domain.opening_momentum_universe import (
    OpeningMomentumUniverseCandidate,
    OpeningMomentumUniverseConfig,
    opening_momentum_variant_config_version,
    select_opening_momentum_universe,
)


def _candidate(
    symbol: str,
    *,
    sector: str = "Technology",
    trend_efficiency: float = 0.5,
    reasons: tuple[str, ...] = (),
) -> OpeningMomentumUniverseCandidate:
    return OpeningMomentumUniverseCandidate(
        symbol=symbol,
        sector=sector,
        avg_dollar_volume=1_000_000_000.0,
        relative_spread_bps=1.0,
        opportunity_to_cost_ratio=12.0,
        momentum_5d_pct=2.0,
        trend_efficiency_10d=trend_efficiency,
        exclusion_reasons=reasons,
    )


def _trend_only_config(
    *,
    max_selected: int = 2,
    max_per_sector: int = 2,
) -> OpeningMomentumUniverseConfig:
    return OpeningMomentumUniverseConfig(
        max_selected=max_selected,
        max_per_sector=max_per_sector,
        liquidity_weight=0.0,
        spread_weight=0.0,
        opportunity_weight=0.0,
        momentum_weight=0.0,
        trend_efficiency_weight=1.0,
    )


def test_continuation_selector_prefers_higher_trend_efficiency() -> None:
    selected = select_opening_momentum_universe(
        [
            _candidate("CHOP.US", trend_efficiency=0.1),
            _candidate("TREND.US", trend_efficiency=0.9),
        ],
        _trend_only_config(max_selected=1),
    )

    assert selected[0].symbol == "TREND.US"
    assert selected[0].selected is True
    assert selected[0].rank == 1
    assert selected[1].exclusion_reasons == (
        "BELOW_SELECTION_CUTOFF",
    )


def test_soft_incumbent_reasons_are_reconsidered_but_hard_gates_remain() -> None:
    selected = select_opening_momentum_universe(
        [
            _candidate(
                "SOFT.US",
                trend_efficiency=0.9,
                reasons=("BELOW_SELECTION_CUTOFF",),
            ),
            _candidate(
                "HARD.US",
                trend_efficiency=1.0,
                reasons=("DOLLAR_VOLUME_BELOW_MINIMUM",),
            ),
        ],
        _trend_only_config(max_selected=1),
    )
    by_symbol = {row.symbol: row for row in selected}

    assert by_symbol["SOFT.US"].selected is True
    assert by_symbol["HARD.US"].selected is False
    assert by_symbol["HARD.US"].exclusion_reasons == (
        "DOLLAR_VOLUME_BELOW_MINIMUM",
    )


def test_sector_cap_is_applied_before_global_cutoff() -> None:
    selected = select_opening_momentum_universe(
        [
            _candidate(
                "TECH1.US",
                sector="Technology",
                trend_efficiency=1.0,
            ),
            _candidate(
                "TECH2.US",
                sector="Technology",
                trend_efficiency=0.9,
            ),
            _candidate(
                "HEALTH.US",
                sector="Health Care",
                trend_efficiency=0.8,
            ),
        ],
        _trend_only_config(
            max_selected=2,
            max_per_sector=1,
        ),
    )
    by_symbol = {row.symbol: row for row in selected}

    assert [
        row.symbol for row in selected if row.selected
    ] == ["TECH1.US", "HEALTH.US"]
    assert by_symbol["TECH2.US"].exclusion_reasons == ("SECTOR_CAP",)


def test_selection_and_variant_version_are_order_deterministic() -> None:
    config = _trend_only_config(max_selected=2)
    candidates = [
        _candidate("B.US", trend_efficiency=0.5),
        _candidate("A.US", trend_efficiency=0.5),
    ]

    forward = select_opening_momentum_universe(candidates, config)
    reverse = select_opening_momentum_universe(
        list(reversed(candidates)),
        config,
    )

    assert [
        (row.symbol, row.rank, row.score)
        for row in forward
    ] == [
        (row.symbol, row.rank, row.score)
        for row in reverse
    ]
    assert opening_momentum_variant_config_version(
        "base-version",
        config,
    ) == opening_momentum_variant_config_version(
        "base-version",
        config,
    )
    assert opening_momentum_variant_config_version(
        "base-version",
        config,
    ) != opening_momentum_variant_config_version(
        "other-base-version",
        config,
    )
