from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Generator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.opening_momentum_shadow import router
from app.config import settings
from app.database import get_db
from app.domain.opening_momentum import (
    ALGORITHM_VERSION,
    OpeningMomentumConfig,
)
from app.models import (
    Base,
    OpeningMomentumExecution,
    OpeningMomentumShadowRun,
)
from app.services.opening_momentum_shadow_service import (
    OpeningMomentumShadowService,
)


_NOW = datetime(2026, 7, 23, 14, 31, tzinfo=timezone.utc)


class TestOpeningMomentumShadowApi:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=cls.engine)
        cls.session_factory = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=cls.engine,
        )
        cls.app = FastAPI()
        cls.app.include_router(router)

        def override_get_db() -> Generator[Session, None, None]:
            db = cls.session_factory()
            try:
                yield db
            finally:
                db.close()

        cls.app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(cls.app)

    @classmethod
    def teardown_class(cls) -> None:
        cls.client.close()
        cls.app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setup_method(self) -> None:
        settings.api_key = ""
        settings.opening_momentum_shadow_enabled = False
        settings.opening_momentum_challenger_enabled = False
        settings.opening_momentum_execution_enabled = False
        settings.opening_momentum_execution_paper_confirmed = False
        with self.session_factory() as db:
            db.query(OpeningMomentumShadowRun).delete()
            db.query(OpeningMomentumExecution).delete()
            db.commit()

    def teardown_method(self) -> None:
        settings.api_key = ""
        settings.opening_momentum_shadow_enabled = False
        settings.opening_momentum_challenger_enabled = False
        settings.opening_momentum_execution_enabled = False
        settings.opening_momentum_execution_paper_confirmed = False

    def test_execution_status_is_disabled_and_fail_closed(self) -> None:
        response = self.client.get(
            "/api/opening-momentum-shadow/execution/status"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "DISABLED"
        assert body["latest"] is None
        assert body["config"]["enabled"] is False
        assert body["config"]["paper_account_confirmed"] is False
        assert body["config"]["order_submission_allowed"] is False
        assert body["config"]["signal_minutes"] == 3
        assert body["config"]["execution_delay_minutes"] == 1
        assert body["config"]["holding_minutes"] == 60
        assert body["config"]["stop_loss_pct"] == 1.0
        assert body["config"]["minimum_path_efficiency"] == 0.70
        assert body["config"]["maximum_market_return_bps"] == 0.0
        assert (
            body["config"]["exceptional_minimum_path_efficiency"]
            == 0.90
        )
        assert (
            body["config"]["exceptional_maximum_market_return_bps"]
            == 5.0
        )
        assert body["config"]["forward_evidence_start_date"] == (
            "2026-07-28"
        )
        assert body["config"]["universe_source"] == (
            "NONE"
        )
        assert body["config"]["selection_run_id"] is None
        assert body["config"]["universe_size"] == 0
        assert body["config"]["universe"] == []
        assert body["config"]["required_symbols"] == []
        assert body["config"]["excluded_symbols"] == []
        assert body["config"]["universe_ready"] is False

        runs = self.client.get(
            "/api/opening-momentum-shadow/execution/runs"
        )
        assert runs.status_code == 200
        assert runs.json() == []

    def test_status_is_explicitly_shadow_only(self) -> None:
        response = self.client.get(
            "/api/opening-momentum-shadow/status"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "DISABLED"
        assert body["config"]["mode"] == "SHADOW"
        assert body["config"]["order_submission_allowed"] is False
        assert body["config"]["signal_minutes"] == 30
        assert body["config"]["execution_delay_minutes"] == 1
        assert body["config"]["holding_minutes"] == 30
        assert body["config"]["round_trip_cost_bps"] == 14.0

    def test_status_serializes_reversal_challenger(self) -> None:
        settings.opening_momentum_challenger_enabled = True

        response = self.client.get(
            "/api/opening-momentum-shadow/status"
        )

        assert response.status_code == 200
        variants = {
            item["variant"]: item
            for item in response.json()["variants"]
        }
        reversal = variants["REVERSAL_CHALLENGER"]
        early = variants["EARLY_BROAD_CHALLENGER"]
        early_sndk = variants["EARLY_SNDK_CHALLENGER"]
        execution = variants["EXECUTION_BROAD_CHALLENGER"]
        path_efficiency = variants[
            "EXECUTION_PATH_EFFICIENCY_CHALLENGER"
        ]
        weak_breadth_path = variants[
            "WEAK_BREADTH_PATH_CHALLENGER"
        ]
        weak_breadth_relaxed = variants[
            "WEAK_BREADTH_RELAXED_CHALLENGER"
        ]
        moderate_breadth_path = variants[
            "MODERATE_BREADTH_PATH_CHALLENGER"
        ]
        weak_breadth_exceptional_path = variants[
            "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER"
        ]
        quality_first_path_rerank = variants[
            "QUALITY_FIRST_PATH_RERANK_CHALLENGER"
        ]
        exceptional_path_panw_cohort = variants[
            "EXCEPTIONAL_PATH_PANW_COHORT_CHALLENGER"
        ]
        weak_breadth_index_cohort = variants[
            "WEAK_BREADTH_INDEX_COHORT_CHALLENGER"
        ]
        weak_breadth_sparse_index_cohort = variants[
            "WEAK_BREADTH_SPARSE_INDEX_COHORT_CHALLENGER"
        ]
        weak_breadth_mrvl_exclusion = variants[
            "WEAK_BREADTH_MRVL_EXCLUSION_CHALLENGER"
        ]
        weak_breadth_wide_stop = variants[
            "WEAK_BREADTH_WIDE_STOP_CHALLENGER"
        ]
        etf_regime = variants["ETF_REGIME_PATH_CHALLENGER"]
        opening_range_stop = variants[
            "OPENING_RANGE_STOP_CHALLENGER"
        ]
        five_minute_orb = variants["FIVE_MINUTE_ORB_CHALLENGER"]
        stocks_in_play_orb = variants[
            "STOCKS_IN_PLAY_ORB_CHALLENGER"
        ]
        stocks_in_play_orb_top10 = variants[
            "STOCKS_IN_PLAY_ORB_TOP10_CHALLENGER"
        ]
        stocks_in_play_orb_top5 = variants[
            "STOCKS_IN_PLAY_ORB_TOP5_CHALLENGER"
        ]
        index_catalog_orb = variants[
            "INDEX_CATALOG_FIVE_MINUTE_ORB_CHALLENGER"
        ]
        index_catalog_stocks_in_play = variants[
            "INDEX_CATALOG_STOCKS_IN_PLAY_ORB_CHALLENGER"
        ]
        index_catalog_stocks_in_play_top10 = variants[
            "INDEX_CATALOG_STOCKS_IN_PLAY_ORB_TOP10_CHALLENGER"
        ]
        index_catalog_stocks_in_play_top5 = variants[
            "INDEX_CATALOG_STOCKS_IN_PLAY_ORB_TOP5_CHALLENGER"
        ]
        execution_sndk = variants["EXECUTION_SNDK_CHALLENGER"]
        assert len(variants) == 43
        assert early["universe_source"] == "OPENING_EARLY_BROAD"
        assert early["signal_minutes"] == 3
        assert early["minimum_market_return_bps"] == -50.0
        assert early["minimum_candidate_return_bps"] == 50.0
        assert early["minimum_excess_return_bps"] == 25.0
        assert early["minimum_data_coverage"] == 0.95
        assert early["holding_minutes"] == 120
        assert early["required_symbols"] == []
        assert early["comparison_baseline"] == "INCUMBENT"
        assert early_sndk["universe_source"] == "OPENING_EARLY_SNDK"
        assert early_sndk["signal_minutes"] == 3
        assert early_sndk["holding_minutes"] == 120
        assert early_sndk["minimum_data_coverage"] == 0.95
        assert early_sndk["required_symbols"] == ["SNDK.US"]
        assert (
            early_sndk["comparison_baseline"]
            == "EARLY_BROAD_CHALLENGER"
        )
        extension_symbols = {
            "EARLY_RKLB_CHALLENGER": "RKLB.US",
            "EARLY_WDAY_CHALLENGER": "WDAY.US",
            "EARLY_SNDK_CHALLENGER": "SNDK.US",
            "EARLY_ALAB_CHALLENGER": "ALAB.US",
            "EARLY_LITE_CHALLENGER": "LITE.US",
            "EARLY_QCOM_CHALLENGER": "QCOM.US",
        }
        for variant, symbol in extension_symbols.items():
            extension = variants[variant]
            assert extension["required_symbols"] == [symbol]
            assert (
                extension["comparison_baseline"]
                == "EARLY_BROAD_CHALLENGER"
            )
            assert (
                extension["comparison"][
                    "policy_displacement_sessions"
                ]
                == 0
            )
            assert (
                extension["comparison"][
                    "minimum_policy_displacement_sessions"
                ]
                == 3
            )
            assert (
                extension["comparison"]["evidence_gate_passed"]
                is False
            )
            assert (
                extension["comparison"]["multiple_testing_method"]
                == "HOLM_BONFERRONI"
            )
            assert (
                extension["comparison"][
                    "multiple_testing_family_size"
                ]
                == 6
            )
            assert (
                extension["comparison"][
                    "multiple_testing_adjusted_pvalue"
                ]
                is None
            )
            assert (
                extension["comparison"][
                    "multiple_testing_evidence_passed"
                ]
                is None
            )
        assert execution["universe_source"] == "OPENING_EXECUTION_BROAD"
        assert execution["signal_minutes"] == 3
        assert execution["holding_minutes"] == 60
        assert execution["stop_loss_pct"] == 1.0
        assert execution["minimum_data_coverage"] == 0.95
        assert execution["required_symbols"] == []
        assert execution["comparison_baseline"] == "INCUMBENT"
        assert execution["minimum_path_efficiency"] is None
        assert path_efficiency["universe_source"] == (
            "OPENING_EXECUTION_PATH_EFFICIENCY"
        )
        assert path_efficiency["signal_minutes"] == 3
        assert path_efficiency["holding_minutes"] == 60
        assert path_efficiency["stop_loss_pct"] == 1.0
        assert path_efficiency["minimum_data_coverage"] == 0.95
        assert path_efficiency["minimum_path_efficiency"] == 0.70
        assert path_efficiency["required_symbols"] == []
        assert path_efficiency["comparison_baseline"] == (
            "EXECUTION_BROAD_CHALLENGER"
        )
        assert path_efficiency["comparison"] is not None
        assert weak_breadth_path["universe_source"] == (
            "OPENING_EXECUTION_WEAK_BREADTH_PATH"
        )
        assert weak_breadth_path["signal_minutes"] == 3
        assert weak_breadth_path["holding_minutes"] == 60
        assert weak_breadth_path["stop_loss_pct"] == 1.0
        assert weak_breadth_path["minimum_data_coverage"] == 0.95
        assert weak_breadth_path["minimum_path_efficiency"] == 0.70
        assert weak_breadth_path["maximum_market_return_bps"] == 0.0
        assert weak_breadth_path["required_symbols"] == []
        assert weak_breadth_path["comparison_baseline"] == (
            "EXECUTION_BROAD_CHALLENGER"
        )
        assert weak_breadth_path["comparison"] is not None
        assert weak_breadth_relaxed["universe_source"] == (
            "OPENING_EXECUTION_WEAK_BREADTH_RELAXED"
        )
        assert weak_breadth_relaxed["minimum_path_efficiency"] == 0.70
        assert weak_breadth_relaxed["maximum_market_return_bps"] == 5.0
        assert weak_breadth_relaxed["required_symbols"] == []
        assert weak_breadth_relaxed["comparison_baseline"] == (
            "WEAK_BREADTH_PATH_CHALLENGER"
        )
        assert weak_breadth_relaxed["comparison"] is not None
        relaxed_comparison = weak_breadth_relaxed["comparison"]
        assert relaxed_comparison["policy_displacement_sessions"] == 0
        assert (
            relaxed_comparison["minimum_policy_displacement_sessions"]
            == 3
        )
        assert relaxed_comparison["evidence_gate_passed"] is False
        assert moderate_breadth_path["universe_source"] == (
            "OPENING_EXECUTION_MODERATE_BREADTH_PATH"
        )
        assert moderate_breadth_path[
            "minimum_path_efficiency"
        ] == 0.70
        assert moderate_breadth_path[
            "maximum_market_return_bps"
        ] == 20.0
        assert moderate_breadth_path["required_symbols"] == []
        assert moderate_breadth_path[
            "forward_evidence_start_date"
        ] == "2026-07-28"
        assert moderate_breadth_path["comparison_baseline"] == (
            "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER"
        )
        assert moderate_breadth_path["comparison"] is not None
        moderate_comparison = moderate_breadth_path["comparison"]
        assert moderate_comparison["policy_displacement_sessions"] == 0
        assert moderate_comparison[
            "minimum_policy_displacement_sessions"
        ] == 3
        assert moderate_comparison["evidence_gate_passed"] is False
        assert moderate_comparison[
            "multiple_testing_family_size"
        ] == 4
        assert weak_breadth_exceptional_path["universe_source"] == (
            "OPENING_EXECUTION_WEAK_BREADTH_EXCEPTIONAL_PATH"
        )
        assert (
            weak_breadth_exceptional_path["minimum_path_efficiency"]
            == 0.70
        )
        assert (
            weak_breadth_exceptional_path[
                "maximum_market_return_bps"
            ]
            == 0.0
        )
        assert (
            weak_breadth_exceptional_path[
                "exceptional_minimum_path_efficiency"
            ]
            == 0.90
        )
        assert (
            weak_breadth_exceptional_path[
                "exceptional_maximum_market_return_bps"
            ]
            == 5.0
        )
        assert weak_breadth_exceptional_path["required_symbols"] == []
        assert (
            weak_breadth_exceptional_path[
                "forward_evidence_start_date"
            ]
            == "2026-07-28"
        )
        assert (
            weak_breadth_exceptional_path[
                "excluded_pre_forward_sessions"
            ]
            == 0
        )
        assert (
            weak_breadth_exceptional_path["comparison_baseline"]
            == "WEAK_BREADTH_PATH_CHALLENGER"
        )
        assert weak_breadth_exceptional_path["comparison"] is not None
        assert (
            weak_breadth_exceptional_path["candidate_selection_mode"]
            == "TOP_THEN_GATE"
        )
        exceptional_comparison = weak_breadth_exceptional_path[
            "comparison"
        ]
        assert exceptional_comparison["policy_displacement_sessions"] == 0
        assert exceptional_comparison["evidence_gate_passed"] is False
        assert (
            exceptional_comparison["multiple_testing_family_size"]
            == 7
        )
        assert quality_first_path_rerank["universe_source"] == (
            "OPENING_EXECUTION_QUALITY_FIRST_PATH_RERANK"
        )
        assert quality_first_path_rerank["candidate_selection_mode"] == (
            "PATH_ELIGIBLE_RERANK"
        )
        assert quality_first_path_rerank[
            "minimum_path_efficiency"
        ] == 0.70
        assert quality_first_path_rerank[
            "maximum_market_return_bps"
        ] == 0.0
        assert quality_first_path_rerank[
            "exceptional_minimum_path_efficiency"
        ] == 0.90
        assert quality_first_path_rerank[
            "exceptional_maximum_market_return_bps"
        ] == 5.0
        assert quality_first_path_rerank[
            "forward_evidence_start_date"
        ] == "2026-07-28"
        assert quality_first_path_rerank["comparison_baseline"] == (
            "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER"
        )
        assert quality_first_path_rerank["comparison"] is not None
        assert quality_first_path_rerank["comparison"][
            "multiple_testing_family_size"
        ] == 4
        assert exceptional_path_panw_cohort["universe_source"] == (
            "OPENING_EXECUTION_EXCEPTIONAL_PANW_COHORT"
        )
        assert exceptional_path_panw_cohort[
            "minimum_path_efficiency"
        ] == 0.70
        assert exceptional_path_panw_cohort[
            "maximum_market_return_bps"
        ] == 0.0
        assert exceptional_path_panw_cohort[
            "exceptional_minimum_path_efficiency"
        ] == 0.90
        assert exceptional_path_panw_cohort[
            "exceptional_maximum_market_return_bps"
        ] == 5.0
        assert exceptional_path_panw_cohort["required_symbols"] == [
            "PANW.US"
        ]
        assert exceptional_path_panw_cohort[
            "forward_evidence_start_date"
        ] == "2026-07-28"
        assert exceptional_path_panw_cohort["comparison_baseline"] == (
            "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER"
        )
        assert exceptional_path_panw_cohort["comparison"] is not None
        assert exceptional_path_panw_cohort["comparison"][
            "multiple_testing_family_size"
        ] == 4
        assert weak_breadth_index_cohort["universe_source"] == (
            "OPENING_EXECUTION_WEAK_BREADTH_INDEX_COHORT"
        )
        assert (
            weak_breadth_index_cohort["minimum_path_efficiency"]
            == 0.70
        )
        assert (
            weak_breadth_index_cohort["maximum_market_return_bps"]
            == 0.0
        )
        assert weak_breadth_index_cohort["required_symbols"] == ["PANW.US"]
        assert weak_breadth_index_cohort["comparison_baseline"] == (
            "WEAK_BREADTH_PATH_CHALLENGER"
        )
        assert weak_breadth_index_cohort["comparison"] is not None
        cohort_comparison = weak_breadth_index_cohort["comparison"]
        assert (
            cohort_comparison["policy_displacement_sessions"]
            == 0
        )
        assert (
            cohort_comparison["minimum_policy_displacement_sessions"]
            == 3
        )
        assert weak_breadth_sparse_index_cohort["universe_source"] == (
            "OPENING_EXECUTION_WEAK_BREADTH_SPARSE_INDEX_COHORT"
        )
        assert (
            weak_breadth_sparse_index_cohort[
                "minimum_path_efficiency"
            ]
            == 0.70
        )
        assert (
            weak_breadth_sparse_index_cohort[
                "maximum_market_return_bps"
            ]
            == 0.0
        )
        assert weak_breadth_sparse_index_cohort["required_symbols"] == [
            "SNDK.US",
            "STX.US",
            "CRWD.US",
            "ABNB.US",
            "CPRT.US",
        ]
        assert weak_breadth_sparse_index_cohort["comparison_baseline"] == (
            "WEAK_BREADTH_PATH_CHALLENGER"
        )
        assert weak_breadth_sparse_index_cohort["comparison"] is not None
        sparse_cohort_comparison = (
            weak_breadth_sparse_index_cohort["comparison"]
        )
        assert (
            sparse_cohort_comparison[
                "minimum_policy_displacement_sessions"
            ]
            == 3
        )
        assert (
            sparse_cohort_comparison["multiple_testing_family_size"]
            == 7
        )
        assert (
            cohort_comparison["evidence_gate_passed"]
            is False
        )
        assert (
            cohort_comparison["multiple_testing_family_size"]
            == 7
        )
        assert weak_breadth_mrvl_exclusion["universe_source"] == (
            "OPENING_EXECUTION_WEAK_BREADTH_EX_MRVL"
        )
        assert weak_breadth_mrvl_exclusion[
            "minimum_path_efficiency"
        ] == 0.70
        assert weak_breadth_mrvl_exclusion[
            "maximum_market_return_bps"
        ] == 0.0
        assert weak_breadth_mrvl_exclusion["required_symbols"] == []
        assert weak_breadth_mrvl_exclusion["excluded_symbols"] == [
            "MRVL.US"
        ]
        assert weak_breadth_mrvl_exclusion[
            "forward_evidence_start_date"
        ] == "2026-07-28"
        assert weak_breadth_mrvl_exclusion["comparison_baseline"] == (
            "WEAK_BREADTH_PATH_CHALLENGER"
        )
        assert weak_breadth_mrvl_exclusion["comparison"] is not None
        assert weak_breadth_mrvl_exclusion["comparison"][
            "minimum_policy_displacement_sessions"
        ] == 3
        assert weak_breadth_mrvl_exclusion["comparison"][
            "multiple_testing_family_size"
        ] == 7
        assert weak_breadth_wide_stop["universe_source"] == (
            "OPENING_EXECUTION_WEAK_BREADTH_WIDE_STOP"
        )
        assert weak_breadth_wide_stop["signal_minutes"] == 3
        assert weak_breadth_wide_stop["holding_minutes"] == 60
        assert weak_breadth_wide_stop["stop_loss_pct"] == 4.0
        assert weak_breadth_wide_stop["minimum_data_coverage"] == 0.95
        assert (
            weak_breadth_wide_stop["minimum_path_efficiency"]
            == 0.70
        )
        assert (
            weak_breadth_wide_stop["maximum_market_return_bps"]
            == 0.0
        )
        assert weak_breadth_wide_stop["required_symbols"] == []
        assert weak_breadth_wide_stop["comparison_baseline"] == (
            "WEAK_BREADTH_PATH_CHALLENGER"
        )
        assert weak_breadth_wide_stop["comparison"] is not None
        assert etf_regime["universe_source"] == (
            "OPENING_EXECUTION_ETF_REGIME"
        )
        assert etf_regime["minimum_path_efficiency"] == 0.70
        assert etf_regime["maximum_market_return_bps"] is None
        assert (
            etf_regime["maximum_benchmark_average_return_bps"]
            == 0.0
        )
        assert etf_regime["comparison_baseline"] == (
            "WEAK_BREADTH_PATH_CHALLENGER"
        )
        for variant, symbol in (
            ("ETF_REGIME_CRWD_CHALLENGER", "CRWD.US"),
            ("ETF_REGIME_TRV_CHALLENGER", "TRV.US"),
        ):
            extension = variants[variant]
            assert extension["required_symbols"] == [symbol]
            assert extension["minimum_path_efficiency"] == 0.70
            assert (
                extension["maximum_benchmark_average_return_bps"]
                == 0.0
            )
            assert extension["comparison_baseline"] == (
                "ETF_REGIME_PATH_CHALLENGER"
            )
        assert opening_range_stop["universe_source"] == (
            "OPENING_EXECUTION_RANGE_STOP"
        )
        assert opening_range_stop["signal_minutes"] == 3
        assert opening_range_stop["holding_minutes"] == 60
        assert opening_range_stop["stop_loss_pct"] == 4.0
        assert opening_range_stop["minimum_data_coverage"] == 0.95
        assert opening_range_stop["minimum_path_efficiency"] is None
        assert opening_range_stop["maximum_market_return_bps"] is None
        assert opening_range_stop["required_symbols"] == []
        assert opening_range_stop["comparison_baseline"] == (
            "EXECUTION_BROAD_CHALLENGER"
        )
        assert opening_range_stop["comparison"] is not None
        assert five_minute_orb["universe_source"] == (
            "OPENING_FIVE_MINUTE_ORB"
        )
        assert five_minute_orb["signal_minutes"] == 5
        assert five_minute_orb["holding_minutes"] == 60
        assert five_minute_orb["stop_loss_pct"] == 4.0
        assert five_minute_orb["minimum_data_coverage"] == 0.95
        assert five_minute_orb["minimum_market_return_bps"] == -10_000.0
        assert five_minute_orb["minimum_candidate_return_bps"] == 0.0
        assert five_minute_orb["minimum_excess_return_bps"] == 0.0
        assert five_minute_orb["forward_evidence_start_date"] == (
            "2026-07-28"
        )
        assert five_minute_orb["comparison_baseline"] == (
            "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER"
        )
        assert five_minute_orb["comparison"] is not None
        assert five_minute_orb["comparison"][
            "minimum_policy_displacement_sessions"
        ] == 3
        assert five_minute_orb["comparison"][
            "multiple_testing_family_size"
        ] == 4
        assert stocks_in_play_orb["universe_source"] == (
            "OPENING_FIVE_MINUTE_ORB_STOCKS_IN_PLAY"
        )
        assert stocks_in_play_orb["signal_minutes"] == 5
        assert stocks_in_play_orb["holding_minutes"] == 60
        assert stocks_in_play_orb["stop_loss_pct"] == 4.0
        assert stocks_in_play_orb["minimum_data_coverage"] == 0.95
        assert stocks_in_play_orb["candidate_selection_mode"] == (
            "OPENING_ACTIVITY_TOP_N_THEN_BREAKOUT"
        )
        assert stocks_in_play_orb["opening_activity_top_n"] == 20
        assert stocks_in_play_orb["forward_evidence_start_date"] == (
            "2026-07-28"
        )
        assert stocks_in_play_orb["comparison_baseline"] == (
            "FIVE_MINUTE_ORB_CHALLENGER"
        )
        assert stocks_in_play_orb["comparison"] is not None
        assert stocks_in_play_orb["comparison"][
            "minimum_policy_displacement_sessions"
        ] == 3
        assert stocks_in_play_orb["comparison"][
            "multiple_testing_family_size"
        ] == 4
        for sensitivity, top_n in (
            (stocks_in_play_orb_top10, 10),
            (stocks_in_play_orb_top5, 5),
        ):
            assert sensitivity["universe_source"] == (
                "OPENING_FIVE_MINUTE_ORB_STOCKS_IN_PLAY_"
                f"TOP{top_n}"
            )
            assert sensitivity["candidate_selection_mode"] == (
                "OPENING_ACTIVITY_TOP_N_THEN_BREAKOUT"
            )
            assert sensitivity["opening_activity_top_n"] == top_n
            assert sensitivity["forward_evidence_start_date"] == (
                "2026-07-28"
            )
            assert sensitivity["comparison_baseline"] == (
                "FIVE_MINUTE_ORB_CHALLENGER"
            )
            assert sensitivity["comparison"] is not None
            assert sensitivity["comparison"][
                "multiple_testing_family_size"
            ] == 4
        assert index_catalog_orb["universe_source"] == (
            "OPENING_INDEX_CATALOG_FIVE_MINUTE_ORB"
        )
        assert index_catalog_orb["signal_minutes"] == 5
        assert index_catalog_orb["holding_minutes"] == 60
        assert index_catalog_orb["stop_loss_pct"] == 4.0
        assert index_catalog_orb["minimum_data_coverage"] == 0.95
        assert index_catalog_orb["forward_evidence_start_date"] == (
            "2026-07-28"
        )
        assert index_catalog_orb["comparison_baseline"] == (
            "FIVE_MINUTE_ORB_CHALLENGER"
        )
        assert index_catalog_orb["comparison"] is not None
        assert index_catalog_orb["comparison"][
            "multiple_testing_family_size"
        ] == 4
        for sensitivity, top_n in (
            (index_catalog_stocks_in_play, 20),
            (index_catalog_stocks_in_play_top10, 10),
            (index_catalog_stocks_in_play_top5, 5),
        ):
            suffix = "" if top_n == 20 else f"_TOP{top_n}"
            assert sensitivity["universe_source"] == (
                "OPENING_INDEX_CATALOG_FIVE_MINUTE_ORB_STOCKS_IN_PLAY"
                f"{suffix}"
            )
            assert sensitivity["candidate_selection_mode"] == (
                "OPENING_ACTIVITY_TOP_N_THEN_BREAKOUT"
            )
            assert sensitivity["opening_activity_top_n"] == top_n
            assert sensitivity["forward_evidence_start_date"] == (
                "2026-07-28"
            )
            assert sensitivity["comparison_baseline"] == (
                "INDEX_CATALOG_FIVE_MINUTE_ORB_CHALLENGER"
            )
            assert sensitivity["comparison"] is not None
            assert sensitivity["comparison"][
                "multiple_testing_family_size"
            ] == 3
        assert execution_sndk["required_symbols"] == ["SNDK.US"]
        assert (
            execution_sndk["comparison_baseline"]
            == "EXECUTION_BROAD_CHALLENGER"
        )
        execution_symbols = {
            "EXECUTION_SNDK_CHALLENGER": "SNDK.US",
            "EXECUTION_INTC_CHALLENGER": "INTC.US",
            "EXECUTION_QCOM_CHALLENGER": "QCOM.US",
            "EXECUTION_RKLB_CHALLENGER": "RKLB.US",
            "EXECUTION_PANW_CHALLENGER": "PANW.US",
            "EXECUTION_CRWD_CHALLENGER": "CRWD.US",
        }
        for variant, symbol in execution_symbols.items():
            extension = variants[variant]
            assert extension["required_symbols"] == [symbol]
            assert extension["holding_minutes"] == 60
            assert extension["stop_loss_pct"] == 1.0
            assert (
                extension["comparison_baseline"]
                == "EXECUTION_BROAD_CHALLENGER"
            )
            assert (
                extension["comparison"][
                    "minimum_policy_displacement_sessions"
                ]
                == 3
            )
        assert "forward-only-two-slice-positive-tail" in variants[
            "EXECUTION_CRWD_CHALLENGER"
        ]["algorithm_version"]
        assert reversal["universe_source"] == "OPENING_REVERSAL"
        assert (
            reversal["algorithm_version"]
            == "cross-sectional-opening-reversal-v1"
        )
        assert reversal["minimum_market_return_bps"] == -25.0
        assert reversal["signal_minutes"] == 30
        assert reversal["minimum_data_coverage"] == 1.0
        assert reversal["holding_minutes"] == 30
        assert reversal["comparison"] is not None
        assert reversal["comparison"]["promotion_ready"] is False

    def test_runs_endpoint_serializes_evidence_and_metrics(self) -> None:
        config = OpeningMomentumConfig()
        with self.session_factory() as db:
            service = OpeningMomentumShadowService(db, config=config)
            db.add(
                OpeningMomentumShadowRun(
                    session_date=date(2026, 7, 23),
                    algorithm_version=ALGORITHM_VERSION,
                    config_version=(
                        service._incumbent_config_version()
                    ),
                    status="CLOSED",
                    reason="FIXED_HOLD_EXIT",
                    signal_at=_NOW,
                    observed_at=_NOW,
                    universe_source="UNIVERSE_SELECTION",
                    universe_size=8,
                    universe_json='["AAPL.US","MSFT.US"]',
                    excluded_symbols_json="{}",
                    ranking_json=(
                        '[{"symbol":"AAPL.US",'
                        '"opening_return_bps":80.0}]'
                    ),
                    candidate_symbol="AAPL.US",
                    market_return_bps=10.0,
                    candidate_return_bps=80.0,
                    excess_return_bps=70.0,
                    candidate_first_five_return_bps=25.0,
                    candidate_last_five_return_bps=18.0,
                    candidate_path_efficiency=0.42,
                    candidate_max_pullback_bps=-35.0,
                    candidate_opening_range_bps=130.0,
                    candidate_signal_turnover=31_000_000.0,
                    candidate_avg_dollar_volume=1_250_000_000.0,
                    candidate_signal_turnover_ratio=0.0248,
                    candidate_overnight_gap_bps=25.0,
                    candidate_prev_close_to_signal_bps=105.0,
                    benchmark_qqq_return_bps=-8.0,
                    benchmark_dia_return_bps=-12.0,
                    entry_at=_NOW,
                    entry_price=100.0,
                    exit_due_at=_NOW,
                    exit_at=_NOW,
                    exit_price=101.0,
                    gross_return_bps=100.0,
                    estimated_cost_bps=14.0,
                    net_return_bps=86.0,
                )
            )
            db.commit()

        runs = self.client.get(
            "/api/opening-momentum-shadow/runs",
            params={"limit": 1},
        )
        status = self.client.get(
            "/api/opening-momentum-shadow/status"
        )

        assert runs.status_code == 200
        assert runs.json()[0]["candidate_symbol"] == "AAPL.US"
        assert runs.json()[0]["ranking"][0]["opening_return_bps"] == 80.0
        assert runs.json()[0]["candidate_first_five_return_bps"] == 25.0
        assert runs.json()[0]["candidate_path_efficiency"] == 0.42
        assert runs.json()[0]["candidate_max_pullback_bps"] == -35.0
        assert runs.json()[0]["candidate_signal_turnover"] == 31_000_000.0
        assert (
            runs.json()[0]["candidate_avg_dollar_volume"]
            == 1_250_000_000.0
        )
        assert runs.json()[0]["candidate_signal_turnover_ratio"] == 0.0248
        assert runs.json()[0]["candidate_overnight_gap_bps"] == 25.0
        assert (
            runs.json()[0]["candidate_prev_close_to_signal_bps"]
            == 105.0
        )
        assert runs.json()[0]["benchmark_qqq_return_bps"] == -8.0
        assert runs.json()[0]["benchmark_dia_return_bps"] == -12.0
        assert runs.json()[0]["benchmark_average_return_bps"] == -10.0
        assert status.json()["metrics"]["closed_trades"] == 1
        assert status.json()["metrics"]["cumulative_net_return_bps"] == 86.0

    def test_router_enforces_api_key(self) -> None:
        settings.api_key = "opening-secret"

        assert self.client.get(
            "/api/opening-momentum-shadow/status"
        ).status_code == 401
        response = self.client.get(
            "/api/opening-momentum-shadow/status",
            headers={"X-API-Key": "opening-secret"},
        )

        assert response.status_code == 200
