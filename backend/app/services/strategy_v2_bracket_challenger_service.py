from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.domain.strategy_v2 import (
    BracketAction,
    BracketConfig,
    StrategyBar,
    estimated_net_reward_risk_ratio,
    estimated_round_trip_cost_pct,
    evaluate_bracket_bar,
)
from app.models import (
    StrategyV2BracketChallengerRegistration,
    StrategyV2BracketChallengerTrade,
    StrategyV2ShadowDecision,
    StrategyV2ShadowTrade,
)
from app.schemas import (
    StrategyV2BracketChallengerReport,
    StrategyV2BracketChallengerVariant,
)


@dataclass(frozen=True)
class _BracketSpec:
    algorithm_version: str
    stop_loss_pct: float
    profit_target_pct: float

    def config(
        self,
        *,
        slippage_bps: float,
        flatten_minutes_before_close: int,
    ) -> BracketConfig:
        return BracketConfig(
            stop_loss_pct=self.stop_loss_pct,
            profit_target_pct=self.profit_target_pct,
            slippage_bps=slippage_bps,
            flatten_minutes_before_close=flatten_minutes_before_close,
        )


_BRACKET_SPECS = (
    _BracketSpec("strategy-v2-bracket-s40-t70-v1", 0.40, 0.70),
    _BracketSpec("strategy-v2-bracket-s50-t100-v1", 0.50, 1.00),
)
_EVALUATOR_VERSION = "strategy-v2-bracket-forward-evaluator-v1"
_MIN_READY_PAIRS = 20
_MIN_MATURE_PAIRS = 50
_MIN_CHANGED_EXITS = 5
_MIN_NET_REWARD_RISK_RATIO = 1.0
_UNCHANGED_EPSILON = 1e-9


class StrategyV2BracketChallengerService:
    """Collect isolated, forward-only bracket alternatives."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def ensure_registrations(
        self,
        *,
        symbol: str,
        market: str,
        source_config_version: str,
        slippage_bps: float,
        estimated_fee_rate: float,
        max_holding_minutes: int,
        flatten_minutes_before_close: int,
        now: datetime,
    ) -> bool:
        current = _as_utc(now)
        if (
            not math.isfinite(estimated_fee_rate)
            or estimated_fee_rate < 0
        ):
            raise ValueError("estimated_fee_rate must be finite and non-negative")
        if not 1 <= max_holding_minutes <= 1_440:
            raise ValueError("max_holding_minutes must be in [1, 1440]")
        eligible_after = current.replace(
            second=0,
            microsecond=0,
        ) + timedelta(minutes=1)
        existing = {
            row.algorithm_version: row
            for row in self.db.query(
                StrategyV2BracketChallengerRegistration
            ).filter(
                StrategyV2BracketChallengerRegistration.symbol == symbol,
                StrategyV2BracketChallengerRegistration.source_config_version
                == source_config_version,
            ).all()
        }
        created = False
        for spec in _BRACKET_SPECS:
            spec.config(
                slippage_bps=slippage_bps,
                flatten_minutes_before_close=flatten_minutes_before_close,
            )
            round_trip_cost = estimated_round_trip_cost_pct(
                one_side_fee_rate=estimated_fee_rate,
                slippage_bps=slippage_bps,
            )
            reward_risk = estimated_net_reward_risk_ratio(
                profit_target_pct=spec.profit_target_pct,
                stop_loss_pct=spec.stop_loss_pct,
                one_side_fee_rate=estimated_fee_rate,
                slippage_bps=slippage_bps,
            )
            if reward_risk + _UNCHANGED_EPSILON < (
                _MIN_NET_REWARD_RISK_RATIO
            ):
                raise ValueError(
                    "bracket challenger net reward/risk must be at least one"
                )
            digest = self._evaluator_digest(
                spec,
                slippage_bps=slippage_bps,
                estimated_fee_rate=estimated_fee_rate,
                max_holding_minutes=max_holding_minutes,
                flatten_minutes_before_close=flatten_minutes_before_close,
            )
            row = existing.get(spec.algorithm_version)
            if row is not None:
                self._validate_frozen_registration(
                    row,
                    market=market,
                    spec=spec,
                    slippage_bps=slippage_bps,
                    estimated_fee_rate=estimated_fee_rate,
                    max_holding_minutes=max_holding_minutes,
                    flatten_minutes_before_close=(
                        flatten_minutes_before_close
                    ),
                    round_trip_cost=round_trip_cost,
                    reward_risk=reward_risk,
                    digest=digest,
                )
                continue
            self.db.add(StrategyV2BracketChallengerRegistration(
                symbol=symbol,
                market=market.upper(),
                source_config_version=source_config_version,
                algorithm_version=spec.algorithm_version,
                stop_loss_pct=spec.stop_loss_pct,
                profit_target_pct=spec.profit_target_pct,
                slippage_bps=slippage_bps,
                estimated_fee_rate=estimated_fee_rate,
                max_holding_minutes=max_holding_minutes,
                flatten_minutes_before_close=flatten_minutes_before_close,
                estimated_round_trip_cost_pct=round_trip_cost,
                estimated_net_reward_risk_ratio=reward_risk,
                evaluator_digest=digest,
                registered_at=current,
                eligible_after=eligible_after,
            ))
            created = True
        if created:
            self.db.commit()
        return created

    def has_open_trades(self, symbol: str) -> bool:
        return (
            self.db.query(StrategyV2BracketChallengerTrade.id).filter(
                StrategyV2BracketChallengerTrade.symbol == symbol,
                StrategyV2BracketChallengerTrade.status == "OPEN",
            ).first()
            is not None
        )

    def advance_bar(
        self,
        *,
        symbol: str,
        bar: StrategyBar,
        observed_at: datetime,
    ) -> None:
        observed = _as_utc(observed_at)
        self._attach_eligible_entry(symbol=symbol)
        rows = self.db.query(
            StrategyV2BracketChallengerTrade
        ).filter(
            StrategyV2BracketChallengerTrade.symbol == symbol,
            StrategyV2BracketChallengerTrade.status == "OPEN",
        ).all()
        for row in rows:
            if _as_utc(bar.timestamp) <= _as_utc(row.last_bar_at):
                continue
            registration = self.db.get(
                StrategyV2BracketChallengerRegistration,
                row.registration_id,
            )
            if registration is None:
                raise ValueError(
                    "bracket challenger registration is missing"
                )
            decision = evaluate_bracket_bar(
                config=BracketConfig(
                    stop_loss_pct=registration.stop_loss_pct,
                    profit_target_pct=registration.profit_target_pct,
                    slippage_bps=registration.slippage_bps,
                    flatten_minutes_before_close=(
                        registration.flatten_minutes_before_close
                    ),
                ),
                market=registration.market,
                entry_price=row.entry_price,
                signal_vwap=row.signal_vwap,
                holding_deadline=_as_utc(row.holding_deadline),
                bar=bar,
            )
            row.last_bar_at = bar.timestamp
            row.stop_price = decision.stop_price
            row.target_price = decision.target_price
            if decision.action == BracketAction.EXIT:
                if decision.exit_price is None:
                    raise ValueError(
                        "bracket challenger exit is missing its price"
                    )
                self._close_challenger(
                    row,
                    exit_at=decision.event_at,
                    exit_price=decision.exit_price,
                    reason=decision.reason,
                )
            self.db.add(row)

        self._sync_baseline_outcomes(symbol=symbol, paired_at=observed)
        self.db.flush()

    def get_report(
        self,
        symbol: str,
    ) -> StrategyV2BracketChallengerReport:
        registrations = self.db.query(
            StrategyV2BracketChallengerRegistration
        ).filter(
            StrategyV2BracketChallengerRegistration.symbol == symbol
        ).order_by(
            StrategyV2BracketChallengerRegistration.registered_at.asc(),
            StrategyV2BracketChallengerRegistration.id.asc(),
        ).all()
        return StrategyV2BracketChallengerReport(
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
        positive_values = (
            baseline.entry_price,
            baseline.quantity,
            baseline.signal_vwap,
        )
        if any(
            value is None
            or not math.isfinite(float(value))
            or float(value) <= 0
            for value in positive_values
        ):
            raise ValueError(
                "eligible baseline trade has incomplete bracket inputs"
            )
        if (
            baseline.estimated_fee_rate is None
            or not math.isfinite(float(baseline.estimated_fee_rate))
            or float(baseline.estimated_fee_rate) < 0
        ):
            raise ValueError(
                "eligible baseline trade has an invalid fee rate"
            )
        if baseline.holding_deadline is None:
            raise ValueError(
                "eligible baseline trade has no holding deadline"
            )

        registrations = self.db.query(
            StrategyV2BracketChallengerRegistration
        ).filter(
            StrategyV2BracketChallengerRegistration.symbol == symbol,
            StrategyV2BracketChallengerRegistration.source_config_version
            == baseline.config_version,
        ).all()
        for registration in registrations:
            if _as_utc(baseline.entry_at) < _as_utc(
                registration.eligible_after
            ):
                continue
            existing = self.db.query(
                StrategyV2BracketChallengerTrade.id
            ).filter(
                StrategyV2BracketChallengerTrade.registration_id
                == registration.id,
                StrategyV2BracketChallengerTrade.baseline_trade_id
                == baseline.id,
            ).first()
            if existing is not None:
                continue
            fee_rate = float(baseline.estimated_fee_rate or 0.0)
            if not math.isclose(
                fee_rate,
                registration.estimated_fee_rate,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise ValueError(
                    "baseline fee rate differs from frozen registration"
                )
            expected_deadline = _as_utc(
                baseline.entry_at
            ) + timedelta(minutes=registration.max_holding_minutes)
            if _as_utc(baseline.holding_deadline) != expected_deadline:
                raise ValueError(
                    "baseline holding deadline differs from registration"
                )
            config = BracketConfig(
                stop_loss_pct=registration.stop_loss_pct,
                profit_target_pct=registration.profit_target_pct,
                slippage_bps=registration.slippage_bps,
                flatten_minutes_before_close=(
                    registration.flatten_minutes_before_close
                ),
            )
            signal_vwap = float(baseline.signal_vwap or 0.0)
            self.db.add(StrategyV2BracketChallengerTrade(
                registration_id=registration.id,
                baseline_trade_id=baseline.id,
                symbol=symbol,
                source_config_version=baseline.config_version,
                status="OPEN",
                entry_at=baseline.entry_at,
                entry_price=baseline.entry_price,
                quantity=baseline.quantity,
                signal_vwap=signal_vwap,
                holding_deadline=baseline.holding_deadline,
                estimated_fee_rate=fee_rate,
                last_bar_at=baseline.entry_at,
                stop_price=config.stop_price(baseline.entry_price),
                target_price=config.target_price(
                    baseline.entry_price,
                    signal_vwap,
                ),
            ))
        self.db.flush()

    @staticmethod
    def _close_challenger(
        row: StrategyV2BracketChallengerTrade,
        *,
        exit_at: datetime,
        exit_price: float,
        reason: str,
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
        row.challenger_exit_reason = reason
        row.challenger_gross_pnl = gross
        row.challenger_estimated_fees = fees
        row.challenger_net_pnl = gross - fees

    def _sync_baseline_outcomes(
        self,
        *,
        symbol: str,
        paired_at: datetime,
    ) -> None:
        rows = self.db.query(
            StrategyV2BracketChallengerTrade
        ).filter(
            StrategyV2BracketChallengerTrade.symbol == symbol,
            StrategyV2BracketChallengerTrade.status == "CLOSED",
            StrategyV2BracketChallengerTrade.baseline_net_pnl.is_(None),
        ).all()
        for row in rows:
            baseline = self.db.get(
                StrategyV2ShadowTrade,
                row.baseline_trade_id,
            )
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
            row.net_pnl_delta = (
                row.challenger_net_pnl - baseline.net_pnl
            )
            row.paired_at = paired_at
            self.db.add(row)

    def _variant_report(
        self,
        registration: StrategyV2BracketChallengerRegistration,
    ) -> StrategyV2BracketChallengerVariant:
        rows = self.db.query(
            StrategyV2BracketChallengerTrade
        ).filter(
            StrategyV2BracketChallengerTrade.registration_id
            == registration.id
        ).order_by(
            StrategyV2BracketChallengerTrade.baseline_exit_at.asc(),
            StrategyV2BracketChallengerTrade.id.asc(),
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
        baseline_values = [
            float(row.baseline_net_pnl or 0.0)
            for row in paired
        ]
        challenger_values = [
            float(row.challenger_net_pnl or 0.0)
            for row in paired
        ]
        deltas = [float(row.net_pnl_delta or 0.0) for row in paired]
        changed_exits = sum(self._exit_changed(row) for row in paired)
        baseline_net = sum(baseline_values)
        challenger_net = sum(challenger_values)
        net_delta = sum(deltas)
        baseline_drawdown = _max_drawdown(baseline_values)
        challenger_drawdown = _max_drawdown(challenger_values)
        blockers: list[str] = []
        if len(paired) < _MIN_READY_PAIRS:
            blockers.append("MIN_PAIRED_TRADES")
        if changed_exits < _MIN_CHANGED_EXITS:
            blockers.append("MIN_CHANGED_EXITS")
        if paired and net_delta <= 0:
            blockers.append("NET_PNL_DELTA_NON_POSITIVE")
        if (
            paired
            and challenger_drawdown
            > baseline_drawdown + _UNCHANGED_EPSILON
        ):
            blockers.append("MAX_DRAWDOWN_WORSE")
        if (
            registration.estimated_net_reward_risk_ratio
            + _UNCHANGED_EPSILON
            < _MIN_NET_REWARD_RISK_RATIO
        ):
            blockers.append("NET_REWARD_RISK_BELOW_ONE")
        status = (
            "MATURE_EVIDENCE"
            if len(paired) >= _MIN_MATURE_PAIRS
            else "READY_FOR_REVIEW"
            if len(paired) >= _MIN_READY_PAIRS
            else "COLLECTING"
        )
        return StrategyV2BracketChallengerVariant(
            registration_id=registration.id,
            algorithm_version=registration.algorithm_version,
            source_config_version=registration.source_config_version,
            evaluator_digest=registration.evaluator_digest,
            stop_loss_pct=registration.stop_loss_pct,
            profit_target_pct=registration.profit_target_pct,
            slippage_bps=registration.slippage_bps,
            estimated_fee_rate=registration.estimated_fee_rate,
            max_holding_minutes=registration.max_holding_minutes,
            flatten_minutes_before_close=(
                registration.flatten_minutes_before_close
            ),
            estimated_round_trip_cost_pct=(
                registration.estimated_round_trip_cost_pct
            ),
            estimated_net_reward_risk_ratio=(
                registration.estimated_net_reward_risk_ratio
            ),
            registered_at=registration.registered_at,
            eligible_after=registration.eligible_after,
            status=status,
            paired_trades=len(paired),
            open_trades=sum(row.status == "OPEN" for row in rows),
            awaiting_baseline_trades=sum(
                row.status == "CLOSED"
                and row.baseline_net_pnl is None
                for row in rows
            ),
            changed_exits=changed_exits,
            exit_reasons=dict(sorted(Counter(
                row.challenger_exit_reason
                for row in rows
                if row.challenger_exit_reason
            ).items())),
            baseline_exit_reasons=dict(sorted(Counter(
                row.baseline_exit_reason
                for row in paired
                if row.baseline_exit_reason
            ).items())),
            improved_trades=sum(
                value > _UNCHANGED_EPSILON for value in deltas
            ),
            worsened_trades=sum(
                value < -_UNCHANGED_EPSILON for value in deltas
            ),
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
            mean_net_pnl_delta=(
                net_delta / len(paired) if paired else 0.0
            ),
            baseline_max_drawdown=baseline_drawdown,
            challenger_max_drawdown=challenger_drawdown,
            promotion_ready=not blockers,
            blockers=blockers,
        )

    @staticmethod
    def _exit_changed(
        row: StrategyV2BracketChallengerTrade,
    ) -> bool:
        if (
            row.challenger_exit_at is None
            or row.baseline_exit_at is None
            or row.challenger_exit_price is None
            or row.baseline_exit_price is None
        ):
            return False
        return (
            _as_utc(row.challenger_exit_at)
            != _as_utc(row.baseline_exit_at)
            or row.challenger_exit_reason != row.baseline_exit_reason
            or abs(
                row.challenger_exit_price - row.baseline_exit_price
            ) > _UNCHANGED_EPSILON
        )

    @staticmethod
    def _evaluator_digest(
        spec: _BracketSpec,
        *,
        slippage_bps: float,
        estimated_fee_rate: float,
        max_holding_minutes: int,
        flatten_minutes_before_close: int,
    ) -> str:
        payload = {
            "evaluator_version": _EVALUATOR_VERSION,
            **asdict(spec),
            "slippage_bps": slippage_bps,
            "estimated_fee_rate": estimated_fee_rate,
            "max_holding_minutes": max_holding_minutes,
            "flatten_minutes_before_close": flatten_minutes_before_close,
            "entry_effective": "NEXT_BAR_OPEN",
            "exit_priority": [
                "PRICE_STOP",
                "EOD_FLATTEN",
                "PROFIT_TARGET",
                "MAX_HOLD",
            ],
            "ambiguous_stop_target": "PRICE_STOP",
            "target_rule": "MAX_PARAMETER_TARGET_SIGNAL_VWAP",
            "position_side": "LONG",
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_frozen_registration(
        row: StrategyV2BracketChallengerRegistration,
        *,
        market: str,
        spec: _BracketSpec,
        slippage_bps: float,
        estimated_fee_rate: float,
        max_holding_minutes: int,
        flatten_minutes_before_close: int,
        round_trip_cost: float,
        reward_risk: float,
        digest: str,
    ) -> None:
        float_pairs = (
            (row.stop_loss_pct, spec.stop_loss_pct),
            (row.profit_target_pct, spec.profit_target_pct),
            (row.slippage_bps, slippage_bps),
            (row.estimated_fee_rate, estimated_fee_rate),
            (
                row.estimated_round_trip_cost_pct,
                round_trip_cost,
            ),
            (
                row.estimated_net_reward_risk_ratio,
                reward_risk,
            ),
        )
        if (
            row.market.upper() != market.upper()
            or row.max_holding_minutes != max_holding_minutes
            or row.flatten_minutes_before_close
            != flatten_minutes_before_close
            or row.evaluator_digest != digest
            or any(
                not math.isclose(
                    actual,
                    expected,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
                for actual, expected in float_pairs
            )
        ):
            raise ValueError(
                "persisted bracket registration differs from frozen evaluator"
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
