from __future__ import annotations

import json
import math
import random
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.services.strategy_v2_shadow_service as shadow_service_module
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
from app.schemas import (
    StrategyV2AdxChallengerRequest,
    StrategyV2ReplayBar,
    StrategyV2ShadowConfigUpdate,
)
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


def _candles(
    count: int = 180,
    *,
    start: datetime = _SESSION_OPEN,
) -> list[BrokerCandle]:
    result: list[BrokerCandle] = []
    for index in range(count):
        close = 100 + math.sin(index / 6) * 0.35 + index * 0.0005
        result.append(
            BrokerCandle(
                timestamp=start + timedelta(minutes=index),
                open=close - 0.01,
                high=close + 0.08,
                low=close - 0.08,
                close=close,
                volume=1000 + index,
            )
        )
    return result


def _closed_trade_candles() -> list[BrokerCandle]:
    rng = random.Random(1)
    displacement = 0.0
    result: list[BrokerCandle] = []
    for index in range(390):
        displacement = 0.75 * displacement + rng.gauss(0.0, 0.18)
        close = 100.0 + displacement
        open_price = close + rng.gauss(0.0, 0.015)
        result.append(
            BrokerCandle(
                timestamp=_SESSION_OPEN + timedelta(minutes=index),
                open=open_price,
                high=max(open_price, close) + 0.05,
                low=min(open_price, close) - 0.05,
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
        establish_current_state: bool = True,
    ) -> StrategyV2ShadowConfig:
        config = StrategyV2ShadowConfig(
            symbol="AAPL.US",
            enabled=True,
            updated_at=activated_at,
        )
        db.add(config)
        db.commit()
        if establish_current_state:
            db.add(StrategyV2ShadowState(
                symbol=config.symbol,
                config_version=StrategyV2ShadowService(db)._config_version(config),
                state_json="{}",
            ))
            db.commit()
        return config

    def _collect_complete_session(
        self,
        db: Session,
    ) -> tuple[StrategyV2ShadowService, str]:
        config = self._enabled_config(
            db,
            activated_at=_SESSION_OPEN - timedelta(days=1),
        )
        service = StrategyV2ShadowService(db, _FakeCandles(_candles(390)))
        service.tick(
            "AAPL.US",
            "US",
            now=_SESSION_OPEN + timedelta(minutes=390, seconds=10),
        )
        version = service._config_version(config)
        service._ensure_version_snapshot(config)
        return service, version

    def _collect_complete_trade_session(
        self,
        db: Session,
    ) -> tuple[StrategyV2ShadowService, str]:
        config = StrategyV2ShadowConfig(
            symbol="AAPL.US",
            enabled=True,
            zscore_window_1m_bars=10,
            zscore_window_5m_bars=5,
            breach_zscore=-0.5,
            reclaim_zscore=-0.1,
            five_minute_zscore_max=0.0,
            adx_period=5,
            max_adx=40.0,
            realized_vol_window_bars=10,
            min_realized_vol=0.0,
            max_realized_vol=3.0,
            profit_target_pct=0.05,
            updated_at=_SESSION_OPEN - timedelta(days=1),
        )
        db.add(config)
        db.commit()
        service = StrategyV2ShadowService(
            db,
            _FakeCandles(_closed_trade_candles()),
        )
        version = service._config_version(config)
        db.add(StrategyV2ShadowState(
            symbol=config.symbol,
            config_version=version,
            state_json="{}",
        ))
        service._ensure_version_snapshot(config)
        service.tick(
            "AAPL.US",
            "US",
            now=_SESSION_OPEN + timedelta(minutes=390, seconds=10),
        )
        return service, version

    def _collect_two_complete_sessions(
        self,
        db: Session,
        *,
        target_day_offset: int = 3,
    ) -> tuple[StrategyV2ShadowService, str, datetime]:
        config = self._enabled_config(
            db,
            activated_at=_SESSION_OPEN - timedelta(days=1),
        )
        candles = _FakeCandles(_candles(390))
        service = StrategyV2ShadowService(db, candles)
        service.tick(
            "AAPL.US",
            "US",
            now=_SESSION_OPEN + timedelta(minutes=390, seconds=10),
        )
        target_open = _SESSION_OPEN + timedelta(days=target_day_offset)
        candles.candles = _candles(390, start=target_open)
        service.tick(
            "AAPL.US",
            "US",
            now=target_open + timedelta(minutes=390, seconds=10),
        )
        version = service._config_version(config)
        service._ensure_version_snapshot(config)
        return service, version, target_open

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
                gate_passed=index == 140,
                gate_reasons_json=json.dumps(
                    ["ADX_5M_WARMUP"] if index < 139 else ["NO_BREACH"]
                ),
            )
            for index in range(390)
        ]

        complete = StrategyV2ShadowService._daily_evidence(rows, [])[0]
        with_duplicate = StrategyV2ShadowService._daily_evidence(
            [*rows, rows[140]],
            [],
        )[0]
        conflicting_duplicate = StrategyV2ShadowDecision(
            idempotency_key="conflicting-duplicate",
            symbol="AAPL.US",
            market="US",
            config_version="complete-version",
            session_date=_SESSION_OPEN.date(),
            bar_at=_SESSION_OPEN + timedelta(minutes=140),
            close_price=100.0,
            gate_passed=False,
            gate_reasons_json=json.dumps(["ADX_REGIME_BLOCKED"]),
        )
        with_conflicting_duplicate = StrategyV2ShadowService._daily_evidence(
            [*rows, conflicting_duplicate],
            [],
        )[0]
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
        rows[200].gate_reasons_json = json.dumps(["SESSION_DATA_INCOMPLETE"])
        with_incomplete_features = StrategyV2ShadowService._daily_evidence(
            rows,
            [],
        )[0]
        rows[200].gate_reasons_json = "{malformed"
        with_malformed_features = StrategyV2ShadowService._daily_evidence(
            rows,
            [],
        )[0]

        assert complete.complete_session is True
        assert complete.coverage_ratio == pytest.approx(1.0)
        assert complete.missing_internal_bars == 0
        assert complete.partial_start is False
        assert complete.partial_end is False
        assert complete.outside_session_bars == 0
        assert complete.eligible_bars == 1
        assert complete.first_ready_at == _SESSION_OPEN + timedelta(minutes=139)
        assert complete.ready_bars == 251
        assert complete.warmup_lost_bars == 139
        assert complete.hourly_eligibility[0].session_hour == 9
        assert complete.hourly_eligibility[0].bars == 30
        assert complete.hourly_eligibility[0].ready_bars == 0
        assert complete.hourly_eligibility[2].session_hour == 11
        assert complete.hourly_eligibility[2].ready_bars == 11
        assert all(
            "NO_BREACH" not in item.gate_counts
            for item in complete.hourly_eligibility
        )
        assert with_duplicate.bars == 390
        assert with_duplicate.ready_bars == 251
        assert with_duplicate.eligible_bars == 1
        assert with_conflicting_duplicate.complete_session is False
        assert with_conflicting_duplicate.incomplete_feature_bars == 1
        assert with_conflicting_duplicate.eligible_bars == 0
        assert with_gap.complete_session is False
        assert with_gap.coverage_ratio == pytest.approx(389 / 390)
        assert with_gap.missing_internal_bars == 1
        assert with_outside.complete_session is False
        assert with_outside.coverage_ratio == pytest.approx(1.0)
        assert with_outside.outside_session_bars == 1
        assert with_incomplete_features.complete_session is False
        assert with_incomplete_features.incomplete_feature_bars == 1
        assert with_malformed_features.complete_session is False
        assert with_malformed_features.incomplete_feature_bars == 1

    def test_gate_counts_use_unique_bars_as_the_denominator(self) -> None:
        with self._db() as db:
            db.add_all([
                StrategyV2ShadowDecision(
                    idempotency_key=f"gate-count-{index}",
                    symbol="AAPL.US",
                    market="US",
                    config_version="gate-version",
                    session_date=_SESSION_OPEN.date(),
                    bar_at=_SESSION_OPEN + timedelta(minutes=minute),
                    close_price=100.0,
                    gate_reasons_json=json.dumps(["ADX_REGIME_BLOCKED"]),
                )
                for index, minute in enumerate((0, 0, 1))
            ])
            db.add_all([
                StrategyV2ShadowDecision(
                    idempotency_key=f"invalid-gate-{index}",
                    symbol="AAPL.US",
                    market="US",
                    config_version="gate-version",
                    session_date=_SESSION_OPEN.date(),
                    bar_at=_SESSION_OPEN + timedelta(minutes=minute),
                    close_price=100.0,
                    gate_reasons_json=raw,
                )
                for index, (minute, raw) in enumerate(
                    ((2, "{malformed"), (2, json.dumps({"reason": "bad"})))
                )
            ])
            db.commit()

            counts = StrategyV2ShadowService(db)._gate_counts(
                "AAPL.US",
                "gate-version",
            )

        assert counts == {
            "ADX_REGIME_BLOCKED": 2,
            "FEATURE_EVIDENCE_INVALID": 1,
        }

    def test_trade_evidence_requires_linked_rth_ordered_same_session_decisions(
        self,
    ) -> None:
        version = "evidence-version"

        def decision(
            row_id: int,
            action: str,
            bar_at: datetime,
            *,
            session_date: date = _SESSION_OPEN.date(),
        ) -> StrategyV2ShadowDecision:
            return StrategyV2ShadowDecision(
                id=row_id,
                idempotency_key=f"evidence-{row_id}-{bar_at.isoformat()}",
                symbol="AAPL.US",
                market="US",
                config_version=version,
                session_date=session_date,
                bar_at=bar_at,
                action=action,
                close_price=100.0,
            )

        def trade(
            entry: StrategyV2ShadowDecision,
            exit_row: StrategyV2ShadowDecision,
        ) -> StrategyV2ShadowTrade:
            return StrategyV2ShadowTrade(
                id=1,
                symbol="AAPL.US",
                config_version=version,
                entry_decision_id=entry.id,
                exit_decision_id=exit_row.id,
                status="CLOSED",
                entry_at=entry.bar_at,
                exit_at=exit_row.bar_at,
                entry_price=100.0,
                exit_price=101.0,
            )

        entry = decision(
            1,
            StrategyV2Action.FILL_ENTRY.value,
            _SESSION_OPEN + timedelta(minutes=10),
        )
        exit_row = decision(
            2,
            StrategyV2Action.EXIT_LONG.value,
            _SESSION_OPEN + timedelta(minutes=20),
        )
        valid = trade(entry, exit_row)
        assert StrategyV2ShadowService._trade_evidence_session_date(
            valid,
            {1: entry, 2: exit_row},
        ) == _SESSION_OPEN.date()
        assert StrategyV2ShadowService._trade_evidence_session_date(
            valid,
            {1: entry},
        ) is None

        wrong_action = decision(
            3,
            StrategyV2Action.WAIT.value,
            entry.bar_at,
        )
        wrong_action_trade = trade(wrong_action, exit_row)
        assert StrategyV2ShadowService._trade_evidence_session_date(
            wrong_action_trade,
            {2: exit_row, 3: wrong_action},
        ) is None

        outside_rth = decision(
            4,
            StrategyV2Action.FILL_ENTRY.value,
            _SESSION_OPEN - timedelta(minutes=1),
        )
        outside_trade = trade(outside_rth, exit_row)
        assert StrategyV2ShadowService._trade_evidence_session_date(
            outside_trade,
            {2: exit_row, 4: outside_rth},
        ) is None

        next_open = datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc)
        next_session_exit = decision(
            5,
            StrategyV2Action.EXIT_LONG.value,
            next_open + timedelta(minutes=10),
            session_date=next_open.date(),
        )
        cross_session_trade = trade(entry, next_session_exit)
        assert StrategyV2ShadowService._trade_evidence_session_date(
            cross_session_trade,
            {1: entry, 5: next_session_exit},
        ) is None

        reverse_entry = decision(
            6,
            StrategyV2Action.FILL_ENTRY.value,
            _SESSION_OPEN + timedelta(minutes=30),
        )
        reverse_exit = decision(
            7,
            StrategyV2Action.EXIT_LONG.value,
            _SESSION_OPEN + timedelta(minutes=29),
        )
        reverse_trade = trade(reverse_entry, reverse_exit)
        assert StrategyV2ShadowService._trade_evidence_session_date(
            reverse_trade,
            {6: reverse_entry, 7: reverse_exit},
        ) is None

        mismatched_time = trade(entry, exit_row)
        mismatched_time.entry_at = entry.bar_at + timedelta(minutes=1)
        assert StrategyV2ShadowService._trade_evidence_session_date(
            mismatched_time,
            {1: entry, 2: exit_row},
        ) is None

    def test_reused_entry_decision_excludes_every_linked_trade(self) -> None:
        with self._db() as db:
            config = self._enabled_config(db, activated_at=_SESSION_OPEN)
            service = StrategyV2ShadowService(db)
            version = service._config_version(config)
            rows = [
                StrategyV2ShadowDecision(
                    idempotency_key=f"duplicate-link-{index}",
                    symbol="AAPL.US",
                    market="US",
                    config_version=version,
                    session_date=_SESSION_OPEN.date(),
                    bar_at=_SESSION_OPEN + timedelta(minutes=index),
                    action=(
                        StrategyV2Action.FILL_ENTRY.value
                        if index == 60
                        else StrategyV2Action.EXIT_LONG.value
                        if index in {61, 62}
                        else StrategyV2Action.WAIT.value
                    ),
                    close_price=100.0,
                )
                for index in range(390)
            ]
            db.add_all(rows)
            db.flush()
            entry = rows[60]
            db.add_all([
                StrategyV2ShadowTrade(
                    symbol="AAPL.US",
                    config_version=version,
                    entry_decision_id=entry.id,
                    exit_decision_id=rows[index].id,
                    status="CLOSED",
                    entry_at=entry.bar_at,
                    exit_at=rows[index].bar_at,
                    entry_price=100.0,
                    exit_price=101.0,
                    quantity=1.0,
                    gross_pnl=1.0,
                    estimated_fees=0.1,
                    net_pnl=0.9,
                )
                for index in (61, 62)
            ])
            db.commit()

            evaluation = service.get_evaluation("AAPL.US")

        assert evaluation.closed_trades == 2
        assert evaluation.eligible_closed_trades == 0
        assert evaluation.excluded_closed_trades == 2
        assert "DATA_TRADE_EVIDENCE_INVALID" in evaluation.readiness_blockers
        assert any(
            "2 closed trades have invalid decision linkage" in warning
            for warning in evaluation.data_quality_warnings
        )

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

    def test_empty_legacy_version_first_tick_only_advances_watermark(self) -> None:
        provider = _FakeCandles(_candles(180))
        first_now = _SESSION_OPEN + timedelta(minutes=60, seconds=10)
        with self._db() as db:
            config = self._enabled_config(
                db,
                activated_at=_SESSION_OPEN,
                establish_current_state=False,
            )
            service = StrategyV2ShadowService(db, provider)

            service.tick("AAPL.US", "US", now=first_now)

            state = db.query(StrategyV2ShadowState).filter_by(symbol="AAPL.US").one()
            assert provider.calls == []
            assert state.config_version == service._config_version(config)
            assert state.last_bar_at is not None
            assert state.last_bar_at.replace(tzinfo=timezone.utc) == (
                _SESSION_OPEN + timedelta(minutes=60)
            )
            assert db.query(StrategyV2ShadowDecision).count() == 0

            service.tick("AAPL.US", "US", now=first_now + timedelta(minutes=2))

            rows = db.query(StrategyV2ShadowDecision).order_by(
                StrategyV2ShadowDecision.bar_at
            ).all()
            assert provider.calls == [("AAPL.US", "MIN_1", 500)]
            assert rows
            assert rows[0].bar_at.replace(tzinfo=timezone.utc) == (
                _SESSION_OPEN + timedelta(minutes=61)
            )

    def test_incomplete_session_before_close_keeps_watermark_for_retry(self) -> None:
        observed_at = _SESSION_OPEN + timedelta(minutes=121, seconds=10)
        with self._db() as db:
            config = self._enabled_config(db, activated_at=_SESSION_OPEN)
            service = StrategyV2ShadowService(db)
            state = db.query(StrategyV2ShadowState).filter_by(symbol="AAPL.US").one()

            service._evaluate_candles(
                config=config,
                state=state,
                market="US",
                one_minute=_candles(120, start=_SESSION_OPEN + timedelta(minutes=1)),
                observed_at=observed_at,
            )

            db.refresh(state)
            assert state.last_bar_at is None
            assert state.last_poll_error == (
                "SESSION_DATA_INCOMPLETE_WAITING:"
                f"{(_SESSION_OPEN + timedelta(minutes=1)).isoformat()}"
            )
            assert db.query(StrategyV2ShadowDecision).count() == 0

            service._evaluate_candles(
                config=config,
                state=state,
                market="US",
                one_minute=_candles(121),
                observed_at=observed_at,
            )

            db.refresh(state)
            assert state.last_bar_at is not None
            assert state.last_bar_at.replace(tzinfo=timezone.utc) == (
                _SESSION_OPEN + timedelta(minutes=120)
            )
            assert state.last_poll_error == ""
            assert db.query(StrategyV2ShadowDecision).count() == 121

    def test_closed_incomplete_session_is_quarantined_then_next_session_collects(
        self,
    ) -> None:
        next_open = datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc)
        with self._db() as db:
            config = self._enabled_config(db, activated_at=_SESSION_OPEN)
            service = StrategyV2ShadowService(db)
            state = db.query(StrategyV2ShadowState).filter_by(symbol="AAPL.US").one()

            service._evaluate_candles(
                config=config,
                state=state,
                market="US",
                one_minute=_candles(389, start=_SESSION_OPEN + timedelta(minutes=1)),
                observed_at=_SESSION_OPEN + timedelta(minutes=390, seconds=10),
            )

            db.refresh(state)
            assert state.last_bar_at is not None
            assert state.last_bar_at.replace(tzinfo=timezone.utc) == (
                _SESSION_OPEN + timedelta(minutes=389)
            )
            assert state.last_poll_error.startswith("DATA_SESSION_QUARANTINED:")
            quarantined = db.query(StrategyV2ShadowDecision).one()
            assert quarantined.reason == "SESSION_DATA_INCOMPLETE"
            assert "SESSION_DATA_INCOMPLETE" in json.loads(
                quarantined.gate_reasons_json
            )

            service._evaluate_candles(
                config=config,
                state=state,
                market="US",
                one_minute=_candles(390, start=next_open),
                observed_at=next_open + timedelta(minutes=390, seconds=10),
            )

            db.refresh(state)
            assert state.last_bar_at is not None
            assert state.last_bar_at.replace(tzinfo=timezone.utc) == (
                next_open + timedelta(minutes=389)
            )
            assert state.last_poll_error == ""
            daily = service._daily_evidence(
                db.query(StrategyV2ShadowDecision).order_by(
                    StrategyV2ShadowDecision.bar_at
                ).all(),
                [],
            )
            assert [item.complete_session for item in daily] == [False, True]
            assert daily[0].incomplete_feature_bars == 1
            assert daily[1].bars == 390

    def test_cross_session_gap_does_not_consume_next_opening_decision(self) -> None:
        next_open = datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc)
        previous_last_observed = _SESSION_OPEN + timedelta(minutes=388)
        previous_close_bar = _SESSION_OPEN + timedelta(minutes=389)
        with self._db() as db:
            config = self._enabled_config(db, activated_at=_SESSION_OPEN)
            service = StrategyV2ShadowService(db)
            version = service._config_version(config)
            state = db.query(StrategyV2ShadowState).filter_by(symbol="AAPL.US").one()
            state.last_bar_at = previous_last_observed
            db.add(StrategyV2ShadowDecision(
                idempotency_key="previous-session-last-observed",
                symbol="AAPL.US",
                market="US",
                config_version=version,
                session_date=_SESSION_OPEN.date(),
                bar_at=previous_last_observed,
                action="WAIT",
                reason="WARMUP",
                close_price=100.0,
                gate_reasons_json=json.dumps(["WARMUP"]),
            ))
            db.commit()

            service._evaluate_candles(
                config=config,
                state=state,
                market="US",
                one_minute=_candles(1, start=next_open),
                observed_at=next_open + timedelta(minutes=1, seconds=10),
            )

            db.refresh(state)
            quarantined_at = state.last_bar_at
            assert quarantined_at is not None
            assert quarantined_at.replace(tzinfo=timezone.utc) == previous_close_bar
            assert state.last_poll_error.startswith("DATA_SESSION_QUARANTINED:")
            assert db.query(StrategyV2ShadowDecision).count() == 1

            service._evaluate_candles(
                config=config,
                state=state,
                market="US",
                one_minute=_candles(1, start=next_open),
                observed_at=next_open + timedelta(minutes=1, seconds=10),
            )

            decisions = db.query(StrategyV2ShadowDecision).order_by(
                StrategyV2ShadowDecision.bar_at
            ).all()
            opening = decisions[-1]
            assert opening.bar_at.replace(tzinfo=timezone.utc) == next_open
            assert opening.reason != "SESSION_DATA_INCOMPLETE"
            assert "SESSION_DATA_INCOMPLETE" not in json.loads(
                opening.gate_reasons_json
            )
            db.refresh(state)
            resumed_at = state.last_bar_at
            assert resumed_at is not None
            assert resumed_at.replace(tzinfo=timezone.utc) == next_open

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
                control_db.add(control_config)
                control_db.flush()
                control_service = StrategyV2ShadowService(control_db)
                control_state = StrategyV2ShadowState(
                    symbol="AAPL.US",
                    config_version=control_service._config_version(control_config),
                    state_json="{}",
                )
                control_db.add(control_state)
                control_db.commit()
                control_service._evaluate_candles(
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
            config = self._enabled_config(
                db,
                activated_at=_SESSION_OPEN,
                establish_current_state=False,
            )
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
            config = self._enabled_config(
                db,
                activated_at=_SESSION_OPEN,
                establish_current_state=False,
            )
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

            transition_status = service.get_status("AAPL.US")
            assert transition_status.config.config_version == current_version
            assert transition_status.evidence_config_version == legacy_version
            assert transition_status.version_transition_pending is True
            assert transition_status.phase == StrategyV2State.LONG.value

            exit_bar_at = last_bar_at + timedelta(minutes=1)
            candles = [
                BrokerCandle(
                    timestamp=_SESSION_OPEN,
                    open=100,
                    high=100.2,
                    low=99.8,
                    close=100,
                    volume=1000,
                ),
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

            current_status = service.get_status("AAPL.US")
            assert current_status.evidence_config_version == current_version
            assert current_status.version_transition_pending is False
            assert current_status.phase == StrategyV2State.COLD.value

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

    def test_status_pins_latest_metrics_and_gates_to_open_trade_version(self) -> None:
        with self._db() as db:
            config = self._enabled_config(
                db,
                activated_at=_SESSION_OPEN,
                establish_current_state=False,
            )
            service = StrategyV2ShadowService(db)
            current_version = service._config_version(config)
            legacy_version = "legacy-open-trade-version"
            db.add_all([
                StrategyV2ShadowState(
                    symbol="AAPL.US",
                    config_version=legacy_version,
                    phase=StrategyV2State.LONG.value,
                    last_bar_at=_SESSION_OPEN,
                    open_trade_id=1,
                    state_json="{}",
                ),
                StrategyV2ShadowTrade(
                    id=1,
                    symbol="AAPL.US",
                    config_version=legacy_version,
                    status="OPEN",
                    entry_at=_SESSION_OPEN,
                    entry_price=100.0,
                    quantity=1.0,
                ),
                StrategyV2ShadowDecision(
                    idempotency_key="legacy-status-evidence",
                    symbol="AAPL.US",
                    market="US",
                    config_version=legacy_version,
                    session_date=_SESSION_OPEN.date(),
                    bar_at=_SESSION_OPEN,
                    close_price=101.0,
                    gate_reasons_json='["LEGACY_GATE"]',
                    virtual_position="LONG",
                ),
                StrategyV2ShadowDecision(
                    idempotency_key="current-status-evidence",
                    symbol="AAPL.US",
                    market="US",
                    config_version=current_version,
                    session_date=_SESSION_OPEN.date(),
                    bar_at=_SESSION_OPEN + timedelta(minutes=1),
                    close_price=999.0,
                    gate_reasons_json='["CURRENT_GATE"]',
                ),
                StrategyV2ShadowTrade(
                    symbol="AAPL.US",
                    config_version=current_version,
                    status="CLOSED",
                    entry_at=_SESSION_OPEN,
                    exit_at=_SESSION_OPEN + timedelta(minutes=1),
                    entry_price=100.0,
                    exit_price=223.0,
                    quantity=1.0,
                    gross_pnl=123.0,
                    estimated_fees=0.0,
                    net_pnl=123.0,
                ),
            ])
            db.commit()

            status = service.get_status("AAPL.US")

            assert status.config.config_version == current_version
            assert status.evidence_config_version == legacy_version
            assert status.version_transition_pending is True
            assert status.latest is not None
            assert status.latest.price == pytest.approx(101.0)
            assert status.metrics.bars == 1
            assert status.metrics.closed_trades == 0
            assert status.metrics.net_pnl == pytest.approx(0.0)
            assert status.gate_counts == {"LEGACY_GATE": 1}

    def test_flat_legacy_version_resets_forward_without_rewriting_evidence(self) -> None:
        observed_at = _SESSION_OPEN + timedelta(minutes=180, seconds=10)
        with self._db() as db:
            config = self._enabled_config(
                db,
                activated_at=_SESSION_OPEN,
                establish_current_state=False,
            )
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
            pending_status = service.get_status("AAPL.US")
            assert pending_status.evidence_config_version == current_version
            assert pending_status.version_transition_pending is True
            assert pending_status.phase == StrategyV2State.COLD.value
            assert pending_status.latest is None
            assert pending_status.metrics.bars == 0

            service.tick("AAPL.US", "US", now=observed_at)

            state = db.query(StrategyV2ShadowState).filter_by(symbol="AAPL.US").one()
            assert state.config_version == current_version
            assert state.last_bar_at is not None
            assert state.last_bar_at.replace(tzinfo=timezone.utc) == (
                observed_at.replace(second=0, microsecond=0)
            )
            assert db.query(StrategyV2ShadowDecision).filter_by(
                config_version=legacy_version
            ).count() == 1
            assert db.query(StrategyV2ShadowDecision).filter_by(
                config_version=current_version
            ).count() == 0

            activated_status = service.get_status("AAPL.US")
            assert activated_status.evidence_config_version == current_version
            assert activated_status.version_transition_pending is False
            assert activated_status.phase == StrategyV2State.COLD.value

            snapshot = service._ensure_version_snapshot(config)
            assert snapshot.activated_at is not None
            assert snapshot.activated_at.replace(tzinfo=timezone.utc) >= before_activation

    def test_premarket_version_transition_preserves_the_next_complete_session(
        self,
    ) -> None:
        premarket = _SESSION_OPEN - timedelta(hours=1)
        post_close = _SESSION_OPEN + timedelta(minutes=390, seconds=10)
        candles = _FakeCandles(_candles(390))
        with self._db() as db:
            config = self._enabled_config(
                db,
                activated_at=_SESSION_OPEN - timedelta(days=1),
                establish_current_state=False,
            )
            state = StrategyV2ShadowState(
                symbol="AAPL.US",
                config_version="legacy-algorithm",
                phase="FLAT",
                last_bar_at=_SESSION_OPEN - timedelta(days=1, minutes=1),
                state_json="{}",
            )
            db.add(state)
            db.commit()
            service = StrategyV2ShadowService(db, candles)
            current_version = service._config_version(config)

            service.tick("AAPL.US", "US", now=premarket)

            db.refresh(state)
            assert candles.calls == []
            assert state.config_version == current_version
            assert state.last_bar_at is not None
            assert state.last_bar_at.replace(tzinfo=timezone.utc) == premarket

            service.tick("AAPL.US", "US", now=post_close)

            decisions = db.query(StrategyV2ShadowDecision).filter_by(
                config_version=current_version
            ).order_by(StrategyV2ShadowDecision.bar_at).all()
            assert decisions[0].bar_at.replace(tzinfo=timezone.utc) == _SESSION_OPEN
            evidence = service._daily_evidence(decisions, [])
            assert len(evidence) == 1
            assert evidence[0].bars == 390
            assert evidence[0].incomplete_feature_bars == 0
            assert evidence[0].complete_session is True

    def test_adx_challengers_replay_same_complete_evidence_without_writes(
        self,
    ) -> None:
        with self._db() as db:
            service, version = self._collect_complete_session(db)
            state = db.query(StrategyV2ShadowState).filter_by(symbol="AAPL.US").one()
            before_state = (
                state.config_version,
                state.phase,
                state.last_bar_at,
                state.state_json,
            )
            before_counts = tuple(
                db.query(model).count()
                for model in (
                    StrategyV2ShadowConfig,
                    StrategyV2ShadowVersion,
                    StrategyV2ShadowState,
                    StrategyV2ShadowDecision,
                    StrategyV2ShadowTrade,
                )
            )
            expected_eligible = len({
                row.bar_at
                for row in db.query(StrategyV2ShadowDecision).filter_by(
                    symbol="AAPL.US",
                    config_version=version,
                    gate_passed=True,
                )
            })

            response = service.compare_adx_challengers(
                StrategyV2AdxChallengerRequest(
                    symbol="AAPL.US",
                    config_version=version,
                )
            )

            db.refresh(state)
            after_counts = tuple(
                db.query(model).count()
                for model in (
                    StrategyV2ShadowConfig,
                    StrategyV2ShadowVersion,
                    StrategyV2ShadowState,
                    StrategyV2ShadowDecision,
                    StrategyV2ShadowTrade,
                )
            )
            assert response.status == "INSUFFICIENT_EVIDENCE"
            assert response.observed_complete_sessions == 1
            assert response.evaluated_complete_sessions == 1
            assert response.baseline_replay_match is True
            assert response.blockers == ["MIN_COMPLETE_SESSIONS"]
            assert [item.max_adx for item in response.candidates] == [20.0, 25.0, 30.0]
            assert response.candidates[0].label == "BASELINE"
            assert response.candidates[0].config_version == version
            assert response.candidates[0].metrics.bars == 390
            assert response.candidates[0].metrics.eligible_bars == expected_eligible
            assert len(response.candidates[0].daily) == 1
            assert response.candidates[0].daily[0].bars == 390
            assert response.warmup_diagnostic is not None
            assert response.warmup_diagnostic.status == "INSUFFICIENT_EVIDENCE"
            assert response.warmup_diagnostic.evaluated_causal_pairs == 0
            assert response.warmup_diagnostic.variants == []
            assert after_counts == before_counts
            assert (
                state.config_version,
                state.phase,
                state.last_bar_at,
                state.state_json,
            ) == before_state

    def test_causal_warmup_compares_the_same_consecutive_target_session(
        self,
    ) -> None:
        with self._db() as db:
            service, version, target_open = self._collect_two_complete_sessions(db)
            state = db.query(StrategyV2ShadowState).filter_by(symbol="AAPL.US").one()
            before_state = (
                state.config_version,
                state.phase,
                state.last_bar_at,
                state.state_json,
                state.last_poll_error,
            )
            before_decisions = [
                (
                    item.id,
                    item.idempotency_key,
                    item.config_version,
                    item.bar_at,
                    item.action,
                    item.reason,
                    item.gate_passed,
                    item.gate_reasons_json,
                    item.features_json,
                )
                for item in db.query(StrategyV2ShadowDecision).order_by(
                    StrategyV2ShadowDecision.id.asc()
                )
            ]
            before_counts = tuple(
                db.query(model).count()
                for model in (
                    StrategyV2ShadowConfig,
                    StrategyV2ShadowVersion,
                    StrategyV2ShadowState,
                    StrategyV2ShadowDecision,
                    StrategyV2ShadowTrade,
                )
            )

            response = service.compare_adx_challengers(
                StrategyV2AdxChallengerRequest(
                    symbol="AAPL.US",
                    config_version=version,
                )
            )

            diagnostic = response.warmup_diagnostic
            assert diagnostic is not None
            assert diagnostic.algorithm_version == "strategy-v2-causal-trend-prewarm-v1"
            assert diagnostic.status == "INSUFFICIENT_EVIDENCE"
            assert diagnostic.minimum_causal_pairs == 5
            assert diagnostic.observed_causal_pairs == 1
            assert diagnostic.evaluated_causal_pairs == 1
            assert diagnostic.blockers == ["MIN_CAUSAL_PAIRS"]
            assert diagnostic.same_sample is True
            assert diagnostic.causal_history_only is True
            assert diagnostic.vwap_zscore_session_local is True
            assert [item.label for item in diagnostic.variants] == [
                "SESSION_LOCAL",
                "CAUSAL_TREND_PREWARM",
            ]
            baseline, prewarmed = diagnostic.variants
            assert baseline.warmup_scope == "NONE"
            assert prewarmed.warmup_scope == "ADX_VOL_ONLY"
            assert baseline.source_config_version == version
            assert prewarmed.source_config_version == version
            assert baseline.metrics.bars == 390
            assert prewarmed.metrics.bars == 390
            assert [item.session_date for item in baseline.daily] == [
                item.session_date for item in prewarmed.daily
            ]
            assert baseline.daily[0].first_ready_at == target_open + timedelta(
                minutes=139
            )
            assert baseline.daily[0].warmup_lost_bars == 139
            assert baseline.daily[0].ready_bars == 251
            assert prewarmed.daily[0].first_ready_at == target_open + timedelta(
                minutes=64
            )
            assert prewarmed.daily[0].warmup_lost_bars == 64
            assert prewarmed.daily[0].ready_bars == 326
            assert prewarmed.daily[0].seed_session_date == _SESSION_OPEN.date()
            assert prewarmed.daily[0].trend_context_cutoff_at == (
                _SESSION_OPEN + timedelta(minutes=390)
            )
            assert prewarmed.daily[0].eligible_bars <= prewarmed.daily[0].ready_bars
            assert tuple(
                db.query(model).count()
                for model in (
                    StrategyV2ShadowConfig,
                    StrategyV2ShadowVersion,
                    StrategyV2ShadowState,
                    StrategyV2ShadowDecision,
                    StrategyV2ShadowTrade,
                )
            ) == before_counts
            db.refresh(state)
            assert (
                state.config_version,
                state.phase,
                state.last_bar_at,
                state.state_json,
                state.last_poll_error,
            ) == before_state
            assert [
                (
                    item.id,
                    item.idempotency_key,
                    item.config_version,
                    item.bar_at,
                    item.action,
                    item.reason,
                    item.gate_passed,
                    item.gate_reasons_json,
                    item.features_json,
                )
                for item in db.query(StrategyV2ShadowDecision).order_by(
                    StrategyV2ShadowDecision.id.asc()
                )
            ] == before_decisions

    def test_causal_warmup_does_not_use_a_stale_complete_session(self) -> None:
        with self._db() as db:
            service, version, _target_open = self._collect_two_complete_sessions(
                db,
                target_day_offset=4,
            )

            response = service.compare_adx_challengers(
                StrategyV2AdxChallengerRequest(
                    symbol="AAPL.US",
                    config_version=version,
                )
            )

            diagnostic = response.warmup_diagnostic
            assert diagnostic is not None
            assert diagnostic.status == "INSUFFICIENT_EVIDENCE"
            assert diagnostic.observed_causal_pairs == 0
            assert diagnostic.evaluated_causal_pairs == 0
            assert diagnostic.variants == []

    def test_causal_warmup_is_ready_after_the_frozen_pair_minimum(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(shadow_service_module, "_MIN_WARMUP_CAUSAL_PAIRS", 1)
        with self._db() as db:
            service, version, _target_open = self._collect_two_complete_sessions(db)

            response = service.compare_adx_challengers(
                StrategyV2AdxChallengerRequest(
                    symbol="AAPL.US",
                    config_version=version,
                )
            )

        diagnostic = response.warmup_diagnostic
        assert diagnostic is not None
        assert diagnostic.status == "READY_FOR_REVIEW"
        assert diagnostic.minimum_causal_pairs == 1
        assert diagnostic.evaluated_causal_pairs == 1
        assert diagnostic.blockers == []
        assert len(diagnostic.variants) == 2

    def test_causal_warmup_aggregates_five_real_consecutive_pairs(self) -> None:
        session_opens = [
            _SESSION_OPEN + timedelta(days=offset)
            for offset in (0, 3, 4, 5, 6, 7)
        ]
        replay_bars = [
            StrategyV2ReplayBar(
                timestamp=item.timestamp,
                open=item.open,
                high=item.high,
                low=item.low,
                close=item.close,
                volume=item.volume,
            )
            for session_open in session_opens
            for item in _candles(390, start=session_open)
        ]
        with self._db() as db:
            config = self._enabled_config(
                db,
                activated_at=_SESSION_OPEN - timedelta(days=1),
            )
            service = StrategyV2ShadowService(db)
            version = service._config_version(config)

            diagnostic = service._warmup_diagnostic(
                replay_bars=replay_bars,
                source_config=config,
                source_config_version=version,
                market="US",
                session_dates=[item.date() for item in session_opens],
            )

        assert diagnostic.status == "READY_FOR_REVIEW"
        assert diagnostic.minimum_causal_pairs == 5
        assert diagnostic.observed_causal_pairs == 5
        assert diagnostic.evaluated_causal_pairs == 5
        assert diagnostic.blockers == []
        assert len(diagnostic.variants) == 2
        assert all(len(item.daily) == 5 for item in diagnostic.variants)
        assert all(item.metrics.bars == 5 * 390 for item in diagnostic.variants)

    def test_causal_warmup_blocks_session_local_feature_drift(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            StrategyV2ShadowService,
            "_session_local_features_match",
            classmethod(lambda cls, baseline, prewarmed: False),
        )
        with self._db() as db:
            service, version, _target_open = self._collect_two_complete_sessions(db)

            response = service.compare_adx_challengers(
                StrategyV2AdxChallengerRequest(
                    symbol="AAPL.US",
                    config_version=version,
                )
            )

        diagnostic = response.warmup_diagnostic
        assert diagnostic is not None
        assert diagnostic.status == "BLOCKED"
        assert diagnostic.evaluated_causal_pairs == 0
        assert diagnostic.blockers == ["SESSION_LOCAL_FEATURE_DRIFT"]
        assert diagnostic.variants == []

    def test_causal_warmup_fails_closed_when_prewarm_replay_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _BrokenPrewarmEngine:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                raise ValueError("synthetic prewarm failure")

        monkeypatch.setattr(
            shadow_service_module,
            "CausalTrendPrewarmFeatureEngine",
            _BrokenPrewarmEngine,
        )
        with self._db() as db:
            service, version, _target_open = self._collect_two_complete_sessions(db)

            response = service.compare_adx_challengers(
                StrategyV2AdxChallengerRequest(
                    symbol="AAPL.US",
                    config_version=version,
                )
            )

        diagnostic = response.warmup_diagnostic
        assert diagnostic is not None
        assert diagnostic.status == "BLOCKED"
        assert diagnostic.evaluated_causal_pairs == 0
        assert diagnostic.blockers == ["PREWARM_REPLAY_FAILED"]
        assert diagnostic.variants == []

    def test_replay_metrics_count_unique_gate_eligible_bars(self) -> None:
        metrics = StrategyV2ShadowService._metrics_from_replay(
            [
                {"timestamp": "2026-07-10T13:30:00+00:00", "gate_passed": True},
                {"timestamp": "2026-07-10T13:30:00+00:00", "gate_passed": True},
                {"timestamp": "2026-07-10T13:31:00+00:00", "gate_passed": False},
            ],
            [],
        )

        assert metrics.bars == 2
        assert metrics.eligible_bars == 1

    def test_domain_config_preserves_frozen_execution_parameters(self) -> None:
        with self._db() as db:
            row = StrategyV2ShadowConfig(
                symbol="AAPL.US",
                max_holding_minutes=37,
                entry_cutoff_minutes_before_close=52,
                flatten_minutes_before_close=21,
                max_entries_per_day=1,
                entry_cooldown_minutes=23,
            )
            db.add(row)
            db.flush()

            config = StrategyV2ShadowService._domain_config(row, "US")

        assert config.max_holding_minutes == 37
        assert config.entry_cutoff_minutes_before_close == 52
        assert config.flatten_minutes_before_close == 21
        assert config.max_entries_per_session == 1
        assert config.entry_cooldown_minutes == 23

    def test_adx_challenger_baseline_matches_a_complete_closed_trade(self) -> None:
        with self._db() as db:
            service, version = self._collect_complete_trade_session(db)
            persisted_trade = db.query(StrategyV2ShadowTrade).filter_by(
                symbol="AAPL.US",
                config_version=version,
                status="CLOSED",
            ).one()

            response = service.compare_adx_challengers(
                StrategyV2AdxChallengerRequest(symbol="AAPL.US")
            )

            assert response.baseline_replay_match is True
            assert response.candidates[0].metrics.closed_trades == 1
            assert response.candidates[0].metrics.net_pnl == pytest.approx(
                persisted_trade.net_pnl
            )

    def test_adx_challengers_block_mutated_raw_feature_evidence(self) -> None:
        with self._db() as db:
            service, version = self._collect_complete_session(db)
            row = db.query(StrategyV2ShadowDecision).filter_by(
                symbol="AAPL.US",
                config_version=version,
            ).order_by(StrategyV2ShadowDecision.bar_at.asc()).first()
            assert row is not None
            evidence = json.loads(row.features_json)
            evidence["bar"]["high"] = float(evidence["bar"]["high"]) + 0.01
            row.features_json = json.dumps(evidence, default=str, sort_keys=True)
            db.commit()

            response = service.compare_adx_challengers(
                StrategyV2AdxChallengerRequest(symbol="AAPL.US")
            )

            assert response.status == "BLOCKED"
            assert response.baseline_replay_match is False
            assert "BASELINE_REPLAY_MISMATCH" in response.blockers

    def test_adx_challengers_tolerate_derived_float_roundoff(self) -> None:
        with self._db() as db:
            service, version = self._collect_complete_session(db)
            rows = db.query(StrategyV2ShadowDecision).filter_by(
                symbol="AAPL.US",
                config_version=version,
            ).order_by(StrategyV2ShadowDecision.bar_at.asc()).all()
            row = next(
                item
                for item in rows
                if json.loads(item.features_json).get("adx_5m") is not None
            )
            evidence = json.loads(row.features_json)
            evidence["adx_5m"] = float(evidence["adx_5m"]) + 1e-12
            row.features_json = json.dumps(evidence, default=str, sort_keys=True)
            db.commit()

            response = service.compare_adx_challengers(
                StrategyV2AdxChallengerRequest(symbol="AAPL.US")
            )

            assert response.baseline_replay_match is True

    def test_adx_challengers_block_broken_trade_linkage(self) -> None:
        with self._db() as db:
            service, version = self._collect_complete_trade_session(db)
            trade = db.query(StrategyV2ShadowTrade).filter_by(
                symbol="AAPL.US",
                config_version=version,
                status="CLOSED",
            ).one()
            trade.entry_decision_id = None
            db.commit()

            response = service.compare_adx_challengers(
                StrategyV2AdxChallengerRequest(symbol="AAPL.US")
            )

            assert response.status == "BLOCKED"
            assert response.baseline_replay_match is False

    def test_adx_challengers_compare_persisted_trade_quantity(self) -> None:
        with self._db() as db:
            service, version = self._collect_complete_trade_session(db)
            trade = db.query(StrategyV2ShadowTrade).filter_by(
                symbol="AAPL.US",
                config_version=version,
                status="CLOSED",
            ).one()
            trade.quantity += 1
            db.commit()

            response = service.compare_adx_challengers(
                StrategyV2AdxChallengerRequest(symbol="AAPL.US")
            )

            assert response.status == "BLOCKED"
            assert response.baseline_replay_match is False

    def test_adx_challengers_require_global_trade_linkage_uniqueness(self) -> None:
        with self._db() as db:
            service, version = self._collect_complete_trade_session(db)
            trade = db.query(StrategyV2ShadowTrade).filter_by(
                symbol="AAPL.US",
                config_version=version,
                status="CLOSED",
            ).one()
            assert trade.exit_at is not None
            db.add(StrategyV2ShadowTrade(
                symbol=trade.symbol,
                config_version=trade.config_version,
                entry_decision_id=trade.entry_decision_id,
                exit_decision_id=trade.exit_decision_id,
                status="CLOSED",
                entry_at=trade.entry_at + timedelta(days=90),
                exit_at=trade.exit_at + timedelta(days=90),
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                quantity=trade.quantity,
                entry_reason=trade.entry_reason,
                exit_reason=trade.exit_reason,
            ))
            db.commit()

            response = service.compare_adx_challengers(
                StrategyV2AdxChallengerRequest(symbol="AAPL.US")
            )

            assert response.status == "BLOCKED"
            assert response.baseline_replay_match is False

    def test_adx_challengers_block_unsupported_version_without_evidence(self) -> None:
        with self._db() as db:
            legacy_version = "a" * 64
            db.add(StrategyV2ShadowConfig(symbol="AAPL.US"))
            db.add(StrategyV2ShadowVersion(
                symbol="AAPL.US",
                config_version=legacy_version,
                config_json=json.dumps({"algorithm_version": "legacy"}),
                activated_at=_SESSION_OPEN,
            ))
            db.commit()

            response = StrategyV2ShadowService(db).compare_adx_challengers(
                StrategyV2AdxChallengerRequest(
                    symbol="AAPL.US",
                    config_version=legacy_version,
                )
            )

            assert response.status == "BLOCKED"
            assert response.observed_complete_sessions == 0
            assert "ALGORITHM_VERSION_UNSUPPORTED" in response.blockers

    def test_adx_challengers_block_when_baseline_replay_does_not_match(self) -> None:
        with self._db() as db:
            service, version = self._collect_complete_session(db)
            row = db.query(StrategyV2ShadowDecision).filter_by(
                symbol="AAPL.US",
                config_version=version,
            ).order_by(StrategyV2ShadowDecision.bar_at.asc()).first()
            assert row is not None
            row.reason = "PERSISTED_DECISION_DRIFT"
            db.commit()

            response = service.compare_adx_challengers(
                StrategyV2AdxChallengerRequest(symbol="AAPL.US")
            )

            assert response.status == "BLOCKED"
            assert response.baseline_replay_match is False
            assert "BASELINE_REPLAY_MISMATCH" in response.blockers
            assert len(response.candidates) == 1
            assert response.candidates[0].label == "BASELINE"

    def test_adx_challengers_become_reviewable_at_complete_session_gate(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "app.services.strategy_v2_shadow_service."
            "_MIN_CHALLENGER_COMPLETE_SESSIONS",
            1,
        )
        with self._db() as db:
            service, _version = self._collect_complete_session(db)

            response = service.compare_adx_challengers(
                StrategyV2AdxChallengerRequest(symbol="AAPL.US")
            )

            assert response.status == "READY_FOR_REVIEW"
            assert response.minimum_complete_sessions == 1
            assert response.blockers == []
            assert response.baseline_replay_match is True

    def test_adx_challengers_reject_corrupt_persisted_bar_evidence(self) -> None:
        with self._db() as db:
            service, version = self._collect_complete_session(db)
            row = db.query(StrategyV2ShadowDecision).filter_by(
                symbol="AAPL.US",
                config_version=version,
            ).order_by(StrategyV2ShadowDecision.bar_at.asc()).first()
            assert row is not None
            row.features_json = "{}"
            db.commit()

            with pytest.raises(ValueError, match="invalid persisted shadow feature evidence"):
                service.compare_adx_challengers(
                    StrategyV2AdxChallengerRequest(symbol="AAPL.US")
                )
