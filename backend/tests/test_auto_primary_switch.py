"""Automatic primary-symbol switching — fitness-driven, default off."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_auto_switch_{os.getpid()}.db"
)

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Base,
    StrategyConfig,
    StrategyV2ShadowDecision,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
)
from app.services.auto_primary_switch_service import (
    AutoPrimarySwitchService,
    OUTCOME_DISABLED,
    OUTCOME_INCUMBENT_ACCEPTABLE,
    OUTCOME_INCUMBENT_EVIDENCE_THIN,
    OUTCOME_NO_ELIGIBLE_CANDIDATE,
    OUTCOME_NO_PRIMARY,
    OUTCOME_SWITCH_BLOCKED,
    OUTCOME_SWITCHED,
)

TREND = json.dumps(["ADX_REGIME_BLOCKED"])
CALM = json.dumps([])


class _Runner:
    def __init__(self, block: Exception | None = None) -> None:
        self.block = block
        self.calls: list[tuple[str, str]] = []

    def assert_primary_switch_safe(self, symbol: str, market: str) -> None:
        self.calls.append((symbol, market))
        if self.block is not None:
            raise self.block


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
        db.query(StrategyV2ShadowDecision).delete()
        db.query(UniverseSelectionCandidate).delete()
        db.query(UniverseSelectionRun).delete()
        db.query(StrategyConfig).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _primary(self, symbol: str) -> None:
        db = self._db()
        db.add(StrategyConfig(
            symbol=symbol,
            market="US",
            buy_low=100.0,
            sell_high=110.0,
        ))
        db.commit()
        db.close()

    def _evidence(
        self,
        symbol: str,
        *,
        trend_bars: int,
        calm_bars: int,
        close_price: float = 100.0,
    ) -> None:
        db = self._db()
        now = datetime.now(timezone.utc)
        seq = 0
        for reasons, count in ((TREND, trend_bars), (CALM, calm_bars)):
            for _ in range(count):
                seq += 1
                db.add(StrategyV2ShadowDecision(
                    idempotency_key=f"{symbol}-{seq}",
                    symbol=symbol,
                    config_version="v1",
                    session_date=now.date(),
                    bar_at=now - timedelta(minutes=seq),
                    action="WAIT",
                    gate_passed=False,
                    gate_reasons_json=reasons,
                    adx_5m=30.0,
                    close_price=close_price,
                ))
        db.commit()
        db.close()

    def _selection_run(self, selected: list[str]) -> None:
        db = self._db()
        run = UniverseSelectionRun(
            status="COMPLETE",
            algorithm_version="test",
            source_version="test",
            as_of_date=datetime.now(timezone.utc).date(),
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        for symbol in selected:
            db.add(UniverseSelectionCandidate(
                run_id=run.id,
                symbol=symbol,
                market="US",
                selected=True,
            ))
        db.commit()
        db.close()


@pytest.fixture(autouse=True)
def _enable_switch(monkeypatch):
    monkeypatch.setattr(settings, "auto_primary_switch_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auto_primary_switch_lookback_days", 3, raising=False)
    monkeypatch.setattr(settings, "auto_primary_switch_min_samples", 60, raising=False)
    monkeypatch.setattr(
        settings, "auto_primary_switch_incumbent_trend_pct", 60.0, raising=False
    )
    monkeypatch.setattr(
        settings, "auto_primary_switch_candidate_trend_pct", 30.0, raising=False
    )
    yield


class TestAutoPrimarySwitch(_Base):
    def test_disabled_switch_is_a_noop(self, monkeypatch) -> None:
        monkeypatch.setattr(
            settings, "auto_primary_switch_enabled", False, raising=False
        )
        runner = _Runner()
        result = AutoPrimarySwitchService(self._db()).evaluate(runner)
        assert result.outcome == OUTCOME_DISABLED
        assert runner.calls == []

    def test_requires_a_configured_primary(self) -> None:
        result = AutoPrimarySwitchService(self._db()).evaluate(_Runner())
        assert result.outcome == OUTCOME_NO_PRIMARY

    def test_keeps_incumbent_while_it_is_range_like(self) -> None:
        self._primary("NVDA.US")
        self._evidence("NVDA.US", trend_bars=10, calm_bars=70)
        self._selection_run(["AAPL.US"])
        self._evidence("AAPL.US", trend_bars=0, calm_bars=80)

        runner = _Runner()
        result = AutoPrimarySwitchService(self._db()).evaluate(runner)
        assert result.outcome == OUTCOME_INCUMBENT_ACCEPTABLE
        assert runner.calls == []

    def test_thin_incumbent_evidence_never_switches(self) -> None:
        self._primary("NVDA.US")
        self._evidence("NVDA.US", trend_bars=5, calm_bars=0)
        self._selection_run(["AAPL.US"])
        self._evidence("AAPL.US", trend_bars=0, calm_bars=80)

        runner = _Runner()
        result = AutoPrimarySwitchService(self._db()).evaluate(runner)
        assert result.outcome == OUTCOME_INCUMBENT_EVIDENCE_THIN
        assert runner.calls == []

    def test_switches_to_best_selected_candidate(self) -> None:
        self._primary("NVDA.US")
        self._evidence("NVDA.US", trend_bars=70, calm_bars=10)
        self._selection_run(["AAPL.US", "CSCO.US"])
        self._evidence("AAPL.US", trend_bars=8, calm_bars=72)
        self._evidence("CSCO.US", trend_bars=0, calm_bars=80)

        runner = _Runner()
        result = AutoPrimarySwitchService(self._db()).evaluate(runner)

        assert result.outcome == OUTCOME_SWITCHED
        assert result.incumbent == "NVDA.US"
        assert result.candidate == "CSCO.US"
        assert runner.calls == [("CSCO.US", "US")]

        db = self._db()
        try:
            config = db.query(StrategyConfig).order_by(
                StrategyConfig.id.desc()
            ).first()
            assert config is not None
            assert config.symbol == "CSCO.US"
        finally:
            db.close()

    def test_ignores_candidates_outside_the_selection_run(self) -> None:
        self._primary("NVDA.US")
        self._evidence("NVDA.US", trend_bars=70, calm_bars=10)
        self._selection_run(["AAPL.US"])
        self._evidence("TSLA.US", trend_bars=0, calm_bars=80)

        runner = _Runner()
        result = AutoPrimarySwitchService(self._db()).evaluate(runner)
        assert result.outcome == OUTCOME_NO_ELIGIBLE_CANDIDATE
        assert runner.calls == []

    def test_rejects_candidate_above_the_trend_ceiling(self) -> None:
        self._primary("NVDA.US")
        self._evidence("NVDA.US", trend_bars=70, calm_bars=10)
        self._selection_run(["AAPL.US"])
        self._evidence("AAPL.US", trend_bars=40, calm_bars=40)

        result = AutoPrimarySwitchService(self._db()).evaluate(_Runner())
        assert result.outcome == OUTCOME_NO_ELIGIBLE_CANDIDATE

    def test_safety_gate_veto_prevents_the_switch(self) -> None:
        self._primary("NVDA.US")
        self._evidence("NVDA.US", trend_bars=70, calm_bars=10)
        self._selection_run(["AAPL.US"])
        self._evidence("AAPL.US", trend_bars=0, calm_bars=80)

        runner = _Runner(block=RuntimeError("positions are tracked"))
        result = AutoPrimarySwitchService(self._db()).evaluate(runner)

        assert result.outcome == OUTCOME_SWITCH_BLOCKED
        assert "positions are tracked" in result.detail
        db = self._db()
        try:
            config = db.query(StrategyConfig).order_by(
                StrategyConfig.id.desc()
            ).first()
            assert config is not None
            assert config.symbol == "NVDA.US"
        finally:
            db.close()

    def test_never_switches_to_the_incumbent_itself(self) -> None:
        self._primary("NVDA.US")
        self._evidence("NVDA.US", trend_bars=70, calm_bars=10)
        self._selection_run(["NVDA.US"])

        result = AutoPrimarySwitchService(self._db()).evaluate(_Runner())
        assert result.outcome == OUTCOME_NO_ELIGIBLE_CANDIDATE

    def test_resets_interval_around_the_candidate_price(self) -> None:
        self._primary("NVDA.US")
        self._evidence("NVDA.US", trend_bars=70, calm_bars=10, close_price=210.0)
        self._selection_run(["CSCO.US"])
        self._evidence("CSCO.US", trend_bars=0, calm_bars=80, close_price=111.0)

        result = AutoPrimarySwitchService(self._db()).evaluate(_Runner())
        assert result.outcome == OUTCOME_SWITCHED

        db = self._db()
        try:
            config = db.query(StrategyConfig).order_by(
                StrategyConfig.id.desc()
            ).first()
            assert config is not None
            assert config.symbol == "CSCO.US"
            # The old NVDA interval would sit ~90 above CSCO's price and could
            # never trigger; the interval must follow the new symbol.
            assert config.buy_low < 111.0 < config.sell_high
        finally:
            db.close()

    def test_skips_candidate_without_a_reference_price(self) -> None:
        self._primary("NVDA.US")
        self._evidence("NVDA.US", trend_bars=70, calm_bars=10)
        self._selection_run(["CSCO.US"])
        self._evidence("CSCO.US", trend_bars=0, calm_bars=80, close_price=0.0)

        result = AutoPrimarySwitchService(self._db()).evaluate(_Runner())
        assert result.outcome == OUTCOME_NO_ELIGIBLE_CANDIDATE


class TestAutoPrimarySwitchConfigGuards:
    def test_candidate_ceiling_must_be_below_incumbent_threshold(
        self, monkeypatch
    ) -> None:
        from app.config import Settings

        monkeypatch.setenv("AUTO_TRADE_AUTO_PRIMARY_SWITCH_ENABLED", "true")
        monkeypatch.setenv("AUTO_TRADE_UNIVERSE_SELECTION_ENABLED", "true")
        monkeypatch.setenv(
            "AUTO_TRADE_UNIVERSE_SELECTION_APPLY_TO_WATCHLIST", "true"
        )
        monkeypatch.setenv("AUTO_TRADE_UNIVERSE_SELECTION_ENABLE_SHADOW", "true")
        monkeypatch.setenv(
            "AUTO_TRADE_AUTO_PRIMARY_SWITCH_INCUMBENT_TREND_PCT", "30"
        )
        monkeypatch.setenv(
            "AUTO_TRADE_AUTO_PRIMARY_SWITCH_CANDIDATE_TREND_PCT", "60"
        )
        try:
            Settings()
            raise AssertionError("inverted thresholds must be rejected")
        except ValueError:
            pass

    def test_requires_universe_selection(self, monkeypatch) -> None:
        from app.config import Settings

        monkeypatch.setenv("AUTO_TRADE_AUTO_PRIMARY_SWITCH_ENABLED", "true")
        monkeypatch.setenv("AUTO_TRADE_UNIVERSE_SELECTION_ENABLED", "false")
        try:
            Settings()
            raise AssertionError("missing universe selection must be rejected")
        except ValueError:
            pass
