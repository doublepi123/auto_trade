from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal, Protocol, Sequence

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.fees import one_side_fee_rate
from app.core.market_calendar import (
    get_session,
    is_trading_hours,
    next_session_open,
    session_status,
)
from app.domain.strategy_v2 import (
    CausalTrendPrewarmFeatureEngine,
    StrategyBar,
    StrategyV2Action,
    StrategyV2Config,
    StrategyV2Decision,
    StrategyV2Engine,
    StrategyV2State,
    VirtualPosition,
    aggregate_complete_five_minute_bars,
    minimum_profit_target_pct,
)
from app.platform.strategy_quality import strategy_quality_report
from app.models import (
    StrategyConfig,
    StrategyV2ForwardEvidence,
    StrategyV2ForwardRegistration,
    StrategyV2ShadowConfig,
    StrategyV2ShadowDecision,
    StrategyV2ShadowState,
    StrategyV2ShadowTrade,
    StrategyV2ShadowVersion,
)
from app.schemas import (
    StrategyV2AdxChallengerDaily,
    StrategyV2AdxChallengerRequest,
    StrategyV2AdxChallengerResponse,
    StrategyV2AdxChallengerResult,
    StrategyV2ForwardDailyEvidence,
    StrategyV2ForwardRegistrationRequest,
    StrategyV2ForwardRegistrationResponse,
    StrategyV2ForwardValidationResponse,
    StrategyV2ShadowConfigResponse,
    StrategyV2ShadowConfigUpdate,
    StrategyV2ShadowConfigValues,
    StrategyV2ShadowDecisionPage,
    StrategyV2ShadowDecisionResponse,
    StrategyV2ShadowDailyEvidence,
    StrategyV2ShadowEvaluationResponse,
    StrategyV2ShadowHourlyEvidence,
    StrategyV2ShadowLatestResponse,
    StrategyV2ShadowMetrics,
    StrategyV2ReplayBar,
    StrategyV2ShadowReplayRequest,
    StrategyV2ShadowReplayResponse,
    StrategyV2ShadowStatusResponse,
    StrategyV2ShadowTradeResponse,
    StrategyV2ShadowVersionResponse,
    StrategyV2WarmupDaily,
    StrategyV2WarmupDiagnostic,
    StrategyV2WarmupVariant,
)
from app.services.strategy_service import StrategyService


class CandleProvider(Protocol):
    def get_candlesticks(self, symbol: str, period: str, count: int) -> list[Any]: ...


_CONFIG_FIELDS = (
    "enabled",
    "symbol",
    "zscore_window_1m_bars",
    "zscore_window_5m_bars",
    "breach_zscore",
    "reclaim_zscore",
    "five_minute_zscore_max",
    "adx_period",
    "max_adx",
    "realized_vol_window_bars",
    "min_realized_vol",
    "max_realized_vol",
    "stop_loss_pct",
    "profit_target_pct",
    "max_holding_minutes",
    "entry_cutoff_minutes_before_close",
    "flatten_minutes_before_close",
    "arm_ttl_bars",
    "max_entries_per_day",
    "entry_cooldown_minutes",
    "slippage_bps",
    "estimated_fee_rate_us",
    "estimated_fee_rate_hk",
)
_ALGORITHM_VERSION = "strategy-v2-rth-mr-v4-frozen-config"
_VALID_ACTIONS = frozenset(action.value for action in StrategyV2Action)
_SHADOW_SYMBOL_RE = re.compile(r"^[A-Z0-9\-]{1,12}\.(US|HK)$")
_MIN_POLL_SECONDS = 45.0
_ONE_MINUTE_CANDLE_COUNT = 500
_POST_CLOSE_COLLECTION_MINUTES = 15
_MIN_REVIEW_TRADING_DAYS = 20
_MIN_REVIEW_CLOSED_TRADES = 50
_MIN_COMPLETE_SESSION_COVERAGE = 0.995
_ADX_CHALLENGER_VALUES = (20.0, 25.0, 30.0)
_MIN_CHALLENGER_COMPLETE_SESSIONS = 5
_MAX_CHALLENGER_COMPLETE_SESSIONS = 20
_MIN_WARMUP_CAUSAL_PAIRS = 5
_FORWARD_CANDIDATE_VERSION = "strategy-v2-causal-trend-prewarm-v1"
_FORWARD_EVALUATOR_VERSION = "strategy-v2-forward-evaluator-v2"
_FORWARD_READY_PAIRS = 5
_FORWARD_MATURE_PAIRS = 20
_FORWARD_FINALIZE_START_MINUTES = 10
_FORWARD_INCOMPLETE_DEADLINE_MINUTES = 14
_FEATURE_NOT_READY_REASONS = frozenset({
    "FEATURES_NOT_READY",
    "SESSION_DATA_INCOMPLETE",
    "NON_POSITIVE_VOLUME",
    "VWAP_1M_UNAVAILABLE",
    "RESIDUAL_SIGMA_1M_ZERO",
    "ZSCORE_1M_WARMUP",
    "VWAP_5M_UNAVAILABLE",
    "RESIDUAL_SIGMA_5M_ZERO",
    "ZSCORE_5M_WARMUP",
    "ADX_5M_WARMUP",
    "REALIZED_VOL_1M_WARMUP",
})
_ENTRY_GATE_REASONS = _FEATURE_NOT_READY_REASONS | frozenset({
    "RESIDUAL_SIGMA_1M_TOO_LOW",
    "RESIDUAL_SIGMA_5M_TOO_LOW",
    "ZSCORE_5M_NOT_OVERSOLD",
    "ADX_REGIME_BLOCKED",
    "REALIZED_VOL_REGIME_BLOCKED",
    "ENTRY_CUTOFF",
    "MAX_SESSION_ENTRIES",
    "ENTRY_COOLDOWN",
})


@dataclass(frozen=True)
class _DecisionEvidenceRow:
    session_date: date
    market: str
    bar_at: datetime
    gate_passed: bool
    gate_reasons_json: str


class StrategyV2ShadowService:
    """Persist and evaluate the P2 strategy without any order capability.

    The constructor accepts only a candle reader.  There is intentionally no
    execution client, order callback, or dependency on ``TradeExecutionService``.
    """

    def __init__(self, db: Session, candle_provider: CandleProvider | None = None) -> None:
        self.db = db
        self.candle_provider = candle_provider

    def get_config(self, symbol: str | None = None) -> StrategyV2ShadowConfigResponse:
        row = self._get_or_create_config(symbol)
        self._ensure_version_snapshot(row)
        return self._config_response(row)

    def list_configs(self) -> list[StrategyV2ShadowConfigResponse]:
        rows = self.db.query(StrategyV2ShadowConfig).all()
        try:
            self._get_or_create_config()
        except ValueError:
            if not rows:
                raise
        rows = self.db.query(StrategyV2ShadowConfig).order_by(
            StrategyV2ShadowConfig.symbol.asc()
        ).all()
        for row in rows:
            self._ensure_version_snapshot(row, commit=False)
        self.db.commit()
        return [self._config_response(row) for row in rows]

    def list_versions(self, symbol: str | None = None) -> list[StrategyV2ShadowVersionResponse]:
        config = self._get_or_create_config(symbol)
        self._ensure_version_snapshot(config)
        self._backfill_legacy_version_snapshots(config.symbol)
        current_version = self._config_version(config)
        rows = self.db.query(StrategyV2ShadowVersion).filter(
            StrategyV2ShadowVersion.symbol == config.symbol
        ).order_by(StrategyV2ShadowVersion.activated_at.desc()).all()
        return [self._version_response(row, current_version) for row in rows]

    def get_evaluation(
        self,
        symbol: str | None = None,
        config_version: str | None = None,
    ) -> StrategyV2ShadowEvaluationResponse:
        normalized = self._resolve_symbol(symbol)
        version = self._resolve_config_version(normalized, config_version)
        decisions = self.db.query(StrategyV2ShadowDecision).filter(
            StrategyV2ShadowDecision.symbol == normalized,
            StrategyV2ShadowDecision.config_version == version,
        ).order_by(StrategyV2ShadowDecision.bar_at.asc()).all()
        trades = self.db.query(StrategyV2ShadowTrade).filter(
            StrategyV2ShadowTrade.symbol == normalized,
            StrategyV2ShadowTrade.config_version == version,
            StrategyV2ShadowTrade.status == "CLOSED",
        ).order_by(StrategyV2ShadowTrade.exit_at.asc()).all()
        daily = self._daily_evidence(decisions, trades)
        observed_days = sum(item.complete_session for item in daily)
        complete_dates = {
            item.session_date for item in daily if item.complete_session
        }
        decisions_by_id = {item.id: item for item in decisions}
        entry_linkage_counts = Counter(
            item.entry_decision_id
            for item in trades
            if item.entry_decision_id is not None
        )
        exit_linkage_counts = Counter(
            item.exit_decision_id
            for item in trades
            if item.exit_decision_id is not None
        )

        def has_unique_linkage(item: StrategyV2ShadowTrade) -> bool:
            entry_id = item.entry_decision_id
            exit_id = item.exit_decision_id
            return (
                entry_id is not None
                and exit_id is not None
                and entry_linkage_counts[entry_id] == 1
                and exit_linkage_counts[exit_id] == 1
            )

        linked_trade_days = {
            item.id: (
                self._trade_evidence_session_date(item, decisions_by_id)
                if has_unique_linkage(item)
                else None
            )
            for item in trades
        }
        eligible_trades = [
            item
            for item in trades
            if linked_trade_days[item.id] in complete_dates
        ]
        closed_trades = len(trades)
        eligible_closed_trades = len(eligible_trades)
        excluded_closed_trades = closed_trades - eligible_closed_trades
        invalid_trade_evidence = sum(
            session_date is None for session_date in linked_trade_days.values()
        )
        blockers: list[str] = []
        if observed_days < _MIN_REVIEW_TRADING_DAYS:
            blockers.append("MIN_TRADING_DAYS")
        if eligible_closed_trades < _MIN_REVIEW_CLOSED_TRADES:
            blockers.append("MIN_CLOSED_TRADES")
        if invalid_trade_evidence:
            blockers.append("DATA_TRADE_EVIDENCE_INVALID")
        if excluded_closed_trades:
            blockers.append("DATA_TRADE_SESSION_INCOMPLETE")

        params = self._version_params(normalized, version)
        edge_blocker = self._net_edge_blocker(normalized, params)
        if edge_blocker is not None:
            blockers.append(edge_blocker)
        quality, quality_blockers = self._readiness_quality(
            eligible_trades,
            params,
        )
        if eligible_closed_trades >= _MIN_REVIEW_CLOSED_TRADES:
            blockers.extend(quality_blockers)

        warnings: list[str] = []
        for item in daily:
            issues: list[str] = []
            if item.missing_internal_bars:
                issues.append(f"{item.missing_internal_bars} internal bars missing")
            if item.incomplete_feature_bars:
                issues.append(
                    f"{item.incomplete_feature_bars} feature bars incomplete"
                )
            if item.partial_start or item.partial_end:
                issues.append("partial session boundary")
            if item.coverage_ratio < _MIN_COMPLETE_SESSION_COVERAGE:
                issues.append(f"coverage {item.coverage_ratio:.3%}")
            if item.outside_session_bars:
                issues.append(f"{item.outside_session_bars} outside-session bars")
            if issues:
                warnings.append(
                    f"{item.session_date.isoformat()}: " + "; ".join(issues)
                )
        if invalid_trade_evidence:
            warnings.append(
                f"{invalid_trade_evidence} closed trades have invalid decision linkage"
            )
        if excluded_closed_trades:
            warnings.append(
                f"{excluded_closed_trades} closed trades excluded from complete-session evidence"
            )
        blockers = list(dict.fromkeys(blockers))
        return StrategyV2ShadowEvaluationResponse(
            symbol=normalized,
            config_version=version,
            status="READY_FOR_REVIEW" if not blockers else "COLLECTING",
            observed_trading_days=observed_days,
            excluded_trading_days=len(daily) - observed_days,
            remaining_trading_days=max(
                0,
                _MIN_REVIEW_TRADING_DAYS - observed_days,
            ),
            closed_trades=closed_trades,
            eligible_closed_trades=eligible_closed_trades,
            excluded_closed_trades=excluded_closed_trades,
            remaining_closed_trades=max(
                0,
                _MIN_REVIEW_CLOSED_TRADES - eligible_closed_trades,
            ),
            first_bar_at=decisions[0].bar_at if decisions else None,
            last_bar_at=decisions[-1].bar_at if decisions else None,
            bars=len({item.bar_at for item in decisions}),
            readiness_blockers=blockers,
            data_quality_warnings=warnings,
            quality=quality,
            daily=daily,
        )

    def update_config(
        self,
        payload: StrategyV2ShadowConfigUpdate,
        *,
        symbol: str | None = None,
        preserve_universe_management: bool = False,
    ) -> StrategyV2ShadowConfigResponse:
        row = self._get_or_create_config(symbol)
        self._ensure_version_snapshot(row)
        updates = payload.model_dump(exclude_unset=True, exclude_none=True)
        tunable_updates = set(updates) - {"enabled"}
        open_trade = self._open_trade(row.symbol)
        if open_trade is not None and tunable_updates:
            raise ValueError("strategy v2 shadow config cannot change while a virtual trade is open")
        if not updates:
            return self._config_response(row)
        ownership_changed = False
        if (
            "enabled" in updates
            and not preserve_universe_management
            and row.universe_managed
        ):
            # An explicit operator toggle takes ownership away from the
            # universe reconciler. In particular, a manual disable must not
            # be undone by the next selection refresh.
            row.universe_managed = False
            ownership_changed = True

        merged = self._config_values(row)
        merged.update(updates)
        validated = StrategyV2ShadowConfigValues.model_validate(merged)
        if tunable_updates or validated.enabled:
            self._validate_minimum_net_edge(validated.model_dump())

        was_enabled = row.enabled
        changed = ownership_changed or any(
            getattr(row, field) != value
            for field, value in updates.items()
        )
        if not changed:
            return self._config_response(row)
        now = datetime.now(timezone.utc)
        for field in _CONFIG_FIELDS:
            setattr(row, field, getattr(validated, field))
        row.updated_at = now
        self.db.add(row)
        self.db.flush()

        # Disabling is an operational control, not a strategy revision.  Keep
        # an open virtual position and its engine snapshot intact so the cron
        # can still execute deterministic protective exits.  Enabling from a
        # flat state and every tunable edit start strictly forward from now.
        reset_for_forward_run = bool(tunable_updates) or (
            not was_enabled and row.enabled and open_trade is None
        )
        if reset_for_forward_run:
            state = self._get_or_create_state(row.symbol, commit=False)
            state.phase = StrategyV2State.COLD.value
            market = "HK" if row.symbol.endswith(".HK") else "US"
            state.last_bar_at = self._forward_watermark(market, now)
            state.armed_at = None
            state.armed_zscore = None
            state.open_trade_id = None
            state.state_json = "{}"
            state.last_polled_at = None
            state.last_poll_error = ""
            state.config_version = self._config_version(row)
            self.db.add(state)
        self.db.commit()
        self.db.refresh(row)
        self._ensure_version_snapshot(row)
        return self._config_response(row)

    def get_status(self, symbol: str | None = None) -> StrategyV2ShadowStatusResponse:
        config_row = self._get_or_create_config(symbol)
        self._ensure_version_snapshot(config_row)
        config_version = self._config_version(config_row)
        open_trade = self._open_trade(config_row.symbol)
        active_version = (
            open_trade.config_version if open_trade is not None else config_version
        )
        latest_row = (
            self.db.query(StrategyV2ShadowDecision)
            .filter(
                StrategyV2ShadowDecision.symbol == config_row.symbol,
                StrategyV2ShadowDecision.config_version == active_version,
            )
            .order_by(StrategyV2ShadowDecision.bar_at.desc(), StrategyV2ShadowDecision.id.desc())
            .first()
        )
        state = self.db.query(StrategyV2ShadowState).filter(
            StrategyV2ShadowState.symbol == config_row.symbol
        ).first()
        state_version_mismatch = (
            state is not None and state.config_version != config_version
        )
        return StrategyV2ShadowStatusResponse(
            config=self._config_response(config_row),
            evidence_config_version=active_version,
            version_transition_pending=(
                active_version != config_version or state_version_mismatch
            ),
            latest=(
                self._latest_response(latest_row, open_trade)
                if latest_row is not None
                else None
            ),
            metrics=self._metrics(config_row.symbol, active_version),
            gate_counts=self._gate_counts(config_row.symbol, active_version),
            phase=(
                "DISABLED"
                if not config_row.enabled and open_trade is None
                else StrategyV2State.COLD.value
                if open_trade is None and state_version_mismatch
                else state.phase if state is not None else StrategyV2State.COLD.value
            ),
            last_polled_at=state.last_polled_at if state is not None else None,
            last_poll_error=state.last_poll_error if state is not None else "",
        )

    def list_decisions(
        self,
        *,
        symbol: str | None = None,
        page: int = 1,
        page_size: int = 50,
        action: str | None = None,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        config_version: str | None = None,
    ) -> StrategyV2ShadowDecisionPage:
        normalized = self._resolve_symbol(symbol)
        active_version = self._resolve_config_version(normalized, config_version)
        query = self.db.query(StrategyV2ShadowDecision).filter(
            StrategyV2ShadowDecision.symbol == normalized,
            StrategyV2ShadowDecision.config_version == active_version,
        )
        if action:
            normalized_action = action.strip().upper()
            if normalized_action not in _VALID_ACTIONS:
                raise ValueError(f"unsupported strategy v2 shadow action: {action}")
            query = query.filter(StrategyV2ShadowDecision.action == normalized_action)
        if from_dt is not None and to_dt is not None and _as_utc(from_dt) > _as_utc(to_dt):
            raise ValueError("from must not be later than to")
        if from_dt is not None:
            query = query.filter(StrategyV2ShadowDecision.bar_at >= _as_utc(from_dt))
        if to_dt is not None:
            query = query.filter(StrategyV2ShadowDecision.bar_at <= _as_utc(to_dt))
        total = query.count()
        rows = (
            query.order_by(
                StrategyV2ShadowDecision.bar_at.desc(),
                StrategyV2ShadowDecision.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return StrategyV2ShadowDecisionPage(
            items=[self._decision_response(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    def list_trades(
        self,
        *,
        symbol: str | None = None,
        limit: int = 200,
        config_version: str | None = None,
    ) -> list[StrategyV2ShadowTradeResponse]:
        normalized = self._resolve_symbol(symbol)
        active_version = self._resolve_config_version(normalized, config_version)
        rows = (
            self.db.query(StrategyV2ShadowTrade)
            .filter(
                StrategyV2ShadowTrade.symbol == normalized,
                StrategyV2ShadowTrade.config_version == active_version,
            )
            .order_by(StrategyV2ShadowTrade.entry_at.desc())
            .limit(limit)
            .all()
        )
        return [StrategyV2ShadowTradeResponse.model_validate(row) for row in rows]

    def tick(
        self,
        symbol: str,
        market: str,
        now: datetime | None = None,
    ) -> StrategyV2ShadowStatusResponse:
        """Fetch 1m candles, derive complete 5m bars, and advance shadow state."""
        current = _as_utc(now or datetime.now(timezone.utc))
        normalized = self._resolve_symbol(symbol)
        config = self._get_or_create_config(normalized)
        state = self._get_or_create_state(normalized)
        open_trade = self._open_trade(normalized)

        # A flat algorithm revision is activated at the first observation, even
        # when that observation is outside RTH. A pre-market deployment can then
        # consume the opening minute instead of burning it on the transition.
        current_version = self._config_version(config)
        if open_trade is None and state.config_version != current_version:
            self._ensure_version_snapshot(config)
            self._reset_state_forward(
                state,
                config_version=current_version,
                watermark=self._forward_watermark(market, current),
            )
            self.db.add(state)
            self.db.commit()
            return self.get_status(normalized)

        if not config.enabled and open_trade is None:
            return self.get_status(normalized)
        if open_trade is None:
            self._validate_minimum_net_edge(self._config_values(config))
        if (
            not is_trading_hours(market, current)
            and open_trade is None
            and not self._in_post_close_collection_window(market, current)
        ):
            return self.get_status(normalized)
        if state.last_polled_at is not None:
            elapsed = (current - _as_utc(state.last_polled_at)).total_seconds()
            if elapsed < _MIN_POLL_SECONDS:
                return self.get_status(normalized)
        if self.candle_provider is None:
            raise RuntimeError("strategy v2 shadow candle provider is unavailable")

        # Persist the attempt before broker I/O. Concurrent or failing cron
        # calls therefore remain rate-limited instead of hammering the API.
        state.last_polled_at = current
        state.last_poll_error = ""
        self.db.add(state)
        self.db.commit()
        try:
            one_minute = self.candle_provider.get_candlesticks(
                normalized, "MIN_1", _ONE_MINUTE_CANDLE_COUNT
            )
            one_minute = self._historical_page_for_frontier(
                config=config,
                state=state,
                market=market,
                recent=one_minute,
                observed_at=current,
            )
            self._evaluate_candles(
                config=config,
                state=state,
                market=market,
                one_minute=one_minute,
                observed_at=current,
            )
        except Exception as exc:
            self.db.rollback()
            state = self._get_or_create_state(normalized)
            state.last_polled_at = current
            state.last_poll_error = str(exc)[:1000]
            self.db.add(state)
            self.db.commit()
            raise
        return self.get_status(normalized)

    def _historical_page_for_frontier(
        self,
        *,
        config: StrategyV2ShadowConfig,
        state: StrategyV2ShadowState,
        market: str,
        recent: list[Any],
        observed_at: datetime,
    ) -> list[Any]:
        """Page forward from the watermark when it fell out of the recent window."""
        if state.last_bar_at is None or not recent or self.candle_provider is None:
            return recent
        last_bar_at = _as_utc(state.last_bar_at)
        engine = StrategyV2Engine(self._domain_config(config, market))
        grace = timedelta(seconds=engine.config.settlement_grace_seconds)
        session = get_session(market)
        processable = [
            bar
            for bar in self._coerce_strategy_bars(recent, symbol=config.symbol)
            if (
                bar.timestamp > last_bar_at
                and session.is_rth(bar.timestamp)
                and bar.end_at + grace <= observed_at
            )
        ]
        if not processable:
            return recent
        first_bar_at = min(bar.timestamp for bar in processable)
        if self._missing_rth_minute(
            market,
            previous=last_bar_at,
            current=first_bar_at,
        ) is None:
            return recent
        history_reader = getattr(
            self.candle_provider,
            "get_history_candlesticks_by_offset",
            None,
        )
        if not callable(history_reader):
            return recent
        frontier_local = session.local(last_bar_at)
        session_open = datetime.combine(
            frontier_local.date(),
            session.rth_open,
            tzinfo=session.timezone,
        ).astimezone(timezone.utc)
        history_anchor = session_open - timedelta(minutes=1)
        historical = history_reader(
            config.symbol,
            "MIN_1",
            _ONE_MINUTE_CANDLE_COUNT,
            history_anchor,
        )
        if not isinstance(historical, list):
            raise ValueError("historical candle provider returned a non-list response")
        return historical or recent

    def compare_adx_challengers(
        self,
        payload: StrategyV2AdxChallengerRequest,
    ) -> StrategyV2AdxChallengerResponse:
        """Replay fixed ADX gates over the same complete persisted sessions."""
        symbol = self._resolve_symbol(payload.symbol)
        config_version = self._resolve_existing_config_version(
            symbol,
            payload.config_version,
        )
        complete_dates = self._complete_session_dates(symbol, config_version)
        selected_dates = complete_dates[-_MAX_CHALLENGER_COMPLETE_SESSIONS:]
        blockers: list[str] = []
        if len(complete_dates) < _MIN_CHALLENGER_COMPLETE_SESSIONS:
            blockers.append("MIN_COMPLETE_SESSIONS")
        params = self._version_params(symbol, config_version)
        if params.get("algorithm_version") != _ALGORITHM_VERSION:
            return StrategyV2AdxChallengerResponse(
                symbol=symbol,
                source_config_version=config_version,
                status="BLOCKED",
                minimum_complete_sessions=_MIN_CHALLENGER_COMPLETE_SESSIONS,
                observed_complete_sessions=len(complete_dates),
                evaluated_complete_sessions=len(selected_dates),
                blockers=[*blockers, "ALGORITHM_VERSION_UNSUPPORTED"],
                warmup_diagnostic=self._empty_warmup_diagnostic(
                    status="BLOCKED",
                    blockers=["ALGORITHM_VERSION_UNSUPPORTED"],
                ),
            )
        try:
            source_config = self._challenger_config(
                symbol=symbol,
                params=params,
                max_adx=float(params["max_adx"]),
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            return StrategyV2AdxChallengerResponse(
                symbol=symbol,
                source_config_version=config_version,
                status="BLOCKED",
                minimum_complete_sessions=_MIN_CHALLENGER_COMPLETE_SESSIONS,
                observed_complete_sessions=len(complete_dates),
                evaluated_complete_sessions=len(selected_dates),
                blockers=[*blockers, "CONFIG_SNAPSHOT_INVALID"],
                warmup_diagnostic=self._empty_warmup_diagnostic(
                    status="BLOCKED",
                    blockers=["CONFIG_SNAPSHOT_INVALID"],
                ),
            )
        if self._config_version(source_config) != config_version:
            return StrategyV2AdxChallengerResponse(
                symbol=symbol,
                source_config_version=config_version,
                status="BLOCKED",
                minimum_complete_sessions=_MIN_CHALLENGER_COMPLETE_SESSIONS,
                observed_complete_sessions=len(complete_dates),
                evaluated_complete_sessions=len(selected_dates),
                blockers=[*blockers, "CONFIG_SNAPSHOT_VERSION_MISMATCH"],
                warmup_diagnostic=self._empty_warmup_diagnostic(
                    status="BLOCKED",
                    blockers=["CONFIG_SNAPSHOT_VERSION_MISMATCH"],
                ),
            )
        if not selected_dates:
            return StrategyV2AdxChallengerResponse(
                symbol=symbol,
                source_config_version=config_version,
                status="INSUFFICIENT_EVIDENCE",
                minimum_complete_sessions=_MIN_CHALLENGER_COMPLETE_SESSIONS,
                observed_complete_sessions=0,
                evaluated_complete_sessions=0,
                blockers=blockers,
                warmup_diagnostic=self._empty_warmup_diagnostic(),
            )

        selected_set = set(selected_dates)
        selected_decisions = self.db.query(StrategyV2ShadowDecision).filter(
            StrategyV2ShadowDecision.symbol == symbol,
            StrategyV2ShadowDecision.config_version == config_version,
            StrategyV2ShadowDecision.session_date.in_(selected_dates),
        ).order_by(
            StrategyV2ShadowDecision.bar_at.asc(),
            StrategyV2ShadowDecision.id.asc(),
        ).execution_options(populate_existing=True).all()
        trade_window_start = datetime.combine(
            min(selected_dates) - timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        trade_window_end = datetime.combine(
            max(selected_dates) + timedelta(days=2),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        trades = self.db.query(StrategyV2ShadowTrade).filter(
            StrategyV2ShadowTrade.symbol == symbol,
            StrategyV2ShadowTrade.config_version == config_version,
            StrategyV2ShadowTrade.status == "CLOSED",
            StrategyV2ShadowTrade.exit_at >= trade_window_start,
            StrategyV2ShadowTrade.exit_at < trade_window_end,
        ).order_by(StrategyV2ShadowTrade.entry_at.asc()).all()
        market = "HK" if symbol.endswith(".HK") else "US"
        if any(item.market.upper() != market for item in selected_decisions):
            raise ValueError("persisted challenger evidence mixes markets")
        bars = self._bars_from_persisted_evidence(
            selected_decisions,
            symbol=symbol,
        )
        replay_payload = StrategyV2ShadowReplayRequest(
            symbol=symbol,
            market=market,
            bars=bars,
        )

        baseline_max_adx = float(source_config.max_adx)
        candidate_values = [
            baseline_max_adx,
            *(
                value
                for value in _ADX_CHALLENGER_VALUES
                if not math.isclose(value, baseline_max_adx, abs_tol=1e-12)
            ),
        ]
        results: list[StrategyV2AdxChallengerResult] = []
        baseline_replay: StrategyV2ShadowReplayResponse | None = None
        for index, max_adx in enumerate(candidate_values):
            candidate_config = self._challenger_config(
                symbol=symbol,
                params=params,
                max_adx=max_adx,
            )
            replay = self._replay_payload(
                replay_payload,
                candidate_config,
                include_feature_evidence=index == 0,
            )
            if index == 0:
                baseline_replay = replay
            results.append(StrategyV2AdxChallengerResult(
                label="BASELINE" if index == 0 else "CHALLENGER",
                max_adx=max_adx,
                config_version=replay.config_version,
                metrics=replay.metrics,
                daily=self._challenger_daily(replay, market, selected_dates),
            ))

        if baseline_replay is None:
            raise RuntimeError("ADX challenger baseline replay was not produced")
        baseline_match = self._baseline_replay_matches(
            decisions=selected_decisions,
            trades=trades,
            replay=baseline_replay,
            market=market,
            session_dates=selected_set,
        )
        if not baseline_match:
            return StrategyV2AdxChallengerResponse(
                symbol=symbol,
                source_config_version=config_version,
                status="BLOCKED",
                minimum_complete_sessions=_MIN_CHALLENGER_COMPLETE_SESSIONS,
                observed_complete_sessions=len(complete_dates),
                evaluated_complete_sessions=len(selected_dates),
                baseline_replay_match=False,
                blockers=[*blockers, "BASELINE_REPLAY_MISMATCH"],
                candidates=results[:1],
                warmup_diagnostic=self._empty_warmup_diagnostic(
                    status="BLOCKED",
                    blockers=["BASELINE_REPLAY_MISMATCH"],
                ),
            )
        warmup_diagnostic = self._warmup_diagnostic(
            replay_bars=bars,
            source_config=source_config,
            source_config_version=config_version,
            market=market,
            session_dates=selected_dates,
        )
        return StrategyV2AdxChallengerResponse(
            symbol=symbol,
            source_config_version=config_version,
            status=(
                "READY_FOR_REVIEW"
                if len(complete_dates) >= _MIN_CHALLENGER_COMPLETE_SESSIONS
                else "INSUFFICIENT_EVIDENCE"
            ),
            minimum_complete_sessions=_MIN_CHALLENGER_COMPLETE_SESSIONS,
            observed_complete_sessions=len(complete_dates),
            evaluated_complete_sessions=len(selected_dates),
            baseline_replay_match=True,
            blockers=blockers,
            candidates=results,
            warmup_diagnostic=warmup_diagnostic,
        )

    def register_forward_validation(
        self,
        payload: StrategyV2ForwardRegistrationRequest,
        *,
        now: datetime | None = None,
    ) -> StrategyV2ForwardRegistrationResponse:
        """Freeze the one prospective warm-up candidate without touching shadow state."""
        symbol = self._resolve_symbol(payload.symbol)
        existing = self.db.query(StrategyV2ForwardRegistration).filter(
            StrategyV2ForwardRegistration.symbol == symbol,
        ).first()
        if existing is not None:
            if (
                existing.candidate_algorithm_version
                != payload.candidate_algorithm_version
                or existing.source_config_version != payload.source_config_version
                or existing.evaluator_digest != self._forward_evaluator_digest()
                or not self._forward_spec_matches(existing)
            ):
                raise ValueError(
                    "strategy v2 forward candidate was already registered with a different definition"
                )
            return self._forward_registration_response(existing)

        config = self.db.query(StrategyV2ShadowConfig).filter(
            StrategyV2ShadowConfig.symbol == symbol
        ).first()
        if config is None:
            raise ValueError("strategy v2 shadow config was not found for symbol")
        source_version = self._resolve_existing_config_version(
            symbol,
            payload.source_config_version,
        )
        if source_version != self._config_version(config):
            raise ValueError("forward validation requires the current shadow config version")
        if not config.enabled:
            raise ValueError("forward validation requires an enabled shadow config")
        state = self.db.query(StrategyV2ShadowState).filter(
            StrategyV2ShadowState.symbol == symbol
        ).first()
        open_trade = self._open_trade(symbol)
        if state is None or state.config_version != source_version:
            raise ValueError("forward validation requires an activated current shadow state")
        if (
            open_trade is not None
            or state.open_trade_id is not None
            or state.phase == StrategyV2State.LONG.value
        ):
            raise ValueError("forward validation cannot register during a virtual position")
        params = self._version_params(symbol, source_version)
        if params.get("algorithm_version") != _ALGORITHM_VERSION:
            raise ValueError("forward validation source algorithm version is unsupported")
        registered_at = _as_utc(now or datetime.now(timezone.utc))
        market = "HK" if symbol.endswith(".HK") else "US"
        spec = self._forward_candidate_spec(source_version, params)
        registration = StrategyV2ForwardRegistration(
            symbol=symbol,
            market=market,
            candidate_algorithm_version=_FORWARD_CANDIDATE_VERSION,
            source_config_version=source_version,
            evaluator_digest=self._forward_evaluator_digest(),
            candidate_spec_json=json.dumps(
                spec,
                sort_keys=True,
                separators=(",", ":"),
            ),
            registered_at=registered_at,
            eligible_after=self._forward_eligible_after(market, registered_at),
        )
        self.db.add(registration)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self.db.query(StrategyV2ForwardRegistration).filter(
                StrategyV2ForwardRegistration.symbol == symbol,
            ).first()
            if (
                existing is None
                or existing.candidate_algorithm_version != _FORWARD_CANDIDATE_VERSION
                or existing.source_config_version != source_version
                or existing.evaluator_digest != self._forward_evaluator_digest()
                or not self._forward_spec_matches(existing)
            ):
                raise ValueError(
                    "strategy v2 forward candidate registration conflicts with an existing definition"
                )
            return self._forward_registration_response(existing)
        self.db.refresh(registration)
        return self._forward_registration_response(registration)

    def get_forward_validation(
        self,
        symbol: str,
    ) -> StrategyV2ForwardValidationResponse:
        """Read only materialized prospective evidence; never replay or persist here."""
        normalized = self._resolve_symbol(symbol)
        registration = self.db.query(StrategyV2ForwardRegistration).filter(
            StrategyV2ForwardRegistration.symbol == normalized,
        ).first()
        if registration is None:
            return StrategyV2ForwardValidationResponse(status="NOT_REGISTERED")

        rows = self.db.query(StrategyV2ForwardEvidence).filter(
            StrategyV2ForwardEvidence.registration_id == registration.id
        ).order_by(
            StrategyV2ForwardEvidence.target_session_date.asc(),
            StrategyV2ForwardEvidence.id.asc(),
        ).all()
        blockers: list[str] = []
        expected_market = "HK" if normalized.endswith(".HK") else "US"
        if (
            registration.market != expected_market
            or registration.candidate_algorithm_version
            != _FORWARD_CANDIDATE_VERSION
            or not re.fullmatch(r"[0-9a-f]{64}", registration.evaluator_digest)
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                registration.source_config_version,
            )
        ):
            blockers.append("REGISTRATION_METADATA_INVALID")
        if (
            registration.evaluator_digest != self._forward_evaluator_digest()
            or not self._forward_spec_matches(registration)
        ):
            blockers.append("EVALUATOR_DEFINITION_MISMATCH")
        try:
            expected_eligible_after = self._forward_eligible_after(
                expected_market,
                registration.registered_at,
            )
        except ValueError:
            blockers.append("REGISTRATION_BOUNDARY_INVALID")
        else:
            if _as_utc(registration.eligible_after) != expected_eligible_after:
                blockers.append("REGISTRATION_BOUNDARY_INVALID")

        daily: list[StrategyV2ForwardDailyEvidence] = []
        baseline_metrics: list[tuple[StrategyV2ShadowMetrics, list[float]]] = []
        candidate_metrics: list[tuple[StrategyV2ShadowMetrics, list[float]]] = []
        valid_included = 0
        market_session = get_session(expected_market)
        for row in rows:
            baseline_daily: StrategyV2WarmupDaily | None = None
            candidate_daily: StrategyV2WarmupDaily | None = None
            baseline_metric: StrategyV2ShadowMetrics | None = None
            candidate_metric: StrategyV2ShadowMetrics | None = None
            if (
                not self._is_sha256(row.evidence_digest_sha256)
                or self._forward_evidence_digest(row)
                != row.evidence_digest_sha256
            ):
                blockers.append("EVIDENCE_DIGEST_MISMATCH")
            if row.disposition not in {"INCLUDED", "EXCLUDED"}:
                blockers.append("EVIDENCE_DISPOSITION_INVALID")
            target_open = _as_utc(row.target_open_at)
            expected_target_open = datetime.combine(
                row.target_session_date,
                market_session.rth_open,
                tzinfo=market_session.timezone,
            ).astimezone(timezone.utc)
            if (
                target_open < _as_utc(registration.eligible_after)
                or target_open != expected_target_open
                or not market_session.is_rth(target_open)
                or market_session.trade_day(target_open) != row.target_session_date
            ):
                blockers.append("EVIDENCE_TARGET_BOUNDARY_INVALID")
            if row.disposition == "INCLUDED":
                try:
                    if (
                        self._forward_collection_phase(
                            registration.market,
                            _as_utc(row.evaluated_at),
                        )
                        != "FINALIZE"
                        or market_session.trade_day(_as_utc(row.evaluated_at))
                        != row.target_session_date
                        or row.structural_failure
                        or bool(row.exclusion_reason)
                        or row.seed_session_date is None
                        or next_session_open(
                            registration.market,
                            datetime.combine(
                                row.seed_session_date,
                                market_session.close_time(row.seed_session_date),
                                tzinfo=market_session.timezone,
                            ).astimezone(timezone.utc),
                        )
                        != target_open
                        or not row.same_target_bars
                        or not self._is_sha256(row.seed_bars_sha256)
                        or not self._is_sha256(row.target_bars_sha256)
                        or not self._is_sha256(row.baseline_input_sha256)
                        or not self._is_sha256(row.candidate_input_sha256)
                        or not self._is_sha256(row.baseline_result_sha256)
                        or not self._is_sha256(row.candidate_result_sha256)
                        or row.target_bars_sha256 != row.baseline_input_sha256
                        or row.target_bars_sha256 != row.candidate_input_sha256
                        or self._forward_text_hash(row.baseline_result_json)
                        != row.baseline_result_sha256
                        or self._forward_text_hash(row.candidate_result_json)
                        != row.candidate_result_sha256
                        or row.baseline_replay_match is not True
                        or row.session_local_invariant is not True
                    ):
                        raise ValueError("forward evidence hash mismatch")
                    baseline_payload = json.loads(row.baseline_result_json)
                    candidate_payload = json.loads(row.candidate_result_json)
                    if not isinstance(baseline_payload, dict) or not isinstance(
                        candidate_payload,
                        dict,
                    ):
                        raise TypeError("forward evidence payload is not an object")
                    baseline_daily = StrategyV2WarmupDaily.model_validate(
                        baseline_payload["daily"]
                    )
                    candidate_daily = StrategyV2WarmupDaily.model_validate(
                        candidate_payload["daily"]
                    )
                    baseline_metric = StrategyV2ShadowMetrics.model_validate(
                        baseline_payload["metrics"]
                    )
                    candidate_metric = StrategyV2ShadowMetrics.model_validate(
                        candidate_payload["metrics"]
                    )
                    baseline_net_sequence = self._forward_net_pnl_sequence(
                        baseline_payload,
                        baseline_metric,
                    )
                    candidate_net_sequence = self._forward_net_pnl_sequence(
                        candidate_payload,
                        candidate_metric,
                    )
                    if (
                        baseline_daily.session_date != row.target_session_date
                        or candidate_daily.session_date != row.target_session_date
                        or baseline_daily.seed_session_date != row.seed_session_date
                        or candidate_daily.seed_session_date != row.seed_session_date
                        or baseline_daily.bars != row.target_bars
                        or candidate_daily.bars != row.target_bars
                        or _as_utc(baseline_daily.trend_context_cutoff_at)
                        >= target_open
                        or _as_utc(candidate_daily.trend_context_cutoff_at)
                        >= target_open
                    ):
                        raise ValueError("forward evidence session identity mismatch")
                except (
                    KeyError,
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                ):
                    blockers.append("EVIDENCE_PAYLOAD_INVALID")
                else:
                    baseline_metrics.append((baseline_metric, baseline_net_sequence))
                    candidate_metrics.append((candidate_metric, candidate_net_sequence))
                    valid_included += 1
            elif not self._forward_exclusion_semantics_valid(row):
                blockers.append("EVIDENCE_EXCLUSION_INVALID")
            if row.structural_failure:
                blockers.append(row.exclusion_reason or "STRUCTURAL_EVALUATION_FAILURE")
            daily.append(StrategyV2ForwardDailyEvidence(
                target_session_date=row.target_session_date,
                seed_session_date=row.seed_session_date,
                target_open_at=_as_utc(row.target_open_at),
                evaluated_at=_as_utc(row.evaluated_at),
                disposition=(
                    "INCLUDED" if row.disposition == "INCLUDED" else "EXCLUDED"
                ),
                exclusion_reason=row.exclusion_reason,
                structural_failure=row.structural_failure,
                target_bars=row.target_bars,
                target_bars_sha256=row.target_bars_sha256,
                seed_bars_sha256=row.seed_bars_sha256,
                baseline_input_sha256=row.baseline_input_sha256,
                candidate_input_sha256=row.candidate_input_sha256,
                same_target_bars=row.same_target_bars,
                baseline_replay_match=row.baseline_replay_match,
                session_local_invariant=row.session_local_invariant,
                baseline=baseline_daily,
                candidate=candidate_daily,
                baseline_metrics=baseline_metric,
                candidate_metrics=candidate_metric,
                baseline_result_sha256=row.baseline_result_sha256,
                candidate_result_sha256=row.candidate_result_sha256,
                evidence_digest_sha256=row.evidence_digest_sha256,
            ))

        blockers = list(dict.fromkeys(blockers))
        included = valid_included
        excluded = sum(row.disposition == "EXCLUDED" for row in rows)
        status = self._forward_validation_status(
            included=included,
            has_rows=bool(rows),
            blockers=blockers,
        )
        return StrategyV2ForwardValidationResponse(
            registration=self._forward_registration_response(registration),
            status=status,
            included_pairs=included,
            excluded_targets=excluded,
            remaining_ready_pairs=max(0, _FORWARD_READY_PAIRS - included),
            remaining_mature_pairs=max(0, _FORWARD_MATURE_PAIRS - included),
            blockers=blockers,
            baseline_metrics=self._aggregate_forward_metrics(baseline_metrics),
            candidate_metrics=self._aggregate_forward_metrics(candidate_metrics),
            daily=daily,
        )

    def collect_forward_validation(
        self,
        symbol: str,
        market: str,
        *,
        now: datetime | None = None,
    ) -> StrategyV2ForwardValidationResponse | None:
        """Materialize at most one target outcome during the fixed close window."""
        current = _as_utc(now or datetime.now(timezone.utc))
        normalized = self._resolve_symbol(symbol)
        registration = self.db.query(StrategyV2ForwardRegistration).filter(
            StrategyV2ForwardRegistration.symbol == normalized,
        ).first()
        if registration is None:
            return None
        normalized_market = market.upper()
        if normalized_market != registration.market:
            raise ValueError("forward validation registration market mismatch")
        current_view = self.get_forward_validation(normalized)
        if current_view.status in {"BLOCKED", "MATURE_EVIDENCE"}:
            return current_view

        active_config = self.db.query(StrategyV2ShadowConfig).filter(
            StrategyV2ShadowConfig.symbol == normalized
        ).first()
        active_state = self.db.query(StrategyV2ShadowState).filter(
            StrategyV2ShadowState.symbol == normalized
        ).first()
        active_trade = self._open_trade(normalized)
        source_superseded = (
            active_config is None
            or self._config_version(active_config)
            != registration.source_config_version
            or active_state is None
            or active_state.config_version != registration.source_config_version
            or (
                active_trade is not None
                and active_trade.config_version != registration.source_config_version
            )
        )
        appended_missed = self._record_forward_missed_targets(
            registration,
            current,
            reason=(
                "SOURCE_VERSION_SUPERSEDED"
                if source_superseded
                else "FINALIZATION_WINDOW_MISSED"
            ),
            structural=source_superseded,
        )
        current_view = self.get_forward_validation(normalized)
        if appended_missed:
            return current_view
        collection_phase = self._forward_collection_phase(
            normalized_market,
            current,
        )
        if collection_phase == "WAIT":
            return None
        if current_view.status in {"BLOCKED", "MATURE_EVIDENCE"}:
            return current_view

        session = get_session(normalized_market)
        target_day = session.trade_day(current)
        target_open = datetime.combine(
            target_day,
            session.rth_open,
            tzinfo=session.timezone,
        ).astimezone(timezone.utc)
        if target_open < _as_utc(registration.eligible_after):
            return None
        existing = self.db.query(StrategyV2ForwardEvidence.id).filter(
            StrategyV2ForwardEvidence.registration_id == registration.id,
            StrategyV2ForwardEvidence.target_session_date == target_day,
        ).first()
        if existing is not None:
            return current_view
        if collection_phase == "MISSED":
            self._persist_forward_exclusion(
                registration=registration,
                target_day=target_day,
                target_open=target_open,
                evaluated_at=current,
                reason="FINALIZATION_WINDOW_MISSED",
                structural=False,
            )
            return self.get_forward_validation(normalized)
        if (
            registration.evaluator_digest != self._forward_evaluator_digest()
            or not self._forward_spec_matches(registration)
        ):
            self._persist_forward_exclusion(
                registration=registration,
                target_day=target_day,
                target_open=target_open,
                evaluated_at=current,
                reason="EVALUATOR_DEFINITION_MISMATCH",
                structural=True,
            )
            return self.get_forward_validation(normalized)

        if source_superseded:
            self._persist_forward_exclusion(
                registration=registration,
                target_day=target_day,
                target_open=target_open,
                evaluated_at=current,
                reason="SOURCE_VERSION_SUPERSEDED",
                structural=True,
            )
            return self.get_forward_validation(normalized)
        if active_config is not None and not active_config.enabled:
            self._persist_forward_exclusion(
                registration=registration,
                target_day=target_day,
                target_open=target_open,
                evaluated_at=current,
                reason="COLLECTION_DISABLED",
                structural=False,
            )
            return self.get_forward_validation(normalized)
        if (
            active_trade is not None
            or active_state is None
            or active_state.open_trade_id is not None
            or active_state.phase == StrategyV2State.LONG.value
        ):
            self._persist_forward_exclusion(
                registration=registration,
                target_day=target_day,
                target_open=target_open,
                evaluated_at=current,
                reason="TARGET_STATE_NOT_FLAT",
                structural=True,
            )
            return self.get_forward_validation(normalized)

        target_decisions = self.db.query(StrategyV2ShadowDecision).filter(
            StrategyV2ShadowDecision.symbol == normalized,
            StrategyV2ShadowDecision.config_version
            == registration.source_config_version,
            StrategyV2ShadowDecision.session_date == target_day,
        ).order_by(
            StrategyV2ShadowDecision.bar_at.asc(),
            StrategyV2ShadowDecision.id.asc(),
        ).execution_options(populate_existing=True).all()
        target_daily = self._daily_evidence(target_decisions, [])
        if target_decisions and max(
            _as_utc(item.observed_at) for item in target_decisions
        ) > current:
            self._persist_forward_exclusion(
                registration=registration,
                target_day=target_day,
                target_open=target_open,
                evaluated_at=current,
                reason="TARGET_EVIDENCE_NOT_KNOWN_AT_EVALUATION",
                structural=True,
                target_bars=len(target_decisions),
            )
            return self.get_forward_validation(normalized)
        if (
            len(target_daily) != 1
            or not target_daily[0].complete_session
            or _as_utc(target_daily[0].first_bar_at) != target_open
        ):
            local_current = session.local(current)
            incomplete_deadline = datetime.combine(
                target_day,
                session.close_time(target_day),
                tzinfo=session.timezone,
            ) + timedelta(minutes=_FORWARD_INCOMPLETE_DEADLINE_MINUTES)
            if local_current < incomplete_deadline:
                return current_view
            self._persist_forward_exclusion(
                registration=registration,
                target_day=target_day,
                target_open=target_open,
                evaluated_at=current,
                reason="TARGET_SESSION_INCOMPLETE",
                structural=False,
                target_bars=(target_daily[0].bars if target_daily else 0),
            )
            return self.get_forward_validation(normalized)

        complete_dates = self._complete_session_dates(
            normalized,
            registration.source_config_version,
        )
        prior_dates = [item for item in complete_dates if item < target_day]
        seed_day = prior_dates[-1] if prior_dates else None
        seed_decisions: list[StrategyV2ShadowDecision] = []
        if seed_day is not None:
            seed_decisions = self.db.query(StrategyV2ShadowDecision).filter(
                StrategyV2ShadowDecision.symbol == normalized,
                StrategyV2ShadowDecision.config_version
                == registration.source_config_version,
                StrategyV2ShadowDecision.session_date == seed_day,
            ).order_by(
                StrategyV2ShadowDecision.bar_at.asc(),
                StrategyV2ShadowDecision.id.asc(),
            ).execution_options(populate_existing=True).all()
        if (
            seed_day is None
            or not seed_decisions
            or next_session_open(
                normalized_market,
                _as_utc(seed_decisions[-1].bar_at) + timedelta(minutes=1),
            )
            != target_open
        ):
            self._persist_forward_exclusion(
                registration=registration,
                target_day=target_day,
                target_open=target_open,
                evaluated_at=current,
                reason="IMMEDIATE_COMPLETE_SEED_UNAVAILABLE",
                structural=False,
                target_bars=target_daily[0].bars,
                seed_day=seed_day,
            )
            return self.get_forward_validation(normalized)
        if max(_as_utc(item.observed_at) for item in seed_decisions) > target_open:
            self._persist_forward_exclusion(
                registration=registration,
                target_day=target_day,
                target_open=target_open,
                evaluated_at=current,
                reason="SEED_NOT_KNOWN_AT_TARGET_OPEN",
                structural=False,
                target_bars=target_daily[0].bars,
                seed_day=seed_day,
            )
            return self.get_forward_validation(normalized)

        try:
            params = self._version_params(
                normalized,
                registration.source_config_version,
            )
            source_config = self._challenger_config(
                symbol=normalized,
                params=params,
                max_adx=float(params["max_adx"]),
            )
            if self._config_version(source_config) != registration.source_config_version:
                raise ValueError("source config snapshot version mismatch")
            seed_replay_bars = self._bars_from_persisted_evidence(
                seed_decisions,
                symbol=normalized,
            )
            target_replay_bars = self._bars_from_persisted_evidence(
                target_decisions,
                symbol=normalized,
            )
            target_hash = self._forward_bars_hash(target_replay_bars)
            seed_hash = self._forward_bars_hash(seed_replay_bars)
            target_payload = StrategyV2ShadowReplayRequest(
                symbol=normalized,
                market="HK" if normalized_market == "HK" else "US",
                bars=target_replay_bars,
            )
            baseline_input_hash = self._forward_bars_hash(target_payload.bars)
            baseline = self._replay_payload(
                target_payload,
                source_config,
                include_feature_evidence=True,
            )
            if self._forward_bars_hash(target_payload.bars) != baseline_input_hash:
                raise ValueError("baseline replay mutated its target input")
            trades = self.db.query(StrategyV2ShadowTrade).filter(
                StrategyV2ShadowTrade.symbol == normalized,
                StrategyV2ShadowTrade.config_version
                == registration.source_config_version,
                StrategyV2ShadowTrade.status == "CLOSED",
            ).order_by(StrategyV2ShadowTrade.entry_at.asc()).all()
            if not self._baseline_replay_matches(
                decisions=target_decisions,
                trades=trades,
                replay=baseline,
                market=normalized_market,
                session_dates={target_day},
            ):
                self._persist_forward_exclusion(
                    registration=registration,
                    target_day=target_day,
                    target_open=target_open,
                    evaluated_at=current,
                    reason="BASELINE_REPLAY_MISMATCH",
                    structural=True,
                    target_bars=len(target_replay_bars),
                    target_hash=target_hash,
                    seed_hash=seed_hash,
                    seed_day=seed_day,
                    baseline_input_hash=baseline_input_hash,
                    candidate_input_hash=self._forward_bars_hash(target_payload.bars),
                    baseline_match=False,
                )
                return self.get_forward_validation(normalized)
            seed_bars = self._strategy_bars_from_replay(
                seed_replay_bars,
                symbol=normalized,
            )
            target_bars = self._strategy_bars_from_replay(
                target_replay_bars,
                symbol=normalized,
            )
            candidate_input_hash = self._forward_bars_hash(target_payload.bars)
            if not (
                target_hash == baseline_input_hash == candidate_input_hash
            ):
                self._persist_forward_exclusion(
                    registration=registration,
                    target_day=target_day,
                    target_open=target_open,
                    evaluated_at=current,
                    reason="TARGET_INPUT_HASH_MISMATCH",
                    structural=True,
                    target_bars=len(target_replay_bars),
                    target_hash=target_hash,
                    seed_hash=seed_hash,
                    seed_day=seed_day,
                    baseline_input_hash=baseline_input_hash,
                    candidate_input_hash=candidate_input_hash,
                    baseline_match=True,
                )
                return self.get_forward_validation(normalized)
            prewarmed = self._replay_payload(
                target_payload,
                source_config,
                include_feature_evidence=True,
                features=CausalTrendPrewarmFeatureEngine(
                    self._domain_config(source_config, normalized_market).feature_config(),
                    seed_bars,
                ),
            )
            if self._forward_bars_hash(target_payload.bars) != candidate_input_hash:
                raise ValueError("candidate replay mutated its target input")
            if not self._session_local_features_match(baseline, prewarmed):
                self._persist_forward_exclusion(
                    registration=registration,
                    target_day=target_day,
                    target_open=target_open,
                    evaluated_at=current,
                    reason="SESSION_LOCAL_FEATURE_DRIFT",
                    structural=True,
                    target_bars=len(target_replay_bars),
                    target_hash=target_hash,
                    seed_hash=seed_hash,
                    seed_day=seed_day,
                    baseline_input_hash=baseline_input_hash,
                    candidate_input_hash=candidate_input_hash,
                    baseline_match=True,
                    local_invariant=False,
                )
                return self.get_forward_validation(normalized)
            baseline_daily = self._warmup_daily_from_replay(
                replay=baseline,
                market=normalized_market,
                seed_day=seed_day,
                target_day=target_day,
                seed_bars=seed_bars,
                target_bars=target_bars,
            )
            candidate_daily = self._warmup_daily_from_replay(
                replay=prewarmed,
                market=normalized_market,
                seed_day=seed_day,
                target_day=target_day,
                seed_bars=seed_bars,
                target_bars=target_bars,
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            self.db.rollback()
            self._persist_forward_exclusion(
                registration=registration,
                target_day=target_day,
                target_open=target_open,
                evaluated_at=current,
                reason="FORWARD_EVALUATION_FAILED",
                structural=True,
                target_bars=target_daily[0].bars,
                seed_day=seed_day,
            )
            return self.get_forward_validation(normalized)

        baseline_result = self._forward_result_json(
            baseline.metrics,
            baseline_daily,
            baseline.trades,
        )
        candidate_result = self._forward_result_json(
            prewarmed.metrics,
            candidate_daily,
            prewarmed.trades,
        )
        result = StrategyV2ForwardEvidence(
            registration_id=registration.id,
            target_session_date=target_day,
            seed_session_date=seed_day,
            target_open_at=target_open,
            evaluated_at=current,
            disposition="INCLUDED",
            exclusion_reason="",
            structural_failure=False,
            target_bars=len(target_replay_bars),
            target_bars_sha256=target_hash,
            seed_bars_sha256=seed_hash,
            baseline_input_sha256=baseline_input_hash,
            candidate_input_sha256=candidate_input_hash,
            same_target_bars=(
                target_hash == baseline_input_hash == candidate_input_hash
            ),
            baseline_replay_match=True,
            session_local_invariant=True,
            baseline_result_json=baseline_result,
            candidate_result_json=candidate_result,
            baseline_result_sha256=self._forward_text_hash(baseline_result),
            candidate_result_sha256=self._forward_text_hash(candidate_result),
        )
        result.evidence_digest_sha256 = self._forward_evidence_digest(result)
        self.db.add(result)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
        return self.get_forward_validation(normalized)

    @staticmethod
    def _forward_evaluator_spec() -> dict[str, Any]:
        return {
            "schema_version": 2,
            "evaluator_version": _FORWARD_EVALUATOR_VERSION,
            "candidate_algorithm_version": _FORWARD_CANDIDATE_VERSION,
            "source_algorithm_version": _ALGORITHM_VERSION,
            "evaluation_scope": "FORWARD_OUT_OF_SAMPLE",
            "target_semantics": "FIRST_FULL_RTH_SESSION_OPEN_STRICTLY_AFTER_REGISTRATION",
            "seed_semantics": "IMMEDIATE_PRIOR_COMPLETE_SAME_VERSION_SESSION_KNOWN_AT_OPEN",
            "warmup_scope": "ADX_VOL_ONLY",
            "baseline_scope": "SESSION_LOCAL",
            "minimum_ready_pairs": _FORWARD_READY_PAIRS,
            "minimum_mature_pairs": _FORWARD_MATURE_PAIRS,
            "finalize_start_minutes_after_close": _FORWARD_FINALIZE_START_MINUTES,
            "incomplete_deadline_minutes_after_close": (
                _FORWARD_INCOMPLETE_DEADLINE_MINUTES
            ),
            "finalize_end_minutes_after_close": _POST_CLOSE_COLLECTION_MINUTES,
            "historical_target_backfill_allowed": False,
            "evidence_integrity": "CANONICAL_ROW_SHA256",
            "aggregate_drawdown": "ORDERED_TRADE_NET_PNL",
            "order_submission_allowed": False,
            "automatic_promotion_allowed": False,
        }

    @classmethod
    def _forward_evaluator_digest(cls) -> str:
        encoded = json.dumps(
            cls._forward_evaluator_spec(),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def _forward_candidate_spec(
        cls,
        source_config_version: str,
        source_params: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            **cls._forward_evaluator_spec(),
            "source_config_version": source_config_version,
            "source_config": source_params,
        }

    def _forward_spec_matches(
        self,
        registration: StrategyV2ForwardRegistration,
    ) -> bool:
        try:
            decoded = json.loads(registration.candidate_spec_json)
            source_params = self._version_params(
                registration.symbol,
                registration.source_config_version,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
        return (
            isinstance(decoded, dict)
            and decoded
            == self._forward_candidate_spec(
                registration.source_config_version,
                source_params,
            )
        )

    @staticmethod
    def _forward_eligible_after(market: str, registered_at: datetime) -> datetime:
        """Return a full RTH open strictly after registration, never HK lunch."""
        registered = _as_utc(registered_at)
        session = get_session(market)
        probe = registered
        for _ in range(4):
            candidate = next_session_open(market, probe)
            if (
                candidate > registered
                and session.local(candidate).time() == session.rth_open
            ):
                return candidate
            probe = candidate + timedelta(microseconds=1)
        raise ValueError("unable to resolve the next complete market session")

    @staticmethod
    def _forward_collection_phase(market: str, current: datetime) -> str:
        if session_status(market, current) != "post":
            return "WAIT"
        session = get_session(market)
        local = session.local(current)
        close_at = datetime.combine(
            local.date(),
            session.close_time(local.date()),
            tzinfo=session.timezone,
        )
        if local < close_at + timedelta(minutes=_FORWARD_FINALIZE_START_MINUTES):
            return "WAIT"
        if local < close_at + timedelta(minutes=_POST_CLOSE_COLLECTION_MINUTES):
            return "FINALIZE"
        return "MISSED"

    @classmethod
    def _in_forward_finalize_window(cls, market: str, current: datetime) -> bool:
        return cls._forward_collection_phase(market, current) == "FINALIZE"

    @classmethod
    def _forward_registration_response(
        cls,
        registration: StrategyV2ForwardRegistration,
    ) -> StrategyV2ForwardRegistrationResponse:
        market = "HK" if registration.symbol.endswith(".HK") else "US"
        session = get_session(market)
        return StrategyV2ForwardRegistrationResponse(
            id=registration.id,
            symbol=registration.symbol,
            market=market,
            market_timezone=str(session.timezone),
            candidate_algorithm_version=_FORWARD_CANDIDATE_VERSION,
            source_config_version=registration.source_config_version,
            evaluator_digest=registration.evaluator_digest,
            registered_at=_as_utc(registration.registered_at),
            eligible_after=_as_utc(registration.eligible_after),
        )

    @staticmethod
    def _forward_bars_hash(bars: Sequence[StrategyV2ReplayBar]) -> str:
        encoded = json.dumps(
            [item.model_dump(mode="json") for item in bars],
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _forward_result_json(
        metrics: StrategyV2ShadowMetrics,
        daily: StrategyV2WarmupDaily,
        trades: Sequence[dict[str, Any]],
    ) -> str:
        return json.dumps(
            {
                "metrics": metrics.model_dump(mode="json"),
                "daily": daily.model_dump(mode="json"),
                "trade_net_pnl": [
                    float(item.get("net_pnl", 0.0)) for item in trades
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _forward_text_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_sha256(value: str) -> bool:
        return re.fullmatch(r"[0-9a-f]{64}", value or "") is not None

    @classmethod
    def _forward_evidence_digest(
        cls,
        row: StrategyV2ForwardEvidence,
    ) -> str:
        encoded = json.dumps(
            {
                "registration_id": row.registration_id,
                "target_session_date": row.target_session_date.isoformat(),
                "seed_session_date": (
                    row.seed_session_date.isoformat()
                    if row.seed_session_date is not None
                    else None
                ),
                "target_open_at": _as_utc(row.target_open_at).isoformat(),
                "evaluated_at": _as_utc(row.evaluated_at).isoformat(),
                "disposition": row.disposition,
                "exclusion_reason": row.exclusion_reason,
                "structural_failure": row.structural_failure,
                "target_bars": row.target_bars,
                "target_bars_sha256": row.target_bars_sha256,
                "seed_bars_sha256": row.seed_bars_sha256,
                "baseline_input_sha256": row.baseline_input_sha256,
                "candidate_input_sha256": row.candidate_input_sha256,
                "same_target_bars": row.same_target_bars,
                "baseline_replay_match": row.baseline_replay_match,
                "session_local_invariant": row.session_local_invariant,
                "baseline_result_json": row.baseline_result_json,
                "candidate_result_json": row.candidate_result_json,
                "baseline_result_sha256": row.baseline_result_sha256,
                "candidate_result_sha256": row.candidate_result_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return cls._forward_text_hash(encoded)

    @staticmethod
    def _forward_net_pnl_sequence(
        payload: dict[str, Any],
        metrics: StrategyV2ShadowMetrics,
    ) -> list[float]:
        raw = payload.get("trade_net_pnl")
        if not isinstance(raw, list) or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in raw
        ):
            raise ValueError("forward trade PnL sequence is invalid")
        values = [float(value) for value in raw]
        if (
            any(not math.isfinite(value) for value in values)
            or len(values) != metrics.closed_trades
            or not math.isclose(
                sum(values),
                metrics.net_pnl,
                rel_tol=1e-9,
                abs_tol=1e-9,
            )
        ):
            raise ValueError("forward trade PnL sequence does not match metrics")
        cumulative = 0.0
        peak = 0.0
        drawdown = 0.0
        for value in values:
            cumulative += value
            peak = max(peak, cumulative)
            drawdown = max(drawdown, peak - cumulative)
        if not math.isclose(
            drawdown,
            metrics.max_drawdown,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise ValueError("forward trade PnL sequence drawdown does not match metrics")
        return values

    @staticmethod
    def _forward_exclusion_semantics_valid(
        row: StrategyV2ForwardEvidence,
    ) -> bool:
        nonstructural = {
            "FINALIZATION_WINDOW_MISSED",
            "COLLECTION_DISABLED",
            "TARGET_SESSION_INCOMPLETE",
            "IMMEDIATE_COMPLETE_SEED_UNAVAILABLE",
            "SEED_NOT_KNOWN_AT_TARGET_OPEN",
        }
        structural = {
            "EVALUATOR_DEFINITION_MISMATCH",
            "SOURCE_VERSION_SUPERSEDED",
            "TARGET_STATE_NOT_FLAT",
            "TARGET_EVIDENCE_NOT_KNOWN_AT_EVALUATION",
            "BASELINE_REPLAY_MISMATCH",
            "TARGET_INPUT_HASH_MISMATCH",
            "SESSION_LOCAL_FEATURE_DRIFT",
            "FORWARD_EVALUATION_FAILED",
        }
        expected_structural = row.exclusion_reason in structural
        return (
            row.exclusion_reason in nonstructural | structural
            and row.structural_failure is expected_structural
            and row.baseline_result_json == "{}"
            and row.candidate_result_json == "{}"
            and row.baseline_result_sha256 == ""
            and row.candidate_result_sha256 == ""
        )

    @staticmethod
    def _forward_validation_status(
        *,
        included: int,
        has_rows: bool,
        blockers: Sequence[str],
    ) -> Literal[
        "FROZEN",
        "COLLECTING",
        "READY_FOR_REVIEW",
        "MATURE_EVIDENCE",
        "BLOCKED",
    ]:
        if blockers:
            return "BLOCKED"
        if included >= _FORWARD_MATURE_PAIRS:
            return "MATURE_EVIDENCE"
        if included >= _FORWARD_READY_PAIRS:
            return "READY_FOR_REVIEW"
        return "COLLECTING" if has_rows else "FROZEN"

    @staticmethod
    def _aggregate_forward_metrics(
        values: Sequence[tuple[StrategyV2ShadowMetrics, Sequence[float]]],
    ) -> StrategyV2ShadowMetrics:
        if not values:
            return StrategyV2ShadowMetrics()
        metrics = [item for item, _sequence in values]
        closed = sum(item.closed_trades for item in metrics)
        cumulative = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for _item, sequence in values:
            for net_pnl in sequence:
                cumulative += net_pnl
                peak = max(peak, cumulative)
                max_drawdown = max(max_drawdown, peak - cumulative)
        return StrategyV2ShadowMetrics(
            bars=sum(item.bars for item in metrics),
            eligible_bars=sum(item.eligible_bars for item in metrics),
            breaches=sum(item.breaches for item in metrics),
            reclaims=sum(item.reclaims for item in metrics),
            entries=sum(item.entries for item in metrics),
            exits=sum(item.exits for item in metrics),
            closed_trades=closed,
            win_rate=(
                sum(item.win_rate * item.closed_trades for item in metrics) / closed
                if closed
                else 0.0
            ),
            gross_pnl=sum(item.gross_pnl for item in metrics),
            fees=sum(item.fees for item in metrics),
            net_pnl=sum(item.net_pnl for item in metrics),
            max_drawdown=max_drawdown,
            avg_holding_minutes=(
                sum(item.avg_holding_minutes * item.closed_trades for item in metrics)
                / closed
                if closed
                else 0.0
            ),
            avg_mae_pct=(
                sum(item.avg_mae_pct * item.closed_trades for item in metrics) / closed
                if closed
                else 0.0
            ),
            avg_mfe_pct=(
                sum(item.avg_mfe_pct * item.closed_trades for item in metrics) / closed
                if closed
                else 0.0
            ),
        )

    def _persist_forward_exclusion(
        self,
        *,
        registration: StrategyV2ForwardRegistration,
        target_day: date,
        target_open: datetime,
        evaluated_at: datetime,
        reason: str,
        structural: bool,
        target_bars: int = 0,
        target_hash: str = "",
        seed_hash: str = "",
        seed_day: date | None = None,
        baseline_input_hash: str = "",
        candidate_input_hash: str = "",
        baseline_match: bool | None = None,
        local_invariant: bool | None = None,
    ) -> None:
        row = StrategyV2ForwardEvidence(
            registration_id=registration.id,
            target_session_date=target_day,
            seed_session_date=seed_day,
            target_open_at=target_open,
            evaluated_at=evaluated_at,
            disposition="EXCLUDED",
            exclusion_reason=reason,
            structural_failure=structural,
            target_bars=target_bars,
            target_bars_sha256=target_hash,
            seed_bars_sha256=seed_hash,
            baseline_input_sha256=baseline_input_hash,
            candidate_input_sha256=candidate_input_hash,
            same_target_bars=(
                bool(target_hash)
                and target_hash == baseline_input_hash == candidate_input_hash
            ),
            baseline_replay_match=baseline_match,
            session_local_invariant=local_invariant,
            baseline_result_json="{}",
            candidate_result_json="{}",
            baseline_result_sha256="",
            candidate_result_sha256="",
        )
        row.evidence_digest_sha256 = self._forward_evidence_digest(row)
        self.db.add(row)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()

    def _record_forward_missed_targets(
        self,
        registration: StrategyV2ForwardRegistration,
        current: datetime,
        *,
        reason: str,
        structural: bool,
    ) -> bool:
        """Append MISSED markers for elapsed deadlines without reading bar evidence."""
        session = get_session(registration.market)
        current_utc = _as_utc(current)
        first_day = session.trade_day(_as_utc(registration.eligible_after))
        last_day = session.trade_day(current_utc)
        existing = {
            item
            for (item,) in self.db.query(
                StrategyV2ForwardEvidence.target_session_date
            ).filter(
                StrategyV2ForwardEvidence.registration_id == registration.id
            ).all()
        }
        candidate_day = first_day
        while candidate_day <= last_day:
            target_open = datetime.combine(
                candidate_day,
                session.rth_open,
                tzinfo=session.timezone,
            ).astimezone(timezone.utc)
            deadline = datetime.combine(
                candidate_day,
                session.close_time(candidate_day),
                tzinfo=session.timezone,
            ).astimezone(timezone.utc) + timedelta(
                minutes=_POST_CLOSE_COLLECTION_MINUTES
            )
            if (
                target_open >= _as_utc(registration.eligible_after)
                and session.is_rth(target_open)
                and current_utc >= deadline
                and candidate_day not in existing
            ):
                self._persist_forward_exclusion(
                    registration=registration,
                    target_day=candidate_day,
                    target_open=target_open,
                    evaluated_at=current_utc,
                    reason=reason,
                    structural=structural,
                )
                return True
            candidate_day += timedelta(days=1)
        return False

    def replay(self, payload: StrategyV2ShadowReplayRequest) -> StrategyV2ShadowReplayResponse:
        """Evaluate supplied bars without mutating any persistent shadow state."""
        config = self.db.query(StrategyV2ShadowConfig).filter(
            StrategyV2ShadowConfig.symbol == payload.symbol
        ).first()
        if config is None:
            config = self._transient_config(payload.symbol)
        self._validate_minimum_net_edge(self._config_values(config))
        return self._replay_payload(payload, config)

    def _evaluate_candles(
        self,
        *,
        config: StrategyV2ShadowConfig,
        state: StrategyV2ShadowState,
        market: str,
        one_minute: list[Any],
        observed_at: datetime,
    ) -> None:
        if not one_minute:
            raise ValueError("empty candle response")
        bars = self._coerce_strategy_bars(one_minute, symbol=config.symbol)
        current_version = self._config_version(config)
        activation_at = _as_utc(config.updated_at)
        engine = StrategyV2Engine(self._domain_config(config, market))
        session = get_session(market)
        grace = timedelta(seconds=engine.config.settlement_grace_seconds)
        bars = [
            bar
            for bar in bars
            if session.is_rth(bar.timestamp) and bar.end_at + grace <= observed_at
        ]
        if not bars:
            raise ValueError("no processable one-minute bars")

        open_trade = self._open_trade(config.symbol)
        decision_version = (
            open_trade.config_version if open_trade is not None else current_version
        )
        version_transition_open = open_trade is not None and (
            decision_version != current_version
            or state.config_version != current_version
        )
        manage_existing_position_only = open_trade is not None and (
            not config.enabled or version_transition_open
        )
        restored = False
        last_bar_at = _as_utc(state.last_bar_at) if state.last_bar_at is not None else None

        # A flat old algorithm must begin strictly at this deployment's first
        # observation. It must not replay downtime under the new code version.
        if open_trade is None and state.config_version != current_version:
            self._ensure_version_snapshot(config)
            self._reset_state_forward(
                state,
                config_version=current_version,
                watermark=self._forward_watermark(market, observed_at),
            )
            self.db.commit()
            return

        if last_bar_at is not None:
            for bar in bars:
                if bar.timestamp <= last_bar_at:
                    engine.features.on_bar(bar, observed_at=observed_at)

        if state.state_json and state.state_json != "{}" and (
            state.config_version == decision_version or open_trade is not None
        ):
            try:
                snapshot_payload = json.loads(state.state_json)
                if not isinstance(snapshot_payload, dict):
                    raise ValueError("shadow engine snapshot must be an object")
                engine.restore(snapshot_payload)
                restored = True
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                if state.phase not in {"", "FLAT", StrategyV2State.COLD.value}:
                    raise ValueError("persisted shadow engine state cannot be restored") from exc
                engine = StrategyV2Engine(self._domain_config(config, market))
        if open_trade is not None and (
            not restored or engine.state != StrategyV2State.LONG
        ):
            raise ValueError("open shadow trade requires a restorable LONG engine state")
        if open_trade is None and engine.state == StrategyV2State.LONG:
            raise ValueError("LONG shadow engine state has no persisted open trade")
        existing_keys = {
            key
            for (key,) in self.db.query(StrategyV2ShadowDecision.idempotency_key)
            .filter(
                StrategyV2ShadowDecision.symbol == config.symbol,
                StrategyV2ShadowDecision.config_version == decision_version,
            )
            .all()
        }
        armed_at: datetime | None = state.armed_at if restored else None
        armed_zscore: float | None = state.armed_zscore if restored else None
        latest_feature: Any = None
        quarantined_feature: Any = None
        quarantined_until: datetime | None = None
        exited_managed_position = False
        gap_error = ""
        frontier = last_bar_at

        for bar in bars:
            if last_bar_at is not None and bar.timestamp <= last_bar_at:
                continue
            if last_bar_at is None and bar.timestamp < activation_at:
                engine.features.on_bar(bar, observed_at=observed_at)
                continue
            if frontier is not None:
                missing_at = self._missing_rth_minute(
                    market,
                    previous=frontier,
                    current=bar.timestamp,
                )
                if missing_at is not None:
                    if engine.position is None and self._session_has_closed(
                        market,
                        missing_at,
                        observed_at,
                        grace,
                    ):
                        if (
                            session.trade_day(bar.timestamp)
                            == session.trade_day(missing_at)
                        ):
                            quarantined_feature = engine.features.on_bar(
                                bar,
                                observed_at=observed_at,
                            )
                        quarantined_until = self._session_last_bar_at(
                            market,
                            missing_at,
                        )
                        gap_error = (
                            "DATA_SESSION_QUARANTINED:"
                            f"{session.trade_day(missing_at).isoformat()};"
                            f"missing={missing_at.isoformat()}"
                        )
                        break
                    if engine.position is None:
                        gap_error = f"DATA_GAP_WAITING:{missing_at.isoformat()}"
                        break
            feature = engine.features.on_bar(bar, observed_at=observed_at)
            frontier = bar.timestamp
            if feature is None:
                continue
            gate_reasons = engine.entry_gate_reasons(feature)
            if (
                "SESSION_DATA_INCOMPLETE" in gate_reasons
                and engine.position is None
            ):
                if self._session_has_closed(
                    market,
                    bar.timestamp,
                    observed_at,
                    grace,
                ):
                    quarantined_feature = feature
                    quarantined_until = self._session_last_bar_at(
                        market,
                        bar.timestamp,
                    )
                    gap_error = (
                        "DATA_SESSION_QUARANTINED:"
                        f"{feature.session_day.isoformat()};"
                        f"incomplete={bar.timestamp.isoformat()}"
                    )
                else:
                    gap_error = (
                        "SESSION_DATA_INCOMPLETE_WAITING:"
                        f"{bar.timestamp.isoformat()}"
                    )
                break
            before_step = engine.snapshot()
            step = engine.on_feature(feature)
            latest_feature = feature
            for index, decision in enumerate(step.decisions):
                if decision.action == StrategyV2Action.ARM_LONG:
                    armed_at = decision.timestamp
                    armed_zscore = feature.zscore_1m
                elif decision.action in {
                    StrategyV2Action.CANCEL_ARM,
                    StrategyV2Action.SUBMIT_ENTRY,
                }:
                    armed_at = None
                    armed_zscore = None
                key = self._decision_key(
                    symbol=config.symbol,
                    config_version=decision_version,
                    timestamp=feature.bar.timestamp,
                    index=index,
                    action=decision.action.value,
                )
                if key in existing_keys:
                    continue
                row = self._new_decision_row(
                    key=key,
                    symbol=config.symbol,
                    market=market,
                    config_version=decision_version,
                    feature=feature,
                    decision=decision,
                    gate_reasons=gate_reasons,
                    observed_at=observed_at,
                )
                self.db.add(row)
                self.db.flush()
                self._apply_virtual_trade(
                    row,
                    decision,
                    feature,
                    config,
                    market,
                    position=step.position,
                    pending_signal_vwap=before_step.pending_signal_vwap,
                )
                if (
                    manage_existing_position_only
                    and decision.action == StrategyV2Action.EXIT_LONG
                ):
                    exited_managed_position = True
                existing_keys.add(key)
            if exited_managed_position:
                break

        if quarantined_until is not None:
            if quarantined_feature is not None:
                decision = StrategyV2Decision(
                    timestamp=quarantined_feature.bar.timestamp,
                    action=StrategyV2Action.WAIT,
                    reason="SESSION_DATA_INCOMPLETE",
                    state_before=engine.state,
                    state_after=StrategyV2State.COLD,
                )
                key = self._decision_key(
                    symbol=config.symbol,
                    config_version=decision_version,
                    timestamp=quarantined_feature.bar.timestamp,
                    index=0,
                    action=decision.action.value,
                )
                if key not in existing_keys:
                    gate_reasons = tuple(
                        dict.fromkeys((
                            *engine.entry_gate_reasons(quarantined_feature),
                            "SESSION_DATA_INCOMPLETE",
                        ))
                    )
                    self.db.add(self._new_decision_row(
                        key=key,
                        symbol=config.symbol,
                        market=market,
                        config_version=decision_version,
                        feature=quarantined_feature,
                        decision=decision,
                        gate_reasons=gate_reasons,
                        observed_at=observed_at,
                    ))
            self._reset_state_forward(
                state,
                config_version=current_version,
                watermark=quarantined_until,
            )
            state.session_date = session.trade_day(quarantined_until)
            state.last_poll_error = gap_error
            self.db.add(state)
            self.db.commit()
        elif latest_feature is not None:
            if exited_managed_position and version_transition_open:
                self._reset_state_forward(
                    state,
                    config_version=current_version,
                    watermark=latest_feature.bar.timestamp,
                )
                state.session_date = latest_feature.session_day
            else:
                state.config_version = decision_version
                state.session_date = latest_feature.session_day
                state.phase = engine.state.value
                state.last_bar_at = latest_feature.bar.timestamp
                state.armed_at = armed_at
                state.armed_zscore = armed_zscore
                state.entries_today = engine.entries_this_session
                remaining_trade = self._open_trade(config.symbol)
                state.open_trade_id = (
                    remaining_trade.id if remaining_trade is not None else None
                )
                state.last_entry_at = (
                    remaining_trade.entry_at
                    if remaining_trade is not None
                    else state.last_entry_at
                )
                state.state_json = json.dumps(
                    engine.snapshot().to_dict(),
                    sort_keys=True,
                )
            state.last_poll_error = gap_error
            self.db.add(state)
            self.db.commit()
        elif gap_error:
            state.last_poll_error = gap_error
            self.db.add(state)
            self.db.commit()

    @staticmethod
    def _in_post_close_collection_window(market: str, current: datetime) -> bool:
        if session_status(market, current) != "post":
            return False
        session = get_session(market)
        local = session.local(current)
        close_at = datetime.combine(
            local.date(),
            session.close_time(local.date()),
            tzinfo=session.timezone,
        )
        return close_at <= local < close_at + timedelta(
            minutes=_POST_CLOSE_COLLECTION_MINUTES
        )

    @staticmethod
    def _session_last_bar_at(market: str, instant: datetime) -> datetime:
        session = get_session(market)
        local = session.local(instant)
        close_at = datetime.combine(
            session.trade_day(instant),
            session.close_time(local.date()),
            tzinfo=session.timezone,
        )
        return close_at.astimezone(timezone.utc) - timedelta(minutes=1)

    @classmethod
    def _session_has_closed(
        cls,
        market: str,
        instant: datetime,
        observed_at: datetime,
        grace: timedelta,
    ) -> bool:
        return cls._session_last_bar_at(market, instant) + timedelta(
            minutes=1
        ) + grace <= observed_at

    @staticmethod
    def _missing_rth_minute(
        market: str,
        *,
        previous: datetime,
        current: datetime,
    ) -> datetime | None:
        previous_minute = _as_utc(previous).replace(second=0, microsecond=0)
        current_minute = _as_utc(current).replace(second=0, microsecond=0)
        expected = previous_minute + timedelta(minutes=1)
        if expected >= current_minute:
            return None
        session = get_session(market)
        if not session.is_rth(expected):
            expected = next_session_open(market, expected)
        return expected if expected < current_minute else None

    def _replay_payload(
        self,
        payload: StrategyV2ShadowReplayRequest,
        config: StrategyV2ShadowConfig,
        *,
        include_feature_evidence: bool = False,
        features: CausalTrendPrewarmFeatureEngine | None = None,
    ) -> StrategyV2ShadowReplayResponse:
        domain_config = self._domain_config(config, payload.market)
        engine = StrategyV2Engine(domain_config, features=features)
        bars = [
            StrategyBar(
                timestamp=item.timestamp,
                open=item.open,
                high=item.high,
                low=item.low,
                close=item.close,
                volume=item.volume,
                symbol=payload.symbol,
            )
            for item in sorted(payload.bars, key=lambda value: value.timestamp)
        ]
        decisions: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []
        open_trade: dict[str, Any] | None = None
        fee_rate = self._fee_rate(config, payload.market)

        for bar in bars:
            feature = engine.features.on_bar(
                bar,
                observed_at=bar.end_at + timedelta(seconds=engine.config.settlement_grace_seconds),
            )
            if feature is None:
                continue
            before_step = engine.snapshot()
            gate_reasons = engine.entry_gate_reasons(feature)
            step = engine.on_feature(feature)
            feature_evidence = (
                json.loads(json.dumps(asdict(feature), default=str, sort_keys=True))
                if include_feature_evidence
                else None
            )
            for decision in step.decisions:
                item = {
                    "timestamp": decision.timestamp.isoformat(),
                    "action": decision.action.value,
                    "reason": decision.reason,
                    "state_before": decision.state_before.value,
                    "state_after": decision.state_after.value,
                    "price": decision.price,
                    "quantity": decision.quantity,
                    "zscore_1m": feature.zscore_1m,
                    "zscore_5m": feature.zscore_5m,
                    "adx": feature.adx_5m,
                    "realized_vol": feature.realized_vol_1m,
                    "gate_passed": not gate_reasons,
                    "gate_reasons": list(gate_reasons),
                }
                if feature_evidence is not None:
                    item["_feature_evidence"] = feature_evidence
                decisions.append(item)
                if decision.action == StrategyV2Action.FILL_ENTRY and decision.price is not None:
                    entry_price = decision.price
                    entry_fee = entry_price * decision.quantity * fee_rate
                    position = step.position
                    signal_vwap = (
                        position.signal_vwap
                        if position is not None
                        else before_step.pending_signal_vwap
                    )
                    holding_deadline = (
                        position.holding_deadline
                        if position is not None
                        else decision.timestamp
                        + timedelta(minutes=engine.config.max_holding_minutes)
                    )
                    open_trade = {
                        "entry_at": decision.timestamp,
                        "entry_reason": decision.reason,
                        "entry_price": entry_price,
                        "quantity": decision.quantity,
                        "entry_fee": entry_fee,
                        "estimated_fee_rate": fee_rate,
                        "fee_source": "ESTIMATED",
                        "stop_price": (
                            position.stop_price if position is not None else decision.stop_price
                        ),
                        "target_price": (
                            position.target_price if position is not None else decision.target_price
                        ),
                        "signal_vwap": signal_vwap,
                        "holding_deadline": holding_deadline,
                        # Fill occurs at the open. Intrabar ordering before and
                        # after that fill is unknowable, so the entry bar does
                        # not contribute an excursion.
                        "highest_price": entry_price,
                        "lowest_price": entry_price,
                    }
                elif open_trade is not None:
                    if decision.action == StrategyV2Action.EXIT_LONG and decision.price is not None:
                        self._update_replay_exit_excursion(open_trade, decision)
                        exit_price = decision.price
                        quantity = float(open_trade["quantity"])
                        gross = (exit_price - float(open_trade["entry_price"])) * quantity
                        exit_fee = exit_price * quantity * fee_rate
                        fees = float(open_trade["entry_fee"]) + exit_fee
                        entry_price = float(open_trade["entry_price"])
                        trades.append(
                            {
                                "entry_at": open_trade["entry_at"].isoformat(),
                                "entry_reason": open_trade["entry_reason"],
                                "exit_at": decision.timestamp.isoformat(),
                                "entry_price": entry_price,
                                "exit_price": exit_price,
                                "quantity": quantity,
                                "exit_reason": decision.reason,
                                "stop_price": open_trade["stop_price"],
                                "target_price": open_trade["target_price"],
                                "signal_vwap": open_trade["signal_vwap"],
                                "holding_deadline": (
                                    open_trade["holding_deadline"].isoformat()
                                ),
                                "estimated_fee_rate": fee_rate,
                                "fee_source": "ESTIMATED",
                                "gross_pnl": gross,
                                "fees": fees,
                                "net_pnl": gross - fees,
                                "holding_minutes": (
                                    decision.timestamp - open_trade["entry_at"]
                                ).total_seconds()
                                / 60,
                                "mfe_pct": (
                                    float(open_trade["highest_price"]) - entry_price
                                )
                                / entry_price,
                                "mae_pct": (
                                    float(open_trade["lowest_price"]) - entry_price
                                )
                                / entry_price,
                            }
                        )
                        open_trade = None
                    else:
                        self._update_replay_full_bar_excursion(open_trade, bar)

        return StrategyV2ShadowReplayResponse(
            persisted=False,
            config_version=self._config_version_for_symbol(config, payload.symbol),
            decisions=decisions,
            trades=trades,
            metrics=self._metrics_from_replay(decisions, trades),
        )

    @staticmethod
    def _domain_config(row: StrategyV2ShadowConfig, market: str) -> StrategyV2Config:
        return StrategyV2Config(
            market=market,
            zscore_window_1m=row.zscore_window_1m_bars,
            zscore_window_5m=row.zscore_window_5m_bars,
            adx_period=row.adx_period,
            realized_vol_window_1m=row.realized_vol_window_bars,
            breach_zscore_1m=row.breach_zscore,
            reclaim_zscore_1m=row.reclaim_zscore,
            five_minute_zscore_max=row.five_minute_zscore_max,
            adx_max=row.max_adx,
            realized_vol_min=row.min_realized_vol,
            realized_vol_max=row.max_realized_vol,
            arm_ttl_bars=row.arm_ttl_bars,
            stop_loss_pct=row.stop_loss_pct,
            profit_target_pct=row.profit_target_pct,
            max_holding_minutes=row.max_holding_minutes,
            entry_cutoff_minutes_before_close=row.entry_cutoff_minutes_before_close,
            flatten_minutes_before_close=row.flatten_minutes_before_close,
            max_entries_per_session=row.max_entries_per_day,
            entry_cooldown_minutes=row.entry_cooldown_minutes,
            virtual_quantity=1.0,
            slippage_bps=row.slippage_bps,
            settlement_grace_seconds=5,
        )

    @staticmethod
    def _coerce_strategy_bars(values: list[Any], *, symbol: str) -> list[StrategyBar]:
        by_timestamp: dict[datetime, StrategyBar] = {}
        for index, value in enumerate(values):
            try:
                timestamp = _as_utc(getattr(value, "timestamp"))
                bar = StrategyBar(
                    timestamp=timestamp,
                    open=float(getattr(value, "open")),
                    high=float(getattr(value, "high")),
                    low=float(getattr(value, "low")),
                    close=float(getattr(value, "close")),
                    volume=float(getattr(value, "volume", 0.0)),
                    symbol=symbol,
                )
            except (AttributeError, TypeError, ValueError, OverflowError) as exc:
                raise ValueError(
                    f"invalid one-minute candle at index {index}"
                ) from exc
            previous = by_timestamp.get(timestamp)
            if previous is not None and previous != bar:
                raise ValueError(f"conflicting duplicate one-minute bar at {timestamp.isoformat()}")
            by_timestamp[timestamp] = bar
        return [by_timestamp[key] for key in sorted(by_timestamp)]

    @staticmethod
    def _decision_key(
        *,
        symbol: str,
        config_version: str,
        timestamp: datetime,
        index: int,
        action: str,
    ) -> str:
        raw = f"{symbol}|{config_version}|{timestamp.isoformat()}|{index}|{action}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _new_decision_row(
        self,
        *,
        key: str,
        symbol: str,
        market: str,
        config_version: str,
        feature: Any,
        decision: StrategyV2Decision,
        gate_reasons: tuple[str, ...],
        observed_at: datetime,
    ) -> StrategyV2ShadowDecision:
        state_after = decision.state_after.value
        virtual_position = "LONG" if state_after == StrategyV2State.LONG.value else "FLAT"
        persisted_reasons = list(gate_reasons)
        if decision.action in {
            StrategyV2Action.WAIT,
            StrategyV2Action.CANCEL_ARM,
            StrategyV2Action.CANCEL_ENTRY,
        } and decision.reason not in persisted_reasons:
            persisted_reasons.append(decision.reason)
        return StrategyV2ShadowDecision(
            idempotency_key=key,
            symbol=symbol,
            market=market.upper(),
            config_version=config_version,
            session_date=feature.session_day,
            bar_at=feature.bar.timestamp,
            bar_at_5m=feature.bar_timestamp_5m,
            observed_at=observed_at,
            action=decision.action.value,
            reason=decision.reason,
            state_before=decision.state_before.value,
            state_after=state_after,
            close_price=feature.bar.close,
            vwap_1m=feature.session_vwap_1m,
            zscore_1m=feature.zscore_1m,
            vwap_5m=feature.session_vwap_5m,
            zscore_5m=feature.zscore_5m,
            adx_5m=feature.adx_5m,
            realized_vol_1m=feature.realized_vol_1m,
            gate_passed=not gate_reasons,
            breach_armed=state_after in {
                StrategyV2State.ARMED_LONG.value,
                StrategyV2State.ENTRY_PENDING.value,
            },
            virtual_position=virtual_position,
            reference_price=decision.price,
            quantity=decision.quantity,
            exit_reason=(
                decision.reason if decision.action == StrategyV2Action.EXIT_LONG else ""
            ),
            gate_reasons_json=json.dumps(persisted_reasons),
            features_json=json.dumps(asdict(feature), default=str, sort_keys=True),
        )

    def _apply_virtual_trade(
        self,
        row: StrategyV2ShadowDecision,
        decision: StrategyV2Decision,
        feature: Any,
        config: StrategyV2ShadowConfig,
        market: str,
        *,
        position: VirtualPosition | None = None,
        pending_signal_vwap: float | None = None,
    ) -> None:
        trade = self._open_trade(config.symbol)
        fee_rate = self._fee_rate(config, market)
        if decision.action == StrategyV2Action.FILL_ENTRY and decision.price is not None:
            if trade is not None:
                raise ValueError("shadow engine attempted a position add-on")
            entry_price = decision.price
            quantity = decision.quantity
            entry_fee = entry_price * quantity * fee_rate
            stop_price = position.stop_price if position is not None else decision.stop_price
            target_price = position.target_price if position is not None else decision.target_price
            signal_vwap = (
                position.signal_vwap
                if position is not None
                else pending_signal_vwap
            )
            holding_deadline = (
                position.holding_deadline
                if position is not None
                else decision.timestamp + timedelta(minutes=config.max_holding_minutes)
            )
            trade = StrategyV2ShadowTrade(
                symbol=config.symbol,
                config_version=row.config_version,
                entry_decision_id=row.id,
                status="OPEN",
                entry_at=decision.timestamp,
                entry_price=entry_price,
                quantity=quantity,
                stop_price=stop_price,
                target_price=target_price,
                signal_vwap=signal_vwap,
                holding_deadline=holding_deadline,
                entry_reason=decision.reason,
                estimated_fees=entry_fee,
                highest_price=entry_price,
                lowest_price=entry_price,
                fee_source="ESTIMATED",
                estimated_fee_rate=fee_rate,
            )
            self._recalculate_trade_excursion(trade)
            self.db.add(trade)
            row.fee = entry_fee
            row.net_pnl = -entry_fee
            row.reference_price = decision.price
            row.quantity = quantity
            row.virtual_position = "LONG"
            self.db.add(row)
            self.db.flush()
            return

        if decision.action != StrategyV2Action.EXIT_LONG or decision.price is None:
            if trade is not None:
                self._update_trade_full_bar_excursion(trade, feature.bar)
                self.db.add(trade)
            return
        if trade is None:
            raise ValueError("shadow exit has no persisted virtual entry")

        self._update_trade_exit_excursion(trade, decision)
        exit_price = decision.price
        quantity = trade.quantity
        gross = (exit_price - trade.entry_price) * quantity
        fee_rate = float(trade.estimated_fee_rate or fee_rate)
        trade.estimated_fee_rate = fee_rate
        exit_fee = exit_price * quantity * fee_rate
        total_fees = float(trade.estimated_fees or 0.0) + exit_fee
        net = gross - total_fees
        holding_seconds = max(
            0.0,
            (_as_utc(decision.timestamp) - _as_utc(trade.entry_at)).total_seconds(),
        )
        trade.status = "CLOSED"
        trade.exit_decision_id = row.id
        trade.exit_at = decision.timestamp
        trade.exit_price = exit_price
        trade.exit_reason = decision.reason
        trade.gross_pnl = gross
        trade.estimated_fees = total_fees
        trade.net_pnl = net
        trade.holding_seconds = holding_seconds
        row.gross_pnl = gross
        row.fee = total_fees
        row.net_pnl = net
        row.exit_reason = decision.reason
        row.holding_minutes = holding_seconds / 60
        row.mae_pct = trade.mae_pct
        row.mfe_pct = trade.mfe_pct
        row.virtual_position = "FLAT"
        self.db.add_all([trade, row])
        self.db.flush()

    @staticmethod
    def _update_trade_full_bar_excursion(
        trade: StrategyV2ShadowTrade,
        bar: StrategyBar,
    ) -> None:
        highest = max(float(trade.highest_price or trade.entry_price), bar.high)
        lowest = min(float(trade.lowest_price or trade.entry_price), bar.low)
        trade.highest_price = highest
        trade.lowest_price = lowest
        StrategyV2ShadowService._recalculate_trade_excursion(trade)

    @staticmethod
    def _update_trade_exit_excursion(
        trade: StrategyV2ShadowTrade,
        decision: StrategyV2Decision,
    ) -> None:
        if decision.price is None:
            return
        if decision.reason == "PRICE_STOP":
            stop = float(trade.stop_price or decision.price)
            trade.lowest_price = min(
                float(trade.lowest_price or trade.entry_price),
                decision.price,
                stop,
            )
        elif decision.reason == "PROFIT_TARGET":
            target = float(trade.target_price or decision.price)
            trade.highest_price = max(
                float(trade.highest_price or trade.entry_price),
                decision.price,
                target,
            )
        StrategyV2ShadowService._recalculate_trade_excursion(trade)

    @staticmethod
    def _recalculate_trade_excursion(trade: StrategyV2ShadowTrade) -> None:
        highest = float(trade.highest_price or trade.entry_price)
        lowest = float(trade.lowest_price or trade.entry_price)
        favorable = highest - trade.entry_price
        adverse = lowest - trade.entry_price
        trade.mfe_amount = favorable * trade.quantity
        trade.mae_amount = adverse * trade.quantity
        trade.mfe_pct = favorable / trade.entry_price
        trade.mae_pct = adverse / trade.entry_price

    @staticmethod
    def _fee_rate(config: StrategyV2ShadowConfig, market: str) -> float:
        return float(
            one_side_fee_rate(
                market,
                Decimal(str(config.estimated_fee_rate_us)),
                Decimal(str(config.estimated_fee_rate_hk)),
            )
        )

    @staticmethod
    def _update_replay_full_bar_excursion(
        trade: dict[str, Any],
        bar: StrategyBar,
    ) -> None:
        trade["highest_price"] = max(float(trade["highest_price"]), bar.high)
        trade["lowest_price"] = min(float(trade["lowest_price"]), bar.low)

    @staticmethod
    def _update_replay_exit_excursion(
        trade: dict[str, Any],
        decision: StrategyV2Decision,
    ) -> None:
        if decision.price is None:
            return
        if decision.reason == "PRICE_STOP":
            stop = float(trade.get("stop_price") or decision.price)
            trade["lowest_price"] = min(
                float(trade["lowest_price"]),
                decision.price,
                stop,
            )
        elif decision.reason == "PROFIT_TARGET":
            target = float(trade.get("target_price") or decision.price)
            trade["highest_price"] = max(
                float(trade["highest_price"]),
                decision.price,
                target,
            )

    @staticmethod
    def _metrics_from_replay(
        decisions: list[dict[str, Any]],
        trades: list[dict[str, Any]],
    ) -> StrategyV2ShadowMetrics:
        actions = [str(item.get("action", "")) for item in decisions]
        net_values = [float(item.get("net_pnl", 0.0)) for item in trades]
        cumulative = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for value in net_values:
            cumulative += value
            peak = max(peak, cumulative)
            max_drawdown = max(max_drawdown, peak - cumulative)
        return StrategyV2ShadowMetrics(
            bars=len({str(item.get("timestamp", "")) for item in decisions}),
            eligible_bars=len({
                str(item.get("timestamp", ""))
                for item in decisions
                if bool(item.get("gate_passed", False))
            }),
            breaches=actions.count(StrategyV2Action.ARM_LONG.value),
            reclaims=actions.count(StrategyV2Action.SUBMIT_ENTRY.value),
            entries=actions.count(StrategyV2Action.FILL_ENTRY.value),
            exits=actions.count(StrategyV2Action.EXIT_LONG.value),
            closed_trades=len(trades),
            win_rate=(sum(value > 0 for value in net_values) / len(net_values)) if net_values else 0.0,
            gross_pnl=sum(float(item.get("gross_pnl", 0.0)) for item in trades),
            fees=sum(float(item.get("fees", 0.0)) for item in trades),
            net_pnl=sum(net_values),
            max_drawdown=max_drawdown,
            avg_holding_minutes=(
                sum(float(item.get("holding_minutes", 0.0)) for item in trades) / len(trades)
                if trades
                else 0.0
            ),
            avg_mae_pct=(
                sum(float(item.get("mae_pct", 0.0)) for item in trades) / len(trades)
                if trades
                else 0.0
            ),
            avg_mfe_pct=(
                sum(float(item.get("mfe_pct", 0.0)) for item in trades) / len(trades)
                if trades
                else 0.0
            ),
        )

    @staticmethod
    def _challenger_config(
        *,
        symbol: str,
        params: dict[str, Any],
        max_adx: float,
    ) -> StrategyV2ShadowConfig:
        values = dict(params)
        values.update({
            "enabled": False,
            "symbol": symbol,
            "max_adx": max_adx,
        })
        validated = StrategyV2ShadowConfigValues.model_validate(values)
        return StrategyV2ShadowConfig(
            **{
                field: getattr(validated, field)
                for field in _CONFIG_FIELDS
            },
            updated_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _bars_from_persisted_evidence(
        decisions: list[StrategyV2ShadowDecision],
        *,
        symbol: str,
    ) -> list[StrategyV2ReplayBar]:
        by_timestamp: dict[datetime, StrategyBar] = {}
        expected_timestamps: set[datetime] = set()
        for row in decisions:
            row_timestamp = _as_utc(row.bar_at).replace(second=0, microsecond=0)
            expected_timestamps.add(row_timestamp)
            try:
                decoded = json.loads(row.features_json)
                raw_bar = decoded["bar"]
                if not isinstance(decoded, dict) or not isinstance(raw_bar, dict):
                    raise TypeError("feature payload is not an object")
                bar = StrategyBar(
                    timestamp=datetime.fromisoformat(str(raw_bar["timestamp"])),
                    open=float(raw_bar["open"]),
                    high=float(raw_bar["high"]),
                    low=float(raw_bar["low"]),
                    close=float(raw_bar["close"]),
                    volume=float(raw_bar["volume"]),
                    symbol=str(raw_bar["symbol"]),
                    duration_minutes=int(raw_bar["duration_minutes"]),
                )
            except (
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
                OverflowError,
            ) as exc:
                raise ValueError(
                    f"invalid persisted shadow feature evidence at decision {row.id}"
                ) from exc
            if (
                bar.duration_minutes != 1
                or bar.symbol.strip().upper() != symbol
                or bar.timestamp != row_timestamp
                or not math.isclose(
                    bar.close,
                    float(row.close_price),
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
            ):
                raise ValueError(
                    f"persisted shadow feature evidence conflicts at decision {row.id}"
                )
            previous = by_timestamp.get(bar.timestamp)
            if previous is not None and previous != bar:
                raise ValueError(
                    f"conflicting persisted shadow bars at {bar.timestamp.isoformat()}"
                )
            by_timestamp[bar.timestamp] = bar
        if set(by_timestamp) != expected_timestamps:
            raise ValueError("persisted shadow feature evidence is incomplete")
        return [
            StrategyV2ReplayBar(
                timestamp=bar.timestamp,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            for bar in (by_timestamp[key] for key in sorted(by_timestamp))
        ]

    @classmethod
    def _challenger_daily(
        cls,
        replay: StrategyV2ShadowReplayResponse,
        market: str,
        session_dates: list[date],
    ) -> list[StrategyV2AdxChallengerDaily]:
        session = get_session(market)
        decisions_by_day: dict[date, list[dict[str, Any]]] = {
            item: [] for item in session_dates
        }
        trades_by_day: dict[date, list[dict[str, Any]]] = {
            item: [] for item in session_dates
        }
        for item in replay.decisions:
            timestamp = _as_utc(datetime.fromisoformat(str(item["timestamp"])))
            session_day = session.trade_day(timestamp)
            if session_day in decisions_by_day:
                decisions_by_day[session_day].append(item)
        for item in replay.trades:
            timestamp = _as_utc(datetime.fromisoformat(str(item["exit_at"])))
            session_day = session.trade_day(timestamp)
            if session_day in trades_by_day:
                trades_by_day[session_day].append(item)
        result: list[StrategyV2AdxChallengerDaily] = []
        for session_day in session_dates:
            day_decisions = decisions_by_day[session_day]
            day_trades = trades_by_day[session_day]
            metrics = cls._metrics_from_replay(day_decisions, day_trades)
            exits = Counter(str(item.get("exit_reason") or "UNKNOWN") for item in day_trades)
            result.append(StrategyV2AdxChallengerDaily(
                session_date=session_day,
                bars=metrics.bars,
                eligible_bars=metrics.eligible_bars,
                breaches=metrics.breaches,
                reclaims=metrics.reclaims,
                closed_trades=metrics.closed_trades,
                net_pnl=metrics.net_pnl,
                max_drawdown=metrics.max_drawdown,
                exit_reasons=dict(sorted(exits.items())),
            ))
        return result

    @staticmethod
    def _empty_warmup_diagnostic(
        *,
        status: str = "INSUFFICIENT_EVIDENCE",
        blockers: Sequence[str] = ("MIN_CAUSAL_PAIRS",),
        observed_causal_pairs: int = 0,
    ) -> StrategyV2WarmupDiagnostic:
        normalized_status = (
            "BLOCKED" if status == "BLOCKED" else "INSUFFICIENT_EVIDENCE"
        )
        return StrategyV2WarmupDiagnostic(
            status=normalized_status,
            minimum_causal_pairs=_MIN_WARMUP_CAUSAL_PAIRS,
            observed_causal_pairs=observed_causal_pairs,
            evaluated_causal_pairs=0,
            blockers=list(dict.fromkeys(blockers)),
        )

    @staticmethod
    def _strategy_bars_from_replay(
        bars: Sequence[StrategyV2ReplayBar],
        *,
        symbol: str,
    ) -> list[StrategyBar]:
        return [
            StrategyBar(
                timestamp=item.timestamp,
                open=item.open,
                high=item.high,
                low=item.low,
                close=item.close,
                volume=item.volume,
                symbol=symbol,
            )
            for item in sorted(bars, key=lambda value: value.timestamp)
        ]

    @staticmethod
    def _causal_warmup_pairs(
        *,
        bars: Sequence[StrategyBar],
        market: str,
        session_dates: Sequence[date],
        config: StrategyV2ShadowConfig,
    ) -> tuple[int, list[tuple[date, date, list[StrategyBar], list[StrategyBar]]]]:
        session = get_session(market)
        by_day: dict[date, list[StrategyBar]] = {item: [] for item in session_dates}
        for bar in bars:
            session_day = session.trade_day(bar.timestamp)
            if session_day in by_day:
                by_day[session_day].append(bar)
        immediate_pairs = 0
        result: list[tuple[date, date, list[StrategyBar], list[StrategyBar]]] = []
        ordered_dates = sorted(session_dates)
        for seed_day, target_day in zip(ordered_dates, ordered_dates[1:]):
            seed = sorted(by_day[seed_day], key=lambda item: item.timestamp)
            target = sorted(by_day[target_day], key=lambda item: item.timestamp)
            if not seed or not target:
                continue
            expected_open = next_session_open(market, seed[-1].end_at)
            if (
                session.trade_day(expected_open) != target_day
                or target[0].timestamp != expected_open
            ):
                continue
            immediate_pairs += 1
            completed_5m = aggregate_complete_five_minute_bars(
                seed,
                market=market,
                observed_at=seed[-1].end_at,
            )
            valid_returns = sum(
                current.timestamp - previous.timestamp == timedelta(minutes=1)
                and session.trade_day(current.timestamp)
                == session.trade_day(previous.timestamp)
                for previous, current in zip(seed, seed[1:])
            )
            if (
                len(completed_5m) < 2 * config.adx_period
                or valid_returns < config.realized_vol_window_bars
            ):
                continue
            result.append((seed_day, target_day, seed, target))
        return immediate_pairs, result

    @staticmethod
    def _replay_feature_map(
        replay: StrategyV2ShadowReplayResponse,
    ) -> dict[datetime, dict[str, Any]]:
        result: dict[datetime, dict[str, Any]] = {}
        for item in replay.decisions:
            timestamp = _as_utc(datetime.fromisoformat(str(item["timestamp"])))
            raw_feature = item.get("_feature_evidence")
            if not isinstance(raw_feature, dict):
                raise ValueError("warm-up replay is missing feature evidence")
            existing = result.get(timestamp)
            if existing is not None and not StrategyV2ShadowService._feature_evidence_matches(
                existing,
                raw_feature,
            ):
                raise ValueError("warm-up replay has conflicting feature evidence")
            result[timestamp] = raw_feature
        return result

    @classmethod
    def _session_local_features_match(
        cls,
        baseline: StrategyV2ShadowReplayResponse,
        prewarmed: StrategyV2ShadowReplayResponse,
    ) -> bool:
        baseline_features = cls._replay_feature_map(baseline)
        prewarmed_features = cls._replay_feature_map(prewarmed)
        if set(baseline_features) != set(prewarmed_features):
            return False
        local_fields = (
            "session_day",
            "bar_index",
            "bar_timestamp_5m",
            "session_vwap_1m",
            "residual_1m",
            "residual_mean_1m",
            "residual_sigma_1m",
            "zscore_1m",
            "session_vwap_5m",
            "residual_5m",
            "residual_mean_5m",
            "residual_sigma_5m",
            "zscore_5m",
        )
        return all(
            all(
                field in baseline_features[timestamp]
                and field in prewarmed_features[timestamp]
                and cls._feature_evidence_matches(
                    baseline_features[timestamp][field],
                    prewarmed_features[timestamp][field],
                    (field,),
                )
                for field in local_fields
            )
            for timestamp in baseline_features
        )

    @classmethod
    def _warmup_daily_from_replay(
        cls,
        *,
        replay: StrategyV2ShadowReplayResponse,
        market: str,
        seed_day: date,
        target_day: date,
        seed_bars: Sequence[StrategyBar],
        target_bars: Sequence[StrategyBar],
    ) -> StrategyV2WarmupDaily:
        features = cls._replay_feature_map(replay)
        expected_timestamps = {item.timestamp for item in target_bars}
        if set(features) != expected_timestamps:
            raise ValueError("warm-up replay target evidence is incomplete")
        decisions_by_timestamp: dict[datetime, list[dict[str, Any]]] = {}
        for item in replay.decisions:
            timestamp = _as_utc(datetime.fromisoformat(str(item["timestamp"])))
            decisions_by_timestamp.setdefault(timestamp, []).append(item)
        records: dict[datetime, tuple[bool, bool, set[str]]] = {}
        for timestamp in sorted(expected_timestamps):
            raw_ready = features[timestamp].get("ready")
            if not isinstance(raw_ready, bool):
                raise ValueError("warm-up replay readiness evidence is invalid")
            gates: set[str] = set()
            gate_passed = False
            for item in decisions_by_timestamp[timestamp]:
                raw_gates = item.get("gate_reasons")
                if not isinstance(raw_gates, list):
                    raise ValueError("warm-up replay gate evidence is invalid")
                gates.update(
                    str(value)
                    for value in raw_gates
                    if str(value) in _ENTRY_GATE_REASONS
                )
                gate_passed = gate_passed or bool(item.get("gate_passed", False))
            records[timestamp] = (raw_ready, raw_ready and gate_passed, gates)

        ordered = sorted(expected_timestamps)
        ready_timestamps = [item for item in ordered if records[item][0]]
        eligible_timestamps = [item for item in ordered if records[item][1]]
        session = get_session(market)
        timestamps_by_hour: dict[int, list[datetime]] = {}
        for timestamp in ordered:
            timestamps_by_hour.setdefault(
                timestamp.astimezone(session.timezone).hour,
                [],
            ).append(timestamp)
        hourly: list[StrategyV2ShadowHourlyEvidence] = []
        for market_hour, timestamps in sorted(timestamps_by_hour.items()):
            hourly_gate_counts: Counter[str] = Counter()
            for timestamp in timestamps:
                hourly_gate_counts.update(records[timestamp][2])
            hourly.append(StrategyV2ShadowHourlyEvidence(
                session_hour=market_hour,
                bars=len(timestamps),
                ready_bars=sum(records[item][0] for item in timestamps),
                eligible_bars=sum(records[item][1] for item in timestamps),
                gate_counts=dict(sorted(hourly_gate_counts.items())),
            ))
        first_ready_at = ready_timestamps[0] if ready_timestamps else None
        warmup_lost_bars = (
            ordered.index(first_ready_at)
            if first_ready_at is not None
            else len(ordered)
        )
        return StrategyV2WarmupDaily(
            session_date=target_day,
            seed_session_date=seed_day,
            trend_context_cutoff_at=seed_bars[-1].end_at,
            overnight_gap_pct=(target_bars[0].open / seed_bars[-1].close) - 1.0,
            first_ready_at=first_ready_at,
            bars=len(ordered),
            ready_bars=len(ready_timestamps),
            warmup_lost_bars=warmup_lost_bars,
            eligible_bars=len(eligible_timestamps),
            hourly_eligibility=hourly,
        )

    def _warmup_diagnostic(
        self,
        *,
        replay_bars: Sequence[StrategyV2ReplayBar],
        source_config: StrategyV2ShadowConfig,
        source_config_version: str,
        market: str,
        session_dates: Sequence[date],
    ) -> StrategyV2WarmupDiagnostic:
        strategy_bars = self._strategy_bars_from_replay(
            replay_bars,
            symbol=source_config.symbol,
        )
        observed_pairs, pairs = self._causal_warmup_pairs(
            bars=strategy_bars,
            market=market,
            session_dates=session_dates,
            config=source_config,
        )
        blockers = (
            ["MIN_CAUSAL_PAIRS"]
            if len(pairs) < _MIN_WARMUP_CAUSAL_PAIRS
            else []
        )
        if not pairs:
            return StrategyV2WarmupDiagnostic(
                status="INSUFFICIENT_EVIDENCE",
                minimum_causal_pairs=_MIN_WARMUP_CAUSAL_PAIRS,
                observed_causal_pairs=observed_pairs,
                evaluated_causal_pairs=0,
                blockers=blockers,
            )

        baseline_decisions: list[dict[str, Any]] = []
        baseline_trades: list[dict[str, Any]] = []
        baseline_daily: list[StrategyV2WarmupDaily] = []
        prewarm_decisions: list[dict[str, Any]] = []
        prewarm_trades: list[dict[str, Any]] = []
        prewarm_daily: list[StrategyV2WarmupDaily] = []
        domain_config = self._domain_config(source_config, market)
        try:
            for seed_day, target_day, seed_bars, target_bars in pairs:
                target_payload = StrategyV2ShadowReplayRequest(
                    symbol=source_config.symbol,
                    market="HK" if market == "HK" else "US",
                    bars=[
                        StrategyV2ReplayBar(
                            timestamp=item.timestamp,
                            open=item.open,
                            high=item.high,
                            low=item.low,
                            close=item.close,
                            volume=item.volume,
                        )
                        for item in target_bars
                    ],
                )
                baseline = self._replay_payload(
                    target_payload,
                    source_config,
                    include_feature_evidence=True,
                )
                prewarmed = self._replay_payload(
                    target_payload,
                    source_config,
                    include_feature_evidence=True,
                    features=CausalTrendPrewarmFeatureEngine(
                        domain_config.feature_config(),
                        seed_bars,
                    ),
                )
                if not self._session_local_features_match(baseline, prewarmed):
                    return StrategyV2WarmupDiagnostic(
                        status="BLOCKED",
                        minimum_causal_pairs=_MIN_WARMUP_CAUSAL_PAIRS,
                        observed_causal_pairs=observed_pairs,
                        evaluated_causal_pairs=0,
                        blockers=["SESSION_LOCAL_FEATURE_DRIFT"],
                    )
                baseline_decisions.extend(baseline.decisions)
                baseline_trades.extend(baseline.trades)
                prewarm_decisions.extend(prewarmed.decisions)
                prewarm_trades.extend(prewarmed.trades)
                baseline_daily.append(self._warmup_daily_from_replay(
                    replay=baseline,
                    market=market,
                    seed_day=seed_day,
                    target_day=target_day,
                    seed_bars=seed_bars,
                    target_bars=target_bars,
                ))
                prewarm_daily.append(self._warmup_daily_from_replay(
                    replay=prewarmed,
                    market=market,
                    seed_day=seed_day,
                    target_day=target_day,
                    seed_bars=seed_bars,
                    target_bars=target_bars,
                ))
        except (KeyError, TypeError, ValueError, OverflowError):
            return StrategyV2WarmupDiagnostic(
                status="BLOCKED",
                minimum_causal_pairs=_MIN_WARMUP_CAUSAL_PAIRS,
                observed_causal_pairs=observed_pairs,
                evaluated_causal_pairs=0,
                blockers=["PREWARM_REPLAY_FAILED"],
            )

        variants = [
            StrategyV2WarmupVariant(
                label="SESSION_LOCAL",
                warmup_scope="NONE",
                source_config_version=source_config_version,
                metrics=self._metrics_from_replay(
                    baseline_decisions,
                    baseline_trades,
                ),
                daily=baseline_daily,
            ),
            StrategyV2WarmupVariant(
                label="CAUSAL_TREND_PREWARM",
                warmup_scope="ADX_VOL_ONLY",
                source_config_version=source_config_version,
                metrics=self._metrics_from_replay(
                    prewarm_decisions,
                    prewarm_trades,
                ),
                daily=prewarm_daily,
            ),
        ]
        return StrategyV2WarmupDiagnostic(
            status=(
                "READY_FOR_REVIEW"
                if len(pairs) >= _MIN_WARMUP_CAUSAL_PAIRS
                else "INSUFFICIENT_EVIDENCE"
            ),
            minimum_causal_pairs=_MIN_WARMUP_CAUSAL_PAIRS,
            observed_causal_pairs=observed_pairs,
            evaluated_causal_pairs=len(pairs),
            blockers=blockers,
            variants=variants,
        )

    def _baseline_replay_matches(
        self,
        *,
        decisions: list[StrategyV2ShadowDecision],
        trades: list[StrategyV2ShadowTrade],
        replay: StrategyV2ShadowReplayResponse,
        market: str,
        session_dates: set[date],
    ) -> bool:
        if len(decisions) != len(replay.decisions):
            return False
        for expected, actual in zip(decisions, replay.decisions):
            try:
                expected_features = json.loads(expected.features_json)
                actual_at = _as_utc(
                    datetime.fromisoformat(str(actual["timestamp"]))
                ).isoformat()
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return False
            if (
                not isinstance(expected_features, dict)
                or not StrategyV2ShadowService._feature_evidence_matches(
                    expected_features,
                    actual.get("_feature_evidence"),
                )
                or _as_utc(expected.bar_at).isoformat() != actual_at
                or expected.action != str(actual.get("action", ""))
                or expected.reason != str(actual.get("reason", ""))
                or expected.state_before != str(actual.get("state_before", ""))
                or expected.state_after != str(actual.get("state_after", ""))
                or bool(expected.gate_passed)
                != bool(actual.get("gate_passed", False))
                or not StrategyV2ShadowService._optional_numbers_match(
                    expected.reference_price,
                    actual.get("price"),
                )
                or not StrategyV2ShadowService._optional_numbers_match(
                    expected.quantity,
                    actual.get("quantity"),
                )
            ):
                return False

        session = get_session(market)
        selected_trades = [
            item
            for item in trades
            if item.exit_at is not None
            and session.trade_day(_as_utc(item.exit_at)) in session_dates
        ]
        decisions_by_id = {item.id: item for item in decisions}
        entry_ids = [
            int(item.entry_decision_id)
            for item in selected_trades
            if item.entry_decision_id is not None
        ]
        exit_ids = [
            int(item.exit_decision_id)
            for item in selected_trades
            if item.exit_decision_id is not None
        ]
        if (
            len(entry_ids) != len(selected_trades)
            or len(exit_ids) != len(selected_trades)
        ):
            return False
        entry_linkage_counts = {
            int(decision_id): int(count)
            for decision_id, count in self.db.query(
                StrategyV2ShadowTrade.entry_decision_id,
                func.count(StrategyV2ShadowTrade.id),
            ).filter(
                StrategyV2ShadowTrade.symbol == decisions[0].symbol,
                StrategyV2ShadowTrade.config_version == decisions[0].config_version,
                StrategyV2ShadowTrade.entry_decision_id.in_(entry_ids),
            ).group_by(StrategyV2ShadowTrade.entry_decision_id).all()
        }
        exit_linkage_counts = {
            int(decision_id): int(count)
            for decision_id, count in self.db.query(
                StrategyV2ShadowTrade.exit_decision_id,
                func.count(StrategyV2ShadowTrade.id),
            ).filter(
                StrategyV2ShadowTrade.symbol == decisions[0].symbol,
                StrategyV2ShadowTrade.config_version == decisions[0].config_version,
                StrategyV2ShadowTrade.exit_decision_id.in_(exit_ids),
            ).group_by(StrategyV2ShadowTrade.exit_decision_id).all()
        }
        expected_trades: list[StrategyV2ShadowTrade] = []
        for trade in selected_trades:
            entry_id = trade.entry_decision_id
            exit_id = trade.exit_decision_id
            if (
                entry_id is None
                or exit_id is None
                or entry_linkage_counts[entry_id] != 1
                or exit_linkage_counts[exit_id] != 1
                or StrategyV2ShadowService._trade_evidence_session_date(
                    trade,
                    decisions_by_id,
                )
                not in session_dates
            ):
                return False
            expected_trades.append(trade)
        if len(expected_trades) != len(replay.trades):
            return False
        for expected, actual in zip(expected_trades, replay.trades):
            if expected.exit_at is None:
                return False
            try:
                actual_entry_at = _as_utc(
                    datetime.fromisoformat(str(actual["entry_at"]))
                ).isoformat()
                actual_exit_at = _as_utc(
                    datetime.fromisoformat(str(actual["exit_at"]))
                ).isoformat()
            except (KeyError, TypeError, ValueError):
                return False
            if (
                _as_utc(expected.entry_at).isoformat() != actual_entry_at
                or _as_utc(expected.exit_at).isoformat() != actual_exit_at
                or expected.entry_reason != str(actual.get("entry_reason", ""))
                or expected.exit_reason != str(actual.get("exit_reason", ""))
                or expected.fee_source != str(actual.get("fee_source", ""))
                or not StrategyV2ShadowService._optional_datetimes_match(
                    expected.holding_deadline,
                    actual.get("holding_deadline"),
                )
            ):
                return False
            numeric_pairs = (
                (expected.entry_price, actual.get("entry_price")),
                (expected.exit_price, actual.get("exit_price")),
                (expected.quantity, actual.get("quantity")),
                (expected.stop_price, actual.get("stop_price")),
                (expected.target_price, actual.get("target_price")),
                (expected.signal_vwap, actual.get("signal_vwap")),
                (expected.estimated_fee_rate, actual.get("estimated_fee_rate")),
                (expected.gross_pnl, actual.get("gross_pnl")),
                (expected.estimated_fees, actual.get("fees")),
                (expected.net_pnl, actual.get("net_pnl")),
                (
                    (
                        float(expected.holding_seconds) / 60
                        if expected.holding_seconds is not None
                        else None
                    ),
                    actual.get("holding_minutes"),
                ),
                (expected.mae_pct, actual.get("mae_pct")),
                (expected.mfe_pct, actual.get("mfe_pct")),
            )
            if any(
                not StrategyV2ShadowService._optional_numbers_match(left, right)
                for left, right in numeric_pairs
            ):
                return False
        return True

    @staticmethod
    def _feature_evidence_matches(
        expected: Any,
        actual: Any,
        path: tuple[str, ...] = (),
    ) -> bool:
        if isinstance(expected, dict):
            if not isinstance(actual, dict) or set(expected) != set(actual):
                return False
            return all(
                StrategyV2ShadowService._feature_evidence_matches(
                    value,
                    actual[key],
                    (*path, str(key)),
                )
                for key, value in expected.items()
            )
        if isinstance(expected, list):
            return (
                isinstance(actual, list)
                and len(expected) == len(actual)
                and all(
                    StrategyV2ShadowService._feature_evidence_matches(
                        left,
                        right,
                        (*path, str(index)),
                    )
                    for index, (left, right) in enumerate(zip(expected, actual))
                )
            )
        if isinstance(expected, bool) or isinstance(actual, bool):
            return type(expected) is type(actual) and expected == actual
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            if path and path[0] == "bar":
                return expected == actual
            return math.isclose(
                float(expected),
                float(actual),
                rel_tol=1e-9,
                abs_tol=1e-12,
            )
        return type(expected) is type(actual) and expected == actual

    @staticmethod
    def _optional_numbers_match(left: Any, right: Any) -> bool:
        if left is None or right is None:
            return left is None and right is None
        try:
            return math.isclose(
                float(left),
                float(right),
                rel_tol=1e-10,
                abs_tol=1e-10,
            )
        except (TypeError, ValueError, OverflowError):
            return False

    @staticmethod
    def _optional_datetimes_match(left: datetime | None, right: Any) -> bool:
        if left is None or right is None:
            return left is None and right is None
        try:
            return _as_utc(left).isoformat() == _as_utc(
                datetime.fromisoformat(str(right))
            ).isoformat()
        except (TypeError, ValueError):
            return False

    def _ensure_version_snapshot(
        self,
        row: StrategyV2ShadowConfig,
        *,
        commit: bool = True,
    ) -> StrategyV2ShadowVersion:
        version = self._config_version(row)
        existing = self.db.query(StrategyV2ShadowVersion).filter(
            StrategyV2ShadowVersion.symbol == row.symbol,
            StrategyV2ShadowVersion.config_version == version,
        ).first()
        if existing is not None:
            return existing
        params = self._config_values(row)
        params.pop("enabled", None)
        activated_at = _as_utc(row.updated_at)
        state = self.db.query(StrategyV2ShadowState).filter(
            StrategyV2ShadowState.symbol == row.symbol
        ).first()
        if (
            state is not None
            and state.config_version != version
        ):
            activated_at = datetime.now(timezone.utc)
        snapshot = StrategyV2ShadowVersion(
            symbol=row.symbol,
            config_version=version,
            config_json=json.dumps(params, sort_keys=True, separators=(",", ":")),
            activated_at=activated_at,
        )
        self.db.add(snapshot)
        if commit:
            try:
                self.db.commit()
            except IntegrityError:
                self.db.rollback()
                existing = self.db.query(StrategyV2ShadowVersion).filter(
                    StrategyV2ShadowVersion.symbol == row.symbol,
                    StrategyV2ShadowVersion.config_version == version,
                ).first()
                if existing is None:
                    raise
                return existing
            self.db.refresh(snapshot)
        return snapshot

    def _resolve_config_version(
        self,
        symbol: str,
        requested: str | None,
    ) -> str:
        config = self._get_or_create_config(symbol)
        self._ensure_version_snapshot(config)
        self._backfill_legacy_version_snapshots(symbol)
        if requested is None:
            open_trade = self._open_trade(symbol)
            return open_trade.config_version if open_trade is not None else self._config_version(config)
        normalized = requested.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("invalid strategy v2 shadow config_version")
        exists = self.db.query(StrategyV2ShadowVersion.id).filter(
            StrategyV2ShadowVersion.symbol == symbol,
            StrategyV2ShadowVersion.config_version == normalized,
        ).first()
        if exists is None:
            raise ValueError("strategy v2 shadow config_version was not found for symbol")
        return normalized

    def _resolve_existing_config_version(
        self,
        symbol: str,
        requested: str | None,
    ) -> str:
        """Resolve a snapshotted version without creating or backfilling rows."""
        config = self.db.query(StrategyV2ShadowConfig).filter(
            StrategyV2ShadowConfig.symbol == symbol
        ).first()
        if config is None:
            raise ValueError("strategy v2 shadow config was not found for symbol")
        if requested is None:
            open_trade = self._open_trade(symbol)
            normalized = (
                open_trade.config_version
                if open_trade is not None
                else self._config_version(config)
            )
        else:
            normalized = requested.strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", normalized):
                raise ValueError("invalid strategy v2 shadow config_version")
        exists = self.db.query(StrategyV2ShadowVersion.id).filter(
            StrategyV2ShadowVersion.symbol == symbol,
            StrategyV2ShadowVersion.config_version == normalized,
        ).first()
        if exists is None:
            raise ValueError(
                "strategy v2 shadow immutable config snapshot was not found for symbol"
            )
        return normalized

    def _version_params(self, symbol: str, config_version: str) -> dict[str, Any]:
        row = self.db.query(StrategyV2ShadowVersion).filter(
            StrategyV2ShadowVersion.symbol == symbol,
            StrategyV2ShadowVersion.config_version == config_version,
        ).first()
        if row is None:
            return {}
        try:
            payload = json.loads(row.config_json)
        except (TypeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _required_hk_profit_target(values: dict[str, Any]) -> float | None:
        symbol = str(values.get("symbol", "") or "").upper()
        if not symbol.endswith(".HK"):
            return None
        return minimum_profit_target_pct(
            one_side_fee_rate=float(values["estimated_fee_rate_hk"]),
            slippage_bps=float(values["slippage_bps"]),
        )

    @classmethod
    def _validate_minimum_net_edge(cls, values: dict[str, Any]) -> None:
        try:
            required = cls._required_hk_profit_target(values)
            target = float(values["profit_target_pct"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "strategy v2 shadow cost assumptions are incomplete"
            ) from exc
        if required is not None and target + 1e-12 < required:
            raise ValueError(
                "HK profit_target_pct must be at least "
                f"{required:.4f}% to cover round-trip fees, slippage, "
                "and the safety buffer"
            )

    @classmethod
    def _net_edge_blocker(
        cls,
        symbol: str,
        params: dict[str, Any],
    ) -> str | None:
        if not symbol.endswith(".HK"):
            return None
        values = dict(params)
        values.setdefault("symbol", symbol)
        try:
            cls._validate_minimum_net_edge(values)
        except ValueError as exc:
            return (
                "HK_MIN_NET_EDGE"
                if "must be at least" in str(exc)
                else "CONFIG_COST_SNAPSHOT_INCOMPLETE"
            )
        return None

    @staticmethod
    def _readiness_quality(
        trades: list[StrategyV2ShadowTrade],
        params: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, list[str]]:
        if not trades:
            return None, []
        net_values: list[float] = []
        for item in trades:
            if item.net_pnl is None:
                return {
                    "n_trades": len(trades),
                    "quality_data_complete": False,
                }, ["QUALITY_DATA_INCOMPLETE"]
            net_values.append(float(item.net_pnl))
        quality = strategy_quality_report(net_values).to_dict()
        cumulative = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for value in net_values:
            cumulative += value
            peak = max(peak, cumulative)
            max_drawdown = max(max_drawdown, peak - cumulative)
        total_net_pnl = sum(net_values)

        cost_stressed_net_pnl: float | None = 0.0
        try:
            slippage_bps = float(params["slippage_bps"])
        except (KeyError, TypeError, ValueError, OverflowError):
            cost_stressed_net_pnl = None
        if cost_stressed_net_pnl is not None:
            for item, net_pnl in zip(trades, net_values):
                exit_price = item.exit_price
                if exit_price is None:
                    cost_stressed_net_pnl = None
                    break
                quantity = float(item.quantity)
                notional = (float(item.entry_price) + float(exit_price)) * quantity
                estimated_fees = item.estimated_fees
                if estimated_fees is None:
                    if item.estimated_fee_rate is None:
                        cost_stressed_net_pnl = None
                        break
                    estimated_fees = notional * float(item.estimated_fee_rate)
                extra_slippage = notional * slippage_bps / 10_000
                cost_stressed_net_pnl += (
                    net_pnl - float(estimated_fees) - extra_slippage
                )

        quality.update({
            "quality_data_complete": True,
            "total_net_pnl": total_net_pnl,
            "max_drawdown": max_drawdown,
            "profit_to_drawdown_ratio": (
                total_net_pnl / max_drawdown if max_drawdown > 0 else None
            ),
            "cost_stressed_net_pnl": cost_stressed_net_pnl,
            "stress_fee_multiplier": 2.0,
            "stress_slippage_multiplier": 2.0,
        })
        blockers: list[str] = []
        if total_net_pnl <= 0:
            blockers.append("NET_PNL_NON_POSITIVE")
        if total_net_pnl > 0 and max_drawdown > total_net_pnl:
            blockers.append("MAX_DRAWDOWN_EXCEEDS_NET_PNL")
        if cost_stressed_net_pnl is None:
            blockers.append("COST_STRESS_UNAVAILABLE")
        elif cost_stressed_net_pnl <= 0:
            blockers.append("COST_STRESS_NET_PNL_NON_POSITIVE")
        return quality, blockers

    def _backfill_legacy_version_snapshots(self, symbol: str) -> None:
        """Keep pre-P2.1 evidence queryable when its parameters are unknowable."""
        known = {
            value
            for (value,) in self.db.query(StrategyV2ShadowVersion.config_version).filter(
                StrategyV2ShadowVersion.symbol == symbol
            ).all()
        }
        candidates: dict[str, datetime] = {}
        for version, created_at in self.db.query(
            StrategyV2ShadowDecision.config_version,
            StrategyV2ShadowDecision.created_at,
        ).filter(StrategyV2ShadowDecision.symbol == symbol).all():
            if re.fullmatch(r"[0-9a-f]{64}", str(version or "")):
                current = candidates.get(str(version))
                if current is None or _as_utc(created_at) < _as_utc(current):
                    candidates[str(version)] = created_at
        for version, created_at in self.db.query(
            StrategyV2ShadowTrade.config_version,
            StrategyV2ShadowTrade.created_at,
        ).filter(StrategyV2ShadowTrade.symbol == symbol).all():
            if re.fullmatch(r"[0-9a-f]{64}", str(version or "")):
                current = candidates.get(str(version))
                if current is None or _as_utc(created_at) < _as_utc(current):
                    candidates[str(version)] = created_at
        missing = sorted(set(candidates) - known)
        if not missing:
            return
        for version in missing:
            self.db.add(StrategyV2ShadowVersion(
                symbol=symbol,
                config_version=version,
                config_json=json.dumps({
                    "parameters_available": False,
                    "reason": "evidence predates immutable config snapshots",
                }, sort_keys=True, separators=(",", ":")),
                activated_at=candidates[version],
            ))
        self.db.commit()

    def _version_response(
        self,
        row: StrategyV2ShadowVersion,
        current_version: str,
    ) -> StrategyV2ShadowVersionResponse:
        metrics = self._metrics(row.symbol, row.config_version)
        decisions = self.db.query(StrategyV2ShadowDecision).filter(
            StrategyV2ShadowDecision.symbol == row.symbol,
            StrategyV2ShadowDecision.config_version == row.config_version,
        ).order_by(StrategyV2ShadowDecision.bar_at.asc()).all()
        trades = self.db.query(StrategyV2ShadowTrade).filter(
            StrategyV2ShadowTrade.symbol == row.symbol,
            StrategyV2ShadowTrade.config_version == row.config_version,
            StrategyV2ShadowTrade.status == "CLOSED",
        ).all()
        observed_days = sum(
            item.complete_session
            for item in self._daily_evidence(decisions, trades)
        )
        try:
            params = json.loads(row.config_json)
        except json.JSONDecodeError:
            params = {}
        return StrategyV2ShadowVersionResponse(
            symbol=row.symbol,
            config_version=row.config_version,
            activated_at=row.activated_at,
            current=row.config_version == current_version,
            params=params if isinstance(params, dict) else {},
            observed_trading_days=observed_days,
            bars=metrics.bars,
            closed_trades=metrics.closed_trades,
            net_pnl=metrics.net_pnl,
        )

    @staticmethod
    def _stored_gate_reasons(
        row: StrategyV2ShadowDecision | _DecisionEvidenceRow,
    ) -> set[str] | None:
        try:
            decoded = json.loads(row.gate_reasons_json or "[]")
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(decoded, list):
            return None
        return {str(item) for item in decoded}

    def _complete_session_dates(
        self,
        symbol: str,
        config_version: str,
    ) -> list[date]:
        query = self.db.query(
            StrategyV2ShadowDecision.session_date,
            StrategyV2ShadowDecision.market,
            StrategyV2ShadowDecision.bar_at,
            StrategyV2ShadowDecision.gate_passed,
            StrategyV2ShadowDecision.gate_reasons_json,
        ).filter(
            StrategyV2ShadowDecision.symbol == symbol,
            StrategyV2ShadowDecision.config_version == config_version,
        ).order_by(
            StrategyV2ShadowDecision.session_date.asc(),
            StrategyV2ShadowDecision.bar_at.asc(),
            StrategyV2ShadowDecision.id.asc(),
        ).execution_options(stream_results=True).yield_per(1000)
        complete_dates: list[date] = []
        day_rows: list[_DecisionEvidenceRow] = []

        def finish_day() -> None:
            if not day_rows:
                return
            daily = self._daily_evidence(day_rows, [])
            if daily and daily[0].complete_session:
                complete_dates.append(daily[0].session_date)

        current_date: date | None = None
        for session_date, market, bar_at, gate_passed, gate_reasons_json in query:
            if current_date is not None and session_date != current_date:
                finish_day()
                day_rows = []
            current_date = session_date
            day_rows.append(_DecisionEvidenceRow(
                session_date=session_date,
                market=market,
                bar_at=bar_at,
                gate_passed=gate_passed,
                gate_reasons_json=gate_reasons_json,
            ))
        finish_day()
        return complete_dates

    @staticmethod
    def _trade_evidence_session_date(
        trade: StrategyV2ShadowTrade,
        decisions_by_id: dict[int, StrategyV2ShadowDecision],
    ) -> date | None:
        if trade.entry_decision_id is None or trade.exit_decision_id is None:
            return None
        entry = decisions_by_id.get(trade.entry_decision_id)
        exit_row = decisions_by_id.get(trade.exit_decision_id)
        if entry is None or exit_row is None or trade.exit_at is None:
            return None
        if (
            entry.action != StrategyV2Action.FILL_ENTRY.value
            or exit_row.action != StrategyV2Action.EXIT_LONG.value
            or entry.symbol != trade.symbol
            or exit_row.symbol != trade.symbol
            or entry.config_version != trade.config_version
            or exit_row.config_version != trade.config_version
            or entry.market.upper() != exit_row.market.upper()
            or entry.session_date != exit_row.session_date
            or _as_utc(entry.bar_at) >= _as_utc(exit_row.bar_at)
            or _as_utc(trade.entry_at) >= _as_utc(trade.exit_at)
        ):
            return None
        session = get_session(entry.market)
        entry_at = _as_utc(entry.bar_at).replace(second=0, microsecond=0)
        exit_at = _as_utc(exit_row.bar_at).replace(second=0, microsecond=0)
        if (
            not session.is_rth(entry_at)
            or not session.is_rth(exit_at)
            or session.trade_day(entry_at) != entry.session_date
            or session.trade_day(exit_at) != entry.session_date
            or _as_utc(trade.entry_at).replace(second=0, microsecond=0) != entry_at
            or _as_utc(trade.exit_at).replace(second=0, microsecond=0) != exit_at
        ):
            return None
        return entry.session_date

    @staticmethod
    def _bar_readiness(
        rows: Sequence[StrategyV2ShadowDecision | _DecisionEvidenceRow],
    ) -> tuple[bool, bool, set[str]]:
        stored = [StrategyV2ShadowService._stored_gate_reasons(row) for row in rows]
        if any(item is None for item in stored):
            return False, False, {"READINESS_EVIDENCE_MALFORMED"}
        reasons = set().union(*(item or set() for item in stored))
        gate_passed = any(row.gate_passed for row in rows)
        if gate_passed and reasons & _ENTRY_GATE_REASONS:
            return False, False, {
                *reasons,
                "READINESS_EVIDENCE_MALFORMED",
            }
        ready = not bool(reasons & _FEATURE_NOT_READY_REASONS)
        eligible = ready and gate_passed
        return ready, eligible, reasons

    @staticmethod
    def _daily_evidence(
        decisions: Sequence[StrategyV2ShadowDecision | _DecisionEvidenceRow],
        trades: Sequence[StrategyV2ShadowTrade],
    ) -> list[StrategyV2ShadowDailyEvidence]:
        by_day: dict[
            Any,
            list[StrategyV2ShadowDecision | _DecisionEvidenceRow],
        ] = {}
        for row in decisions:
            by_day.setdefault(row.session_date, []).append(row)
        trades_by_day: dict[Any, list[StrategyV2ShadowTrade]] = {}
        for trade in trades:
            if trade.exit_at is not None:
                trades_by_day.setdefault(_as_utc(trade.exit_at).date(), []).append(trade)
        result: list[StrategyV2ShadowDailyEvidence] = []
        for session_date, rows in sorted(by_day.items()):
            rows_by_timestamp: dict[
                datetime,
                list[StrategyV2ShadowDecision | _DecisionEvidenceRow],
            ] = {}
            for row in rows:
                timestamp = _as_utc(row.bar_at).replace(second=0, microsecond=0)
                rows_by_timestamp.setdefault(timestamp, []).append(row)
            timestamps = sorted(rows_by_timestamp)
            market = rows[0].market.upper()
            session = get_session(market)
            midnight = datetime.combine(session_date, datetime.min.time(), tzinfo=timezone.utc)
            expected = [
                midnight + timedelta(minutes=offset)
                for offset in range(24 * 60)
                if session.is_rth(midnight + timedelta(minutes=offset))
            ]
            expected_count = len(expected)
            expected_set = set(expected)
            rth_timestamps = [
                timestamp for timestamp in timestamps if timestamp in expected_set
            ]
            outside_session_bars = len(timestamps) - len(rth_timestamps)
            if rth_timestamps:
                first_bar_at = rth_timestamps[0]
                last_bar_at = rth_timestamps[-1]
                internal_expected = [
                    timestamp
                    for timestamp in expected
                    if first_bar_at <= timestamp <= last_bar_at
                ]
                actual_set = set(rth_timestamps)
                missing = sum(
                    timestamp not in actual_set for timestamp in internal_expected
                )
            else:
                first_bar_at = timestamps[0]
                last_bar_at = timestamps[-1]
                internal_expected = []
                missing = 0
            partial_start = not expected or not rth_timestamps or (
                rth_timestamps[0] != expected[0]
            )
            partial_end = not expected or not rth_timestamps or (
                rth_timestamps[-1] != expected[-1]
            )
            coverage_ratio = (
                len(rth_timestamps) / expected_count if expected_count else 0.0
            )
            evidence_by_timestamp = {
                timestamp: StrategyV2ShadowService._bar_readiness(
                    rows_by_timestamp[timestamp]
                )
                for timestamp in rth_timestamps
            }
            ready_timestamps = [
                timestamp
                for timestamp in rth_timestamps
                if evidence_by_timestamp[timestamp][0]
            ]
            eligible_timestamps = [
                timestamp
                for timestamp in rth_timestamps
                if evidence_by_timestamp[timestamp][1]
            ]
            incomplete_feature_bars = sum(
                "READINESS_EVIDENCE_MALFORMED" in reasons
                or "SESSION_DATA_INCOMPLETE" in reasons
                for _ready, _eligible, reasons in evidence_by_timestamp.values()
            )
            complete_session = (
                expected_count > 0
                and coverage_ratio >= _MIN_COMPLETE_SESSION_COVERAGE
                and missing == 0
                and incomplete_feature_bars == 0
                and not partial_start
                and not partial_end
                and outside_session_bars == 0
            )
            day_trades = trades_by_day.get(session_date, [])
            exits = Counter(str(trade.exit_reason or "UNKNOWN") for trade in day_trades)
            hourly_rows: list[StrategyV2ShadowHourlyEvidence] = []
            timestamps_by_hour: dict[int, list[datetime]] = {}
            for timestamp in rth_timestamps:
                market_hour = timestamp.astimezone(session.timezone).hour
                timestamps_by_hour.setdefault(market_hour, []).append(timestamp)
            for market_hour, hour_timestamps in sorted(timestamps_by_hour.items()):
                gate_counts: Counter[str] = Counter()
                for timestamp in hour_timestamps:
                    gate_counts.update(
                        evidence_by_timestamp[timestamp][2]
                        & _ENTRY_GATE_REASONS
                    )
                hourly_rows.append(StrategyV2ShadowHourlyEvidence(
                    session_hour=market_hour,
                    bars=len(hour_timestamps),
                    ready_bars=sum(
                        evidence_by_timestamp[timestamp][0]
                        for timestamp in hour_timestamps
                    ),
                    eligible_bars=sum(
                        evidence_by_timestamp[timestamp][1]
                        for timestamp in hour_timestamps
                    ),
                    gate_counts=dict(sorted(gate_counts.items())),
                ))
            first_ready_at = ready_timestamps[0] if ready_timestamps else None
            warmup_lost_bars = (
                rth_timestamps.index(first_ready_at)
                if first_ready_at is not None
                else len(rth_timestamps)
            )
            result.append(StrategyV2ShadowDailyEvidence(
                session_date=session_date,
                first_bar_at=first_bar_at,
                last_bar_at=last_bar_at,
                bars=len(rth_timestamps),
                eligible_bars=len(eligible_timestamps),
                expected_internal_bars=len(internal_expected),
                missing_internal_bars=missing,
                incomplete_feature_bars=incomplete_feature_bars,
                coverage_ratio=coverage_ratio,
                trades=len(day_trades),
                net_pnl=sum(float(trade.net_pnl or 0.0) for trade in day_trades),
                exit_reasons=dict(sorted(exits.items())),
                partial_start=partial_start,
                partial_end=partial_end,
                outside_session_bars=outside_session_bars,
                complete_session=complete_session,
                first_ready_at=first_ready_at,
                ready_bars=len(ready_timestamps),
                warmup_lost_bars=warmup_lost_bars,
                hourly_eligibility=hourly_rows,
            ))
        return result

    def _get_or_create_config(self, symbol: str | None = None) -> StrategyV2ShadowConfig:
        normalized = self._resolve_symbol(symbol)
        row = self.db.query(StrategyV2ShadowConfig).filter(
            StrategyV2ShadowConfig.symbol == normalized
        ).first()
        if row is not None:
            return row
        live = self.db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
        fee_rate_us = float(live.fee_rate_us) if live is not None else 0.0005
        fee_rate_hk = float(live.fee_rate_hk) if live is not None else 0.003
        default_profit_target = 0.50
        if normalized.endswith(".HK"):
            default_profit_target = min(
                5.0,
                max(
                    default_profit_target,
                    minimum_profit_target_pct(
                        one_side_fee_rate=fee_rate_hk,
                        slippage_bps=2.0,
                    ),
                ),
            )
        row = StrategyV2ShadowConfig(
            symbol=normalized,
            enabled=False,
            profit_target_pct=default_profit_target,
            estimated_fee_rate_us=fee_rate_us,
            estimated_fee_rate_hk=fee_rate_hk,
        )
        self.db.add(row)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self.db.query(StrategyV2ShadowConfig).filter(
                StrategyV2ShadowConfig.symbol == normalized
            ).first()
            if existing is None:
                raise
            return existing
        self.db.refresh(row)
        return row

    def _transient_config(self, symbol: str) -> StrategyV2ShadowConfig:
        live = self.db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
        fee_rate_us = float(live.fee_rate_us) if live is not None else 0.0005
        fee_rate_hk = float(live.fee_rate_hk) if live is not None else 0.003
        default_profit_target = 0.50
        if symbol.endswith(".HK"):
            default_profit_target = min(
                5.0,
                max(
                    default_profit_target,
                    minimum_profit_target_pct(
                        one_side_fee_rate=fee_rate_hk,
                        slippage_bps=2.0,
                    ),
                ),
            )
        values = StrategyV2ShadowConfigValues(
            symbol=symbol,
            profit_target_pct=default_profit_target,
            estimated_fee_rate_us=fee_rate_us,
            estimated_fee_rate_hk=fee_rate_hk,
        )
        return StrategyV2ShadowConfig(
            **{
                field: getattr(values, field)
                for field in _CONFIG_FIELDS
            },
            updated_at=datetime.now(timezone.utc),
        )

    def _get_or_create_state(
        self,
        symbol: str,
        *,
        commit: bool = True,
    ) -> StrategyV2ShadowState:
        row = self.db.query(StrategyV2ShadowState).filter(
            StrategyV2ShadowState.symbol == symbol
        ).first()
        if row is not None:
            return row
        row = StrategyV2ShadowState(symbol=symbol)
        self.db.add(row)
        if commit:
            try:
                self.db.commit()
            except IntegrityError:
                self.db.rollback()
                existing = self.db.query(StrategyV2ShadowState).filter(
                    StrategyV2ShadowState.symbol == symbol
                ).first()
                if existing is None:
                    raise
                return existing
            self.db.refresh(row)
        return row

    @staticmethod
    def _reset_state_forward(
        state: StrategyV2ShadowState,
        *,
        config_version: str,
        watermark: datetime,
    ) -> None:
        state.config_version = config_version
        state.session_date = None
        state.phase = StrategyV2State.COLD.value
        state.last_bar_at = watermark
        state.armed_at = None
        state.armed_zscore = None
        state.entries_today = 0
        state.open_trade_id = None
        state.state_json = "{}"
        state.last_poll_error = ""

    @staticmethod
    def _forward_watermark(market: str, current: datetime) -> datetime:
        normalized = _as_utc(current)
        if get_session(market).is_rth(normalized):
            return normalized.replace(second=0, microsecond=0)
        return normalized

    def _resolve_symbol(self, symbol: str | None) -> str:
        normalized = (symbol or "").strip().upper()
        if not normalized:
            normalized = StrategyService(self.db).resolve_primary_symbol()
        if not normalized or _SHADOW_SYMBOL_RE.fullmatch(normalized) is None:
            raise ValueError(
                "strategy v2 shadow requires a US/HK CODE.MARKET symbol"
            )
        return normalized

    def _open_trade(self, symbol: str) -> StrategyV2ShadowTrade | None:
        return self.db.query(StrategyV2ShadowTrade).filter(
            StrategyV2ShadowTrade.symbol == symbol,
            StrategyV2ShadowTrade.status == "OPEN",
        ).first()

    @staticmethod
    def _config_values(row: StrategyV2ShadowConfig) -> dict[str, Any]:
        values = {field: getattr(row, field) for field in _CONFIG_FIELDS}
        values.update(
            {
                "algorithm_version": _ALGORITHM_VERSION,
                "mode": "SHADOW",
                "order_submission_allowed": False,
                "allow_position_addons": False,
                "short_entries_enabled": False,
            }
        )
        return values

    def _config_response(
        self,
        row: StrategyV2ShadowConfig,
    ) -> StrategyV2ShadowConfigResponse:
        return StrategyV2ShadowConfigResponse(
            **self._config_values(row),
            config_version=self._config_version(row),
            updated_at=row.updated_at,
        )

    def _config_version(self, row: StrategyV2ShadowConfig) -> str:
        return self._config_version_for_symbol(row, row.symbol)

    def _config_version_for_symbol(
        self,
        row: StrategyV2ShadowConfig,
        symbol: str,
    ) -> str:
        payload = self._config_values(row)
        payload["symbol"] = symbol
        # Operational enable/disable and timestamps do not change the strategy
        # identity. Algorithm and frozen fee assumptions do.
        payload.pop("enabled", None)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _decision_response(
        row: StrategyV2ShadowDecision,
    ) -> StrategyV2ShadowDecisionResponse:
        try:
            decoded = json.loads(row.gate_reasons_json or "[]")
        except json.JSONDecodeError:
            decoded = []
        gate_reasons = [str(item) for item in decoded] if isinstance(decoded, list) else []
        return StrategyV2ShadowDecisionResponse(
            id=row.id,
            idempotency_key=row.idempotency_key,
            symbol=row.symbol,
            market=row.market,
            config_version=row.config_version,
            observed_at=row.observed_at,
            bar_timestamp_1m=row.bar_at,
            bar_timestamp_5m=row.bar_at_5m,
            price=row.close_price,
            vwap_1m=row.vwap_1m,
            zscore_1m=row.zscore_1m,
            vwap_5m=row.vwap_5m,
            zscore_5m=row.zscore_5m,
            adx=row.adx_5m,
            realized_vol=row.realized_vol_1m,
            regime_eligible=row.gate_passed,
            breach_armed=row.breach_armed,
            action=row.action,
            reason=row.reason,
            virtual_position=row.virtual_position,
            reference_price=row.reference_price,
            quantity=row.quantity,
            gross_pnl=row.gross_pnl,
            fee=row.fee,
            net_pnl=row.net_pnl,
            exit_reason=row.exit_reason,
            holding_minutes=row.holding_minutes,
            mae_pct=row.mae_pct,
            mfe_pct=row.mfe_pct,
            gate_reasons=gate_reasons,
        )

    @staticmethod
    def _latest_response(
        row: StrategyV2ShadowDecision,
        open_trade: StrategyV2ShadowTrade | None,
    ) -> StrategyV2ShadowLatestResponse:
        bar_end = _as_utc(row.bar_at) + timedelta(minutes=1)
        return StrategyV2ShadowLatestResponse(
            observed_at=row.observed_at,
            data_age_seconds=max(
                0.0,
                (datetime.now(timezone.utc) - bar_end).total_seconds(),
            ),
            bar_timestamp_1m=row.bar_at,
            bar_timestamp_5m=row.bar_at_5m,
            price=row.close_price,
            vwap_1m=row.vwap_1m,
            zscore_1m=row.zscore_1m,
            vwap_5m=row.vwap_5m,
            zscore_5m=row.zscore_5m,
            adx=row.adx_5m,
            realized_vol=row.realized_vol_1m,
            regime_eligible=row.gate_passed,
            breach_armed=row.breach_armed,
            virtual_position="LONG" if open_trade is not None else "FLAT",
            virtual_entry_price=open_trade.entry_price if open_trade is not None else None,
            virtual_entry_at=open_trade.entry_at if open_trade is not None else None,
            last_action=row.action,
            last_reason=row.reason,
        )

    def _metrics(self, symbol: str, config_version: str) -> StrategyV2ShadowMetrics:
        decisions = self.db.query(StrategyV2ShadowDecision).filter(
            StrategyV2ShadowDecision.symbol == symbol,
            StrategyV2ShadowDecision.config_version == config_version,
        ).order_by(StrategyV2ShadowDecision.bar_at.asc()).all()
        trades = self.db.query(StrategyV2ShadowTrade).filter(
            StrategyV2ShadowTrade.symbol == symbol,
            StrategyV2ShadowTrade.config_version == config_version,
            StrategyV2ShadowTrade.status == "CLOSED",
        ).order_by(StrategyV2ShadowTrade.exit_at.asc()).all()
        actions = [row.action.upper() for row in decisions]
        bar_minutes = {
            _as_utc(row.bar_at).replace(second=0, microsecond=0)
            for row in decisions
        }
        eligible_bar_minutes = {
            _as_utc(row.bar_at).replace(second=0, microsecond=0)
            for row in decisions
            if row.gate_passed
        }
        net_values = [float(row.net_pnl or 0.0) for row in trades]
        wins = sum(value > 0 for value in net_values)
        cumulative = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for value in net_values:
            cumulative += value
            peak = max(peak, cumulative)
            max_drawdown = max(max_drawdown, peak - cumulative)
        holding = [float(row.holding_seconds) / 60 for row in trades if row.holding_seconds is not None]
        mae = [float(row.mae_pct) for row in trades if row.mae_pct is not None]
        mfe = [float(row.mfe_pct) for row in trades if row.mfe_pct is not None]
        return StrategyV2ShadowMetrics(
            bars=len(bar_minutes),
            eligible_bars=len(eligible_bar_minutes),
            breaches=actions.count(StrategyV2Action.ARM_LONG.value),
            reclaims=actions.count(StrategyV2Action.SUBMIT_ENTRY.value),
            entries=actions.count(StrategyV2Action.FILL_ENTRY.value),
            exits=actions.count(StrategyV2Action.EXIT_LONG.value),
            closed_trades=len(trades),
            win_rate=(wins / len(trades)) if trades else 0.0,
            gross_pnl=sum(float(row.gross_pnl or 0.0) for row in trades),
            fees=sum(float(row.estimated_fees or 0.0) for row in trades),
            net_pnl=sum(net_values),
            max_drawdown=max_drawdown,
            avg_holding_minutes=sum(holding) / len(holding) if holding else 0.0,
            avg_mae_pct=sum(mae) / len(mae) if mae else 0.0,
            avg_mfe_pct=sum(mfe) / len(mfe) if mfe else 0.0,
        )

    def _gate_counts(self, symbol: str, config_version: str) -> dict[str, int]:
        bars_by_reason: dict[str, set[datetime]] = {}
        rows = self.db.query(
            StrategyV2ShadowDecision.bar_at,
            StrategyV2ShadowDecision.gate_reasons_json,
        ).filter(
            StrategyV2ShadowDecision.symbol == symbol,
            StrategyV2ShadowDecision.config_version == config_version,
        ).all()
        for bar_at, raw in rows:
            try:
                values = json.loads(raw or "[]")
            except (TypeError, json.JSONDecodeError):
                values = ["FEATURE_EVIDENCE_INVALID"]
            if not isinstance(values, list):
                values = ["FEATURE_EVIDENCE_INVALID"]
            normalized_bar_at = _as_utc(bar_at).replace(
                second=0,
                microsecond=0,
            )
            for value in values:
                bars_by_reason.setdefault(str(value), set()).add(
                    normalized_bar_at
                )
        return {
            reason: len(timestamps)
            for reason, timestamps in sorted(bars_by_reason.items())
        }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
