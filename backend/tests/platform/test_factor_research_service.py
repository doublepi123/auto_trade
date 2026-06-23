"""Tests for the P196 factor research warehouse service + tables."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.database import engine, init_db
from app.models import Base, FactorICSeries, FactorSnapshot
from app.platform.factor_research_service import FactorResearchService, FactorSnapshotData


def _setup() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    init_db()


def _ts(day: int) -> datetime:
    return datetime(2026, 6, day, tzinfo=timezone.utc)


def test_record_and_list_snapshot() -> None:
    _setup()
    with Session(engine) as db:
        svc = FactorResearchService(db=db)
        svc.record_snapshot(
            FactorSnapshotData(
                factor_name="momentum_10",
                symbol="A.US",
                as_of=_ts(1),
                factor_value=0.05,
                forward_return=0.02,
            )
        )
        rows = svc.list_snapshots(factor_name="momentum_10")
    assert len(rows) == 1
    assert rows[0]["symbol"] == "A.US"
    assert rows[0]["factor_value"] == 0.05
    assert rows[0]["forward_return"] == 0.02


def test_record_many_persists_all() -> None:
    _setup()
    rows = [
        FactorSnapshotData("momentum_10", "A.US", _ts(1), 0.05, 0.02),
        FactorSnapshotData("momentum_10", "B.US", _ts(1), 0.03, 0.01),
        FactorSnapshotData("momentum_10", "C.US", _ts(1), -0.01, -0.005),
    ]
    with Session(engine) as db:
        count = FactorResearchService(db=db).record_many(rows)
        persisted = db.query(FactorSnapshot).count()
    assert count == 3
    assert persisted == 3


def test_rank_snapshot_assigns_cross_sectional_rank() -> None:
    _setup()
    with Session(engine) as db:
        svc = FactorResearchService(db=db)
        svc.record_many(
            [
                FactorSnapshotData("m", "A.US", _ts(1), 0.05),
                FactorSnapshotData("m", "B.US", _ts(1), 0.10),
                FactorSnapshotData("m", "C.US", _ts(1), 0.02),
            ]
        )
        n = svc.rank_snapshot("m", _ts(1))
        ranked = db.query(FactorSnapshot).order_by(FactorSnapshot.rank.asc()).all()
    assert n == 3
    assert [r.symbol for r in ranked] == ["B.US", "A.US", "C.US"]
    assert [r.rank for r in ranked] == [1, 2, 3]


def test_compute_ic_series_with_perfect_monotonic_factor() -> None:
    _setup()
    with Session(engine) as db:
        svc = FactorResearchService(db=db)
        # Day 1: factor perfectly predicts forward return cross-sectionally.
        svc.record_many(
            [
                FactorSnapshotData("m", "A.US", _ts(1), 0.01, 0.01),
                FactorSnapshotData("m", "B.US", _ts(1), 0.02, 0.02),
                FactorSnapshotData("m", "C.US", _ts(1), 0.03, 0.03),
            ]
        )
        # Day 2: factor inversely predicts return (IC = -1).
        svc.record_many(
            [
                FactorSnapshotData("m", "A.US", _ts(2), 0.01, 0.03),
                FactorSnapshotData("m", "B.US", _ts(2), 0.02, 0.02),
                FactorSnapshotData("m", "C.US", _ts(2), 0.03, 0.01),
            ]
        )
        result = svc.compute_ic_series("m")
        ic_rows = db.query(FactorICSeries).count()
    assert result["num_periods"] == 2
    per_period_ics = [p["ic"] for p in result["per_period"]]
    assert per_period_ics[0] > 0.99
    assert per_period_ics[1] < -0.99
    # average of +1 and -1 is exactly 0.0
    assert abs(result["mean_ic"]) < 1e-9
    assert ic_rows == 2


def test_compute_ic_series_skips_periods_with_too_few_symbols() -> None:
    _setup()
    with Session(engine) as db:
        svc = FactorResearchService(db=db)
        # Day 1: only 1 symbol -> skipped.
        svc.record_many([FactorSnapshotData("m", "A.US", _ts(1), 0.01, 0.01)])
        # Day 2: 3 symbols -> counted.
        svc.record_many(
            [
                FactorSnapshotData("m", "A.US", _ts(2), 0.01, 0.01),
                FactorSnapshotData("m", "B.US", _ts(2), 0.02, 0.02),
                FactorSnapshotData("m", "C.US", _ts(2), 0.03, 0.03),
            ]
        )
        result = svc.compute_ic_series("m")
    assert result["num_periods"] == 1
    assert result["per_period"][0]["num_symbols"] == 3


def test_list_filters_by_symbol_and_date_range() -> None:
    _setup()
    with Session(engine) as db:
        svc = FactorResearchService(db=db)
        svc.record_many(
            [
                FactorSnapshotData("m", "A.US", _ts(1), 0.05),
                FactorSnapshotData("m", "A.US", _ts(3), 0.06),
                FactorSnapshotData("m", "B.US", _ts(2), 0.07),
            ]
        )
        a_rows = svc.list_snapshots(symbol="A.US")
        ranged = svc.list_snapshots(since=_ts(2), until=_ts(3))
    assert len(a_rows) == 2
    assert all(r["symbol"] == "A.US" for r in a_rows)
    # ranged covers _ts(2).._ts(3): excludes the day-1 A.US row.
    assert all(r["as_of"] != _ts(1).isoformat() for r in ranged)


def test_factor_snapshots_table_created_by_init_db() -> None:
    _setup()
    # init_db() already ran in _setup; verify tables exist by inserting directly.
    with Session(engine) as db:
        db.add(FactorSnapshot(factor_name="m", symbol="X.US", as_of=_ts(1), factor_value=0.1))
        db.add(FactorICSeries(factor_name="m", as_of=_ts(1), mean_ic=0.5))
        db.commit()
        assert db.query(FactorSnapshot).count() == 1
        assert db.query(FactorICSeries).count() == 1
