"""Capital efficiency service — regression coverage against the new API.

``CapitalEfficiencyService.analyze`` consumes quality-gated closed round trips
loaded via ``load_analytics_trade_sample`` (FIFO-paired ``ClosedRoundTrip``
objects), not a raw ``(filled_at, net_pnl, quantity)`` tuple cursor. A prior
local version unpacked a 3-tuple as 2-tuples in several places, which raised
``ValueError: too many values to unpack`` (and would have mis-assigned the
datetime to ``quantity`` even if it hadn't). The tuple model is gone; these
tests pin the corrected contract against the live service: analysis must not
raise, must report the sample size and total PnL, and must derive turnover and
entry notional from the round-trip trade prices/quantities (not a fixed
``abs(qty) * 100`` approximation).

Seeds real BUY->SELL FIFO round trips so ``DailyPnlService`` pairs them into
``ClosedRoundTrip`` objects the analytics loader accepts. Per-module sqlite
(isolated, deterministic). Read-only service — no orders.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, OrderRecord, StrategyConfig
from app.services.capital_efficiency_service import CapitalEfficiencyService


def _order(
    *,
    broker_order_id: str,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    filled_at: datetime,
) -> OrderRecord:
    return OrderRecord(
        broker_order_id=broker_order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        executed_quantity=quantity,
        price=price,
        executed_price=price,
        status="FILLED",
        created_at=filled_at,
        filled_at=filled_at,
    )


def _roundtrip(
    *,
    buy_id: str,
    sell_id: str,
    symbol: str,
    qty: float,
    entry_price: float,
    exit_price: float,
    entry_at: datetime,
    holding_seconds: int = 3600,
) -> tuple[OrderRecord, OrderRecord]:
    exit_at = entry_at + timedelta(seconds=holding_seconds)
    return (
        _order(
            broker_order_id=buy_id,
            symbol=symbol,
            side="BUY",
            quantity=qty,
            price=entry_price,
            filled_at=entry_at,
        ),
        _order(
            broker_order_id=sell_id,
            symbol=symbol,
            side="SELL",
            quantity=qty,
            price=exit_price,
            filled_at=exit_at,
        ),
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
        db.query(StrategyConfig).delete()
        # Zero fees so net_pnl == gross_pnl and assertions are deterministic.
        db.add(StrategyConfig(fee_rate_us=0.0, fee_rate_hk=0.0))
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)


class TestCapitalEfficiencyInsufficientSamples(_Base):
    def test_below_five_closed_returns_error_contract(self) -> None:
        db = self._db()
        now = datetime.now(timezone.utc)
        for i in range(4):
            db.add_all(
                _roundtrip(
                    buy_id=f"b{i}",
                    sell_id=f"s{i}",
                    symbol="AAPL.US",
                    qty=10,
                    entry_price=100.0,
                    exit_price=110.0,
                    entry_at=now - timedelta(days=i + 1),
                )
            )
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
    """Five+ closed round trips: analysis must not raise and must compute."""

    def _seed_five(self) -> datetime:
        # 5 round trips: 3 winners (qty 10, 100->110, +100 each) and
        # 2 losers (qty 5, 100->96, -20 each). Spread exits across 3 distinct
        # US market-local days so exit_active_days is observable.
        #
        # Exits are pinned to 14:00 UTC (10:00 ET, safely inside the US market
        # day) on 3 distinct recent calendar dates, so the market-local day
        # mapping never crosses a date boundary regardless of when the suite
        # runs. Entries sit 1 hour before each exit (holding_seconds=3600).
        now = datetime.now(timezone.utc)
        # Anchor to 14:00 UTC today, then step back whole days.
        base = now.replace(hour=14, minute=0, second=0, microsecond=0)
        # If 14:00 UTC today is in the future, start from yesterday so exits
        # stay in the trailing window and in the past.
        if base >= now:
            base = base - timedelta(days=1)
        day_a = base  # most recent
        day_b = base - timedelta(days=1)
        day_c = base - timedelta(days=2)

        db = self._db()
        rows: list[OrderRecord] = []
        # day A: winner 1
        rows.extend(
            _roundtrip(
                buy_id="w1b",
                sell_id="w1s",
                symbol="AAPL.US",
                qty=10,
                entry_price=100.0,
                exit_price=110.0,
                entry_at=day_a - timedelta(seconds=3600),
            )
        )
        # day B: winner 2 + loser 1
        rows.extend(
            _roundtrip(
                buy_id="w2b",
                sell_id="w2s",
                symbol="AAPL.US",
                qty=10,
                entry_price=100.0,
                exit_price=110.0,
                entry_at=day_b - timedelta(seconds=3600),
            )
        )
        rows.extend(
            _roundtrip(
                buy_id="l1b",
                sell_id="l1s",
                symbol="AAPL.US",
                qty=5,
                entry_price=100.0,
                exit_price=96.0,
                entry_at=day_b - timedelta(seconds=3600, minutes=30),
            )
        )
        # day C: winner 3 + loser 2
        rows.extend(
            _roundtrip(
                buy_id="w3b",
                sell_id="w3s",
                symbol="AAPL.US",
                qty=10,
                entry_price=100.0,
                exit_price=110.0,
                entry_at=day_c - timedelta(seconds=3600),
            )
        )
        rows.extend(
            _roundtrip(
                buy_id="l2b",
                sell_id="l2s",
                symbol="AAPL.US",
                qty=5,
                entry_price=100.0,
                exit_price=96.0,
                entry_at=day_c - timedelta(seconds=3600, minutes=30),
            )
        )
        db.add_all(rows)
        db.commit()
        db.close()
        return now

    def test_analysis_does_not_raise(self) -> None:
        # Regression: previously raised ValueError unpacking the 3-tuple.
        self._seed_five()
        out = CapitalEfficiencyService(self._db()).analyze()
        assert "error" not in out

    def test_sample_size_and_total_pnl(self) -> None:
        self._seed_five()
        out = CapitalEfficiencyService(self._db()).analyze()

        assert out["sample_size"] == 5
        # 3*100 + 2*(-20) = 300 - 40 = 260 (zero fees -> net == gross)
        assert out["total_pnl"] == 260.0

    def test_turnover_uses_trade_prices_not_fixed_notional(self) -> None:
        # Regression: turnover must use quantity * (entry + exit) per round
        # trip, not the former abs(qty) * 100 approximation.
        #   winners: 3 * (10 * (100 + 110)) = 3 * 2100 = 6300
        #   losers:  2 * (5  * (100 + 96))  = 2 * 980  = 1960
        #   total_traded_value = 8260; turnover = 8260 / 10000 = 0.826
        # The service rounds turnover to 2 decimals -> 0.83.
        self._seed_five()
        out = CapitalEfficiencyService(self._db()).analyze()

        assert out["turnover_ratio"] == 0.83
        # pnl_per_unit_traded = 260 / 8260 (rounded to 6 decimals)
        assert out["pnl_per_unit_traded"] == round(260.0 / 8260.0, 6)

    def test_entry_notional_and_winner_share(self) -> None:
        # total_entry_notional = 3*(10*100) + 2*(5*100) = 3000 + 1000 = 4000
        # winning_entry_notional = 3*(10*100) = 3000; share = 3000/4000 = 0.75
        self._seed_five()
        out = CapitalEfficiencyService(self._db()).analyze()

        assert out["total_entry_notional"] == 4000.0
        assert out["winning_entry_notional_share"] == 0.75

    def test_exit_active_days_counted_from_exit_at(self) -> None:
        # Regression: exit_active_days must come from the round-trip exit
        # market-local day, not a quantity column a buggy unpack would have
        # clobbered. Exits span 3 distinct days (1, 2, 3 days ago).
        self._seed_five()
        out = CapitalEfficiencyService(self._db()).analyze()

        assert out["exit_active_days"] == 3
        assert out["exit_active_day_rate"] == round(3 / 180, 4)

    def test_capital_time_utilization_is_separate_from_exit_days(self) -> None:
        # utilization_rate now mirrors capital_time_utilization_rate (capital
        # deployed over the window), not the former exit-date ratio.
        self._seed_five()
        out = CapitalEfficiencyService(self._db()).analyze()

        assert out["utilization_rate"] == out["capital_time_utilization_rate"]
        # exit_active_day_rate is the preserved date-count metric under its
        # explicit name; it must NOT equal utilization_rate here.
        assert out["exit_active_day_rate"] != out["utilization_rate"]

    def test_symbol_filter_scopes_round_trips(self) -> None:
        now = datetime.now(timezone.utc)
        db = self._db()
        aapl_rows: list[OrderRecord] = []
        for i in range(5):
            aapl_rows.extend(
                _roundtrip(
                    buy_id=f"a{i}b",
                    sell_id=f"a{i}s",
                    symbol="AAPL.US",
                    qty=10,
                    entry_price=100.0,
                    exit_price=110.0,
                    entry_at=now - timedelta(days=i + 1, hours=2),
                )
            )
        db.add_all(aapl_rows)
        # One MSFT round trip — below the 5-row floor when filtered.
        db.add_all(
            _roundtrip(
                buy_id="m1b",
                sell_id="m1s",
                symbol="MSFT.US",
                qty=10,
                entry_price=200.0,
                exit_price=190.0,
                entry_at=now - timedelta(days=1, hours=2),
            )
        )
        db.commit()
        db.close()

        aapl = CapitalEfficiencyService(self._db()).analyze(symbol="AAPL.US")
        assert aapl["sample_size"] == 5
        assert aapl["symbol"] == "AAPL.US"
        # 5 * (10 * 10) = 500 (zero fees)
        assert aapl["total_pnl"] == 500.0

        msft = CapitalEfficiencyService(self._db()).analyze(symbol="MSFT.US")
        assert msft["sample_size"] == 1
        assert "error" in msft  # below the 5-row floor