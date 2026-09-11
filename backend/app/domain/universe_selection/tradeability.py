"""Pure cost-to-trade ranking; callers must label the basis, never claim edge."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from math import isfinite


@dataclass(frozen=True, slots=True)
class TradeabilityInput:
    symbol: str
    market: str
    price: float | None
    avg_dollar_volume: float | None
    relative_spread_bps: float | None
    atr_pct_14d: float | None
    opportunity_to_cost_ratio: float | None


@dataclass(frozen=True, slots=True)
class TradeabilityRow:
    symbol: str
    market: str
    rank: int | None
    eligible: bool
    reasons: tuple[str, ...]
    board_lot_uncertain: bool
    price: float | None
    avg_dollar_volume: float | None
    relative_spread_bps: float | None
    atr_pct_14d: float | None
    opportunity_to_cost_ratio: float | None


def _finite_or_none(value: float | None) -> float | None:
    return value if value is not None and isfinite(value) else None


def rank_tradeability(
    rows: Sequence[TradeabilityInput],
    *,
    min_price: float,
    max_spread_bps: float,
    min_avg_dollar_volume: float,
) -> tuple[TradeabilityRow, ...]:
    """Rank cost and liquidity only; optional metrics carry no edge evidence."""
    eligible: list[tuple[float, float, str, TradeabilityRow]] = []
    ineligible: list[TradeabilityRow] = []
    for item in rows:
        price = _finite_or_none(item.price)
        spread = _finite_or_none(item.relative_spread_bps)
        volume = _finite_or_none(item.avg_dollar_volume)
        reasons = [
            f"METRIC_MISSING:{name}"
            for name, value in (
                ("price", price),
                ("relative_spread_bps", spread),
                ("avg_dollar_volume", volume),
            )
            if value is None
        ]
        if price is not None and price < min_price:
            reasons.append("PRICE_BELOW_MINIMUM")
        if spread is not None and spread > max_spread_bps:
            reasons.append("SPREAD_ABOVE_MAXIMUM")
        if volume is not None and volume < min_avg_dollar_volume:
            reasons.append("DOLLAR_VOLUME_BELOW_MINIMUM")
        row = TradeabilityRow(
            symbol=item.symbol,
            market=item.market,
            rank=None,
            eligible=not reasons,
            reasons=tuple(reasons),
            board_lot_uncertain=item.market == "HK" or item.symbol.endswith(".HK"),
            price=price,
            avg_dollar_volume=volume,
            relative_spread_bps=spread,
            atr_pct_14d=_finite_or_none(item.atr_pct_14d),
            opportunity_to_cost_ratio=_finite_or_none(item.opportunity_to_cost_ratio),
        )
        if not reasons and spread is not None and volume is not None:
            eligible.append((spread, -volume, item.symbol, row))
        else:
            ineligible.append(row)
    eligible.sort(key=lambda entry: entry[:3])
    ineligible.sort(key=lambda row: row.symbol)
    return (
        *(replace(entry[3], rank=rank) for rank, entry in enumerate(eligible, start=1)),
        *ineligible,
    )
