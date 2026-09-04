"""Signal edge service — evidence assembly over shadow trades."""
from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_signal_edge_{os.getpid()}.db"
)

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.domain.strategy_v2.signal_edge import (
    VERDICT_FAIL,
    VERDICT_INSUFFICIENT_DATA,
)
from app.models import (
    Base,
    StrategyV2ShadowConfig,
    StrategyV2ShadowTrade,
    StrategyV2ShadowVersion,
)
from app.services.signal_edge_service import SignalEdgeService


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
        db.query(StrategyV2ShadowTrade).delete()
        db.query(StrategyV2ShadowVersion).delete()
        db.query(StrategyV2ShadowConfig).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _config(self, symbol: str, *, stop: float, target: float) -> None:
        db = self._db()
        db.add(StrategyV2ShadowConfig(
            symbol=symbol,
            enabled=True,
            stop_loss_pct=stop,
            profit_target_pct=target,
        ))
        db.add(StrategyV2ShadowVersion(
            symbol=symbol,
            config_version="v1",
            config_json=json.dumps(
                {"stop_loss_pct": stop, "profit_target_pct": target}
            ),
            activated_at=datetime.now(timezone.utc),
        ))
        db.commit()
        db.close()

    def _trade(
        self,
        symbol: str,
        *,
        exit_reason: str,
        net_pnl: float,
        gross_pnl: float | None = None,
        gross_available: bool = True,
        config_version: str = "v1",
        age_days: float = 1.0,
        day_offset: int = 0,
        status: str = "CLOSED",
    ) -> None:
        db = self._db()
        exit_at = datetime.now(timezone.utc) - timedelta(days=age_days + day_offset)
        db.add(StrategyV2ShadowTrade(
            symbol=symbol,
            config_version=config_version,
            status=status,
            entry_at=exit_at - timedelta(minutes=30),
            exit_at=None if status == "OPEN" else exit_at,
            entry_price=100.0,
            quantity=1.0,
            gross_pnl=(
                net_pnl if gross_pnl is None else gross_pnl
            ) if gross_available else None,
            net_pnl=net_pnl,
            exit_reason=exit_reason,
        ))
        db.commit()
        db.close()


class TestSignalEdgeService(_Base):
    def test_distinguishes_fee_blocked_gross_edge_from_no_edge(self) -> None:
        # Given: both cohorts beat first passage, but only one has positive gross returns.
        gross_edge = (0.8, 1.0, 1.2, 0.9, 1.1)
        for symbol, direction in (("EDGE.US", 1.0), ("NOEDGE.US", -1.0)):
            self._config(symbol, stop=0.45, target=0.80)
            for i in range(25):
                gross_pnl = direction * gross_edge[i % len(gross_edge)]
                self._trade(
                    symbol,
                    exit_reason="PROFIT_TARGET" if i < 20 else "PRICE_STOP",
                    gross_pnl=gross_pnl,
                    net_pnl=gross_pnl - 1.5,
                    day_offset=i % 5,
                )

        # When: the promotion gate assesses each cohort independently.
        fee_blocked, _, _, _ = SignalEdgeService(self._db()).assess(
            symbol="EDGE.US",
            min_resolved_trades=20,
            min_distinct_days=5,
        )
        no_edge, _, _, _ = SignalEdgeService(self._db()).assess(
            symbol="NOEDGE.US",
            min_resolved_trades=20,
            min_distinct_days=5,
        )

        # Then: fees blocking a real gross edge is distinct from no gross edge.
        assert fee_blocked.verdict == "FEE_BLOCKED"
        assert no_edge.verdict == VERDICT_FAIL

    def test_missing_gross_days_remain_insufficient_data(self) -> None:
        # Given: first passage and net span 20 days, but gross exists on one day.
        self._config("PAIR.US", stop=0.45, target=0.80)
        outcomes = ["PROFIT_TARGET"] * 32 + ["PRICE_STOP"] * 8
        for index, exit_reason in enumerate(outcomes):
            self._trade(
                "PAIR.US",
                exit_reason=exit_reason,
                gross_available=index % 20 == 0,
                net_pnl=1.0 if exit_reason == "PROFIT_TARGET" else -0.5,
                day_offset=index % 20,
            )

        # When: all three evidence families use the production floors.
        verdict, _, _, _ = SignalEdgeService(self._db()).assess(
            symbol="PAIR.US",
            min_resolved_trades=30,
            min_distinct_days=20,
        )

        # Then: thin gross evidence is scarcity, never disproof.
        assert verdict.verdict == VERDICT_INSUFFICIENT_DATA
        assert verdict.gross.distinct_days == 1
        assert verdict.net.distinct_days == 1
        assert verdict.first_passage.missing_pnl_excluded == 38

    def test_no_symbol_assessment_uses_enabled_current_us_cohort(self) -> None:
        # Given: a stale minority config precedes the enabled production cohort.
        now = datetime.now(timezone.utc)
        db = self._db()
        db.add(StrategyV2ShadowConfig(
            symbol="LEGACY.US",
            enabled=False,
            stop_loss_pct=0.75,
            profit_target_pct=0.50,
        ))
        db.add(StrategyV2ShadowConfig(
            symbol="NVDA.US",
            enabled=True,
            stop_loss_pct=0.45,
            profit_target_pct=0.80,
        ))
        for index in range(105):
            symbol = f"CUR{index:03d}.US"
            config_version = f"current-{index:03d}"
            db.add(StrategyV2ShadowVersion(
                symbol=symbol,
                config_version=config_version,
                config_json=json.dumps({
                    "algorithm_version": "strategy-v2-rth-mr-v5-causal-entry",
                    "stop_loss_pct": 0.45,
                    "profit_target_pct": 0.80,
                }),
                activated_at=now,
            ))
            exit_at = now - timedelta(days=index % 25 + 1, minutes=index)
            db.add(StrategyV2ShadowTrade(
                symbol=symbol,
                config_version=config_version,
                status="CLOSED",
                entry_at=exit_at - timedelta(minutes=30),
                exit_at=exit_at,
                entry_price=100.0,
                quantity=1.0,
                gross_pnl=1.0 if index < 84 else -0.5,
                net_pnl=1.0 if index < 84 else -0.5,
                exit_reason="PROFIT_TARGET" if index < 84 else "PRICE_STOP",
            ))
        for index in range(34):
            symbol = f"OLD{index:03d}.US"
            config_version = f"legacy-{index:03d}"
            db.add(StrategyV2ShadowVersion(
                symbol=symbol,
                config_version=config_version,
                config_json=json.dumps({
                    "algorithm_version": "strategy-v2-rth-mr-v5-causal-entry",
                    "stop_loss_pct": 0.75,
                    "profit_target_pct": 0.50,
                }),
                activated_at=now,
            ))
            exit_at = now - timedelta(days=index % 25 + 1, minutes=index)
            db.add(StrategyV2ShadowTrade(
                symbol=symbol,
                config_version=config_version,
                status="CLOSED",
                entry_at=exit_at - timedelta(minutes=30),
                exit_at=exit_at,
                entry_price=100.0,
                quantity=1.0,
                gross_pnl=1.0 if index < 27 else -0.5,
                net_pnl=1.0 if index < 27 else -0.5,
                exit_reason="PROFIT_TARGET" if index < 27 else "PRICE_STOP",
            ))
        db.commit()
        db.close()

        # When: the live-style global assessment resolves its barrier cohort.
        verdict, stop, target, symbol = SignalEdgeService(self._db()).assess()

        # Then: the enabled 0.45/0.80 majority is assessed, never the first row.
        assert (stop, target, symbol) == (0.45, 0.80, None)
        assert verdict.first_passage.resolved == 105
        assert verdict.first_passage.matched_versions == 105
        assert verdict.first_passage.matched_trades == 105
        assert verdict.first_passage.provenance_excluded_trades == 34

    def test_clustered_statistic_estimates_the_per_trade_mean(self) -> None:
        # Given: one ten-trade winning day and nine one-trade losing days.
        self._config("WEIGHT.US", stop=0.45, target=0.80)
        for _ in range(10):
            self._trade(
                "WEIGHT.US",
                exit_reason="MAX_HOLD",
                gross_pnl=1.0,
                net_pnl=1.0,
            )
        for day_offset in range(1, 10):
            self._trade(
                "WEIGHT.US",
                exit_reason="MAX_HOLD",
                gross_pnl=-0.5,
                net_pnl=-0.5,
                day_offset=day_offset,
            )

        # When: clustered significance is computed for the service report.
        verdict, _, _, _ = SignalEdgeService(self._db()).assess(
            symbol="WEIGHT.US"
        )

        # Then: both the mean and CRVE t-statistic retain per-trade weighting.
        assert math.isclose(verdict.gross.naive_mean or 0.0, 5.5 / 19)
        assert math.isclose(
            verdict.gross.clustered_t or 0.0,
            0.6966666666666668,
            rel_tol=1e-12,
        )

    def test_first_passage_excludes_mismatched_barrier_versions(self) -> None:
        # Given: resolved trades span the tested barriers and a different version.
        self._config("MIX.US", stop=0.45, target=0.80)
        db = self._db()
        db.add(StrategyV2ShadowVersion(
            symbol="MIX.US",
            config_version="v2",
            config_json=json.dumps(
                {"stop_loss_pct": 0.90, "profit_target_pct": 1.60}
            ),
            activated_at=datetime.now(timezone.utc),
        ))
        db.commit()
        db.close()
        for exit_reason in ("PROFIT_TARGET", "PRICE_STOP"):
            self._trade(
                "MIX.US",
                config_version="v1",
                exit_reason=exit_reason,
                net_pnl=0.1,
            )
        for exit_reason in ("PROFIT_TARGET", "PROFIT_TARGET", "PRICE_STOP"):
            self._trade(
                "MIX.US",
                config_version="v2",
                exit_reason=exit_reason,
                net_pnl=0.1,
            )

        # When: first passage is tested against the current 0.45/0.80 barriers.
        verdict, _, _, _ = SignalEdgeService(self._db()).assess(symbol="MIX.US")

        # Then: only matching outcomes enter the exact binomial experiment.
        assert (
            verdict.first_passage.target_hits,
            verdict.first_passage.stop_hits,
            verdict.first_passage.resolved,
        ) == (1, 1, 2)
        assert verdict.first_passage.barrier_mismatch_excluded == 3

    def test_reproduces_the_live_negative_finding(self) -> None:
        """38 targets against 83 stops must not clear the gate."""
        self._config("NVDA.US", stop=0.45, target=0.80)
        for i in range(38):
            self._trade("NVDA.US", exit_reason="PROFIT_TARGET",
                        net_pnl=0.8, day_offset=i % 25)
        for i in range(83):
            self._trade("NVDA.US", exit_reason="PRICE_STOP",
                        net_pnl=-0.45, day_offset=i % 25)

        verdict, stop, target, symbol = SignalEdgeService(self._db()).assess(
            symbol="NVDA.US", min_distinct_days=20
        )
        assert (stop, target, symbol) == (0.45, 0.80, "NVDA.US")
        assert verdict.verdict == VERDICT_FAIL
        assert verdict.first_passage.resolved == 121
        assert verdict.first_passage.beats_baseline is False

    def test_barriers_come_from_the_config_the_trades_ran_under(self) -> None:
        """Judging outcomes against barriers they never used compares unlike things."""
        self._config("APP.US", stop=0.60, target=1.20)
        self._trade("APP.US", exit_reason="PROFIT_TARGET", net_pnl=1.2)

        _, stop, target, _ = SignalEdgeService(self._db()).assess(symbol="APP.US")
        assert (stop, target) == (0.60, 1.20)

    def test_explicit_barriers_override_the_config(self) -> None:
        self._config("APP.US", stop=0.60, target=1.20)
        self._trade("APP.US", exit_reason="PROFIT_TARGET", net_pnl=1.2)

        _, stop, target, _ = SignalEdgeService(self._db()).assess(
            symbol="APP.US", stop_pct=0.30, target_pct=0.90
        )
        assert (stop, target) == (0.30, 0.90)

    def test_open_trades_are_excluded(self) -> None:
        self._config("TER.US", stop=0.45, target=0.80)
        self._trade("TER.US", exit_reason="PROFIT_TARGET", net_pnl=0.8)
        self._trade("TER.US", exit_reason="", net_pnl=0.0, status="OPEN")

        verdict, _, _, _ = SignalEdgeService(self._db()).assess(symbol="TER.US")
        assert verdict.clustered.observations == 1

    def test_trades_outside_the_window_are_ignored(self) -> None:
        self._config("MU.US", stop=0.45, target=0.80)
        self._trade("MU.US", exit_reason="PROFIT_TARGET", net_pnl=0.8, age_days=200)

        verdict, _, _, _ = SignalEdgeService(self._db()).assess(
            symbol="MU.US", lookback_days=30
        )
        assert verdict.first_passage.resolved == 0
        assert verdict.verdict == VERDICT_INSUFFICIENT_DATA

    def test_max_hold_exits_inform_significance_but_not_first_passage(self) -> None:
        """A trade that touched neither barrier says nothing about first passage."""
        self._config("CAT.US", stop=0.45, target=0.80)
        for i in range(10):
            self._trade("CAT.US", exit_reason="MAX_HOLD",
                        net_pnl=-0.05, day_offset=i)

        verdict, _, _, _ = SignalEdgeService(self._db()).assess(symbol="CAT.US")
        assert verdict.first_passage.resolved == 0
        assert verdict.clustered.observations == 10

    def test_time_exit_conditioning_is_counted_and_disclosed(self) -> None:
        # Given the live exit mix: 44 target, 88 stop, 135 MAX_HOLD, 15 EOD_FLATTEN.
        self._config("LIVE.US", stop=0.45, target=0.80)
        outcomes = (
            ["PROFIT_TARGET"] * 44
            + ["PRICE_STOP"] * 88
            + ["MAX_HOLD"] * 135
            + ["EOD_FLATTEN"] * 15
        )
        for index, exit_reason in enumerate(outcomes):
            self._trade(
                "LIVE.US",
                exit_reason=exit_reason,
                net_pnl=-0.05,
                day_offset=index % 25,
            )

        # When the gate assesses the cohort.
        verdict, _, _, _ = SignalEdgeService(self._db()).assess(symbol="LIVE.US")

        # Then both time-barrier exit reasons are threaded in as excluded evidence.
        assert verdict.first_passage.resolved == 132
        assert verdict.first_passage.matched_trades == 282
        assert verdict.first_passage.time_exit_excluded == 150
        assert verdict.first_passage.time_exit_fraction is not None
        assert math.isclose(
            verdict.first_passage.time_exit_fraction, 150 / 282, rel_tol=1e-12
        )

    def test_live_shaped_cohort_reports_sensitivity_bounds_and_still_fails(
        self,
    ) -> None:
        # Given the live exit mix over enough days to clear the evidence floors.
        self._config("BOUND.US", stop=0.45, target=0.80)
        outcomes = (
            ["PROFIT_TARGET"] * 44
            + ["PRICE_STOP"] * 88
            + ["MAX_HOLD"] * 135
            + ["EOD_FLATTEN"] * 15
        )
        for index, exit_reason in enumerate(outcomes):
            self._trade(
                "BOUND.US",
                exit_reason=exit_reason,
                gross_pnl=0.1 if index % 2 == 0 else -0.12,
                net_pnl=-0.4 if index % 2 == 0 else -0.62,
                day_offset=index % 25,
            )

        # When the gate assesses it against its own driftless baseline.
        verdict, _, _, _ = SignalEdgeService(self._db()).assess(symbol="BOUND.US")

        # Then the bounds bracket the reported rate and the verdict is still FAIL.
        first_passage = verdict.first_passage
        assert first_passage.observed_rate_floor is not None
        assert first_passage.observed_rate_ceiling is not None
        assert math.isclose(first_passage.observed_rate_floor, 44 / 282, rel_tol=1e-12)
        assert math.isclose(
            first_passage.observed_rate_ceiling, 194 / 282, rel_tol=1e-12
        )
        assert first_passage.observed_rate_floor < first_passage.baseline_rate
        assert first_passage.observed_rate_ceiling > first_passage.baseline_rate
        assert verdict.verdict == VERDICT_FAIL
        assert first_passage.beats_baseline is False

    def test_missing_config_without_explicit_barriers_is_rejected(self) -> None:
        self._trade("GS.US", exit_reason="PROFIT_TARGET", net_pnl=0.8)
        try:
            SignalEdgeService(self._db()).assess(symbol="GS.US")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    def test_rejects_non_positive_lookback(self) -> None:
        try:
            SignalEdgeService(self._db()).assess(lookback_days=0)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    def test_does_not_mutate_evidence(self) -> None:
        self._config("NVDA.US", stop=0.45, target=0.80)
        for i in range(5):
            self._trade("NVDA.US", exit_reason="PROFIT_TARGET",
                        net_pnl=0.8, day_offset=i)
        SignalEdgeService(self._db()).assess(symbol="NVDA.US")

        db = self._db()
        try:
            assert db.query(StrategyV2ShadowTrade).count() == 5
        finally:
            db.close()
