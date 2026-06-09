from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_BB_SQUEEZE_WIDTH_THRESHOLD = 0.05  # Bollinger squeeze threshold
_ATR_HIGH_VOLATILITY_THRESHOLD = 0.03  # High volatility ATR threshold


@dataclass
class MarketState:
    """Market state detected from technical indicators."""

    state: str  # "trending" | "ranging" | "volatile" | "neutral"
    confidence: float  # 0.0-1.0
    description: str  # Human-readable description
    suggested_indicators: list[str]  # Recommended indicators


class MarketStateDetector:
    """Detect market state based on technical indicators."""

    @staticmethod
    def detect(
        adx: dict[str, Any],
        bb_upper: float,
        bb_middle: float,
        bb_lower: float,
        atr: float,
        current_price: float,
        volume_analysis: dict[str, Any],
    ) -> MarketState:
        """Detect market state from indicators."""
        # Default for insufficient data
        if not adx or bb_middle <= 0:
            return MarketState(
                state="neutral",
                confidence=0.5,
                description="数据不足，无法判断市场状态",
                suggested_indicators=["rsi", "macd", "atr", "vwap"],
            )

        adx_value = float(adx.get("adx_value", 0))
        di_plus = float(adx.get("di_plus", 0))
        di_minus = float(adx.get("di_minus", 0))

        # 1. Trending market
        if adx_value > 25 and abs(di_plus - di_minus) > 10:
            direction = "上升" if di_plus > di_minus else "下降"
            return MarketState(
                state="trending",
                confidence=min(adx_value / 50, 1.0),
                description=f"{direction}趋势（ADX={adx_value:.1f}, DI+={di_plus:.1f}, DI-={di_minus:.1f}）",
                suggested_indicators=["adx", "macd", "obv", "vwap"],
            )

        # 2. Ranging market
        bb_width = (bb_upper - bb_lower) / bb_middle
        if adx_value < 20 and bb_width < _BB_SQUEEZE_WIDTH_THRESHOLD:
            return MarketState(
                state="ranging",
                confidence=1.0 - adx_value / 20,
                description=f"震荡市场（ADX={adx_value:.1f}, 布林带宽度={bb_width:.2%}）",
                suggested_indicators=["stochastic", "cci", "williams_r", "rsi"],
            )

        # 3. Volatile market
        atr_pct = atr / current_price if current_price > 0 else 0
        if atr_pct > _ATR_HIGH_VOLATILITY_THRESHOLD:
            return MarketState(
                state="volatile",
                confidence=min(atr_pct / 0.05, 1.0),
                description=f"高波动（ATR={atr:.2f}, 占价格{atr_pct:.1%}）",
                suggested_indicators=["atr", "vwap", "obv"],
            )

        # 4. Neutral market
        return MarketState(
            state="neutral",
            confidence=0.5,
            description="中性市场",
            suggested_indicators=["rsi", "macd", "atr", "vwap"],
        )
