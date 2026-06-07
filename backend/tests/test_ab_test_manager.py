from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "AUTO_TRADE_DATABASE_URL",
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_ab_test.db",
)

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, PromptVersion, ExperimentResult
from app.domain.experiment.ab_test_manager import ABTestManager


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test_ab.db")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


class TestABTestManager:
    def test_create_and_list_versions(self, db_session: Session) -> None:
        manager = ABTestManager(db_session)
        v1 = manager.create_version("baseline", "1.0", "Original prompt", "template A")
        v2 = manager.create_version("enhanced", "1.1", "With RSI", "template B")

        versions = manager.list_versions()
        assert len(versions) == 2
        assert versions[0].name == "baseline"

    def test_activate_version(self, db_session: Session) -> None:
        manager = ABTestManager(db_session)
        v1 = manager.create_version("baseline", "1.0", "", "template A")
        v2 = manager.create_version("enhanced", "1.1", "", "template B")

        manager.activate_version(v2.id)
        active = manager.get_active_version()
        assert active is not None
        assert active.name == "enhanced"

        versions = manager.list_versions()
        active_count = sum(1 for v in versions if v.is_active)
        assert active_count == 1

    def test_get_active_returns_none_when_no_active(self, db_session: Session) -> None:
        manager = ABTestManager(db_session)
        assert manager.get_active_version() is None

    def test_select_variant_for_experiment(self, db_session: Session) -> None:
        manager = ABTestManager(db_session)
        # Create two versions under the same experiment name
        v1 = manager.create_version("prompt_optimization", "1.0", "", "template A")
        manager.create_version("prompt_optimization", "1.1", "", "template B")
        manager.activate_version(v1.id)

        # Deterministic selection based on symbol hash among active versions
        variant = manager.select_variant("AAPL.US", "prompt_optimization")
        assert variant is not None
        assert variant.name == "prompt_optimization"

    def test_record_result(self, db_session: Session) -> None:
        manager = ABTestManager(db_session)
        v1 = manager.create_version("baseline", "1.0", "", "template A")

        manager.record_result(
            experiment_name="test_exp",
            variant_name="baseline",
            interaction_id=1,
            order_action="BUY_NOW",
            predicted_direction="UP",
            actual_pnl=50.0,
            was_profitable=True,
        )

        results = db_session.query(ExperimentResult).all()
        assert len(results) == 1
        assert results[0].actual_pnl == 50.0

    def test_get_experiment_summary(self, db_session: Session) -> None:
        manager = ABTestManager(db_session)
        manager.record_result(
            experiment_name="exp1", variant_name="v1", interaction_id=1,
            order_action="BUY_NOW", predicted_direction="UP", actual_pnl=50.0, was_profitable=True,
        )
        manager.record_result(
            experiment_name="exp1", variant_name="v1", interaction_id=2,
            order_action="SELL_NOW", predicted_direction="DOWN", actual_pnl=-20.0, was_profitable=False,
        )
        manager.record_result(
            experiment_name="exp1", variant_name="v2", interaction_id=3,
            order_action="BUY_NOW", predicted_direction="UP", actual_pnl=30.0, was_profitable=True,
        )

        summary = manager.get_experiment_summary("exp1")
        assert len(summary) == 2
        v1_summary = next(s for s in summary if s["variant_name"] == "v1")
        assert v1_summary["total_count"] == 2
        assert v1_summary["win_rate"] == pytest.approx(0.5)
