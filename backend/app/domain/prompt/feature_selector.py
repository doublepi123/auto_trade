from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("auto_trade.feature_selector")

AVAILABLE_INDICATORS = {
    "rsi": "RSI",
    "macd": "MACD",
    "atr": "ATR",
    "obv": "OBV",
    "adx": "ADX",
    "stochastic": "Stochastic",
    "cci": "CCI",
    "williams_r": "Williams %R",
    "vwap": "VWAP",
}

DEFAULT_INDICATORS = ["rsi", "macd", "atr", "vwap"]


class FeatureSelector:
    """Parse LLM indicator selection and filter context."""

    @staticmethod
    def parse_selection(llm_response: str, suggested: list[str]) -> list[str]:
        """Parse LLM response to extract selected indicators."""
        try:
            start = llm_response.find("{")
            end = llm_response.rfind("}") + 1
            if start >= 0 and end > start:
                json_str = llm_response[start:end]
                data = json.loads(json_str)
                selected = data.get("selected_indicators", [])
                if isinstance(selected, list):
                    valid = [s for s in selected if s in AVAILABLE_INDICATORS]
                    if valid:
                        return valid
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.warning("Failed to parse LLM indicator selection")

        if suggested:
            return [s for s in suggested if s in AVAILABLE_INDICATORS]
        return DEFAULT_INDICATORS

    @staticmethod
    def filter_context(
        context: dict[str, Any],
        selected_indicators: list[str],
    ) -> dict[str, Any]:
        """Filter context to only include selected indicators."""
        filtered = dict(context)

        indicator_fields = {
            "rsi": ["rsi"],
            "macd": ["macd"],
            "atr": ["atr"],
            "obv": ["obv"],
            "adx": ["adx"],
            "stochastic": ["stochastic"],
            "cci": ["cci"],
            "williams_r": ["williams_r"],
            "vwap": ["vwap"],
        }

        for indicator, fields in indicator_fields.items():
            if indicator not in selected_indicators:
                for field in fields:
                    filtered.pop(field, None)

        if len(selected_indicators) < 5:
            filtered.pop("aggregate_signals", None)

        if "atr" not in selected_indicators and "adx" not in selected_indicators:
            filtered.pop("bb_upper", None)
            filtered.pop("bb_middle", None)
            filtered.pop("bb_lower", None)

        if "obv" not in selected_indicators and "vwap" not in selected_indicators:
            filtered.pop("volume_analysis", None)

        return filtered
