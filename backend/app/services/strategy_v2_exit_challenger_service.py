from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy.orm import Session

from app.domain.strategy_v2 import (
    ProfitLockAction,
    ProfitLockConfig,
    StrategyBar,
    TimeExitAction,
    TimeExitConfig,
    evaluate_profit_lock_bar,
    evaluate_time_exit_bar,
)
from app.models import (
    StrategyV2ExitChallengerRegistration,
    StrategyV2ExitChallengerTrade,
    StrategyV2ShadowDecision,
    StrategyV2ShadowTrade,
)
from app.schemas import (
    StrategyV2ExitChallengerReport,
    StrategyV2ExitChallengerVariant,
)


@dataclass(frozen=True)
class _ProfitLockSpec:
    algorithm_version: str
    activation_pct: float
    locked_profit_pct: float

    def config(self, *, slippage_bps: float) -> ProfitLockConfig:
        return ProfitLockConfig(
            activation_pct=self.activation_pct,
            locked_profit_pct=self.locked_profit_pct,
            slippage_bps=slippage_bps,
        )


@dataclass(frozen=True)
class _TimeExitSpec:
    algorithm_version: str
    max_holding_minutes: int

    def config(self, *, slippage_bps: float) -> TimeExitConfig:
        return TimeExitConfig(
            max_holding_minutes=self.max_holding_minutes,
            slippage_bps=slippage_bps,
        )


_PROFIT_LOCK_SPECS = (
    _ProfitLockSpec("strategy-v2-profit-lock-a40-f10-v2", 0.40, 0.10),
    _ProfitLockSpec("strategy-v2-profit-lock-a40-f20-v2", 0.40, 0.20),
    _ProfitLockSpec("strategy-v2-profit-lock-a40-f30-v2", 0.40, 0.30),
    _ProfitLockSpec("strategy-v2-profit-lock-a60-f40-v1", 0.60, 0.40),
)
_TIME_EXIT_SPECS = (
    _TimeExitSpec("strategy-v2-time-stop-m15-v2", 15),
    _TimeExitSpec("strategy-v2-time-stop-m30-v2", 30),
    _TimeExitSpec("strategy-v2-time-stop-m45-v2", 45),
)
_CURRENT_ALGORITHM_VERSIONS = tuple(
    spec.algorithm_version for spec in (*_PROFIT_LOCK_SPECS, *_TIME_EXIT_SPECS)
)
_EVALUATOR_VERSION = "strategy-v2-profit-lock-forward-evaluator-v2"
_TIME_EXIT_EVALUATOR_VERSION = "strategy-v2-time-stop-forward-evaluator-v2"
_MIN_READY_PAIRS = 20
_MIN_MATURE_PAIRS = 50
_MIN_PROFIT_LOCK_EXITS = 5
_MIN_TIME_STOP_EXITS = 5
_UNCHANGED_EPSILON = 1e-9
_OPEN_PRIORITY_EXIT_REASONS = frozenset({"EOD_FLATTEN", "MAX_HOLD"})


class StrategyV2ExitChallengerService:
    """Collect causal, forward-only exit-policy evidence without submitting orders."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def ensure_registrations(
        self,
        *,
        symbol: str,
        market: str,
        source_config_version: str,
        slippage_bps: float,
        now: datetime,
    ) -> bool:
        """Freeze all declared variants before their first eligible minute."""
        current = _as_utc(now)
        eligible_after = current.replace(second=0, microsecond=0) + timedelta(
            minutes=1
        )
        existing = {
            row.algorithm_version: row
            for row in self.db.query(StrategyV2ExitChallengerRegistration).filter(
                StrategyV2ExitChallengerRegistration.symbol == symbol,
                StrategyV2ExitChallengerRegistration.source_config_version
                == source_config_version,
            ).all()
        }
        created = False
        for spec in _PROFIT_LOCK_SPECS:
            spec.config(slippage_bps=slippage_bps)
            digest = self._evaluator_digest(
                spec,
                slippage_bps=slippage_bps,
            )
            row = existing.get(spec.algorithm_version)
            if row is not None:
                self._validate_frozen_registration(
                    row,
                    market=market,
                    spec=spec,
                    slippage_bps=slippage_bps,
                    digest=digest,
                )
                continue
            self.db.add(StrategyV2ExitChallengerRegistration(
                symbol=symbol,
                market=market.upper(),
                source_config_version=source_config_version,
                algorithm_version=spec.algorithm_version,
                policy_type="PROFIT_LOCK",
                activation_pct=spec.activation_pct,
                locked_profit_pct=spec.locked_profit_pct,
                max_holding_minutes=None,
                slippage_bps=slippage_bps,
                evaluator_digest=digest,
                registered_at=current,
                eligible_after=eligible_after,
            ))
            created = True
        for spec in _TIME_EXIT_SPECS:
            spec.config(slippage_bps=slippage_bps)
            digest = self._time_exit_evaluator_digest(
                spec,
                slippage_bps=slippage_bps,
            )
            row = existing.get(spec.algorithm_version)
            if row is not None:
                self._validate_frozen_time_exit_registration(
                    row,
                    market=market,
                    spec=spec,
                    slippage_bps=slippage_bps,
                    digest=digest,
                )
                continue
            self.db.add(StrategyV2ExitChallengerRegistration(
                symbol=symbol,
                market=market.upper(),
                source_config_version=source_config_version,
                algorithm_version=spec.algorithm_version,
                policy_type="TIME_STOP",
                activation_pct=0.0,
                locked_profit_pct=0.0,
                max_holding_minutes=spec.max_holding_minutes,
                slippage_bps=slippage_bps,
                evaluator_digest=digest,
                registered_at=current,
                eligible_after=eligible_after,
            ))
            created = True
        if created:
            self.db.commit()
        return created

    def advance_bar(
        self,
        *,
        symbol: str,
        bar: StrategyBar,
        observed_at: datetime,
    ) -> None:
        """Advance registered variants for one already-settled baseline bar."""
        observed = _as_utc(observed_at)
        self._attach_eligible_entry(symbol=symbol)
        rows = self.db.query(StrategyV2ExitChallengerTrade).filter(
            StrategyV2ExitChallengerTrade.symbol == symbol,
            StrategyV2ExitChallengerTrade.status == "OPEN",
        ).all()
        for row in rows:
            registration = self.db.get(
                StrategyV2ExitChallengerRegistration,
                row.registration_id,
            )
            baseline = self.db.get(StrategyV2ShadowTrade, row.baseline_trade_id)
            if registration is None or baseline is None:
                raise ValueError("profit-lock challenger linkage is incomplete")

            bar_at = _as_utc(bar.timestamp)
            last_bar_at = _as_utc(row.last_bar_at)
            if bar_at <= last_bar_at:
                if (
                    bar_at == last_bar_at
                    and baseline.status == "CLOSED"
                    and baseline.exit_at is not None
                    and _as_utc(baseline.exit_at) == bar_at
                ):
                    self._close_from_baseline(row, baseline)
                    self.db.add(row)
                continue

            baseline_closed_this_bar = (
                baseline.status == "CLOSED"
                and baseline.exit_at is not None
                and _as_utc(baseline.exit_at) == _as_utc(bar.timestamp)
            )
            if registration.policy_type == "TIME_STOP":
                if (
                    baseline_closed_this_bar
                    and baseline.exit_reason == "EOD_FLATTEN"
                ):
                    self._close_from_baseline(row, baseline)
                else:
                    if registration.max_holding_minutes is None:
                        raise ValueError(
                            "time-stop registration has no holding limit"
                        )
                    decision = evaluate_time_exit_bar(
                        config=TimeExitConfig(
                            max_holding_minutes=(
                                registration.max_holding_minutes
                            ),
                            slippage_bps=registration.slippage_bps,
                        ),
                        entry_at=_as_utc(row.entry_at),
                        bar=bar,
                    )
                    if decision.action == TimeExitAction.EXIT:
                        if decision.exit_price is None:
                            raise ValueError(
                                "time-stop exit is missing its price"
                            )
                        self._close_with_time_stop(
                            row,
                            exit_at=decision.event_at,
                            exit_price=decision.exit_price,
                        )
                    elif baseline.status == "CLOSED":
                        self._close_from_baseline(row, baseline)
                row.last_bar_at = bar.timestamp
                self.db.add(row)
                continue
            if registration.policy_type != "PROFIT_LOCK":
                raise ValueError("unsupported exit challenger policy type")
            if (
                baseline_closed_this_bar
                and baseline.exit_reason in _OPEN_PRIORITY_EXIT_REASONS
            ):
                self._close_from_baseline(row, baseline)
                row.last_bar_at = bar.timestamp
                self.db.add(row)
                continue

            config = ProfitLockConfig(
                activation_pct=registration.activation_pct,
                locked_profit_pct=registration.locked_profit_pct,
                slippage_bps=registration.slippage_bps,
            )
            decision = evaluate_profit_lock_bar(
                config=config,
                entry_price=row.entry_price,
                bar=bar,
                armed_before_bar=row.activation_at is not None,
            )
            row.activation_price = decision.activation_price
            row.floor_price = decision.floor_price
            row.last_bar_at = bar.timestamp

            if decision.action == ProfitLockAction.EXIT:
                if decision.exit_price is None:
                    raise ValueError("profit-lock exit is missing its price")
                self._close_with_profit_lock(
                    row,
                    exit_at=decision.event_at,
                    exit_price=decision.exit_price,
                )
            elif baseline.status == "CLOSED":
                self._close_from_baseline(row, baseline)
            elif decision.action == ProfitLockAction.ACTIVATE:
                row.activation_at = decision.event_at
                row.activation_effective_at = decision.effective_at
            self.db.add(row)

        self._sync_baseline_outcomes(symbol=symbol, paired_at=observed)
        self.db.flush()

    def get_report(self, symbol: str) -> StrategyV2ExitChallengerReport:
        registrations = self.db.query(
            StrategyV2ExitChallengerRegistration
        ).filter(
            StrategyV2ExitChallengerRegistration.symbol == symbol,
            StrategyV2ExitChallengerRegistration.algorithm_version.in_(
                _CURRENT_ALGORITHM_VERSIONS
            ),
        ).order_by(
            StrategyV2ExitChallengerRegistration.registered_at.asc(),
            StrategyV2ExitChallengerRegistration.id.asc(),
        ).all()
        return StrategyV2ExitChallengerReport(
            symbol=symbol,
            variants=[
                self._variant_report(row)
                for row in registrations
            ],
        )

    def _attach_eligible_entry(self, *, symbol: str) -> None:
        baseline = self.db.query(StrategyV2ShadowTrade).filter(
            StrategyV2ShadowTrade.symbol == symbol,
            StrategyV2ShadowTrade.status == "OPEN",
        ).first()
        if baseline is None or baseline.entry_decision_id is None:
            return
        entry_decision = self.db.get(
            StrategyV2ShadowDecision,
            baseline.entry_decision_id,
        )
        if entry_decision is None:
            return
        if (
            entry_decision.action != "FILL_ENTRY"
            or entry_decision.symbol != baseline.symbol
            or entry_decision.config_version != baseline.config_version
            or _as_utc(entry_decision.bar_at) != _as_utc(baseline.entry_at)
        ):
            raise ValueError("eligible baseline entry linkage is invalid")
        if (
            not math.isfinite(float(baseline.entry_price))
            or baseline.entry_price <= 0
            or not math.isfinite(float(baseline.quantity))
            or baseline.quantity <= 0
        ):
            raise ValueError("eligible baseline trade has invalid entry values")
        registrations = self.db.query(
            StrategyV2ExitChallengerRegistration
        ).filter(
            StrategyV2ExitChallengerRegistration.symbol == symbol,
            StrategyV2ExitChallengerRegistration.source_config_version
            == baseline.config_version,
            StrategyV2ExitChallengerRegistration.algorithm_version.in_(
                _CURRENT_ALGORITHM_VERSIONS
            ),
        ).all()
        for registration in registrations:
            if _as_utc(baseline.entry_at) < _as_utc(registration.eligible_after):
                continue
            existing = self.db.query(StrategyV2ExitChallengerTrade.id).filter(
                StrategyV2ExitChallengerTrade.registration_id == registration.id,
                StrategyV2ExitChallengerTrade.baseline_trade_id == baseline.id,
            ).first()
            if existing is not None:
                continue
            fee_rate = baseline.estimated_fee_rate
            if (
                fee_rate is None
                or not math.isfinite(float(fee_rate))
                or float(fee_rate) < 0
            ):
                raise ValueError("eligible baseline trade has no frozen fee rate")
            self.db.add(StrategyV2ExitChallengerTrade(
                registration_id=registration.id,
                baseline_trade_id=baseline.id,
                symbol=symbol,
                source_config_version=baseline.config_version,
                status="OPEN",
                entry_at=baseline.entry_at,
                entry_price=baseline.entry_price,
                quantity=baseline.quantity,
                estimated_fee_rate=float(fee_rate),
                last_bar_at=_as_utc(baseline.entry_at) - timedelta(
                    microseconds=1
                ),
            ))
        self.db.flush()

    @staticmethod
    def _close_with_profit_lock(
        row: StrategyV2ExitChallengerTrade,
        *,
        exit_at: datetime,
        exit_price: float,
    ) -> None:
        gross = (exit_price - row.entry_price) * row.quantity
        fees = (
            (row.entry_price + exit_price)
            * row.quantity
            * row.estimated_fee_rate
        )
        row.status = "CLOSED"
        row.challenger_exit_at = exit_at
        row.challenger_exit_price = exit_price
        row.challenger_exit_reason = "PROFIT_LOCK"
        row.challenger_gross_pnl = gross
        row.challenger_estimated_fees = fees
        row.challenger_net_pnl = gross - fees

    @staticmethod
    def _close_with_time_stop(
        row: StrategyV2ExitChallengerTrade,
        *,
        exit_at: datetime,
        exit_price: float,
    ) -> None:
        gross = (exit_price - row.entry_price) * row.quantity
        fees = (
            (row.entry_price + exit_price)
            * row.quantity
            * row.estimated_fee_rate
        )
        row.status = "CLOSED"
        row.challenger_exit_at = exit_at
        row.challenger_exit_price = exit_price
        row.challenger_exit_reason = "TIME_STOP"
        row.challenger_gross_pnl = gross
        row.challenger_estimated_fees = fees
        row.challenger_net_pnl = gross - fees

    @staticmethod
    def _close_from_baseline(
        row: StrategyV2ExitChallengerTrade,
        baseline: StrategyV2ShadowTrade,
    ) -> None:
        if (
            baseline.exit_at is None
            or baseline.exit_price is None
            or baseline.gross_pnl is None
            or baseline.estimated_fees is None
            or baseline.net_pnl is None
        ):
            raise ValueError("closed baseline trade has incomplete outcome data")
        row.status = "CLOSED"
        row.challenger_exit_at = baseline.exit_at
        row.challenger_exit_price = baseline.exit_price
        row.challenger_exit_reason = f"BASELINE_{baseline.exit_reason or 'EXIT'}"
        row.challenger_gross_pnl = baseline.gross_pnl
        row.challenger_estimated_fees = baseline.estimated_fees
        row.challenger_net_pnl = baseline.net_pnl

    def _sync_baseline_outcomes(
        self,
        *,
        symbol: str,
        paired_at: datetime,
    ) -> None:
        rows = self.db.query(StrategyV2ExitChallengerTrade).filter(
            StrategyV2ExitChallengerTrade.symbol == symbol,
            StrategyV2ExitChallengerTrade.status == "CLOSED",
            StrategyV2ExitChallengerTrade.baseline_net_pnl.is_(None),
        ).all()
        for row in rows:
            baseline = self.db.get(StrategyV2ShadowTrade, row.baseline_trade_id)
            if (
                baseline is None
                or baseline.status != "CLOSED"
                or baseline.exit_at is None
                or baseline.exit_price is None
                or baseline.net_pnl is None
                or row.challenger_net_pnl is None
            ):
                continue
            row.baseline_exit_at = baseline.exit_at
            row.baseline_exit_price = baseline.exit_price
            row.baseline_exit_reason = baseline.exit_reason
            row.baseline_net_pnl = baseline.net_pnl
            row.net_pnl_delta = row.challenger_net_pnl - baseline.net_pnl
            row.paired_at = paired_at
            self.db.add(row)

    def _variant_report(
        self,
        registration: StrategyV2ExitChallengerRegistration,
    ) -> StrategyV2ExitChallengerVariant:
        policy_type: Literal["PROFIT_LOCK", "TIME_STOP"]
        if registration.policy_type == "PROFIT_LOCK":
            policy_type = "PROFIT_LOCK"
        elif registration.policy_type == "TIME_STOP":
            policy_type = "TIME_STOP"
        else:
            raise ValueError("unsupported exit challenger policy type")
        rows = self.db.query(StrategyV2ExitChallengerTrade).filter(
            StrategyV2ExitChallengerTrade.registration_id == registration.id
        ).order_by(
            StrategyV2ExitChallengerTrade.baseline_exit_at.asc(),
            StrategyV2ExitChallengerTrade.id.asc(),
        ).all()
        paired = [
            row
            for row in rows
            if (
                row.baseline_net_pnl is not None
                and row.challenger_net_pnl is not None
                and row.net_pnl_delta is not None
            )
        ]
        baseline_values = [float(row.baseline_net_pnl or 0.0) for row in paired]
        challenger_values = [
            float(row.challenger_net_pnl or 0.0)
            for row in paired
        ]
        deltas = [float(row.net_pnl_delta or 0.0) for row in paired]
        profit_lock_exits = sum(
            row.challenger_exit_reason == "PROFIT_LOCK"
            for row in rows
        )
        time_stop_exits = sum(
            row.challenger_exit_reason == "TIME_STOP"
            for row in rows
        )
        baseline_net = sum(baseline_values)
        challenger_net = sum(challenger_values)
        net_delta = sum(deltas)
        baseline_drawdown = _max_drawdown(baseline_values)
        challenger_drawdown = _max_drawdown(challenger_values)
        blockers: list[str] = []
        if len(paired) < _MIN_READY_PAIRS:
            blockers.append("MIN_PAIRED_TRADES")
        if (
            registration.policy_type == "TIME_STOP"
            and time_stop_exits < _MIN_TIME_STOP_EXITS
        ):
            blockers.append("MIN_TIME_STOP_EXITS")
        elif (
            registration.policy_type == "PROFIT_LOCK"
            and profit_lock_exits < _MIN_PROFIT_LOCK_EXITS
        ):
            blockers.append("MIN_PROFIT_LOCK_EXITS")
        if paired and net_delta <= 0:
            blockers.append("NET_PNL_DELTA_NON_POSITIVE")
        if paired and challenger_drawdown > baseline_drawdown + _UNCHANGED_EPSILON:
            blockers.append("MAX_DRAWDOWN_WORSE")
        status = (
            "MATURE_EVIDENCE"
            if len(paired) >= _MIN_MATURE_PAIRS
            else "READY_FOR_REVIEW"
            if len(paired) >= _MIN_READY_PAIRS
            else "COLLECTING"
        )
        return StrategyV2ExitChallengerVariant(
            registration_id=registration.id,
            algorithm_version=registration.algorithm_version,
            source_config_version=registration.source_config_version,
            evaluator_digest=registration.evaluator_digest,
            policy_type=policy_type,
            activation_pct=registration.activation_pct,
            locked_profit_pct=registration.locked_profit_pct,
            max_holding_minutes=registration.max_holding_minutes,
            slippage_bps=registration.slippage_bps,
            registered_at=registration.registered_at,
            eligible_after=registration.eligible_after,
            status=status,
            paired_trades=len(paired),
            open_trades=sum(row.status == "OPEN" for row in rows),
            awaiting_baseline_trades=sum(
                row.status == "CLOSED" and row.baseline_net_pnl is None
                for row in rows
            ),
            profit_lock_exits=profit_lock_exits,
            time_stop_exits=time_stop_exits,
            improved_trades=sum(value > _UNCHANGED_EPSILON for value in deltas),
            worsened_trades=sum(value < -_UNCHANGED_EPSILON for value in deltas),
            unchanged_trades=sum(
                abs(value) <= _UNCHANGED_EPSILON for value in deltas
            ),
            baseline_win_rate=(
                sum(value > 0 for value in baseline_values) / len(paired)
                if paired
                else 0.0
            ),
            challenger_win_rate=(
                sum(value > 0 for value in challenger_values) / len(paired)
                if paired
                else 0.0
            ),
            baseline_net_pnl=baseline_net,
            challenger_net_pnl=challenger_net,
            net_pnl_delta=net_delta,
            mean_net_pnl_delta=(net_delta / len(paired) if paired else 0.0),
            baseline_max_drawdown=baseline_drawdown,
            challenger_max_drawdown=challenger_drawdown,
            promotion_ready=not blockers,
            blockers=blockers,
        )

    @staticmethod
    def _evaluator_digest(
        spec: _ProfitLockSpec,
        *,
        slippage_bps: float,
    ) -> str:
        payload = {
            "evaluator_version": _EVALUATOR_VERSION,
            **asdict(spec),
            "slippage_bps": slippage_bps,
            "entry_bar_evaluation": "INCLUDED_AFTER_OPEN_FILL",
            "activation_effective": "NEXT_BAR",
            "forced_exit_priority": sorted(_OPEN_PRIORITY_EXIT_REASONS),
            "position_side": "LONG",
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_frozen_registration(
        row: StrategyV2ExitChallengerRegistration,
        *,
        market: str,
        spec: _ProfitLockSpec,
        slippage_bps: float,
        digest: str,
    ) -> None:
        if (
            row.market.upper() != market.upper()
            or row.policy_type != "PROFIT_LOCK"
            or row.activation_pct != spec.activation_pct
            or row.locked_profit_pct != spec.locked_profit_pct
            or row.max_holding_minutes is not None
            or row.slippage_bps != slippage_bps
            or row.evaluator_digest != digest
        ):
            raise ValueError(
                "persisted profit-lock registration differs from frozen evaluator"
            )

    @staticmethod
    def _time_exit_evaluator_digest(
        spec: _TimeExitSpec,
        *,
        slippage_bps: float,
    ) -> str:
        payload = {
            "evaluator_version": _TIME_EXIT_EVALUATOR_VERSION,
            **asdict(spec),
            "slippage_bps": slippage_bps,
            "entry_bar_evaluation": "INCLUDED_AFTER_OPEN_FILL",
            "exit_trigger": "FIRST_BAR_OPEN_AT_OR_AFTER_ENTRY_PLUS_TTL",
            "same_bar_priority": (
                "EOD_FLATTEN_THEN_TIME_STOP_THEN_INTRABAR_BASELINE"
            ),
            "position_side": "LONG",
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_frozen_time_exit_registration(
        row: StrategyV2ExitChallengerRegistration,
        *,
        market: str,
        spec: _TimeExitSpec,
        slippage_bps: float,
        digest: str,
    ) -> None:
        if (
            row.market.upper() != market.upper()
            or row.policy_type != "TIME_STOP"
            or row.activation_pct != 0.0
            or row.locked_profit_pct != 0.0
            or row.max_holding_minutes != spec.max_holding_minutes
            or row.slippage_bps != slippage_bps
            or row.evaluator_digest != digest
        ):
            raise ValueError(
                "persisted time-stop registration differs from frozen evaluator"
            )


def _max_drawdown(values: list[float]) -> float:
    cumulative = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return drawdown


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
