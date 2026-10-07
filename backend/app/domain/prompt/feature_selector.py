from __future__ import annotations

import json
import logging
import re

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
_REASONING_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


class FeatureSelector:
    """Parse LLM indicator selection."""

    @staticmethod
    def parse_selection(llm_response: str, suggested: list[str]) -> list[str]:
        """Parse LLM response to extract selected indicators."""
        parse_error: Exception | None = None
        cleaned_response = _REASONING_BLOCK_RE.sub("", llm_response)
        for json_str in reversed(_json_object_candidates(cleaned_response)):
            try:
                data = json.loads(json_str)
                selected = data.get("selected_indicators")
                if isinstance(selected, list) and all(isinstance(x, str) for x in selected):
                    valid = [s for s in selected if s in AVAILABLE_INDICATORS]
                    if valid:
                        return valid
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                parse_error = exc
        if parse_error is not None:
            logger.warning(
                "Failed to parse LLM indicator selection: %s  response=%.200r",
                parse_error,
                llm_response,
            )

        if suggested:
            return [s for s in suggested if s in AVAILABLE_INDICATORS]
        return DEFAULT_INDICATORS


def _json_object_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    start: int | None = None
    depth = 0
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(text[start:index + 1])
                start = None

    return candidates
