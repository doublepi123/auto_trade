"""Entry-window overlap — do the two entry conditions ever coincide?

A live entry needs two things at the same minute: price at or below the
band's lower edge, AND the regime gate open. Each can be satisfied often
while the pair is never satisfied. On the live deployment the pattern was
exactly that: trend days pushed price through the band but closed the gate
(high ADX); range days opened the gate but price never reached the band.
Seven sessions, two with any overlap at all.

Reach rate alone (interval-width-fitness) and gate pass rate alone (shadow
status) both overstate opportunity because each ignores the other. This
service replays both conditions minute by minute against recorded shadow
decisions and reports how many minutes per session BOTH held.

Read-only: no order, no config write, no promotion.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta, timezone

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_overlap_{os.getpid()}.db"
)

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, StrategyV2ShadowDecision
from app.services.entry_window_overlap_service import (
    VERDICT_INSUFFICIENT_DATA,
    VERDICT_NO_OVERLAP,
    VERDICT_OVERLAP_PRESENT,
    EntryWindowOverlapService,
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
        bars: list[tuple[float, bool]],
        *,
        symbol: str = "TSLA.US",
    ) -> None:
        """Write one session: a list of (close_price, gate_passed) per minute."""
        day = datetime.strptime(session_date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
        base = day + timedelta(hours=13, minutes=30)
        db = self._db()
        for i, (px, gate) in enumerate(bars):
            bar_at = base + timedelta(minutes=i)
            db.add(StrategyV2ShadowDecision(
                symbol=symbol,
                config_version="v-test",
                session_date=day.date(),
                bar_at=bar_at,
                action="WAIT",
                reason="TEST",
                close_price=px,
                gate_passed=gate,
                idempotency_key=f"{symbol}:v-test:{bar_at.isoformat()}",
            ))
        db.commit()
        db.close()

    def _assess(self, **kw):
        db = self._db()
        try:
            return EntryWindowOverlapService(db).assess(**kw)
        finally:
            db.close()


def _flat(px: float, gate: bool, n: int) -> list[tuple[float, bool]]:
    return [(px, gate)] * n


class TestEntryWindowOverlap(_Base):
    def test_trend_day_breaks_the_band_while_the_gate_is_shut(self) -> None:
        # The live 2026-09-04 shape: price gaps down 3% and stays there, ADX
        # is high all day, gate never opens. Lots of "reach", zero windows.
        bars = _flat(100.0, False, 5) + _flat(96.0, False, 200)
        self._seed_day("2026-09-04", bars)
        report = self._assess(symbol="TSLA.US", lookback_days=30)
        day = report.sessions[0]
        assert day.below_band_minutes > 0
        assert day.gate_open_minutes == 0
        assert day.overlap_minutes == 0

    def test_range_day_opens_the_gate_but_never_reaches_the_band(self) -> None:
        # The live 2026-09-09 shape: price drifts in a tight range inside the
        # band, gate opens for an hour, price never touches the lower edge.
        bars = _flat(100.0, False, 5) + _flat(100.2, True, 60) + _flat(99.8, False, 140)
        self._seed_day("2026-09-09", bars)
        report = self._assess(symbol="TSLA.US", lookback_days=30)
        day = report.sessions[0]
        assert day.gate_open_minutes == 60
        assert day.below_band_minutes == 0
        assert day.overlap_minutes == 0

    def test_overlap_is_counted_only_where_both_hold_at_the_same_minute(
        self,
    ) -> None:
        # Price below band for minutes 5-104; gate open for minutes 80-129.
        # Overlap must be exactly 25 minutes (80-104), not 100 and not 50.
        bars = (
            _flat(100.0, False, 5)
            + _flat(98.0, False, 75)   # 5-79 below, gate shut
            + _flat(98.0, True, 25)    # 80-104 below AND open
            + _flat(101.0, True, 25)   # 105-129 open but above band
            + _flat(101.0, False, 75)
        )
        self._seed_day("2026-09-02", bars)
        report = self._assess(symbol="TSLA.US", lookback_days=30, warmup_minutes=0)
        day = report.sessions[0]
        assert day.below_band_minutes == 100
        assert day.gate_open_minutes == 50
        assert day.overlap_minutes == 25

    def test_warmup_minutes_are_excluded_from_overlap(self) -> None:
        # Live rejects entries for the first 90 minutes. An overlap inside the
        # warmup is not an entry window and must not be counted as one.
        bars = _flat(100.0, False, 1) + _flat(98.0, True, 89) + _flat(101.0, False, 110)
        self._seed_day("2026-09-01", bars)
        report = self._assess(symbol="TSLA.US", lookback_days=30, warmup_minutes=90)
        day = report.sessions[0]
        assert day.overlap_minutes == 0
        # Without the warmup the same tape has 89 minutes of overlap.
        report2 = self._assess(symbol="TSLA.US", lookback_days=30, warmup_minutes=0)
        assert report2.sessions[0].overlap_minutes == 89

    def test_band_recenters_on_drift_like_the_live_job(self) -> None:
        # Price falls 3% at minute 40. Without recentering it stays "below
        # band" forever. With the live rule (recenter every 30 bars when drift
        # >= 1.5%) the band follows at the next placement and the count stops.
        bars = _flat(100.0, True, 40) + _flat(97.0, True, 160)
        self._seed_day("2026-08-28", bars)
        report = self._assess(symbol="TSLA.US", lookback_days=30, warmup_minutes=0)
        day = report.sessions[0]
        # Below band from 40 until the recenter placement at 60, then inside.
        assert 0 < day.below_band_minutes < 160
        assert day.recenters >= 1

    def test_verdict_no_overlap_when_every_session_is_zero(self) -> None:
        for d in range(1, 13):
            self._seed_day(
                f"2026-09-{d:02d}",
                _flat(100.0, False, 5) + _flat(96.0, False, 200),
            )
        report = self._assess(symbol="TSLA.US", lookback_days=30)
        assert report.verdict == VERDICT_NO_OVERLAP
        assert report.sessions_with_overlap == 0

    def test_verdict_overlap_present_reports_the_share_of_sessions(self) -> None:
        for d in range(1, 13):
            gate = d % 4 == 0  # 3 of 12 sessions have a real window
            self._seed_day(
                f"2026-09-{d:02d}",
                _flat(100.0, False, 95) + _flat(98.0, gate, 110),
            )
        report = self._assess(symbol="TSLA.US", lookback_days=30)
        assert report.verdict == VERDICT_OVERLAP_PRESENT
        assert report.sessions_with_overlap == 3
        assert report.sessions_total == 12
        assert report.overlap_session_share_pct == pytest.approx(25.0)

    def test_insufficient_data_below_the_session_floor(self) -> None:
        self._seed_day("2026-09-01", _flat(100.0, True, 200))
        report = self._assess(symbol="TSLA.US", lookback_days=30)
        assert report.verdict == VERDICT_INSUFFICIENT_DATA

    def test_unknown_symbol_is_insufficient_data(self) -> None:
        report = self._assess(symbol="NOPE.US", lookback_days=30)
        assert report.verdict == VERDICT_INSUFFICIENT_DATA
        assert report.sessions_total == 0

    def test_rejects_invalid_lookback(self) -> None:
        with pytest.raises(ValueError):
            self._assess(symbol="TSLA.US", lookback_days=0)

    def test_pool_summary_ranks_symbols_by_windows_and_counts_per_day(self) -> None:
        # Three symbols, twelve sessions. A has a window on 6 of them, B on 2,
        # C never. Per-day count must reflect how many symbols had a window
        # that day, which is what an operator scanning for "anything tradeable
        # today" needs and which the single-symbol view cannot show.
        for d in range(1, 13):
            day = f"2026-09-{d:02d}"
            self._seed_day(day, _flat(100.0, False, 95) + _flat(98.0, d % 2 == 0, 110), symbol="AAA.US")
            self._seed_day(day, _flat(100.0, False, 95) + _flat(98.0, d in (3, 7), 110), symbol="BBB.US")
            self._seed_day(day, _flat(100.0, False, 95) + _flat(101.0, True, 110), symbol="CCC.US")
        db = self._db()
        try:
            pool = EntryWindowOverlapService(db).assess_pool(
                symbols=["CCC.US", "AAA.US", "BBB.US"], lookback_days=30,
            )
        finally:
            db.close()
        ranked = [r.symbol for r in pool.symbols]
        assert ranked == ["AAA.US", "BBB.US", "CCC.US"]
        assert pool.symbols[0].sessions_with_overlap == 6
        assert pool.symbols[1].sessions_with_overlap == 2
        assert pool.symbols[2].sessions_with_overlap == 0
        by_day = {row.session_date.isoformat(): row.symbols_with_window for row in pool.days}
        assert by_day["2026-09-02"] == 1   # AAA only
        assert by_day["2026-09-03"] == 1   # BBB only
        assert by_day["2026-09-07"] == 1   # BBB only (7 is odd)
        assert by_day["2026-09-01"] == 0
        assert pool.symbols_total == 3
        assert pool.symbols_with_any_window == 2

    def test_pool_summary_rejects_an_empty_symbol_list(self) -> None:
        db = self._db()
        try:
            with pytest.raises(ValueError):
                EntryWindowOverlapService(db).assess_pool(symbols=[], lookback_days=30)
        finally:
            db.close()

    def test_is_read_only(self) -> None:
        for d in range(1, 13):
            self._seed_day(f"2026-09-{d:02d}", _flat(100.0, True, 200))
        db = self._db()
        before = db.query(StrategyV2ShadowDecision).count()
        db.close()
        self._assess(symbol="TSLA.US", lookback_days=30)
        db = self._db()
        after = db.query(StrategyV2ShadowDecision).count()
        db.close()
        assert after == before
        import app.services.entry_window_overlap_service as mod
        path = mod.__file__
        assert path is not None
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for forbidden in ("submit_limit_order", "update_config", "db.add(", "commit()"):
            assert forbidden not in src, forbidden
