from __future__ import annotations

import ast
import importlib.util
import inspect
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import cache
from pathlib import Path
from types import ModuleType

import pytest

from app.domain.strategy_v2.signal_edge import (
    VERDICT_FAIL,
    VERDICT_INSUFFICIENT_DATA,
    VERDICT_PASS,
)
from app.platform.events import BarEvent, EventSource
from app.platform.paper_broker import PaperBrokerConfig
from app.platform.registry import get_default_registry
from app.platform.round_trips import RoundTrip

_EXPECTED_PLUGIN_NAMES = frozenset(
    {"interval", "mean_reversion", "momentum_breakout", "trend_following"}
)
_NVDA_MINUTE_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "research"
    / "nvda-full-minute-20260601-20260724.json"
)
_SCREEN_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "screen_strategy_plugin_inventory.py"
)
_BANNED_IMPORT_PREFIXES = (
    "app.core.backtest",
    "app.services.trade_execution_service",
    "app.core.broker",
)


@cache
def _screen() -> ModuleType:
    if not _SCREEN_PATH.is_file():
        raise FileNotFoundError(_SCREEN_PATH)
    spec = importlib.util.spec_from_file_location(
        "screen_strategy_plugin_inventory",
        _SCREEN_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ohlc_bar(ts: datetime, close: str, *, volume: int = 0) -> BarEvent:
    price = Decimal(close)
    return BarEvent(
        timestamp=ts,
        source=EventSource.MARKET,
        symbol="NVDA.US",
        open=price,
        high=price + Decimal("0.50"),
        low=price - Decimal("0.50"),
        close=price,
        volume=volume,
    )


def _interval_round_trip_bars() -> tuple[BarEvent, ...]:
    t0 = datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc)
    return (
        _ohlc_bar(t0, "200.00"),
        _ohlc_bar(t0 + timedelta(minutes=1), "215.00"),
    )


def _imported_module_names(module: ModuleType) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}")
    return names


class TestRegistryDiscovery:
    def test_discover_plugin_names_matches_default_registry(self) -> None:
        registry_names = {meta.name for meta in get_default_registry().list()}
        assert registry_names == _EXPECTED_PLUGIN_NAMES

        names = set(_screen().discover_plugin_names())
        assert names == registry_names

    def test_default_plugin_params_cover_the_four_schemas(self) -> None:
        params = _screen().DEFAULT_PLUGIN_PARAMS
        assert set(params) == _EXPECTED_PLUGIN_NAMES
        assert params["interval"] == {
            "buy_low": 209.65,
            "sell_high": 212.63,
            "quantity": 1,
        }
        assert params["mean_reversion"] == {
            "lookback": 20,
            "entry_z": -1.5,
            "exit_z": 0.0,
            "quantity": 1,
        }
        assert params["momentum_breakout"] == {
            "channel_period": 20,
            "atr_period": 14,
            "atr_multiplier": 2.0,
            "quantity": 1,
        }
        assert params["trend_following"] == {
            "fast_period": 10,
            "slow_period": 30,
            "atr_period": 14,
            "quantity": 1,
            "atr_threshold_pct": 0.0,
        }


class TestCostScenarioInjection:
    def test_tenx_commission_changes_net_pnl_for_the_same_bars(self) -> None:
        screen = _screen()
        scenarios = {item.name: item for item in screen.cost_scenarios()}
        assert set(scenarios) >= {"default", "commission_10x"}
        default = scenarios["default"].broker_config
        expensive = scenarios["commission_10x"].broker_config
        assert expensive.commission_rate == default.commission_rate * 10

        bars = _interval_round_trip_bars()
        interval_params = screen.DEFAULT_PLUGIN_PARAMS["interval"]
        cheap = screen.run_plugin_offline(
            screen.PluginRunRequest(
                plugin_name="interval",
                params=interval_params,
                bars=bars,
                broker_config=default,
                symbol="NVDA.US",
            )
        )
        costly = screen.run_plugin_offline(
            screen.PluginRunRequest(
                plugin_name="interval",
                params=interval_params,
                bars=bars,
                broker_config=expensive,
                symbol="NVDA.US",
            )
        )
        assert cheap.round_trips
        assert costly.round_trips
        assert cheap.net_pnl != costly.net_pnl


class TestGrossAndNetReporting:
    def test_gross_and_net_differ_when_fees_are_positive(self) -> None:
        screen = _screen()
        default = next(
            item.broker_config
            for item in screen.cost_scenarios()
            if item.name == "default"
        )
        assert default.commission_rate > 0
        result = screen.run_plugin_offline(
            screen.PluginRunRequest(
                plugin_name="interval",
                params=screen.DEFAULT_PLUGIN_PARAMS["interval"],
                bars=_interval_round_trip_bars(),
                broker_config=default,
                symbol="NVDA.US",
            )
        )
        assert result.round_trips
        assert result.fees > 0
        assert result.gross_pnl != result.net_pnl
        assert result.net_pnl == result.gross_pnl - result.fees
        trip_gross = sum((trip.gross_pnl for trip in result.round_trips), Decimal("0"))
        trip_net = sum((trip.net_pnl for trip in result.round_trips), Decimal("0"))
        assert result.gross_pnl == trip_gross
        assert result.net_pnl == trip_net


class TestVolumeZeroHandling:
    def test_five_field_rows_load_as_volume_zero_and_run_completes(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "bars.json"
        _ = path.write_text(
            json.dumps(
                [
                    ["2026-06-02T14:30:00+00:00", 200.0, 200.5, 199.5, 200.0],
                    ["2026-06-02T14:31:00+00:00", 215.0, 215.5, 214.5, 215.0],
                ]
            )
        )
        screen = _screen()
        bars = screen.load_minute_bars(path, symbol="NVDA.US")
        assert len(bars) == 2
        assert all(bar.volume == 0 for bar in bars)
        assert all(bar.symbol == "NVDA.US" for bar in bars)

        result = screen.run_plugin_offline(
            screen.PluginRunRequest(
                plugin_name="interval",
                params=screen.DEFAULT_PLUGIN_PARAMS["interval"],
                bars=bars,
                broker_config=PaperBrokerConfig(),
                symbol="NVDA.US",
            )
        )
        assert result.gross_pnl - result.net_pnl == result.fees

    def test_cost_scenarios_do_not_include_volume_share_slippage_model(self) -> None:
        # VolumeShareSlippageModel returns zero slippage when bar.volume <= 0, so
        # a volume-less dataset would silently become a fake zero-impact cost
        # scenario. Screen scenarios must leave fill_model=None and use the
        # PaperBrokerConfig tick/commission fields instead.
        for scenario in _screen().cost_scenarios():
            assert scenario.broker_config.fill_model is None


class TestThinEvidenceVerdict:
    def test_thin_inventory_screen_never_verdicts_fail(self) -> None:
        screen = _screen()
        result = screen.screen_inventory(
            screen.ScreenRequest(
                bars=_interval_round_trip_bars(),
                symbol="NVDA.US",
            )
        )
        assert result.rows
        assert all(row.verdict == VERDICT_INSUFFICIENT_DATA for row in result.rows)
        assert all(row.verdict != VERDICT_FAIL for row in result.rows)


class TestFirstPassageApplicability:
    def test_clustered_verdict_is_used_when_first_passage_does_not_apply(self) -> None:
        # Bracket-less plugins exit on their own signal, not stop/target hits.
        # Feeding 0/0 into assess_first_passage (or assess_signal_edge) fabricates
        # a first-passage result and, with resolved=0, would force
        # INSUFFICIENT_DATA even when the clustered t-test has enough evidence
        # to PASS or FAIL. The screen must mark first-passage not_applicable
        # and judge on clustered evidence only.
        trips = tuple(
            RoundTrip(
                symbol="NVDA.US",
                entry_at=datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc)
                + timedelta(days=i % 20),
                exit_at=datetime(2026, 6, 1, 14, 35, tzinfo=timezone.utc)
                + timedelta(days=i % 20),
                entry_price=Decimal("210"),
                exit_price=Decimal("211"),
                quantity=1,
                gross_pnl=Decimal("1"),
                fees=Decimal("0.10"),
                net_pnl=Decimal("0.90"),
                entry_notional=Decimal("210"),
                net_return_pct=1.0 + (i % 3) * 0.05,
            )
            for i in range(30)
        )
        assessment = _screen().assess_plugin_edge(trips)
        assert assessment.first_passage_applicability == "not_applicable"
        assert assessment.first_passage is None
        assert assessment.verdict == VERDICT_PASS
        assert assessment.verdict != VERDICT_INSUFFICIENT_DATA

    def test_inventory_rows_mark_first_passage_not_applicable(self) -> None:
        screen = _screen()
        result = screen.screen_inventory(
            screen.ScreenRequest(
                bars=_interval_round_trip_bars(),
                symbol="NVDA.US",
            )
        )
        assert result.rows
        assert all(
            row.first_passage_applicability == "not_applicable" for row in result.rows
        )
        assert all(row.first_passage is None for row in result.rows)


class TestImportBan:
    def test_screen_module_does_not_import_hashed_backtest_or_live_execution(
        self,
    ) -> None:
        imported = _imported_module_names(_screen())
        for prefix in _BANNED_IMPORT_PREFIXES:
            assert prefix not in imported
            assert not any(
                name == prefix or name.startswith(prefix + ".") for name in imported
            )


class TestDeterminism:
    def test_same_bars_and_config_produce_identical_pnl(self) -> None:
        screen = _screen()
        request = screen.PluginRunRequest(
            plugin_name="interval",
            params=screen.DEFAULT_PLUGIN_PARAMS["interval"],
            bars=_interval_round_trip_bars(),
            broker_config=PaperBrokerConfig(),
            symbol="NVDA.US",
        )
        first = screen.run_plugin_offline(request)
        second = screen.run_plugin_offline(request)
        assert first.gross_pnl == second.gross_pnl
        assert first.net_pnl == second.net_pnl
        assert first.fees == second.fees
        assert len(first.round_trips) == len(second.round_trips)
        assert tuple(
            (trip.entry_price, trip.exit_price, trip.quantity, trip.net_pnl)
            for trip in first.round_trips
        ) == tuple(
            (trip.entry_price, trip.exit_price, trip.quantity, trip.net_pnl)
            for trip in second.round_trips
        )


class TestRealDatasetLoader:
    def test_loader_handles_real_five_field_nvda_file(self) -> None:
        if not _NVDA_MINUTE_PATH.is_file():
            pytest.skip("gitignored NVDA minute dataset is absent in CI")
        bars = _screen().load_minute_bars(_NVDA_MINUTE_PATH, symbol="NVDA.US")
        assert len(bars) == 14820
        assert all(bar.volume == 0 for bar in bars)
        assert all(bar.symbol == "NVDA.US" for bar in bars)
