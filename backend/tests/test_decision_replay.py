"""Trade decision replay (GET /api/decision-replay/*). Per-file sqlite."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_decision_replay_{os.getpid()}.db"
)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import Base, OrderRecord, StrategyConfig, TradeEvent
from app.services.decision_replay_service import DecisionReplayService


def _dt(day: date, hour: int = 10, minute: int = 0, second: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute, second), tzinfo=timezone.utc)


def _order(
    oid: str, symbol: str, side: str, qty: float, price: float, day: date, hour: int = 10
) -> OrderRecord:
    return OrderRecord(
        broker_order_id=oid,
        symbol=symbol,
        side=side,
        quantity=qty,
        price=price,
        executed_quantity=qty,
        executed_price=price,
        status="FILLED",
        created_at=_dt(day, hour),
        filled_at=_dt(day, hour, 1),
    )


def _event(
    *,
    event_type: str,
    symbol: str,
    created_at: datetime,
    status: str = "",
    message: str = "",
    payload: dict[str, Any] | None = None,
) -> TradeEvent:
    return TradeEvent(
        event_type=event_type,
        symbol=symbol,
        status=status,
        message=message,
        payload_json=json.dumps(payload or {}),
        created_at=created_at,
    )


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
        db.query(TradeEvent).delete()
        db.query(OrderRecord).delete()
        db.query(StrategyConfig).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _ensure_config(self) -> None:
        db = self._db()
        db.add(StrategyConfig(symbol="AAPL.US", market="US", fee_rate_us=0.0, fee_rate_hk=0.0))
        db.commit()
        db.close()

    def _seed_round_trip(
        self, *, entry_oid: str, exit_oid: str, qty: float, entry_price: float,
        exit_price: float, entry_day: date, exit_day: date,
    ) -> tuple[int, int]:
        """Seed a BUY/SELL round trip and return (entry_order_id, exit_order_id)."""
        db = self._db()
        buy = _order(entry_oid, "AAPL.US", "BUY", qty, entry_price, entry_day)
        sell = _order(exit_oid, "AAPL.US", "SELL", qty, exit_price, exit_day)
        db.add_all([buy, sell])
        db.commit()
        entry_id, exit_id = buy.id, sell.id
        db.close()
        return entry_id, exit_id


class TestReplayNotFound(_Base):
    def test_nonexistent_trade_returns_none_and_404(self) -> None:
        self._ensure_config()
        # No round trip seeded.
        assert DecisionReplayService(self._db()).replay_trade(999999) is None

        resp = self.client.get("/api/decision-replay/trade/999999")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "trade not found"

    def test_invalid_id_returns_none(self) -> None:
        self._ensure_config()
        assert DecisionReplayService(self._db()).replay_trade(0) is None
        assert DecisionReplayService(self._db()).replay_trade(-1) is None


class TestReplayTimeline(_Base):
    def test_replay_returns_correct_timeline_and_metrics(self) -> None:
        self._ensure_config()
        entry_day = date(2026, 1, 10)
        exit_day = date(2026, 1, 12)
        entry_id, exit_id = self._seed_round_trip(
            entry_oid="buy1", exit_oid="sell1", qty=100.0,
            entry_price=10.0, exit_price=11.0,
            entry_day=entry_day, exit_day=exit_day,
        )
        # Seed a chronological decision timeline spanning entry-5m .. exit+5m.
        # Entry fill lands at 10:01 (see _order: filled_at = hour:01), exit fill
        # lands at 10:01 on the exit day, so the window is roughly
        # [entry_day 09:56, exit_day 10:06].
        events = [
            _event(
                event_type="PRICE_UPDATE", symbol="AAPL.US",
                created_at=_dt(entry_day, 9, 57),
                payload={"price": 9.98},
            ),
            _event(
                event_type="LLM_ANALYSIS", symbol="AAPL.US",
                created_at=_dt(entry_day, 9, 58),
                payload={"recommendation": "BUY", "confidence": 0.8},
            ),
            _event(
                event_type="RISK_CHECK", symbol="AAPL.US",
                created_at=_dt(entry_day, 9, 59),
                payload={"passed": True},
            ),
            _event(
                event_type="ORDER_SUBMITTED", symbol="AAPL.US",
                created_at=_dt(entry_day, 10, 0),
                status="SUBMITTED",
            ),
            _event(
                event_type="ORDER_FILLED", symbol="AAPL.US",
                created_at=_dt(entry_day, 10, 0, 30),
                status="FILLED",
                payload={"fill_price": 10.0},
            ),
            _event(
                event_type="PRICE_UPDATE", symbol="AAPL.US",
                created_at=_dt(exit_day, 10, 2),
                payload={"price": 11.05},
            ),
            _event(
                event_type="TRADE_CLOSE", symbol="AAPL.US",
                created_at=_dt(exit_day, 10, 4),
                status="CLOSED",
                message="target reached",
            ),
        ]
        # Plus an out-of-window event that must NOT appear in the timeline.
        out_of_window = _event(
            event_type="PRICE_UPDATE", symbol="AAPL.US",
            created_at=_dt(date(2026, 2, 1), 10, 0),
            payload={"price": 99.0},
        )
        # Plus an event for a different symbol inside the window (must be excluded).
        other_symbol = _event(
            event_type="ORDER_FILLED", symbol="TSLA.US",
            created_at=_dt(entry_day, 10, 0, 30),
        )
        db = self._db()
        for ev in events + [out_of_window, other_symbol]:
            db.add(ev)
        db.commit()
        db.close()

        svc = DecisionReplayService(self._db())
        # Addressable by BOTH entry and exit order id.
        for trade_id in (entry_id, exit_id):
            replay = svc.replay_trade(trade_id)
            assert replay is not None
            assert replay["symbol"] == "AAPL.US"
            assert replay["market"] == "US"
            assert replay["side"] == "long"
            assert replay["entry_price"] == 10.0
            assert replay["exit_price"] == 11.0
            # Net PnL = (11 - 10) * 100 - fees(0) = 100.
            assert replay["pnl"] == 100.0
            assert replay["entry_time"].startswith("2026-01-10")
            assert replay["exit_time"].startswith("2026-01-12")
            # 7 in-window events; out-of-window and other-symbol excluded.
            assert len(replay["timeline"]) == 7
            types = [entry["event_type"] for entry in replay["timeline"]]
            assert types == [
                "PRICE_UPDATE", "LLM_ANALYSIS", "RISK_CHECK",
                "ORDER_SUBMITTED", "ORDER_FILLED", "PRICE_UPDATE", "TRADE_CLOSE",
            ]
            # Timeline is chronological by timestamp.
            timestamps = [entry["timestamp"] for entry in replay["timeline"]]
            assert timestamps == sorted(timestamps)
            # Payload summary is JSON-safe and truncated.
            llm_entry = next(
                e for e in replay["timeline"] if e["event_type"] == "LLM_ANALYSIS"
            )
            assert llm_entry["payload_summary"]["recommendation"] == "BUY"

    def test_replay_endpoint_returns_full_timeline(self) -> None:
        self._ensure_config()
        entry_id, _ = self._seed_round_trip(
            entry_oid="b2", exit_oid="s2", qty=100.0,
            entry_price=10.0, exit_price=10.5,
            entry_day=date(2026, 1, 5), exit_day=date(2026, 1, 7),
        )
        db = self._db()
        db.add(_event(
            event_type="ORDER_FILLED", symbol="AAPL.US",
            created_at=_dt(date(2026, 1, 5), 10, 0),
            status="FILLED",
        ))
        db.commit()
        db.close()

        resp = self.client.get(f"/api/decision-replay/trade/{entry_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["symbol"] == "AAPL.US"
        assert body["market"] == "US"
        assert len(body["timeline"]) == 1
        assert body["timeline"][0]["event_type"] == "ORDER_FILLED"

    def test_market_inferred_from_hk_symbol_suffix(self) -> None:
        self._ensure_config()
        # Seed an HK round trip directly (config fee rate applies per market).
        db = self._db()
        buy = _order("hb", "0700.HK", "BUY", 100.0, 100.0, date(2026, 1, 1))
        sell = _order("hs", "0700.HK", "SELL", 100.0, 101.0, date(2026, 1, 3))
        db.add_all([buy, sell])
        db.commit()
        sell_id = sell.id  # capture before the session closes
        db.close()
        replay = DecisionReplayService(self._db()).replay_trade(sell_id)
        assert replay is not None
        assert replay["symbol"] == "0700.HK"
        assert replay["market"] == "HK"


class TestListReplayableTrades(_Base):
    def test_list_orders_by_exit_time_descending(self) -> None:
        self._ensure_config()
        _, early_exit = self._seed_round_trip(
            entry_oid="eb", exit_oid="es", qty=100.0,
            entry_price=10.0, exit_price=11.0,
            entry_day=date(2026, 1, 1), exit_day=date(2026, 1, 3),
        )
        _, late_exit = self._seed_round_trip(
            entry_oid="lb", exit_oid="ls", qty=100.0,
            entry_price=10.0, exit_price=12.0,
            entry_day=date(2026, 1, 10), exit_day=date(2026, 1, 14),
        )
        rows = DecisionReplayService(self._db()).list_replayable_trades(limit=10)
        assert len(rows) == 2
        # Most-recent exit first.
        assert rows[0]["exit_time"] > rows[1]["exit_time"]
        assert rows[0]["exit_order_id"] == late_exit
        assert rows[1]["exit_order_id"] == early_exit

    def test_list_filters_by_symbol(self) -> None:
        self._ensure_config()
        self._seed_round_trip(
            entry_oid="ab", exit_oid="as", qty=100.0,
            entry_price=10.0, exit_price=11.0,
            entry_day=date(2026, 1, 1), exit_day=date(2026, 1, 3),
        )
        db = self._db()
        db.add_all([
            _order("tb", "TSLA.US", "BUY", 100.0, 20.0, date(2026, 1, 1)),
            _order("ts", "TSLA.US", "SELL", 100.0, 21.0, date(2026, 1, 3)),
        ])
        db.commit()
        db.close()
        rows = DecisionReplayService(self._db()).list_replayable_trades(
            limit=10, symbol="TSLA.US"
        )
        assert len(rows) == 1
        assert rows[0]["symbol"] == "TSLA.US"

    def test_list_respects_limit(self) -> None:
        self._ensure_config()
        for i in range(5):
            self._seed_round_trip(
                entry_oid=f"lb{i}", exit_oid=f"ls{i}", qty=100.0,
                entry_price=10.0, exit_price=10.5,
                entry_day=date(2026, 1, 1 + i), exit_day=date(2026, 1, 3 + i),
            )
        rows = DecisionReplayService(self._db()).list_replayable_trades(limit=3)
        assert len(rows) == 3

    def test_list_event_count_reflects_window(self) -> None:
        self._ensure_config()
        _, exit_id = self._seed_round_trip(
            entry_oid="cb", exit_oid="cs", qty=100.0,
            entry_price=10.0, exit_price=11.0,
            entry_day=date(2026, 1, 1), exit_day=date(2026, 1, 3),
        )
        db = self._db()
        # Two events inside the trade window (entry-5m .. exit+5m) ...
        db.add(_event(
            event_type="ORDER_FILLED", symbol="AAPL.US",
            created_at=_dt(date(2026, 1, 1), 10, 0),
        ))
        db.add(_event(
            event_type="TRADE_CLOSE", symbol="AAPL.US",
            created_at=_dt(date(2026, 1, 3), 10, 0),
        ))
        # ... and one well outside.
        db.add(_event(
            event_type="PRICE_UPDATE", symbol="AAPL.US",
            created_at=_dt(date(2026, 2, 1), 10, 0),
        ))
        db.commit()
        db.close()
        rows = DecisionReplayService(self._db()).list_replayable_trades(limit=10)
        assert len(rows) == 1
        assert rows[0]["event_count"] == 2
        assert rows[0]["trade_id"] == exit_id

    def test_list_endpoint(self) -> None:
        self._ensure_config()
        _, exit_id = self._seed_round_trip(
            entry_oid="pb", exit_oid="ps", qty=100.0,
            entry_price=10.0, exit_price=11.0,
            entry_day=date(2026, 1, 1), exit_day=date(2026, 1, 3),
        )
        resp = self.client.get("/api/decision-replay/trades")
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["trade_id"] == exit_id
        assert rows[0]["symbol"] == "AAPL.US"
        assert "timeline" not in rows[0]  # list view is summary-only

    def test_empty_list_when_no_trades(self) -> None:
        self._ensure_config()
        resp = self.client.get("/api/decision-replay/trades")
        assert resp.status_code == 200, resp.text
        assert resp.json() == []