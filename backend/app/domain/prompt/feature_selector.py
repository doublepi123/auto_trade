from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("auto_trade.feature_selector")

MIN_INDICATORS_FOR_AGGREGATE = 5

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
                selected = data.get("selected_indicators")
                if isinstance(selected, list) and all(isinstance(x, str) for x in selected):
                    valid = [s for s in selected if s in AVAILABLE_INDICATORS]
                    if valid:
                        return valid
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning(
                "Failed to parse LLM indicator selection: %s  response=%.200r",
                exc,
                llm_response,
            )

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

        if len(selected_indicators) < MIN_INDICATORS_FOR_AGGREGATE:
            filtered.pop("aggregate_signals", None)

        # Bollinger Bands are always retained as fundamental reference data;
        # they are not gated behind any specific indicator selection.

        if "obv" not in selected_indicators and "vwap" not in selected_indicators:
            filtered.pop("volume_analysis", None)

        return filtered
