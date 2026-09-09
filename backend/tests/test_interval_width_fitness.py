"""Interval width fitness — does the configured band width ever get touched?

A range strategy has two ways to earn nothing, and they look identical from
the outside: the band can sit where price never goes (stranded), or the band
can be so wide that price never reaches its edges (untouchable). The live
deployment hit the first for 34 days. Recentering fixed that, but recentering
alone cannot answer the second, and the second is the one that silently argues
for narrowing the band — a change that increases fill count while making money
faster, if the underlying signal has no edge.

This service answers both halves in one read-only pass over recorded shadow
prices: how often a band of a given half-width would be TOUCHED, and what the
resulting round trips would have NETTED after costs. Reporting reach without
net return is what makes narrowing look attractive; the two must travel
together.

Read-only: no order, no config write, no promotion.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_width_{os.getpid()}.db"
)

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, StrategyV2ShadowDecision
from app.services.interval_width_fitness_service import (
    VERDICT_INSUFFICIENT_DATA,
    VERDICT_NEGATIVE_EDGE,
    VERDICT_UNREACHABLE,
    IntervalWidthFitnessService,
)


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
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _seed_day(
        self,
        session_date: str,
        prices: list[float],
        *,
        symbol: str = "TSLA.US",
    ) -> None:
        """Write one synthetic session of 1-minute closes."""
        day = datetime.strptime(session_date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
        db = self._db()
        base = day + timedelta(hours=13, minutes=30)
        for i, px in enumerate(prices):
            bar_at = base + timedelta(minutes=i)
            db.add(StrategyV2ShadowDecision(
                symbol=symbol,
                config_version="v-test",
                session_date=day.date(),
                bar_at=bar_at,
                action="WAIT",
                reason="TEST",
                close_price=px,
                gate_passed=False,
                idempotency_key=f"{symbol}:v-test:{bar_at.isoformat()}",
            ))
        db.commit()
        db.close()

    def _assess(self, **kw):
        db = self._db()
        try:
            return IntervalWidthFitnessService(db).assess(**kw)
        finally:
            db.close()


class TestIntervalWidthFitness(_Base):
    def test_reports_insufficient_data_without_enough_days(self) -> None:
        # One day cannot support a day-clustered statistic. Thin evidence is
        # never a verdict about the width — it is an absence of evidence.
        self._seed_day("2026-09-01", [100.0 + i * 0.01 for i in range(120)])
        report = self._assess(symbol="TSLA.US", lookback_days=30)
        assert report.verdict == VERDICT_INSUFFICIENT_DATA
        for row in report.widths:
            assert row.trades < report.min_trades or report.distinct_days < report.min_days

    def test_a_band_price_never_reaches_is_reported_unreachable(self) -> None:
        # A dead-flat tape: no half-width above the noise floor is ever hit,
        # which is the "band too wide" failure. It must NOT be reported as a
        # losing strategy, because no trade ever happened.
        for d in range(1, 13):
            self._seed_day(
                f"2026-09-{d:02d}",
                [100.0 + (i % 3) * 0.001 for i in range(120)],
            )
        report = self._assess(symbol="TSLA.US", lookback_days=30)
        assert report.verdict == VERDICT_UNREACHABLE
        assert report.best_width is None
        widest = [w for w in report.widths if w.half_width_pct >= 1.0]
        assert widest and all(w.trades == 0 for w in widest)

    def test_narrower_widths_are_touched_at_least_as_often(self) -> None:
        # Monotonicity is the one structural property this must never violate:
        # a tighter band cannot be reached less often than a looser one on the
        # same tape. A regression here means the reach counting is wrong.
        import random

        rng = random.Random(11)
        for d in range(1, 13):
            px = 100.0
            series = []
            for _ in range(200):
                px *= 1 + rng.gauss(0, 0.0012)
                series.append(round(px, 4))
            self._seed_day(f"2026-09-{d:02d}", series)
        report = self._assess(symbol="TSLA.US", lookback_days=30)
        by_width = sorted(report.widths, key=lambda w: w.half_width_pct)
        reaches = [w.reach_rate_pct for w in by_width]
        assert reaches == sorted(reaches, reverse=True), reaches

    def test_a_reachable_but_losing_width_is_not_recommended(self) -> None:
        # THE point of this service. A tape that always dips to the entry and
        # then keeps falling is highly reachable and reliably loses. Reach
        # alone would recommend narrowing; net return must veto it.
        # 24 sessions so the one-entry-per-day cap still clears the 20-trade
        # evidence floor — the cap is real, so the fixture must respect it.
        start = datetime(2026, 8, 10, tzinfo=timezone.utc)
        for d in range(24):
            series: list[float] = []
            px = 100.0
            for _ in range(12):
                # Dip below the band's lower edge, then grind lower: entry
                # fills, the upper edge is never revisited, close is worse.
                series.extend([px, px * 0.99, px * 0.985])
                px *= 0.994
            self._seed_day(
                (start + timedelta(days=d)).strftime("%Y-%m-%d"),
                [round(x, 4) for x in series],
            )
        report = self._assess(symbol="TSLA.US", lookback_days=60)
        assert report.verdict == VERDICT_NEGATIVE_EDGE
        assert report.best_width is None
        touched = [w for w in report.widths if w.trades > 0]
        assert touched, "the tape must produce fills for this to be meaningful"
        assert all(w.net_ci_lower_bps <= 0 for w in touched)

    def test_costs_are_subtracted_from_every_round_trip(self) -> None:
        self._seed_day("2026-09-01", [100.0] * 60)
        db = self._db()
        try:
            svc = IntervalWidthFitnessService(db)
            gross = svc._round_trip_bps(100.0, 101.0, cost_bps=0.0)
            net = svc._round_trip_bps(100.0, 101.0, cost_bps=14.0)
            assert gross == pytest.approx(100.0, abs=0.5)
            assert net == pytest.approx(gross - 14.0, abs=0.01)
        finally:
            db.close()

    def test_is_read_only(self) -> None:
        for d in range(1, 13):
            self._seed_day(f"2026-09-{d:02d}", [100.0 + i * 0.02 for i in range(120)])
        before = self._count_rows()
        self._assess(symbol="TSLA.US", lookback_days=30)
        assert self._count_rows() == before
        import app.services.interval_width_fitness_service as mod
        path = mod.__file__
        assert path is not None
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for forbidden in (
            "submit_limit_order",
            "update_config",
            "db.add(",
            "commit()",
        ):
            assert forbidden not in src, forbidden

    def _count_rows(self) -> int:
        db = self._db()
        try:
            return db.query(StrategyV2ShadowDecision).count()
        finally:
            db.close()

    def test_rejects_an_invalid_lookback(self) -> None:
        with pytest.raises(ValueError):
            self._assess(symbol="TSLA.US", lookback_days=0)

    def test_unknown_symbol_is_insufficient_data_not_an_error(self) -> None:
        report = self._assess(symbol="NOPE.US", lookback_days=30)
        assert report.verdict == VERDICT_INSUFFICIENT_DATA
        assert report.distinct_days == 0
