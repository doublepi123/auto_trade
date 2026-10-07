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

    def test_parse_empty_selection_falls_back_to_suggested(self) -> None:
        response = '{"selected_indicators": [], "reasoning": "none"}'
        result = FeatureSelector.parse_selection(response, ["rsi"])
        assert result == ["rsi"]

    def test_parse_unknown_indicators(self) -> None:
        response = '{"selected_indicators": ["adx", "unknown", "macd"]}'
        result = FeatureSelector.parse_selection(response, [])
        assert result == ["adx", "macd"]

    def test_parse_uses_final_json_after_reasoning_block(self) -> None:
        response = (
            '<think>{"selected_indicators": ["rsi"], "reasoning": "draft"}</think>\n'
            '{"selected_indicators": ["adx", "macd"], "reasoning": "final"}'
        )
        result = FeatureSelector.parse_selection(response, [])
        assert result == ["adx", "macd"]

    def test_parse_fallback_to_default(self) -> None:
        response = "no json here"
        result = FeatureSelector.parse_selection(response, [])
        assert result == ["rsi", "macd", "atr", "vwap"]
