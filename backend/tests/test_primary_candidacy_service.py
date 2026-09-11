"""Read-only candidacy evidence, independent of live switching configuration."""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Base, StrategyConfig, StrategyV2ShadowDecision, StrategyV2ShadowTrade
from app.models import UniverseSelectionCandidate, UniverseSelectionRun
from app.services.auto_primary_switch_service import _SignalEdgeBlock

NOW = datetime(2026, 9, 10, 18, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture(autouse=True)
def gate_defaults(monkeypatch):
    for name, value in {
        "enabled": True, "require_signal_edge": True, "lookback_days": 3,
        "min_samples": 60, "incumbent_trend_pct": 60.0,
        "candidate_trend_pct": 30.0, "reach_lookback_days": 30,
        "min_reach_rate_pct": 60.0, "min_closed_trades": 5,
        "max_price_age_seconds": 1800,
    }.items():
        monkeypatch.setattr(settings, f"auto_primary_switch_{name}", value)


def _run(db: Session, *, day: int = 0, status: str = "COMPLETE") -> UniverseSelectionRun:
    run = UniverseSelectionRun(as_of_date=(NOW + timedelta(days=day)).date(),
        algorithm_version="test", source_version="test", status=status)
    db.add(run)
    db.flush()
    return run


def _seed(db: Session, *, trades: int = 6, trend: int = 186) -> None:
    db.add(StrategyConfig(symbol="TSLA.US", market="US"))
    run = _run(db)
    for symbol, blocked, count, hits in (
        ("TSLA.US", 352, trades, trades), ("META.US", trend, trades, trades - 1),
        ("CAT.US", 120, 3, 0), ("MU.US", 100, 4, 2),
    ):
        db.add(UniverseSelectionCandidate(run_id=run.id, symbol=symbol, selected=True,
            metrics_json=json.dumps({"price": 100, "avg_dollar_volume": 1e10,
                "relative_spread_bps": 1 if symbol == "META.US" else 5})))
        for i in range(1000):
            db.add(StrategyV2ShadowDecision(idempotency_key=f"{symbol}-{i}",
                symbol=symbol, config_version="v1", session_date=NOW.date(),
                bar_at=NOW - timedelta(seconds=i), action="WAIT", close_price=100,
                gate_passed=i >= blocked,
                gate_reasons_json=json.dumps(["ADX_REGIME_BLOCKED"] if i < blocked else [])))
        for i in range(count):
            db.add(StrategyV2ShadowTrade(symbol=symbol, status="CLOSED",
                entry_at=NOW - timedelta(days=i % 3, minutes=30),
                exit_at=NOW - timedelta(days=i % 3), entry_price=100, quantity=1,
                net_pnl=0.5 if i % 2 else -0.5, mfe_pct=0.005 if i < hits else 0))
    db.commit()


class _FakeEdgeAssessor:
    def __init__(self, passing: bool = False) -> None:
        self.passing = passing

    def __call__(self) -> _SignalEdgeBlock | None:
        return None if self.passing else _SignalEdgeBlock("edge not proven", False)


def _service(db: Session, *, passing: bool = False):
    assert importlib.util.find_spec("app.services.primary_candidacy_service") is not None, "candidacy service is not implemented"
    from app.services.primary_candidacy_service import PrimaryCandidacyService
    return PrimaryCandidacyService(db, signal_edge_assessor=_FakeEdgeAssessor(passing), clock=lambda: NOW)


def test_unpowered_verdict_even_when_one_candidate_passes_all_gates(db):
    # Given
    _seed(db)
    # When
    report = _service(db).assess()
    # Then
    assert report.verdict == "SELECTION_NOT_SUPPORTED_BY_EVIDENCE"
    assert report.edge_pick is None
    pick = report.gates_only_pick
    assert pick is not None
    assert pick.symbol == "META.US" and pick.passing_count == 1
    assert pick.withheld_from_edge_pick_because == ["POOL_SIGNAL_EDGE_BLOCKED", "UNPOWERED"]
    rows = {row.symbol: row for row in report.candidates}
    assert "REACH_BELOW_TRADE_FLOOR" in rows["CAT.US"].gate_reasons
    assert "REACH_BELOW_TRADE_FLOOR" in rows["MU.US"].gate_reasons


def test_gates_only_pick_is_null_when_nothing_passes(db):
    _seed(db, trend=500)
    report = _service(db).assess()
    assert report.gates_only_pick is None and report.edge_pick is None


def test_edge_and_gates_only_picks_coincide_when_supported(db):
    _seed(db, trades=60)
    report = _service(db, passing=True).assess()
    assert report.verdict == "SELECTION_SUPPORTED"
    assert report.edge_pick is not None and report.gates_only_pick is not None
    assert report.edge_pick.symbol == report.gates_only_pick.symbol == "META.US"
    assert report.gates_only_pick.withheld_from_edge_pick_because == []


def test_gates_only_tiebreak_matches_evaluate_rule(db):
    _seed(db, trend=50)
    report = _service(db).assess(min_closed_trades=3, min_reach_rate_pct=50)
    assert report.gates_only_pick is not None
    assert report.gates_only_pick.symbol == "META.US"
    assert report.gates_only_pick.passing_count == 2
    rows = {row.symbol: row for row in report.candidates}
    assert "REACH_BELOW_RATE_FLOOR" in rows["CAT.US"].gate_reasons


def test_pool_gate_assessed_even_when_switch_does_not_require_it(db, monkeypatch):
    _seed(db)
    monkeypatch.setattr(settings, "auto_primary_switch_require_signal_edge", False)
    report = _service(db).assess()
    assert report.pool_gate.status == "BLOCKED"
    assert report.pool_gate.enforced_by_switch is False
    assert report.gates_only_pick is not None
    assert report.gates_only_pick.withheld_from_edge_pick_because[0] == "POOL_SIGNAL_EDGE_BLOCKED"


def test_power_unmeasurable_is_a_withhold_reason(db):
    _seed(db)
    db.query(StrategyV2ShadowTrade).filter(StrategyV2ShadowTrade.symbol != "META.US").delete()
    db.commit()
    report = _service(db).assess()
    assert report.gates_only_pick is not None
    assert "POWER_UNMEASURABLE" in report.gates_only_pick.withheld_from_edge_pick_because
    assert "UNPOWERED" not in report.gates_only_pick.withheld_from_edge_pick_because


@pytest.mark.parametrize(("blocked", "samples", "expected"), [(599, 1000, "ACCEPTABLE"), (600, 1000, "TREND_UNSUITABLE"), (59, 59, "EVIDENCE_THIN")])
def test_incumbent_status_mirrors_evaluate_thresholds(db, blocked, samples, expected):
    _seed(db)
    rows = db.scalars(select(StrategyV2ShadowDecision).where(StrategyV2ShadowDecision.symbol == "TSLA.US")).all()
    for i, row in enumerate(rows):
        if i >= samples:
            db.delete(row)
        else:
            row.gate_reasons_json = json.dumps(["ADX_REGIME_BLOCKED"] if i < blocked else [])
    db.commit()
    report = _service(db).assess(incumbent_trend_pct=60, min_samples=60)
    assert report.incumbent_status == expected


def test_report_is_computed_when_switch_disabled(db, monkeypatch):
    _seed(db)
    monkeypatch.setattr(settings, "auto_primary_switch_enabled", False)
    report = _service(db).assess()
    assert report.switch_enabled is False and report.candidates


def test_tradeability_pick_from_latest_complete_run_metrics(db):
    _seed(db)
    newer = _run(db, day=1)
    db.add(UniverseSelectionCandidate(run_id=newer.id, symbol="NEW.US", selected=True,
        metrics_json='{"price":200,"avg_dollar_volume":10000000000,"relative_spread_bps":0.5}'))
    _run(db, day=2, status="FAILED")
    db.commit()
    report = _service(db).assess()
    assert report.tradeability_pick is not None
    assert report.tradeability_pick.symbol == "NEW.US"
    assert report.tradeability_pick.metrics_as_of == newer.as_of_date


def test_defaults_resolve_from_settings_and_are_echoed(db, monkeypatch):
    _seed(db)
    service = _service(db)
    monkeypatch.setattr(settings, "auto_primary_switch_min_closed_trades", 7)
    report = service.assess()
    assert report.gate_parameters.min_closed_trades == 7
    assert report.gates_only_pick is None


def test_never_writes(db):
    _seed(db)
    before = {table.name: db.scalar(select(func.count()).select_from(table)) for table in Base.metadata.sorted_tables}
    report = _service(db).assess()
    after = {table.name: db.scalar(select(func.count()).select_from(table)) for table in Base.metadata.sorted_tables}
    assert before == after
    assert db.scalar(select(StrategyConfig.symbol)) == "TSLA.US"
    assert report.safety_gate_evaluated is False
    assert not db.new and not db.dirty and not db.deleted


def test_no_selection_run_verdict(db):
    report = _service(db).assess()
    assert report.verdict == "NO_SELECTION_RUN"
    assert report.candidates == [] and report.tradeability_pick is None
