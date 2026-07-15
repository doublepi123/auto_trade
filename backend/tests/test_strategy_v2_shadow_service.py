from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.broker import BrokerCandle
from app.domain.strategy_v2 import (
    StrategyBar,
    StrategyV2Action,
    StrategyV2Decision,
    StrategyV2EngineSnapshot,
    StrategyV2State,
    VirtualPosition,
)
from app.models import (
    Base,
    StrategyConfig,
    StrategyV2ShadowConfig,
    StrategyV2ShadowDecision,
    StrategyV2ShadowState,
    StrategyV2ShadowTrade,
    StrategyV2ShadowVersion,
)
from app.schemas import StrategyV2ShadowConfigUpdate
from app.services.strategy_v2_shadow_service import StrategyV2ShadowService


_SESSION_OPEN = datetime(2026, 7, 10, 13, 30, tzinfo=timezone.utc)


class _FakeCandles:
    def __init__(self, candles: list[BrokerCandle]) -> None:
        self.candles = candles
        self.calls: list[tuple[str, str, int]] = []

    def get_candlesticks(self, symbol: str, period: str, count: int) -> list[BrokerCandle]:
        self.calls.append((symbol, period, count))
        return list(self.candles)


class _PagedFakeCandles(_FakeCandles):
    def __init__(
        self,
        candles: list[BrokerCandle],
        historical: list[BrokerCandle],
    ) -> None:
        super().__init__(candles)
        self.historical = historical
        self.history_calls: list[tuple[str, str, int, datetime]] = []

    def get_history_candlesticks_by_offset(
        self,
        symbol: str,
        period: str,
        count: int,
        after: datetime,
    ) -> list[BrokerCandle]:
        self.history_calls.append((symbol, period, count, after))
        return list(self.historical)


def _candles(count: int = 180) -> list[BrokerCandle]:
    result: list[BrokerCandle] = []
    for index in range(count):
        close = 100 + math.sin(index / 6) * 0.35 + index * 0.0005
        result.append(
            BrokerCandle(
                timestamp=_SESSION_OPEN + timedelta(minutes=index),
                open=close - 0.01,
                high=close + 0.08,
                low=close - 0.08,
                close=close,
                volume=1000 + index,
            )
        )
    return result


class TestStrategyV2ShadowService:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=cls.engine)

    def setup_method(self) -> None:
        with Session(bind=self.engine) as db:
            for model in (
                StrategyV2ShadowDecision,
                StrategyV2ShadowTrade,
                StrategyV2ShadowState,
                StrategyV2ShadowVersion,
                StrategyV2ShadowConfig,
                StrategyConfig,
            ):
                db.query(model).delete()
            db.add(
                StrategyConfig(
                    symbol="AAPL.US",
                    market="US",
                    fee_rate_us=0.0005,
                    fee_rate_hk=0.003,
                )
            )
            db.commit()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _enabled_config(
        self,
        db: Session,
        *,
        activated_at: datetime,
    ) -> StrategyV2ShadowConfig:
        config = StrategyV2ShadowConfig(
            symbol="AAPL.US",
            enabled=True,
            updated_at=activated_at,
        )
        db.add(config)
        db.commit()
        return config

    def test_default_config_is_disabled_and_hard_shadow_only(self) -> None:
        with self._db() as db:
            config = StrategyV2ShadowService(db).get_config()

        assert config.enabled is False
        assert config.mode == "SHADOW"
        assert config.order_submission_allowed is False
        assert config.allow_position_addons is False
        assert config.short_entries_enabled is False
        assert config.max_holding_minutes == 60
        assert config.entry_cutoff_minutes_before_close == 45
        assert config.flatten_minutes_before_close == 15

    def test_daily_evidence_requires_a_complete_contiguous_rth_session(self) -> None:
        rows = [
            StrategyV2ShadowDecision(
                idempotency_key=f"complete-{index}",
                symbol="AAPL.US",
                market="US",
                config_version="complete-version",
                session_date=_SESSION_OPEN.date(),
                bar_at=_SESSION_OPEN + timedelta(minutes=index),
                close_price=100.0,
                gate_passed=index == 120,
            )
            for index in range(390)
        ]

        complete = StrategyV2ShadowService._daily_evidence(rows, [])[0]
        with_gap = StrategyV2ShadowService._daily_evidence(
            rows[:200] + rows[201:],
            [],
        )[0]
        outside = StrategyV2ShadowDecision(
            idempotency_key="outside-rth",
            symbol="AAPL.US",
            market="US",
            config_version="complete-version",
            session_date=_SESSION_OPEN.date(),
            bar_at=_SESSION_OPEN - timedelta(minutes=1),
            close_price=100.0,
        )
        with_outside = StrategyV2ShadowService._daily_evidence(
            [outside, *rows],
            [],
        )[0]

        assert complete.complete_session is True
        assert complete.coverage_ratio == pytest.approx(1.0)
        assert complete.missing_internal_bars == 0
        assert complete.partial_start is False
        assert complete.partial_end is False
        assert complete.outside_session_bars == 0
        assert complete.eligible_bars == 1
        assert with_gap.complete_session is False
        assert with_gap.coverage_ratio == pytest.approx(389 / 390)
        assert with_gap.missing_internal_bars == 1
        assert with_outside.complete_session is False
        assert with_outside.coverage_ratio == pytest.approx(1.0)
        assert with_outside.outside_session_bars == 1

    def test_readiness_quality_blocks_loss_drawdown_and_cost_stress(self) -> None:
        def trades(
            values: list[float],
            *,
            estimated_fee: float,
            exit_price: float,
        ) -> list[StrategyV2ShadowTrade]:
            return [
                StrategyV2ShadowTrade(
                    symbol="AAPL.US",
                    config_version="quality-version",
                    status="CLOSED",
                    entry_at=_SESSION_OPEN + timedelta(minutes=index),
                    exit_at=_SESSION_OPEN + timedelta(minutes=index + 1),
                    entry_price=100.0,
                    exit_price=exit_price,
                    quantity=1.0,
                    gross_pnl=value + estimated_fee,
                    estimated_fees=estimated_fee,
                    net_pnl=value,
                    fee_source="ESTIMATED",
                    estimated_fee_rate=0.0005,
                )
                for index, value in enumerate(values)
            ]

        _loss_quality, loss_blockers = StrategyV2ShadowService._readiness_quality(
            trades([-1.0] * 50, estimated_fee=0.1, exit_price=99.0),
            {"slippage_bps": 2.0},
        )
        drawdown_values = [0.2] * 25 + [-4.0] + [0.1] * 24
        drawdown_quality, drawdown_blockers = (
            StrategyV2ShadowService._readiness_quality(
                trades(drawdown_values, estimated_fee=0.0, exit_price=100.0),
                {"slippage_bps": 0.0},
            )
        )
        cost_quality, cost_blockers = StrategyV2ShadowService._readiness_quality(
            trades([0.05] * 50, estimated_fee=0.04, exit_price=100.1),
            {"slippage_bps": 2.0},
        )
        pass_quality, pass_blockers = StrategyV2ShadowService._readiness_quality(
            trades([0.9] * 50, estimated_fee=0.1, exit_price=101.0),
            {"slippage_bps": 2.0},
        )

        assert "NET_PNL_NON_POSITIVE" in loss_blockers
        assert "MAX_DRAWDOWN_EXCEEDS_NET_PNL" in drawdown_blockers
        assert drawdown_quality is not None
        assert drawdown_quality["max_drawdown"] == pytest.approx(4.0)
        assert "COST_STRESS_NET_PNL_NON_POSITIVE" in cost_blockers
        assert cost_quality is not None
        assert float(cost_quality["cost_stressed_net_pnl"]) < 0
        assert pass_blockers == []
        assert pass_quality is not None
        assert float(pass_quality["cost_stressed_net_pnl"]) > 0

    def test_config_updates_and_listing_are_scoped_by_symbol(self) -> None:
        with self._db() as db:
            service = StrategyV2ShadowService(db)
            primary = service.get_config()
            secondary = service.get_config("MSFT.US")

            updated = service.update_config(
                StrategyV2ShadowConfigUpdate(enabled=True, max_adx=17.0),
                symbol="MSFT.US",
            )
            configs = service.list_configs()

            assert primary.symbol == "AAPL.US"
            assert primary.enabled is False
            assert secondary.symbol == "MSFT.US"
            assert updated.symbol == "MSFT.US"
            assert updated.enabled is True
            assert updated.max_adx == pytest.approx(17.0)
            assert service.get_config("AAPL.US").enabled is False
            assert [item.symbol for item in configs] == ["AAPL.US", "MSFT.US"]

    @pytest.mark.parametrize(
        "symbol",
        ["X.BAD", "AAPL.EU", "AAPL", "AAPL_US", "AAPL.US.EXTRA", "AAPL!.US"],
    )
    def test_symbol_resolution_accepts_only_us_or_hk_code_market(self, symbol: str) -> None:
        with self._db() as db:
            with pytest.raises(ValueError, match="CODE.MARKET"):
                StrategyV2ShadowService(db).get_config(symbol)

            assert db.query(StrategyV2ShadowConfig).count() == 0

    def test_update_rejects_threshold_inversion_but_allows_safe_disable(self) -> None:
        with self._db() as db:
            service = StrategyV2ShadowService(db)
            with pytest.raises(ValueError, match="breach_zscore"):
                service.update_config(
                    StrategyV2ShadowConfigUpdate(
                        breach_zscore=-0.5,
                        reclaim_zscore=-1.0,
                    )
                )

            config = db.query(StrategyV2ShadowConfig).filter_by(symbol="AAPL.US").one()
            config.enabled = True
            version_before = service._config_version(config)
            state = StrategyV2ShadowState(
                symbol="AAPL.US",
                config_version=version_before,
                phase="LONG",
                state_json='{"state":"LONG"}',
            )
            db.add(state)
            db.add(
                StrategyV2ShadowTrade(
                    symbol="AAPL.US",
                    config_version=version_before,
                    status="OPEN",
                    entry_at=_SESSION_OPEN,
                    entry_price=100,
                    quantity=1,
                )
            )
            db.commit()
            with pytest.raises(ValueError, match="virtual trade is open"):
                service.update_config(StrategyV2ShadowConfigUpdate(max_adx=18.0))

            response = service.update_config(StrategyV2ShadowConfigUpdate(enabled=False))
            db.refresh(state)
            assert response.enabled is False
            assert response.config_version == version_before
            assert state.phase == "LONG"
            assert state.state_json == '{"state":"LONG"}'

            db.query(StrategyV2ShadowTrade).delete()
            db.commit()
            reenabled = service.update_config(StrategyV2ShadowConfigUpdate(enabled=True))
            assert reenabled.config_version == version_before

    def test_tick_is_forward_only_rate_limited_and_idempotent(self) -> None:
        provider = _FakeCandles(_candles())
        with self._db() as db:
            self._enabled_config(db, activated_at=_SESSION_OPEN + timedelta(minutes=100))
            service = StrategyV2ShadowService(db, provider)
            first_now = _SESSION_OPEN + timedelta(minutes=181, seconds=10)
            service.tick("AAPL.US", "US", now=first_now)

            rows = db.query(StrategyV2ShadowDecision).order_by(
                StrategyV2ShadowDecision.bar_at
            ).all()
            assert rows
            assert min(row.bar_at.replace(tzinfo=timezone.utc) for row in rows) >= (
                _SESSION_OPEN + timedelta(minutes=100)
            )
            first_count = len(rows)
            assert provider.calls == [("AAPL.US", "MIN_1", 500)]

            service.tick(
                "AAPL.US",
                "US",
                now=first_now + timedelta(seconds=30),
            )
            assert len(provider.calls) == 1

            service.tick(
                "AAPL.US",
                "US",
                now=first_now + timedelta(seconds=60),
            )
            assert len(provider.calls) == 2
            assert db.query(StrategyV2ShadowDecision).count() == first_count
            state = db.query(StrategyV2ShadowState).filter_by(symbol="AAPL.US").one()
            snapshot = state.state_json
            assert "last_processed_at" in snapshot

    def test_tick_stops_at_gap_then_recovers_in_order(self) -> None:
        initial = _candles(121)
        provider = _FakeCandles(initial)
        with self._db() as db:
            self._enabled_config(db, activated_at=_SESSION_OPEN)
            service = StrategyV2ShadowService(db, provider)
            service.tick(
                "AAPL.US",
                "US",
                now=_SESSION_OPEN + timedelta(minutes=121, seconds=10),
            )
            state = db.query(StrategyV2ShadowState).filter_by(symbol="AAPL.US").one()
            assert state.last_bar_at is not None
            assert state.last_bar_at.replace(tzinfo=timezone.utc) == _SESSION_OPEN + timedelta(minutes=120)

            with_gap = _candles(124)
            del with_gap[122]
            provider.candles = with_gap
            service.tick(
                "AAPL.US",
                "US",
                now=_SESSION_OPEN + timedelta(minutes=124, seconds=10),
            )
            db.refresh(state)
            assert state.last_bar_at is not None
            assert state.last_bar_at.replace(tzinfo=timezone.utc) == _SESSION_OPEN + timedelta(minutes=121)
            assert state.last_poll_error == (
                f"DATA_GAP_WAITING:{(_SESSION_OPEN + timedelta(minutes=122)).isoformat()}"
            )

            provider.candles = _candles(125)
            service.tick(
                "AAPL.US",
                "US",
                now=_SESSION_OPEN + timedelta(minutes=125, seconds=10),
            )
            db.refresh(state)
            assert state.last_bar_at is not None
            assert state.last_bar_at.replace(tzinfo=timezone.utc) == _SESSION_OPEN + timedelta(minutes=124)
            assert state.last_poll_error == ""

    def test_tick_pages_from_watermark_when_recent_window_moved_past_gap(
        self,
    ) -> None:
        provider = _PagedFakeCandles(_candles(121), [])
        with self._db() as db:
            self._enabled_config(db, activated_at=_SESSION_OPEN)
            service = StrategyV2ShadowService(db, provider)
            service.tick(
                "AAPL.US",
                "US",
                now=_SESSION_OPEN + timedelta(minutes=121, seconds=10),
            )
            state = db.query(StrategyV2ShadowState).filter_by(symbol="AAPL.US").one()
            assert state.last_bar_at is not None
            frontier = state.last_bar_at.replace(tzinfo=timezone.utc)

            provider.candles = [
                BrokerCandle(
                    timestamp=datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc),
                    open=100,
                    high=100.1,
                    low=99.9,
                    close=100,
                    volume=1000,
                )
            ]
            provider.historical = _candles(181)
            service.tick(
                "AAPL.US",
                "US",
                now=datetime(2026, 7, 13, 13, 31, 10, tzinfo=timezone.utc),
            )

            db.refresh(state)
            assert provider.history_calls == [
                (
                    "AAPL.US",
                    "MIN_1",
                    500,
                    _SESSION_OPEN - timedelta(minutes=1),
                )
            ]
            assert state.last_bar_at is not None
            assert state.last_bar_at.replace(tzinfo=timezone.utc) > frontier
            assert state.last_poll_error == ""
            paged_decisions = [
                (
                    row.bar_at.replace(tzinfo=timezone.utc),
                    row.action,
                    row.reason,
                )
                for row in db.query(StrategyV2ShadowDecision)
                .order_by(StrategyV2ShadowDecision.bar_at)
                .all()
            ]
            paged_snapshot = json.loads(state.state_json)

        control_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=control_engine)
        try:
            with Session(bind=control_engine) as control_db:
                control_config = StrategyV2ShadowConfig(
                    symbol="AAPL.US",
                    enabled=True,
                    updated_at=_SESSION_OPEN,
                )
                control_state = StrategyV2ShadowState(
                    symbol="AAPL.US",
                    state_json="{}",
                )
                control_db.add_all([control_config, control_state])
                control_db.commit()
                StrategyV2ShadowService(control_db)._evaluate_candles(
                    config=control_config,
                    state=control_state,
                    market="US",
                    one_minute=_candles(181),
                    observed_at=datetime(
                        2026,
                        7,
                        13,
                        13,
                        31,
                        10,
                        tzinfo=timezone.utc,
                    ),
                )
                control_decisions = [
                    (
                        row.bar_at.replace(tzinfo=timezone.utc),
                        row.action,
                        row.reason,
                    )
                    for row in control_db.query(StrategyV2ShadowDecision)
                    .order_by(StrategyV2ShadowDecision.bar_at)
                    .all()
                ]
                control_snapshot = json.loads(control_state.state_json)
        finally:
            control_engine.dispose()

        assert paged_decisions == control_decisions
        assert paged_snapshot == control_snapshot

    def test_tick_filters_unsettled_bar_and_collects_after_close(self) -> None:
        provider = _FakeCandles(_candles(3))
        with self._db() as db:
            self._enabled_config(db, activated_at=_SESSION_OPEN)
            service = StrategyV2ShadowService(db, provider)
            service.tick(
                "AAPL.US",
                "US",
                now=_SESSION_OPEN + timedelta(minutes=2, seconds=3),
            )
            state = db.query(StrategyV2ShadowState).filter_by(symbol="AAPL.US").one()
            assert state.last_bar_at is not None
            assert state.last_bar_at.replace(tzinfo=timezone.utc) == _SESSION_OPEN

            provider.candles = _candles(390)
            service.tick(
                "AAPL.US",
                "US",
                now=datetime(2026, 7, 10, 20, 5, tzinfo=timezone.utc),
            )
            assert len(provider.calls) == 2
            service.tick(
                "AAPL.US",
                "US",
                now=datetime(2026, 7, 10, 20, 16, tzinfo=timezone.utc),
            )
            assert len(provider.calls) == 2

    def test_contiguous_frontier_skips_lunch_weekend_and_detects_first_rth_gap(self) -> None:
        service = StrategyV2ShadowService(self._db())
        try:
            assert service._missing_rth_minute(
                "HK",
                previous=datetime(2026, 7, 10, 3, 59, tzinfo=timezone.utc),
                current=datetime(2026, 7, 10, 5, 0, tzinfo=timezone.utc),
            ) is None
            assert service._missing_rth_minute(
                "HK",
                previous=datetime(2026, 7, 10, 3, 59, tzinfo=timezone.utc),
                current=datetime(2026, 7, 10, 5, 1, tzinfo=timezone.utc),
            ) == datetime(2026, 7, 10, 5, 0, tzinfo=timezone.utc)
            assert service._missing_rth_minute(
                "US",
                previous=datetime(2026, 7, 10, 19, 59, tzinfo=timezone.utc),
                current=datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc),
            ) is None
            assert service._missing_rth_minute(
                "HK",
                previous=datetime(2026, 12, 24, 3, 59, tzinfo=timezone.utc),
                current=datetime(2026, 12, 28, 1, 30, tzinfo=timezone.utc),
            ) is None
            assert service._in_post_close_collection_window(
                "HK",
                datetime(2026, 12, 24, 4, 5, tzinfo=timezone.utc),
            ) is True
        finally:
            service.db.close()

    def test_disabled_or_outside_rth_never_fetches(self) -> None:
        provider = _FakeCandles(_candles())
        with self._db() as db:
            service = StrategyV2ShadowService(db, provider)
            service.get_config()
            service.tick(
                "AAPL.US",
                "US",
                now=_SESSION_OPEN + timedelta(minutes=60),
            )
            config = db.query(StrategyV2ShadowConfig).filter_by(symbol="AAPL.US").one()
            config.enabled = True
            db.commit()
            service.tick(
                "AAPL.US",
                "US",
                now=datetime(2026, 7, 11, 14, 0, tzinfo=timezone.utc),
            )

        assert provider.calls == []

    def test_tick_records_empty_and_unprocessable_candle_failures(self) -> None:
        for candles, expected in (
            ([], "empty candle response"),
            (
                [
                    BrokerCandle(
                        timestamp=datetime(2026, 7, 10, 1, 0, tzinfo=timezone.utc),
                        open=100,
                        high=101,
                        low=99,
                        close=100,
                        volume=1000,
                    )
                ],
                "no processable one-minute bars",
            ),
        ):
            provider = _FakeCandles(candles)
            with self._db() as db:
                for model in (
                    StrategyV2ShadowDecision,
                    StrategyV2ShadowTrade,
                    StrategyV2ShadowState,
                    StrategyV2ShadowConfig,
                ):
                    db.query(model).delete()
                db.commit()
                self._enabled_config(db, activated_at=_SESSION_OPEN)
                service = StrategyV2ShadowService(db, provider)

                with pytest.raises(ValueError, match=expected):
                    service.tick(
                        "AAPL.US",
                        "US",
                        now=_SESSION_OPEN + timedelta(minutes=30),
                    )

                state = db.query(StrategyV2ShadowState).filter_by(symbol="AAPL.US").one()
                assert expected in state.last_poll_error

    @pytest.mark.parametrize(
        ("values", "expected_index"),
        [
            ([object()], 0),
            (
                [
                    BrokerCandle(
                        timestamp=_SESSION_OPEN,
                        open=100,
                        high=101,
                        low=99,
                        close=float("nan"),
                        volume=1000,
                    )
                ],
                0,
            ),
            (
                [
                    BrokerCandle(
                        timestamp=_SESSION_OPEN,
                        open=100,
                        high=99,
                        low=98,
                        close=100,
                        volume=1000,
                    )
                ],
                0,
            ),
            (
                [
                    BrokerCandle(
                        timestamp=_SESSION_OPEN,
                        open=100,
                        high=101,
                        low=99,
                        close=100,
                        volume=1000,
                    ),
                    BrokerCandle(
                        timestamp=_SESSION_OPEN + timedelta(minutes=1),
                        open=100,
                        high=101,
                        low=99,
                        close=100,
                        volume=-1,
                    ),
                ],
                1,
            ),
        ],
    )
    def test_candle_coercion_fails_closed_on_bad_market_data(
        self,
        values: list[object],
        expected_index: int,
    ) -> None:
        with pytest.raises(ValueError, match=rf"index {expected_index}"):
            StrategyV2ShadowService._coerce_strategy_bars(values, symbol="AAPL.US")

    def test_virtual_ledger_uses_domain_fill_once_and_estimated_net_pnl(self) -> None:
        with self._db() as db:
            config = self._enabled_config(db, activated_at=_SESSION_OPEN)
            service = StrategyV2ShadowService(db)
            feature = type(
                "Feature",
                (),
                {
                    "bar": StrategyBar(
                        timestamp=_SESSION_OPEN,
                        open=100,
                        high=101,
                        low=99,
                        close=100,
                        volume=1000,
                        symbol="AAPL.US",
                    ),
                    "session_vwap_1m": 100.0,
                },
            )()
            entry_row = StrategyV2ShadowDecision(
                idempotency_key="entry",
                symbol="AAPL.US",
                market="US",
                config_version=service._config_version(config),
                session_date=_SESSION_OPEN.date(),
                bar_at=_SESSION_OPEN,
                action="FILL_ENTRY",
                state_before="ENTRY_PENDING",
                state_after="LONG",
                close_price=100,
            )
            db.add(entry_row)
            db.flush()
            entry = StrategyV2Decision(
                timestamp=_SESSION_OPEN,
                action=StrategyV2Action.FILL_ENTRY,
                reason="NEXT_BAR_OPEN_FILL",
                state_before=StrategyV2State.ENTRY_PENDING,
                state_after=StrategyV2State.LONG,
                price=100.02,
                quantity=1,
                stop_price=99.0,
                target_price=101.0,
            )
            position = VirtualPosition(
                entry_price=100.02,
                entry_at=_SESSION_OPEN,
                quantity=1,
                stop_price=99.0,
                target_price=101.0,
                signal_vwap=99.5,
                holding_deadline=_SESSION_OPEN + timedelta(minutes=60),
                config_version="domain-version",
            )
            service._apply_virtual_trade(
                entry_row,
                entry,
                feature,
                config,
                "US",
                position=position,
            )
            trade = db.query(StrategyV2ShadowTrade).filter_by(status="OPEN").one()
            assert trade.entry_price == pytest.approx(100.02)
            assert trade.highest_price == pytest.approx(100.02)
            assert trade.lowest_price == pytest.approx(100.02)
            assert trade.signal_vwap == pytest.approx(99.5)
            assert trade.holding_deadline is not None
            assert trade.holding_deadline.replace(tzinfo=timezone.utc) == (
                _SESSION_OPEN + timedelta(minutes=60)
            )

            live = db.query(StrategyConfig).one()
            live.fee_rate_us = 0.05
            db.commit()

            exit_row = StrategyV2ShadowDecision(
                idempotency_key="exit",
                symbol="AAPL.US",
                market="US",
                config_version=service._config_version(config),
                session_date=_SESSION_OPEN.date(),
                bar_at=_SESSION_OPEN + timedelta(minutes=5),
                action="EXIT_LONG",
                state_before="LONG",
                state_after="READY",
                close_price=102,
            )
            db.add(exit_row)
            db.flush()
            exit_decision = StrategyV2Decision(
                timestamp=_SESSION_OPEN + timedelta(minutes=5),
                action=StrategyV2Action.EXIT_LONG,
                reason="PROFIT_TARGET",
                state_before=StrategyV2State.LONG,
                state_after=StrategyV2State.READY,
                price=101.98,
                quantity=1,
            )
            service._apply_virtual_trade(
                exit_row,
                exit_decision,
                feature,
                config,
                "US",
            )

            # Exit visibility must be immediate even though this project's
            # Session disables autoflush. A later decision in the same candle
            # batch must not observe the just-closed row as a ghost position.
            assert service._open_trade("AAPL.US") is None

            next_entry_row = StrategyV2ShadowDecision(
                idempotency_key="next-entry",
                symbol="AAPL.US",
                market="US",
                config_version=service._config_version(config),
                session_date=_SESSION_OPEN.date(),
                bar_at=_SESSION_OPEN + timedelta(minutes=20),
                action="FILL_ENTRY",
                state_before="ENTRY_PENDING",
                state_after="LONG",
                close_price=100,
            )
            db.add(next_entry_row)
            db.flush()
            next_entry = StrategyV2Decision(
                timestamp=_SESSION_OPEN + timedelta(minutes=20),
                action=StrategyV2Action.FILL_ENTRY,
                reason="NEXT_BAR_OPEN_FILL",
                state_before=StrategyV2State.ENTRY_PENDING,
                state_after=StrategyV2State.LONG,
                price=100.02,
                quantity=1,
                stop_price=99.0,
                target_price=101.0,
            )
            service._apply_virtual_trade(
                next_entry_row,
                next_entry,
                feature,
                config,
                "US",
                position=position,
            )

            assert trade.exit_price == pytest.approx(101.98)
            assert trade.gross_pnl == pytest.approx(1.96)
            assert trade.estimated_fees == pytest.approx((100.02 + 101.98) * 0.0005)
            assert trade.net_pnl == pytest.approx(1.96 - (100.02 + 101.98) * 0.0005)
            assert trade.fee_source == "ESTIMATED"
            assert trade.estimated_fee_rate == pytest.approx(0.0005)
            assert trade.mfe_pct == pytest.approx((101.98 - 100.02) / 100.02)
            assert trade.mae_pct == pytest.approx(0.0)
            assert db.query(StrategyV2ShadowTrade).filter_by(status="CLOSED").count() == 1
            assert db.query(StrategyV2ShadowTrade).filter_by(status="OPEN").count() == 1

    def test_algorithm_version_change_manages_frozen_open_trade_then_starts_forward(self) -> None:
        with self._db() as db:
            config = self._enabled_config(db, activated_at=_SESSION_OPEN)
            service = StrategyV2ShadowService(db)
            current_version = service._config_version(config)
            legacy_version = "legacy-algorithm-version"
            entry_at = _SESSION_OPEN
            last_bar_at = _SESSION_OPEN + timedelta(minutes=1)
            position = VirtualPosition(
                entry_price=100.0,
                entry_at=entry_at,
                quantity=1.0,
                stop_price=99.0,
                target_price=101.0,
                signal_vwap=100.5,
                holding_deadline=entry_at + timedelta(minutes=60),
                config_version="frozen-domain-version",
            )
            snapshot = StrategyV2EngineSnapshot(
                state=StrategyV2State.LONG,
                session_day=_SESSION_OPEN.date(),
                arm_bar_index=None,
                arm_previous_zscore=None,
                arm_trough_zscore=None,
                pending_signal_at=None,
                pending_signal_bar_index=None,
                pending_signal_vwap=None,
                entries_this_session=1,
                last_exit_at=None,
                position=position,
                last_processed_session_day=_SESSION_OPEN.date(),
                last_processed_at=last_bar_at,
            )
            state = StrategyV2ShadowState(
                symbol="AAPL.US",
                config_version=legacy_version,
                session_date=_SESSION_OPEN.date(),
                phase=StrategyV2State.LONG.value,
                last_bar_at=last_bar_at,
                open_trade_id=1,
                state_json=json.dumps(snapshot.to_dict()),
            )
            trade = StrategyV2ShadowTrade(
                id=1,
                symbol="AAPL.US",
                config_version=legacy_version,
                status="OPEN",
                entry_at=entry_at,
                entry_price=100.0,
                quantity=1.0,
                stop_price=99.0,
                target_price=101.0,
                signal_vwap=100.5,
                holding_deadline=position.holding_deadline,
                estimated_fees=0.05,
                estimated_fee_rate=0.0005,
            )
            db.add_all([state, trade])
            db.commit()

            exit_bar_at = last_bar_at + timedelta(minutes=1)
            candles = [
                BrokerCandle(
                    timestamp=last_bar_at,
                    open=100,
                    high=100.2,
                    low=99.8,
                    close=100,
                    volume=1000,
                ),
                BrokerCandle(
                    timestamp=exit_bar_at,
                    open=100.5,
                    high=101.2,
                    low=100.4,
                    close=101,
                    volume=1000,
                ),
                BrokerCandle(
                    timestamp=exit_bar_at + timedelta(minutes=1),
                    open=100,
                    high=100.2,
                    low=99.8,
                    close=100,
                    volume=1000,
                ),
            ]

            service._evaluate_candles(
                config=config,
                state=state,
                market="US",
                one_minute=candles,
                observed_at=exit_bar_at + timedelta(minutes=2),
            )

            db.refresh(state)
            db.refresh(trade)
            decisions = db.query(StrategyV2ShadowDecision).order_by(
                StrategyV2ShadowDecision.bar_at
            ).all()
            assert trade.status == "CLOSED"
            assert trade.exit_reason == "PROFIT_TARGET"
            assert [
                (
                    row.bar_at.replace(tzinfo=timezone.utc),
                    row.config_version,
                    row.action,
                )
                for row in decisions
            ] == [
                (exit_bar_at, legacy_version, StrategyV2Action.EXIT_LONG.value)
            ]
            assert state.config_version == current_version
            assert state.phase == StrategyV2State.COLD.value
            assert state.last_bar_at is not None
            assert state.last_bar_at.replace(tzinfo=timezone.utc) == exit_bar_at
            assert state.open_trade_id is None
            assert state.state_json == "{}"

            future_bar_at = exit_bar_at + timedelta(minutes=1)
            service._evaluate_candles(
                config=config,
                state=state,
                market="US",
                one_minute=candles,
                observed_at=future_bar_at + timedelta(minutes=1, seconds=10),
            )

            current_rows = db.query(StrategyV2ShadowDecision).filter_by(
                config_version=current_version
            ).all()
            assert current_rows
            assert min(
                row.bar_at.replace(tzinfo=timezone.utc) for row in current_rows
            ) == future_bar_at

    def test_flat_legacy_version_resets_forward_without_rewriting_evidence(self) -> None:
        observed_at = _SESSION_OPEN + timedelta(minutes=180, seconds=10)
        with self._db() as db:
            config = self._enabled_config(db, activated_at=_SESSION_OPEN)
            service = StrategyV2ShadowService(db, _FakeCandles(_candles(181)))
            current_version = service._config_version(config)
            legacy_version = "legacy-with-internal-gap"
            db.add(
                StrategyV2ShadowState(
                    symbol="AAPL.US",
                    config_version=legacy_version,
                    phase="FLAT",
                    last_bar_at=_SESSION_OPEN + timedelta(minutes=179),
                    state_json="{}",
                )
            )
            db.add(
                StrategyV2ShadowDecision(
                    idempotency_key="legacy-evidence",
                    symbol="AAPL.US",
                    market="US",
                    config_version=legacy_version,
                    session_date=_SESSION_OPEN.date(),
                    bar_at=_SESSION_OPEN + timedelta(minutes=178),
                    action="NO_ACTION",
                    state_before="READY",
                    state_after="READY",
                    close_price=100,
                )
            )
            db.commit()

            before_activation = datetime.now(timezone.utc)
            service.tick("AAPL.US", "US", now=observed_at)

            state = db.query(StrategyV2ShadowState).filter_by(symbol="AAPL.US").one()
            assert state.config_version == current_version
            assert state.last_bar_at is not None
            assert state.last_bar_at.replace(tzinfo=timezone.utc) == observed_at
            assert db.query(StrategyV2ShadowDecision).filter_by(
                config_version=legacy_version
            ).count() == 1
            assert db.query(StrategyV2ShadowDecision).filter_by(
                config_version=current_version
            ).count() == 0

            snapshot = service._ensure_version_snapshot(config)
            assert snapshot.activated_at is not None
            assert snapshot.activated_at.replace(tzinfo=timezone.utc) >= before_activation
