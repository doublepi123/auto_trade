from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.fees import one_side_fee_rate
from app.core.market_calendar import get_session, is_trading_hours
from app.domain.strategy_v2 import (
    StrategyBar,
    StrategyV2Action,
    StrategyV2Config,
    StrategyV2Decision,
    StrategyV2Engine,
    StrategyV2State,
    VirtualPosition,
)
from app.platform.strategy_quality import strategy_quality_report
from app.models import (
    StrategyConfig,
    StrategyV2ShadowConfig,
    StrategyV2ShadowDecision,
    StrategyV2ShadowState,
    StrategyV2ShadowTrade,
    StrategyV2ShadowVersion,
)
from app.schemas import (
    StrategyV2ShadowConfigResponse,
    StrategyV2ShadowConfigUpdate,
    StrategyV2ShadowConfigValues,
    StrategyV2ShadowDecisionPage,
    StrategyV2ShadowDecisionResponse,
    StrategyV2ShadowDailyEvidence,
    StrategyV2ShadowEvaluationResponse,
    StrategyV2ShadowLatestResponse,
    StrategyV2ShadowMetrics,
    StrategyV2ShadowReplayRequest,
    StrategyV2ShadowReplayResponse,
    StrategyV2ShadowStatusResponse,
    StrategyV2ShadowTradeResponse,
    StrategyV2ShadowVersionResponse,
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
_ALGORITHM_VERSION = "strategy-v2-rth-mr-v1"
_VALID_ACTIONS = frozenset(action.value for action in StrategyV2Action)
_SHADOW_SYMBOL_RE = re.compile(r"^[A-Z0-9\-]{1,12}\.(US|HK)$")
_MIN_POLL_SECONDS = 45.0
_ONE_MINUTE_CANDLE_COUNT = 500


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
        observed_days = sum(item.coverage_ratio >= 0.8 for item in daily)
        closed_trades = len(trades)
        blockers: list[str] = []
        if observed_days < 20:
            blockers.append("MIN_TRADING_DAYS")
        if closed_trades < 50:
            blockers.append("MIN_CLOSED_TRADES")
        warnings = [
            f"{item.session_date.isoformat()}: {item.missing_internal_bars} internal bars missing"
            for item in daily
            if item.missing_internal_bars > 0
        ]
        net_values = [float(item.net_pnl or 0.0) for item in trades]
        return StrategyV2ShadowEvaluationResponse(
            symbol=normalized,
            config_version=version,
            status="READY_FOR_REVIEW" if not blockers else "COLLECTING",
            observed_trading_days=observed_days,
            remaining_trading_days=max(0, 20 - observed_days),
            closed_trades=closed_trades,
            remaining_closed_trades=max(0, 50 - closed_trades),
            first_bar_at=decisions[0].bar_at if decisions else None,
            last_bar_at=decisions[-1].bar_at if decisions else None,
            bars=len({item.bar_at for item in decisions}),
            readiness_blockers=blockers,
            data_quality_warnings=warnings,
            quality=strategy_quality_report(net_values).to_dict() if net_values else None,
            daily=daily,
        )

    def update_config(
        self,
        payload: StrategyV2ShadowConfigUpdate,
        *,
        symbol: str | None = None,
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

        merged = self._config_values(row)
        merged.update(updates)
        validated = StrategyV2ShadowConfigValues.model_validate(merged)

        was_enabled = row.enabled
        changed = any(getattr(row, field) != value for field, value in updates.items())
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
            state.last_bar_at = now
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
        return StrategyV2ShadowStatusResponse(
            config=self._config_response(config_row),
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

        if not config.enabled and open_trade is None:
            return self.get_status(normalized)
        if not is_trading_hours(market, current) and open_trade is None:
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

    def replay(self, payload: StrategyV2ShadowReplayRequest) -> StrategyV2ShadowReplayResponse:
        """Evaluate supplied bars without mutating any persistent shadow state."""
        config = self.db.query(StrategyV2ShadowConfig).filter(
            StrategyV2ShadowConfig.symbol == payload.symbol
        ).first()
        if config is None:
            config = self._transient_config(payload.symbol)
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
        if not any(
            session.is_rth(bar.timestamp) and bar.end_at + grace <= observed_at
            for bar in bars
        ):
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
        if (
            open_trade is None
            and state.config_version
            and state.config_version != current_version
        ):
            self._reset_state_forward(
                state,
                config_version=current_version,
                watermark=observed_at,
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
        exited_managed_position = False

        for bar in bars:
            if last_bar_at is not None and bar.timestamp <= last_bar_at:
                continue
            if last_bar_at is None and bar.timestamp < activation_at:
                engine.features.on_bar(bar, observed_at=observed_at)
                continue
            feature = engine.features.on_bar(bar, observed_at=observed_at)
            if feature is None:
                continue
            gate_reasons = engine.entry_gate_reasons(feature)
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

        if latest_feature is not None:
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
            state.last_poll_error = ""
            self.db.add(state)
            self.db.commit()

    def _replay_payload(
        self,
        payload: StrategyV2ShadowReplayRequest,
        config: StrategyV2ShadowConfig,
    ) -> StrategyV2ShadowReplayResponse:
        engine = StrategyV2Engine(self._domain_config(config, payload.market))
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
            step = engine.on_feature(feature)
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
                }
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
            max_holding_minutes=60,
            entry_cutoff_minutes_before_close=45,
            flatten_minutes_before_close=15,
            max_entries_per_session=2,
            entry_cooldown_minutes=15,
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
        snapshot = StrategyV2ShadowVersion(
            symbol=row.symbol,
            config_version=version,
            config_json=json.dumps(params, sort_keys=True, separators=(",", ":")),
            activated_at=row.updated_at,
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
            item.coverage_ratio >= 0.8
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
    def _daily_evidence(
        decisions: list[StrategyV2ShadowDecision],
        trades: list[StrategyV2ShadowTrade],
    ) -> list[StrategyV2ShadowDailyEvidence]:
        by_day: dict[Any, list[StrategyV2ShadowDecision]] = {}
        for row in decisions:
            by_day.setdefault(row.session_date, []).append(row)
        trades_by_day: dict[Any, list[StrategyV2ShadowTrade]] = {}
        for trade in trades:
            if trade.exit_at is not None:
                trades_by_day.setdefault(_as_utc(trade.exit_at).date(), []).append(trade)
        result: list[StrategyV2ShadowDailyEvidence] = []
        for session_date, rows in sorted(by_day.items()):
            timestamps = sorted({_as_utc(row.bar_at).replace(second=0, microsecond=0) for row in rows})
            market = rows[0].market.upper()
            session = get_session(market)
            midnight = datetime.combine(session_date, datetime.min.time(), tzinfo=timezone.utc)
            expected = [
                midnight + timedelta(minutes=offset)
                for offset in range(24 * 60)
                if session.is_rth(midnight + timedelta(minutes=offset))
            ]
            expected_count = len(expected)
            internal_expected = [
                timestamp
                for timestamp in expected
                if timestamps[0] <= timestamp <= timestamps[-1]
            ]
            actual_internal = sum(
                timestamps[0] <= timestamp <= timestamps[-1]
                for timestamp in timestamps
            )
            missing = max(0, len(internal_expected) - actual_internal)
            day_trades = trades_by_day.get(session_date, [])
            exits = Counter(str(trade.exit_reason or "UNKNOWN") for trade in day_trades)
            result.append(StrategyV2ShadowDailyEvidence(
                session_date=session_date,
                first_bar_at=timestamps[0],
                last_bar_at=timestamps[-1],
                bars=len(timestamps),
                eligible_bars=len({row.bar_at for row in rows if row.gate_passed}),
                expected_internal_bars=len(internal_expected),
                missing_internal_bars=missing,
                coverage_ratio=(len(timestamps) / expected_count) if expected_count else 0.0,
                trades=len(day_trades),
                net_pnl=sum(float(trade.net_pnl or 0.0) for trade in day_trades),
                exit_reasons=dict(sorted(exits.items())),
                partial_start=bool(expected and timestamps[0] > expected[0]),
                partial_end=bool(expected and timestamps[-1] < expected[-1]),
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
        row = StrategyV2ShadowConfig(
            symbol=normalized,
            enabled=False,
            estimated_fee_rate_us=(
                float(live.fee_rate_us) if live is not None else 0.0005
            ),
            estimated_fee_rate_hk=(
                float(live.fee_rate_hk) if live is not None else 0.003
            ),
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
        values = StrategyV2ShadowConfigValues(
            symbol=symbol,
            estimated_fee_rate_us=(
                float(live.fee_rate_us) if live is not None else 0.0005
            ),
            estimated_fee_rate_hk=(
                float(live.fee_rate_hk) if live is not None else 0.003
            ),
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
            bars=len({row.bar_at for row in decisions}),
            eligible_bars=len({row.bar_at for row in decisions if row.gate_passed}),
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
            live_action_count=0,
            action_agreement_rate=0.0,
            net_pnl_delta_vs_live=0.0,
        )

    def _gate_counts(self, symbol: str, config_version: str) -> dict[str, int]:
        counter: Counter[str] = Counter()
        rows = self.db.query(StrategyV2ShadowDecision.gate_reasons_json).filter(
            StrategyV2ShadowDecision.symbol == symbol,
            StrategyV2ShadowDecision.config_version == config_version,
        ).all()
        for (raw,) in rows:
            try:
                values = json.loads(raw or "[]")
            except json.JSONDecodeError:
                continue
            if isinstance(values, list):
                counter.update(str(value) for value in values)
        return dict(sorted(counter.items()))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
