from __future__ import annotations

import statistics
from typing import Any, TypedDict


class VolumeAnalysis(TypedDict):
    """Volume analysis result with typed fields."""

    avg_volume: float
    volume_ratio: float
    trend: str


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
    def analyze_volume(volumes: list[float], lookback: int = 20) -> VolumeAnalysis:
        """Analyze volume relative to recent average."""
        if not volumes:
            return VolumeAnalysis(avg_volume=0.0, volume_ratio=0.0, trend="unknown")

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

        return VolumeAnalysis(avg_volume=avg_vol, volume_ratio=ratio, trend=trend)

    @staticmethod
    def calculate_obv(
        closes: list[float],
        volumes: list[float],
    ) -> dict[str, Any]:
        """Calculate On-Balance Volume and trend."""
        if not closes or not volumes or len(closes) != len(volumes):
            return {"obv_values": [], "obv_trend": "flat", "price_obv_divergence": "none"}

        obv_values: list[float] = [0.0]
        for i in range(1, len(closes)):
            if closes[i] > closes[i - 1]:
                obv_values.append(obv_values[-1] + volumes[i])
            elif closes[i] < closes[i - 1]:
                obv_values.append(obv_values[-1] - volumes[i])
            else:
                obv_values.append(obv_values[-1])

        lookback = min(5, len(obv_values))
        if lookback < 2:
            obv_trend = "flat"
        else:
            recent_obv = obv_values[-lookback:]
            slope = recent_obv[-1] - recent_obv[0]
            if slope > 0:
                obv_trend = "rising"
            elif slope < 0:
                obv_trend = "falling"
            else:
                obv_trend = "flat"

        price_obv_divergence = "none"
        if len(closes) >= 5:
            price_trend = "up" if closes[-1] > closes[-5] else "down" if closes[-1] < closes[-5] else "flat"
            if price_trend == "up" and obv_trend == "falling":
                price_obv_divergence = "bearish"
            elif price_trend == "down" and obv_trend == "rising":
                price_obv_divergence = "bullish"

        return {
            "obv_values": obv_values,
            "obv_trend": obv_trend,
            "price_obv_divergence": price_obv_divergence,
        }

    @classmethod
    def calculate_adx(
        cls,
        highs: list[float],
        lows: list[float],
        closes: list[float],
        period: int = 14,
    ) -> dict[str, Any]:
        """Calculate Average Directional Index."""
        if len(highs) < period + 1 or len(highs) != len(lows) or len(highs) != len(closes):
            return {"adx_value": 0.0, "trend_strength": "none", "di_plus": 0.0, "di_minus": 0.0}

        plus_dm: list[float] = []
        minus_dm: list[float] = []
        true_ranges: list[float] = []

        for i in range(1, len(highs)):
            high_diff = highs[i] - highs[i - 1]
            low_diff = lows[i - 1] - lows[i]

            plus_dm.append(high_diff if high_diff > low_diff and high_diff > 0 else 0.0)
            minus_dm.append(low_diff if low_diff > high_diff and low_diff > 0 else 0.0)

            true_ranges.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))

        def _smooth(values: list[float], p: int) -> list[float]:
            if len(values) < p:
                return []
            result = [sum(values[:p])]
            for i in range(p, len(values)):
                result.append(result[-1] - result[-1] / p + values[i])
            return result

        smoothed_plus_dm = _smooth(plus_dm, period)
        smoothed_minus_dm = _smooth(minus_dm, period)
        smoothed_tr = _smooth(true_ranges, period)

        if not smoothed_plus_dm or not smoothed_minus_dm or not smoothed_tr:
            return {"adx_value": 0.0, "trend_strength": "none", "di_plus": 0.0, "di_minus": 0.0}

        di_plus = [100.0 * pdm / tr if tr > 0 else 0.0
                   for pdm, tr in zip(smoothed_plus_dm, smoothed_tr)]
        di_minus = [100.0 * mdm / tr if tr > 0 else 0.0
                    for mdm, tr in zip(smoothed_minus_dm, smoothed_tr)]

        dx: list[float] = []
        for dp, dm in zip(di_plus, di_minus):
            total = dp + dm
            dx.append(100.0 * abs(dp - dm) / total if total > 0 else 0.0)

        if len(dx) < period:
            adx_value = sum(dx) / len(dx) if dx else 0.0
        else:
            adx_value = sum(dx[:period]) / period
            for i in range(period, len(dx)):
                adx_value = (adx_value * (period - 1) + dx[i]) / period

        if adx_value < 20:
            trend_strength = "none"
        elif adx_value < 25:
            trend_strength = "weak"
        elif adx_value < 40:
            trend_strength = "moderate"
        elif adx_value < 60:
            trend_strength = "strong"
        else:
            trend_strength = "extreme"

        return {
            "adx_value": adx_value,
            "trend_strength": trend_strength,
            "di_plus": di_plus[-1] if di_plus else 0.0,
            "di_minus": di_minus[-1] if di_minus else 0.0,
        }

    @staticmethod
    def calculate_stochastic(
        highs: list[float],
        lows: list[float],
        closes: list[float],
        k_period: int = 14,
        d_period: int = 3,
    ) -> dict[str, Any]:
        """Calculate Stochastic Oscillator (%K and %D)."""
        if len(highs) < k_period or len(highs) != len(lows) or len(highs) != len(closes):
            return {"stoch_k": 50.0, "stoch_d": 50.0, "signal": "neutral"}

        k_values: list[float] = []
        for i in range(k_period - 1, len(highs)):
            period_high = max(highs[i - k_period + 1:i + 1])
            period_low = min(lows[i - k_period + 1:i + 1])
            if period_high == period_low:
                k_values.append(50.0)
            else:
                k_values.append(100.0 * (closes[i] - period_low) / (period_high - period_low))

        stoch_k = k_values[-1] if k_values else 50.0
        if len(k_values) >= d_period:
            stoch_d = sum(k_values[-d_period:]) / d_period
        else:
            stoch_d = stoch_k

        if stoch_k > 80:
            signal = "overbought"
        elif stoch_k < 20:
            signal = "oversold"
        else:
            signal = "neutral"

        return {"stoch_k": stoch_k, "stoch_d": stoch_d, "signal": signal}

    @staticmethod
    def calculate_cci(
        highs: list[float],
        lows: list[float],
        closes: list[float],
        period: int = 20,
    ) -> dict[str, Any]:
        """Calculate Commodity Channel Index."""
        if len(highs) < period or len(highs) != len(lows) or len(highs) != len(closes):
            return {"cci_value": 0.0, "signal": "neutral"}

        typical_prices = [(h + l + c) / 3.0 for h, l, c in zip(highs, lows, closes)]
        recent_tp = typical_prices[-period:]
        sma_tp = sum(recent_tp) / period
        mean_deviation = sum(abs(tp - sma_tp) for tp in recent_tp) / period

        if mean_deviation == 0:
            cci_value = 0.0
        else:
            cci_value = (typical_prices[-1] - sma_tp) / (0.015 * mean_deviation)

        if cci_value > 100:
            signal = "overbought"
        elif cci_value < -100:
            signal = "oversold"
        else:
            signal = "neutral"

        return {"cci_value": cci_value, "signal": signal}

    @staticmethod
    def calculate_williams_r(
        highs: list[float],
        lows: list[float],
        closes: list[float],
        period: int = 14,
    ) -> dict[str, Any]:
        """Calculate Williams %R."""
        if len(highs) < period or len(highs) != len(lows) or len(highs) != len(closes):
            return {"williams_r": -50.0, "signal": "neutral"}

        period_high = max(highs[-period:])
        period_low = min(lows[-period:])

        if period_high == period_low:
            williams_r = -50.0
        else:
            williams_r = -100.0 * (period_high - closes[-1]) / (period_high - period_low)

        if williams_r > -20:
            signal = "overbought"
        elif williams_r < -80:
            signal = "oversold"
        else:
            signal = "neutral"

        return {"williams_r": williams_r, "signal": signal}

    @classmethod
    def analyze_multi_timeframe(
        cls,
        daily_closes: list[float],
        minute_closes: list[float],
    ) -> dict[str, Any]:
        """Analyze trend alignment across timeframes."""
        daily_trend = "neutral"
        minute_trend = "neutral"

        if len(daily_closes) >= 5:
            daily_sma5 = statistics.mean(daily_closes[-5:])
            daily_current = daily_closes[-1]
            if daily_current > daily_sma5 * 1.01:
                daily_trend = "up"
            elif daily_current < daily_sma5 * 0.99:
                daily_trend = "down"

        if len(minute_closes) >= 20:
            minute_sma20 = statistics.mean(minute_closes[-20:])
            minute_current = minute_closes[-1]
            if minute_current > minute_sma20 * 1.005:
                minute_trend = "up"
            elif minute_current < minute_sma20 * 0.995:
                minute_trend = "down"

        aligned = daily_trend == minute_trend and daily_trend != "neutral"

        return {
            "daily_trend": daily_trend,
            "minute_trend": minute_trend,
            "aligned": aligned,
            "description": f"日线趋势: {daily_trend}, 分钟趋势: {minute_trend}"
            + (", 趋势一致" if aligned else ""),
        }
