"""Conditional alert rules — service + API. Per-file sqlite."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_alert_rules_{os.getpid()}.db"
)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import AlertFiring, AlertRule, Base, RuntimeState, StrategyConfig
from app.schemas import AlertRuleCreate
from app.services.alert_rule_service import AlertRuleService


class FakeQuote:
    def __init__(self, symbol: str, last_price: float) -> None:
        self.symbol = symbol
        self.last_price = last_price


class FakeBroker:
    def __init__(self, quotes: dict[str, float]) -> None:
        self._quotes = quotes

    def get_quotes(self, symbols: list[str]) -> list[FakeQuote]:
        return [FakeQuote(s, self._quotes[s]) for s in symbols if s in self._quotes]


class FakeNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.return_true = True

    def send(self, title: str, content: str, severity: str = "INFO") -> bool:
        self.calls.append((title, content, severity))
        return self.return_true


class FakeRunner:
    def __init__(self, broker: object | None, notifier: object | None) -> None:
        self.broker = broker
        self.notifier = notifier


class _Base:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            os.environ["AUTO_TRADE_DATABASE_URL"], connect_args={"check_same_thread": False}
        )
        Base.metadata.drop_all(bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)

        def override_get_db():
            db = Session(bind=cls.engine)
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def teardown_class(cls) -> None:
        app.dependency_overrides.pop(get_db, None)

    def setup_method(self) -> None:
        db = Session(bind=self.engine)
        db.query(AlertFiring).delete()
        db.query(AlertRule).delete()
        db.query(RuntimeState).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)


class TestAlertRuleService(_Base):
    def _price_rule(self, **kw) -> int:
        svc = AlertRuleService(self._db())
        out = svc.create(AlertRuleCreate(
            name=kw.get("name", "r"),
            symbol=kw.get("symbol", "AAPL.US"),
            rule_type=kw.get("rule_type", "price_above"),
            threshold=kw.get("threshold", 150.0),
            severity="WARNING",
            enabled=True,
            cooldown_seconds=kw.get("cooldown_seconds", 300),
        ))
        return out.id

    def test_price_above_fires(self) -> None:
        rid = self._price_rule(rule_type="price_above", threshold=150.0)
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(
            FakeRunner(FakeBroker({"AAPL.US": 160.0}), notifier)
        )
        assert result.fired == 1
        assert len(notifier.calls) == 1

    def test_price_not_triggered(self) -> None:
        self._price_rule(rule_type="price_above", threshold=150.0)
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(
            FakeRunner(FakeBroker({"AAPL.US": 140.0}), notifier)
        )
        assert result.fired == 0
        assert notifier.calls == []

    def test_price_below_fires(self) -> None:
        self._price_rule(rule_type="price_below", threshold=100.0)
        notifier = FakeNotifier()
        AlertRuleService(self._db()).evaluate(FakeRunner(FakeBroker({"AAPL.US": 90.0}), notifier))
        assert len(notifier.calls) == 1

    def test_daily_loss_fires(self) -> None:
        db = self._db()
        db.add(RuntimeState(symbol="AAPL.US", daily_pnl=-600.0))
        db.commit()
        db.close()
        svc = AlertRuleService(self._db())
        svc.create(AlertRuleCreate(name="loss", symbol="AAPL.US", rule_type="daily_loss", threshold=-500.0))
        notifier = FakeNotifier()
        result = svc.evaluate(FakeRunner(None, notifier))
        assert result.fired == 1

    def test_cooldown_skips_second_fire(self) -> None:
        self._price_rule(rule_type="price_above", threshold=150.0, cooldown_seconds=300)
        now = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)
        notifier = FakeNotifier()
        svc = AlertRuleService(self._db())
        r1 = svc.evaluate(FakeRunner(FakeBroker({"AAPL.US": 160.0}), notifier), now=now)
        assert r1.fired == 1
        # 1 minute later — within cooldown
        r2 = svc.evaluate(FakeRunner(FakeBroker({"AAPL.US": 160.0}), notifier), now=now + timedelta(minutes=1))
        assert r2.fired == 0
        assert r2.skipped_cooldown == 1
        # Past cooldown
        r3 = svc.evaluate(FakeRunner(FakeBroker({"AAPL.US": 160.0}), notifier), now=now + timedelta(minutes=6))
        assert r3.fired == 1

    def test_disabled_rule_not_evaluated(self) -> None:
        svc = AlertRuleService(self._db())
        svc.create(AlertRuleCreate(name="r", symbol="AAPL.US", rule_type="price_above", threshold=150.0, enabled=False))
        result = svc.evaluate(FakeRunner(FakeBroker({"AAPL.US": 160.0}), FakeNotifier()))
        assert result.evaluated == 0
        assert result.fired == 0

    def test_no_notifier_does_not_crash(self) -> None:
        self._price_rule(rule_type="price_above", threshold=150.0)
        result = AlertRuleService(self._db()).evaluate(FakeRunner(FakeBroker({"AAPL.US": 160.0}), None))
        assert result.fired == 0  # condition met but nothing to send through

    def test_daily_loss_does_not_fire_on_unrelated_symbol(self) -> None:
        # Symbol-specific daily_loss rule for AAPL, but only TSLA has a state row.
        # Must NOT fall back to TSLA's loss and fire an AAPL-branded alert.
        db = self._db()
        db.add(RuntimeState(symbol="TSLA.US", daily_pnl=-600.0))
        db.commit()
        db.close()
        svc = AlertRuleService(self._db())
        svc.create(AlertRuleCreate(name="aapl loss", symbol="AAPL.US", rule_type="daily_loss", threshold=-500.0))
        notifier = FakeNotifier()
        result = svc.evaluate(FakeRunner(None, notifier))
        assert result.fired == 0
        assert notifier.calls == []


class _AccountRuleMixin:
    """Shared helpers for account-wide-only rule tests (consecutive_losses,
    kill_switch_engaged). The authoritative account state is resolved from the
    latest StrategyConfig symbol -> that symbol's RuntimeState, falling back
    to the legacy ``symbol == ""`` row."""

    # Subclasses set this to the rule type under test.
    rule_type: str = ""

    def _make_rule(self, base: _Base, **kw) -> int:
        svc = AlertRuleService(base._db())
        out = svc.create(AlertRuleCreate(
            name=kw.get("name", "acct"),
            symbol="",  # account-wide-only
            rule_type=kw.get("rule_type", self.rule_type),  # type: ignore[arg-type]
            threshold=kw.get("threshold", 3 if self.rule_type == "consecutive_losses" else 1.0),
            severity=kw.get("severity", "WARNING"),
            enabled=True,
            cooldown_seconds=kw.get("cooldown_seconds", 300),
        ))
        return out.id

    def _seed_primary(self, base: _Base, symbol: str, **state) -> int:
        """Seed a StrategyConfig (primary symbol) + its RuntimeState row."""
        db = base._db()
        db.add(StrategyConfig(symbol=symbol, market="US"))
        db.commit()
        cfg = db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
        assert cfg is not None
        config_id = cfg.id
        db.add(RuntimeState(symbol=symbol, **state))
        db.commit()
        db.close()
        return config_id


class TestConsecutiveLossesRule(_AccountRuleMixin, _Base):
    """consecutive_losses rule: account-wide-only, fires when the authoritative
    account RuntimeState.consecutive_losses >= threshold. No broker quote fetch;
    cooldown; no-state. Reads the latest StrategyConfig symbol's state, falling
    back to the legacy empty-symbol row — never an arbitrary secondary row."""

    rule_type = "consecutive_losses"

    def test_fires_when_account_consecutive_losses_at_threshold(self) -> None:
        self._seed_primary(self, "AAPL.US", consecutive_losses=3)
        rid = self._make_rule(self, threshold=3)
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(FakeRunner(None, notifier))
        assert result.fired == 1
        assert len(notifier.calls) == 1
        rows = AlertRuleService(self._db()).history(rid)
        assert len(rows) == 1
        assert rows[0].trigger_value == 3.0
        assert rows[0].threshold == 3.0
        assert "连续亏损" in rows[0].message

    def test_fires_when_account_consecutive_losses_exceeds_threshold(self) -> None:
        self._seed_primary(self, "AAPL.US", consecutive_losses=5)
        self._make_rule(self, threshold=3)
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(FakeRunner(None, notifier))
        assert result.fired == 1

    def test_does_not_fire_below_threshold(self) -> None:
        self._seed_primary(self, "AAPL.US", consecutive_losses=2)
        self._make_rule(self, threshold=3)
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(FakeRunner(None, notifier))
        assert result.fired == 0
        assert notifier.calls == []

    def test_uses_primary_symbol_state_not_newer_secondary(self) -> None:
        # Older-ID primary row has high consecutive_losses; a newer, more
        # recently updated secondary row is clear. Account rule must use the
        # primary (authoritative) state, not the most-recently-updated row.
        db = self._db()
        db.add(StrategyConfig(symbol="AAPL.US", market="US"))
        db.commit()
        # Primary row (older id) with high streak.
        db.add(RuntimeState(symbol="AAPL.US", consecutive_losses=5))
        db.commit()
        # Secondary row (newer id, more recently updated) clear.
        db.add(RuntimeState(symbol="TSLA.US", consecutive_losses=0))
        db.commit()
        # Touch the secondary row's updated_at so it is "most recently updated".
        tsla = db.query(RuntimeState).filter(RuntimeState.symbol == "TSLA.US").first()
        assert tsla is not None
        ts = db.get(RuntimeState, tsla.id)
        assert ts is not None
        ts.updated_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
        db.commit()
        db.close()
        self._make_rule(self, threshold=3)
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(FakeRunner(None, notifier))
        assert result.fired == 1  # primary streak=5, not secondary streak=0

    def test_falls_back_to_legacy_empty_row_when_no_primary_symbol_row(self) -> None:
        # Primary symbol configured, but no RuntimeState row for it; a legacy
        # empty-symbol row exists with high streak. Must fall back to legacy.
        db = self._db()
        db.add(StrategyConfig(symbol="AAPL.US", market="US"))
        db.commit()
        db.add(RuntimeState(symbol="", consecutive_losses=4))
        db.commit()
        db.close()
        self._make_rule(self, threshold=3)
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(FakeRunner(None, notifier))
        assert result.fired == 1

    def test_no_primary_and_no_legacy_row_does_not_fire(self) -> None:
        # No StrategyConfig, no RuntimeState at all -> no data, never fires.
        self._make_rule(self, threshold=3)
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(FakeRunner(None, notifier))
        assert result.fired == 0
        assert notifier.calls == []

    def test_no_primary_symbol_uses_legacy_empty_row_only(self) -> None:
        # No StrategyConfig configured; only named rows exist (no empty row).
        # Must NOT fall back to an arbitrary named row.
        db = self._db()
        db.add(RuntimeState(symbol="TSLA.US", consecutive_losses=10))
        db.commit()
        db.close()
        self._make_rule(self, threshold=3)
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(FakeRunner(None, notifier))
        assert result.fired == 0
        assert notifier.calls == []

    def test_legacy_persisted_rule_with_symbol_uses_account_state(self) -> None:
        # A manually-persisted legacy rule with a non-empty symbol (created
        # before the account-wide-only validation) must still use the
        # authoritative account state, never that secondary symbol's row.
        db = self._db()
        db.add(StrategyConfig(symbol="AAPL.US", market="US"))
        db.commit()
        db.add(RuntimeState(symbol="AAPL.US", consecutive_losses=5))
        # Secondary row the legacy rule's symbol points at — must be ignored.
        db.add(RuntimeState(symbol="TSLA.US", consecutive_losses=0))
        db.commit()
        db.close()
        # Bypass schema validation to simulate a legacy-persisted rule.
        db = self._db()
        db.add(AlertRule(
            name="legacy streak", symbol="TSLA.US", rule_type="consecutive_losses",
            threshold=3, severity="WARNING", enabled=True, cooldown_seconds=300,
        ))
        db.commit()
        db.close()
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(FakeRunner(None, notifier))
        assert result.fired == 1  # uses primary AAPL streak=5, not TSLA streak=0

    def test_cooldown_skips_second_fire(self) -> None:
        self._seed_primary(self, "AAPL.US", consecutive_losses=5)
        self._make_rule(self, threshold=3, cooldown_seconds=300)
        now = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)
        notifier = FakeNotifier()
        svc = AlertRuleService(self._db())
        r1 = svc.evaluate(FakeRunner(None, notifier), now=now)
        assert r1.fired == 1
        r2 = svc.evaluate(FakeRunner(None, notifier), now=now + timedelta(minutes=1))
        assert r2.fired == 0
        assert r2.skipped_cooldown == 1
        r3 = svc.evaluate(FakeRunner(None, notifier), now=now + timedelta(minutes=6))
        assert r3.fired == 1

    def test_no_broker_quote_fetch_for_state_rule(self) -> None:
        class ExplodingBroker:
            def get_quotes(self, symbols: list[str]) -> list[FakeQuote]:
                raise AssertionError("consecutive_losses must not fetch quotes")

        self._seed_primary(self, "AAPL.US", consecutive_losses=5)
        self._make_rule(self, threshold=3)
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(FakeRunner(ExplodingBroker(), notifier))
        assert result.fired == 1


class TestConsecutiveLossesValidation(_Base):
    def test_threshold_must_be_positive(self) -> None:
        resp = self.client.post("/api/alert-rules", json={
            "name": "streak", "symbol": "", "rule_type": "consecutive_losses",
            "threshold": 0,
        })
        assert resp.status_code == 422

    def test_threshold_must_be_integer_like(self) -> None:
        resp = self.client.post("/api/alert-rules", json={
            "name": "streak", "symbol": "", "rule_type": "consecutive_losses",
            "threshold": 2.5,
        })
        assert resp.status_code == 422

    def test_negative_threshold_rejected(self) -> None:
        resp = self.client.post("/api/alert-rules", json={
            "name": "streak", "symbol": "", "rule_type": "consecutive_losses",
            "threshold": -1,
        })
        assert resp.status_code == 422

    def test_non_empty_symbol_rejected(self) -> None:
        resp = self.client.post("/api/alert-rules", json={
            "name": "streak", "symbol": "AAPL.US", "rule_type": "consecutive_losses",
            "threshold": 3,
        })
        assert resp.status_code == 422

    def test_whitespace_symbol_rejected(self) -> None:
        resp = self.client.post("/api/alert-rules", json={
            "name": "streak", "symbol": "  ", "rule_type": "consecutive_losses",
            "threshold": 3,
        })
        assert resp.status_code == 422

    def test_valid_positive_integer_threshold_accepted(self) -> None:
        resp = self.client.post("/api/alert-rules", json={
            "name": "streak", "symbol": "", "rule_type": "consecutive_losses",
            "threshold": 3, "severity": "WARNING", "enabled": True, "cooldown_seconds": 300,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["rule_type"] == "consecutive_losses"
        assert resp.json()["threshold"] == 3.0
        assert resp.json()["symbol"] == ""

    def test_existing_rule_types_still_validated(self) -> None:
        # Backward compatibility: a bogus rule type is still rejected.
        resp = self.client.post("/api/alert-rules", json={
            "name": "x", "symbol": "AAPL.US", "rule_type": "bogus", "threshold": 1,
        })
        assert resp.status_code == 422
        # And a valid daily_loss rule still works (no threshold constraint).
        resp = self.client.post("/api/alert-rules", json={
            "name": "loss", "symbol": "AAPL.US", "rule_type": "daily_loss", "threshold": -500.0,
        })
        assert resp.status_code == 200, resp.text


class TestKillSwitchRule(_AccountRuleMixin, _Base):
    """kill_switch_engaged rule: account-wide-only, notification-only, fires
    when the authoritative account RuntimeState.kill_switch is true. Reuses
    the same authoritative account state resolver as consecutive_losses.
    Never mutates RiskController or RuntimeState."""

    rule_type = "kill_switch_engaged"

    def test_fires_when_account_kill_switch_true(self) -> None:
        self._seed_primary(self, "AAPL.US", kill_switch=True)
        rid = self._make_rule(self, threshold=1.0, severity="CRITICAL")
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(FakeRunner(None, notifier))
        assert result.fired == 1
        assert len(notifier.calls) == 1
        rows = AlertRuleService(self._db()).history(rid)
        assert len(rows) == 1
        assert rows[0].trigger_value == 1.0
        assert rows[0].threshold == 1.0
        assert "熔断开关" in rows[0].message

    def test_does_not_fire_when_account_kill_switch_false(self) -> None:
        self._seed_primary(self, "AAPL.US", kill_switch=False)
        self._make_rule(self, threshold=1.0)
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(FakeRunner(None, notifier))
        assert result.fired == 0
        assert notifier.calls == []

    def test_uses_primary_symbol_state_not_newer_secondary(self) -> None:
        # Older-ID primary row has kill_switch=true; a newer, more recently
        # updated secondary row is clear. Account rule must use the primary.
        db = self._db()
        db.add(StrategyConfig(symbol="AAPL.US", market="US"))
        db.commit()
        db.add(RuntimeState(symbol="AAPL.US", kill_switch=True))
        db.commit()
        db.add(RuntimeState(symbol="TSLA.US", kill_switch=False))
        db.commit()
        tsla = db.query(RuntimeState).filter(RuntimeState.symbol == "TSLA.US").first()
        assert tsla is not None
        ts = db.get(RuntimeState, tsla.id)
        assert ts is not None
        ts.updated_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
        db.commit()
        db.close()
        self._make_rule(self, threshold=1.0)
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(FakeRunner(None, notifier))
        assert result.fired == 1  # primary kill_switch=true, not secondary false

    def test_falls_back_to_legacy_empty_row_when_no_primary_symbol_row(self) -> None:
        db = self._db()
        db.add(StrategyConfig(symbol="AAPL.US", market="US"))
        db.commit()
        db.add(RuntimeState(symbol="", kill_switch=True))
        db.commit()
        db.close()
        self._make_rule(self, threshold=1.0)
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(FakeRunner(None, notifier))
        assert result.fired == 1

    def test_no_primary_and_no_legacy_row_does_not_fire(self) -> None:
        self._make_rule(self, threshold=1.0)
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(FakeRunner(None, notifier))
        assert result.fired == 0
        assert notifier.calls == []

    def test_no_primary_symbol_uses_legacy_empty_row_only(self) -> None:
        # No StrategyConfig; only named rows exist. Must NOT fall back.
        db = self._db()
        db.add(RuntimeState(symbol="TSLA.US", kill_switch=True))
        db.commit()
        db.close()
        self._make_rule(self, threshold=1.0)
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(FakeRunner(None, notifier))
        assert result.fired == 0
        assert notifier.calls == []

    def test_legacy_persisted_rule_with_symbol_uses_account_state(self) -> None:
        db = self._db()
        db.add(StrategyConfig(symbol="AAPL.US", market="US"))
        db.commit()
        db.add(RuntimeState(symbol="AAPL.US", kill_switch=True))
        db.add(RuntimeState(symbol="TSLA.US", kill_switch=False))
        db.commit()
        db.close()
        db = self._db()
        db.add(AlertRule(
            name="legacy kill", symbol="TSLA.US", rule_type="kill_switch_engaged",
            threshold=1.0, severity="CRITICAL", enabled=True, cooldown_seconds=300,
        ))
        db.commit()
        db.close()
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(FakeRunner(None, notifier))
        assert result.fired == 1  # uses primary AAPL kill_switch=true

    def test_cooldown_skips_second_fire(self) -> None:
        self._seed_primary(self, "AAPL.US", kill_switch=True)
        self._make_rule(self, threshold=1.0, cooldown_seconds=300)
        now = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)
        notifier = FakeNotifier()
        svc = AlertRuleService(self._db())
        r1 = svc.evaluate(FakeRunner(None, notifier), now=now)
        assert r1.fired == 1
        r2 = svc.evaluate(FakeRunner(None, notifier), now=now + timedelta(minutes=1))
        assert r2.fired == 0
        assert r2.skipped_cooldown == 1
        r3 = svc.evaluate(FakeRunner(None, notifier), now=now + timedelta(minutes=6))
        assert r3.fired == 1

    def test_evaluation_does_not_mutate_runtime_state(self) -> None:
        # Proof that evaluation is notification-only: account state must remain
        # exactly as seeded after evaluate runs.
        self._seed_primary(self, "AAPL.US", kill_switch=True, consecutive_losses=7, daily_pnl=-300.0)
        seeded = self._db().query(RuntimeState).filter(RuntimeState.symbol == "AAPL.US").first()
        assert seeded is not None
        state_id = seeded.id
        self._make_rule(self, threshold=1.0)
        AlertRuleService(self._db()).evaluate(FakeRunner(None, FakeNotifier()))
        db = self._db()
        state = db.get(RuntimeState, state_id)
        assert state is not None
        assert state.kill_switch is True  # unchanged
        assert state.consecutive_losses == 7  # unchanged
        assert state.daily_pnl == -300.0  # unchanged
        db.close()

    def test_no_broker_quote_fetch_for_state_rule(self) -> None:
        class ExplodingBroker:
            def get_quotes(self, symbols: list[str]) -> list[FakeQuote]:
                raise AssertionError("kill_switch_engaged must not fetch quotes")

        self._seed_primary(self, "AAPL.US", kill_switch=True)
        self._make_rule(self, threshold=1.0)
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(FakeRunner(ExplodingBroker(), notifier))
        assert result.fired == 1


class TestKillSwitchValidation(_Base):
    def test_threshold_must_be_exactly_one(self) -> None:
        resp = self.client.post("/api/alert-rules", json={
            "name": "kill", "symbol": "", "rule_type": "kill_switch_engaged",
            "threshold": 0,
        })
        assert resp.status_code == 422

    def test_threshold_two_rejected(self) -> None:
        resp = self.client.post("/api/alert-rules", json={
            "name": "kill", "symbol": "", "rule_type": "kill_switch_engaged",
            "threshold": 2,
        })
        assert resp.status_code == 422

    def test_threshold_fractional_rejected(self) -> None:
        resp = self.client.post("/api/alert-rules", json={
            "name": "kill", "symbol": "", "rule_type": "kill_switch_engaged",
            "threshold": 1.5,
        })
        assert resp.status_code == 422

    def test_non_empty_symbol_rejected(self) -> None:
        resp = self.client.post("/api/alert-rules", json={
            "name": "kill", "symbol": "AAPL.US", "rule_type": "kill_switch_engaged",
            "threshold": 1.0,
        })
        assert resp.status_code == 422

    def test_whitespace_symbol_rejected(self) -> None:
        resp = self.client.post("/api/alert-rules", json={
            "name": "kill", "symbol": "  ", "rule_type": "kill_switch_engaged",
            "threshold": 1.0,
        })
        assert resp.status_code == 422

    def test_threshold_one_accepted(self) -> None:
        resp = self.client.post("/api/alert-rules", json={
            "name": "kill", "symbol": "", "rule_type": "kill_switch_engaged",
            "threshold": 1.0, "severity": "CRITICAL", "enabled": True, "cooldown_seconds": 300,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["rule_type"] == "kill_switch_engaged"
        assert resp.json()["threshold"] == 1.0
        assert resp.json()["symbol"] == ""

    def test_false_state_never_fires_with_zero_threshold_blocked(self) -> None:
        # The schema blocks threshold=0, so a false (0.0) state can never
        # satisfy value >= threshold. Confirm the guard holds.
        resp = self.client.post("/api/alert-rules", json={
            "name": "kill", "symbol": "", "rule_type": "kill_switch_engaged",
            "threshold": 0,
        })
        assert resp.status_code == 422


class TestFiniteThresholdValidation(_Base):
    """threshold must reject NaN and infinity for all rule types — clean 422
    validation rather than runtime conversion errors."""

    def test_nan_threshold_rejected_for_price_above(self) -> None:
        resp = self.client.post("/api/alert-rules", json={
            "name": "x", "symbol": "AAPL.US", "rule_type": "price_above",
            "threshold": "NaN",
        })
        assert resp.status_code == 422

    def test_positive_inf_threshold_rejected_for_price_below(self) -> None:
        resp = self.client.post("/api/alert-rules", json={
            "name": "x", "symbol": "AAPL.US", "rule_type": "price_below",
            "threshold": "Infinity",
        })
        assert resp.status_code == 422

    def test_negative_inf_threshold_rejected_for_daily_loss(self) -> None:
        resp = self.client.post("/api/alert-rules", json={
            "name": "x", "symbol": "AAPL.US", "rule_type": "daily_loss",
            "threshold": "-Infinity",
        })
        assert resp.status_code == 422

    def test_nan_threshold_rejected_for_consecutive_losses(self) -> None:
        resp = self.client.post("/api/alert-rules", json={
            "name": "x", "symbol": "", "rule_type": "consecutive_losses",
            "threshold": "NaN",
        })
        assert resp.status_code == 422

    def test_inf_threshold_rejected_for_kill_switch_engaged(self) -> None:
        resp = self.client.post("/api/alert-rules", json={
            "name": "x", "symbol": "", "rule_type": "kill_switch_engaged",
            "threshold": "Infinity",
        })
        assert resp.status_code == 422

    def test_finite_threshold_still_accepted(self) -> None:
        resp = self.client.post("/api/alert-rules", json={
            "name": "x", "symbol": "AAPL.US", "rule_type": "price_above",
            "threshold": 150.0,
        })
        assert resp.status_code == 200, resp.text


class TestDailyLossStateLookup(_Base):
    """Regression tests for the restored pre-c566e76 daily_loss contract:
    query RuntimeState matching rule.symbol; blank symbol with no blank row
    falls back to the latest row by id. Symbol-specific missing state must
    NOT fall back."""

    def test_blank_daily_loss_uses_legacy_empty_row(self) -> None:
        # A legacy empty-symbol row plus named rows: blank daily_loss must use
        # the empty row (not the latest-by-id named row).
        db = self._db()
        db.add(RuntimeState(symbol="", daily_pnl=-700.0))
        db.add(RuntimeState(symbol="AAPL.US", daily_pnl=-100.0))
        db.add(RuntimeState(symbol="TSLA.US", daily_pnl=-200.0))
        db.commit()
        db.close()
        svc = AlertRuleService(self._db())
        svc.create(AlertRuleCreate(name="acct loss", symbol="", rule_type="daily_loss", threshold=-500.0))
        notifier = FakeNotifier()
        result = svc.evaluate(FakeRunner(None, notifier))
        assert result.fired == 1  # empty row -700 <= -500

    def test_blank_daily_loss_falls_back_to_latest_when_no_empty_row(self) -> None:
        # No empty row; blank daily_loss falls back to the latest row by id.
        db = self._db()
        db.add(RuntimeState(symbol="AAPL.US", daily_pnl=-100.0))
        db.add(RuntimeState(symbol="TSLA.US", daily_pnl=-600.0))  # latest by id
        db.commit()
        db.close()
        svc = AlertRuleService(self._db())
        svc.create(AlertRuleCreate(name="acct loss", symbol="", rule_type="daily_loss", threshold=-500.0))
        notifier = FakeNotifier()
        result = svc.evaluate(FakeRunner(None, notifier))
        assert result.fired == 1  # TSLA -600 <= -500

    def test_symbol_specific_daily_loss_does_not_fall_back(self) -> None:
        # AAPL daily_loss rule, only TSLA has a state row -> no fallback.
        db = self._db()
        db.add(RuntimeState(symbol="TSLA.US", daily_pnl=-600.0))
        db.commit()
        db.close()
        svc = AlertRuleService(self._db())
        svc.create(AlertRuleCreate(name="aapl loss", symbol="AAPL.US", rule_type="daily_loss", threshold=-500.0))
        notifier = FakeNotifier()
        result = svc.evaluate(FakeRunner(None, notifier))
        assert result.fired == 0
        assert notifier.calls == []


class TestAlertRuleAPI(_Base):
    def test_crud_and_evaluate(self) -> None:
        create = self.client.post("/api/alert-rules", json={
            "name": "high price", "symbol": "AAPL.US", "rule_type": "price_above",
            "threshold": 150, "severity": "WARNING", "enabled": True, "cooldown_seconds": 300,
        })
        assert create.status_code == 200, create.text
        rid = create.json()["id"]

        lst = self.client.get("/api/alert-rules")
        assert lst.json()["total"] == 1

        upd = self.client.put(f"/api/alert-rules/{rid}", json={
            "name": "higher", "symbol": "AAPL.US", "rule_type": "price_above",
            "threshold": 200, "severity": "CRITICAL", "enabled": True, "cooldown_seconds": 60,
        })
        assert upd.status_code == 200
        assert upd.json()["threshold"] == 200

        missing = self.client.get("/api/alert-rules/999999")
        assert missing.status_code == 404

        dele = self.client.delete(f"/api/alert-rules/{rid}")
        assert dele.status_code == 204

    def test_invalid_rule_type_422(self) -> None:
        resp = self.client.post("/api/alert-rules", json={
            "name": "x", "symbol": "AAPL.US", "rule_type": "bogus", "threshold": 1,
        })
        assert resp.status_code == 422


class TestAlertFiringHistory(_Base):
    def _make_rule(self, **kw) -> int:
        svc = AlertRuleService(self._db())
        out = svc.create(AlertRuleCreate(
            name=kw.get("name", "r"),
            symbol=kw.get("symbol", "AAPL.US"),
            rule_type=kw.get("rule_type", "price_above"),
            threshold=kw.get("threshold", 150.0),
            severity="WARNING",
            enabled=True,
            cooldown_seconds=kw.get("cooldown_seconds", 0),
        ))
        return out.id

    def test_evaluate_records_firing_and_history_returns_it(self) -> None:
        rid = self._make_rule(threshold=150.0, cooldown_seconds=0)
        svc = AlertRuleService(self._db())
        svc.evaluate(FakeRunner(FakeBroker({"AAPL.US": 160.0}), FakeNotifier()))
        # committed inside evaluate via _record_firing
        rows = svc.history(rid)
        assert len(rows) == 1
        f = rows[0]
        assert f.rule_id == rid
        assert f.symbol == "AAPL.US"
        assert f.rule_type == "price_above"
        assert f.trigger_value == 160.0
        assert f.threshold == 150.0
        assert "160.00" in f.message

    def test_cooldown_records_two_firings_across_window(self) -> None:
        rid = self._make_rule(threshold=150.0, cooldown_seconds=60)
        now = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)
        svc = AlertRuleService(self._db())
        svc.evaluate(FakeRunner(FakeBroker({"AAPL.US": 160.0}), FakeNotifier()), now=now)
        # within cooldown -> no new firing
        svc.evaluate(FakeRunner(FakeBroker({"AAPL.US": 160.0}), FakeNotifier()), now=now + timedelta(seconds=10))
        # past cooldown -> new firing
        svc.evaluate(FakeRunner(FakeBroker({"AAPL.US": 160.0}), FakeNotifier()), now=now + timedelta(seconds=120))
        rows = AlertRuleService(self._db()).history(rid)
        assert len(rows) == 2
        # most-recent first
        assert rows[0].fired_at > rows[1].fired_at

    def test_not_triggered_records_no_firing(self) -> None:
        rid = self._make_rule(threshold=150.0)
        AlertRuleService(self._db()).evaluate(FakeRunner(FakeBroker({"AAPL.US": 140.0}), FakeNotifier()))
        assert AlertRuleService(self._db()).history(rid) == []

    def test_history_endpoint_and_collection_endpoint(self) -> None:
        rid = self._make_rule(threshold=150.0, cooldown_seconds=0)
        AlertRuleService(self._db()).evaluate(FakeRunner(FakeBroker({"AAPL.US": 160.0}), FakeNotifier()))

        per_rule = self.client.get(f"/api/alert-rules/{rid}/history")
        assert per_rule.status_code == 200, per_rule.text
        assert per_rule.json()["total"] == 1
        assert per_rule.json()["items"][0]["trigger_value"] == 160.0

        collection = self.client.get("/api/alert-firings")
        assert collection.status_code == 200, collection.text
        assert collection.json()["total"] == 1

        filtered = self.client.get("/api/alert-firings", params={"rule_id": 999999})
        assert filtered.json()["total"] == 0

    def test_empty_rule_history_is_404_agnostic(self) -> None:
        rid = self._make_rule(threshold=150.0)
        resp = self.client.get(f"/api/alert-rules/{rid}/history")
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    def test_history_to_date_is_inclusive_of_that_day(self) -> None:
        rid = self._make_rule(threshold=150.0, cooldown_seconds=0)
        fire_time = datetime(2026, 6, 16, 23, 59, tzinfo=timezone.utc)
        AlertRuleService(self._db()).evaluate(
            FakeRunner(FakeBroker({"AAPL.US": 160.0}), FakeNotifier()), now=fire_time,
        )
        resp = self.client.get(f"/api/alert-rules/{rid}/history", params={"to_date": "2026-06-16"})
        assert resp.status_code == 200
        # The 23:59 fire is within to_date=2026-06-16 (inclusive end-of-day).
        assert resp.json()["total"] == 1

    def test_history_to_date_excludes_next_midnight_fire(self) -> None:
        # A firing exactly at the following day's 00:00:00 must be EXCLUDED
        # when to_date is the previous day. The end-of-day boundary is the
        # last instant of the selected day (time.max), not the first instant
        # of the next day.
        rid = self._make_rule(threshold=150.0, cooldown_seconds=0)
        fire_time = datetime(2026, 6, 17, 0, 0, 0, tzinfo=timezone.utc)
        AlertRuleService(self._db()).evaluate(
            FakeRunner(FakeBroker({"AAPL.US": 160.0}), FakeNotifier()), now=fire_time,
        )
        resp = self.client.get(f"/api/alert-rules/{rid}/history", params={"to_date": "2026-06-16"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0  # 2026-06-17 00:00:00 is outside 2026-06-16

    def test_history_inverted_date_range_returns_422(self) -> None:
        rid = self._make_rule(threshold=150.0, cooldown_seconds=0)
        resp = self.client.get(
            f"/api/alert-rules/{rid}/history",
            params={"from_date": "2026-06-17", "to_date": "2026-06-16"},
        )
        assert resp.status_code == 422


class TestAlertRuleEffectiveness(_Base):
    def _make_rule(self, **kw) -> int:
        svc = AlertRuleService(self._db())
        out = svc.create(AlertRuleCreate(
            name=kw.get("name", "r"),
            symbol=kw.get("symbol", "AAPL.US"),
            rule_type=kw.get("rule_type", "price_above"),
            threshold=kw.get("threshold", 150.0),
            severity="WARNING",
            enabled=kw.get("enabled", True),
            cooldown_seconds=kw.get("cooldown_seconds", 0),
        ))
        return out.id

    def test_never_fired_rule_is_visible(self) -> None:
        rid = self._make_rule(name="quiet")
        eff = AlertRuleService(self._db()).effectiveness()
        assert len(eff) == 1
        e = eff[0]
        assert e.id == rid
        assert e.firing_count == 0
        assert e.last_fired_at is None
        assert e.never_fired is True
        assert e.enabled is True

    def test_firing_count_and_last_fired(self) -> None:
        self._make_rule(threshold=150.0, cooldown_seconds=0)
        svc = AlertRuleService(self._db())
        svc.evaluate(
            FakeRunner(FakeBroker({"AAPL.US": 160.0}), FakeNotifier()),
            now=datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc),
        )
        svc.evaluate(
            FakeRunner(FakeBroker({"AAPL.US": 160.0}), FakeNotifier()),
            now=datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc),
        )
        eff = AlertRuleService(self._db()).effectiveness()
        assert len(eff) == 1
        e = eff[0]
        assert e.firing_count == 2
        assert e.last_fired_at is not None
        # SQLite returns naive datetimes; compare against the naive instant.
        assert e.last_fired_at >= datetime(2026, 6, 17, 12, 0)
        assert e.never_fired is False

    def test_disabled_rules_included_with_enabled_state(self) -> None:
        self._make_rule(name="on", enabled=True)
        self._make_rule(name="off", enabled=False)
        eff = AlertRuleService(self._db()).effectiveness()
        assert {e.name: e.enabled for e in eff} == {"on": True, "off": False}

    def test_date_bounded_firing_count(self) -> None:
        self._make_rule(threshold=150.0, cooldown_seconds=0)
        svc = AlertRuleService(self._db())
        svc.evaluate(
            FakeRunner(FakeBroker({"AAPL.US": 160.0}), FakeNotifier()),
            now=datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc),
        )
        svc.evaluate(
            FakeRunner(FakeBroker({"AAPL.US": 160.0}), FakeNotifier()),
            now=datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc),
        )
        eff = AlertRuleService(self._db()).effectiveness(
            from_dt=datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc),
            to_dt=datetime(2026, 6, 17, 23, 59, 59, tzinfo=timezone.utc),
        )
        assert len(eff) == 1
        e = eff[0]
        assert e.firing_count == 1  # only the 16th fire is inside the window
        assert e.last_fired_at is not None  # rule.last_fired_at keeps the true last fire
        assert e.never_fired is False

    def test_effectiveness_never_fires_notifications(self) -> None:
        self._make_rule(threshold=150.0, cooldown_seconds=0)
        notifier = FakeNotifier()
        AlertRuleService(self._db()).effectiveness()  # must be side-effect free
        assert notifier.calls == []

    def test_api_effectiveness(self) -> None:
        self._make_rule(name="quiet", threshold=500.0)  # never fires at quote 160
        self._make_rule(name="loud", threshold=150.0)
        AlertRuleService(self._db()).evaluate(
            FakeRunner(FakeBroker({"AAPL.US": 160.0}), FakeNotifier()),
            now=datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc),
        )
        resp = self.client.get("/api/alert-rules/effectiveness")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 2
        by_name = {i["name"]: i for i in data["items"]}
        assert by_name["quiet"]["firing_count"] == 0
        assert by_name["quiet"]["never_fired"] is True
        assert by_name["loud"]["firing_count"] == 1
        assert by_name["loud"]["never_fired"] is False
        # deterministic order: rule id desc (loud created last -> first)
        assert [i["name"] for i in data["items"]] == ["loud", "quiet"]

    def test_api_effectiveness_date_bounds(self) -> None:
        self._make_rule(threshold=150.0, cooldown_seconds=0)
        AlertRuleService(self._db()).evaluate(
            FakeRunner(FakeBroker({"AAPL.US": 160.0}), FakeNotifier()),
            now=datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc),
        )
        resp = self.client.get(
            "/api/alert-rules/effectiveness",
            params={"from_date": "2026-06-16", "to_date": "2026-06-17"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["items"][0]["firing_count"] == 0  # fire outside window
        assert resp.json()["items"][0]["never_fired"] is False

    def test_api_effectiveness_inverted_date_range_returns_422(self) -> None:
        self._make_rule(threshold=150.0, cooldown_seconds=0)
        resp = self.client.get(
            "/api/alert-rules/effectiveness",
            params={"from_date": "2026-06-17", "to_date": "2026-06-16"},
        )
        assert resp.status_code == 422

    def test_effectiveness_never_fired_is_all_time(self) -> None:
        # never_fired must be all-time, not window-scoped. A rule that fired
        # only OUTSIDE the requested window must report never_fired=False with
        # the all-time latest firing as last_fired_at and firing_count=0 for
        # the window. This covers the legacy case where AlertRule.last_fired_at
        # is null (e.g. never populated) but AlertFiring rows exist.
        rid = self._make_rule(threshold=150.0, cooldown_seconds=0)
        # Fire once, well before the requested window.
        AlertRuleService(self._db()).evaluate(
            FakeRunner(FakeBroker({"AAPL.US": 160.0}), FakeNotifier()),
            now=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        )
        # Simulate the legacy case: clear AlertRule.last_fired_at so the only
        # evidence of firing is the AlertFiring row outside the window.
        db = self._db()
        rule = db.get(AlertRule, rid)
        assert rule is not None
        rule.last_fired_at = None
        db.commit()
        db.close()
        # Request a window that does NOT include the 2026-06-10 fire.
        eff = AlertRuleService(self._db()).effectiveness(
            from_dt=datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc),
            to_dt=datetime(2026, 6, 17, 23, 59, 59, tzinfo=timezone.utc),
        )
        assert len(eff) == 1
        e = eff[0]
        assert e.firing_count == 0  # window-scoped: no fires in [06-16, 06-17]
        assert e.never_fired is False  # all-time: a firing exists outside window
        assert e.last_fired_at is not None  # falls back to all-time latest firing
        # The all-time latest firing is 2026-06-10 12:00 (SQLite naive datetime).
        assert e.last_fired_at >= datetime(2026, 6, 10, 12, 0)

    def test_api_effectiveness_never_fired_is_all_time(self) -> None:
        # Same as above but through the API, with date-string bounds.
        rid = self._make_rule(threshold=150.0, cooldown_seconds=0)
        AlertRuleService(self._db()).evaluate(
            FakeRunner(FakeBroker({"AAPL.US": 160.0}), FakeNotifier()),
            now=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        )
        db = self._db()
        rule = db.get(AlertRule, rid)
        assert rule is not None
        rule.last_fired_at = None
        db.commit()
        db.close()
        resp = self.client.get(
            "/api/alert-rules/effectiveness",
            params={"from_date": "2026-06-16", "to_date": "2026-06-17"},
        )
        assert resp.status_code == 200, resp.text
        item = resp.json()["items"][0]
        assert item["firing_count"] == 0
        assert item["never_fired"] is False
        assert item["last_fired_at"] is not None

    def test_effectiveness_never_fired_when_truly_no_firings(self) -> None:
        # A rule with no AlertFiring rows at all (and no last_fired_at) must
        # still report never_fired=True.
        self._make_rule(name="quiet", threshold=500.0)
        eff = AlertRuleService(self._db()).effectiveness(
            from_dt=datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc),
            to_dt=datetime(2026, 6, 17, 23, 59, 59, tzinfo=timezone.utc),
        )
        assert len(eff) == 1
        e = eff[0]
        assert e.firing_count == 0
        assert e.never_fired is True
        assert e.last_fired_at is None


class TestIntervalStaleRule(_Base):
    def setup_method(self) -> None:
        super().setup_method()
        db = self._db()
        db.query(StrategyConfig).delete()
        db.commit()
        db.close()

    def _seed_interval(self, symbol: str, buy_low: float, sell_high: float) -> None:
        db = self._db()
        db.add(StrategyConfig(
            symbol=symbol,
            market="US",
            buy_low=buy_low,
            sell_high=sell_high,
        ))
        db.commit()
        db.close()

    def _rule(self, *, symbol: str = "NVDA.US", threshold: float = 5.0) -> int:
        out = AlertRuleService(self._db()).create(AlertRuleCreate(
            name="interval drift",
            symbol=symbol,
            rule_type="interval_stale",
            threshold=threshold,
            severity="WARNING",
            enabled=True,
            cooldown_seconds=0,
        ))
        return out.id

    def _evaluate(self, price: float) -> tuple[int, FakeNotifier]:
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(
            FakeRunner(FakeBroker({"NVDA.US": price}), notifier)
        )
        return result.fired, notifier

    def test_fires_when_price_far_above_interval(self) -> None:
        # The live NVDA case: interval [191, 196.5] while price traded near 210.
        self._seed_interval("NVDA.US", 191.0, 196.5)
        self._rule(threshold=5.0)
        fired, notifier = self._evaluate(210.0)
        assert fired == 1
        assert "偏离活动区间" in notifier.calls[0][1]

    def test_fires_when_price_far_below_interval(self) -> None:
        self._seed_interval("NVDA.US", 191.0, 196.5)
        self._rule(threshold=5.0)
        fired, _ = self._evaluate(170.0)
        assert fired == 1

    def test_silent_while_price_inside_interval(self) -> None:
        self._seed_interval("NVDA.US", 191.0, 196.5)
        self._rule(threshold=5.0)
        fired, notifier = self._evaluate(194.0)
        assert fired == 0
        assert notifier.calls == []

    def test_silent_when_drift_below_threshold(self) -> None:
        # 198 is outside [191, 196.5] but only ~0.76% above sell_high.
        self._seed_interval("NVDA.US", 191.0, 196.5)
        self._rule(threshold=5.0)
        fired, _ = self._evaluate(198.0)
        assert fired == 0

    def test_silent_without_matching_strategy_config(self) -> None:
        self._rule(threshold=5.0)
        fired, _ = self._evaluate(210.0)
        assert fired == 0

    def test_silent_when_quote_unavailable(self) -> None:
        self._seed_interval("NVDA.US", 191.0, 196.5)
        self._rule(threshold=5.0)
        notifier = FakeNotifier()
        result = AlertRuleService(self._db()).evaluate(
            FakeRunner(FakeBroker({}), notifier)
        )
        assert result.fired == 0

    def test_silent_when_interval_bounds_are_invalid(self) -> None:
        self._seed_interval("NVDA.US", 200.0, 190.0)
        self._rule(threshold=5.0)
        fired, _ = self._evaluate(300.0)
        assert fired == 0

    def test_does_not_modify_the_interval(self) -> None:
        self._seed_interval("NVDA.US", 191.0, 196.5)
        self._rule(threshold=5.0)
        self._evaluate(210.0)
        db = self._db()
        try:
            cfg = db.query(StrategyConfig).order_by(
                StrategyConfig.id.desc()
            ).first()
            assert cfg is not None
            assert cfg.buy_low == 191.0
            assert cfg.sell_high == 196.5
        finally:
            db.close()

    def test_records_firing_history_with_deviation_value(self) -> None:
        self._seed_interval("NVDA.US", 191.0, 196.5)
        rid = self._rule(threshold=5.0)
        self._evaluate(210.0)
        db = self._db()
        try:
            firing = db.query(AlertFiring).filter(
                AlertFiring.rule_id == rid
            ).one()
            assert firing.rule_type == "interval_stale"
            assert firing.trigger_value > 5.0
        finally:
            db.close()

    def test_rejects_blank_symbol(self) -> None:
        try:
            AlertRuleCreate(
                name="bad",
                symbol="",
                rule_type="interval_stale",
                threshold=5.0,
            )
            raise AssertionError("blank symbol must be rejected")
        except ValueError:
            pass

    def test_rejects_non_positive_threshold(self) -> None:
        for bad in (0.0, -1.0):
            try:
                AlertRuleCreate(
                    name="bad",
                    symbol="NVDA.US",
                    rule_type="interval_stale",
                    threshold=bad,
                )
                raise AssertionError("non-positive threshold must be rejected")
            except ValueError:
                pass

