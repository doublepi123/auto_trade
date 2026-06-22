from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable

from app.platform.events import EventSource, RiskEvent
from app.platform.portfolio_config import PortfolioConfig

if TYPE_CHECKING:
    from collections.abc import Callable as AbcCallable

__all__ = ["PortfolioRiskController"]


def _pearson(xs: list[Decimal], ys: list[Decimal]) -> Decimal:
    n = min(len(xs), len(ys))
    if n < 2:
        return Decimal("0")
    xs = xs[:n]
    ys = ys[:n]
    mx = sum(xs) / Decimal(n)
    my = sum(ys) / Decimal(n)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den_x = sum((x - mx) ** 2 for x in xs)
    den_y = sum((y - my) ** 2 for y in ys)
    if den_x == 0 or den_y == 0:
        return Decimal("0")
    return num / ((den_x * den_y).sqrt())


class PortfolioRiskController:
    """组合级风控：gross/net 敞口、集中度、相关性、回撤。可注入 clock 以便确定性测试。"""

    DRAWDOWN_THRESHOLD = Decimal("0.1")

    def __init__(
        self,
        config: PortfolioConfig,
        clock: Callable[[], datetime] | None = None,
        max_concentration: Decimal = Decimal("0.4"),
        max_avg_correlation: Decimal = Decimal("0.7"),
    ) -> None:
        self.config = config
        self._peak_nav: Decimal | None = None
        self._clock: AbcCallable[[], datetime] = clock or (lambda: datetime.now(timezone.utc))
        self.max_concentration = max_concentration
        self.max_avg_correlation = max_avg_correlation

    def _resolve_ts(self, timestamp: datetime | None) -> datetime:
        return timestamp or self._clock()

    def check(
        self,
        prices: dict[str, Decimal],
        positions: dict[str, dict[str, Any]],
        nav: Decimal,
        timestamp: datetime | None = None,
        returns_history: dict[str, list[Decimal]] | None = None,
    ) -> list[RiskEvent]:
        ts = self._resolve_ts(timestamp)
        if self._peak_nav is None or nav > self._peak_nav:
            self._peak_nav = nav

        events: list[RiskEvent] = []
        if nav <= 0:
            return events

        gross = Decimal("0")
        net = Decimal("0")
        held_symbols: list[str] = []
        for symbol, pos in positions.items():
            qty = int(pos.get("quantity", 0))
            if qty == 0:
                continue
            held_symbols.append(symbol)
            price = prices.get(symbol, Decimal("0"))
            exposure = Decimal(qty) * price
            gross += abs(exposure)
            net += exposure
            weight = abs(exposure) / nav
            if weight > self.max_concentration:
                events.append(
                    RiskEvent(
                        timestamp=ts,
                        source=EventSource.RISK,
                        risk_type="CONCENTRATION_BREACH",
                        severity="WARNING",
                        message=f"{symbol} weight {float(weight):.2%} > {float(self.max_concentration):.2%}",
                    )
                )

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

        events.extend(self._correlation_events(held_symbols, returns_history, ts))
        return events

    def _correlation_events(
        self,
        held_symbols: list[str],
        returns_history: dict[str, list[Decimal]] | None,
        ts: datetime,
    ) -> list[RiskEvent]:
        if returns_history is None or len(held_symbols) < 2:
            return []
        series = {s: returns_history[s] for s in held_symbols if s in returns_history and len(returns_history[s]) >= 2}
        symbols = list(series.keys())
        if len(symbols) < 2:
            return []
        corrs: list[Decimal] = []
        for i in range(len(symbols)):
            for j in range(i + 1, len(symbols)):
                corrs.append(abs(_pearson(series[symbols[i]], series[symbols[j]])))
        if not corrs:
            return []
        avg = sum(corrs) / Decimal(len(corrs))
        if avg > self.max_avg_correlation:
            return [
                RiskEvent(
                    timestamp=ts,
                    source=EventSource.RISK,
                    risk_type="CORRELATION_BREACH",
                    severity="WARNING",
                    message=f"avg |correlation| {float(avg):.2%} > {float(self.max_avg_correlation):.2%}",
                )
            ]
        return []

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
