from __future__ import annotations

from typing import Any

from app.domain.prompt.base import PromptModule


class PromptBuilder:
    """Orchestrates modular prompt construction."""

    def __init__(self) -> None:
        self._modules: list[PromptModule] = []

    def add_module(self, module: PromptModule) -> PromptBuilder:
        self._modules.append(module)
        return self

    def build(self, context: dict[str, Any]) -> str:
        parts: list[str] = []
        for module in self._modules:
            rendered = module.render(context)
            if rendered.strip():
                parts.append(rendered)
        return "\n\n".join(parts)
