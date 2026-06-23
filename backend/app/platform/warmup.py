from __future__ import annotations

from app.platform.events import BarEvent
from app.platform.runner import PlatformRunner

__all__ = ["WarmupProvider"]


class WarmupProvider:
    """历史播种提供者（参考 Lean SetWarmup）：持有历史 bar，按需喂给 runner 预热。"""

    def __init__(self, bars: list[BarEvent]) -> None:
        self.bars = list(bars)

    def feed(self, runner: PlatformRunner) -> None:
        runner.warmup(self.bars)
