from __future__ import annotations

from typing import Any

from app.domain.prompt.base import PromptModule


class SentimentModule(PromptModule):
    """Renders market sentiment data in the prompt."""

    def render(self, context: dict[str, Any]) -> str:
        sentiment = context.get("sentiment")
        if not sentiment:
            return "## 市场情绪\n无"

        description = sentiment.get("description", "无")
        score = sentiment.get("score", 0.0)
        label = sentiment.get("sentiment", "neutral")

        return f"""## 市场情绪
- 情绪倾向: {label}
- 情绪得分: {score:.2f}
- 描述: {description}"""
