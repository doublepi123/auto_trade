from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from multiprocessing import get_context
from pathlib import Path
from typing import Protocol, cast

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.broker import BrokerCandle, Quote
from app.core.holiday_calendar import is_market_closed
from app.domain.universe_selection import (
    DIVERSIFIED_INVERSE_VOLATILITY_VARIANT,
    DIVERSIFIED_SHRINKAGE_ROTATION_VARIANT,
    HISTORICAL_INDEX_CANDIDATE_CATALOG,
    ROTATION_ALGORITHM_VERSION,
    ROTATION_WALK_FORWARD_VERSION,
    IndexCandidate,
    UniverseSelectionConfig,
    risk_group_for_sector,
)
from app.models import (
    Base,
    StrategyConfig,
    StrategyV2ShadowConfig,
    TrackedEntry,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
    WatchlistItem,
)
from app.schemas import StrategyV2ShadowConfigUpdate
from app.services.universe_selection_service import (
    _HISTORICAL_RESEARCH_BARS_CACHE,
    UniverseSelectionLeaseBusyError,
    historical_membership_end,
    historical_research_alias_provenance,
    historical_research_candlesticks,
    historical_research_symbol_alias,
    research_candidate_uses_recent_candlesticks,
    UniverseSelectionService,
    minimum_peer_observation_dollar_volume,
    observation_pool_overrides,
    select_exploration_candidates,
    validated_inverse_volatility_observation_symbols,
    validated_point_in_time_shrinkage_observation_symbols,
)
from app.services.durable_job_lease_service import (
    DurableJobLeaseService,
    LeaseKeepalive,
    LeaseLostError,
)

_NOW = datetime(2026, 7, 24, 18, 0, tzinfo=timezone.utc)
_CATALOG = (
    IndexCandidate(
        "AAPL.US",
        "Apple",
        "Technology Hardware",
        ("NASDAQ_100", "DJIA"),
    ),
    IndexCandidate(
        "JPM.US",
        "JPMorgan Chase",
        "Financials",
        ("DJIA",),
    ),
)


def test_historical_research_bars_use_membership_end_and_cache() -> None:
    candidate = next(
        row
        for row in HISTORICAL_INDEX_CANDIDATE_CATALOG
        if row.symbol == "PTON.US"
    )

    class _HistoricalBroker:
        def __init__(self) -> None:
            self.boundaries: list[datetime] = []

        def get_candlesticks(
            self,
            symbol: str,
            period: str,
            count: int,
        ) -> list[BrokerCandle]:
            raise AssertionError("latest bars must not be used")

        def get_forward_adjusted_history_candlesticks_before(
            self,
            symbol: str,
            period: str,
            count: int,
            before: datetime,
        ) -> list[BrokerCandle]:
            self.boundaries.append(before)
            return [
                BrokerCandle(
                    timestamp=datetime(
                        2022,
                        1,
                        21,
                        tzinfo=timezone.utc,
                    ),
                    open=10,
                    high=11,
                    low=9,
                    close=10.5,
                    volume=1_000_000,
                    turnover=10_000_000,
                )
            ]

    broker = _HistoricalBroker()
    _HISTORICAL_RESEARCH_BARS_CACHE.clear()
    try:
        first = historical_research_candlesticks(
            broker,
            candidate,
            count=1000,
        )
        second = historical_research_candlesticks(
            broker,
            candidate,
            count=1000,
        )
    finally:
        _HISTORICAL_RESEARCH_BARS_CACHE.clear()

    assert historical_membership_end(candidate) == date(2022, 1, 24)
    assert first == second
    assert len(broker.boundaries) == 1
    assert broker.boundaries[0] == datetime(
        2022,
        1,
        24,
        12,
        tzinfo=timezone.utc,
    )


def test_historical_research_empty_response_is_not_cached() -> None:
    candidate = next(
        row
        for row in HISTORICAL_INDEX_CANDIDATE_CATALOG
        if row.symbol == "PTON.US"
    )

    class _TransientEmptyBroker:
        def __init__(self) -> None:
            self.calls = 0

        def get_candlesticks(
            self,
            symbol: str,
            period: str,
            count: int,
        ) -> list[BrokerCandle]:
            raise AssertionError("latest bars must not be used")

        def get_forward_adjusted_history_candlesticks_before(
            self,
            symbol: str,
            period: str,
            count: int,
            before: datetime,
        ) -> list[BrokerCandle]:
            self.calls += 1
            if self.calls == 1:
                return []
            return [
                BrokerCandle(
                    timestamp=datetime(
                        2022,
                        1,
                        21,
                        tzinfo=timezone.utc,
                    ),
                    open=10,
                    high=11,
                    low=9,
                    close=10.5,
                    volume=1_000_000,
                    turnover=10_000_000,
                )
            ]

    broker = _TransientEmptyBroker()
    _HISTORICAL_RESEARCH_BARS_CACHE.clear()
    try:
        first = historical_research_candlesticks(
            broker,
            candidate,
            count=1000,
        )
        second = historical_research_candlesticks(
            broker,
            candidate,
            count=1000,
        )
    finally:
        _HISTORICAL_RESEARCH_BARS_CACHE.clear()

    assert first == []
    assert len(second or []) == 1
    assert broker.calls == 2


def test_fb_historical_research_uses_audited_meta_ticker_alias() -> None:
    candidate = next(
        row
        for row in HISTORICAL_INDEX_CANDIDATE_CATALOG
        if row.symbol == "FB.US"
    )

    class _HistoricalBroker:
        def __init__(self) -> None:
            self.requests: list[
                tuple[str, str, int, datetime]
            ] = []

        def get_candlesticks(
            self,
            symbol: str,
            period: str,
            count: int,
        ) -> list[BrokerCandle]:
            raise AssertionError("latest bars must not be used")

        def get_forward_adjusted_history_candlesticks_before(
            self,
            symbol: str,
            period: str,
            count: int,
            before: datetime,
        ) -> list[BrokerCandle]:
            self.requests.append((symbol, period, count, before))
            return [
                BrokerCandle(
                    timestamp=datetime(
                        2022,
                        6,
                        8,
                        tzinfo=timezone.utc,
                    ),
                    open=200,
                    high=205,
                    low=198,
                    close=204,
                    volume=1_000_000,
                    turnover=204_000_000,
                ),
                # Defensive contract: even if an inclusive provider leaks the
                # first META bar, it must never be cached under FB.
                BrokerCandle(
                    timestamp=datetime(
                        2022,
                        6,
                        9,
                        4,
                        tzinfo=timezone.utc,
                    ),
                    open=205,
                    high=206,
                    low=203,
                    close=204,
                    volume=1_000_000,
                    turnover=204_000_000,
                ),
            ]

    broker = _HistoricalBroker()
    _HISTORICAL_RESEARCH_BARS_CACHE.clear()
    try:
        first = historical_research_candlesticks(
            broker,
            candidate,
            count=1000,
        )
        second = historical_research_candlesticks(
            broker,
            candidate,
            count=1000,
        )
        cache_keys = tuple(_HISTORICAL_RESEARCH_BARS_CACHE)
    finally:
        _HISTORICAL_RESEARCH_BARS_CACHE.clear()

    alias = historical_research_symbol_alias(candidate)
    provenance = historical_research_alias_provenance(candidate)
    assert alias is not None
    assert alias.logical_symbol == "FB.US"
    assert alias.provider_symbol == "META.US"
    assert alias.ticker_change_effective_date == date(2022, 6, 9)
    assert alias.alias_version == "same-security-ticker-alias-v1"
    assert alias.adjustment == "ForwardAdjust"
    assert alias.data_provider == "LONGPORT"
    assert "same Meta Platforms security" in alias.provenance
    assert first == second
    assert first is not None
    assert [bar.timestamp.date() for bar in first] == [
        date(2022, 6, 8)
    ]
    assert provenance is not None
    assert provenance["logical_membership_end_exclusive"] == (
        "2022-06-09"
    )
    assert provenance["fetch_before"] == (
        "2022-06-09T00:00:00+00:00"
    )
    assert broker.requests == [
        (
            "META.US",
            "DAY",
            1000,
            datetime(2022, 6, 9, 0, tzinfo=timezone.utc),
        )
    ]
    assert cache_keys == (
        (
            "FB.US",
            "META.US",
            "ForwardAdjust",
            "same-security-ticker-alias-v1",
            1000,
            date(2022, 6, 9),
        ),
    )


def test_historical_research_does_not_alias_acquired_companies() -> None:
    by_symbol = {
        row.symbol: row
        for row in HISTORICAL_INDEX_CANDIDATE_CATALOG
    }

    assert historical_research_symbol_alias(by_symbol["ATVI.US"]) is None
    assert historical_research_symbol_alias(by_symbol["SGEN.US"]) is None
    assert historical_research_symbol_alias(by_symbol["SPLK.US"]) is None
    assert historical_research_symbol_alias(by_symbol["XLNX.US"]) is None


def test_universe_parameters_persist_historical_alias_provenance() -> None:
    current = IndexCandidate(
        "META.US",
        "Meta Platforms",
        "Communication Services",
        ("NASDAQ_100",),
    )
    historical = next(
        row
        for row in HISTORICAL_INDEX_CANDIDATE_CATALOG
        if row.symbol == "FB.US"
    )
    db = _db()
    try:
        service = UniverseSelectionService(
            db,
            _FakeBroker(),
            catalog=(current,),
            rotation_research_catalog=(current, historical),
            config=_config(),
            minimum_evaluable_ratio=0.5,
            minimum_residency_days=1,
            apply_to_watchlist=False,
            enable_shadow=False,
            now=_NOW,
        )

        aliases = service._parameters()[
            "rotation_historical_symbol_aliases"
        ]
    finally:
        db.close()

    assert isinstance(aliases, list)
    assert len(aliases) == 1
    assert aliases[0]["logical_symbol"] == "FB.US"
    assert aliases[0]["provider_symbol"] == "META.US"
    assert aliases[0]["alias_version"] == (
        "same-security-ticker-alias-v1"
    )
    assert aliases[0]["fetch_before"] == (
        "2022-06-09T00:00:00+00:00"
    )


def test_recent_research_fallback_requires_active_snapshot_membership() -> None:
    current_research_only = next(
        row
        for row in HISTORICAL_INDEX_CANDIDATE_CATALOG
        if row.symbol == "GOOG.US"
    )
    former = next(
        row
        for row in HISTORICAL_INDEX_CANDIDATE_CATALOG
        if row.symbol == "PTON.US"
    )
    missing = IndexCandidate(
        "MISSING.US",
        "Missing",
        "Software",
        ("NASDAQ_100",),
    )

    assert research_candidate_uses_recent_candlesticks(
        current_research_only
    ) is True
    assert research_candidate_uses_recent_candlesticks(former) is False
    assert research_candidate_uses_recent_candlesticks(missing) is False


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _config() -> UniverseSelectionConfig:
    return UniverseSelectionConfig(
        max_selected=2,
        max_per_sector=1,
        min_avg_dollar_volume=100_000_000,
        max_relative_spread_bps=20,
        min_realized_vol_20d=0.01,
        max_realized_vol_20d=3.0,
        min_atr_pct_14d=0.1,
        max_atr_pct_14d=20.0,
    )


def _validated_rotation_parameters(
    targets: tuple[tuple[str, float], ...],
    *,
    point_in_time_shrinkage: bool = False,
) -> str:
    variant = (
        DIVERSIFIED_SHRINKAGE_ROTATION_VARIANT
        if point_in_time_shrinkage
        else DIVERSIFIED_INVERSE_VOLATILITY_VARIANT
    )
    assert variant.max_position_weight_pct is not None
    target_weight_pct = 100.0 / len(targets)
    assert target_weight_pct <= variant.max_position_weight_pct
    evaluation = {
        "algorithm_version": ROTATION_WALK_FORWARD_VERSION,
        "status": "COMPLETE",
        "validated_challenger_variant": variant.name,
        "variants": [{
            "variant": {"name": variant.name},
            "validation_passed": True,
            "expanding_validation_passed": True,
        }],
    }
    registration = {
        "cohort_month": "2026-07-01",
        "rotation_algorithm_version": ROTATION_ALGORITHM_VERSION,
        "variant_name": variant.name,
        "signal_date": "2026-06-30",
        "registered_as_of_date": "2026-07-23",
        "forward_eligible": False,
        "target_signals": [
            {
                "symbol": symbol,
                "rank": rank,
                "risk_group": "Test",
                "momentum_pct": score,
                "sma_price": 100.0,
                "above_sma": True,
                "score": score,
                "signal_spread_bps": 1.0,
                "target_weight_pct": target_weight_pct,
            }
            for rank, (symbol, score) in enumerate(
                targets,
                start=1,
            )
        ],
    }
    if point_in_time_shrinkage:
        evaluation["data_scope"] = "POINT_IN_TIME_RESEARCH_CATALOG"
        return json.dumps({
            "rotation_point_in_time_sensitivity": {
                "status": "COMPLETE",
                "membership_history": {
                    "authoritative_ratio": 0.98,
                    "source_version": "pit-membership-v1",
                },
                "evaluation": evaluation,
            },
            "rotation_shrinkage_challenger_registration": registration,
        })
    return json.dumps({
        "rotation_evaluation": evaluation,
        "rotation_weighting_challenger_registration": registration,
    })


def _daily_bars(symbol: str) -> list[BrokerCandle]:
    bars: list[BrokerCandle] = []
    price = 100.0 if symbol == "AAPL.US" else 200.0
    start = datetime(2026, 6, 24, 4, tzinfo=timezone.utc)
    for index in range(30):
        move = 0.012 if index % 2 == 0 else -0.008
        close = price * (1 + move)
        bars.append(
            BrokerCandle(
                timestamp=start + timedelta(days=index),
                open=price,
                high=max(price, close) * 1.01,
                low=min(price, close) * 0.99,
                close=close,
                volume=20_000_000,
            )
        )
        price = close
    return bars


class _FakeBroker:
    def __init__(self, *, failing: bool = False) -> None:
        self.failing = failing
        self.quote_calls = 0
        self.candle_calls = 0

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        self.quote_calls += 1
        if self.failing:
            raise RuntimeError("quotes unavailable")
        return [
            Quote(
                symbol=symbol,
                last_price=100.0,
                bid=99.99,
                ask=100.01,
                timestamp=datetime(
                    2026,
                    7,
                    23,
                    20,
                    tzinfo=timezone.utc,
                ).isoformat(),
            )
            for symbol in symbols
        ]

    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[BrokerCandle]:
        assert period == "DAY"
        self.candle_calls += 1
        if self.failing:
            raise RuntimeError("daily bars unavailable")
        return _daily_bars(symbol)[-count:]


class _ForwardAdjustedBroker(_FakeBroker):
    def __init__(self) -> None:
        super().__init__()
        self.adjusted_candle_calls = 0

    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[BrokerCandle]:
        raise AssertionError(
            f"raw candles must not feed universe selection: "
            f"{symbol} {period} {count}"
        )

    def get_forward_adjusted_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[BrokerCandle]:
        assert period == "DAY"
        self.adjusted_candle_calls += 1
        return _daily_bars(symbol)[-count:]


class _LongHistoryBroker(_ForwardAdjustedBroker):
    def __init__(
        self,
        *,
        end_date: datetime | None = None,
    ) -> None:
        super().__init__()
        self.requested_counts: list[int] = []
        self.end_date = end_date or datetime(
            2026,
            7,
            23,
            20,
            tzinfo=timezone.utc,
        )

    def get_forward_adjusted_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[BrokerCandle]:
        assert period == "DAY"
        self.adjusted_candle_calls += 1
        self.requested_counts.append(count)
        price = 100.0 if symbol == "AAPL.US" else 200.0
        drift = 0.0025 if symbol == "AAPL.US" else 0.0015
        timestamps: list[datetime] = []
        cursor = self.end_date.date()
        while len(timestamps) < 330:
            if (
                cursor.weekday() < 5
                and not is_market_closed("US", cursor)
            ):
                timestamps.append(
                    datetime(
                        cursor.year,
                        cursor.month,
                        cursor.day,
                        20,
                        tzinfo=timezone.utc,
                    )
                )
            cursor -= timedelta(days=1)
        timestamps.reverse()
        result: list[BrokerCandle] = []
        for index, timestamp in enumerate(timestamps):
            move = drift + (0.012 if index % 2 == 0 else -0.012)
            close = price * (1 + move)
            result.append(
                BrokerCandle(
                    timestamp=timestamp,
                    open=price,
                    high=max(price, close) * 1.01,
                    low=min(price, close) * 0.99,
                    close=close,
                    volume=20_000_000,
                )
            )
            price = close
        return result[-count:]


class _EventLike(Protocol):
    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...

    def is_set(self) -> bool: ...


class _QueueLike(Protocol):
    def put(self, item: object) -> None: ...


class _CoordinatedBroker(_FakeBroker):
    def __init__(
        self,
        *,
        failing: bool,
        evaluation_started: _EventLike,
        release_evaluation: _EventLike,
    ) -> None:
        super().__init__(failing=failing)
        self.evaluation_started = evaluation_started
        self.release_evaluation = release_evaluation
        self._announced = False

    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[BrokerCandle]:
        if not self._announced:
            self._announced = True
            self.evaluation_started.set()
            if not self.release_evaluation.wait(timeout=15):
                raise TimeoutError("test did not release catalog evaluation")
        return super().get_candlesticks(symbol, period, count)


def _concurrent_refresh_worker(
    database_path: str,
    *,
    failing: bool,
    worker_started: _EventLike,
    evaluation_started: _EventLike,
    release_evaluation: _EventLike,
    result_queue: _QueueLike,
) -> None:
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"timeout": 15},
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA busy_timeout=15000")
    db = sessionmaker(bind=engine)()
    broker = _CoordinatedBroker(
        failing=failing,
        evaluation_started=evaluation_started,
        release_evaluation=release_evaluation,
    )
    try:
        worker_started.set()
        result = UniverseSelectionService(
            db,
            broker,
            catalog=_CATALOG,
            config=_config(),
            minimum_evaluable_ratio=0.5,
            minimum_residency_days=1,
            apply_to_watchlist=False,
            enable_shadow=False,
            now=_NOW,
        ).refresh(apply_to_watchlist=False)
        result_queue.put(
            {
                "run_id": result.run.id,
                "status": result.run.status,
                "selected_count": result.run.selected_count,
                "selected_symbols": sorted(
                    item.symbol for item in result.items if item.selected
                ),
                "item_count": len(result.items),
            }
        )
    except Exception as exc:
        result_queue.put({"error": repr(exc)})
    finally:
        db.close()
        engine.dispose()


def _service(
    db: Session,
    broker: _FakeBroker,
    *,
    enable_shadow: bool = False,
) -> UniverseSelectionService:
    return UniverseSelectionService(
        db,
        broker,
        catalog=_CATALOG,
        config=_config(),
        minimum_evaluable_ratio=0.5,
        minimum_residency_days=1,
        apply_to_watchlist=True,
        enable_shadow=enable_shadow,
        now=_NOW,
    )


def test_exploration_candidates_are_diverse_hard_gate_passers() -> None:
    def candidate(
        symbol: str,
        sector: str,
        score: float,
        reasons: list[str],
    ) -> UniverseSelectionCandidate:
        return UniverseSelectionCandidate(
            run_id=1,
            symbol=symbol,
            market="US",
            alias=symbol,
            sector=sector,
            memberships_json='["NASDAQ_100"]',
            selected=False,
            score=score,
            metrics_json="{}",
            exclusion_reasons_json=json.dumps(reasons),
            created_at=_NOW,
        )

    items = [
        candidate("INTC.US", "Semiconductors", 90, ["SECTOR_CAP"]),
        candidate("AMAT.US", "Semiconductors", 85, ["SECTOR_CAP"]),
        candidate(
            "GOOGL.US",
            "Communication Services",
            80,
            ["BELOW_SELECTION_CUTOFF"],
        ),
        candidate("ARM.US", "Semiconductors", 79, ["ATR_OUTSIDE_RANGE"]),
        candidate(
            "UNH.US",
            "Healthcare",
            75,
            ["BELOW_SELECTION_CUTOFF"],
        ),
    ]

    selected = select_exploration_candidates(
        items,
        max_symbols=3,
        max_per_sector=1,
    )

    assert [item.symbol for item in selected] == [
        "INTC.US",
        "GOOGL.US",
        "UNH.US",
    ]


def test_exploration_uses_idle_capacity_for_top_score_challengers() -> None:
    def candidate(
        symbol: str,
        sector: str,
        score: float,
        *,
        selected: bool = False,
    ) -> UniverseSelectionCandidate:
        return UniverseSelectionCandidate(
            run_id=1,
            symbol=symbol,
            market="US",
            alias=symbol,
            sector=sector,
            memberships_json='["NASDAQ_100"]',
            selected=selected,
            score=score,
            metrics_json="{}",
            exclusion_reasons_json=json.dumps(
                [] if selected else ["SECTOR_CAP"]
            ),
            created_at=_NOW,
        )

    items = [
        candidate("AMD.US", "Semiconductors", 99, selected=True),
        candidate("AMAT.US", "Semiconductors", 98, selected=True),
        candidate("NVDA.US", "Semiconductors", 90),
        candidate("AVGO.US", "Semiconductors", 85),
        candidate("LRCX.US", "Semiconductors", 80),
        candidate("GOOGL.US", "Communication Services", 75),
    ]

    baseline = select_exploration_candidates(
        items,
        max_symbols=4,
        max_per_sector=2,
    )
    challenged = select_exploration_candidates(
        items,
        max_symbols=4,
        max_per_sector=2,
        top_score_challengers=2,
    )

    assert [item.symbol for item in baseline] == [
        "NVDA.US",
        "GOOGL.US",
    ]
    assert [item.symbol for item in challenged] == [
        "NVDA.US",
        "AVGO.US",
        "LRCX.US",
        "GOOGL.US",
    ]
    assert {item.symbol for item in baseline}.issubset(
        {item.symbol for item in challenged}
    )


def test_exploration_reserves_fresh_challengers_before_durable_observers(
) -> None:
    def candidate(
        symbol: str,
        sector: str,
        score: float,
        *,
        selected: bool = False,
    ) -> UniverseSelectionCandidate:
        return UniverseSelectionCandidate(
            run_id=1,
            symbol=symbol,
            market="US",
            alias=symbol,
            sector=sector,
            memberships_json='["NASDAQ_100"]',
            selected=selected,
            score=score,
            metrics_json="{}",
            exclusion_reasons_json=json.dumps(
                [] if selected else ["SECTOR_CAP"]
            ),
            created_at=_NOW,
        )

    selected = select_exploration_candidates(
        [
            candidate("BASE.US", "Financials", 100, selected=True),
            candidate("FRESH1.US", "Software", 99),
            candidate("FRESH2.US", "Healthcare", 98),
            candidate("OLD1.US", "Energy", 97),
            candidate("OLD2.US", "Utilities", 96),
        ],
        max_symbols=2,
        max_per_sector=1,
        top_score_challengers=2,
        challenger_excluded_symbols={"OLD1.US", "OLD2.US"},
    )

    assert [item.symbol for item in selected] == [
        "FRESH1.US",
        "FRESH2.US",
    ]


def test_exploration_candidates_reserve_frozen_rotation_observers() -> None:
    def candidate(
        symbol: str,
        score: float,
        *,
        rotation_rank: int | None = None,
    ) -> UniverseSelectionCandidate:
        rotation = (
            {}
            if rotation_rank is None
            else {
                "algorithm_version": ROTATION_ALGORITHM_VERSION,
                "selected": True,
                "rank": rotation_rank,
                "score": 100 - rotation_rank,
            }
        )
        return UniverseSelectionCandidate(
            run_id=1,
            symbol=symbol,
            market="US",
            alias=symbol,
            sector="Consumer Discretionary",
            memberships_json='["NASDAQ_100"]',
            selected=False,
            score=score,
            metrics_json=json.dumps({"rotation": rotation}),
            exclusion_reasons_json='["SECTOR_CAP"]',
            created_at=_NOW,
        )

    selected = select_exploration_candidates(
        [
            candidate("HIGH.US", 99),
            candidate("ROST.US", 20, rotation_rank=2),
            candidate("MRK.US", 10, rotation_rank=1),
        ],
        max_symbols=2,
        max_per_sector=1,
    )

    assert [item.symbol for item in selected] == [
        "MRK.US",
        "ROST.US",
    ]


def test_exploration_candidates_prioritize_selected_risk_group_peers() -> None:
    def candidate(
        symbol: str,
        sector: str,
        score: float,
        *,
        selected: bool = False,
    ) -> UniverseSelectionCandidate:
        return UniverseSelectionCandidate(
            run_id=1,
            symbol=symbol,
            market="US",
            alias=symbol,
            sector=sector,
            memberships_json='["NASDAQ_100"]',
            selected=selected,
            score=score,
            metrics_json="{}",
            exclusion_reasons_json=json.dumps(
                [] if selected else ["SECTOR_CAP"]
            ),
            created_at=_NOW,
        )

    items = [
        candidate(
            "AMD.US",
            "Semiconductors",
            95,
            selected=True,
        ),
        candidate("MSFT.US", "Software", 92),
        candidate("INTC.US", "Semiconductors", 90),
        candidate("GOOGL.US", "Communication Services", 80),
    ]

    selected = select_exploration_candidates(
        items,
        max_symbols=2,
        max_per_sector=1,
    )

    assert [item.symbol for item in selected] == [
        "MSFT.US",
        "INTC.US",
    ]


def test_exploration_candidates_reuse_durable_observers() -> None:
    def candidate(
        symbol: str,
        sector: str,
        score: float,
        *,
        selected: bool = False,
    ) -> UniverseSelectionCandidate:
        return UniverseSelectionCandidate(
            run_id=1,
            symbol=symbol,
            market="US",
            alias=symbol,
            sector=sector,
            memberships_json='["NASDAQ_100"]',
            selected=selected,
            score=score,
            metrics_json="{}",
            exclusion_reasons_json=json.dumps(
                [] if selected else ["SECTOR_CAP"]
            ),
            created_at=_NOW,
        )

    items = [
        candidate("AMD.US", "Semiconductors", 95, selected=True),
        candidate("MSFT.US", "Software", 92),
        candidate("INTC.US", "Semiconductors", 90),
        candidate("GOOGL.US", "Communication Services", 80),
    ]

    selected = select_exploration_candidates(
        items,
        max_symbols=3,
        max_per_sector=1,
        already_observed_symbols={"MSFT.US"},
    )

    assert [item.symbol for item in selected] == [
        "INTC.US",
        "MSFT.US",
        "GOOGL.US",
    ]


def test_observation_pool_overrides_separate_durable_and_opt_out() -> None:
    db = _db()
    try:
        db.add(StrategyConfig(symbol="NVDA.US", market="US"))
        db.add_all(
            [
                StrategyV2ShadowConfig(
                    symbol="NVDA.US",
                    enabled=True,
                    universe_managed=False,
                    opening_momentum_execution_eligible=False,
                ),
                StrategyV2ShadowConfig(
                    symbol="MRVL.US",
                    enabled=True,
                    universe_managed=False,
                ),
                StrategyV2ShadowConfig(
                    symbol="CRWD.US",
                    enabled=True,
                    universe_managed=False,
                    opening_momentum_execution_eligible=False,
                ),
                StrategyV2ShadowConfig(
                    symbol="TER.US",
                    enabled=False,
                    universe_managed=False,
                ),
                StrategyV2ShadowConfig(
                    symbol="AAPL.US",
                    enabled=True,
                    universe_managed=True,
                ),
            ]
        )
        db.commit()

        overrides = observation_pool_overrides(db)

        assert overrides.already_observed_symbols == frozenset(
            {"NVDA.US", "MRVL.US", "AAPL.US"}
        )
        assert overrides.durable_observed_symbols == frozenset(
            {"NVDA.US", "MRVL.US", "CRWD.US", "AAPL.US"}
        )
        assert overrides.challenger_excluded_symbols == frozenset(
            {"MRVL.US", "AAPL.US"}
        )
        assert overrides.exploration_excluded_symbols == frozenset(
            {"CRWD.US", "TER.US"}
        )
        assert overrides.unobservable_symbols == frozenset({"TER.US"})
    finally:
        db.close()


def test_observation_only_symbol_does_not_spend_exploration_budget() -> None:
    db = _db()
    try:
        run = UniverseSelectionRun(
            as_of_date=_NOW.date(),
            algorithm_version="selector-v1",
            source_version="catalog-v1",
            status="COMPLETE",
            candidate_count=3,
            evaluable_count=3,
            selected_count=1,
            coverage_ratio=1.0,
            parameters_json="{}",
            started_at=_NOW - timedelta(hours=1),
            completed_at=_NOW,
            created_at=_NOW - timedelta(hours=1),
        )
        db.add(run)
        db.flush()
        db.add_all(
            [
                UniverseSelectionCandidate(
                    run_id=run.id,
                    symbol="AMD.US",
                    market="US",
                    alias="AMD",
                    sector="Semiconductors",
                    selected=True,
                    rank=1,
                    score=95.0,
                    metrics_json="{}",
                    exclusion_reasons_json="[]",
                    created_at=_NOW,
                ),
                UniverseSelectionCandidate(
                    run_id=run.id,
                    symbol="CRWD.US",
                    market="US",
                    alias="CrowdStrike",
                    sector="Software",
                    selected=False,
                    score=94.0,
                    metrics_json="{}",
                    exclusion_reasons_json='["SECTOR_CAP"]',
                    created_at=_NOW,
                ),
                UniverseSelectionCandidate(
                    run_id=run.id,
                    symbol="MSTR.US",
                    market="US",
                    alias="Strategy",
                    sector="Software",
                    selected=False,
                    score=90.0,
                    metrics_json="{}",
                    exclusion_reasons_json='["SECTOR_CAP"]',
                    created_at=_NOW,
                ),
                StrategyV2ShadowConfig(
                    symbol="CRWD.US",
                    enabled=True,
                    universe_managed=False,
                    opening_momentum_execution_eligible=False,
                ),
            ]
        )
        db.commit()

        service = UniverseSelectionService(
            db,
            _FakeBroker(),
            catalog=_CATALOG,
            config=_config(),
            minimum_evaluable_ratio=0.5,
            minimum_residency_days=1,
            exploration_max_symbols=1,
            exploration_top_score_challengers=0,
            apply_to_watchlist=True,
            enable_shadow=True,
            now=_NOW,
        )
        result = service._result_for_existing(
            run,
            service.items_for_run(run.id),
            should_apply=True,
        )

        assert result.exploration_symbols == ("MSTR.US",)
        assert {
            row.symbol for row in db.query(WatchlistItem).all()
        } == {"AMD.US", "CRWD.US", "MSTR.US"}
        crwd = (
            db.query(StrategyV2ShadowConfig)
            .filter(StrategyV2ShadowConfig.symbol == "CRWD.US")
            .one()
        )
        assert crwd.enabled is True
        assert crwd.universe_managed is False
        assert crwd.opening_momentum_execution_eligible is False
    finally:
        db.close()


def test_exploration_candidates_fill_every_selected_risk_group() -> None:
    def candidate(
        symbol: str,
        sector: str,
        score: float,
        *,
        selected: bool = False,
    ) -> UniverseSelectionCandidate:
        return UniverseSelectionCandidate(
            run_id=1,
            symbol=symbol,
            market="US",
            alias=symbol,
            sector=sector,
            memberships_json='["NASDAQ_100"]',
            selected=selected,
            score=score,
            metrics_json="{}",
            exclusion_reasons_json=json.dumps(
                [] if selected else ["BELOW_SELECTION_CUTOFF"]
            ),
            created_at=_NOW,
        )

    items = [
        candidate("AMD.US", "Semiconductors", 99, selected=True),
        candidate("AMAT.US", "Semiconductors", 98, selected=True),
        candidate("JPM.US", "Financials", 97, selected=True),
        candidate("GS.US", "Financials", 96, selected=True),
        candidate("CVX.US", "Energy", 95, selected=True),
        candidate("CEG.US", "Utilities", 94, selected=True),
        candidate("V.US", "Financials", 93),
        candidate("BKR.US", "Energy", 92),
        candidate("XOM.US", "Energy", 91),
        candidate("AEP.US", "Utilities", 90),
        candidate("XEL.US", "Utilities", 89),
        candidate("LIN.US", "Materials", 88),
        candidate("NVDA.US", "Technology Hardware", 87),
    ]

    exploration = select_exploration_candidates(
        items,
        max_symbols=6,
        max_per_sector=2,
        already_observed_symbols={"NVDA.US"},
    )

    observed = {
        item.symbol
        for item in items
        if item.selected
    } | {"NVDA.US"} | {item.symbol for item in exploration}
    sector_by_symbol = {item.symbol: item.sector for item in items}
    counts = Counter(
        risk_group_for_sector(sector_by_symbol[symbol])
        for symbol in observed
    )
    selected_groups = {
        risk_group_for_sector(item.sector)
        for item in items
        if item.selected
    }
    assert all(counts[group] >= 3 for group in selected_groups)
    assert "LIN.US" not in {item.symbol for item in exploration}


def test_exploration_candidates_complete_refined_sector_peers_atomically() -> None:
    def candidate(
        symbol: str,
        sector: str,
        score: float,
        *,
        selected: bool = False,
        reasons: list[str] | None = None,
    ) -> UniverseSelectionCandidate:
        return UniverseSelectionCandidate(
            run_id=1,
            symbol=symbol,
            market="US",
            alias=symbol,
            sector=sector,
            memberships_json='["NASDAQ_100"]',
            selected=selected,
            score=score,
            metrics_json="{}",
            exclusion_reasons_json=json.dumps(
                []
                if selected
                else reasons or ["SECTOR_CAP"]
            ),
            created_at=_NOW,
        )

    items = [
        candidate("AMD.US", "Semiconductors", 99, selected=True),
        candidate("AMAT.US", "Semiconductors", 98, selected=True),
        candidate("NVDA.US", "Semiconductors", 97),
        candidate("PLTR.US", "Software", 96),
        candidate("MSFT.US", "Software", 95),
        candidate("PANW.US", "Software", 92),
        candidate("IBM.US", "Technology Hardware", 94),
        candidate("AAPL.US", "Technology Hardware", 93),
        candidate("CSCO.US", "Technology Hardware", 91),
        candidate("LIN.US", "Materials", 89),
        candidate("SHW.US", "Materials", 88),
        candidate(
            "CRWD.US",
            "Software",
            90,
            reasons=["ATR_OUTSIDE_RANGE"],
        ),
    ]

    full = select_exploration_candidates(
        items,
        max_symbols=7,
        max_per_sector=2,
    )
    tight = select_exploration_candidates(
        items,
        max_symbols=6,
        max_per_sector=2,
    )

    assert [item.symbol for item in full] == [
        "NVDA.US",
        "PLTR.US",
        "MSFT.US",
        "PANW.US",
        "IBM.US",
        "AAPL.US",
        "CSCO.US",
    ]
    full_sector_counts = Counter(item.sector for item in full)
    assert full_sector_counts == {
        "Semiconductors": 1,
        "Software": 3,
        "Technology Hardware": 3,
    }
    assert [item.symbol for item in tight] == [
        "NVDA.US",
        "PLTR.US",
        "MSFT.US",
        "PANW.US",
        "LIN.US",
        "SHW.US",
    ]
    assert len(full) == 7
    assert len(tight) == 6
    assert all(
        item.sector != "Technology Hardware"
        for item in tight
    )
    assert all(item.symbol != "CRWD.US" for item in full)


def test_exploration_peer_fallback_is_observation_only_and_narrow() -> None:
    def candidate(
        symbol: str,
        sector: str,
        score: float,
        reasons: list[str],
        avg_dollar_volume: float,
        *,
        selected: bool = False,
    ) -> UniverseSelectionCandidate:
        return UniverseSelectionCandidate(
            run_id=1,
            symbol=symbol,
            market="US",
            alias=symbol,
            sector=sector,
            memberships_json='["NASDAQ_100"]',
            selected=selected,
            score=score,
            metrics_json=json.dumps(
                {"avg_dollar_volume": avg_dollar_volume}
            ),
            exclusion_reasons_json=json.dumps(
                [] if selected else reasons
            ),
            created_at=_NOW,
        )

    items = [
        candidate(
            "CVX.US",
            "Energy",
            95,
            [],
            1_500_000_000,
            selected=True,
        ),
        candidate(
            "CEG.US",
            "Utilities",
            94,
            [],
            800_000_000,
            selected=True,
        ),
        candidate(
            "BKR.US",
            "Energy",
            90,
            ["BELOW_SELECTION_CUTOFF"],
            535_000_000,
        ),
        candidate(
            "AEP.US",
            "Utilities",
            89,
            ["BELOW_SELECTION_CUTOFF"],
            640_000_000,
        ),
        candidate(
            "EXC.US",
            "Utilities",
            0,
            ["DOLLAR_VOLUME_BELOW_MINIMUM"],
            435_000_000,
        ),
        candidate(
            "XEL.US",
            "Utilities",
            0,
            ["DOLLAR_VOLUME_BELOW_MINIMUM"],
            408_000_000,
        ),
        candidate(
            "FANG.US",
            "Energy",
            0,
            ["DOLLAR_VOLUME_BELOW_MINIMUM"],
            400_000_000,
        ),
        candidate(
            "LOW.US",
            "Energy",
            0,
            ["DOLLAR_VOLUME_BELOW_MINIMUM"],
            300_000_000,
        ),
        candidate(
            "WIDE.US",
            "Energy",
            0,
            ["SPREAD_ABOVE_MAXIMUM"],
            900_000_000,
        ),
    ]

    exploration = select_exploration_candidates(
        items,
        max_symbols=4,
        max_per_sector=2,
        minimum_peer_dollar_volume=(
            minimum_peer_observation_dollar_volume(500_000_000)
        ),
    )

    assert [item.symbol for item in exploration] == [
        "BKR.US",
        "AEP.US",
        "EXC.US",
        "FANG.US",
    ]


def test_refresh_reconciles_exploration_into_read_only_evidence() -> None:
    catalog = (
        IndexCandidate(
            "AAPL.US",
            "Apple",
            "Technology Hardware",
            ("NASDAQ_100", "DJIA"),
        ),
        IndexCandidate(
            "JPM.US",
            "JPMorgan Chase",
            "Financials",
            ("DJIA",),
        ),
        IndexCandidate(
            "MSFT.US",
            "Microsoft",
            "Software",
            ("NASDAQ_100", "DJIA"),
        ),
    )
    db = _db()
    try:
        result = UniverseSelectionService(
            db,
            _FakeBroker(),
            catalog=catalog,
            config=UniverseSelectionConfig(
                max_selected=1,
                max_per_sector=1,
                min_avg_dollar_volume=100_000_000,
                max_relative_spread_bps=20,
                min_realized_vol_20d=0.01,
                max_realized_vol_20d=3.0,
                min_atr_pct_14d=0.1,
                max_atr_pct_14d=20.0,
            ),
            minimum_evaluable_ratio=0.5,
            minimum_residency_days=1,
            exploration_max_symbols=2,
            apply_to_watchlist=True,
            enable_shadow=True,
            now=_NOW,
        ).refresh()

        selected_symbols = {
            item.symbol for item in result.items if item.selected
        }
        assert len(selected_symbols) == 1
        assert result.exploration_symbols == ("AAPL.US", "JPM.US")
        assert selected_symbols.isdisjoint(result.exploration_symbols)
        assert {
            row.symbol for row in db.query(WatchlistItem).all()
        } == selected_symbols | set(result.exploration_symbols)
        assert {
            row.symbol
            for row in db.query(StrategyV2ShadowConfig)
            .filter(StrategyV2ShadowConfig.enabled.is_(True))
            .all()
        } == selected_symbols | set(result.exploration_symbols)
        assert all(
            row.opening_momentum_execution_eligible is False
            for row in db.query(StrategyV2ShadowConfig).all()
        )
        assert all(
            row.is_active is False
            for row in db.query(WatchlistItem).all()
        )
    finally:
        db.close()


def test_reconcile_keeps_execution_pool_and_challengers_stable() -> None:
    db = _db()
    try:
        run = UniverseSelectionRun(
            as_of_date=_NOW.date(),
            algorithm_version="selector-v1",
            source_version="catalog-v1",
            status="COMPLETE",
            candidate_count=6,
            evaluable_count=6,
            selected_count=1,
            coverage_ratio=1.0,
            parameters_json="{}",
            started_at=_NOW - timedelta(minutes=2),
            completed_at=_NOW - timedelta(minutes=1),
            created_at=_NOW - timedelta(minutes=2),
        )
        db.add(run)
        db.flush()

        def candidate(
            symbol: str,
            score: float,
            *,
            selected: bool = False,
        ) -> UniverseSelectionCandidate:
            return UniverseSelectionCandidate(
                run_id=run.id,
                symbol=symbol,
                market="US",
                alias=symbol,
                sector=(
                    "Financials" if selected else "Semiconductors"
                ),
                memberships_json='["NASDAQ_100"]',
                selected=selected,
                rank=1 if selected else None,
                score=score,
                metrics_json="{}",
                exclusion_reasons_json=json.dumps(
                    [] if selected else ["SECTOR_CAP"]
                ),
                created_at=_NOW,
            )

        db.add_all(
            [
                candidate("AAPL.US", 100, selected=True),
                candidate("ASML.US", 99),
                candidate("KLAC.US", 98),
                candidate("AVGO.US", 97),
                candidate("LRCX.US", 96),
                candidate("APP.US", 95),
                StrategyV2ShadowConfig(
                    symbol="AVGO.US",
                    enabled=True,
                    universe_managed=True,
                    opening_momentum_execution_eligible=True,
                ),
                StrategyV2ShadowConfig(
                    symbol="LRCX.US",
                    enabled=True,
                    universe_managed=True,
                    opening_momentum_execution_eligible=True,
                ),
            ]
        )
        db.commit()
        service = UniverseSelectionService(
            db,
            _FakeBroker(),
            catalog=_CATALOG,
            config=_config(),
            minimum_evaluable_ratio=0.5,
            minimum_residency_days=1,
            exploration_max_symbols=4,
            exploration_top_score_challengers=2,
            apply_to_watchlist=True,
            enable_shadow=True,
            now=_NOW,
        )
        items = service.items_for_run(run.id)

        first = service._result_for_existing(
            run,
            items,
            should_apply=True,
        )
        second = service._result_for_existing(
            run,
            items,
            should_apply=True,
        )

        assert first.exploration_symbols == (
            "ASML.US",
            "KLAC.US",
            "AVGO.US",
            "LRCX.US",
        )
        assert second.exploration_symbols == first.exploration_symbols
        assert first.shadow_enabled_symbols == (
            "AAPL.US",
            "ASML.US",
            "KLAC.US",
        )
        assert second.shadow_enabled_symbols == ()
        assert first.shadow_disabled_symbols == ()
        assert second.shadow_disabled_symbols == ()
        configs = {
            row.symbol: row
            for row in db.query(StrategyV2ShadowConfig).all()
        }
        assert all(
            configs[symbol].enabled
            and configs[symbol].opening_momentum_execution_eligible
            for symbol in ("AVGO.US", "LRCX.US")
        )
        assert all(
            configs[symbol].enabled
            and not configs[
                symbol
            ].opening_momentum_execution_eligible
            for symbol in ("ASML.US", "KLAC.US")
        )
    finally:
        db.close()


def test_default_selection_config_uses_active_strategy_fee_rate() -> None:
    db = _db()
    try:
        db.add(StrategyConfig(symbol="NVDA.US", fee_rate_us=0.0012))
        db.commit()

        service = UniverseSelectionService(
            db,
            _FakeBroker(),
            catalog=_CATALOG,
            minimum_evaluable_ratio=0.5,
            minimum_residency_days=1,
            apply_to_watchlist=False,
            enable_shadow=False,
            now=_NOW,
        )

        assert service.config.round_trip_fee_bps == 24.0
        assert not db.dirty
    finally:
        db.close()


def test_refresh_prefers_forward_adjusted_daily_candles() -> None:
    db = _db()
    broker = _ForwardAdjustedBroker()
    try:
        result = _service(db, broker).refresh()

        assert result.run.status == "COMPLETE"
        assert broker.adjusted_candle_calls == (
            len(_CATALOG) + 2
        )
    finally:
        db.close()


def test_refresh_persists_rotation_shadow_evidence() -> None:
    db = _db()
    broker = _LongHistoryBroker()
    try:
        result = _service(db, broker).refresh()

        assert result.run.status == "COMPLETE"
        assert result.run.completed_at is not None
        assert result.run.completed_at >= result.run.started_at
        assert broker.requested_counts
        assert min(broker.requested_counts) >= 253
        assert max(broker.requested_counts) == 1000
        metrics = json.loads(result.items[0].metrics_json)
        rotation = metrics["rotation"]
        assert rotation["algorithm_version"] == (
            "index-momentum-12-1-diversified-monthly-shadow-v3"
        )
        assert rotation["lookback_bars"] == 252
        assert rotation["skip_bars"] == 21
        assert rotation["momentum_pct"] > 0
        assert rotation["selected"] is True
        parameters = json.loads(result.run.parameters_json)
        evaluation = parameters["rotation_evaluation"]
        assert evaluation["algorithm_version"] == (
            ROTATION_WALK_FORWARD_VERSION
        )
        assert evaluation["benchmark_symbols"] == [
            "QQQ.US",
            "DIA.US",
        ]
        assert evaluation["automatic_promotion_allowed"] is False
        assert (
            "CURRENT_CONSTITUENTS_SURVIVORSHIP_BIAS"
            in evaluation["promotion_blockers"]
        )
        point_in_time = parameters[
            "rotation_point_in_time_sensitivity"
        ]
        assert point_in_time["status"] == "HISTORY_INSUFFICIENT"
        assert point_in_time["membership_history"][
            "source_version"
        ].startswith("n100tickers-")
        assert point_in_time["membership_history"][
            "authoritative_symbols"
        ] == 2
        assert point_in_time["membership_history"][
            "missing_symbols"
        ] == []
        point_in_time_evaluation = point_in_time["evaluation"]
        assert point_in_time_evaluation["data_scope"] == (
            "POINT_IN_TIME_RESEARCH_CATALOG"
        )
        assert (
            "HISTORICAL_CONSTITUENTS_OMITTED"
            in point_in_time_evaluation["promotion_blockers"]
        )
        assert (
            "POINT_IN_TIME_MEMBERSHIP_HISTORY_PARTIAL"
            not in point_in_time_evaluation["promotion_blockers"]
        )
        registration = parameters["rotation_cohort_registration"]
        assert registration["cohort_month"] == "2026-07-01"
        assert registration["signal_date"] == "2026-06-30"
        assert registration["registered_as_of_date"] == (
            result.run.as_of_date.isoformat()
        )
        assert registration["forward_eligible"] is False
        assert registration["target_signals"]
        snapshot = parameters["rotation_forward_snapshot"]
        assert snapshot["algorithm_version"] == (
            "rotation-monthly-open-forward-v2"
        )
        assert snapshot["evidence_mode"] == (
            "BACKFILLED_AFTER_ENTRY"
        )
        assert snapshot["entry_date"] == "2026-07-01"
        assert snapshot["mark_date"] == (
            result.run.as_of_date.isoformat()
        )
        assert snapshot["forward_observation_sessions"] == 0
        assert snapshot["total_estimated_cost_pct"] > 0
        assert snapshot["order_execution_allowed"] is False
        assert snapshot["automatic_promotion_allowed"] is False
        assert parameters[
            "rotation_next_cohort_registration_status"
        ] == "NOT_DUE"
        concentration = parameters[
            "rotation_concentration_challenger_snapshot"
        ]
        assert concentration["variant_name"] == (
            "concentrated_top6_12_1"
        )
        assert concentration["evidence_mode"] == (
            "BACKFILLED_AFTER_ENTRY"
        )
        assert concentration["order_execution_allowed"] is False
        concentration_registration = parameters[
            "rotation_concentration_challenger_registration"
        ]
        assert concentration_registration["target_signals"]
        assert len(
            concentration_registration["target_signals"]
        ) <= 6
        assert parameters[
            "rotation_next_concentration_challenger_registration_status"
        ] == "NOT_DUE"
        challenger = parameters[
            "rotation_weighting_challenger_snapshot"
        ]
        assert challenger["variant_name"] == (
            "diversified_top8_12_1_inverse_vol_25"
        )
        assert challenger["evidence_mode"] == (
            "BACKFILLED_AFTER_ENTRY"
        )
        assert challenger["order_execution_allowed"] is False
        challenger_registration = parameters[
            "rotation_weighting_challenger_registration"
        ]
        assert challenger_registration["target_signals"]
        assert all(
            0 < signal["target_weight_pct"] <= 25
            for signal in challenger_registration["target_signals"]
        )
        assert parameters[
            "rotation_next_weighting_challenger_registration_status"
        ] == "NOT_DUE"
        shrinkage = parameters[
            "rotation_shrinkage_challenger_snapshot"
        ]
        assert shrinkage["variant_name"] == (
            "diversified_top8_12_1_eq75_iv25_cap15"
        )
        assert shrinkage["evidence_mode"] == (
            "BACKFILLED_AFTER_ENTRY"
        )
        assert shrinkage["order_execution_allowed"] is False
        shrinkage_registration = parameters[
            "rotation_shrinkage_challenger_registration"
        ]
        assert shrinkage_registration["target_signals"]
        assert all(
            0 < signal["target_weight_pct"] <= 15
            for signal in shrinkage_registration["target_signals"]
        )
        assert parameters[
            "rotation_next_shrinkage_challenger_registration_status"
        ] == "NOT_DUE"
        return_to_variance = parameters[
            "rotation_return_to_variance_challenger_snapshot"
        ]
        assert return_to_variance["variant_name"] == (
            "diversified_top8_12_1_return_to_variance"
        )
        assert return_to_variance["evidence_mode"] == (
            "BACKFILLED_AFTER_ENTRY"
        )
        assert return_to_variance["order_execution_allowed"] is False
        return_to_variance_registration = parameters[
            "rotation_return_to_variance_challenger_registration"
        ]
        assert return_to_variance_registration["target_signals"]
        assert all(
            signal["ranking_method"] == "return_to_variance"
            and signal["formation_realized_volatility"] > 0
            and signal["ranking_metric"] > 0
            for signal in return_to_variance_registration[
                "target_signals"
            ]
        )
        assert parameters[
            "rotation_next_return_to_variance_challenger_registration_status"
        ] == "NOT_DUE"
    finally:
        db.close()


def test_refresh_reuses_frozen_rotation_registration_next_day() -> None:
    db = _db()
    try:
        first = _service(db, _LongHistoryBroker()).refresh()
        second = UniverseSelectionService(
            db,
            _LongHistoryBroker(
                end_date=datetime(
                    2026,
                    7,
                    24,
                    20,
                    tzinfo=timezone.utc,
                )
            ),
            catalog=_CATALOG,
            config=_config(),
            minimum_evaluable_ratio=0.5,
            minimum_residency_days=1,
            apply_to_watchlist=True,
            enable_shadow=False,
            now=datetime(
                2026,
                7,
                24,
                22,
                tzinfo=timezone.utc,
            ),
        ).refresh()

        assert first.run.as_of_date.isoformat() == "2026-07-23"
        assert second.run.as_of_date.isoformat() == "2026-07-24"
        first_parameters = json.loads(first.run.parameters_json)
        second_parameters = json.loads(second.run.parameters_json)
        assert second_parameters[
            "rotation_cohort_registration"
        ] == first_parameters["rotation_cohort_registration"]
        second_snapshot = second_parameters[
            "rotation_forward_snapshot"
        ]
        assert second_snapshot["registered_as_of_date"] == (
            "2026-07-23"
        )
        assert second_snapshot["mark_date"] == "2026-07-24"
        assert second_snapshot["target_symbols"] == (
            first_parameters["rotation_forward_snapshot"][
                "target_symbols"
            ]
        )
        assert second_snapshot["selection_drift_detected"] is False
        first_concentration = first_parameters[
            "rotation_concentration_challenger_snapshot"
        ]
        second_concentration = second_parameters[
            "rotation_concentration_challenger_snapshot"
        ]
        assert second_concentration["registered_as_of_date"] == (
            "2026-07-23"
        )
        assert second_concentration["mark_date"] == "2026-07-24"
        assert second_concentration["target_symbols"] == (
            first_concentration["target_symbols"]
        )
        assert second_concentration[
            "selection_drift_detected"
        ] is False
        first_challenger = first_parameters[
            "rotation_weighting_challenger_snapshot"
        ]
        second_challenger = second_parameters[
            "rotation_weighting_challenger_snapshot"
        ]
        assert second_challenger["registered_as_of_date"] == (
            "2026-07-23"
        )
        assert second_challenger["mark_date"] == "2026-07-24"
        assert [
            (holding["symbol"], holding["weight_pct"])
            for holding in second_challenger["holdings"]
        ] == [
            (holding["symbol"], holding["weight_pct"])
            for holding in first_challenger["holdings"]
        ]
        assert second_challenger[
            "selection_drift_detected"
        ] is False
        first_shrinkage = first_parameters[
            "rotation_shrinkage_challenger_snapshot"
        ]
        second_shrinkage = second_parameters[
            "rotation_shrinkage_challenger_snapshot"
        ]
        assert second_shrinkage["registered_as_of_date"] == (
            "2026-07-23"
        )
        assert second_shrinkage["mark_date"] == "2026-07-24"
        assert [
            (holding["symbol"], holding["weight_pct"])
            for holding in second_shrinkage["holdings"]
        ] == [
            (holding["symbol"], holding["weight_pct"])
            for holding in first_shrinkage["holdings"]
        ]
        assert second_shrinkage[
            "selection_drift_detected"
        ] is False
        first_return_to_variance = first_parameters[
            "rotation_return_to_variance_challenger_snapshot"
        ]
        second_return_to_variance = second_parameters[
            "rotation_return_to_variance_challenger_snapshot"
        ]
        assert second_return_to_variance[
            "registered_as_of_date"
        ] == "2026-07-23"
        assert second_return_to_variance["mark_date"] == "2026-07-24"
        assert [
            (
                holding["symbol"],
                holding["weight_pct"],
                holding["ranking_metric"],
            )
            for holding in second_return_to_variance["holdings"]
        ] == [
            (
                holding["symbol"],
                holding["weight_pct"],
                holding["ranking_metric"],
            )
            for holding in first_return_to_variance["holdings"]
        ]
        assert second_return_to_variance[
            "selection_drift_detected"
        ] is False
        first_rotation = {
            row.symbol: json.loads(row.metrics_json)["rotation"]
            for row in first.items
        }
        second_rotation = {
            row.symbol: json.loads(row.metrics_json)["rotation"]
            for row in second.items
        }
        assert second_rotation == first_rotation
    finally:
        db.close()


def test_month_end_refresh_preregisters_next_rotation_cohort() -> None:
    db = _db()
    try:
        result = UniverseSelectionService(
            db,
            _LongHistoryBroker(
                end_date=datetime(
                    2026,
                    7,
                    31,
                    20,
                    tzinfo=timezone.utc,
                )
            ),
            catalog=_CATALOG,
            config=_config(),
            minimum_evaluable_ratio=0.5,
            minimum_residency_days=1,
            apply_to_watchlist=False,
            enable_shadow=False,
            now=datetime(
                2026,
                8,
                1,
                2,
                tzinfo=timezone.utc,
            ),
        ).refresh()

        parameters = json.loads(result.run.parameters_json)
        assert result.run.as_of_date.isoformat() == "2026-07-31"
        assert parameters[
            "rotation_next_cohort_registration_status"
        ] == "REGISTERED"
        registration = parameters[
            "rotation_next_cohort_registration"
        ]
        assert registration["cohort_month"] == "2026-08-01"
        assert registration["signal_date"] == "2026-07-31"
        assert registration["registered_as_of_date"] == "2026-07-31"
        assert registration["forward_eligible"] is True
        assert registration["target_signals"]
        assert parameters[
            "rotation_next_concentration_challenger_registration_status"
        ] == "REGISTERED"
        concentration_registration = parameters[
            "rotation_next_concentration_challenger_registration"
        ]
        assert concentration_registration["cohort_month"] == (
            "2026-08-01"
        )
        assert concentration_registration["signal_date"] == (
            "2026-07-31"
        )
        assert concentration_registration["forward_eligible"] is True
        assert concentration_registration["variant_name"] == (
            "concentrated_top6_12_1"
        )
        assert concentration_registration["target_signals"]
        assert parameters[
            "rotation_next_weighting_challenger_registration_status"
        ] == "REGISTERED"
        challenger_registration = parameters[
            "rotation_next_weighting_challenger_registration"
        ]
        assert challenger_registration["cohort_month"] == (
            "2026-08-01"
        )
        assert challenger_registration["signal_date"] == (
            "2026-07-31"
        )
        assert challenger_registration["forward_eligible"] is True
        assert challenger_registration["variant_name"] == (
            "diversified_top8_12_1_inverse_vol_25"
        )
        assert challenger_registration["target_signals"]
        assert parameters[
            "rotation_next_shrinkage_challenger_registration_status"
        ] == "REGISTERED"
        shrinkage_registration = parameters[
            "rotation_next_shrinkage_challenger_registration"
        ]
        assert shrinkage_registration["cohort_month"] == (
            "2026-08-01"
        )
        assert shrinkage_registration["signal_date"] == (
            "2026-07-31"
        )
        assert shrinkage_registration["forward_eligible"] is True
        assert shrinkage_registration["variant_name"] == (
            "diversified_top8_12_1_eq75_iv25_cap15"
        )
        assert shrinkage_registration["target_signals"]
        assert all(
            0 < signal["target_weight_pct"] <= 15
            for signal in shrinkage_registration["target_signals"]
        )
        assert parameters[
            "rotation_next_return_to_variance_challenger_registration_status"
        ] == "REGISTERED"
        return_to_variance_registration = parameters[
            "rotation_next_return_to_variance_challenger_registration"
        ]
        assert return_to_variance_registration["cohort_month"] == (
            "2026-08-01"
        )
        assert return_to_variance_registration["signal_date"] == (
            "2026-07-31"
        )
        assert return_to_variance_registration[
            "forward_eligible"
        ] is True
        assert return_to_variance_registration["variant_name"] == (
            "diversified_top8_12_1_return_to_variance"
        )
        assert return_to_variance_registration["target_signals"]
        assert all(
            signal["ranking_method"] == "return_to_variance"
            and signal["formation_realized_volatility"] > 0
            and signal["ranking_metric"] > 0
            for signal in return_to_variance_registration[
                "target_signals"
            ]
        )
    finally:
        db.close()


def test_next_month_refresh_reuses_all_preregistered_rotation_tracks() -> None:
    db = _db()
    try:
        month_end = UniverseSelectionService(
            db,
            _LongHistoryBroker(
                end_date=datetime(
                    2026,
                    7,
                    31,
                    20,
                    tzinfo=timezone.utc,
                )
            ),
            catalog=_CATALOG,
            config=_config(),
            minimum_evaluable_ratio=0.5,
            minimum_residency_days=1,
            apply_to_watchlist=False,
            enable_shadow=False,
            now=datetime(
                2026,
                8,
                1,
                2,
                tzinfo=timezone.utc,
            ),
        ).refresh()
        august = UniverseSelectionService(
            db,
            _LongHistoryBroker(
                end_date=datetime(
                    2026,
                    8,
                    3,
                    20,
                    tzinfo=timezone.utc,
                )
            ),
            catalog=_CATALOG,
            config=_config(),
            minimum_evaluable_ratio=0.5,
            minimum_residency_days=1,
            apply_to_watchlist=False,
            enable_shadow=False,
            now=datetime(
                2026,
                8,
                4,
                2,
                tzinfo=timezone.utc,
            ),
        ).refresh()

        month_end_parameters = json.loads(
            month_end.run.parameters_json
        )
        august_parameters = json.loads(august.run.parameters_json)
        track_keys = (
            (
                "rotation_next_cohort_registration",
                "rotation_forward_snapshot",
            ),
            (
                "rotation_next_concentration_challenger_registration",
                "rotation_concentration_challenger_snapshot",
            ),
            (
                "rotation_next_weighting_challenger_registration",
                "rotation_weighting_challenger_snapshot",
            ),
            (
                "rotation_next_shrinkage_challenger_registration",
                "rotation_shrinkage_challenger_snapshot",
            ),
            (
                "rotation_next_return_to_variance_challenger_registration",
                "rotation_return_to_variance_challenger_snapshot",
            ),
        )
        for registration_key, snapshot_key in track_keys:
            registration = month_end_parameters[registration_key]
            snapshot = august_parameters[snapshot_key]
            assert snapshot["evidence_mode"] == (
                "FORWARD_PRECOMMITTED"
            )
            assert snapshot["registered_as_of_date"] == "2026-07-31"
            assert snapshot["entry_date"] == "2026-08-03"
            assert snapshot["forward_observation_sessions"] == 1
            assert snapshot["target_symbols"] == [
                signal["symbol"]
                for signal in registration["target_signals"]
            ]
            assert snapshot["order_execution_allowed"] is False
    finally:
        db.close()


def test_refresh_persists_and_applies_read_only_candidates_idempotently() -> None:
    db = _db()
    broker = _FakeBroker()
    try:
        db.add(StrategyConfig(symbol="NVDA.US", market="US"))
        db.add(
            WatchlistItem(
                symbol="NVDA.US",
                market="US",
                alias="NVIDIA",
                source="manual",
                is_active=True,
            )
        )
        db.commit()
        service = _service(db, broker)

        first = service.refresh()
        calls_after_first = (broker.quote_calls, broker.candle_calls)
        second = service.refresh()

        assert first.run.status == "COMPLETE"
        assert first.applied is True
        assert set(first.added_symbols) == {"AAPL.US", "JPM.US"}
        assert second.run.id == first.run.id
        assert set(second.retained_symbols) == {"AAPL.US", "JPM.US"}
        assert (broker.quote_calls, broker.candle_calls) == calls_after_first
        rows = {
            row.symbol: row
            for row in db.query(WatchlistItem).all()
        }
        assert rows["NVDA.US"].source == "manual"
        assert rows["NVDA.US"].is_active is True
        assert rows["AAPL.US"].source == "universe"
        assert rows["AAPL.US"].is_active is False
        assert db.query(UniverseSelectionRun).count() == 1
        assert db.query(UniverseSelectionCandidate).count() == 2
    finally:
        db.close()


def test_degraded_same_day_run_retries_and_recovers_in_place() -> None:
    db = _db()
    broker = _FakeBroker(failing=True)
    try:
        service = _service(db, broker)

        degraded = service.refresh()
        degraded_status = degraded.run.status
        degraded_run_id = degraded.run.id
        broker.failing = False
        recovered = service.refresh()

        assert degraded_status == "DEGRADED"
        assert recovered.run.status == "COMPLETE"
        assert recovered.run.id == degraded_run_id
        assert recovered.run.selected_count == 2
        assert db.query(UniverseSelectionRun).count() == 1
        assert db.query(UniverseSelectionCandidate).count() == 2
    finally:
        db.close()


def test_abandoned_running_claim_is_taken_over_after_lease() -> None:
    db = _db()
    try:
        degraded = _service(
            db,
            _FakeBroker(failing=True),
        ).refresh(apply_to_watchlist=False)
        run = db.get(UniverseSelectionRun, degraded.run.id)
        assert run is not None
        run.status = "RUNNING"
        run.error = "refresh-claim:abandoned"
        run.started_at = datetime.now(timezone.utc) - timedelta(
            minutes=10,
        )
        run.completed_at = None
        db.commit()

        recovered = _service(
            db,
            _FakeBroker(),
        ).refresh(apply_to_watchlist=False)

        assert recovered.run.id == degraded.run.id
        assert recovered.run.status == "COMPLETE"
        assert recovered.run.selected_count == 2
        assert len(recovered.items) == recovered.run.candidate_count == 2
        assert (
            sum(item.selected for item in recovered.items)
            == recovered.run.selected_count
        )
    finally:
        db.close()


def test_degraded_retry_has_one_cross_process_claim_and_atomic_evidence(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "universe-cas.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"timeout": 15},
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        connection.exec_driver_sql("PRAGMA busy_timeout=15000")
    Base.metadata.create_all(engine)
    seed_db = sessionmaker(bind=engine)()
    try:
        seeded = UniverseSelectionService(
            seed_db,
            _FakeBroker(failing=True),
            catalog=_CATALOG,
            config=_config(),
            minimum_evaluable_ratio=0.5,
            minimum_residency_days=1,
            apply_to_watchlist=False,
            enable_shadow=False,
            now=_NOW,
        ).refresh(apply_to_watchlist=False)
        seeded_run_id = seeded.run.id
        assert seeded.run.status == "DEGRADED"
    finally:
        seed_db.close()
        engine.dispose()

    context = get_context("spawn")
    healthy_worker_started = context.Event()
    healthy_evaluation_started = context.Event()
    release_healthy = context.Event()
    healthy_results = context.Queue()
    failing_worker_started = context.Event()
    failing_evaluation_started = context.Event()
    release_failing = context.Event()
    failing_results = context.Queue()
    healthy_process = context.Process(
        target=_concurrent_refresh_worker,
        kwargs={
            "database_path": str(database_path),
            "failing": False,
            "worker_started": healthy_worker_started,
            "evaluation_started": healthy_evaluation_started,
            "release_evaluation": release_healthy,
            "result_queue": healthy_results,
        },
    )
    failing_process = context.Process(
        target=_concurrent_refresh_worker,
        kwargs={
            "database_path": str(database_path),
            "failing": True,
            "worker_started": failing_worker_started,
            "evaluation_started": failing_evaluation_started,
            "release_evaluation": release_failing,
            "result_queue": failing_results,
        },
    )
    try:
        healthy_process.start()
        assert healthy_worker_started.wait(timeout=10)
        assert healthy_evaluation_started.wait(timeout=10)

        # The healthy worker owns the retry but is deliberately paused after
        # claiming it. Under the old read/evaluate/write flow this delayed
        # failing worker also evaluated the same DEGRADED run and could
        # replace the healthy candidate rows after COMPLETE was committed.
        failing_process.start()
        assert failing_worker_started.wait(timeout=10)
        failing_worker_evaluated = failing_evaluation_started.wait(
            timeout=1,
        )

        release_healthy.set()
        healthy_process.join(timeout=15)
        assert not healthy_process.is_alive()
        assert healthy_process.exitcode == 0

        release_failing.set()
        failing_process.join(timeout=15)
        assert not failing_process.is_alive()
        assert failing_process.exitcode == 0
    finally:
        release_healthy.set()
        release_failing.set()
        for process in (healthy_process, failing_process):
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)

    healthy_result = cast(
        dict[str, object],
        healthy_results.get(timeout=5),
    )
    failing_result = cast(
        dict[str, object],
        failing_results.get(timeout=5),
    )
    assert "error" not in healthy_result
    assert "error" not in failing_result
    assert healthy_result == failing_result
    assert healthy_result == {
        "run_id": seeded_run_id,
        "status": "COMPLETE",
        "selected_count": 2,
        "selected_symbols": ["AAPL.US", "JPM.US"],
        "item_count": 2,
    }
    assert failing_worker_evaluated is False
    assert failing_evaluation_started.is_set() is False

    verify_engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"timeout": 15},
    )
    verify_db = sessionmaker(bind=verify_engine)()
    try:
        final_run = verify_db.get(
            UniverseSelectionRun,
            seeded_run_id,
        )
        assert final_run is not None
        final_items = (
            verify_db.query(UniverseSelectionCandidate)
            .filter(
                UniverseSelectionCandidate.run_id == seeded_run_id,
            )
            .all()
        )
        assert final_run.status == "COMPLETE"
        assert len(final_items) == final_run.candidate_count == 2
        assert (
            sum(item.selected for item in final_items)
            == final_run.selected_count
            == 2
        )
        assert all(
            "DATA_" not in item.exclusion_reasons_json
            for item in final_items
        )
    finally:
        verify_db.close()
        verify_engine.dispose()


def test_stale_consensus_session_fails_closed_on_expected_run_date() -> None:
    class _StaleBroker(_FakeBroker):
        def get_candlesticks(
            self,
            symbol: str,
            period: str,
            count: int,
        ) -> list[BrokerCandle]:
            return super().get_candlesticks(
                symbol,
                period,
                count,
            )[:-3]

    db = _db()
    try:
        result = _service(db, _StaleBroker()).refresh()

        assert result.run.as_of_date.isoformat() == "2026-07-23"
        assert result.run.status == "DEGRADED"
        assert result.run.selected_count == 0
        assert result.run.evaluable_count == 0
        assert result.exploration_symbols == ()
        assert all(
            "DATA_STALE_SESSION_DATE" in row.exclusion_reasons_json
            for row in result.items
        )
        assert db.query(WatchlistItem).count() == 0
    finally:
        db.close()


def test_post_close_refresh_uses_current_completed_session() -> None:
    class _PostCloseBroker(_FakeBroker):
        def get_candlesticks(
            self,
            symbol: str,
            period: str,
            count: int,
        ) -> list[BrokerCandle]:
            bars = super().get_candlesticks(symbol, period, count)
            previous = bars[-1]
            close = previous.close * 1.01
            return [
                *bars,
                BrokerCandle(
                    timestamp=previous.timestamp + timedelta(days=1),
                    open=previous.close,
                    high=close * 1.01,
                    low=previous.close * 0.99,
                    close=close,
                    volume=20_000_000,
                ),
            ][-count:]

    db = _db()
    try:
        result = UniverseSelectionService(
            db,
            _PostCloseBroker(),
            catalog=_CATALOG,
            config=_config(),
            minimum_evaluable_ratio=0.5,
            minimum_residency_days=1,
            apply_to_watchlist=False,
            enable_shadow=False,
            now=datetime(2026, 7, 24, 20, 15, tzinfo=timezone.utc),
        ).refresh(apply_to_watchlist=False)

        assert result.run.as_of_date.isoformat() == "2026-07-24"
        assert result.run.status == "COMPLETE"
        assert result.run.evaluable_count == 2
        assert result.run.selected_count == 2
    finally:
        db.close()


def test_reconcile_removes_expired_auto_item_but_keeps_live_exposure() -> None:
    db = _db()
    broker = _FakeBroker()
    try:
        old = _NOW - timedelta(days=3)
        db.add_all(
            [
                WatchlistItem(
                    symbol="REMOVE.US",
                    market="US",
                    alias="Remove",
                    source="universe",
                    created_at=old,
                ),
                WatchlistItem(
                    symbol="KEEP.US",
                    market="US",
                    alias="Keep",
                    source="universe",
                    created_at=old,
                ),
                TrackedEntry(
                    symbol="KEEP.US",
                    side="LONG",
                    quantity=1,
                    cost=100,
                ),
            ]
        )
        db.commit()

        result = _service(db, broker).refresh()

        symbols = {
            row.symbol
            for row in db.query(WatchlistItem).all()
        }
        assert "REMOVE.US" not in symbols
        assert "KEEP.US" in symbols
        assert "REMOVE.US" in result.removed_symbols
        assert "KEEP.US" in result.retained_symbols
    finally:
        db.close()


def test_reconcile_disables_shadow_owned_by_removed_universe_item() -> None:
    db = _db()
    try:
        db.add(
            WatchlistItem(
                symbol="REMOVE.US",
                market="US",
                alias="Remove",
                source="universe",
                created_at=_NOW - timedelta(days=3),
            ),
        )
        db.add(
            StrategyV2ShadowConfig(
                symbol="REMOVE.US",
                enabled=True,
                universe_managed=True,
                opening_momentum_execution_eligible=False,
            ),
        )
        db.commit()

        result = _service(
            db,
            _FakeBroker(),
            enable_shadow=True,
        ).refresh()

        config = (
            db.query(StrategyV2ShadowConfig)
            .filter(StrategyV2ShadowConfig.symbol == "REMOVE.US")
            .one()
        )
        assert "REMOVE.US" in result.removed_symbols
        assert config.enabled is False
        assert config.universe_managed is True
        assert result.shadow_disabled_symbols == ("REMOVE.US",)
    finally:
        db.close()


@pytest.mark.parametrize(
    ("targets", "point_in_time_shrinkage"),
    (
        (
            (
                ("INTC.US", 100.0),
                ("CAT.US", 90.0),
                ("GS.US", 80.0),
                ("AEP.US", 70.0),
            ),
            False,
        ),
        (
            (
                ("INTC.US", 100.0),
                ("CAT.US", 90.0),
                ("GOOGL.US", 80.0),
                ("ROST.US", 70.0),
                ("MRK.US", 60.0),
                ("GS.US", 50.0),
                ("FANG.US", 40.0),
                ("AEP.US", 30.0),
            ),
            True,
        ),
    ),
)
def test_reconcile_keeps_validated_rotation_targets_shadow_only(
    targets: tuple[tuple[str, float], ...],
    point_in_time_shrinkage: bool,
) -> None:
    db = _db()
    try:
        run = UniverseSelectionRun(
            as_of_date=_NOW.date() - timedelta(days=1),
            algorithm_version="selector-v1",
            source_version="catalog-v1",
            status="COMPLETE",
            candidate_count=len(targets) + 1,
            evaluable_count=len(targets) + 1,
            selected_count=1,
            coverage_ratio=1.0,
            parameters_json=_validated_rotation_parameters(
                targets,
                point_in_time_shrinkage=point_in_time_shrinkage,
            ),
            started_at=_NOW - timedelta(hours=2),
            completed_at=_NOW - timedelta(hours=1),
            created_at=_NOW - timedelta(hours=2),
        )
        db.add(run)
        db.flush()
        db.add(
            UniverseSelectionCandidate(
                run_id=run.id,
                symbol="AAPL.US",
                sector="Technology Hardware",
                selected=True,
                rank=1,
                score=95.0,
            )
        )
        for rank, (symbol, score) in enumerate(targets, start=1):
            db.add(
                UniverseSelectionCandidate(
                    run_id=run.id,
                    symbol=symbol,
                    sector="Test",
                    selected=False,
                    score=score,
                    metrics_json=json.dumps({
                        "rotation": {
                            "algorithm_version": (
                                ROTATION_ALGORITHM_VERSION
                            ),
                            "selected": (
                                not point_in_time_shrinkage
                            ),
                            "rank": (
                                None
                                if point_in_time_shrinkage
                                else rank
                            ),
                            "score": (
                                score + 1
                                if point_in_time_shrinkage
                                else score
                            ),
                        }
                    }),
                    exclusion_reasons_json=json.dumps([
                        "ATR_OUTSIDE_RANGE"
                    ]),
                )
            )
        db.add(
            StrategyV2ShadowConfig(
                symbol="INTC.US",
                enabled=False,
                universe_managed=True,
            )
        )
        db.commit()

        service = _service(db, _FakeBroker(), enable_shadow=True)
        result = service._result_for_existing(
            run,
            service.items_for_run(run.id),
            should_apply=True,
        )

        watchlist_symbols = {
            row.symbol for row in db.query(WatchlistItem).all()
        }
        shadow_rows = {
            row.symbol: row
            for row in db.query(StrategyV2ShadowConfig).all()
        }
        assert watchlist_symbols == {"AAPL.US"}
        assert result.exploration_symbols == ()
        assert set(result.shadow_enabled_symbols) >= {
            symbol for symbol, _ in targets
        }
        assert all(
            shadow_rows[symbol].enabled
            and shadow_rows[symbol].universe_managed
            and not shadow_rows[
                symbol
            ].opening_momentum_execution_eligible
            for symbol, _ in targets
        )
    finally:
        db.close()


def test_rotation_observation_validation_is_variant_aware_and_fail_closed(
) -> None:
    targets = (
        ("INTC.US", 100.0),
        ("CAT.US", 90.0),
        ("GS.US", 80.0),
        ("AEP.US", 70.0),
    )
    run = UniverseSelectionRun(
        id=17,
        as_of_date=_NOW.date() - timedelta(days=1),
        algorithm_version="selector-v1",
        source_version="catalog-v1",
        status="COMPLETE",
        parameters_json=_validated_rotation_parameters(targets),
    )
    candidates = [
        UniverseSelectionCandidate(
            run_id=run.id,
            symbol=symbol,
            metrics_json=json.dumps({
                "rotation": {
                    "algorithm_version": ROTATION_ALGORITHM_VERSION,
                    "selected": True,
                    "rank": rank,
                    "score": score,
                }
            }),
        )
        for rank, (symbol, score) in enumerate(targets, start=1)
    ]
    candidates[0].metrics_json = json.dumps({
        "rotation": {
            "algorithm_version": ROTATION_ALGORITHM_VERSION,
            "selected": False,
            "rank": None,
            "score": 1.0,
        }
    })

    assert validated_inverse_volatility_observation_symbols(
        run,
        candidates,
        session_date=_NOW.date(),
    ) == frozenset()

    pit_targets = targets + (
        ("ROST.US", 60.0),
        ("MRK.US", 50.0),
        ("GOOGL.US", 40.0),
        ("FANG.US", 30.0),
    )
    pit_candidates = candidates + [
        UniverseSelectionCandidate(
            run_id=run.id,
            symbol=symbol,
            metrics_json="{}",
        )
        for symbol, _ in pit_targets[len(targets):]
    ]
    run.parameters_json = _validated_rotation_parameters(
        pit_targets,
        point_in_time_shrinkage=True,
    )
    assert validated_point_in_time_shrinkage_observation_symbols(
        run,
        pit_candidates,
        session_date=_NOW.date(),
    ) == frozenset(symbol for symbol, _ in pit_targets)
    assert validated_point_in_time_shrinkage_observation_symbols(
        run,
        pit_candidates[:-1],
        session_date=_NOW.date(),
    ) == frozenset()


def test_reconcile_never_disables_manually_enabled_shadow() -> None:
    db = _db()
    try:
        db.add(
            StrategyV2ShadowConfig(
                symbol="MANUAL.US",
                enabled=True,
                universe_managed=False,
            ),
        )
        db.commit()

        _service(
            db,
            _FakeBroker(),
            enable_shadow=True,
        ).refresh()

        config = (
            db.query(StrategyV2ShadowConfig)
            .filter(StrategyV2ShadowConfig.symbol == "MANUAL.US")
            .one()
        )
        assert config.enabled is True
        assert config.universe_managed is False
    finally:
        db.close()


def test_reconcile_upgrades_enabled_managed_legacy_us_bracket() -> None:
    db = _db()
    try:
        db.add(
            StrategyV2ShadowConfig(
                symbol="AAPL.US",
                enabled=True,
                universe_managed=True,
                stop_loss_pct=0.75,
                profit_target_pct=0.50,
            ),
        )
        db.commit()

        result = _service(
            db,
            _FakeBroker(),
            enable_shadow=True,
        ).refresh()
        config = (
            db.query(StrategyV2ShadowConfig)
            .filter(StrategyV2ShadowConfig.symbol == "AAPL.US")
            .one()
        )

        assert config.enabled is True
        assert config.universe_managed is True
        assert config.opening_momentum_execution_eligible is True
        assert config.stop_loss_pct == 0.45
        assert config.profit_target_pct == 0.80
        assert "AAPL.US" not in result.shadow_enabled_symbols
    finally:
        db.close()


def test_reconcile_preserves_enabled_unmanaged_legacy_us_bracket() -> None:
    db = _db()
    try:
        db.add(
            StrategyV2ShadowConfig(
                symbol="AAPL.US",
                enabled=True,
                universe_managed=False,
                stop_loss_pct=0.75,
                profit_target_pct=0.50,
            ),
        )
        db.commit()

        _service(
            db,
            _FakeBroker(),
            enable_shadow=True,
        ).refresh()
        config = (
            db.query(StrategyV2ShadowConfig)
            .filter(StrategyV2ShadowConfig.symbol == "AAPL.US")
            .one()
        )

        assert config.universe_managed is False
        assert config.stop_loss_pct == 0.75
        assert config.profit_target_pct == 0.50
    finally:
        db.close()


def test_manual_disable_is_not_undone_by_next_universe_refresh() -> None:
    from app.services.strategy_v2_shadow_service import (
        StrategyV2ShadowService,
    )

    db = _db()
    try:
        service = _service(
            db,
            _FakeBroker(),
            enable_shadow=True,
        )
        first = service.refresh()
        symbol = first.items[0].symbol
        StrategyV2ShadowService(db).update_config(
            StrategyV2ShadowConfigUpdate(enabled=False),
            symbol=symbol,
        )

        second = service.refresh()
        config = (
            db.query(StrategyV2ShadowConfig)
            .filter(StrategyV2ShadowConfig.symbol == symbol)
            .one()
        )

        assert second.run.id == first.run.id
        assert config.enabled is False
        assert config.universe_managed is False
        assert symbol not in second.shadow_enabled_symbols
    finally:
        db.close()


def test_shadow_enable_failure_does_not_leave_orphaned_ownership(
    monkeypatch,
) -> None:
    from app.services.strategy_v2_shadow_service import (
        StrategyV2ShadowService,
    )

    db = _db()
    original_update = StrategyV2ShadowService.update_config

    def fail_enable(
        service,
        payload,
        *,
        symbol=None,
        preserve_universe_management=False,
    ):
        if payload.enabled:
            raise RuntimeError("injected enable failure")
        return original_update(
            service,
            payload,
            symbol=symbol,
            preserve_universe_management=preserve_universe_management,
        )

    monkeypatch.setattr(
        StrategyV2ShadowService,
        "update_config",
        fail_enable,
    )
    try:
        result = _service(
            db,
            _FakeBroker(),
            enable_shadow=True,
        ).refresh()

        configs = db.query(StrategyV2ShadowConfig).all()
        assert result.shadow_failed_symbols
        assert configs
        assert all(row.enabled is False for row in configs)
        assert all(row.universe_managed is False for row in configs)
    finally:
        db.close()


class _LeaseGuardSpy:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.fenced_transaction: object | None = None

    def __enter__(self) -> _LeaseGuardSpy:
        self.events.append("keepalive_enter")
        return self

    def __exit__(
        self,
        _exc_type: object,
        _exc_value: object,
        _traceback: object,
    ) -> bool:
        self.events.append("release")
        return False

    def checkpoint(self) -> object:
        self.events.append("checkpoint")
        return object()

    def fence_in_transaction(self, session: Session) -> object:
        self.events.append("fence")
        if not session.in_transaction():
            session.begin()
        self.fenced_transaction = session.get_transaction()
        assert self.fenced_transaction is not None
        return object()


class _LeaseServiceSpy:
    def __init__(
        self,
        guard: _LeaseGuardSpy,
        *,
        acquired: bool = True,
    ) -> None:
        self.guard = guard
        self.acquired = acquired
        self.keys: list[str] = []
        self.handle = object()

    def try_acquire(self, lease_key: str) -> object | None:
        self.keys.append(lease_key)
        self.guard.events.append("acquire")
        return self.handle if self.acquired else None

    def keepalive(self, handle: object) -> _LeaseGuardSpy:
        assert handle is self.handle
        self.guard.events.append("keepalive")
        return self.guard


class _LostLeaseGuard:
    def checkpoint(self) -> object:
        return object()

    def fence_in_transaction(self, _session: Session) -> object:
        raise LeaseLostError("injected universe lease loss")


def test_durable_lease_busy_skips_broker_and_database_writes() -> None:
    db = _db()
    broker = _FakeBroker()
    events: list[str] = []
    lease_service = _LeaseServiceSpy(
        _LeaseGuardSpy(events),
        acquired=False,
    )
    service = _service(db, broker)
    service.lease_service = cast(
        DurableJobLeaseService,
        lease_service,
    )
    try:
        with pytest.raises(UniverseSelectionLeaseBusyError):
            service.refresh()

        assert lease_service.keys == ["universe_selection"]
        assert events == ["acquire"]
        assert broker.quote_calls == 0
        assert broker.candle_calls == 0
        assert db.query(UniverseSelectionRun).count() == 0
    finally:
        db.close()


def test_durable_lease_keepalive_checkpoints_and_releases_normally() -> None:
    db = _db()
    events: list[str] = []
    lease_service = _LeaseServiceSpy(_LeaseGuardSpy(events))
    service = _service(db, _FakeBroker())
    service.lease_service = cast(
        DurableJobLeaseService,
        lease_service,
    )
    try:
        result = service.refresh(apply_to_watchlist=False)

        assert result.run.status == "COMPLETE"
        assert lease_service.keys == ["universe_selection"]
        assert events[:3] == [
            "acquire",
            "keepalive",
            "keepalive_enter",
        ]
        assert "fence" in events
        assert events.count("checkpoint") >= 2
        assert events[-1] == "release"
    finally:
        db.close()


def test_claim_run_fences_before_upsert_in_the_same_transaction() -> None:
    db = _db()
    service = _service(db, _FakeBroker())
    events_seen: list[str] = []
    guard = _LeaseGuardSpy(events_seen)

    def _record_run_dml(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        normalized = " ".join(statement.upper().split())
        if normalized.startswith("INSERT INTO UNIVERSE_SELECTION_RUNS"):
            events_seen.append("run_upsert")
            assert db.get_transaction() is guard.fenced_transaction

    event.listen(db.get_bind(), "before_cursor_execute", _record_run_dml)
    try:
        parameters = service._parameters()
        claim = service._claim_run(
            as_of_date=date(2026, 7, 23),
            algorithm_version=service._algorithm_version(parameters),
            parameters=parameters,
            lease_guard=cast(LeaseKeepalive, guard),
        )

        assert claim is not None
        assert events_seen.index("fence") < events_seen.index("run_upsert")
    finally:
        event.remove(
            db.get_bind(),
            "before_cursor_execute",
            _record_run_dml,
        )
        db.close()


def test_publish_claim_lease_loss_keeps_run_nonterminal_and_candidates_empty(
) -> None:
    db = _db()
    service = _service(db, _FakeBroker())
    parameters = service._parameters()
    algorithm_version = service._algorithm_version(parameters)
    try:
        claim = service._claim_run(
            as_of_date=date(2026, 7, 23),
            algorithm_version=algorithm_version,
            parameters=parameters,
        )
        assert claim is not None
        selections, _, rotation_parameters = service._evaluate_catalog(
            expected_as_of_date=date(2026, 7, 23),
        )

        with pytest.raises(LeaseLostError):
            service._publish_claim(
                claim,
                selections=selections,
                status="COMPLETE",
                candidate_count=len(selections),
                evaluable_count=sum(row.evaluable for row in selections),
                selected_count=sum(row.selected for row in selections),
                coverage_ratio=1.0,
                parameters={**parameters, **rotation_parameters},
                error="",
                lease_guard=cast(LeaseKeepalive, _LostLeaseGuard()),
            )

        db.rollback()
        db.expire_all()
        run = db.get(UniverseSelectionRun, claim.run_id)
        assert run is not None
        assert run.status == "RUNNING"
        assert db.query(UniverseSelectionCandidate).count() == 0
    finally:
        db.close()


def test_reconcile_watchlist_lease_loss_leaves_watchlist_unchanged() -> None:
    db = _db()
    db.add(
        WatchlistItem(
            symbol="AAPL.US",
            market="US",
            alias="Original Apple",
            source="manual",
            is_active=False,
            created_at=_NOW,
        )
    )
    db.commit()
    candidate = UniverseSelectionCandidate(
        run_id=1,
        symbol="JPM.US",
        market="US",
        alias="JPMorgan Chase",
        sector="Financials",
        memberships_json='["DJIA"]',
        selected=True,
        score=1.0,
        metrics_json="{}",
        exclusion_reasons_json="[]",
        created_at=_NOW,
    )
    try:
        with pytest.raises(LeaseLostError):
            _service(db, _FakeBroker())._reconcile_watchlist(
                [candidate],
                lease_guard=cast(LeaseKeepalive, _LostLeaseGuard()),
            )

        db.rollback()
        rows = db.query(WatchlistItem).all()
        assert [(row.symbol, row.alias) for row in rows] == [
            ("AAPL.US", "Original Apple")
        ]
    finally:
        db.close()


def test_reconcile_watchlist_rereads_primary_symbol_after_fence(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'primary-switch.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    db = sessions()
    db.add(StrategyConfig(symbol="AAPL.US", market="US"))
    db.add_all([
        WatchlistItem(
            symbol="AAPL.US",
            market="US",
            alias="Apple",
            source="manual",
            is_active=True,
            created_at=_NOW,
        ),
        WatchlistItem(
            symbol="JPM.US",
            market="US",
            alias="JPMorgan Chase",
            source="manual",
            is_active=False,
            created_at=_NOW,
        ),
    ])
    db.commit()
    candidate = UniverseSelectionCandidate(
        run_id=1,
        symbol="JPM.US",
        market="US",
        alias="JPMorgan Chase",
        sector="Financials",
        memberships_json='["DJIA"]',
        selected=True,
        score=1.0,
        metrics_json="{}",
        exclusion_reasons_json="[]",
        created_at=_NOW,
    )

    class _PrimarySwitchGuard:
        switched = False

        @staticmethod
        def checkpoint() -> object:
            return object()

        def fence_in_transaction(self, session: Session) -> object:
            assert session is db
            assert session.in_transaction() is False
            with sessions.begin() as operator_db:
                config = (
                    operator_db.query(StrategyConfig)
                    .order_by(StrategyConfig.id.desc())
                    .one()
                )
                config.symbol = "JPM.US"
                operator_db.query(WatchlistItem).update(
                    {WatchlistItem.is_active: False},
                    synchronize_session=False,
                )
                operator_db.query(WatchlistItem).filter(
                    WatchlistItem.symbol == "JPM.US",
                ).update(
                    {WatchlistItem.is_active: True},
                    synchronize_session=False,
                )
            self.switched = True
            session.begin()
            return object()

    guard = _PrimarySwitchGuard()
    try:
        _service(db, _FakeBroker())._reconcile_watchlist(
            [candidate],
            lease_guard=cast(LeaseKeepalive, guard),
        )

        rows = {
            row.symbol: row.is_active
            for row in db.query(WatchlistItem).all()
        }
        assert guard.switched is True
        assert rows == {"AAPL.US": False, "JPM.US": True}
    finally:
        db.close()
        engine.dispose()


def test_shadow_lease_loss_is_not_swallowed_or_followed_by_more_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.strategy_v2_shadow_service import (
        StrategyV2ShadowService,
    )

    db = _db()
    calls: list[str] = []

    def _lose_lease(
        _service: StrategyV2ShadowService,
        symbol: str,
    ) -> object:
        calls.append(symbol)
        raise LeaseLostError("injected shadow lease loss")

    monkeypatch.setattr(
        StrategyV2ShadowService,
        "ensure_universe_managed_enabled",
        _lose_lease,
    )
    try:
        with pytest.raises(LeaseLostError):
            _service(
                db,
                _FakeBroker(),
                enable_shadow=True,
            )._sync_observation_shadows(
                observed_symbols={"AAPL.US", "JPM.US"},
            )

        assert calls == ["AAPL.US"]
        assert (
            db.query(StrategyV2ShadowConfig)
            .filter(StrategyV2ShadowConfig.symbol == "JPM.US")
            .first()
            is None
        )
    finally:
        db.close()


def test_fenced_shadow_enable_failure_releases_new_config_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.strategy_v2_shadow_service import (
        StrategyV2ShadowService,
    )

    db = _db()
    guard = _LeaseGuardSpy([])

    def _fail_enable(
        _service: StrategyV2ShadowService,
        _symbol: str,
    ) -> object:
        raise RuntimeError("injected fenced enable failure")

    monkeypatch.setattr(
        StrategyV2ShadowService,
        "ensure_universe_managed_enabled",
        _fail_enable,
    )
    try:
        enabled, disabled, failures = _service(
            db,
            _FakeBroker(),
            enable_shadow=True,
        )._sync_observation_shadows(
            observed_symbols={"AAPL.US"},
            lease_guard=cast(LeaseKeepalive, guard),
        )

        config = (
            db.query(StrategyV2ShadowConfig)
            .filter(StrategyV2ShadowConfig.symbol == "AAPL.US")
            .one()
        )
        assert enabled == []
        assert disabled == []
        assert failures == ["enable:AAPL.US"]
        assert config.enabled is False
        assert config.universe_managed is False
    finally:
        db.close()
