from __future__ import annotations

import statistics


class TechnicalIndicators:
    """Compute RSI, MACD, and volume analysis from price/volume series."""

    @staticmethod
    def calculate_rsi(closes: list[float], period: int = 14) -> float:
        """Calculate RSI using the standard smoothed method."""
        if len(closes) < period + 1:
            return 0.0

        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [d if d > 0 else 0.0 for d in deltas]
        losses = [-d if d < 0 else 0.0 for d in deltas]

        avg_gain = statistics.mean(gains[:period])
        avg_loss = statistics.mean(losses[:period])

        for i in range(period, len(deltas)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _ema(values: list[float], period: int) -> list[float]:
        """Compute Exponential Moving Average."""
        if not values:
            return []
        multiplier = 2.0 / (period + 1)
        ema = [values[0]]
        for i in range(1, len(values)):
            ema.append(values[i] * multiplier + ema[-1] * (1 - multiplier))
        return ema

    @classmethod
    def calculate_macd(
        cls,
        closes: list[float],
        fast: int = 12,
        slow: int = 26,
        signal_period: int = 9,
    ) -> dict[str, float]:
        """Calculate MACD line, signal line, and histogram."""
        if len(closes) < slow + signal_period:
            return {"macd": 0.0, "signal": 0.0, "histogram": 0.0}

        ema_fast = cls._ema(closes, fast)
        ema_slow = cls._ema(closes, slow)
        macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
        signal_line = cls._ema(macd_line, signal_period)

        macd_val = macd_line[-1]
        signal_val = signal_line[-1]
        return {
            "macd": macd_val,
            "signal": signal_val,
            "histogram": macd_val - signal_val,
        }

    @staticmethod
    def analyze_volume(volumes: list[float], lookback: int = 20) -> dict[str, float | str]:
        """Analyze volume relative to recent average."""
        if not volumes:
            return {"avg_volume": 0.0, "volume_ratio": 0.0, "trend": "unknown"}

        recent = volumes[-lookback:] if len(volumes) >= lookback else volumes
        avg_vol = statistics.mean(recent[:-1]) if len(recent) > 1 else recent[0]
        current_vol = recent[-1]

        if avg_vol == 0:
            ratio = 0.0
        else:
            ratio = current_vol / avg_vol

        if ratio > 2.0:
            trend = "high"
        elif ratio < 0.5:
            trend = "low"
        else:
            trend = "normal"

        return {"avg_volume": avg_vol, "volume_ratio": ratio, "trend": trend}
