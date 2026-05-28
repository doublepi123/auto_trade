from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "AUTO_TRADE_DATABASE_URL",
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_tech_indicators.db",
)

import pytest
from app.domain.analysis.technical_indicators import TechnicalIndicators


class TestRSI:
    def test_rsi_with_uptrend(self) -> None:
        # Monotonically increasing prices → RSI should be high
        closes = [100.0 + i for i in range(20)]
        rsi = TechnicalIndicators.calculate_rsi(closes, period=14)
        assert rsi > 70  # overbought territory

    def test_rsi_with_downtrend(self) -> None:
        # Monotonically decreasing prices → RSI should be low
        closes = [200.0 - i for i in range(20)]
        rsi = TechnicalIndicators.calculate_rsi(closes, period=14)
        assert rsi < 30  # oversold territory

    def test_rsi_with_flat_prices(self) -> None:
        closes = [100.0] * 20
        rsi = TechnicalIndicators.calculate_rsi(closes, period=14)
        # Flat prices have no gains or losses; RSI defaults to 50
        assert rsi == 50.0

    def test_rsi_returns_zero_for_insufficient_data(self) -> None:
        closes = [100.0, 101.0]
        rsi = TechnicalIndicators.calculate_rsi(closes, period=14)
        assert rsi == 0.0


class TestMACD:
    def test_macd_returns_three_components(self) -> None:
        closes = [100.0 + i * 0.5 for i in range(50)]
        result = TechnicalIndicators.calculate_macd(closes)
        assert "macd" in result
        assert "signal" in result
        assert "histogram" in result

    def test_macd_histogram_is_difference(self) -> None:
        closes = [100.0 + i * 0.5 for i in range(50)]
        result = TechnicalIndicators.calculate_macd(closes)
        assert result["histogram"] == pytest.approx(result["macd"] - result["signal"], abs=0.01)

    def test_macd_returns_zeros_for_insufficient_data(self) -> None:
        closes = [100.0, 101.0]
        result = TechnicalIndicators.calculate_macd(closes)
        assert result["macd"] == 0.0
        assert result["signal"] == 0.0
        assert result["histogram"] == 0.0


class TestVolumeAnalysis:
    def test_volume_analysis_normal(self) -> None:
        volumes = [50000.0] * 20 + [55000.0]
        result = TechnicalIndicators.analyze_volume(volumes)
        assert result["avg_volume"] > 0
        assert result["volume_ratio"] > 0.9
        assert result["trend"] == "normal"

    def test_volume_analysis_high(self) -> None:
        volumes = [50000.0] * 20 + [150000.0]
        result = TechnicalIndicators.analyze_volume(volumes)
        assert result["volume_ratio"] > 2.0
        assert result["trend"] == "high"

    def test_volume_analysis_low(self) -> None:
        volumes = [50000.0] * 20 + [10000.0]
        result = TechnicalIndicators.analyze_volume(volumes)
        assert result["volume_ratio"] < 0.5
        assert result["trend"] == "low"

    def test_volume_analysis_returns_zeros_for_empty(self) -> None:
        result = TechnicalIndicators.analyze_volume([])
        assert result["avg_volume"] == 0.0
        assert result["volume_ratio"] == 0.0
        assert result["trend"] == "unknown"


class TestMultiTimeframe:
    def test_aligned_uptrend(self) -> None:
        daily = [100.0 + i for i in range(10)]
        minute = [105.0 + i * 0.1 for i in range(30)]
        result = TechnicalIndicators.analyze_multi_timeframe(daily, minute)
        assert result["aligned"] is True
        assert result["daily_trend"] == "up"

    def test_mixed_trends_not_aligned(self) -> None:
        daily = [100.0 + i for i in range(10)]  # up
        minute = [110.0 - i * 0.1 for i in range(30)]  # down
        result = TechnicalIndicators.analyze_multi_timeframe(daily, minute)
        assert result["aligned"] is False

    def test_short_data_returns_neutral(self) -> None:
        result = TechnicalIndicators.analyze_multi_timeframe([100.0], [100.0])
        assert result["daily_trend"] == "neutral"
        assert result["minute_trend"] == "neutral"


class TestOBV:
    """Tests for On-Balance Volume calculation."""

    def test_obv_basic_uptrend(self) -> None:
        """OBV should increase when price closes higher."""
        closes = [100.0, 101.0, 102.0, 103.0, 104.0]
        volumes = [1000.0, 1200.0, 1100.0, 1300.0, 1400.0]
        result = TechnicalIndicators.calculate_obv(closes, volumes)
        # Each close > previous, so OBV accumulates all volumes
        assert result["obv_values"] == [0.0, 1200.0, 2300.0, 3600.0, 5000.0]
        assert result["obv_trend"] == "rising"

    def test_obv_basic_downtrend(self) -> None:
        """OBV should decrease when price closes lower."""
        closes = [104.0, 103.0, 102.0, 101.0, 100.0]
        volumes = [1000.0, 1200.0, 1100.0, 1300.0, 1400.0]
        result = TechnicalIndicators.calculate_obv(closes, volumes)
        # Each close < previous, so OBV subtracts all volumes
        assert result["obv_values"] == [0.0, -1200.0, -2300.0, -3600.0, -5000.0]
        assert result["obv_trend"] == "falling"

    def test_obv_mixed_movement(self) -> None:
        """OBV should handle mixed price movements correctly."""
        closes = [100.0, 102.0, 101.0, 103.0, 102.0]
        volumes = [1000.0, 1200.0, 1100.0, 1300.0, 1400.0]
        result = TechnicalIndicators.calculate_obv(closes, volumes)
        # Day 1: +1200, Day 2: -1100, Day 3: +1300, Day 4: -1400
        assert result["obv_values"] == [0.0, 1200.0, 100.0, 1400.0, 0.0]
        assert result["obv_trend"] == "flat"

    def test_obv_empty_input(self) -> None:
        """OBV should return empty result for empty input."""
        result = TechnicalIndicators.calculate_obv([], [])
        assert result["obv_values"] == []
        assert result["obv_trend"] == "flat"
        assert result["price_obv_divergence"] == "none"

    def test_obv_insufficient_data(self) -> None:
        """OBV should handle single data point."""
        result = TechnicalIndicators.calculate_obv([100.0], [1000.0])
        assert result["obv_values"] == [0.0]
        assert result["obv_trend"] == "flat"

    def test_obv_price_obv_divergence_bearish(self) -> None:
        """Detect bearish divergence: price rising but OBV falling."""
        # Price: 100 -> 105 (up), but volumes decreasing on up days
        closes = [100.0, 102.0, 101.0, 103.0, 105.0]
        volumes = [5000.0, 1000.0, 4000.0, 1000.0, 500.0]
        result = TechnicalIndicators.calculate_obv(closes, volumes)
        # OBV: 0, +1000, -3000, -2000, -2500 (falling trend)
        # Price is up from 100 to 105, OBV is down from 0 to -2500
        assert result["price_obv_divergence"] == "bearish"


class TestADX:
    """Tests for Average Directional Index calculation."""

    def test_adx_strong_uptrend(self) -> None:
        """ADX should be high in a strong uptrend."""
        # Simulate strong uptrend: each bar makes new highs
        highs = [float(100 + i * 2) for i in range(20)]
        lows = [float(98 + i * 2) for i in range(20)]
        closes = [float(99 + i * 2) for i in range(20)]
        result = TechnicalIndicators.calculate_adx(highs, lows, closes)
        assert result["adx_value"] > 25  # Should show trend
        assert result["trend_strength"] in ("moderate", "strong", "extreme")

    def test_adx_ranging_market(self) -> None:
        """ADX should be low in a ranging market."""
        # Simulate ranging market: prices oscillate
        highs = [102.0, 101.0, 102.0, 101.0, 102.0, 101.0, 102.0, 101.0,
                 102.0, 101.0, 102.0, 101.0, 102.0, 101.0, 102.0, 101.0,
                 102.0, 101.0, 102.0, 101.0]
        lows = [98.0, 99.0, 98.0, 99.0, 98.0, 99.0, 98.0, 99.0,
                98.0, 99.0, 98.0, 99.0, 98.0, 99.0, 98.0, 99.0,
                98.0, 99.0, 98.0, 99.0]
        closes = [100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0,
                  100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0,
                  100.0, 100.0, 100.0, 100.0]
        result = TechnicalIndicators.calculate_adx(highs, lows, closes)
        assert result["adx_value"] < 25  # Should show no trend
        assert result["trend_strength"] in ("none", "weak")

    def test_adx_insufficient_data(self) -> None:
        """ADX should return default for insufficient data."""
        result = TechnicalIndicators.calculate_adx([100.0], [98.0], [99.0])
        assert result["adx_value"] == 0.0
        assert result["trend_strength"] == "none"
        assert result["di_plus"] == 0.0
        assert result["di_minus"] == 0.0

    def test_adx_empty_input(self) -> None:
        """ADX should return default for empty input."""
        result = TechnicalIndicators.calculate_adx([], [], [])
        assert result["adx_value"] == 0.0
        assert result["trend_strength"] == "none"


class TestStochastic:
    """Tests for Stochastic Oscillator calculation."""

    def test_stoch_overbought(self) -> None:
        """Stochastic should indicate overbought when near highs."""
        highs = [110.0, 112.0, 115.0, 118.0, 120.0, 122.0, 125.0, 128.0,
                 130.0, 132.0, 135.0, 138.0, 140.0, 142.0, 145.0]
        lows = [100.0, 102.0, 105.0, 108.0, 110.0, 112.0, 115.0, 118.0,
                120.0, 122.0, 125.0, 128.0, 130.0, 132.0, 135.0]
        closes = [105.0, 108.0, 112.0, 115.0, 118.0, 120.0, 123.0, 126.0,
                  128.0, 130.0, 133.0, 136.0, 138.0, 141.0, 144.0]
        result = TechnicalIndicators.calculate_stochastic(highs, lows, closes)
        assert result["stoch_k"] > 80
        assert result["signal"] == "overbought"

    def test_stoch_oversold(self) -> None:
        """Stochastic should indicate oversold when near lows."""
        highs = [100.0, 98.0, 96.0, 94.0, 92.0, 90.0, 88.0, 86.0,
                 84.0, 82.0, 80.0, 78.0, 76.0, 74.0, 72.0]
        lows = [90.0, 88.0, 86.0, 84.0, 82.0, 80.0, 78.0, 76.0,
                74.0, 72.0, 70.0, 68.0, 66.0, 64.0, 62.0]
        closes = [95.0, 92.0, 88.0, 85.0, 82.0, 80.0, 78.0, 76.0,
                  74.0, 72.0, 70.0, 68.0, 66.0, 64.0, 63.0]
        result = TechnicalIndicators.calculate_stochastic(highs, lows, closes)
        assert result["stoch_k"] < 20
        assert result["signal"] == "oversold"

    def test_stoch_neutral(self) -> None:
        highs = [float(100 + (i % 3) * 5) for i in range(20)]
        lows = [float(90 + (i % 3) * 5) for i in range(20)]
        closes = [float(95 + (i % 3) * 5) for i in range(20)]
        result = TechnicalIndicators.calculate_stochastic(highs, lows, closes)
        assert 20 <= result["stoch_k"] <= 80
        assert result["signal"] == "neutral"

    def test_stoch_insufficient_data(self) -> None:
        result = TechnicalIndicators.calculate_stochastic([100.0], [90.0], [95.0])
        assert result["stoch_k"] == 50.0
        assert result["stoch_d"] == 50.0
        assert result["signal"] == "neutral"

    def test_stoch_empty_input(self) -> None:
        result = TechnicalIndicators.calculate_stochastic([], [], [])
        assert result["stoch_k"] == 50.0
        assert result["stoch_d"] == 50.0
        assert result["signal"] == "neutral"


class TestCCI:
    """Tests for Commodity Channel Index calculation."""

    def test_cci_overbought(self) -> None:
        highs = [float(100 + i * 3) for i in range(25)]
        lows = [float(95 + i * 3) for i in range(25)]
        closes = [float(98 + i * 3) for i in range(25)]
        result = TechnicalIndicators.calculate_cci(highs, lows, closes)
        assert result["cci_value"] > 100
        assert result["signal"] == "overbought"

    def test_cci_oversold(self) -> None:
        highs = [float(200 - i * 3) for i in range(25)]
        lows = [float(195 - i * 3) for i in range(25)]
        closes = [float(198 - i * 3) for i in range(25)]
        result = TechnicalIndicators.calculate_cci(highs, lows, closes)
        assert result["cci_value"] < -100
        assert result["signal"] == "oversold"

    def test_cci_neutral(self) -> None:
        highs = [float(102 + (i % 4) * 2) for i in range(25)]
        lows = [float(98 + (i % 4) * 2) for i in range(25)]
        closes = [float(100 + (i % 4) * 2) for i in range(25)]
        result = TechnicalIndicators.calculate_cci(highs, lows, closes)
        assert -100 <= result["cci_value"] <= 100
        assert result["signal"] == "neutral"

    def test_cci_insufficient_data(self) -> None:
        result = TechnicalIndicators.calculate_cci([100.0], [95.0], [98.0])
        assert result["cci_value"] == 0.0
        assert result["signal"] == "neutral"

    def test_cci_empty_input(self) -> None:
        result = TechnicalIndicators.calculate_cci([], [], [])
        assert result["cci_value"] == 0.0
        assert result["signal"] == "neutral"


class TestWilliamsR:
    """Tests for Williams %R calculation."""

    def test_williams_overbought(self) -> None:
        highs = [100.0, 102.0, 105.0, 108.0, 110.0, 112.0, 115.0, 118.0,
                 120.0, 122.0, 125.0, 128.0, 130.0, 132.0, 135.0]
        lows = [90.0, 92.0, 95.0, 98.0, 100.0, 102.0, 105.0, 108.0,
                110.0, 112.0, 115.0, 118.0, 120.0, 122.0, 125.0]
        closes = [98.0, 100.0, 103.0, 106.0, 108.0, 110.0, 113.0, 116.0,
                  118.0, 120.0, 123.0, 126.0, 128.0, 131.0, 134.0]
        result = TechnicalIndicators.calculate_williams_r(highs, lows, closes)
        assert result["williams_r"] > -20
        assert result["signal"] == "overbought"

    def test_williams_oversold(self) -> None:
        highs = [100.0, 98.0, 96.0, 94.0, 92.0, 90.0, 88.0, 86.0,
                 84.0, 82.0, 80.0, 78.0, 76.0, 74.0, 72.0]
        lows = [90.0, 88.0, 86.0, 84.0, 82.0, 80.0, 78.0, 76.0,
                74.0, 72.0, 70.0, 68.0, 66.0, 64.0, 62.0]
        closes = [92.0, 90.0, 88.0, 86.0, 84.0, 82.0, 80.0, 78.0,
                  76.0, 74.0, 72.0, 70.0, 68.0, 66.0, 64.0]
        result = TechnicalIndicators.calculate_williams_r(highs, lows, closes)
        assert result["williams_r"] < -80
        assert result["signal"] == "oversold"

    def test_williams_neutral(self) -> None:
        highs = [float(100 + (i % 3) * 5) for i in range(20)]
        lows = [float(90 + (i % 3) * 5) for i in range(20)]
        closes = [float(95 + (i % 3) * 5) for i in range(20)]
        result = TechnicalIndicators.calculate_williams_r(highs, lows, closes)
        assert -80 <= result["williams_r"] <= -20
        assert result["signal"] == "neutral"

    def test_williams_insufficient_data(self) -> None:
        result = TechnicalIndicators.calculate_williams_r([100.0], [90.0], [95.0])
        assert result["williams_r"] == -50.0
        assert result["signal"] == "neutral"

    def test_williams_empty_input(self) -> None:
        result = TechnicalIndicators.calculate_williams_r([], [], [])
        assert result["williams_r"] == -50.0
        assert result["signal"] == "neutral"
