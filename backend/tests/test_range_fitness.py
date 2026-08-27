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

from app.models import (
    Base,
    StrategyConfig,
    StrategyV2ShadowDecision,
    StrategyV2ShadowTrade,
)
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


class TestReachRate(_Base):
    """Reach-rate: share of closed shadow trades whose peak favourable
    excursion cleared the round-trip cost with margin.

    Measured across 247 closed trades, this separated profitable symbols from
    unprofitable ones with no exceptions (85% vs 22%) while the ADX trend share
    ranked them barely better than chance, so the switch gate requires both.
    """

    def setup_method(self) -> None:
        super().setup_method()
        db = self._db()
        db.query(StrategyV2ShadowTrade).delete()
        db.commit()
        db.close()

    def _trade(
        self,
        symbol: str,
        *,
        mfe_pct: float | None,
        status: str = "CLOSED",
        age_days: float = 0.0,
        seq: int = 0,
    ) -> None:
        db = self._db()
        exit_at = datetime.now(timezone.utc) - timedelta(days=age_days)
        db.add(StrategyV2ShadowTrade(
            symbol=symbol,
            config_version="v1",
            status=status,
            entry_at=exit_at - timedelta(minutes=30),
            exit_at=None if status == "OPEN" else exit_at,
            entry_price=100.0,
            quantity=1.0,
            mfe_pct=mfe_pct,
        ))
        db.commit()
        db.close()

    def test_reach_rate_counts_only_trades_clearing_the_threshold(self) -> None:
        self._seed("TER.US", trend_bars=10, other_bars=50)
        for i, mfe in enumerate((0.009, 0.006, 0.004, 0.0039, 0.001)):
            self._trade("TER.US", mfe_pct=mfe, seq=i)
        row = self._row("TER.US", min_samples=60)
        # 0.004 is inclusive; 0.0039 and 0.001 fall short.
        assert row.closed_trades == 5
        assert row.reach_count == 3
        assert row.reach_rate_pct == 60.0

    def test_open_trades_are_excluded(self) -> None:
        self._seed("APP.US", trend_bars=10, other_bars=50)
        self._trade("APP.US", mfe_pct=0.009, seq=0)
        self._trade("APP.US", mfe_pct=0.0, status="OPEN", seq=1)
        row = self._row("APP.US", min_samples=60)
        assert row.closed_trades == 1
        assert row.reach_rate_pct == 100.0

    def test_missing_mfe_is_not_counted_as_a_miss(self) -> None:
        """A backfill gap must not fabricate an unreachable symbol."""
        self._seed("MU.US", trend_bars=10, other_bars=50)
        self._trade("MU.US", mfe_pct=0.009, seq=0)
        self._trade("MU.US", mfe_pct=None, seq=1)
        row = self._row("MU.US", min_samples=60)
        assert row.closed_trades == 1
        assert row.reach_rate_pct == 100.0

    def test_trades_outside_the_window_are_ignored(self) -> None:
        self._seed("GS.US", trend_bars=10, other_bars=50)
        self._trade("GS.US", mfe_pct=0.009, age_days=10.0, seq=0)
        row = self._row("GS.US", min_samples=60, lookback_days=3)
        assert row.closed_trades == 0
        assert row.reach_rate_pct is None

    def test_no_closed_trades_reports_none_not_zero(self) -> None:
        """None (absent evidence) must stay distinguishable from 0% (measured)."""
        self._seed("CAT.US", trend_bars=10, other_bars=50)
        row = self._row("CAT.US", min_samples=60)
        assert row.closed_trades == 0
        assert row.reach_count == 0
        assert row.reach_rate_pct is None

    def test_measured_zero_reach_rate_is_zero_not_none(self) -> None:
        self._seed("ASML.US", trend_bars=10, other_bars=50)
        self._trade("ASML.US", mfe_pct=0.001, seq=0)
        row = self._row("ASML.US", min_samples=60)
        assert row.closed_trades == 1
        assert row.reach_rate_pct == 0.0

    def test_reach_rate_does_not_mutate_trades(self) -> None:
        self._seed("NVDA.US", trend_bars=70)
        self._trade("NVDA.US", mfe_pct=0.009, seq=0)
        RangeFitnessService(self._db()).assess(min_samples=60)
        db = self._db()
        try:
            assert db.query(StrategyV2ShadowTrade).count() == 1
        finally:
            db.close()


class TestReachLookbackWindow(_Base):
    """Reach evidence uses its own, never-shorter window.

    Bars accumulate ~100x faster than closed trades: the live system had 4
    closed trades in 3 days but 212 in 30. A shared window either starves the
    reach gate or, if widened, blunts the trend read that must stay fresh.
    """

    def setup_method(self) -> None:
        super().setup_method()
        db = self._db()
        db.query(StrategyV2ShadowTrade).delete()
        db.commit()
        db.close()

    def _trade(self, symbol: str, *, mfe_pct: float, age_days: float) -> None:
        db = self._db()
        exit_at = datetime.now(timezone.utc) - timedelta(days=age_days)
        db.add(StrategyV2ShadowTrade(
            symbol=symbol,
            config_version="v1",
            status="CLOSED",
            entry_at=exit_at - timedelta(minutes=30),
            exit_at=exit_at,
            entry_price=100.0,
            quantity=1.0,
            mfe_pct=mfe_pct,
        ))
        db.commit()
        db.close()

    def test_longer_reach_window_admits_older_trades(self) -> None:
        self._seed("TER.US", trend_bars=10, other_bars=50)
        for age in (1.0, 8.0, 15.0, 22.0, 28.0):
            self._trade("TER.US", mfe_pct=0.009, age_days=age)

        tight = self._row("TER.US", min_samples=60, lookback_days=3)
        assert tight.closed_trades == 1

        wide = self._row(
            "TER.US", min_samples=60, lookback_days=3, reach_lookback_days=30
        )
        assert wide.closed_trades == 5
        assert wide.reach_rate_pct == 100.0

    def test_bar_window_stays_narrow_when_reach_window_widens(self) -> None:
        """Widening reach evidence must not drag stale bars into the trend read."""
        self._seed("NVDA.US", trend_bars=70, other_bars=10, age_days=0.0)
        self._seed("NVDA.US", trend_bars=0, other_bars=80, age_days=20.0)

        row = self._row(
            "NVDA.US", min_samples=60, lookback_days=3, reach_lookback_days=30
        )
        # Only the fresh 80 bars count; the 20-day-old calm bars stay excluded,
        # so the trend verdict is unaffected by the wider reach window.
        assert row.samples == 80
        assert row.trend_blocked == 70
        assert row.verdict == VERDICT_TREND_UNSUITABLE

    def test_reach_window_never_shorter_than_the_bar_window(self) -> None:
        self._seed("APP.US", trend_bars=10, other_bars=50)
        self._trade("APP.US", mfe_pct=0.009, age_days=5.0)

        # A reach window below the bar window would silently discard evidence the
        # bar window already covers; it is clamped up instead.
        row = self._row(
            "APP.US", min_samples=60, lookback_days=14, reach_lookback_days=1
        )
        assert row.closed_trades == 1

    def test_rejects_non_positive_reach_window(self) -> None:
        svc = RangeFitnessService(self._db())
        try:
            svc.assess(reach_lookback_days=0)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    def test_defaults_to_the_bar_window_when_unset(self) -> None:
        self._seed("MU.US", trend_bars=10, other_bars=50)
        self._trade("MU.US", mfe_pct=0.009, age_days=10.0)

        row = self._row("MU.US", min_samples=60, lookback_days=3)
        assert row.closed_trades == 0
