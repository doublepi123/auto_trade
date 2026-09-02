"""Automatic primary-symbol switching — fitness-driven, default off."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_auto_switch_{os.getpid()}.db"
)

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Base,
    StrategyConfig,
    StrategyV2ShadowConfig,
    StrategyV2ShadowDecision,
    StrategyV2ShadowTrade,
    StrategyV2ShadowVersion,
    TradeEvent,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
)
from app.services.auto_primary_switch_service import (
    AutoPrimarySwitchService,
    AUDIT_PRIMARY_SWITCH,
    EVENT_PRIMARY_SWITCH_BLOCKED,
    EVENT_PRIMARY_SWITCH_ROLLED_BACK,
    EVENT_PRIMARY_SWITCHED,
    OUTCOME_DISABLED,
    OUTCOME_INCUMBENT_ACCEPTABLE,
    OUTCOME_INCUMBENT_EVIDENCE_THIN,
    OUTCOME_NO_ELIGIBLE_CANDIDATE,
    OUTCOME_NO_PRIMARY,
    OUTCOME_SIGNAL_EDGE_UNPROVEN,
    OUTCOME_SWITCH_BLOCKED,
    OUTCOME_SWITCHED,
)
from app.services.range_fitness_service import RangeFitnessService
from app.services.strategy_service import StrategyService
from app.services.trade_event_service import decode_event_payload

TREND = json.dumps(["ADX_REGIME_BLOCKED"])
CALM = json.dumps([])


class _Clock:
    """A hand-advanced clock, so a test can age a reference mid-evaluation."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class _Runner:
    def __init__(
        self,
        block: Exception | None = None,
        reload_error: Exception | None = None,
    ) -> None:
        self.block = block
        self.reload_error = reload_error
        self.calls: list[tuple[str, str]] = []
        self.reloads = 0
        self.clock: _Clock | None = None
        self.safety_advance_seconds = 0.0
        self.session: Session | None = None
        self.symbol_probe: Session | None = None
        self.reload_symbols: list[str] = []
        self.trace: list[str] | None = None
        self.in_transaction_at_reload: list[bool] = []

    def assert_primary_switch_safe(self, symbol: str, market: str) -> None:
        self.calls.append((symbol, market))
        # Stands in for runner.py's blocking broker position probe, which can
        # take seconds while the candidate's reference price keeps ageing.
        if self.clock is not None and self.safety_advance_seconds:
            self.clock.advance(self.safety_advance_seconds)
        if self.block is not None:
            raise self.block

    def reload_strategy(self) -> None:
        self.reloads += 1
        if self.trace is not None:
            self.trace.append("reload")
        if self.session is not None:
            self.in_transaction_at_reload.append(self.session.in_transaction())
        if self.symbol_probe is not None:
            row = self.symbol_probe.query(StrategyConfig).order_by(
                StrategyConfig.id.desc()
            ).first()
            self.reload_symbols.append((row.symbol or "") if row else "")
        # Only the first reload fails, so the rollback reload can still land.
        if self.reload_error is not None and self.reloads == 1:
            raise self.reload_error


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
        db.query(StrategyV2ShadowConfig).delete()
        db.query(StrategyV2ShadowDecision).delete()
        db.query(StrategyV2ShadowTrade).delete()
        db.query(StrategyV2ShadowVersion).delete()
        db.query(UniverseSelectionCandidate).delete()
        db.query(UniverseSelectionRun).delete()
        db.query(StrategyConfig).delete()
        db.query(TradeEvent).delete()
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
        anchor: datetime | None = None,
    ) -> None:
        db = self._db()
        now = anchor or datetime.now(timezone.utc)
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

    def _reach(
        self,
        symbol: str,
        *,
        closed: int,
        reached: int,
    ) -> None:
        """Seed closed shadow trades so the reach-rate gate has evidence.

        The gate rejects candidates without measured reach evidence, so any test
        that expects a switch must supply it explicitly.
        """
        db = self._db()
        now = datetime.now(timezone.utc)
        for i in range(closed):
            db.add(StrategyV2ShadowTrade(
                symbol=symbol,
                config_version="v1",
                status="CLOSED",
                entry_at=now - timedelta(minutes=30 + i),
                exit_at=now - timedelta(minutes=i),
                entry_price=100.0,
                quantity=1.0,
                mfe_pct=0.009 if i < reached else 0.0005,
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
def enable_switch(monkeypatch):
    monkeypatch.setattr(settings, "auto_primary_switch_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auto_primary_switch_lookback_days", 3, raising=False)
    monkeypatch.setattr(settings, "auto_primary_switch_min_samples", 60, raising=False)
    monkeypatch.setattr(
        settings, "auto_primary_switch_incumbent_trend_pct", 60.0, raising=False
    )
    monkeypatch.setattr(
        settings, "auto_primary_switch_candidate_trend_pct", 30.0, raising=False
    )
    # Off by default so each test exercises one gate. TestSignalEdgeGate turns
    # it back on; without this every fixture would also need edge evidence.
    monkeypatch.setattr(
        settings, "auto_primary_switch_require_signal_edge", False, raising=False
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
        self._reach("AAPL.US", closed=6, reached=5)
        self._reach("CSCO.US", closed=6, reached=5)
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
        self._reach("AAPL.US", closed=6, reached=5)

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
        self._reach("CSCO.US", closed=6, reached=5)

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

    def test_blocks_candidate_with_stale_reference_price(self) -> None:
        # Given: a candidate that clears every other gate, but whose freshest
        # close is a day old.
        now = datetime.now(timezone.utc)
        self._primary("NVDA.US")
        self._evidence("NVDA.US", trend_bars=70, calm_bars=10, anchor=now)
        self._selection_run(["CSCO.US"])
        self._evidence(
            "CSCO.US",
            trend_bars=0,
            calm_bars=80,
            close_price=111.0,
            anchor=now - timedelta(days=1),
        )
        self._reach("CSCO.US", closed=6, reached=5)

        # When: the switch is evaluated against an explicit anchor.
        runner = _Runner()
        result = AutoPrimarySwitchService(self._db()).evaluate(runner, now=now)

        # Then: no switch, and the live symbol is untouched. A band centred on a
        # day-old close sits where the market no longer trades, so it would take
        # no fills at all.
        assert result.outcome == OUTCOME_NO_ELIGIBLE_CANDIDATE
        assert runner.calls == []
        db = self._db()
        try:
            config = db.query(StrategyConfig).order_by(
                StrategyConfig.id.desc()
            ).first()
            assert config is not None
            assert config.symbol == "NVDA.US"
        finally:
            db.close()

    def test_switches_with_fresh_reference_price(self) -> None:
        # Given: the same candidate, with its freshest close minutes old.
        now = datetime.now(timezone.utc)
        self._primary("NVDA.US")
        self._evidence(
            "NVDA.US", trend_bars=70, calm_bars=10, close_price=210.0, anchor=now
        )
        self._selection_run(["CSCO.US"])
        self._evidence(
            "CSCO.US", trend_bars=0, calm_bars=80, close_price=111.0, anchor=now
        )
        self._reach("CSCO.US", closed=6, reached=5)

        # When: the switch is evaluated against an explicit anchor.
        runner = _Runner()
        result = AutoPrimarySwitchService(self._db()).evaluate(runner, now=now)

        # Then: the switch proceeds and the band brackets the candidate price.
        assert result.outcome == OUTCOME_SWITCHED
        assert result.candidate == "CSCO.US"
        assert runner.calls == [("CSCO.US", "US")]
        db = self._db()
        try:
            config = db.query(StrategyConfig).order_by(
                StrategyConfig.id.desc()
            ).first()
            assert config is not None
            assert config.symbol == "CSCO.US"
            assert config.buy_low < 111.0 < config.sell_high
        finally:
            db.close()

    def test_missing_last_bar_at_is_treated_as_stale(self, monkeypatch) -> None:
        # Given: a fresh, otherwise eligible candidate whose row reports no bar
        # timestamp — an evidence gap, not a proof of freshness.
        now = datetime.now(timezone.utc)
        self._primary("NVDA.US")
        self._evidence("NVDA.US", trend_bars=70, calm_bars=10, anchor=now)
        self._selection_run(["CSCO.US"])
        self._evidence(
            "CSCO.US", trend_bars=0, calm_bars=80, close_price=111.0, anchor=now
        )
        self._reach("CSCO.US", closed=6, reached=5)
        assessed = RangeFitnessService.assess
        monkeypatch.setattr(
            RangeFitnessService,
            "assess",
            lambda svc, **kwargs: [
                replace(row, last_bar_at=None) for row in assessed(svc, **kwargs)
            ],
        )

        # When: the switch is evaluated against an explicit anchor.
        runner = _Runner()
        result = AutoPrimarySwitchService(self._db()).evaluate(runner, now=now)

        # Then: fail closed — an unmeasurable age is never treated as fresh.
        assert result.outcome == OUTCOME_NO_ELIGIBLE_CANDIDATE
        assert runner.calls == []


class TestAutoPrimarySwitchProvenance(_Base):
    """A switch must leave a durable record of why the live symbol changed.

    Production changed its primary symbol with zero matching rows in
    ``trade_events`` and zero in ``audit_logs``, so an operator could not answer
    "why is the system trading a different symbol than yesterday?" from any
    durable record -- for the single most consequential automatic action in the
    system.
    """

    REFERENCE_PRICE = 111.0

    def _switch_scenario(self, now: datetime) -> _Runner:
        self._primary("NVDA.US")
        self._evidence(
            "NVDA.US", trend_bars=70, calm_bars=10, close_price=210.0, anchor=now
        )
        self._selection_run(["CSCO.US"])
        self._evidence(
            "CSCO.US",
            trend_bars=0,
            calm_bars=80,
            close_price=self.REFERENCE_PRICE,
            anchor=now,
        )
        self._reach("CSCO.US", closed=6, reached=5)
        return _Runner()

    def _events(self, event_type: str) -> list[dict[str, Any]]:
        db = self._db()
        try:
            rows = db.query(TradeEvent).filter(
                TradeEvent.event_type == event_type
            ).order_by(TradeEvent.id).all()
            return [
                {
                    "id": row.id,
                    "event_type": row.event_type,
                    "symbol": row.symbol,
                    "status": row.status,
                    "message": row.message,
                    "payload": decode_event_payload(row.payload_json),
                }
                for row in rows
            ]
        finally:
            db.close()

    def _config(self) -> StrategyConfig:
        db = self._db()
        try:
            config = db.query(StrategyConfig).order_by(
                StrategyConfig.id.desc()
            ).first()
            assert config is not None
            db.expunge(config)
            return config
        finally:
            db.close()

    def test_switch_persists_primary_switch_trade_event(self) -> None:
        # Given: a candidate that clears every gate.
        now = datetime.now(timezone.utc)
        runner = self._switch_scenario(now)

        # When: the switch is applied.
        result = AutoPrimarySwitchService(self._db()).evaluate(runner, now=now)

        # Then: exactly one durable row carries the full provenance.
        assert result.outcome == OUTCOME_SWITCHED
        rows = self._events(EVENT_PRIMARY_SWITCHED)
        assert len(rows) == 1
        row = rows[0]
        print("\nPERSISTED PRIMARY_SWITCHED ROW:")
        print(json.dumps(row, indent=2, ensure_ascii=False, default=str))
        payload = row["payload"]
        assert row["symbol"] == "CSCO.US"
        assert payload["previous_symbol"] == "NVDA.US"
        assert payload["new_symbol"] == "CSCO.US"
        assert payload["previous_buy_low"] == 100.0
        assert payload["previous_sell_high"] == 110.0
        assert (
            payload["new_buy_low"]
            < self.REFERENCE_PRICE
            < payload["new_sell_high"]
        )
        assert payload["reference_price"] == self.REFERENCE_PRICE
        assert payload["reference_bar_at"]
        assert payload["incumbent_trend_pct"] > 60.0
        assert payload["candidate_trend_pct"] == 0.0

    def test_switch_event_is_committed_atomically_with_config(self) -> None:
        """The event must ride the config change's own commit.

        ``StrategyService.update_config`` commits the session itself, so an
        event staged after that call leaves a crash window in which the live
        symbol has changed with no record of why -- the production defect,
        merely narrowed.
        """
        # Given: a switch about to be applied, with every statement recorded.
        now = datetime.now(timezone.utc)
        runner = self._switch_scenario(now)
        steps: list[str] = []

        def _record_dml(
            _connection,
            _cursor,
            statement: str,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            normalized = " ".join(statement.upper().split())
            if normalized.startswith("INSERT INTO TRADE_EVENTS"):
                steps.append("event_insert")
            elif normalized.startswith(
                ("UPDATE STRATEGY_CONFIG", "INSERT INTO STRATEGY_CONFIG")
            ):
                steps.append("config_write")

        def _record_commit(_connection) -> None:
            steps.append("COMMIT")

        # When: the switch runs under those listeners.
        event.listen(self.engine, "before_cursor_execute", _record_dml)
        event.listen(self.engine, "commit", _record_commit)
        try:
            result = AutoPrimarySwitchService(self._db()).evaluate(
                runner, now=now
            )
        finally:
            event.remove(self.engine, "before_cursor_execute", _record_dml)
            event.remove(self.engine, "commit", _record_commit)

        # Then: both writes land inside one transaction.
        assert result.outcome == OUTCOME_SWITCHED
        assert steps.count("event_insert") == 1
        assert "config_write" in steps
        event_at = steps.index("event_insert")
        config_at = steps.index("config_write")
        assert (
            "COMMIT"
            not in steps[min(event_at, config_at):max(event_at, config_at)]
        )
        assert event_at < steps.index("COMMIT", config_at)

    def test_rollback_leaves_no_orphan_switch_event(self) -> None:
        # Given: a switch whose runtime reload will fail.
        now = datetime.now(timezone.utc)
        runner = self._switch_scenario(now)
        runner.reload_error = RuntimeError("engine refused the new symbol")

        # When: the switch is attempted.
        result = AutoPrimarySwitchService(self._db()).evaluate(runner, now=now)

        # Then: the config is restored and no row claims the switch stands.
        assert result.outcome == OUTCOME_SWITCH_BLOCKED
        assert "persist failed" in result.detail
        config = self._config()
        assert config.symbol == "NVDA.US"
        assert config.buy_low == 100.0
        assert config.sell_high == 110.0
        assert self._events(EVENT_PRIMARY_SWITCHED) == []
        rolled = self._events(EVENT_PRIMARY_SWITCH_ROLLED_BACK)
        assert len(rolled) == 1
        payload = rolled[0]["payload"]
        assert payload["previous_symbol"] == "NVDA.US"
        assert payload["new_symbol"] == "CSCO.US"
        assert "engine refused the new symbol" in payload["rollback_reason"]

    def test_switch_writes_audit_log(self, monkeypatch) -> None:
        # Given: a recording audit logger in place of the shared singleton.
        from app.api import deps

        recorded: list[tuple[str, dict[str, Any]]] = []

        class _RecordingAudit:
            @staticmethod
            def record(action: str, **kwargs: Any) -> None:
                recorded.append((action, kwargs))

        monkeypatch.setattr(deps, "init_audit_logger", _RecordingAudit)
        now = datetime.now(timezone.utc)
        runner = self._switch_scenario(now)

        # When: the switch is applied.
        result = AutoPrimarySwitchService(self._db()).evaluate(runner, now=now)

        # Then: the switch is mirrored into the audit trail.
        assert result.outcome == OUTCOME_SWITCHED
        assert [action for action, _ in recorded] == [AUDIT_PRIMARY_SWITCH]
        summary = recorded[0][1]["request_summary"]
        assert summary["previous_symbol"] == "NVDA.US"
        assert summary["new_symbol"] == "CSCO.US"

    def test_audit_failure_does_not_abort_switch(self, monkeypatch) -> None:
        # Given: an audit backend that raises on every attempt.
        from app.api import deps

        def _explode() -> None:
            raise RuntimeError("audit backend is down")

        monkeypatch.setattr(deps, "init_audit_logger", _explode)
        now = datetime.now(timezone.utc)
        runner = self._switch_scenario(now)

        # When: the switch is applied.
        result = AutoPrimarySwitchService(self._db()).evaluate(runner, now=now)

        # Then: provenance never vetoes the operation it records.
        assert result.outcome == OUTCOME_SWITCHED
        assert self._config().symbol == "CSCO.US"
        assert len(self._events(EVENT_PRIMARY_SWITCHED)) == 1

    def test_blocked_switch_is_observable(self) -> None:
        # Given: a fully qualified candidate the safety gate will refuse.
        now = datetime.now(timezone.utc)
        runner = self._switch_scenario(now)
        runner.block = RuntimeError("positions are tracked")

        # When: the switch is attempted.
        result = AutoPrimarySwitchService(self._db()).evaluate(runner, now=now)

        # Then: the refusal is durable, and nothing claims a switch happened.
        assert result.outcome == OUTCOME_SWITCH_BLOCKED
        blocked = self._events(EVENT_PRIMARY_SWITCH_BLOCKED)
        assert len(blocked) == 1
        payload = blocked[0]["payload"]
        assert payload["previous_symbol"] == "NVDA.US"
        assert payload["candidate_symbol"] == "CSCO.US"
        assert "positions are tracked" in payload["reason"]
        assert self._events(EVENT_PRIMARY_SWITCHED) == []
        assert self._config().symbol == "NVDA.US"


    def test_reference_going_stale_during_the_safety_check_blocks(
        self, monkeypatch
    ) -> None:
        """The early gate is measured before a probe that can take seconds.

        ``assert_primary_switch_safe`` performs a blocking broker position
        probe, so a reference that passed the early gate can age past the bound
        before the band built from it is ever persisted.
        """
        # Given: a candidate fresh at evaluation start, and a safety probe that
        # blocks long enough for its reference to age out.
        monkeypatch.setattr(
            settings,
            "auto_primary_switch_max_price_age_seconds",
            120,
            raising=False,
        )
        start = datetime.now(timezone.utc)
        clock = _Clock(start)
        runner = self._switch_scenario(start)
        runner.clock = clock
        runner.safety_advance_seconds = 300.0

        # When: the switch is evaluated against that clock.
        result = AutoPrimarySwitchService(self._db(), clock=clock).evaluate(runner)

        # Then: the early gate passed, but no band is built from a reference
        # that is stale at the moment of the write.
        assert runner.calls == [("CSCO.US", "US")]
        assert result.outcome == OUTCOME_SWITCH_BLOCKED
        config = self._config()
        assert config.symbol == "NVDA.US"
        assert config.buy_low == 100.0
        assert config.sell_high == 110.0
        assert self._events(EVENT_PRIMARY_SWITCHED) == []

    def test_future_dated_reference_is_rejected(self) -> None:
        # Given: a candidate whose freshest close is timestamped in the future.
        now = datetime.now(timezone.utc)
        self._primary("NVDA.US")
        self._evidence(
            "NVDA.US", trend_bars=70, calm_bars=10, close_price=210.0, anchor=now
        )
        self._selection_run(["CSCO.US"])
        self._evidence(
            "CSCO.US",
            trend_bars=0,
            calm_bars=80,
            close_price=self.REFERENCE_PRICE,
            anchor=now + timedelta(minutes=5),
        )
        self._reach("CSCO.US", closed=6, reached=5)

        # When: the switch is evaluated.
        runner = _Runner()
        result = AutoPrimarySwitchService(self._db()).evaluate(runner, now=now)

        # Then: a negative age is invalid rather than fresh -- fail closed.
        assert result.outcome == OUTCOME_NO_ELIGIBLE_CANDIDATE
        assert runner.calls == []
        assert self._config().symbol == "NVDA.US"

    def test_config_write_that_landed_before_raising_still_reloads(
        self, monkeypatch
    ) -> None:
        """``update_config`` commits and only THEN refreshes.

        A raise from the post-commit refresh must not be read as a rejected
        write: the config and the event are already durable, so reporting a
        clean block would strand the live engine on the old symbol while the
        database claims the new one.
        """
        # Given: a config write whose post-commit refresh raises once.
        now = datetime.now(timezone.utc)
        runner = self._switch_scenario(now)
        original_refresh = Session.refresh
        refresh_failures: list[bool] = []

        def _refresh(
            session: Session,
            instance: Any,
            *args: Any,
            **kwargs: Any,
        ) -> None:
            if isinstance(instance, StrategyConfig) and not refresh_failures:
                refresh_failures.append(True)
                raise RuntimeError("post-commit refresh failed")
            original_refresh(session, instance, *args, **kwargs)

        monkeypatch.setattr(Session, "refresh", _refresh)

        # When: the switch is applied.
        result = AutoPrimarySwitchService(self._db()).evaluate(runner, now=now)

        # Then: a durable switch is never left with the runner unreloaded.
        assert refresh_failures == [True]
        config = self._config()
        assert not (config.symbol == "CSCO.US" and runner.reloads == 0), (
            "committed switch stranded: config changed, runner never reloaded"
        )
        assert runner.reloads >= 1
        assert result.outcome == OUTCOME_SWITCHED
        assert config.symbol == "CSCO.US"
        assert len(self._events(EVENT_PRIMARY_SWITCHED)) == 1


    def test_reference_going_stale_during_the_config_write_is_rolled_back(
        self, monkeypatch
    ) -> None:
        """``update_config`` takes the SQLite writer lock BEFORE it commits.

        Lock waits are unbounded under contention, so no check that merely
        precedes the call can bound the age at commit. The age is re-read once
        the commit has returned: it only ever grows, so an age still inside the
        bound there proves the commit was inside it too, and an age outside it
        proves nothing -- and an unproven band must not survive the call.
        """
        # Given: a candidate fresh at the gate, whose config write blocks past
        # the bound before committing.
        monkeypatch.setattr(
            settings,
            "auto_primary_switch_max_price_age_seconds",
            120,
            raising=False,
        )
        start = datetime.now(timezone.utc)
        clock = _Clock(start)
        db = self._db()
        runner = self._switch_scenario(start)
        runner.symbol_probe = db
        original_update = StrategyService.update_config

        def _slow_update(service: StrategyService, data: dict[str, Any]) -> Any:
            clock.advance(300.0)
            return original_update(service, data)

        monkeypatch.setattr(StrategyService, "update_config", _slow_update)

        # When: the switch is evaluated against that clock.
        result = AutoPrimarySwitchService(db, clock=clock).evaluate(runner)

        # Then: the stale band does not survive, and the engine only ever
        # loaded the restored symbol -- it never saw the stale band.
        assert result.outcome == OUTCOME_SWITCH_BLOCKED
        config = self._config()
        assert config.symbol == "NVDA.US"
        assert config.buy_low == 100.0
        assert config.sell_high == 110.0
        assert runner.reload_symbols == ["NVDA.US"]
        assert self._events(EVENT_PRIMARY_SWITCHED) == []
        rolled = self._events(EVENT_PRIMARY_SWITCH_ROLLED_BACK)
        assert len(rolled) == 1
        assert "stale" in rolled[0]["payload"]["rollback_reason"]

    def test_landed_probe_failure_still_reloads(self, monkeypatch) -> None:
        """Deciding whether the commit landed must not need another query.

        A verification SELECT is itself fallible; when it raises too, a
        query-based probe propagates before either reload or rollback runs and
        strands the very state this path exists to prevent.
        """
        # Given: a post-commit refresh that raises, and any follow-up
        # verification query raising as well.
        now = datetime.now(timezone.utc)
        runner = self._switch_scenario(now)
        original_refresh = Session.refresh
        original_scalar = Session.scalar
        refreshed: list[bool] = []

        def _refresh(
            session: Session,
            instance: Any,
            *args: Any,
            **kwargs: Any,
        ) -> None:
            if isinstance(instance, StrategyConfig) and not refreshed:
                refreshed.append(True)
                raise RuntimeError("post-commit refresh failed")
            original_refresh(session, instance, *args, **kwargs)

        def _scalar(session: Session, statement: Any, *args: Any, **kwargs: Any) -> Any:
            if refreshed:
                raise RuntimeError("verification query failed")
            return original_scalar(session, statement, *args, **kwargs)

        monkeypatch.setattr(Session, "refresh", _refresh)
        monkeypatch.setattr(Session, "scalar", _scalar)

        # When: the switch is applied.
        result = AutoPrimarySwitchService(self._db()).evaluate(runner, now=now)

        # Then: the durable switch still reaches the runner.
        assert refreshed == [True]
        config = self._config()
        assert not (config.symbol == "CSCO.US" and runner.reloads == 0), (
            "committed switch stranded: config changed, runner never reloaded"
        )
        assert runner.reloads >= 1
        assert result.outcome == OUTCOME_SWITCHED

    def test_restore_never_upgrades_a_stale_read_snapshot(
        self, monkeypatch
    ) -> None:
        """SQLite fails a write that upgrades a superseded read snapshot.

        Any transaction opened while handling a post-commit failure must be
        ended before the reload is attempted, so the restore writes from a
        fresh snapshot rather than upgrading one taken before the reload.
        """
        # Given: a post-commit refresh that raises, then a reload that fails so
        # the restore has to write.
        now = datetime.now(timezone.utc)
        db = self._db()
        runner = self._switch_scenario(now)
        runner.reload_error = RuntimeError("engine refused the new symbol")
        runner.session = db
        steps: list[str] = []
        runner.trace = steps
        original_refresh = Session.refresh
        refreshed: list[bool] = []

        def _refresh(
            session: Session,
            instance: Any,
            *args: Any,
            **kwargs: Any,
        ) -> None:
            if isinstance(instance, StrategyConfig) and not refreshed:
                refreshed.append(True)
                raise RuntimeError("post-commit refresh failed")
            original_refresh(session, instance, *args, **kwargs)

        monkeypatch.setattr(Session, "refresh", _refresh)

        def _record_dml(
            _connection,
            _cursor,
            statement: str,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            head = " ".join(statement.upper().split()).split(" ")[0]
            if head in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                steps.append(head)

        def _record_commit(_connection) -> None:
            steps.append("COMMIT")

        def _record_rollback(_connection) -> None:
            steps.append("ROLLBACK")

        # When: the switch runs under those listeners.
        event.listen(self.engine, "before_cursor_execute", _record_dml)
        event.listen(self.engine, "commit", _record_commit)
        event.listen(self.engine, "rollback", _record_rollback)
        try:
            result = AutoPrimarySwitchService(db).evaluate(runner, now=now)
        finally:
            event.remove(self.engine, "before_cursor_execute", _record_dml)
            event.remove(self.engine, "commit", _record_commit)
            event.remove(self.engine, "rollback", _record_rollback)

        # Then: no read snapshot is left open across the reload boundary.
        assert refreshed == [True]
        assert result.outcome == OUTCOME_SWITCH_BLOCKED
        assert runner.in_transaction_at_reload == [False, False]
        reload_at = steps.index("reload")
        assert steps[reload_at - 1] in ("COMMIT", "ROLLBACK")
        assert self._config().symbol == "NVDA.US"


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


class TestReachRateGate(_Base):
    """The reach-rate gate: a candidate must prove its swings clear costs.

    A low ADX trend share only says price is not trending; it does not say the
    moves are large enough to pay the round-trip cost. Across 247 closed shadow
    trades, reach-rate separated winners from losers with no exceptions
    (85% vs 22%) while trend share ranked them barely better than chance -- its
    top-ranked symbol, GS.US at 0.0% trend, was a net loser. Both are required.
    """

    def _calm_candidate(self, symbol: str, *, close_price: float = 111.0) -> None:
        self._primary("NVDA.US")
        self._evidence("NVDA.US", trend_bars=70, calm_bars=10)
        self._selection_run([symbol])
        self._evidence(symbol, trend_bars=0, calm_bars=80, close_price=close_price)

    def test_calm_candidate_without_reach_evidence_is_rejected(self) -> None:
        """This is the GS.US case: perfect trend share, no proven reach."""
        self._calm_candidate("GS.US")

        result = AutoPrimarySwitchService(self._db()).evaluate(_Runner())

        assert result.outcome == OUTCOME_NO_ELIGIBLE_CANDIDATE
        assert "reach" in result.detail

    def test_calm_candidate_with_low_reach_rate_is_rejected(self) -> None:
        self._calm_candidate("ASML.US")
        # 2/8 == 25%, near the measured 22% loser average.
        self._reach("ASML.US", closed=8, reached=2)

        result = AutoPrimarySwitchService(self._db()).evaluate(_Runner())

        assert result.outcome == OUTCOME_NO_ELIGIBLE_CANDIDATE

    def test_calm_candidate_with_high_reach_rate_is_promoted(self) -> None:
        self._calm_candidate("TER.US")
        # 7/9 == 77.8%, near the measured 85% winner average.
        self._reach("TER.US", closed=9, reached=7)

        result = AutoPrimarySwitchService(self._db()).evaluate(_Runner())

        assert result.outcome == OUTCOME_SWITCHED
        assert result.candidate == "TER.US"

    def test_high_reach_rate_on_too_few_trades_is_rejected(self) -> None:
        """One lucky trade reads as 100%; the closed-trade floor blocks it."""
        self._calm_candidate("STX.US")
        self._reach("STX.US", closed=2, reached=2)

        result = AutoPrimarySwitchService(self._db()).evaluate(_Runner())

        assert result.outcome == OUTCOME_NO_ELIGIBLE_CANDIDATE

    def test_reach_rate_at_the_floor_is_inclusive(self) -> None:
        self._calm_candidate("MU.US")
        # 3/5 == 60.0%, exactly the configured floor.
        self._reach("MU.US", closed=5, reached=3)

        result = AutoPrimarySwitchService(self._db()).evaluate(_Runner())

        assert result.outcome == OUTCOME_SWITCHED

    def test_trend_share_cannot_outvote_reach_rate(self) -> None:
        """A calmer symbol must not win if it lacks reach evidence."""
        self._primary("NVDA.US")
        self._evidence("NVDA.US", trend_bars=70, calm_bars=10)
        self._selection_run(["GS.US", "TER.US"])
        # GS looks better on trend share (0 trend bars vs 8) but has no reach.
        self._evidence("GS.US", trend_bars=0, calm_bars=80, close_price=1055.0)
        self._evidence("TER.US", trend_bars=8, calm_bars=72, close_price=180.0)
        self._reach("TER.US", closed=9, reached=7)

        result = AutoPrimarySwitchService(self._db()).evaluate(_Runner())

        assert result.outcome == OUTCOME_SWITCHED
        assert result.candidate == "TER.US"


class TestSignalEdgeGate(_Base):
    """Trend share and reach-rate both read shadow evidence, so a signal with no
    provable edge ranks noise. The gate is assessed across all symbols and fails
    closed."""

    @pytest.fixture(autouse=True)
    def _require_edge(self, monkeypatch):
        monkeypatch.setattr(
            settings, "auto_primary_switch_require_signal_edge", True, raising=False
        )
        yield

    def _barriers(self, stop: float = 1.0, target: float = 1.0) -> None:
        db = self._db()
        db.add(StrategyV2ShadowConfig(
            symbol="NVDA.US",
            enabled=True,
            stop_loss_pct=stop,
            profit_target_pct=target,
        ))
        db.add(StrategyV2ShadowVersion(
            symbol="EDGE.US",
            config_version="v1",
            config_json=json.dumps(
                {
                    "algorithm_version": "strategy-v2-rth-mr-v5-causal-entry",
                    "stop_loss_pct": stop,
                    "profit_target_pct": target,
                }
            ),
            activated_at=datetime.now(timezone.utc),
        ))
        db.commit()
        db.close()

    def _edge_trades(
        self,
        *,
        targets: int,
        stops: int,
        days: int,
        pnl_available: bool = True,
    ) -> None:
        """Seed resolved trades spread over ``days`` distinct exit dates.

        Returns are signed to match the exit reason so first-passage and the
        clustered t-test see a coherent sample rather than contradictory ones.
        """
        db = self._db()
        now = datetime.now(timezone.utc)
        outcomes = (
            [("PROFIT_TARGET", 1.0)] * targets + [("PRICE_STOP", -0.5)] * stops
        )
        for index, (reason, pnl) in enumerate(outcomes):
            exit_at = now - timedelta(days=index % days, minutes=index)
            db.add(StrategyV2ShadowTrade(
                symbol="EDGE.US",
                config_version="v1",
                status="CLOSED",
                entry_at=exit_at - timedelta(minutes=20),
                exit_at=exit_at,
                entry_price=100.0,
                quantity=1.0,
                gross_pnl=pnl if pnl_available else None,
                net_pnl=pnl if pnl_available else None,
                exit_reason=reason,
                mfe_pct=0.009,
            ))
        db.commit()
        db.close()

    def _switch_scenario(self) -> _Runner:
        self._primary("NVDA.US")
        self._evidence("NVDA.US", trend_bars=70, calm_bars=10)
        self._selection_run(["AAPL.US"])
        self._evidence("AAPL.US", trend_bars=0, calm_bars=80, close_price=200.0)
        self._reach("AAPL.US", closed=9, reached=8)
        return _Runner()

    def test_proven_edge_lets_the_switch_through(self) -> None:
        runner = self._switch_scenario()
        self._barriers()
        self._edge_trades(targets=32, stops=8, days=20)

        result = AutoPrimarySwitchService(self._db()).evaluate(runner)

        assert result.outcome == OUTCOME_SWITCHED
        assert result.candidate == "AAPL.US"

    def test_signal_without_edge_blocks_the_switch(self) -> None:
        runner = self._switch_scenario()
        self._barriers()
        # Mirrors the live finding: first-passage rate below the random-walk
        # baseline, so no exit re-parameterisation could rescue the signal.
        self._edge_trades(targets=12, stops=28, days=20)

        result = AutoPrimarySwitchService(self._db()).evaluate(runner)

        assert result.outcome == OUTCOME_SIGNAL_EDGE_UNPROVEN
        assert "FAIL" in result.detail
        assert runner.calls == []

    def test_thin_evidence_blocks_but_reports_it_separately(self) -> None:
        runner = self._switch_scenario()
        self._barriers()
        self._edge_trades(targets=4, stops=1, days=3)

        result = AutoPrimarySwitchService(self._db()).evaluate(runner)

        assert result.outcome == OUTCOME_SIGNAL_EDGE_UNPROVEN
        assert "INSUFFICIENT_DATA" in result.detail
        assert runner.calls == []

    def test_unresolved_barrier_provenance_requests_operator_warning(self) -> None:
        # Given: resolved evidence exists but its version has no snapshot.
        runner = self._switch_scenario()
        db = self._db()
        db.add(StrategyV2ShadowConfig(
            symbol="NVDA.US",
            enabled=True,
            stop_loss_pct=1.0,
            profit_target_pct=1.0,
        ))
        db.commit()
        db.close()
        self._edge_trades(targets=32, stops=8, days=20)

        # When: the live promotion gate evaluates the unattributable cohort.
        result = AutoPrimarySwitchService(self._db()).evaluate(runner)

        # Then: it remains fail-closed and asks the cron to warn an operator.
        assert result.outcome == OUTCOME_SIGNAL_EDGE_UNPROVEN
        assert result.signal_edge_unassessable is True
        assert "40" in result.detail
        assert runner.calls == []

    def test_missing_pnl_cohort_emits_operator_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Given: one matching v5 version has 40 trades but no paired PnL.
        from app import main as main_module
        from app import runner as runner_module
        from app.services import auto_primary_switch_service
        from app.services import durable_job_lease_service

        runner = self._switch_scenario()
        self._barriers()
        self._edge_trades(
            targets=32,
            stops=8,
            days=20,
            pnl_available=False,
        )
        result = AutoPrimarySwitchService(self._db()).evaluate(runner)

        # When: the cron receives the real blocked result from that cohort.
        class FakeSession:
            def close(self) -> None:
                return None

        class FakeGuard:
            def __enter__(self) -> "FakeGuard":
                return self

            def __exit__(self, *_args: object) -> bool:
                return False

        class FakeLeaseService:
            def __init__(self, **_kwargs: object) -> None:
                return None

            @staticmethod
            def try_acquire(_lease_key: str) -> object:
                return object()

            @staticmethod
            def keepalive(_lease: object, **_kwargs: object) -> FakeGuard:
                return FakeGuard()

        class FakeSwitchService:
            def __init__(self, _db: object) -> None:
                return None

            @staticmethod
            def evaluate(_runner: object) -> object:
                return result

        monkeypatch.setattr(main_module.settings, "auto_primary_switch_enabled", True)
        monkeypatch.setattr(main_module, "SessionLocal", FakeSession)
        monkeypatch.setattr(runner_module, "get_runner", lambda: object())
        monkeypatch.setattr(
            durable_job_lease_service,
            "DurableJobLeaseService",
            FakeLeaseService,
        )
        monkeypatch.setattr(
            auto_primary_switch_service,
            "AutoPrimarySwitchService",
            FakeSwitchService,
        )
        with caplog.at_level(logging.WARNING, logger="auto_trade.main"):
            assert main_module._auto_primary_switch_tick_sync() is None

        # Then: missing PnL is marked unassessable and warned with full detail.
        assert result.outcome == OUTCOME_SIGNAL_EDGE_UNPROVEN
        assert result.signal_edge_unassessable is True
        assert "missing_pnl_excluded=40" in result.detail
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        assert warnings == [
            "automatic primary switch signal-edge gate unassessable: "
            + result.detail
        ]
        assert runner.calls == []

    def test_unassessable_signal_fails_closed(self) -> None:
        runner = self._switch_scenario()

        result = AutoPrimarySwitchService(self._db()).evaluate(runner)

        assert result.outcome == OUTCOME_SIGNAL_EDGE_UNPROVEN
        assert "not assessable" in result.detail
        assert runner.calls == []

    def test_gate_is_skipped_when_the_requirement_is_off(self, monkeypatch) -> None:
        monkeypatch.setattr(
            settings, "auto_primary_switch_require_signal_edge", False, raising=False
        )
        runner = self._switch_scenario()

        result = AutoPrimarySwitchService(self._db()).evaluate(runner)

        assert result.outcome == OUTCOME_SWITCHED

    def test_acceptable_incumbent_never_pays_for_the_assessment(self) -> None:
        self._primary("NVDA.US")
        self._evidence("NVDA.US", trend_bars=10, calm_bars=70)
        self._selection_run(["AAPL.US"])
        self._evidence("AAPL.US", trend_bars=0, calm_bars=80)

        result = AutoPrimarySwitchService(self._db()).evaluate(_Runner())

        assert result.outcome == OUTCOME_INCUMBENT_ACCEPTABLE


def test_auto_primary_switch_tick_warns_when_signal_edge_is_unassessable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given: the live gate reports a permanently unattributable evidence cohort.
    from app import main as main_module
    from app import runner as runner_module
    from app.services import auto_primary_switch_service
    from app.services import durable_job_lease_service

    detail = (
        "shadow signal edge INSUFFICIENT_DATA: matched_versions=0; "
        "matched_trades=0; provenance_excluded_trades=248"
    )

    class FakeSession:
        def close(self) -> None:
            return None

    class FakeGuard:
        def __enter__(self) -> "FakeGuard":
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

    class FakeLeaseService:
        def __init__(self, **_kwargs: object) -> None:
            return None

        @staticmethod
        def try_acquire(_lease_key: str) -> object:
            return object()

        @staticmethod
        def keepalive(_lease: object, **_kwargs: object) -> FakeGuard:
            return FakeGuard()

    class FakeSwitchService:
        def __init__(self, _db: object) -> None:
            return None

        @staticmethod
        def evaluate(_runner: object) -> SimpleNamespace:
            return SimpleNamespace(
                outcome="SIGNAL_EDGE_UNPROVEN",
                incumbent="NVDA.US",
                candidate="",
                detail=detail,
                signal_edge_unassessable=True,
            )

    monkeypatch.setattr(main_module.settings, "auto_primary_switch_enabled", True)
    monkeypatch.setattr(main_module, "SessionLocal", FakeSession)
    monkeypatch.setattr(runner_module, "get_runner", lambda: object())
    monkeypatch.setattr(
        durable_job_lease_service,
        "DurableJobLeaseService",
        FakeLeaseService,
    )
    monkeypatch.setattr(
        auto_primary_switch_service,
        "AutoPrimarySwitchService",
        FakeSwitchService,
    )

    # When: the synchronous cron tick handles that blocked result.
    with caplog.at_level(logging.WARNING, logger="auto_trade.main"):
        assert main_module._auto_primary_switch_tick_sync() is None

    # Then: an operator-visible warning carries the complete diagnosis.
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert warnings == [
        "automatic primary switch signal-edge gate unassessable: " + detail
    ]
