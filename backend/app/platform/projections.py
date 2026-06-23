from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from app.platform.events import BarEvent, Event, FillEvent
from app.platform.portfolio import Portfolio

__all__ = ["Projection", "NavProjection", "DailyReturnsProjection", "ProjectionEngine"]


@runtime_checkable
class Projection(Protocol):
    """读模型投影（参考事件溯源 CQRS / Lean Consolidator）：消费事件构建派生视图，可快照/恢复。"""

    @property
    def name(self) -> str: ...

    def apply(self, event: Event) -> None: ...

    def state(self) -> dict[str, Any]: ...


@dataclass
class NavProjection:
    """滚动 NAV 读模型：从 fill + bar 聚合出每根 bar 的 NAV 时序。"""

    initial_cash: Decimal = field(default_factory=lambda: Decimal("0"))
    _name: str = "nav"
    _portfolio: Portfolio = field(init=False)
    _last_close: dict[str, Decimal] = field(default_factory=dict)
    _series: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._portfolio = Portfolio(initial_cash=self.initial_cash)

    @property
    def name(self) -> str:
        return self._name

    @property
    def portfolio(self) -> Portfolio:
        return self._portfolio

    def apply(self, event: Event) -> None:
        if isinstance(event, FillEvent):
            self._portfolio.on_fill(event)
        elif isinstance(event, BarEvent):
            symbol = event.symbol or ""
            self._last_close[symbol] = event.close
            nav = self._portfolio.nav(self._last_close)
            self._series.append({"timestamp": event.timestamp.isoformat(), "nav": float(nav)})

    def state(self) -> dict[str, Any]:
        return {
            "nav_series": list(self._series),
            "cash": float(self._portfolio.cash),
            "realized_pnl": float(self._portfolio.realized_pnl),
            "positions": self._portfolio.quantities(),
        }

    def snapshot(self) -> dict[str, Any]:
        return self.state()


@dataclass
class DailyReturnsProjection:
    """每日收益读模型：按日历日聚合 EOD NAV，日收益 = 当日末/前日末 - 1。"""

    initial_cash: Decimal = field(default_factory=lambda: Decimal("0"))
    _name: str = "daily_returns"
    _portfolio: Portfolio = field(init=False)
    _last_close: dict[str, Decimal] = field(default_factory=dict)
    _eod_nav: dict[Any, float] = field(default_factory=dict)  # date -> nav
    _ordered_dates: list[Any] = field(default_factory=list)
    _returns: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._portfolio = Portfolio(initial_cash=self.initial_cash)

    @property
    def name(self) -> str:
        return self._name

    def apply(self, event: Event) -> None:
        if isinstance(event, FillEvent):
            self._portfolio.on_fill(event)
        elif isinstance(event, BarEvent):
            symbol = event.symbol or ""
            self._last_close[symbol] = event.close
            nav = float(self._portfolio.nav(self._last_close))
            day = event.timestamp.date()
            prev = self._eod_nav.get(day)
            if prev is None:
                self._ordered_dates.append(day)
            self._eod_nav[day] = nav

    def _recompute_returns(self) -> None:
        self._returns = []
        for i in range(1, len(self._ordered_dates)):
            d_prev = self._ordered_dates[i - 1]
            d_cur = self._ordered_dates[i]
            prev = self._eod_nav[d_prev]
            cur = self._eod_nav[d_cur]
            ret = (cur / prev - 1.0) if prev != 0 else 0.0
            self._returns.append({"date": d_cur.isoformat(), "return": ret})

    def state(self) -> dict[str, Any]:
        self._recompute_returns()
        return {
            "daily_returns": list(self._returns),
            "eod_nav": {d.isoformat(): v for d, v in self._eod_nav.items()},
        }

    def snapshot(self) -> dict[str, Any]:
        return self.state()


@dataclass
class ProjectionEngine:
    """订阅 bus，按事件类型分发给注册的 Projection，构建读模型（参考事件溯源投影引擎）。"""

    _projections: list[Projection] = field(default_factory=list)

    def register(self, projection: Projection) -> None:
        self._projections.append(projection)

    def apply(self, event: Event) -> None:
        for proj in self._projections:
            proj.apply(event)

    def on_event(self, event: Event) -> None:
        self.apply(event)

    def subscribe(self, bus: Any, event_types: tuple[str, ...] = ("fill", "bar")) -> None:
        for et in event_types:
            bus.subscribe(et, self.on_event)

    def state(self) -> dict[str, Any]:
        return {proj.name: proj.state() for proj in self._projections}

    def snapshot(self) -> dict[str, Any]:
        return self.state()
