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
from app.models import AlertFiring, AlertRule, Base, RuntimeState
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

