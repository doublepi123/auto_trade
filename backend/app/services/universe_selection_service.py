from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Protocol, Sequence

from sqlalchemy import and_, or_, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.core.broker import BrokerCandle
from app.core.holiday_calendar import is_market_closed
from app.core.market_calendar import get_session
from app.domain.universe_selection import (
    CATALOG_SOURCE_VERSION,
    INDEX_CANDIDATE_CATALOG,
    UNIVERSE_ALGORITHM_VERSION,
    CandidateInput,
    CandidateSelection,
    DailyBar,
    IndexCandidate,
    UniverseSelectionConfig,
    completed_daily_bars,
    liquidity_spread_proxy_bps,
    latest_complete_session_date,
    select_candidates,
)
from app.models import (
    OrderRecord,
    StrategyConfig,
    StrategyV2ShadowConfig,
    TrackedEntry,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
    WatchlistItem,
    WatchlistScore,
)
from app.schemas import StrategyV2ShadowConfigUpdate
from app.services.strategy_v2_shadow_service import StrategyV2ShadowService

logger = logging.getLogger("auto_trade.universe_selection_service")

_WATCHLIST_SOURCE = "universe"
_DAILY_BAR_COUNT = 35
_LIVE_ORDER_STATUSES = ("SUBMITTED", "PARTIAL_FILLED")
_REFRESH_LOCK = threading.Lock()
_RUN_WAIT_POLL_SECONDS = 0.05
_RUN_CLAIM_LEASE_SECONDS = 300.0
_RUN_WAIT_TIMEOUT_SECONDS = _RUN_CLAIM_LEASE_SECONDS + 30.0
_CLAIM_PREFIX = "refresh-claim:"


class UniverseMarketDataProvider(Protocol):
    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[BrokerCandle]: ...


@dataclass(frozen=True)
class UniverseRefreshResult:
    run: UniverseSelectionRun
    items: tuple[UniverseSelectionCandidate, ...]
    added_symbols: tuple[str, ...] = ()
    removed_symbols: tuple[str, ...] = ()
    retained_symbols: tuple[str, ...] = ()
    shadow_enabled_symbols: tuple[str, ...] = ()
    shadow_disabled_symbols: tuple[str, ...] = ()
    shadow_failed_symbols: tuple[str, ...] = ()
    applied: bool = False
    reason: str = ""


@dataclass(frozen=True)
class _RunClaim:
    run_id: int
    token: str


def selection_config_from_settings() -> UniverseSelectionConfig:
    return UniverseSelectionConfig(
        max_selected=settings.universe_selection_max_symbols,
        max_per_sector=settings.universe_selection_max_per_sector,
        min_price=settings.universe_selection_min_price,
        min_avg_dollar_volume=(
            settings.universe_selection_min_avg_dollar_volume
        ),
        max_relative_spread_bps=settings.universe_selection_max_spread_bps,
        min_realized_vol_20d=settings.universe_selection_min_realized_vol,
        max_realized_vol_20d=settings.universe_selection_max_realized_vol,
        min_atr_pct_14d=settings.universe_selection_min_atr_pct,
        max_atr_pct_14d=settings.universe_selection_max_atr_pct,
    )


class UniverseSelectionService:
    def __init__(
        self,
        db: Session,
        broker: UniverseMarketDataProvider,
        *,
        catalog: Sequence[IndexCandidate] = INDEX_CANDIDATE_CATALOG,
        config: UniverseSelectionConfig | None = None,
        minimum_evaluable_ratio: float | None = None,
        minimum_residency_days: int | None = None,
        apply_to_watchlist: bool | None = None,
        enable_shadow: bool | None = None,
        now: datetime | None = None,
    ) -> None:
        if not catalog:
            raise ValueError("universe catalog must not be empty")
        self.db = db
        self.broker = broker
        self.catalog = tuple(catalog)
        self.config = config or selection_config_from_settings()
        self.minimum_evaluable_ratio = (
            settings.universe_selection_min_evaluable_ratio
            if minimum_evaluable_ratio is None
            else minimum_evaluable_ratio
        )
        self.minimum_residency_days = (
            settings.universe_selection_min_residency_days
            if minimum_residency_days is None
            else minimum_residency_days
        )
        if not 0 < self.minimum_evaluable_ratio <= 1:
            raise ValueError("minimum_evaluable_ratio must be in (0, 1]")
        if self.minimum_residency_days < 1:
            raise ValueError("minimum_residency_days must be positive")
        self.apply_to_watchlist = (
            settings.universe_selection_apply_to_watchlist
            if apply_to_watchlist is None
            else apply_to_watchlist
        )
        self.enable_shadow = (
            settings.universe_selection_enable_shadow
            if enable_shadow is None
            else enable_shadow
        )
        observed_at = now or datetime.now(timezone.utc)
        if observed_at.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        self.now = observed_at.astimezone(timezone.utc)

    def latest_run(self) -> UniverseSelectionRun | None:
        return (
            self.db.query(UniverseSelectionRun)
            .order_by(
                UniverseSelectionRun.as_of_date.desc(),
                UniverseSelectionRun.created_at.desc(),
                UniverseSelectionRun.id.desc(),
            )
            .first()
        )

    def items_for_run(
        self,
        run_id: int,
    ) -> list[UniverseSelectionCandidate]:
        return (
            self.db.query(UniverseSelectionCandidate)
            .filter(UniverseSelectionCandidate.run_id == run_id)
            .order_by(
                UniverseSelectionCandidate.selected.desc(),
                UniverseSelectionCandidate.rank.asc(),
                UniverseSelectionCandidate.score.desc(),
                UniverseSelectionCandidate.symbol.asc(),
            )
            .all()
        )

    def refresh(
        self,
        *,
        apply_to_watchlist: bool | None = None,
    ) -> UniverseRefreshResult:
        with _REFRESH_LOCK:
            return self._refresh_locked(
                apply_to_watchlist=apply_to_watchlist,
            )

    def _refresh_locked(
        self,
        *,
        apply_to_watchlist: bool | None,
    ) -> UniverseRefreshResult:
        should_apply = (
            self.apply_to_watchlist
            if apply_to_watchlist is None
            else apply_to_watchlist
        )
        parameters = self._parameters()
        algorithm_version = self._algorithm_version(parameters)
        expected_as_of_date = self._consensus_as_of_date({})
        existing = self._run_for_identity(
            as_of_date=expected_as_of_date,
            algorithm_version=algorithm_version,
        )
        if existing is not None and existing.status == "COMPLETE":
            items = self.items_for_run(existing.id)
            return self._result_for_existing(
                existing,
                items,
                should_apply=should_apply,
            )

        claim = self._claim_run(
            as_of_date=expected_as_of_date,
            algorithm_version=algorithm_version,
            parameters=parameters,
        )
        if claim is None:
            resolution = self._wait_for_winner(
                as_of_date=expected_as_of_date,
                algorithm_version=algorithm_version,
                parameters=parameters,
            )
            if isinstance(resolution, _RunClaim):
                claim = resolution
            else:
                winner, items = resolution
                return self._result_for_existing(
                    winner,
                    items,
                    should_apply=should_apply,
                )

        try:
            selections, as_of_date = self._evaluate_catalog(
                expected_as_of_date=expected_as_of_date,
            )
            evaluable_count = sum(item.evaluable for item in selections)
            selected_count = sum(item.selected for item in selections)
            candidate_count = len(selections)
            coverage_ratio = (
                evaluable_count / candidate_count
                if candidate_count
                else 0.0
            )
            errors: list[str] = []
            if coverage_ratio < self.minimum_evaluable_ratio:
                errors.append(
                    "evaluable coverage below minimum: "
                    f"{coverage_ratio:.1%} < "
                    f"{self.minimum_evaluable_ratio:.1%}"
                )
            if selected_count == 0:
                errors.append("no eligible candidates selected")
            status = "DEGRADED" if errors else "COMPLETE"

            published = self._publish_claim(
                claim,
                selections=selections,
                status=status,
                candidate_count=candidate_count,
                evaluable_count=evaluable_count,
                selected_count=selected_count,
                coverage_ratio=coverage_ratio,
                parameters=parameters,
                error="; ".join(errors),
            )
        except Exception as exc:
            self._release_failed_claim(claim, exc)
            raise
        if published is None:
            resolution = self._wait_for_winner(
                as_of_date=as_of_date,
                algorithm_version=algorithm_version,
                parameters=parameters,
            )
            if isinstance(resolution, _RunClaim):
                # The original owner lost its lease and the intervening owner
                # also disappeared. This caller already has complete T-1
                # evidence, so publish it under the newly acquired token.
                published = self._publish_claim(
                    resolution,
                    selections=selections,
                    status=status,
                    candidate_count=candidate_count,
                    evaluable_count=evaluable_count,
                    selected_count=selected_count,
                    coverage_ratio=coverage_ratio,
                    parameters=parameters,
                    error="; ".join(errors),
                )
                if published is None:
                    raise RuntimeError(
                        "universe selection takeover claim was lost"
                    )
            else:
                winner, items = resolution
                return self._result_for_existing(
                    winner,
                    items,
                    should_apply=should_apply,
                )
        run, rows = published

        if status != "COMPLETE":
            return UniverseRefreshResult(
                run=run,
                items=tuple(rows),
                reason=run.error,
            )
        return self._result_for_existing(
            run,
            rows,
            should_apply=should_apply,
        )

    def _run_for_identity(
        self,
        *,
        as_of_date: date,
        algorithm_version: str,
    ) -> UniverseSelectionRun | None:
        return (
            self.db.query(UniverseSelectionRun)
            .filter(
                UniverseSelectionRun.as_of_date == as_of_date,
                UniverseSelectionRun.algorithm_version
                == algorithm_version,
                UniverseSelectionRun.source_version
                == CATALOG_SOURCE_VERSION,
            )
            .first()
        )

    def _claim_run(
        self,
        *,
        as_of_date: date,
        algorithm_version: str,
        parameters: dict[str, object],
    ) -> _RunClaim | None:
        # A preceding identity read may hold a WAL snapshot. End it before
        # the atomic UPSERT so SQLite never has to upgrade a stale reader into
        # the single writer (which can fail with SQLITE_BUSY_SNAPSHOT).
        self.db.rollback()
        token = f"{_CLAIM_PREFIX}{uuid.uuid4().hex}"
        parameters_json = json.dumps(
            parameters,
            sort_keys=True,
            separators=(",", ":"),
        )
        claim_started_at = datetime.now(timezone.utc)
        stale_before = claim_started_at - timedelta(
            seconds=_RUN_CLAIM_LEASE_SECONDS,
        )
        insert = sqlite_insert(UniverseSelectionRun).values(
            as_of_date=as_of_date,
            algorithm_version=algorithm_version,
            source_version=CATALOG_SOURCE_VERSION,
            status="RUNNING",
            candidate_count=0,
            evaluable_count=0,
            selected_count=0,
            coverage_ratio=0.0,
            parameters_json=parameters_json,
            error=token,
            started_at=claim_started_at,
            completed_at=None,
            created_at=self.now,
        )
        claimed_id = self.db.execute(
            insert.on_conflict_do_update(
                index_elements=[
                    UniverseSelectionRun.as_of_date,
                    UniverseSelectionRun.algorithm_version,
                    UniverseSelectionRun.source_version,
                ],
                set_={
                    "status": "RUNNING",
                    "parameters_json": parameters_json,
                    "error": token,
                    "started_at": claim_started_at,
                    "completed_at": None,
                },
                where=or_(
                    UniverseSelectionRun.status == "DEGRADED",
                    and_(
                        UniverseSelectionRun.status == "RUNNING",
                        UniverseSelectionRun.started_at < stale_before,
                    ),
                ),
            ).returning(UniverseSelectionRun.id)
        ).scalar_one_or_none()
        # Releasing the SQLite writer lock here lets the market-data fetch run
        # without blocking unrelated persistence. The opaque token protects
        # the later publication transaction from a stale owner.
        self.db.commit()
        if claimed_id is None:
            return None
        return _RunClaim(run_id=claimed_id, token=token)

    def _wait_for_winner(
        self,
        *,
        as_of_date: date,
        algorithm_version: str,
        parameters: dict[str, object],
    ) -> (
        tuple[
            UniverseSelectionRun,
            list[UniverseSelectionCandidate],
        ]
        | _RunClaim
    ):
        deadline = time.monotonic() + _RUN_WAIT_TIMEOUT_SECONDS
        while True:
            self.db.rollback()
            self.db.expire_all()
            run = self._run_for_identity(
                as_of_date=as_of_date,
                algorithm_version=algorithm_version,
            )
            if run is not None and run.status != "RUNNING":
                return run, self.items_for_run(run.id)
            if run is not None and self._claim_is_stale(run):
                takeover = self._claim_run(
                    as_of_date=as_of_date,
                    algorithm_version=algorithm_version,
                    parameters=parameters,
                )
                if takeover is not None:
                    return takeover
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "timed out waiting for universe selection refresh"
                )
            time.sleep(_RUN_WAIT_POLL_SECONDS)

    @staticmethod
    def _claim_is_stale(run: UniverseSelectionRun) -> bool:
        started_at = run.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        age_seconds = (
            datetime.now(timezone.utc) - started_at
        ).total_seconds()
        return age_seconds >= _RUN_CLAIM_LEASE_SECONDS

    def _publish_claim(
        self,
        claim: _RunClaim,
        *,
        selections: Sequence[CandidateSelection],
        status: str,
        candidate_count: int,
        evaluable_count: int,
        selected_count: int,
        coverage_ratio: float,
        parameters: dict[str, object],
        error: str,
    ) -> tuple[
        UniverseSelectionRun,
        list[UniverseSelectionCandidate],
    ] | None:
        claimed_id = self.db.execute(
            update(UniverseSelectionRun)
            .where(
                UniverseSelectionRun.id == claim.run_id,
                UniverseSelectionRun.status == "RUNNING",
                UniverseSelectionRun.error == claim.token,
            )
            .values(
                status=status,
                candidate_count=candidate_count,
                evaluable_count=evaluable_count,
                selected_count=selected_count,
                coverage_ratio=coverage_ratio,
                parameters_json=json.dumps(
                    parameters,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                error=error,
                completed_at=self.now,
            )
            .execution_options(synchronize_session=False)
            .returning(UniverseSelectionRun.id)
        ).scalar_one_or_none()
        if claimed_id != claim.run_id:
            self.db.rollback()
            return None

        # Run metadata and its complete candidate evidence are published in
        # the same SQLite write transaction, so readers never observe a
        # terminal winner paired with another attempt's rows.
        self.db.query(UniverseSelectionCandidate).filter(
            UniverseSelectionCandidate.run_id == claim.run_id,
        ).delete(synchronize_session="fetch")
        rows = [
            self._candidate_row(claim.run_id, selection)
            for selection in selections
        ]
        self.db.add_all(rows)
        self.db.commit()
        run = self.db.get(UniverseSelectionRun, claim.run_id)
        if run is None:
            raise RuntimeError("published universe selection run disappeared")
        return run, self.items_for_run(claim.run_id)

    def _release_failed_claim(
        self,
        claim: _RunClaim,
        exc: Exception,
    ) -> None:
        self.db.rollback()
        try:
            self.db.execute(
                update(UniverseSelectionRun)
                .where(
                    UniverseSelectionRun.id == claim.run_id,
                    UniverseSelectionRun.status == "RUNNING",
                    UniverseSelectionRun.error == claim.token,
                )
                .values(
                    status="DEGRADED",
                    error=(
                        "refresh failed before publication: "
                        f"{type(exc).__name__}"
                    ),
                    completed_at=datetime.now(timezone.utc),
                )
                .execution_options(synchronize_session=False)
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            logger.exception(
                "failed to release universe selection claim %s",
                claim.run_id,
            )

    def _evaluate_catalog(
        self,
        *,
        expected_as_of_date: date,
    ) -> tuple[list[CandidateSelection], date]:
        complete_by_symbol: dict[str, Sequence[DailyBar]] = {}
        spread_by_symbol: dict[str, float] = {}
        latest_by_symbol: dict[str, date] = {}
        errors_by_symbol: dict[str, list[str]] = {}
        for candidate in self.catalog:
            data_errors: list[str] = []
            try:
                raw_bars = self.broker.get_candlesticks(
                    candidate.symbol,
                    "DAY",
                    _DAILY_BAR_COUNT,
                )
                bars = completed_daily_bars(
                    raw_bars,
                    market=candidate.market,
                    now=self.now,
                )
                latest = latest_complete_session_date(
                    raw_bars,
                    market=candidate.market,
                    now=self.now,
                )
                if latest is None:
                    data_errors.append("DATA_NO_COMPLETED_DAILY_BAR")
                else:
                    latest_by_symbol[candidate.symbol] = latest
                spread_proxy = liquidity_spread_proxy_bps(bars)
                if spread_proxy is None:
                    data_errors.append("DATA_INVALID_SPREAD_PROXY")
                else:
                    spread_by_symbol[candidate.symbol] = spread_proxy
            except Exception as exc:
                bars = []
                data_errors.append(
                    f"DATA_DAILY_BARS_{type(exc).__name__.upper()}"
                )
                logger.warning(
                    "universe daily bars failed for %s: %s",
                    candidate.symbol,
                    exc,
                    exc_info=True,
                )
            complete_by_symbol[candidate.symbol] = bars
            errors_by_symbol[candidate.symbol] = data_errors

        inputs: list[CandidateInput] = []
        for candidate in self.catalog:
            data_errors = list(
                errors_by_symbol.get(candidate.symbol, ())
            )
            latest = latest_by_symbol.get(candidate.symbol)
            if latest is not None and latest != expected_as_of_date:
                data_errors.append("DATA_STALE_SESSION_DATE")
            inputs.append(
                CandidateInput(
                    candidate=candidate,
                    completed_daily_bars=complete_by_symbol.get(
                        candidate.symbol,
                        [],
                    ),
                    bid=None,
                    ask=None,
                    estimated_spread_bps=spread_by_symbol.get(
                        candidate.symbol,
                    ),
                    data_errors=tuple(data_errors),
                )
            )
        return select_candidates(inputs, self.config), expected_as_of_date

    def _consensus_as_of_date(
        self,
        latest_by_symbol: dict[str, date],
    ) -> date:
        if latest_by_symbol:
            counts = Counter(latest_by_symbol.values())
            return max(counts, key=lambda value: (counts[value], value))
        session = get_session("US")
        candidate = session.local(self.now).date() - timedelta(days=1)
        for _ in range(14):
            if (
                candidate.weekday() < 5
                and not is_market_closed("US", candidate)
            ):
                return candidate
            candidate -= timedelta(days=1)
        return candidate

    def _candidate_row(
        self,
        run_id: int,
        selection: CandidateSelection,
        *,
        row: UniverseSelectionCandidate | None = None,
    ) -> UniverseSelectionCandidate:
        candidate_row = row or UniverseSelectionCandidate()
        candidate_row.run_id = run_id
        candidate_row.symbol = selection.candidate.symbol
        candidate_row.market = selection.candidate.market
        candidate_row.alias = selection.candidate.alias
        candidate_row.sector = selection.candidate.sector
        candidate_row.memberships_json = json.dumps(
            selection.candidate.memberships,
            separators=(",", ":"),
        )
        candidate_row.selected = selection.selected
        candidate_row.rank = selection.rank
        candidate_row.score = round(selection.score, 6)
        candidate_row.metrics_json = json.dumps(
            asdict(selection.metrics),
            sort_keys=True,
            separators=(",", ":"),
        )
        candidate_row.exclusion_reasons_json = json.dumps(
            selection.exclusion_reasons,
            separators=(",", ":"),
        )
        candidate_row.created_at = self.now
        return candidate_row

    def _result_for_existing(
        self,
        run: UniverseSelectionRun,
        items: Sequence[UniverseSelectionCandidate],
        *,
        should_apply: bool,
    ) -> UniverseRefreshResult:
        if not should_apply:
            return UniverseRefreshResult(
                run=run,
                items=tuple(items),
                reason="watchlist application disabled",
            )
        if run.status != "COMPLETE":
            return UniverseRefreshResult(
                run=run,
                items=tuple(items),
                reason=run.error or "selection run is not complete",
            )
        selected = [item for item in items if item.selected]
        added, removed, retained = self._reconcile_watchlist(selected)
        shadow_enabled, shadow_disabled, shadow_failures = (
            self._sync_selected_shadows(
                selected_symbols={item.symbol for item in selected},
            )
        )
        reason = "candidate watchlist reconciled"
        if shadow_failures:
            reason += "; shadow sync failed for " + ", ".join(
                shadow_failures
            )
        return UniverseRefreshResult(
            run=run,
            items=tuple(items),
            added_symbols=tuple(added),
            removed_symbols=tuple(removed),
            retained_symbols=tuple(retained),
            shadow_enabled_symbols=tuple(shadow_enabled),
            shadow_disabled_symbols=tuple(shadow_disabled),
            shadow_failed_symbols=tuple(shadow_failures),
            applied=True,
            reason=reason,
        )

    def _reconcile_watchlist(
        self,
        selected: Sequence[UniverseSelectionCandidate],
    ) -> tuple[list[str], list[str], list[str]]:
        existing_rows = self.db.query(WatchlistItem).all()
        existing = {row.symbol: row for row in existing_rows}
        primary = (
            self.db.query(StrategyConfig)
            .order_by(StrategyConfig.id.desc())
            .first()
        )
        primary_symbol = primary.symbol if primary is not None else ""
        selected_symbols = {item.symbol for item in selected}
        added: list[str] = []
        retained: list[str] = []
        for candidate in selected:
            row = existing.get(candidate.symbol)
            if row is None:
                inserted_id = self.db.execute(
                    sqlite_insert(WatchlistItem)
                    .values(
                        symbol=candidate.symbol,
                        market=candidate.market,
                        alias=candidate.alias,
                        source=_WATCHLIST_SOURCE,
                        is_active=candidate.symbol == primary_symbol,
                        created_at=self.now,
                    )
                    .on_conflict_do_nothing(index_elements=["symbol"])
                    .returning(WatchlistItem.id)
                ).scalar_one_or_none()
                row = (
                    self.db.query(WatchlistItem)
                    .filter(WatchlistItem.symbol == candidate.symbol)
                    .one()
                )
                existing[candidate.symbol] = row
                if inserted_id is not None:
                    added.append(candidate.symbol)
                else:
                    retained.append(candidate.symbol)
            else:
                retained.append(candidate.symbol)
            row.is_active = candidate.symbol == primary_symbol
            if row.source == _WATCHLIST_SOURCE:
                row.market = candidate.market
                row.alias = candidate.alias

        residency_cutoff = self.now - timedelta(
            days=self.minimum_residency_days
        )
        removed: list[str] = []
        for row in existing_rows:
            row.is_active = row.symbol == primary_symbol
            if row.source != _WATCHLIST_SOURCE:
                continue
            if row.symbol in selected_symbols:
                continue
            created_at = row.created_at
            if created_at is None:
                created_at = self.now
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if (
                row.is_active
                or row.symbol == primary_symbol
                or created_at > residency_cutoff
                or self._has_live_exposure(row.symbol)
            ):
                retained.append(row.symbol)
                continue
            removed.append(row.symbol)
            self.db.query(WatchlistScore).filter(
                WatchlistScore.symbol == row.symbol,
            ).delete(synchronize_session=False)
            self.db.delete(row)
        self.db.commit()
        return (
            sorted(added),
            sorted(removed),
            sorted(set(retained)),
        )

    def _has_live_exposure(self, symbol: str) -> bool:
        tracked = (
            self.db.query(TrackedEntry)
            .filter(
                TrackedEntry.symbol == symbol,
                TrackedEntry.quantity > 0,
            )
            .first()
        )
        if tracked is not None:
            return True
        live_order = (
            self.db.query(OrderRecord)
            .filter(
                OrderRecord.symbol == symbol,
                OrderRecord.status.in_(_LIVE_ORDER_STATUSES),
            )
            .first()
        )
        return live_order is not None

    def _sync_selected_shadows(
        self,
        *,
        selected_symbols: set[str],
    ) -> tuple[list[str], list[str], list[str]]:
        if not self.enable_shadow:
            return [], [], []
        enabled: list[str] = []
        disabled: list[str] = []
        failures: list[str] = []
        service = StrategyV2ShadowService(self.db)
        for symbol in sorted(selected_symbols):
            try:
                row = (
                    self.db.query(StrategyV2ShadowConfig)
                    .filter(StrategyV2ShadowConfig.symbol == symbol)
                    .first()
                )
                created_for_universe = row is None
                if row is None:
                    service.get_config(symbol)
                    row = (
                        self.db.query(StrategyV2ShadowConfig)
                        .filter(StrategyV2ShadowConfig.symbol == symbol)
                        .one()
                    )
                if row.enabled:
                    continue
                if not created_for_universe and not row.universe_managed:
                    # Existing disabled unmanaged configs are explicit
                    # operator opt-outs. Never silently re-enable them.
                    continue
                row.universe_managed = True
                self.db.add(row)
                service.update_config(
                    StrategyV2ShadowConfigUpdate(enabled=True),
                    symbol=symbol,
                    preserve_universe_management=True,
                )
                enabled.append(symbol)
            except Exception:
                self.db.rollback()
                logger.exception(
                    "failed to enable Strategy v2 shadow for %s",
                    symbol,
                )
                failures.append(f"enable:{symbol}")
        managed_rows = (
            self.db.query(StrategyV2ShadowConfig)
            .filter(StrategyV2ShadowConfig.universe_managed.is_(True))
            .all()
        )
        for managed in managed_rows:
            symbol = managed.symbol
            if symbol in selected_symbols:
                continue
            try:
                if managed.enabled:
                    service.update_config(
                        StrategyV2ShadowConfigUpdate(enabled=False),
                        symbol=symbol,
                        preserve_universe_management=True,
                    )
                    disabled.append(symbol)
            except Exception:
                self.db.rollback()
                logger.exception(
                    "failed to disable retired Strategy v2 shadow for %s",
                    symbol,
                )
                failures.append(f"disable:{symbol}")
        return enabled, disabled, failures

    def _parameters(self) -> dict[str, object]:
        return {
            **asdict(self.config),
            "catalog_size": len(self.catalog),
            "minimum_evaluable_ratio": self.minimum_evaluable_ratio,
            "minimum_residency_days": self.minimum_residency_days,
        }

    @staticmethod
    def _algorithm_version(parameters: dict[str, object]) -> str:
        encoded = json.dumps(
            parameters,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        digest = hashlib.sha256(encoded).hexdigest()[:12]
        return f"{UNIVERSE_ALGORITHM_VERSION}-{digest}"
