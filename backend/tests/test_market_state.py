from __future__ import annotations

from app.domain.analysis.market_state import MarketState, MarketStateDetector


class TestMarketStateDetector:
    """Tests for market state detection."""

    def test_trending_market(self) -> None:
        """Should detect trending market when ADX > 25 and DI spread > 10."""
        adx = {"adx_value": 32.0, "di_plus": 28.0, "di_minus": 12.0}
        result = MarketStateDetector.detect(
            adx=adx,
            bb_upper=105.0,
            bb_middle=100.0,
            bb_lower=95.0,
            atr=2.0,
            current_price=100.0,
            volume_analysis={"volume_ratio": 1.5},
        )
        assert result.state == "trending"
        assert result.confidence > 0.5
        assert "adx" in result.suggested_indicators

    def test_ranging_market(self) -> None:
        """Should detect ranging market when ADX < 20 and BB narrow."""
        adx = {"adx_value": 15.0, "di_plus": 18.0, "di_minus": 17.0}
        result = MarketStateDetector.detect(
            adx=adx,
            bb_upper=102.0,
            bb_middle=100.0,
            bb_lower=98.0,
            atr=1.0,
            current_price=100.0,
            volume_analysis={"volume_ratio": 0.8},
        )
        assert result.state == "ranging"
        assert "stochastic" in result.suggested_indicators

    def test_volatile_market(self) -> None:
        """Should detect volatile market when ATR/price > 3%."""
        adx = {"adx_value": 22.0, "di_plus": 20.0, "di_minus": 18.0}
        result = MarketStateDetector.detect(
            adx=adx,
            bb_upper=108.0,
            bb_middle=100.0,
            bb_lower=92.0,
            atr=4.0,
            current_price=100.0,
            volume_analysis={"volume_ratio": 2.0},
        )
        assert result.state == "volatile"
        assert "atr" in result.suggested_indicators

    def test_neutral_market(self) -> None:
        """Should detect neutral market as default."""
        adx = {"adx_value": 22.0, "di_plus": 20.0, "di_minus": 18.0}
        result = MarketStateDetector.detect(
            adx=adx,
            bb_upper=104.0,
            bb_middle=100.0,
            bb_lower=96.0,
            atr=1.5,
            current_price=100.0,
            volume_analysis={"volume_ratio": 1.0},
        )
        assert result.state == "neutral"

    def test_insufficient_data(self) -> None:
        """Should return neutral for insufficient data."""
        result = MarketStateDetector.detect(
            adx={},
            bb_upper=0.0,
            bb_middle=0.0,
            bb_lower=0.0,
            atr=0.0,
            current_price=0.0,
            volume_analysis={},
        )
        assert result.state == "neutral"
        assert result.confidence == 0.5
