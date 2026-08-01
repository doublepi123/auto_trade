"""Capital efficiency service — regression coverage for the 3-tuple unpack.

``CapitalEfficiencyService._fetch`` returns ``(filled_at, net_pnl, quantity)``.
A prior version of ``analyze()`` unpacked that 3-tuple as 2-tuples in several
places, which raised ``ValueError: too many values to unpack`` (and would have
mis-assigned the datetime to ``quantity`` even if it hadn't). These tests pin
the corrected shape: analysis must not raise, must report the sample size and
total PnL, and must derive active days and quantity-based traded value from the
right column.

Per-module sqlite (isolated, deterministic). Read-only service — no orders.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, OrderRecord
from app.services.capital_efficiency_service import CapitalEfficiencyService


def _dt(days_ago: int, hour: int = 10) -> datetime:
    """A deterministic UTC timestamp ``days_ago`` days before now."""
    return datetime.now(timezone.utc) - timedelta(days=days_ago, hours=24 - hour)


def _closed(
    oid: str,
    symbol: str,
    qty: float,
    pnl: float,
    filled_at: datetime,
) -> OrderRecord:
    """A filled, PnL-bearing order row (the shape ``_fetch`` selects)."""
    return OrderRecord(
        broker_order_id=oid,
        symbol=symbol,
        side="SELL",
        quantity=qty,
        price=100.0,
        executed_quantity=qty,
        executed_price=100.0,
        status="FILLED",
        filled_at=filled_at,
        net_pnl=pnl,
    )


class _Base:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_cap_eff_{os.getpid()}.db",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.drop_all(bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)

    @classmethod
    def teardown_class(cls) -> None:
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setup_method(self) -> None:
        db = Session(bind=self.engine)
        db.query(OrderRecord).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)


class TestCapitalEfficiencyInsufficientSamples(_Base):
    def test_below_five_closed_returns_error_contract(self) -> None:
        db = self._db()
        for i in range(4):
            db.add(_closed(f"b{i}", "AAPL.US", 10, 5.0, _dt(i)))
        db.commit()
        db.close()

        out = CapitalEfficiencyService(self._db()).analyze()

        assert out["symbol"] == "ALL"
        assert out["sample_size"] == 4
        assert out["error"] == "Need at least 5 closed trades."
        # The error contract carries no metrics keys.
        assert "total_pnl" not in out
        assert "return_on_capital" not in out


class TestCapitalEfficiencyAnalysis(_Base):
    """Five+ closed rows: analysis must not raise and must compute correctly."""

    def _seed_five(self) -> None:
        # 5 closed rows: 3 winners (+50 each, qty 10) and 2 losers (-20 each, qty 5).
        # Spread across 3 distinct days so active_days is observable.
        db = self._db()
        rows = [
            _closed("w1", "AAPL.US", 10, 50.0, _dt(0)),   # day A
            _closed("w2", "AAPL.US", 10, 50.0, _dt(1)),   # day B
            _closed("l1", "AAPL.US", 5, -20.0, _dt(1)),   # day B
            _closed("w3", "AAPL.US", 10, 50.0, _dt(2)),   # day C
            _closed("l2", "AAPL.US", 5, -20.0, _dt(2)),   # day C
        ]
        db.add_all(rows)
        db.commit()
        db.close()

    def test_analysis_does_not_raise(self) -> None:
        # Regression: previously raised ValueError unpacking the 3-tuple.
        self._seed_five()
        out = CapitalEfficiencyService(self._db()).analyze()
        assert "error" not in out

    def test_sample_size_and_total_pnl(self) -> None:
        self._seed_five()
        out = CapitalEfficiencyService(self._db()).analyze()

        assert out["sample_size"] == 5
        # 3*50 + 2*(-20) = 150 - 40 = 110
        assert out["total_pnl"] == 110.0

    def test_active_days_counted_from_filled_at(self) -> None:
        # Regression: active_days must come from filled_at (col 0), not from
        # the quantity column that a buggy unpack would have clobbered.
        self._seed_five()
        out = CapitalEfficiencyService(self._db()).analyze()

        # Rows span 3 distinct calendar days (days 0, 1, 2 ago).
        assert out["active_days"] == 3
        assert out["utilization_rate"] == round(3 / 180, 4)

    def test_traded_value_derived_from_quantity(self) -> None:
        # Regression: total_traded_value must use quantity (col 2), not the
        # datetime a 2-tuple unpack would have assigned to ``qty``.
        self._seed_five()
        out = CapitalEfficiencyService(self._db()).analyze()

        # notional = abs(qty) * 100 per row:
        #   3 winners * (10 * 100) = 3000
        #   2 losers  * (5  * 100) = 1000
        # total = 4000; turnover = 4000 / 10000 = 0.4
        assert out["turnover_ratio"] == 0.4
        # pnl_per_unit_traded = 110 / 4000 = 0.0275
        assert out["pnl_per_unit_traded"] == 0.0275

    def test_winner_loser_capital_split(self) -> None:
        self._seed_five()
        out = CapitalEfficiencyService(self._db()).analyze()

        # win_capital  = 3 * (10 * 100) = 3000
        # loss_capital = 2 * (5  * 100) = 1000
        # capital_efficiency = 3000 / (3000 + 1000) = 0.75
        assert out["capital_efficiency"] == 0.75

    def test_symbol_filter_scopes_rows(self) -> None:
        db = self._db()
        db.add_all([
            _closed("a1", "AAPL.US", 10, 50.0, _dt(0)),
            _closed("a2", "AAPL.US", 10, 50.0, _dt(1)),
            _closed("a3", "AAPL.US", 10, 50.0, _dt(2)),
            _closed("a4", "AAPL.US", 10, 50.0, _dt(3)),
            _closed("a5", "AAPL.US", 10, 50.0, _dt(4)),
            _closed("m1", "MSFT.US", 10, -10.0, _dt(0)),
        ])
        db.commit()
        db.close()

        aapl = CapitalEfficiencyService(self._db()).analyze(symbol="AAPL.US")
        assert aapl["sample_size"] == 5
        assert aapl["symbol"] == "AAPL.US"
        assert aapl["total_pnl"] == 250.0

        msft = CapitalEfficiencyService(self._db()).analyze(symbol="MSFT.US")
        assert msft["sample_size"] == 1
        assert "error" in msft  # below the 5-row floor