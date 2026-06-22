from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable

from app.platform.events import EventSource, RiskEvent
from app.platform.portfolio_config import PortfolioConfig

if TYPE_CHECKING:
    from collections.abc import Callable as AbcCallable

__all__ = ["PortfolioRiskController"]


class PortfolioRiskController:
    """组合级风控：gross/net 敞口、回撤检测。可注入 clock 以便确定性测试。"""

    DRAWDOWN_THRESHOLD = Decimal("0.1")

    def __init__(
        self,
        config: PortfolioConfig,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self._peak_nav: Decimal | None = None
        self._clock: AbcCallable[[], datetime] = clock or (lambda: datetime.now(timezone.utc))

    def _resolve_ts(self, timestamp: datetime | None) -> datetime:
        return timestamp or self._clock()

    def check(
        self,
        prices: dict[str, Decimal],
        positions: dict[str, dict[str, Any]],
        nav: Decimal,
        timestamp: datetime | None = None,
    ) -> list[RiskEvent]:
        ts = self._resolve_ts(timestamp)
        if self._peak_nav is None or nav > self._peak_nav:
            self._peak_nav = nav

        events: list[RiskEvent] = []
        if nav <= 0:
            return events

        gross = Decimal("0")
        net = Decimal("0")
        for symbol, pos in positions.items():
            qty = int(pos.get("quantity", 0))
            price = prices.get(symbol, Decimal("0"))
            exposure = Decimal(qty) * price
            gross += abs(exposure)
            net += exposure

        gross_ratio = gross / nav
        net_ratio = abs(net) / nav

        if gross_ratio > self.config.max_gross_exposure:
            events.append(
                RiskEvent(
                    timestamp=ts,
                    source=EventSource.RISK,
                    risk_type="MAX_GROSS_EXPOSURE_BREACH",
                    severity="CRITICAL",
                    message=f"gross exposure {float(gross_ratio):.2%} > {float(self.config.max_gross_exposure):.2%}",
                )
            )
        if net_ratio > self.config.max_net_exposure:
            events.append(
                RiskEvent(
                    timestamp=ts,
                    source=EventSource.RISK,
                    risk_type="MAX_NET_EXPOSURE_BREACH",
                    severity="CRITICAL",
                    message=f"net exposure {float(net_ratio):.2%} > {float(self.config.max_net_exposure):.2%}",
                )
            )
        return events

    def drawdown(self, nav: Decimal, timestamp: datetime | None = None) -> list[RiskEvent]:
        ts = self._resolve_ts(timestamp)
        if self._peak_nav is None or self._peak_nav <= 0 or nav >= self._peak_nav:
            return []
        dd = (self._peak_nav - nav) / self._peak_nav
        if dd <= self.DRAWDOWN_THRESHOLD:
            return []
        return [
            RiskEvent(
                timestamp=ts,
                source=EventSource.RISK,
                risk_type="DRAWDOWN_BREACH",
                severity="WARNING",
                message=f"drawdown {float(dd):.2%}",
            )
        ]
