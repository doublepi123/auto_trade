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
from app.models import Base, OpeningMomentumShadowRun
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
        with self.session_factory() as db:
            db.query(OpeningMomentumShadowRun).delete()
            db.commit()

    def teardown_method(self) -> None:
        settings.api_key = ""
        settings.opening_momentum_shadow_enabled = False
        settings.opening_momentum_challenger_enabled = False

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
        weak_breadth_wide_stop = variants[
            "WEAK_BREADTH_WIDE_STOP_CHALLENGER"
        ]
        opening_range_stop = variants[
            "OPENING_RANGE_STOP_CHALLENGER"
        ]
        execution_sndk = variants["EXECUTION_SNDK_CHALLENGER"]
        assert len(variants) == 23
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
        assert runs.json()[0]["candidate_overnight_gap_bps"] == 25.0
        assert (
            runs.json()[0]["candidate_prev_close_to_signal_bps"]
            == 105.0
        )
        assert runs.json()[0]["benchmark_qqq_return_bps"] == -8.0
        assert runs.json()[0]["benchmark_dia_return_bps"] == -12.0
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
