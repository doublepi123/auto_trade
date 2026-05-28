from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "AUTO_TRADE_DATABASE_URL",
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_performance.db",
)

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, ExperimentResult
from app.domain.performance.performance_tracker import PerformanceTracker


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test_perf.db")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def _seed_results(db: Session, experiment: str, variant: str, count: int, win_rate: float):
    import random
    random.seed(42)
    for i in range(count):
        profitable = random.random() < win_rate
        db.add(ExperimentResult(
            experiment_name=experiment,
            variant_name=variant,
            interaction_id=i + 1,
            order_action="BUY_NOW" if i % 2 == 0 else "SELL_NOW",
            predicted_direction="UP" if i % 2 == 0 else "DOWN",
            actual_pnl=50.0 if profitable else -30.0,
            was_profitable=profitable,
        ))
    db.commit()


class TestPerformanceTracker:
    def test_get_overall_stats(self, db_session: Session) -> None:
        _seed_results(db_session, "exp1", "v1", 20, 0.6)
        tracker = PerformanceTracker(db_session)
        stats = tracker.get_overall_stats("exp1")

        assert stats["total_trades"] == 20
        assert 0.4 < stats["win_rate"] < 0.8  # ~60% with seed
        assert stats["total_pnl"] != 0

    def test_get_variant_comparison(self, db_session: Session) -> None:
        _seed_results(db_session, "exp1", "v1", 20, 0.6)
        _seed_results(db_session, "exp1", "v2", 20, 0.4)
        tracker = PerformanceTracker(db_session)

        comparison = tracker.compare_variants("exp1")
        assert len(comparison) == 2
        v1 = next(c for c in comparison if c["variant"] == "v1")
        v2 = next(c for c in comparison if c["variant"] == "v2")
        assert v1["win_rate"] > v2["win_rate"]

    def test_get_recommendations(self, db_session: Session) -> None:
        _seed_results(db_session, "exp1", "v1", 30, 0.2)  # Poor performance
        tracker = PerformanceTracker(db_session)
        recs = tracker.get_recommendations("exp1")
        assert len(recs) > 0
        assert any("v1" in r for r in recs)
