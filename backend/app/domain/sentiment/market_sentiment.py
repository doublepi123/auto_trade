from __future__ import annotations

import statistics
from typing import TypedDict


class SentimentResult(TypedDict):
    """Sentiment analysis result with typed fields."""

    sentiment: str
    score: float
    description: str


class MarketSentimentAnalyzer:
    """Derives market sentiment from price action data."""

    def analyze_from_price_changes(self, price_changes: list[float]) -> SentimentResult:
        """Analyze sentiment from a series of price changes.

        Args:
            price_changes: List of price deltas (positive = up, negative = down).

        Returns:
            Dict with 'sentiment' (bullish/bearish/neutral), 'score' (-1 to 1), 'description'.
        """
        if not price_changes:
            return SentimentResult(sentiment="neutral", score=0.0, description="无价格数据")

        avg_change = statistics.mean(price_changes)
        positive_count = sum(1 for c in price_changes if c > 0)
        negative_count = sum(1 for c in price_changes if c < 0)
        total = len(price_changes)

        # Score: normalized average + direction bias
        # Use mean of absolute changes for more robust normalization than max,
        # which can produce misleading scores when all changes are identical.
        mean_abs = statistics.mean(abs(c) for c in price_changes)
        if mean_abs == 0:
            normalized = 0.0
        else:
            normalized = avg_change / mean_abs

        direction_bias = (positive_count - negative_count) / total
        score = (normalized * 0.6 + direction_bias * 0.4)
        score = max(-1.0, min(1.0, score))

        if score > 0.2:
            sentiment = "bullish"
            description = f"市场情绪偏多（得分 {score:.2f}）"
        elif score < -0.2:
            sentiment = "bearish"
            description = f"市场情绪偏空（得分 {score:.2f}）"
        else:
            sentiment = "neutral"
            description = f"市场情绪中性（得分 {score:.2f}）"

        return SentimentResult(sentiment=sentiment, score=score, description=description)
