from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy.orm import Session

from app.config import settings
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
    LiveExitChallengerRegistration,
    LiveExitChallengerTrade,
    OrderRecord,
    StrategyConfig,
    TrackedEntry,
)
from app.schemas import (
    LiveExitChallengerReport,
    LiveExitChallengerVariant,
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
    _ProfitLockSpec("live-profit-lock-a30-f20-v1", 0.30, 0.20),
    _ProfitLockSpec("live-profit-lock-a40-f10-v1", 0.40, 0.10),
    _ProfitLockSpec("live-profit-lock-a40-f20-v1", 0.40, 0.20),
    _ProfitLockSpec("live-profit-lock-a40-f30-v1", 0.40, 0.30),
    _ProfitLockSpec("live-profit-lock-a60-f40-v1", 0.60, 0.40),
    _ProfitLockSpec("live-profit-lock-a60-f50-v1", 0.60, 0.50),
    _ProfitLockSpec("live-profit-lock-a70-f60-v1", 0.70, 0.60),
)
LIVE_PROFIT_LOCK_ALGORITHM_VERSIONS = tuple(
    spec.algorithm_version for spec in _PROFIT_LOCK_SPECS
)
_TIME_EXIT_SPECS = (
    _TimeExitSpec("live-time-stop-m10-v1", 10),
    _TimeExitSpec("live-time-stop-m15-v1", 15),
    _TimeExitSpec("live-time-stop-m30-v1", 30),
    _TimeExitSpec("live-time-stop-m45-v1", 45),
)
LIVE_EXIT_ALGORITHM_VERSIONS = (
    *LIVE_PROFIT_LOCK_ALGORITHM_VERSIONS,
    *(spec.algorithm_version for spec in _TIME_EXIT_SPECS),
)
_EVALUATOR_VERSION = "live-profit-lock-forward-evaluator-v1"
_TIME_EXIT_EVALUATOR_VERSION = "live-time-stop-forward-evaluator-v1"
_MIN_READY_PAIRS = 20
_MIN_MATURE_PAIRS = 50
_MIN_PROFIT_LOCK_EXITS = 5
_MIN_TIME_STOP_EXITS = 5
_UNCHANGED_EPSILON = 1e-9
_ENTRY_MATCH_TOLERANCE_SECONDS = 300
_BASELINE_MATCH_TOLERANCE_SECONDS = 5
_LiveExitPolicyType = Literal["PROFIT_LOCK", "TIME_STOP"]


def _validated_policy_type(value: str) -> _LiveExitPolicyType:
    if value == "PROFIT_LOCK":
        return "PROFIT_LOCK"
    if value == "TIME_STOP":
        return "TIME_STOP"
    raise ValueError("unsupported live exit challenger policy type")


class LiveExitChallengerService:
    """Compare frozen profit locks with real trades without submitting orders."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def ensure_registrations(
        self,
        *,
        symbol: str,
        market: str,
        now: datetime,
    ) -> bool:
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("live exit challenger symbol is required")
        current = _as_utc(now)
        eligible_after = current.replace(second=0, microsecond=0) + timedelta(
            minutes=1
        )
        slippage_bps = float(settings.entry_round_trip_slippage_bps)
        existing = {
            row.algorithm_version: row
            for row in self.db.query(LiveExitChallengerRegistration).filter(
                LiveExitChallengerRegistration.symbol == normalized
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
            self.db.add(LiveExitChallengerRegistration(
                symbol=normalized,
                market=market.upper(),
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
            self.db.add(LiveExitChallengerRegistration(
                symbol=normalized,
                market=market.upper(),
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

    def prepare_open_position(
        self,
        *,
        symbol: str,
        now: datetime,
    ) -> bool:
        """Attach only a currently open real position created after registration."""
        normalized = symbol.strip().upper()
        tracked = self.db.get(TrackedEntry, normalized)
        if (
            tracked is None
            or tracked.side.upper() != "LONG"
            or tracked.quantity <= 0
            or tracked.cost <= 0
            or tracked.opened_at is None
        ):
            return False
        entry_order = self._entry_order(tracked, now=now)
        if entry_order is None or entry_order.filled_at is None:
            return False
        entry_at = _as_utc(tracked.opened_at)
        entry_price = float(tracked.cost) / float(tracked.quantity)
        quantity = float(tracked.quantity)
        fee_rate = self._entry_fee_rate(
            entry_order,
            entry_price=entry_price,
            quantity=quantity,
        )
        registrations = self.db.query(
            LiveExitChallengerRegistration
        ).filter(
            LiveExitChallengerRegistration.symbol == normalized,
            LiveExitChallengerRegistration.eligible_after
            <= _as_utc(entry_order.filled_at),
            LiveExitChallengerRegistration.eligible_after <= entry_at,
        ).all()
        created = False
        for registration in registrations:
            existing = self.db.query(LiveExitChallengerTrade.id).filter(
                LiveExitChallengerTrade.registration_id == registration.id,
                LiveExitChallengerTrade.entry_order_id == entry_order.id,
            ).first()
            if existing is not None:
                continue
            self.db.add(LiveExitChallengerTrade(
                registration_id=registration.id,
                entry_order_id=entry_order.id,
                symbol=normalized,
                entry_config_version=str(entry_order.config_version or ""),
                status="OPEN",
                entry_at=entry_at,
                entry_price=entry_price,
                quantity=quantity,
                estimated_fee_rate=fee_rate,
                last_bar_at=_minute_floor(entry_at),
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
        normalized = symbol.strip().upper()
        observed = _as_utc(observed_at)
        bar_at = _as_utc(bar.timestamp)
        rows = self.db.query(LiveExitChallengerTrade).filter(
            LiveExitChallengerTrade.symbol == normalized,
            (
                (LiveExitChallengerTrade.status == "OPEN")
                | (LiveExitChallengerTrade.baseline_net_pnl.is_(None))
            ),
        ).all()
        for row in rows:
            registration = self.db.get(
                LiveExitChallengerRegistration,
                row.registration_id,
            )
            if registration is None:
                raise ValueError("live exit challenger registration is missing")
            policy_type = _validated_policy_type(registration.policy_type)
            baseline = self._baseline_exit(row)
            if (
                baseline is not None
                and baseline.filled_at is not None
                and _minute_floor(_as_utc(baseline.filled_at)) <= bar_at
            ):
                if (
                    row.status == "OPEN"
                    and not self._baseline_frontier_complete(row, baseline)
                ):
                    continue
                self._finalize_against_baseline(
                    row,
                    baseline,
                    paired_at=observed,
                )
                self.db.add(row)
                continue
            if row.status != "OPEN" or bar_at <= _as_utc(row.last_bar_at):
                if baseline is not None:
                    self._pair_baseline(row, baseline, paired_at=observed)
                continue
            if policy_type == "TIME_STOP":
                if registration.max_holding_minutes is None:
                    raise ValueError("live time-stop registration has no TTL")
                decision = evaluate_time_exit_bar(
                    config=TimeExitConfig(
                        max_holding_minutes=registration.max_holding_minutes,
                        slippage_bps=registration.slippage_bps,
                    ),
                    entry_at=_as_utc(row.entry_at),
                    bar=bar,
                )
                row.last_bar_at = bar.timestamp
                if decision.action == TimeExitAction.EXIT:
                    if decision.exit_price is None:
                        raise ValueError("live time-stop exit has no price")
                    self._close_challenger(
                        row,
                        exit_at=decision.event_at,
                        exit_price=decision.exit_price,
                        reason="TIME_STOP",
                    )
            else:
                decision = evaluate_profit_lock_bar(
                    config=ProfitLockConfig(
                        activation_pct=registration.activation_pct,
                        locked_profit_pct=registration.locked_profit_pct,
                        slippage_bps=registration.slippage_bps,
                    ),
                    entry_price=row.entry_price,
                    bar=bar,
                    armed_before_bar=row.activation_at is not None,
                )
                row.activation_price = decision.activation_price
                row.floor_price = decision.floor_price
                row.last_bar_at = bar.timestamp
                if decision.action == ProfitLockAction.EXIT:
                    if decision.exit_price is None:
                        raise ValueError(
                            "live profit-lock exit is missing its price"
                        )
                    self._close_challenger(
                        row,
                        exit_at=decision.event_at,
                        exit_price=decision.exit_price,
                        reason="PROFIT_LOCK",
                    )
                elif decision.action == ProfitLockAction.ACTIVATE:
                    row.activation_at = decision.event_at
                    row.activation_effective_at = decision.effective_at
            if baseline is not None:
                self._pair_baseline(row, baseline, paired_at=observed)
            self.db.add(row)
        self.db.flush()

    def sync_baseline_outcomes(
        self,
        *,
        symbol: str,
        paired_at: datetime,
    ) -> None:
        normalized = symbol.strip().upper()
        paired = _as_utc(paired_at)
        rows = self.db.query(LiveExitChallengerTrade).filter(
            LiveExitChallengerTrade.symbol == normalized,
            LiveExitChallengerTrade.baseline_net_pnl.is_(None),
        ).all()
        for row in rows:
            baseline = self._baseline_exit(row)
            if baseline is None:
                continue
            if (
                row.status == "OPEN"
                and not self._baseline_frontier_complete(row, baseline)
            ):
                continue
            self._finalize_against_baseline(
                row,
                baseline,
                paired_at=paired,
            )
            self.db.add(row)
        self.db.flush()

    def get_report(self, symbol: str) -> LiveExitChallengerReport:
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("live exit challenger symbol is required")
        registrations = self.db.query(
            LiveExitChallengerRegistration
        ).filter(
            LiveExitChallengerRegistration.symbol == normalized
        ).order_by(
            LiveExitChallengerRegistration.registered_at.asc(),
            LiveExitChallengerRegistration.id.asc(),
        ).all()
        return LiveExitChallengerReport(
            symbol=normalized,
            enabled=settings.live_exit_challenger_enabled,
            variants=[
                self._variant_report(registration)
                for registration in registrations
            ],
        )

    def _entry_order(
        self,
        tracked: TrackedEntry,
        *,
        now: datetime,
    ) -> OrderRecord | None:
        assert tracked.opened_at is not None
        opened_at = _as_utc(tracked.opened_at)
        candidates = self.db.query(OrderRecord).filter(
            OrderRecord.symbol == tracked.symbol,
            OrderRecord.side == "BUY",
            OrderRecord.status.in_(("FILLED", "PARTIAL_FILLED")),
            OrderRecord.filled_at.isnot(None),
            OrderRecord.filled_at <= opened_at,
            OrderRecord.filled_at <= _as_utc(now),
        ).order_by(
            OrderRecord.filled_at.desc(),
            OrderRecord.id.desc(),
        ).limit(20).all()
        for candidate in candidates:
            assert candidate.filled_at is not None
            seconds_since_fill = (
                opened_at - _as_utc(candidate.filled_at)
            ).total_seconds()
            if not 0 <= seconds_since_fill <= _ENTRY_MATCH_TOLERANCE_SECONDS:
                continue
            quantity = float(
                candidate.executed_quantity
                if candidate.executed_quantity is not None
                else candidate.quantity
            )
            if quantity + _UNCHANGED_EPSILON >= float(tracked.quantity):
                return candidate
        return None

    def _baseline_exit(
        self,
        row: LiveExitChallengerTrade,
    ) -> OrderRecord | None:
        candidates = self.db.query(OrderRecord).filter(
            OrderRecord.symbol == row.symbol,
            OrderRecord.side == "SELL",
            OrderRecord.status == "FILLED",
            OrderRecord.filled_at.isnot(None),
            OrderRecord.filled_at >= row.entry_at,
            OrderRecord.cost_basis_opened_at.isnot(None),
        ).order_by(
            OrderRecord.filled_at.asc(),
            OrderRecord.id.asc(),
        ).all()
        for candidate in candidates:
            assert candidate.cost_basis_opened_at is not None
            if (
                abs(
                    (
                        _as_utc(candidate.cost_basis_opened_at)
                        - _as_utc(row.entry_at)
                    ).total_seconds()
                )
                > _BASELINE_MATCH_TOLERANCE_SECONDS
            ):
                continue
            quantity = float(
                candidate.executed_quantity
                if candidate.executed_quantity is not None
                else candidate.quantity
            )
            if quantity + _UNCHANGED_EPSILON >= row.quantity:
                return candidate
        return None

    def _entry_fee_rate(
        self,
        order: OrderRecord,
        *,
        entry_price: float,
        quantity: float,
    ) -> float:
        notional = entry_price * quantity
        if (
            order.estimated_fee is not None
            and math.isfinite(float(order.estimated_fee))
            and float(order.estimated_fee) >= 0
            and notional > 0
        ):
            return float(order.estimated_fee) / notional
        config = self.db.query(StrategyConfig).order_by(
            StrategyConfig.id.desc()
        ).first()
        if _market_for_symbol(order.symbol) == "HK":
            return float(
                getattr(config, "fee_rate_hk", 0.003)
                if config is not None
                else 0.003
            )
        return float(
            getattr(config, "fee_rate_us", 0.0005)
            if config is not None
            else 0.0005
        )

    @staticmethod
    def _close_challenger(
        row: LiveExitChallengerTrade,
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

    @staticmethod
    def _close_from_baseline(
        row: LiveExitChallengerTrade,
        baseline: OrderRecord,
    ) -> bool:
        if (
            baseline.filled_at is None
            or baseline.gross_pnl is None
            or baseline.pnl_fee is None
            or baseline.net_pnl is None
        ):
            return False
        exit_price = float(
            baseline.executed_price
            if baseline.executed_price is not None
            else baseline.price
        )
        gross_pnl = float(baseline.gross_pnl)
        pnl_fee = float(baseline.pnl_fee)
        net_pnl = float(baseline.net_pnl)
        if (
            not math.isfinite(exit_price)
            or exit_price <= 0
            or not math.isfinite(gross_pnl)
            or not math.isfinite(pnl_fee)
            or pnl_fee < 0
            or not math.isfinite(net_pnl)
        ):
            return False
        row.status = "CLOSED"
        row.challenger_exit_at = baseline.filled_at
        row.challenger_exit_price = exit_price
        row.challenger_exit_reason = (
            f"BASELINE_{baseline.exit_cause or 'EXIT'}"
        )
        row.challenger_gross_pnl = gross_pnl
        row.challenger_estimated_fees = pnl_fee
        row.challenger_net_pnl = net_pnl
        return True

    def _finalize_against_baseline(
        self,
        row: LiveExitChallengerTrade,
        baseline: OrderRecord,
        *,
        paired_at: datetime,
    ) -> None:
        if row.status == "OPEN" and not self._close_from_baseline(
            row,
            baseline,
        ):
            return
        self._pair_baseline(row, baseline, paired_at=paired_at)

    @staticmethod
    def _baseline_frontier_complete(
        row: LiveExitChallengerTrade,
        baseline: OrderRecord,
    ) -> bool:
        if baseline.filled_at is None:
            return False
        entry_minute = _minute_floor(row.entry_at)
        exit_minute = _minute_floor(baseline.filled_at)
        required_through = max(
            entry_minute,
            exit_minute - timedelta(minutes=1),
        )
        return _as_utc(row.last_bar_at) >= required_through

    @staticmethod
    def _pair_baseline(
        row: LiveExitChallengerTrade,
        baseline: OrderRecord,
        *,
        paired_at: datetime,
    ) -> None:
        if (
            row.baseline_net_pnl is not None
            or row.challenger_net_pnl is None
            or baseline.filled_at is None
            or baseline.net_pnl is None
        ):
            return
        exit_price = float(
            baseline.executed_price
            if baseline.executed_price is not None
            else baseline.price
        )
        baseline_net_pnl = float(baseline.net_pnl)
        if (
            _as_utc(baseline.filled_at) < _as_utc(row.entry_at)
            or not math.isfinite(exit_price)
            or exit_price <= 0
            or not math.isfinite(baseline_net_pnl)
        ):
            return
        row.baseline_exit_order_id = baseline.id
        row.baseline_exit_at = baseline.filled_at
        row.baseline_exit_price = exit_price
        row.baseline_exit_reason = (
            baseline.exit_cause or baseline.exit_reason or "EXIT"
        )
        row.baseline_net_pnl = baseline_net_pnl
        row.net_pnl_delta = row.challenger_net_pnl - baseline_net_pnl
        row.paired_at = paired_at

    @staticmethod
    def _paired_evidence(
        rows: list[LiveExitChallengerTrade],
    ) -> tuple[
        list[LiveExitChallengerTrade],
        list[float],
        list[float],
        list[float],
        list[float],
        list[float],
        bool,
    ]:
        paired: list[LiveExitChallengerTrade] = []
        baseline_values: list[float] = []
        challenger_values: list[float] = []
        deltas: list[float] = []
        baseline_holding_minutes: list[float] = []
        challenger_holding_minutes: list[float] = []
        evidence_data_valid = True
        for row in rows:
            complete_pnl = (
                row.baseline_net_pnl is not None
                and row.challenger_net_pnl is not None
                and row.net_pnl_delta is not None
            )
            has_pair_marker = (
                row.baseline_net_pnl is not None
                or row.net_pnl_delta is not None
                or row.baseline_exit_at is not None
                or row.paired_at is not None
            )
            if not complete_pnl:
                if has_pair_marker:
                    evidence_data_valid = False
                continue
            assert row.baseline_net_pnl is not None
            assert row.challenger_net_pnl is not None
            assert row.net_pnl_delta is not None
            if (
                row.baseline_exit_at is None
                or row.challenger_exit_at is None
                or row.paired_at is None
            ):
                evidence_data_valid = False
                continue
            try:
                baseline_net_pnl = float(row.baseline_net_pnl)
                challenger_net_pnl = float(row.challenger_net_pnl)
                net_pnl_delta = float(row.net_pnl_delta)
                entry_at = _as_utc(row.entry_at)
                baseline_exit_at = _as_utc(row.baseline_exit_at)
                challenger_exit_at = _as_utc(row.challenger_exit_at)
                paired_at = _as_utc(row.paired_at)
            except (
                AttributeError,
                TypeError,
                ValueError,
                OverflowError,
            ):
                evidence_data_valid = False
                continue
            if (
                not math.isfinite(baseline_net_pnl)
                or not math.isfinite(challenger_net_pnl)
                or not math.isfinite(net_pnl_delta)
                or not math.isclose(
                    net_pnl_delta,
                    challenger_net_pnl - baseline_net_pnl,
                    rel_tol=1e-9,
                    abs_tol=1e-9,
                )
                or baseline_exit_at < entry_at
                or challenger_exit_at < entry_at
                or paired_at < baseline_exit_at
                or paired_at < challenger_exit_at
            ):
                evidence_data_valid = False
                continue
            paired.append(row)
            baseline_values.append(baseline_net_pnl)
            challenger_values.append(challenger_net_pnl)
            deltas.append(net_pnl_delta)
            baseline_holding_minutes.append(
                (baseline_exit_at - entry_at).total_seconds() / 60.0
            )
            challenger_holding_minutes.append(
                (challenger_exit_at - entry_at).total_seconds() / 60.0
            )
        if not (
            len(baseline_values)
            == len(challenger_values)
            == len(deltas)
            == len(baseline_holding_minutes)
            == len(challenger_holding_minutes)
            == len(paired)
        ):
            evidence_data_valid = False
        if not evidence_data_valid:
            paired = []
            baseline_values = []
            challenger_values = []
            deltas = []
            baseline_holding_minutes = []
            challenger_holding_minutes = []
        return (
            paired,
            baseline_values,
            challenger_values,
            deltas,
            baseline_holding_minutes,
            challenger_holding_minutes,
            evidence_data_valid,
        )

    def _variant_report(
        self,
        registration: LiveExitChallengerRegistration,
    ) -> LiveExitChallengerVariant:
        rows = self.db.query(LiveExitChallengerTrade).filter(
            LiveExitChallengerTrade.registration_id == registration.id
        ).order_by(
            LiveExitChallengerTrade.baseline_exit_at.asc(),
            LiveExitChallengerTrade.id.asc(),
        ).all()
        (
            paired,
            baseline_values,
            challenger_values,
            deltas,
            baseline_holding_minutes,
            challenger_holding_minutes,
            evidence_data_valid,
        ) = self._paired_evidence(rows)
        baseline_mean_holding = (
            sum(baseline_holding_minutes) / len(paired)
            if evidence_data_valid and paired
            else 0.0
        )
        challenger_mean_holding = (
            sum(challenger_holding_minutes) / len(paired)
            if evidence_data_valid and paired
            else 0.0
        )
        profit_lock_exits = sum(
            row.challenger_exit_reason == "PROFIT_LOCK"
            for row in paired
        )
        time_stop_exits = sum(
            row.challenger_exit_reason == "TIME_STOP"
            for row in paired
        )
        baseline_net = sum(baseline_values)
        challenger_net = sum(challenger_values)
        net_delta = sum(deltas)
        baseline_drawdown = _max_drawdown(baseline_values)
        challenger_drawdown = _max_drawdown(challenger_values)
        blockers: list[str] = []
        if not evidence_data_valid:
            blockers.append("EVIDENCE_DATA_INVALID")
        if len(paired) < _MIN_READY_PAIRS:
            blockers.append("MIN_PAIRED_TRADES")
        policy_type = _validated_policy_type(registration.policy_type)
        if policy_type == "TIME_STOP":
            if time_stop_exits < _MIN_TIME_STOP_EXITS:
                blockers.append("MIN_TIME_STOP_EXITS")
        elif profit_lock_exits < _MIN_PROFIT_LOCK_EXITS:
            blockers.append("MIN_PROFIT_LOCK_EXITS")
        if paired and net_delta <= 0:
            blockers.append("NET_PNL_DELTA_NON_POSITIVE")
        if (
            paired
            and challenger_drawdown
            > baseline_drawdown + _UNCHANGED_EPSILON
        ):
            blockers.append("MAX_DRAWDOWN_WORSE")
        status = (
            "MATURE_EVIDENCE"
            if len(paired) >= _MIN_MATURE_PAIRS
            else "READY_FOR_REVIEW"
            if len(paired) >= _MIN_READY_PAIRS
            else "COLLECTING"
        )
        return LiveExitChallengerVariant(
            registration_id=registration.id,
            algorithm_version=registration.algorithm_version,
            evaluator_digest=registration.evaluator_digest,
            policy_type=policy_type,
            activation_pct=registration.activation_pct,
            locked_profit_pct=registration.locked_profit_pct,
            max_holding_minutes=registration.max_holding_minutes,
            slippage_bps=registration.slippage_bps,
            registered_at=registration.registered_at,
            eligible_after=registration.eligible_after,
            status=status,
            entry_config_versions=sorted({
                row.entry_config_version
                for row in rows
                if row.entry_config_version
            }),
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
                if evidence_data_valid and paired
                else 0.0
            ),
            challenger_win_rate=(
                sum(value > 0 for value in challenger_values) / len(paired)
                if evidence_data_valid and paired
                else 0.0
            ),
            baseline_net_pnl=baseline_net,
            challenger_net_pnl=challenger_net,
            net_pnl_delta=net_delta,
            mean_net_pnl_delta=(
                net_delta / len(paired)
                if evidence_data_valid and paired
                else 0.0
            ),
            baseline_mean_holding_minutes=baseline_mean_holding,
            challenger_mean_holding_minutes=challenger_mean_holding,
            mean_holding_minutes_saved=(
                baseline_mean_holding - challenger_mean_holding
            ),
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
            "activation_effective": "NEXT_BAR",
            "baseline_exit_priority": "BASELINE_EXIT_MINUTE",
            "position_side": "LONG",
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_frozen_registration(
        row: LiveExitChallengerRegistration,
        *,
        market: str,
        spec: _ProfitLockSpec,
        slippage_bps: float,
        digest: str,
    ) -> None:
        if (
            row.market != market.upper()
            or row.policy_type != "PROFIT_LOCK"
            or row.activation_pct != spec.activation_pct
            or row.locked_profit_pct != spec.locked_profit_pct
            or row.max_holding_minutes is not None
            or row.slippage_bps != slippage_bps
            or row.evaluator_digest != digest
        ):
            raise ValueError(
                "persisted live exit challenger differs from frozen evaluator"
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
            "exit_trigger": "FIRST_BAR_OPEN_AT_OR_AFTER_ENTRY_PLUS_TTL",
            "baseline_exit_priority": "BASELINE_EXIT_MINUTE",
            "position_side": "LONG",
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_frozen_time_exit_registration(
        row: LiveExitChallengerRegistration,
        *,
        market: str,
        spec: _TimeExitSpec,
        slippage_bps: float,
        digest: str,
    ) -> None:
        if (
            row.market != market.upper()
            or row.policy_type != "TIME_STOP"
            or row.activation_pct != 0.0
            or row.locked_profit_pct != 0.0
            or row.max_holding_minutes != spec.max_holding_minutes
            or row.slippage_bps != slippage_bps
            or row.evaluator_digest != digest
        ):
            raise ValueError(
                "persisted live time-stop differs from frozen evaluator"
            )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _minute_floor(value: datetime) -> datetime:
    return _as_utc(value).replace(second=0, microsecond=0)


def _market_for_symbol(symbol: str) -> str:
    return "HK" if symbol.upper().endswith(".HK") else "US"


def _max_drawdown(values: list[float]) -> float:
    cumulative = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return drawdown
