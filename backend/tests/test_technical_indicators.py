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
