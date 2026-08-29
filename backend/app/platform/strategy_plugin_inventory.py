"""Pure helpers for the offline strategy-plugin inventory screen.

Volume=0: five-field JSON rows carry no volume. The only volume consumer is
VolumeShareSlippageModel (fill_model.py:49-52), which is opt-in and returns
zero slippage when volume<=0. Bars are loaded with volume=0.

VolumeShareSlippageModel is prohibited in every cost scenario. On a
volume-less dataset it degrades to zero slippage and manufactures a fake
low-cost result. Scenarios keep fill_model=None.

First-passage is not_applicable: plugins exit on their own signal, not
stop/target brackets. Feeding 0/0 into assess_first_passage would force
INSUFFICIENT_DATA regardless of evidence. This clustered-t-only bar is
WEAKER than the live signal gate.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal, NamedTuple

from app.domain.strategy_v2.signal_edge import (
    DEFAULT_MIN_DISTINCT_DAYS,
    DEFAULT_MIN_RESOLVED_TRADES,
    ClusteredTTestResult,
    SignalEdgeVerdictLabel,
    VERDICT_FAIL,
    VERDICT_INSUFFICIENT_DATA,
    VERDICT_PASS,
    clustered_t_test,
)
from app.platform.bus import EventBus
from app.platform.events import BarEvent, Event, EventSource, FillEvent
from app.platform.paper_broker import PaperBroker, PaperBrokerConfig
from app.platform.registry import get_default_registry
from app.platform.round_trips import RoundTrip, pair_round_trips
from app.platform.runner import PlatformRunner
from app.platform.sdk import Strategy

PluginParams = dict[str, float | int]
FirstPassageApplicability = Literal["not_applicable"]

DEFAULT_PLUGIN_PARAMS: Final[dict[str, PluginParams]] = {
    "interval": {"buy_low": 209.65, "sell_high": 212.63, "quantity": 1},
    "mean_reversion": {
        "lookback": 20,
        "entry_z": -1.5,
        "exit_z": 0.0,
        "quantity": 1,
    },
    "momentum_breakout": {
        "channel_period": 20,
        "atr_period": 14,
        "atr_multiplier": 2.0,
        "quantity": 1,
    },
    "trend_following": {
        "fast_period": 10,
        "slow_period": 30,
        "atr_period": 14,
        "quantity": 1,
        "atr_threshold_pct": 0.0,
    },
}


class CostScenario(NamedTuple):
    name: str
    broker_config: PaperBrokerConfig


class PluginRunRequest(NamedTuple):
    plugin_name: str
    params: Mapping[str, float | int]
    bars: Sequence[BarEvent]
    broker_config: PaperBrokerConfig
    symbol: str


class PluginRunResult(NamedTuple):
    round_trips: tuple[RoundTrip, ...]
    gross_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal


class PluginEdgeAssessment(NamedTuple):
    verdict: SignalEdgeVerdictLabel
    first_passage_applicability: FirstPassageApplicability
    first_passage: None
    clustered: ClusteredTTestResult


class ScreenRequest(NamedTuple):
    bars: Sequence[BarEvent]
    symbol: str


class InventoryRow(NamedTuple):
    plugin_name: str
    cost_scenario: str
    round_trip_count: int
    distinct_days: int
    gross_pnl: Decimal
    net_pnl: Decimal
    fees: Decimal
    verdict: SignalEdgeVerdictLabel
    first_passage_applicability: FirstPassageApplicability
    first_passage: None


class ScreenResult(NamedTuple):
    rows: tuple[InventoryRow, ...]


def discover_plugin_names() -> tuple[str, ...]:
    return tuple(meta.name for meta in get_default_registry().list())


def cost_scenarios() -> tuple[CostScenario, ...]:
    default = PaperBrokerConfig()
    return (
        CostScenario(name="default", broker_config=default),
        CostScenario(
            name="commission_10x",
            broker_config=PaperBrokerConfig(
                commission_rate=default.commission_rate * 10,
            ),
        ),
    )


def load_minute_bars(path: Path, *, symbol: str) -> tuple[BarEvent, ...]:
    with path.open(encoding="utf-8") as handle:
        payload: list[list[str | float]] = json.load(handle)
    bars: list[BarEvent] = []
    for row in payload:
        timestamp_raw, open_, high, low, close = row
        bars.append(
            BarEvent(
                timestamp=datetime.fromisoformat(str(timestamp_raw)),
                source=EventSource.MARKET,
                symbol=symbol,
                open=Decimal(str(open_)),
                high=Decimal(str(high)),
                low=Decimal(str(low)),
                close=Decimal(str(close)),
                volume=0,
            )
        )
    return tuple(bars)


def _make_strategy(plugin_name: str, params: Mapping[str, float | int]) -> Strategy:
    strategy_cls = get_default_registry().get(plugin_name)
    constructed: Strategy = getattr(strategy_cls, "__call__")(params=dict(params))
    return constructed


def run_plugin_offline(request: PluginRunRequest) -> PluginRunResult:
    clock_state: dict[str, datetime] = {
        "now": datetime.fromisoformat("1970-01-01T00:00:00+00:00")
    }

    def clock() -> datetime:
        return clock_state["now"]

    fills: list[FillEvent] = []

    def collect(event: Event) -> None:
        if isinstance(event, FillEvent):
            fills.append(event)

    bus = EventBus()
    bus.subscribe("fill", collect)
    runner = PlatformRunner(
        symbols=[request.symbol],
        strategy=_make_strategy(request.plugin_name, request.params),
        mode="paper",
        bus=bus,
        clock=clock,
        broker=PaperBroker(clock=clock, config=request.broker_config),
    )
    for bar in request.bars:
        clock_state["now"] = bar.timestamp
        runner.on_bar(bar)

    trips, _open_lots = pair_round_trips(fills)
    gross = sum((trip.gross_pnl for trip in trips), Decimal("0"))
    fees = sum((trip.fees for trip in trips), Decimal("0"))
    net = sum((trip.net_pnl for trip in trips), Decimal("0"))
    return PluginRunResult(
        round_trips=tuple(trips),
        gross_pnl=gross,
        fees=fees,
        net_pnl=net,
    )


def assess_plugin_edge(trips: Sequence[RoundTrip]) -> PluginEdgeAssessment:
    clustered = clustered_t_test(
        tuple((trip.entry_at.date(), trip.net_return_pct) for trip in trips)
    )
    thin = (
        clustered.observations < DEFAULT_MIN_RESOLVED_TRADES
        or clustered.distinct_days < DEFAULT_MIN_DISTINCT_DAYS
    )
    if thin:
        verdict: SignalEdgeVerdictLabel = VERDICT_INSUFFICIENT_DATA
    elif clustered.significant:
        verdict = VERDICT_PASS
    else:
        verdict = VERDICT_FAIL
    return PluginEdgeAssessment(
        verdict=verdict,
        first_passage_applicability="not_applicable",
        first_passage=None,
        clustered=clustered,
    )


def screen_inventory(request: ScreenRequest) -> ScreenResult:
    rows: list[InventoryRow] = []
    for plugin_name in discover_plugin_names():
        params = DEFAULT_PLUGIN_PARAMS[plugin_name]
        for scenario in cost_scenarios():
            run = run_plugin_offline(
                PluginRunRequest(
                    plugin_name=plugin_name,
                    params=params,
                    bars=request.bars,
                    broker_config=scenario.broker_config,
                    symbol=request.symbol,
                )
            )
            assessment = assess_plugin_edge(run.round_trips)
            rows.append(
                InventoryRow(
                    plugin_name=plugin_name,
                    cost_scenario=scenario.name,
                    round_trip_count=len(run.round_trips),
                    distinct_days=assessment.clustered.distinct_days,
                    gross_pnl=run.gross_pnl,
                    net_pnl=run.net_pnl,
                    fees=run.fees,
                    verdict=assessment.verdict,
                    first_passage_applicability=assessment.first_passage_applicability,
                    first_passage=assessment.first_passage,
                )
            )
    return ScreenResult(rows=tuple(rows))
