"""Signal edge service — evidence assembly over shadow trades."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_signal_edge_{os.getpid()}.db"
)

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.domain.strategy_v2.signal_edge import (
    VERDICT_FAIL,
    VERDICT_INSUFFICIENT_DATA,
)
from app.models import Base, StrategyV2ShadowConfig, StrategyV2ShadowTrade
from app.services.signal_edge_service import SignalEdgeService


class _Base:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            os.environ["AUTO_TRADE_DATABASE_URL"],
            connect_args={"check_same_thread": False},
        )
        Base.metadata.drop_all(bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)

    def setup_method(self) -> None:
        db = Session(bind=self.engine)
        db.query(StrategyV2ShadowTrade).delete()
        db.query(StrategyV2ShadowConfig).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _config(self, symbol: str, *, stop: float, target: float) -> None:
        db = self._db()
        db.add(StrategyV2ShadowConfig(
            symbol=symbol,
            enabled=True,
            stop_loss_pct=stop,
            profit_target_pct=target,
        ))
        db.commit()
        db.close()

    def _trade(
        self,
        symbol: str,
        *,
        exit_reason: str,
        net_pnl: float,
        age_days: float = 1.0,
        day_offset: int = 0,
        status: str = "CLOSED",
    ) -> None:
        db = self._db()
        exit_at = datetime.now(timezone.utc) - timedelta(days=age_days + day_offset)
        db.add(StrategyV2ShadowTrade(
            symbol=symbol,
            config_version="v1",
            status=status,
            entry_at=exit_at - timedelta(minutes=30),
            exit_at=None if status == "OPEN" else exit_at,
            entry_price=100.0,
            quantity=1.0,
            net_pnl=net_pnl,
            exit_reason=exit_reason,
        ))
        db.commit()
        db.close()


class TestSignalEdgeService(_Base):
    def test_reproduces_the_live_negative_finding(self) -> None:
        """38 targets against 83 stops must not clear the gate."""
        self._config("NVDA.US", stop=0.45, target=0.80)
        for i in range(38):
            self._trade("NVDA.US", exit_reason="PROFIT_TARGET",
                        net_pnl=0.8, day_offset=i % 25)
        for i in range(83):
            self._trade("NVDA.US", exit_reason="PRICE_STOP",
                        net_pnl=-0.45, day_offset=i % 25)

        verdict, stop, target, symbol = SignalEdgeService(self._db()).assess(
            symbol="NVDA.US", min_distinct_days=20
        )
        assert (stop, target, symbol) == (0.45, 0.80, "NVDA.US")
        assert verdict.verdict == VERDICT_FAIL
        assert verdict.first_passage.resolved == 121
        assert verdict.first_passage.beats_baseline is False

    def test_barriers_come_from_the_config_the_trades_ran_under(self) -> None:
        """Judging outcomes against barriers they never used compares unlike things."""
        self._config("APP.US", stop=0.60, target=1.20)
        self._trade("APP.US", exit_reason="PROFIT_TARGET", net_pnl=1.2)

        _, stop, target, _ = SignalEdgeService(self._db()).assess(symbol="APP.US")
        assert (stop, target) == (0.60, 1.20)

    def test_explicit_barriers_override_the_config(self) -> None:
        self._config("APP.US", stop=0.60, target=1.20)
        self._trade("APP.US", exit_reason="PROFIT_TARGET", net_pnl=1.2)

        _, stop, target, _ = SignalEdgeService(self._db()).assess(
            symbol="APP.US", stop_pct=0.30, target_pct=0.90
        )
        assert (stop, target) == (0.30, 0.90)

    def test_open_trades_are_excluded(self) -> None:
        self._config("TER.US", stop=0.45, target=0.80)
        self._trade("TER.US", exit_reason="PROFIT_TARGET", net_pnl=0.8)
        self._trade("TER.US", exit_reason="", net_pnl=0.0, status="OPEN")

        verdict, _, _, _ = SignalEdgeService(self._db()).assess(symbol="TER.US")
        assert verdict.clustered.observations == 1

    def test_trades_outside_the_window_are_ignored(self) -> None:
        self._config("MU.US", stop=0.45, target=0.80)
        self._trade("MU.US", exit_reason="PROFIT_TARGET", net_pnl=0.8, age_days=200)

        verdict, _, _, _ = SignalEdgeService(self._db()).assess(
            symbol="MU.US", lookback_days=30
        )
        assert verdict.first_passage.resolved == 0
        assert verdict.verdict == VERDICT_INSUFFICIENT_DATA

    def test_max_hold_exits_inform_significance_but_not_first_passage(self) -> None:
        """A trade that touched neither barrier says nothing about first passage."""
        self._config("CAT.US", stop=0.45, target=0.80)
        for i in range(10):
            self._trade("CAT.US", exit_reason="MAX_HOLD",
                        net_pnl=-0.05, day_offset=i)

        verdict, _, _, _ = SignalEdgeService(self._db()).assess(symbol="CAT.US")
        assert verdict.first_passage.resolved == 0
        assert verdict.clustered.observations == 10

    def test_missing_config_without_explicit_barriers_is_rejected(self) -> None:
        self._trade("GS.US", exit_reason="PROFIT_TARGET", net_pnl=0.8)
        try:
            SignalEdgeService(self._db()).assess(symbol="GS.US")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    def test_rejects_non_positive_lookback(self) -> None:
        try:
            SignalEdgeService(self._db()).assess(lookback_days=0)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    def test_does_not_mutate_evidence(self) -> None:
        self._config("NVDA.US", stop=0.45, target=0.80)
        for i in range(5):
            self._trade("NVDA.US", exit_reason="PROFIT_TARGET",
                        net_pnl=0.8, day_offset=i)
        SignalEdgeService(self._db()).assess(symbol="NVDA.US")

        db = self._db()
        try:
            assert db.query(StrategyV2ShadowTrade).count() == 5
        finally:
            db.close()
