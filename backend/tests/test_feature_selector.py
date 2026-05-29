from __future__ import annotations

from app.domain.prompt.feature_selector import FeatureSelector


class TestFeatureSelector:
    """Tests for feature selector."""

    def test_parse_valid_json(self) -> None:
        response = '{"selected_indicators": ["adx", "macd", "obv"], "reasoning": "trend"}'
        result = FeatureSelector.parse_selection(response, [])
        assert result == ["adx", "macd", "obv"]

    def test_parse_invalid_json(self) -> None:
        response = "invalid json"
        result = FeatureSelector.parse_selection(response, ["rsi", "cci"])
        assert result == ["rsi", "cci"]

    def test_parse_empty_selection(self) -> None:
        response = '{"selected_indicators": [], "reasoning": "none"}'
        result = FeatureSelector.parse_selection(response, ["rsi"])
        assert result == ["rsi"]

    def test_parse_unknown_indicators(self) -> None:
        response = '{"selected_indicators": ["adx", "unknown", "macd"]}'
        result = FeatureSelector.parse_selection(response, [])
        assert result == ["adx", "macd"]

    def test_parse_fallback_to_default(self) -> None:
        response = "no json here"
        result = FeatureSelector.parse_selection(response, [])
        assert result == ["rsi", "macd", "atr", "vwap"]

    def test_filter_context_removes_unselected(self) -> None:
        context = {
            "rsi": 50.0,
            "macd": {"macd": 0.5},
            "atr": 2.0,
            "obv": {"obv_trend": "rising"},
            "adx": {"adx_value": 30.0},
            "current_price": 100.0,
        }
        filtered = FeatureSelector.filter_context(context, ["rsi", "macd"])
        assert "rsi" in filtered
        assert "macd" in filtered
        assert "atr" not in filtered
        assert "obv" not in filtered
        assert "adx" not in filtered
        assert "current_price" in filtered

    def test_filter_context_keeps_essential(self) -> None:
        context = {
            "symbol": "AAPL",
            "current_price": 100.0,
            "rsi": 50.0,
        }
        filtered = FeatureSelector.filter_context(context, [])
        assert "symbol" in filtered
        assert "current_price" in filtered

    def test_filter_removes_bb_without_atr_or_adx(self) -> None:
        context = {"bb_upper": 1.0, "bb_middle": 0.5, "bb_lower": 0.0, "rsi": 50.0}
        filtered = FeatureSelector.filter_context(context, ["rsi"])
        assert "bb_upper" not in filtered
        assert "bb_middle" not in filtered
        assert "bb_lower" not in filtered

    def test_filter_keeps_bb_with_atr(self) -> None:
        context = {"bb_upper": 1.0, "bb_middle": 0.5, "bb_lower": 0.0, "atr": 2.0}
        filtered = FeatureSelector.filter_context(context, ["atr"])
        assert "bb_upper" in filtered
        assert "bb_middle" in filtered
        assert "bb_lower" in filtered

    def test_filter_removes_volume_without_obv_or_vwap(self) -> None:
        context = {"volume_analysis": {"vol_trend": "up"}, "rsi": 50.0}
        filtered = FeatureSelector.filter_context(context, ["rsi"])
        assert "volume_analysis" not in filtered

    def test_filter_keeps_volume_with_obv(self) -> None:
        context = {"volume_analysis": {"vol_trend": "up"}, "obv": {"obv_trend": "rising"}}
        filtered = FeatureSelector.filter_context(context, ["obv"])
        assert "volume_analysis" in filtered

    def test_filter_removes_aggregate_when_less_than_5(self) -> None:
        context = {"aggregate_signals": {"trend": "up"}, "rsi": 50.0}
        filtered = FeatureSelector.filter_context(context, ["rsi", "macd", "atr", "vwap"])
        assert "aggregate_signals" not in filtered

    def test_filter_keeps_aggregate_when_5_or_more(self) -> None:
        context = {"aggregate_signals": {"trend": "up"}, "rsi": 50.0}
        filtered = FeatureSelector.filter_context(context, ["rsi", "macd", "atr", "vwap", "adx"])
        assert "aggregate_signals" in filtered
