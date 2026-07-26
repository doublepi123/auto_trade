from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.domain.strategy_v2 import (
    ProfitLockAction,
    ProfitLockConfig,
    StrategyBar,
    evaluate_profit_lock_bar,
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


_PROFIT_LOCK_SPECS = (
    _ProfitLockSpec("live-profit-lock-a40-f10-v1", 0.40, 0.10),
    _ProfitLockSpec("live-profit-lock-a40-f20-v1", 0.40, 0.20),
    _ProfitLockSpec("live-profit-lock-a40-f30-v1", 0.40, 0.30),
    _ProfitLockSpec("live-profit-lock-a60-f40-v1", 0.60, 0.40),
)
_EVALUATOR_VERSION = "live-profit-lock-forward-evaluator-v1"
_MIN_READY_PAIRS = 20
_MIN_MATURE_PAIRS = 50
_MIN_PROFIT_LOCK_EXITS = 5
_UNCHANGED_EPSILON = 1e-9
_ENTRY_MATCH_TOLERANCE_SECONDS = 300
_BASELINE_MATCH_TOLERANCE_SECONDS = 5


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
                activation_pct=spec.activation_pct,
                locked_profit_pct=spec.locked_profit_pct,
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
                self._close_with_profit_lock(
                    row,
                    exit_at=decision.event_at,
                    exit_price=decision.exit_price,
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
            OrderRecord.filled_at <= _as_utc(now),
        ).order_by(
            OrderRecord.filled_at.desc(),
            OrderRecord.id.desc(),
        ).limit(20).all()
        for candidate in candidates:
            assert candidate.filled_at is not None
            if (
                abs(
                    (
                        _as_utc(candidate.filled_at) - opened_at
                    ).total_seconds()
                )
                > _ENTRY_MATCH_TOLERANCE_SECONDS
            ):
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
    def _close_with_profit_lock(
        row: LiveExitChallengerTrade,
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
    def _close_from_baseline(
        row: LiveExitChallengerTrade,
        baseline: OrderRecord,
    ) -> bool:
        if (
            baseline.filled_at is None
            or baseline.net_pnl is None
        ):
            return False
        exit_price = float(
            baseline.executed_price
            if baseline.executed_price is not None
            else baseline.price
        )
        row.status = "CLOSED"
        row.challenger_exit_at = baseline.filled_at
        row.challenger_exit_price = exit_price
        row.challenger_exit_reason = (
            f"BASELINE_{baseline.exit_cause or 'EXIT'}"
        )
        row.challenger_gross_pnl = baseline.gross_pnl
        row.challenger_estimated_fees = baseline.pnl_fee
        row.challenger_net_pnl = baseline.net_pnl
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
        row.baseline_exit_order_id = baseline.id
        row.baseline_exit_at = baseline.filled_at
        row.baseline_exit_price = exit_price
        row.baseline_exit_reason = (
            baseline.exit_cause or baseline.exit_reason or "EXIT"
        )
        row.baseline_net_pnl = baseline.net_pnl
        row.net_pnl_delta = row.challenger_net_pnl - baseline.net_pnl
        row.paired_at = paired_at

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
            for row in paired
        )
        baseline_net = sum(baseline_values)
        challenger_net = sum(challenger_values)
        net_delta = sum(deltas)
        baseline_drawdown = _max_drawdown(baseline_values)
        challenger_drawdown = _max_drawdown(challenger_values)
        blockers: list[str] = []
        if len(paired) < _MIN_READY_PAIRS:
            blockers.append("MIN_PAIRED_TRADES")
        if profit_lock_exits < _MIN_PROFIT_LOCK_EXITS:
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
            activation_pct=registration.activation_pct,
            locked_profit_pct=registration.locked_profit_pct,
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
            mean_net_pnl_delta=(
                net_delta / len(paired) if paired else 0.0
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
            or row.activation_pct != spec.activation_pct
            or row.locked_profit_pct != spec.locked_profit_pct
            or row.slippage_bps != slippage_bps
            or row.evaluator_digest != digest
        ):
            raise ValueError(
                "persisted live exit challenger differs from frozen evaluator"
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
