"""Range-strategy fitness — read-only aggregation over shadow evidence."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_range_fitness_{os.getpid()}.db"
)

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, StrategyConfig, StrategyV2ShadowDecision
from app.services.range_fitness_service import (
    RangeFitnessService,
    VERDICT_INSUFFICIENT_DATA,
    VERDICT_MIXED,
    VERDICT_RANGE_SUITABLE,
    VERDICT_TREND_UNSUITABLE,
)

TREND = json.dumps(["ADX_REGIME_BLOCKED"])
OTHER = json.dumps(["ZSCORE_5M_NOT_OVERSOLD"])


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
        db.query(StrategyConfig).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _seed(
        self,
        symbol: str,
        *,
        trend_bars: int = 0,
        other_bars: int = 0,
        passed_bars: int = 0,
        adx: float | None = None,
        age_days: float = 0.0,
    ) -> None:
        db = self._db()
        base = datetime.now(timezone.utc) - timedelta(days=age_days)
        seq = 0
        for reasons, count, passed in (
            (TREND, trend_bars, False),
            (OTHER, other_bars, False),
            ("[]", passed_bars, True),
        ):
            for _ in range(count):
                seq += 1
                db.add(StrategyV2ShadowDecision(
                    idempotency_key=f"{symbol}-{age_days}-{seq}",
                    symbol=symbol,
                    config_version="v1",
                    session_date=base.date(),
                    bar_at=base - timedelta(minutes=seq),
                    action="WAIT",
                    gate_passed=passed,
                    gate_reasons_json=reasons,
                    adx_5m=adx,
                ))
        db.commit()
        db.close()

    def _row(self, symbol: str, **kwargs):
        rows = RangeFitnessService(self._db()).assess(**kwargs)
        for row in rows:
            if row.symbol == symbol:
                return row
        raise AssertionError(f"{symbol} missing from result")


class TestRangeFitnessVerdicts(_Base):
    def test_sustained_trend_is_unsuitable(self) -> None:
        self._seed("NVDA.US", trend_bars=90, other_bars=10, adx=41.6)
        row = self._row("NVDA.US", min_samples=60)
        assert row.verdict == VERDICT_TREND_UNSUITABLE
        assert row.trend_blocked == 90
        assert row.trend_blocked_pct == 90.0
        assert row.avg_adx_5m == 41.6

    def test_mostly_range_is_suitable(self) -> None:
        self._seed("AAPL.US", trend_bars=10, other_bars=50, passed_bars=40)
        row = self._row("AAPL.US", min_samples=60)
        assert row.verdict == VERDICT_RANGE_SUITABLE
        assert row.gate_passed == 40

    def test_middle_band_is_mixed(self) -> None:
        self._seed("AMD.US", trend_bars=45, other_bars=55)
        row = self._row("AMD.US", min_samples=60)
        assert row.verdict == VERDICT_MIXED

    def test_thin_sample_is_insufficient(self) -> None:
        self._seed("MU.US", trend_bars=5)
        row = self._row("MU.US", min_samples=60)
        assert row.verdict == VERDICT_INSUFFICIENT_DATA
        assert row.samples == 5


class TestRangeFitnessScoping(_Base):
    def test_excludes_bars_outside_lookback(self) -> None:
        self._seed("NVDA.US", trend_bars=80, age_days=10)
        rows = RangeFitnessService(self._db()).assess(lookback_days=3)
        assert all(row.symbol != "NVDA.US" for row in rows)

    def test_marks_primary_symbol_and_sorts_it_first(self) -> None:
        db = self._db()
        db.add(StrategyConfig(symbol="NVDA.US", market="US"))
        db.commit()
        db.close()
        self._seed("AAPL.US", other_bars=80)
        self._seed("NVDA.US", trend_bars=80)

        rows = RangeFitnessService(self._db()).assess(min_samples=60)
        assert rows[0].symbol == "NVDA.US"
        assert rows[0].is_primary is True
        assert all(not r.is_primary for r in rows[1:])

    def test_ignores_unrelated_gate_reasons(self) -> None:
        self._seed("TSLA.US", other_bars=80)
        row = self._row("TSLA.US", min_samples=60)
        assert row.trend_blocked == 0
        assert row.verdict == VERDICT_RANGE_SUITABLE

    def test_tolerates_malformed_reason_payload(self) -> None:
        db = self._db()
        now = datetime.now(timezone.utc)
        for i, payload in enumerate(("not-json", "{}", "")):
            db.add(StrategyV2ShadowDecision(
                idempotency_key=f"BAD.US-malformed-{i}",
                symbol="BAD.US",
                config_version="v1",
                session_date=now.date(),
                bar_at=now - timedelta(minutes=i + 1),
                action="WAIT",
                gate_passed=False,
                gate_reasons_json=payload,
            ))
        db.commit()
        db.close()
        row = self._row("BAD.US", min_samples=1)
        assert row.samples == 3
        assert row.trend_blocked == 0

    def test_handles_missing_adx(self) -> None:
        self._seed("NFLX.US", trend_bars=70, adx=None)
        row = self._row("NFLX.US", min_samples=60)
        assert row.avg_adx_5m is None

    def test_empty_evidence_returns_empty(self) -> None:
        assert RangeFitnessService(self._db()).assess() == []


class TestRangeFitnessValidation(_Base):
    def test_rejects_non_positive_lookback(self) -> None:
        svc = RangeFitnessService(self._db())
        try:
            svc.assess(lookback_days=0)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    def test_rejects_non_positive_min_samples(self) -> None:
        svc = RangeFitnessService(self._db())
        try:
            svc.assess(min_samples=0)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    def test_rejects_inverted_verdict_bands(self) -> None:
        svc = RangeFitnessService(self._db())
        try:
            svc.assess(trend_unsuitable_pct=20.0, range_suitable_pct=50.0)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    def test_rejects_out_of_range_percentage(self) -> None:
        svc = RangeFitnessService(self._db())
        try:
            svc.assess(trend_unsuitable_pct=120.0)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    def test_does_not_mutate_evidence(self) -> None:
        self._seed("NVDA.US", trend_bars=70)
        RangeFitnessService(self._db()).assess(min_samples=60)
        db = self._db()
        try:
            assert db.query(StrategyV2ShadowDecision).count() == 70
        finally:
            db.close()
