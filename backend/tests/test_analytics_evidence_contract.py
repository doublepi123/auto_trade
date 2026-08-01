from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base
from app.services.asymmetry_service import AsymmetryService
from app.services.autocorrelation_service import AutocorrelationService
from app.services.capital_efficiency_service import CapitalEfficiencyService
from app.services.concentration_service import ConcentrationService
from app.services.daily_consistency_service import DailyConsistencyService
from app.services.decay_detection_service import DecayDetectionService
from app.services.distribution_shape_service import DistributionShapeService
from app.services.drawdown_duration_service import DrawdownDurationService
from app.services.edge_quality_service import EdgeQualityService
from app.services.exit_efficiency_service import ExitEfficiencyService
from app.services.fee_drag_service import FeeDragService
from app.services.first_trade_service import FirstTradeService
from app.services.holding_time_service import HoldingTimeService
from app.services.intraday_seasonality_service import IntradaySeasonalityService
from app.services.loss_containment_service import LossContainmentService
from app.services.milestone_service import MilestoneService
from app.services.momentum_ranking_service import MomentumRankingService
from app.services.prediction_score_service import PredictionScoreService
from app.services.profit_concentration_service import ProfitConcentrationService
from app.services.profit_factor_service import ProfitFactorService
from app.services.r_multiples_service import RMultiplesService
from app.services.reentry_analysis_service import ReentryAnalysisService
from app.services.regime_sensitivity_service import RegimeSensitivityService
from app.services.return_calendar_service import ReturnCalendarService
from app.services.robustness_service import RobustnessService
from app.services.rolling_var_service import RollingVarService
from app.services.scratch_analysis_service import ScratchAnalysisService
from app.services.size_impact_service import SizeImpactService
from app.services.trade_frequency_service import TradeFrequencyService


AnalyticsCall = Callable[[Session], dict[str, Any]]


_ANALYTICS: list[tuple[str, AnalyticsCall]] = [
    ("asymmetry", lambda db: AsymmetryService(db).analyze()),
    ("autocorrelation", lambda db: AutocorrelationService(db).analyze()),
    ("capital_efficiency", lambda db: CapitalEfficiencyService(db).analyze()),
    ("concentration", lambda db: ConcentrationService(db).analyze()),
    ("daily_consistency", lambda db: DailyConsistencyService(db).summary()),
    ("decay_detection", lambda db: DecayDetectionService(db).detect()),
    ("distribution_shape", lambda db: DistributionShapeService(db).analyze()),
    ("drawdown_duration", lambda db: DrawdownDurationService(db).analyze()),
    ("edge_quality", lambda db: EdgeQualityService(db).score()),
    ("exit_efficiency", lambda db: ExitEfficiencyService(db).summary()),
    ("fee_drag", lambda db: FeeDragService(db).summary()),
    ("first_trade", lambda db: FirstTradeService(db).summary()),
    ("holding_time", lambda db: HoldingTimeService(db).analyze()),
    ("intraday_seasonality", lambda db: IntradaySeasonalityService(db).analyze()),
    ("loss_containment", lambda db: LossContainmentService(db).summary()),
    ("milestones", lambda db: MilestoneService(db).track()),
    ("momentum_ranking", lambda db: MomentumRankingService(db).rank()),
    ("prediction_score", lambda db: PredictionScoreService(db).analyze()),
    ("profit_concentration", lambda db: ProfitConcentrationService(db).summary()),
    ("profit_factor", lambda db: ProfitFactorService(db).analyze()),
    ("r_multiples", lambda db: RMultiplesService(db).distribution()),
    ("reentry_analysis", lambda db: ReentryAnalysisService(db).summary()),
    ("regime_sensitivity", lambda db: RegimeSensitivityService(db).analyze()),
    ("return_calendar", lambda db: ReturnCalendarService(db).compute()),
    ("robustness", lambda db: RobustnessService(db).score()),
    ("rolling_var", lambda db: RollingVarService(db).compute()),
    ("scratch_analysis", lambda db: ScratchAnalysisService(db).summary()),
    ("size_impact", lambda db: SizeImpactService(db).analyze()),
    ("trade_frequency", lambda db: TradeFrequencyService(db).analyze()),
]


@pytest.mark.parametrize(("name", "invoke"), _ANALYTICS, ids=[item[0] for item in _ANALYTICS])
def test_every_order_analytics_empty_branch_exposes_evidence_contract(
    name: str,
    invoke: AnalyticsCall,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        result = invoke(db)

    assert result.get("error"), name
    assert result["statistics_quality"] == {
        "status": "COMPLETE",
        "known_exclusion_count": 0,
        "unresolved_issue_count": 0,
        "omitted_day_count": 0,
        "items": [],
    }
    assert result["currency"] is None
    assert result["currencies"] == []
    assert result["totals_comparable"] is True
