from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class PromptModule(ABC):
    """Abstract base class for prompt modules."""

    @abstractmethod
    def render(self, context: dict[str, Any]) -> str:
        """Render this module's section of the prompt."""
        ...
