"""Signal consensus matrix — service + API. Per-module sqlite."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Generator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.signal_consensus import router
from app.config import settings
from app.database import get_db
from app.models import (
    Base,
    LLMInteraction,
    OpeningMomentumShadowRun,
    StrategyConfig,
    StrategyV2ShadowTrade,
    TradeEvent,
    WatchlistScore,
)
from app.services.signal_consensus_service import SignalConsensusService


_NOW = datetime(2026, 7, 30, 14, 31, tzinfo=timezone.utc)


class _Base:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=cls.engine)
        cls.session_factory = sessionmaker(
            autocommit=False, autoflush=False, bind=cls.engine,
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
        # Auth disabled in dev/test (settings.api_key == "").
        settings.api_key = ""
        with self.session_factory() as db:
            for model in (
                TradeEvent,
                StrategyConfig,
                StrategyV2ShadowTrade,
                OpeningMomentumShadowRun,
                WatchlistScore,
                LLMInteraction,
            ):
                db.query(model).delete()
            db.commit()

    def _db(self) -> Session:
        return self.session_factory()


class TestSignalConsensusEmpty(_Base):
    def test_empty_matrix_returns_empty_list(self) -> None:
        rows = SignalConsensusService(self._db()).get_matrix()
        assert rows == []

    def test_empty_summary_returns_zeros(self) -> None:
        summary = SignalConsensusService(self._db()).get_summary()
        assert summary == {
            "total_symbols": 0,
            "agree_bullish": 0,
            "agree_bearish": 0,
            "mixed": 0,
            "insufficient": 0,
        }

    def test_explicit_symbols_with_no_data_returns_insufficient(self) -> None:
        # An explicit symbol with no sources at all still produces a row,
        # but every source is NO_DATA → INSUFFICIENT_DATA.
        rows = SignalConsensusService(self._db()).get_matrix(["AAPL.US"])
        assert len(rows) == 1
        assert rows[0]["symbol"] == "AAPL.US"
        assert rows[0]["consensus"] == "INSUFFICIENT_DATA"
        assert rows[0]["agreement_score"] == 0.0
        for source in rows[0]["sources"].values():
            assert source["signal"] == "NO_DATA"

    def test_api_matrix_endpoint_empty(self) -> None:
        resp = self.client.get("/api/signal-consensus/matrix")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_api_summary_endpoint_empty(self) -> None:
        resp = self.client.get("/api/signal-consensus/summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_symbols"] == 0


class TestSignalConsensusAgreement(_Base):
    def test_agree_bullish_when_majority_sources_bullish(self) -> None:
        db = self._db()
        # Range engine: price below buy_low → BULLISH
        db.add(StrategyConfig(symbol="AAPL.US", buy_low=100.0, sell_high=110.0))
        db.add(TradeEvent(
            symbol="AAPL.US",
            event_type="ORDER_SKIPPED",
            payload_json='{"last_price": 95.0}',
            created_at=_NOW,
        ))
        # Quant: BUY → BULLISH
        db.add(WatchlistScore(
            symbol="AAPL.US", score=85.0, confidence=0.8,
            recommended_action="BUY", created_at=_NOW, expires_at=_NOW,
        ))
        # LLM: BUY → BULLISH
        db.add(LLMInteraction(
            symbol="AAPL.US", order_action="BUY",
            success=True, created_at=_NOW,
        ))
        db.commit()
        db.close()

        rows = SignalConsensusService(self._db()).get_matrix(["AAPL.US"])
        assert len(rows) == 1
        row = rows[0]
        assert row["consensus"] == "AGREE_BULLISH"
        # 3 concrete bullish votes out of 5 sources (opening momentum + v2
        # shadow abstain as NO_DATA).
        assert row["agreement_score"] == round(3 / 3, 4)

    def test_agreement_score_is_fraction_of_concrete_votes(self) -> None:
        db = self._db()
        # Range engine: BULLISH (price below band)
        db.add(StrategyConfig(symbol="TSLA.US", buy_low=200.0, sell_high=210.0))
        db.add(TradeEvent(
            symbol="TSLA.US", event_type="ORDER_SKIPPED",
            payload_json='{"last_price": 195.0}', created_at=_NOW,
        ))
        # Strategy v2 shadow OPEN → BULLISH
        db.add(StrategyV2ShadowTrade(
            symbol="TSLA.US", status="OPEN", entry_at=_NOW,
            entry_price=196.0, quantity=1.0,
        ))
        # Quant: SELL → BEARISH (the dissenting vote)
        db.add(WatchlistScore(
            symbol="TSLA.US", score=40.0, confidence=0.7,
            recommended_action="SELL", created_at=_NOW, expires_at=_NOW,
        ))
        # Opening momentum + LLM = NO_DATA (abstentions)
        db.commit()
        db.close()

        rows = SignalConsensusService(self._db()).get_matrix(["TSLA.US"])
        row = rows[0]
        # 2 BULLISH vs 1 BEARISH → MIXED (both sides present).
        assert row["consensus"] == "MIXED"
        # agreement = max(2, 1) / 3 = 2/3
        assert row["agreement_score"] == round(2 / 3, 4)

    def test_mixed_when_both_bullish_and_bearish_present(self) -> None:
        db = self._db()
        # Range engine BEARISH (price above sell_high)
        db.add(StrategyConfig(symbol="MSFT.US", buy_low=300.0, sell_high=310.0))
        db.add(TradeEvent(
            symbol="MSFT.US", event_type="ORDER_SKIPPED",
            payload_json='{"last_price": 315.0}', created_at=_NOW,
        ))
        # Quant BULLISH
        db.add(WatchlistScore(
            symbol="MSFT.US", score=90.0, confidence=0.9,
            recommended_action="BUY", created_at=_NOW, expires_at=_NOW,
        ))
        db.commit()
        db.close()

        rows = SignalConsensusService(self._db()).get_matrix(["MSFT.US"])
        assert rows[0]["consensus"] == "MIXED"

    def test_insufficient_when_only_one_concrete_vote(self) -> None:
        db = self._db()
        # Only quant votes; everything else is NO_DATA or NEUTRAL.
        db.add(WatchlistScore(
            symbol="NVDA.US", score=80.0, confidence=0.8,
            recommended_action="BUY", created_at=_NOW, expires_at=_NOW,
        ))
        db.commit()
        db.close()

        rows = SignalConsensusService(self._db()).get_matrix(["NVDA.US"])
        row = rows[0]
        # Only 1 concrete vote → below the _MIN_VOTES_FOR_CONSENSUS threshold.
        assert row["consensus"] == "INSUFFICIENT_DATA"
        assert row["agreement_score"] == 0.0

    def test_agree_bearish_when_majority_sources_bearish(self) -> None:
        db = self._db()
        # Range engine BEARISH
        db.add(StrategyConfig(symbol="GOOG.US", buy_low=100.0, sell_high=110.0))
        db.add(TradeEvent(
            symbol="GOOG.US", event_type="ORDER_SKIPPED",
            payload_json='{"last_price": 120.0}', created_at=_NOW,
        ))
        # Quant AVOID
        db.add(WatchlistScore(
            symbol="GOOG.US", score=20.0, confidence=0.7,
            recommended_action="AVOID", created_at=_NOW, expires_at=_NOW,
        ))
        db.commit()
        db.close()

        rows = SignalConsensusService(self._db()).get_matrix(["GOOG.US"])
        assert rows[0]["consensus"] == "AGREE_BEARISH"

    def test_neutral_inside_band_does_not_count_as_vote(self) -> None:
        db = self._db()
        # Price in the middle of the band → NEUTRAL (not a vote).
        db.add(StrategyConfig(symbol="SPY.US", buy_low=100.0, sell_high=110.0))
        db.add(TradeEvent(
            symbol="SPY.US", event_type="ORDER_SKIPPED",
            payload_json='{"last_price": 105.0}', created_at=_NOW,
        ))
        db.commit()
        db.close()

        rows = SignalConsensusService(self._db()).get_matrix(["SPY.US"])
        row = rows[0]
        assert row["sources"]["range_engine"]["signal"] == "NEUTRAL"
        assert row["consensus"] == "INSUFFICIENT_DATA"


class TestSignalConsensusApi(_Base):
    def test_matrix_with_symbols_query_param(self) -> None:
        db = self._db()
        db.add(WatchlistScore(
            symbol="AMD.US", score=80.0, confidence=0.8,
            recommended_action="BUY", created_at=_NOW, expires_at=_NOW,
        ))
        db.add(WatchlistScore(
            symbol="INTC.US", score=20.0, confidence=0.7,
            recommended_action="SELL", created_at=_NOW, expires_at=_NOW,
        ))
        db.commit()
        db.close()

        resp = self.client.get(
            "/api/signal-consensus/matrix", params={"symbols": "amd.us,intc.us"}
        )
        assert resp.status_code == 200
        rows = resp.json()
        symbols = {row["symbol"] for row in rows}
        assert symbols == {"AMD.US", "INTC.US"}

    def test_summary_counts_match_matrix(self) -> None:
        db = self._db()
        # Two symbols, both insufficient (single vote each).
        db.add(WatchlistScore(
            symbol="A.US", score=80.0, confidence=0.8,
            recommended_action="BUY", created_at=_NOW, expires_at=_NOW,
        ))
        db.add(WatchlistScore(
            symbol="B.US", score=20.0, confidence=0.7,
            recommended_action="SELL", created_at=_NOW, expires_at=_NOW,
        ))
        db.commit()
        db.close()

        resp = self.client.get("/api/signal-consensus/summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_symbols"] == 2
        assert body["insufficient"] == 2
        assert body["agree_bullish"] == 0
        assert body["agree_bearish"] == 0
        assert body["mixed"] == 0

    def test_symbols_param_normalizes_and_dedupes(self) -> None:
        db = self._db()
        db.add(WatchlistScore(
            symbol="X.US", score=50.0, confidence=0.5,
            recommended_action="HOLD", created_at=_NOW, expires_at=_NOW,
        ))
        db.commit()
        db.close()

        resp = self.client.get(
            "/api/signal-consensus/matrix", params={"symbols": "x.us,, X.US, "}
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["symbol"] == "X.US"

    def test_opening_momentum_bullish_when_candidate_entered(self) -> None:
        db = self._db()
        db.add(OpeningMomentumShadowRun(
            session_date=_NOW.date(),
            algorithm_version="v1",
            config_version="v1",
            status="ENTERED",
            reason="",
            signal_at=_NOW,
            observed_at=_NOW,
            universe_size=1,
            candidate_symbol="COKE.US",
            estimated_cost_bps=5.0,
        ))
        db.commit()
        db.close()

        rows = SignalConsensusService(self._db()).get_matrix(["COKE.US"])
        row = rows[0]
        assert row["sources"]["opening_momentum"]["signal"] == "BULLISH"

    def test_strategy_v2_shadow_open_is_bullish(self) -> None:
        db = self._db()
        db.add(StrategyV2ShadowTrade(
            symbol="QQQ.US", status="OPEN", entry_at=_NOW,
            entry_price=400.0, quantity=1.0,
        ))
        db.commit()
        db.close()

        rows = SignalConsensusService(self._db()).get_matrix(["QQQ.US"])
        assert rows[0]["sources"]["strategy_v2_shadow"]["signal"] == "BULLISH"
