from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import universe as universe_api
from app.database import get_db
from app.domain.universe_selection.rotation_forward_scorecard import (
    ROTATION_FORWARD_SCORECARD_VERSION,
    build_rotation_forward_track_score,
    parse_rotation_forward_cohort,
)
from app.domain.universe_selection.rotation_forward import (
    ROTATION_FORWARD_VERSION,
)
from app.domain.universe_selection.selector import (
    ROTATION_ALGORITHM_VERSION,
)
from app.domain.universe_selection.rotation_walk_forward import (
    DIVERSIFIED_ROTATION_VARIANT,
)
from app.models import Base, UniverseSelectionRun
from app.services.rotation_forward_scorecard_service import (
    RotationForwardScorecardService,
)


_VARIANT = DIVERSIFIED_ROTATION_VARIANT.name


def _payload(
    *,
    cohort_month: date,
    signal_date: date,
    entry_date: date,
    mark_date: date,
    net_return: float,
    qqq_return: float,
    dia_return: float,
    forward_eligible: bool = True,
    selection_drift: bool = False,
    status: str | None = None,
) -> dict[str, object]:
    return {
        "algorithm_version": ROTATION_FORWARD_VERSION,
        "rotation_algorithm_version": ROTATION_ALGORITHM_VERSION,
        "status": status or (
            "FORWARD_OPEN"
            if forward_eligible
            else "BACKFILLED_OPEN"
        ),
        "evidence_mode": (
            "FORWARD_PRECOMMITTED"
            if forward_eligible
            else "BACKFILLED_AFTER_ENTRY"
        ),
        "cohort_month": cohort_month.isoformat(),
        "variant_name": _VARIANT,
        "signal_date": signal_date.isoformat(),
        "entry_date": entry_date.isoformat(),
        "mark_date": mark_date.isoformat(),
        "registered_as_of_date": (
            signal_date if forward_eligible else entry_date
        ).isoformat(),
        "forward_eligible": forward_eligible,
        "selection_drift_detected": selection_drift,
        "target_symbols": ["AAPL.US", "MSFT.US"],
        "holdings": [],
        "elapsed_sessions": 20,
        "forward_observation_sessions": (
            20 if forward_eligible else 0
        ),
        "gross_return_pct": net_return + 0.1,
        "entry_cost_pct": 0.05,
        "estimated_exit_cost_pct": 0.05,
        "total_estimated_cost_pct": 0.1,
        "net_liquidation_return_pct": net_return,
        "qqq_return_pct": qqq_return,
        "dia_return_pct": dia_return,
        "excess_return_vs_qqq_pct": net_return - qqq_return,
        "excess_return_vs_dia_pct": net_return - dia_return,
        "survivorship_bias": True,
        "order_execution_allowed": False,
        "automatic_promotion_allowed": False,
        "blockers": [],
    }


def _evidence(
    payload: dict[str, object],
    *,
    source_run_id: int,
    source_as_of_date: date,
):
    return parse_rotation_forward_cohort(
        payload,
        source_run_id=source_run_id,
        source_as_of_date=source_as_of_date,
        expected_variant_name=_VARIANT,
    )


def _completed_evidence():
    rows = (
        (
            date(2025, 7, 1),
            date(2025, 6, 30),
            date(2025, 7, 1),
            date(2025, 7, 31),
            3.0,
            1.0,
            0.5,
        ),
        (
            date(2025, 8, 1),
            date(2025, 7, 31),
            date(2025, 8, 1),
            date(2025, 8, 29),
            2.0,
            1.0,
            0.5,
        ),
        (
            date(2025, 9, 1),
            date(2025, 8, 29),
            date(2025, 9, 2),
            date(2025, 9, 30),
            -1.0,
            -0.5,
            -1.5,
        ),
    )
    return tuple(
        _evidence(
            _payload(
                cohort_month=cohort_month,
                signal_date=signal_date,
                entry_date=entry_date,
                mark_date=mark_date,
                net_return=net_return,
                qqq_return=qqq_return,
                dia_return=dia_return,
            ),
            source_run_id=index,
            source_as_of_date=mark_date,
        )
        for index, (
            cohort_month,
            signal_date,
            entry_date,
            mark_date,
            net_return,
            qqq_return,
            dia_return,
        ) in enumerate(rows, start=1)
    )


def _db() -> tuple[Engine, Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def _run_with_snapshot(
    db: Session,
    *,
    run_date: date,
    snapshot: dict[str, object],
) -> UniverseSelectionRun:
    observed_at = datetime.combine(
        run_date,
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    run = UniverseSelectionRun(
        as_of_date=run_date,
        algorithm_version="selector-v1",
        source_version="catalog-v1",
        status="COMPLETE",
        candidate_count=2,
        evaluable_count=2,
        selected_count=2,
        coverage_ratio=1.0,
        parameters_json=json.dumps(
            {"rotation_forward_snapshot": snapshot}
        ),
        error="",
        started_at=observed_at,
        completed_at=observed_at,
        created_at=observed_at,
    )
    db.add(run)
    db.flush()
    return run


def test_scorecard_counts_only_deduplicated_month_end_forward_evidence() -> None:
    completed = list(_completed_evidence())
    intra_month = _evidence(
        _payload(
            cohort_month=date(2025, 7, 1),
            signal_date=date(2025, 6, 30),
            entry_date=date(2025, 7, 1),
            mark_date=date(2025, 7, 15),
            net_return=10.0,
            qqq_return=1.0,
            dia_return=0.5,
        ),
        source_run_id=4,
        source_as_of_date=date(2025, 7, 15),
    )
    backfilled = _evidence(
        _payload(
            cohort_month=date(2025, 10, 1),
            signal_date=date(2025, 9, 30),
            entry_date=date(2025, 10, 1),
            mark_date=date(2025, 10, 15),
            net_return=25.0,
            qqq_return=0.0,
            dia_return=0.0,
            forward_eligible=False,
        ),
        source_run_id=5,
        source_as_of_date=date(2025, 10, 15),
    )

    score = build_rotation_forward_track_score(
        variant_name=_VARIANT,
        evidence=[intra_month, *completed, backfilled],
        as_of_date=date(2025, 10, 15),
    )

    assert score.status == "READY_FOR_MANUAL_REVIEW"
    assert score.observed_cohorts == 4
    assert score.forward_eligible_cohorts == 3
    assert score.completed_cohorts == 3
    assert score.backfilled_cohorts == 1
    assert score.open_cohort is None
    assert score.compounded_return_pct == pytest.approx(4.0094)
    assert score.qqq_compounded_return_pct == pytest.approx(1.49995)
    assert score.compounded_excess_vs_qqq_pct == pytest.approx(2.50945)
    assert score.excess_win_rate_vs_qqq_pct == pytest.approx(66.666667)
    assert score.excess_win_rate_vs_dia_pct == 100.0
    assert score.manual_review_ready is True
    assert score.automatic_promotion_allowed is False
    assert score.blockers == ()
    assert score.warnings == (
        "BACKFILLED_COHORTS_EXCLUDED",
        "SURVIVORSHIP_BIAS",
    )


def test_scorecard_blocks_invalid_drift_and_weak_performance() -> None:
    rows = list(_completed_evidence())
    rows[0] = _evidence(
        _payload(
            cohort_month=date(2025, 7, 1),
            signal_date=date(2025, 6, 30),
            entry_date=date(2025, 7, 1),
            mark_date=date(2025, 7, 31),
            net_return=-3.0,
            qqq_return=1.0,
            dia_return=0.5,
            selection_drift=True,
        ),
        source_run_id=10,
        source_as_of_date=date(2025, 7, 31),
    )
    score = build_rotation_forward_track_score(
        variant_name=_VARIANT,
        evidence=rows,
        as_of_date=date(2025, 9, 30),
        invalid_evidence_records=1,
    )

    assert score.status == "DATA_BLOCKED"
    assert score.completed_cohorts == 2
    assert score.selection_drift_cohorts == 1
    assert score.incomplete_closed_cohorts == 1
    assert score.manual_review_ready is False
    assert "FORWARD_EVIDENCE_INVALID" in score.blockers
    assert "FORWARD_SELECTION_DRIFT" in score.blockers
    assert "FORWARD_COHORT_DATA_INCOMPLETE" in score.blockers


def test_rotation_forward_parser_rejects_unsafe_or_inconsistent_payload() -> None:
    payload = _payload(
        cohort_month=date(2025, 7, 1),
        signal_date=date(2025, 6, 30),
        entry_date=date(2025, 7, 1),
        mark_date=date(2025, 7, 31),
        net_return=3.0,
        qqq_return=1.0,
        dia_return=0.5,
    )
    payload["automatic_promotion_allowed"] = True
    with pytest.raises(ValueError, match="automatic promotion"):
        _evidence(
            payload,
            source_run_id=1,
            source_as_of_date=date(2025, 7, 31),
        )

    payload["automatic_promotion_allowed"] = False
    payload["excess_return_vs_qqq_pct"] = 99.0
    with pytest.raises(ValueError, match="QQQ excess"):
        _evidence(
            payload,
            source_run_id=1,
            source_as_of_date=date(2025, 7, 31),
        )

    payload["excess_return_vs_qqq_pct"] = 2.0
    payload["status"] = "UNKNOWN"
    with pytest.raises(ValueError, match="status is invalid"):
        _evidence(
            payload,
            source_run_id=1,
            source_as_of_date=date(2025, 7, 31),
        )

    payload["status"] = "FORWARD_OPEN"
    payload["algorithm_version"] = "rotation-monthly-open-forward-v1"
    with pytest.raises(ValueError, match="algorithm version"):
        _evidence(
            payload,
            source_run_id=1,
            source_as_of_date=date(2025, 7, 31),
        )

    payload["algorithm_version"] = ROTATION_FORWARD_VERSION
    payload["target_symbols"] = []
    with pytest.raises(ValueError, match="requires target symbols"):
        _evidence(
            payload,
            source_run_id=1,
            source_as_of_date=date(2025, 7, 31),
        )


def test_zero_session_and_invalid_evidence_cannot_open_review() -> None:
    payload = _payload(
        cohort_month=date(2025, 7, 1),
        signal_date=date(2025, 6, 30),
        entry_date=date(2025, 7, 1),
        mark_date=date(2025, 7, 31),
        net_return=3.0,
        qqq_return=1.0,
        dia_return=0.5,
    )
    payload["forward_observation_sessions"] = 0
    zero_session = _evidence(
        payload,
        source_run_id=1,
        source_as_of_date=date(2025, 7, 31),
    )
    assert zero_session.complete is False

    score = build_rotation_forward_track_score(
        variant_name=_VARIANT,
        evidence=[],
        as_of_date=date(2025, 7, 31),
        invalid_evidence_records=1,
    )
    assert score.status == "DATA_BLOCKED"
    assert score.manual_review_ready is False


def test_scorecard_service_and_endpoint_are_read_only() -> None:
    engine, db = _db()
    payloads = (
        _payload(
            cohort_month=date(2025, 7, 1),
            signal_date=date(2025, 6, 30),
            entry_date=date(2025, 7, 1),
            mark_date=date(2025, 7, 31),
            net_return=3.0,
            qqq_return=1.0,
            dia_return=0.5,
        ),
        _payload(
            cohort_month=date(2025, 8, 1),
            signal_date=date(2025, 7, 31),
            entry_date=date(2025, 8, 1),
            mark_date=date(2025, 8, 29),
            net_return=2.0,
            qqq_return=1.0,
            dia_return=0.5,
        ),
        _payload(
            cohort_month=date(2025, 9, 1),
            signal_date=date(2025, 8, 29),
            entry_date=date(2025, 9, 2),
            mark_date=date(2025, 9, 30),
            net_return=-1.0,
            qqq_return=-0.5,
            dia_return=-1.5,
        ),
    )
    for payload in payloads:
        _run_with_snapshot(
            db,
            run_date=date.fromisoformat(str(payload["mark_date"])),
            snapshot=payload,
        )
    db.commit()
    writes_before = (
        db.query(UniverseSelectionRun).count(),
        list(db.new),
        list(db.dirty),
        list(db.deleted),
    )

    response = RotationForwardScorecardService(
        db,
        now=datetime(2025, 10, 1, tzinfo=timezone.utc),
    ).get_scorecard()

    assert response is not None
    assert response.algorithm_version == ROTATION_FORWARD_SCORECARD_VERSION
    assert response.source_run_count == 3
    assert len(response.tracks) == 5
    incumbent = response.tracks[0]
    assert incumbent.status == "READY_FOR_MANUAL_REVIEW"
    assert incumbent.completed_cohorts == 3
    assert incumbent.manual_review_ready is True
    assert response.automatic_promotion_allowed is False
    assert writes_before == (
        db.query(UniverseSelectionRun).count(),
        list(db.new),
        list(db.dirty),
        list(db.deleted),
    )

    api = FastAPI()
    api.include_router(universe_api.router)

    def override_db() -> Generator[Session, None, None]:
        yield db

    api.dependency_overrides[get_db] = override_db
    client = TestClient(api)
    try:
        endpoint = client.get(
            "/api/universe/rotation-forward-scorecard"
        )
        assert endpoint.status_code == 200
        body = endpoint.json()
        assert body["algorithm_version"] == (
            ROTATION_FORWARD_SCORECARD_VERSION
        )
        assert body["tracks"][0]["completed_cohorts"] == 3
        assert body["tracks"][0]["automatic_promotion_allowed"] is False
    finally:
        client.close()
        db.close()
        engine.dispose()


def test_scorecard_endpoint_returns_404_without_terminal_run() -> None:
    engine, db = _db()
    api = FastAPI()
    api.include_router(universe_api.router)

    def override_db() -> Generator[Session, None, None]:
        yield db

    api.dependency_overrides[get_db] = override_db
    client = TestClient(api)
    try:
        response = client.get(
            "/api/universe/rotation-forward-scorecard"
        )
        assert response.status_code == 404
        assert response.json() == {
            "detail": "no universe selection run available",
        }
    finally:
        client.close()
        db.close()
        engine.dispose()
