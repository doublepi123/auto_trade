"""Interval recentering — keeps a stranded band tracking price, default off.

The live band is written once when the primary symbol switches and never
tracks price afterwards. A sustained trend therefore strands it: the observed
deployment ran 34 days and 40k+ quote evaluations with zero threshold
crossings, because a long-only entry needs ``price <= buy_low`` and price sat
6% above the band.

These tests pin the guards, not the arithmetic. Recentering mutates the live
strategy, so every safety boundary that ``assert_primary_switch_safe`` already
enforces must hold, and the band must never be walked along with price.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_recenter_{os.getpid()}.db"
)

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Base, StrategyConfig, TradeEvent
from app.services.interval_recenter_service import (
    EVENT_INTERVAL_RECENTERED,
    OUTCOME_BLOCKED,
    OUTCOME_DISABLED,
    OUTCOME_NO_PRIMARY,
    OUTCOME_RECENTERED,
    OUTCOME_STALE_PRICE,
    OUTCOME_WITHIN_BAND,
    IntervalRecenterService,
)


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class _Quote:
    def __init__(self, symbol: str, last_price: float, at: datetime | None) -> None:
        self.symbol = symbol
        self.last_price = last_price
        self.timestamp = at


class _Broker:
    def __init__(self, price: float | None, price_at: datetime | None) -> None:
        self.price = price
        self.price_at = price_at

    def get_quotes(self, symbols: list[str]) -> list[Any]:
        if self.price is None:
            return []
        return [_Quote(s, self.price, self.price_at) for s in symbols]


class _Runner:
    """Stands in for AppRunner: the safety gate plus the reload hook."""

    def __init__(
        self,
        *,
        price: float | None = 366.0,
        price_at: datetime | None = None,
        block: Exception | None = None,
        reload_error: Exception | None = None,
    ) -> None:
        self.block = block
        self.reload_error = reload_error
        self.broker = _Broker(price, price_at)
        self.safety_calls = 0
        self.reloads = 0

    def assert_primary_switch_safe(self, symbol: str, market: str) -> None:
        self.safety_calls += 1
        if self.block is not None:
            raise self.block

    def reload_strategy(self, db: object = None) -> None:
        self.reloads += 1
        if self.reload_error is not None and self.reloads == 1:
            raise self.reload_error


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setattr(settings, "interval_recenter_enabled", True, raising=False)
    monkeypatch.setattr(
        settings, "interval_recenter_min_drift_pct", 1.5, raising=False
    )
    monkeypatch.setattr(
        settings, "interval_recenter_max_price_age_seconds", 300, raising=False
    )
    monkeypatch.setattr(settings, "interval_recenter_max_per_day", 4, raising=False)
    monkeypatch.setattr(
        settings, "llm_interval_volatility_threshold_pct", 1.0, raising=False
    )
    yield


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
        db.query(StrategyConfig).delete()
        db.query(TradeEvent).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _seed(
        self,
        *,
        symbol: str = "TSLA.US",
        buy_low: float = 344.5447,
        sell_high: float = 351.5052,
    ) -> None:
        db = self._db()
        db.add(StrategyConfig(
            symbol=symbol, market="US", buy_low=buy_low, sell_high=sell_high,
        ))
        db.commit()
        db.close()

    def _config(self) -> StrategyConfig:
        db = self._db()
        try:
            row = db.query(StrategyConfig).order_by(
                StrategyConfig.id.desc()
            ).first()
            assert row is not None
            db.expunge(row)
            return row
        finally:
            db.close()


class TestIntervalRecenter(_Base):
    def _run(self, runner: _Runner, *, now: datetime | None = None):
        db = self._db()
        try:
            return IntervalRecenterService(
                db, clock=_Clock(now or datetime.now(timezone.utc))
            ).evaluate(runner)
        finally:
            db.close()

    # --- the failure this exists to fix ---------------------------------

    def test_recenters_a_band_price_has_drifted_above(self) -> None:
        # The live case: band 344.55-351.51 while price traded near 366.
        self._seed()
        now = datetime.now(timezone.utc)
        runner = _Runner(price=366.0, price_at=now)
        result = self._run(runner, now=now)
        assert result.outcome == OUTCOME_RECENTERED
        cfg = self._config()
        # +-1% of 366 (llm_interval_volatility_threshold_pct).
        assert cfg.buy_low == pytest.approx(362.34, abs=0.01)
        assert cfg.sell_high == pytest.approx(369.66, abs=0.01)
        # The band must now bracket price, which is the whole point.
        assert cfg.buy_low < 366.0 < cfg.sell_high

    def test_recenters_a_band_price_has_drifted_below(self) -> None:
        self._seed(buy_low=400.0, sell_high=404.0)
        now = datetime.now(timezone.utc)
        result = self._run(_Runner(price=366.0, price_at=now), now=now)
        assert result.outcome == OUTCOME_RECENTERED
        cfg = self._config()
        assert cfg.buy_low < 366.0 < cfg.sell_high

    def test_reloads_the_runner_so_the_live_engine_sees_the_new_band(self) -> None:
        # A committed row the in-memory engine never reloads changes nothing.
        self._seed()
        now = datetime.now(timezone.utc)
        runner = _Runner(price=366.0, price_at=now)
        self._run(runner, now=now)
        assert runner.reloads == 1

    # --- do not walk the band along with price --------------------------

    def test_silent_while_price_sits_inside_the_band(self) -> None:
        self._seed(buy_low=362.0, sell_high=370.0)
        now = datetime.now(timezone.utc)
        result = self._run(_Runner(price=366.0, price_at=now), now=now)
        assert result.outcome == OUTCOME_WITHIN_BAND
        cfg = self._config()
        assert cfg.buy_low == 362.0
        assert cfg.sell_high == 370.0

    def test_silent_when_drift_is_below_the_minimum(self) -> None:
        # 366 is outside [360, 365] but only ~0.27% above sell_high, far under
        # the 1.5% floor. Recentering here would chase price tick by tick.
        self._seed(buy_low=360.0, sell_high=365.0)
        now = datetime.now(timezone.utc)
        result = self._run(_Runner(price=366.0, price_at=now), now=now)
        assert result.outcome == OUTCOME_WITHIN_BAND
        cfg = self._config()
        assert cfg.sell_high == 365.0

    def test_daily_cap_bounds_how_far_the_band_can_be_walked(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings, "interval_recenter_max_per_day", 2)
        self._seed()
        now = datetime.now(timezone.utc)
        prices = [366.0, 380.0, 400.0]
        outcomes = []
        for i, px in enumerate(prices):
            runner = _Runner(price=px, price_at=now + timedelta(minutes=i))
            outcomes.append(
                self._run(runner, now=now + timedelta(minutes=i)).outcome
            )
        assert outcomes[0] == OUTCOME_RECENTERED
        assert outcomes[1] == OUTCOME_RECENTERED
        assert outcomes[2] == OUTCOME_BLOCKED
        cfg = self._config()
        # Still centred on the 2nd price, never the 3rd.
        assert cfg.buy_low < 380.0 < cfg.sell_high

    # --- safety boundary: reuse, never bypass ---------------------------

    def test_asserts_the_live_safety_gate_before_writing(self) -> None:
        self._seed()
        now = datetime.now(timezone.utc)
        runner = _Runner(price=366.0, price_at=now)
        self._run(runner, now=now)
        assert runner.safety_calls == 1

    def test_blocked_when_the_safety_gate_refuses(self) -> None:
        # A tracked position, pending order, in-flight trigger, unresolved
        # reconciliation or non-FLAT engine all surface here. Moving buy_low
        # while long would be an add-on in disguise, which P0 forbids.
        self._seed()
        now = datetime.now(timezone.utc)
        runner = _Runner(
            price=366.0,
            price_at=now,
            block=RuntimeError("positions are tracked: TSLA.US"),
        )
        result = self._run(runner, now=now)
        assert result.outcome == OUTCOME_BLOCKED
        cfg = self._config()
        assert cfg.buy_low == 344.5447
        assert cfg.sell_high == 351.5052
        assert runner.reloads == 0

    def test_stale_price_never_recenters(self) -> None:
        # Recentering onto a price the market has left reproduces the very
        # stranded band this feature exists to fix.
        self._seed()
        now = datetime.now(timezone.utc)
        runner = _Runner(price=366.0, price_at=now - timedelta(seconds=3600))
        result = self._run(runner, now=now)
        assert result.outcome == OUTCOME_STALE_PRICE
        cfg = self._config()
        assert cfg.buy_low == 344.5447

    def test_future_dated_price_never_recenters(self) -> None:
        self._seed()
        now = datetime.now(timezone.utc)
        runner = _Runner(price=366.0, price_at=now + timedelta(seconds=600))
        result = self._run(runner, now=now)
        assert result.outcome == OUTCOME_STALE_PRICE

    def test_undatable_price_never_recenters(self) -> None:
        # Fail closed: an unmeasurable age cannot be proven fresh.
        self._seed()
        now = datetime.now(timezone.utc)
        result = self._run(_Runner(price=366.0, price_at=None), now=now)
        assert result.outcome == OUTCOME_STALE_PRICE

    def test_missing_quote_never_recenters(self) -> None:
        self._seed()
        now = datetime.now(timezone.utc)
        result = self._run(_Runner(price=None, price_at=now), now=now)
        assert result.outcome == OUTCOME_STALE_PRICE

    def test_non_positive_price_never_recenters(self) -> None:
        self._seed()
        now = datetime.now(timezone.utc)
        result = self._run(_Runner(price=0.0, price_at=now), now=now)
        assert result.outcome == OUTCOME_STALE_PRICE

    # --- default off, and rollback --------------------------------------

    def test_disabled_by_default(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "interval_recenter_enabled", False)
        self._seed()
        now = datetime.now(timezone.utc)
        runner = _Runner(price=366.0, price_at=now)
        result = self._run(runner, now=now)
        assert result.outcome == OUTCOME_DISABLED
        assert runner.safety_calls == 0
        cfg = self._config()
        assert cfg.buy_low == 344.5447

    def test_no_primary_configured(self) -> None:
        now = datetime.now(timezone.utc)
        result = self._run(_Runner(price=366.0, price_at=now), now=now)
        assert result.outcome == OUTCOME_NO_PRIMARY

    def test_reload_failure_rolls_the_band_back(self) -> None:
        # A band the engine could not load must not stay committed, or the
        # persisted config and the live engine disagree about where entries are.
        self._seed()
        now = datetime.now(timezone.utc)
        runner = _Runner(
            price=366.0,
            price_at=now,
            reload_error=RuntimeError("reload failed"),
        )
        with pytest.raises(RuntimeError):
            self._run(runner, now=now)
        cfg = self._config()
        assert cfg.buy_low == pytest.approx(344.5447)
        assert cfg.sell_high == pytest.approx(351.5052)
        assert runner.reloads == 2  # failed apply, then the rollback reload

    # --- provenance ------------------------------------------------------

    def test_records_a_durable_event_with_both_bands(self) -> None:
        self._seed()
        now = datetime.now(timezone.utc)
        self._run(_Runner(price=366.0, price_at=now), now=now)
        db = self._db()
        try:
            ev = db.query(TradeEvent).filter(
                TradeEvent.event_type == EVENT_INTERVAL_RECENTERED
            ).one()
            assert ev.symbol == "TSLA.US"
            from app.services.trade_event_service import decode_event_payload
            payload = decode_event_payload(ev.payload_json)
            assert payload["previous_buy_low"] == pytest.approx(344.5447)
            assert payload["new_buy_low"] == pytest.approx(362.34, abs=0.01)
            assert payload["reference_price"] == pytest.approx(366.0)
            assert payload["drift_pct"] > 1.5
        finally:
            db.close()

    def test_never_submits_an_order(self) -> None:
        # The service must have no broker mutation surface at all.
        self._seed()
        now = datetime.now(timezone.utc)
        runner = _Runner(price=366.0, price_at=now)
        self._run(runner, now=now)
        assert not hasattr(runner, "submitted")
        import app.services.interval_recenter_service as mod
        module_path = mod.__file__
        assert module_path is not None
        with open(module_path, encoding="utf-8") as handle:
            src = handle.read()
        for forbidden in ("submit_limit_order", "submit_order", "place_order"):
            assert forbidden not in src


class TestIntervalRecenterConfigGuards:
    def test_defaults_are_fail_closed(self) -> None:
        from app.config import Settings
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.interval_recenter_enabled is False
        assert s.interval_recenter_min_drift_pct > 0
        assert s.interval_recenter_max_price_age_seconds > 0
        assert s.interval_recenter_max_per_day >= 1
