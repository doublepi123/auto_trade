from __future__ import annotations

import math
import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any

from app.core.market_calendar import get_session, is_closing_window
from app.domain.strategy_v2.features import (
    SessionFeatureEngine,
    StrategyBar,
    StrategyV2FeatureConfig,
    StrategyV2FeatureSnapshot,
)


class StrategyV2State(str, Enum):
    COLD = "COLD"
    READY = "READY"
    ARMED_LONG = "ARMED_LONG"
    ENTRY_PENDING = "ENTRY_PENDING"
    LONG = "LONG"


class StrategyV2Action(str, Enum):
    WAIT = "WAIT"
    ARM_LONG = "ARM_LONG"
    CANCEL_ARM = "CANCEL_ARM"
    SUBMIT_ENTRY = "SUBMIT_ENTRY"
    CANCEL_ENTRY = "CANCEL_ENTRY"
    FILL_ENTRY = "FILL_ENTRY"
    EXIT_LONG = "EXIT_LONG"


@dataclass(frozen=True)
class StrategyV2Config:
    market: str = "US"
    zscore_window_1m: int = 30
    zscore_window_5m: int = 12
    adx_period: int = 14
    realized_vol_window_1m: int = 30
    breach_zscore_1m: float = -2.0
    reclaim_zscore_1m: float = -1.0
    five_minute_zscore_max: float = -0.5
    adx_max: float = 20.0
    realized_vol_min: float = 0.10
    realized_vol_max: float = 0.80
    residual_sigma_min: float = 0.0008
    arm_ttl_bars: int = 10
    stop_loss_pct: float = 0.75
    profit_target_pct: float = 0.50
    max_holding_minutes: int = 60
    entry_cutoff_minutes_before_close: int = 45
    flatten_minutes_before_close: int = 15
    max_entries_per_session: int = 2
    entry_cooldown_minutes: int = 15
    virtual_quantity: float = 1.0
    slippage_bps: float = 2.0
    settlement_grace_seconds: int = 5

    def __post_init__(self) -> None:
        market = self.market.upper()
        if market not in {"US", "HK"}:
            raise ValueError("market must be US or HK")
        object.__setattr__(self, "market", market)
        finite_values = (
            self.breach_zscore_1m,
            self.reclaim_zscore_1m,
            self.five_minute_zscore_max,
            self.adx_max,
            self.realized_vol_min,
            self.realized_vol_max,
            self.residual_sigma_min,
            self.stop_loss_pct,
            self.profit_target_pct,
            self.virtual_quantity,
            self.slippage_bps,
        )
        if any(not math.isfinite(value) for value in finite_values):
            raise ValueError("strategy thresholds must be finite")
        if self.breach_zscore_1m >= self.reclaim_zscore_1m:
            raise ValueError("breach_zscore_1m must be below reclaim_zscore_1m")
        if not 0 <= self.adx_max <= 100:
            raise ValueError("adx_max must be in [0, 100]")
        if self.realized_vol_min < 0 or self.realized_vol_max <= self.realized_vol_min:
            raise ValueError("realized-vol bounds are invalid")
        if self.residual_sigma_min < 0:
            raise ValueError("residual_sigma_min must be non-negative")
        if self.stop_loss_pct <= 0 or self.stop_loss_pct > 0.75:
            raise ValueError("stop_loss_pct must be in (0, 0.75]")
        if self.profit_target_pct <= 0:
            raise ValueError("profit_target_pct must be positive")
        if self.max_holding_minutes <= 0 or self.max_holding_minutes > 60:
            raise ValueError("max_holding_minutes must be in [1, 60]")
        if self.entry_cutoff_minutes_before_close < 45:
            raise ValueError("entry cutoff must be at least 45 minutes")
        if self.flatten_minutes_before_close < 15:
            raise ValueError("flatten window must be at least 15 minutes")
        if self.flatten_minutes_before_close > self.entry_cutoff_minutes_before_close:
            raise ValueError("flatten window must not exceed entry cutoff")
        if (
            self.arm_ttl_bars <= 0
            or self.max_entries_per_session <= 0
            or self.max_entries_per_session > 2
        ):
            raise ValueError("arm TTL and max entries must be positive")
        if self.entry_cooldown_minutes < 15 or self.virtual_quantity <= 0:
            raise ValueError("cooldown must be at least 15 minutes and quantity positive")
        if not 0 <= self.slippage_bps <= 50:
            raise ValueError("slippage_bps must be in [0, 50]")
        if not 0 <= self.settlement_grace_seconds <= 60:
            raise ValueError("settlement_grace_seconds must be in [0, 60]")

    def feature_config(self) -> StrategyV2FeatureConfig:
        return StrategyV2FeatureConfig(
            market=self.market,
            zscore_window_1m=self.zscore_window_1m,
            zscore_window_5m=self.zscore_window_5m,
            adx_period=self.adx_period,
            realized_vol_window_1m=self.realized_vol_window_1m,
            settlement_grace_seconds=self.settlement_grace_seconds,
        )

    def version_hash(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VirtualPosition:
    entry_price: float
    entry_at: datetime
    quantity: float
    stop_price: float
    target_price: float
    signal_vwap: float
    holding_deadline: datetime
    config_version: str


@dataclass(frozen=True)
class StrategyV2EngineSnapshot:
    state: StrategyV2State
    session_day: date | None
    arm_bar_index: int | None
    arm_previous_zscore: float | None
    arm_trough_zscore: float | None
    pending_signal_at: datetime | None
    pending_signal_bar_index: int | None
    pending_signal_vwap: float | None
    entries_this_session: int
    last_exit_at: datetime | None
    position: VirtualPosition | None
    last_processed_session_day: date | None
    last_processed_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        position = None
        if self.position is not None:
            position = {
                **asdict(self.position),
                "entry_at": self.position.entry_at.isoformat(),
                "holding_deadline": self.position.holding_deadline.isoformat(),
            }
        return {
            "state": self.state.value,
            "session_day": self.session_day.isoformat() if self.session_day else None,
            "arm_bar_index": self.arm_bar_index,
            "arm_previous_zscore": self.arm_previous_zscore,
            "arm_trough_zscore": self.arm_trough_zscore,
            "pending_signal_at": self.pending_signal_at.isoformat() if self.pending_signal_at else None,
            "pending_signal_bar_index": self.pending_signal_bar_index,
            "pending_signal_vwap": self.pending_signal_vwap,
            "entries_this_session": self.entries_this_session,
            "last_exit_at": self.last_exit_at.isoformat() if self.last_exit_at else None,
            "position": position,
            "last_processed_session_day": (
                self.last_processed_session_day.isoformat()
                if self.last_processed_session_day
                else None
            ),
            "last_processed_at": (
                self.last_processed_at.isoformat() if self.last_processed_at else None
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> StrategyV2EngineSnapshot:
        raw_position = payload.get("position")
        position: VirtualPosition | None = None
        if isinstance(raw_position, Mapping):
            position = VirtualPosition(
                entry_price=float(raw_position["entry_price"]),
                entry_at=datetime.fromisoformat(str(raw_position["entry_at"])),
                quantity=float(raw_position["quantity"]),
                stop_price=float(raw_position["stop_price"]),
                target_price=float(raw_position["target_price"]),
                signal_vwap=float(raw_position["signal_vwap"]),
                holding_deadline=datetime.fromisoformat(str(raw_position["holding_deadline"])),
                config_version=str(raw_position["config_version"]),
            )
        return cls(
            state=StrategyV2State(str(payload.get("state", StrategyV2State.COLD.value))),
            session_day=_optional_date(payload.get("session_day")),
            arm_bar_index=_optional_int(payload.get("arm_bar_index")),
            arm_previous_zscore=_optional_float(payload.get("arm_previous_zscore")),
            arm_trough_zscore=_optional_float(payload.get("arm_trough_zscore")),
            pending_signal_at=_optional_datetime(payload.get("pending_signal_at")),
            pending_signal_bar_index=_optional_int(payload.get("pending_signal_bar_index")),
            pending_signal_vwap=_optional_float(payload.get("pending_signal_vwap")),
            entries_this_session=int(payload.get("entries_this_session", 0)),
            last_exit_at=_optional_datetime(payload.get("last_exit_at")),
            position=position,
            last_processed_session_day=_optional_date(payload.get("last_processed_session_day")),
            last_processed_at=_optional_datetime(payload.get("last_processed_at")),
        )


def _optional_datetime(value: Any) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value not in (None, "") else None


def _optional_date(value: Any) -> date | None:
    return date.fromisoformat(str(value)) if value not in (None, "") else None


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _optional_float(value: Any) -> float | None:
    return float(value) if value is not None else None


@dataclass(frozen=True)
class StrategyV2Decision:
    timestamp: datetime
    action: StrategyV2Action
    reason: str
    state_before: StrategyV2State
    state_after: StrategyV2State
    price: float | None = None
    quantity: float = 0.0
    stop_price: float | None = None
    target_price: float | None = None


@dataclass(frozen=True)
class StrategyV2Step:
    feature: StrategyV2FeatureSnapshot | None
    decisions: tuple[StrategyV2Decision, ...]
    state: StrategyV2State
    position: VirtualPosition | None


@dataclass(frozen=True)
class _PendingEntry:
    signal_at: datetime
    signal_bar_index: int
    signal_vwap: float


class StrategyV2Engine:
    """Long-only, shadow-only RTH VWAP mean-reversion state machine."""

    def __init__(
        self,
        config: StrategyV2Config | None = None,
        *,
        features: SessionFeatureEngine | None = None,
    ) -> None:
        self.config = config or StrategyV2Config()
        self.features = features or SessionFeatureEngine(self.config.feature_config())
        self.state = StrategyV2State.COLD
        self.position: VirtualPosition | None = None
        self._session_day: date | None = None
        self._arm_bar_index: int | None = None
        self._arm_previous_zscore: float | None = None
        self._arm_trough_zscore: float | None = None
        self._pending_entry: _PendingEntry | None = None
        self._entries_this_session = 0
        self._last_exit_at: datetime | None = None
        self._last_processed_key: tuple[date, datetime] | None = None

    @property
    def entries_this_session(self) -> int:
        return self._entries_this_session

    def reset(self) -> None:
        self.features.reset()
        self.state = StrategyV2State.COLD
        self.position = None
        self._session_day = None
        self._clear_arm()
        self._pending_entry = None
        self._entries_this_session = 0
        self._last_exit_at = None
        self._last_processed_key = None

    def snapshot(self) -> StrategyV2EngineSnapshot:
        pending = self._pending_entry
        processed_day = self._last_processed_key[0] if self._last_processed_key else None
        processed_at = self._last_processed_key[1] if self._last_processed_key else None
        return StrategyV2EngineSnapshot(
            state=self.state,
            session_day=self._session_day,
            arm_bar_index=self._arm_bar_index,
            arm_previous_zscore=self._arm_previous_zscore,
            arm_trough_zscore=self._arm_trough_zscore,
            pending_signal_at=pending.signal_at if pending else None,
            pending_signal_bar_index=pending.signal_bar_index if pending else None,
            pending_signal_vwap=pending.signal_vwap if pending else None,
            entries_this_session=self._entries_this_session,
            last_exit_at=self._last_exit_at,
            position=self.position,
            last_processed_session_day=processed_day,
            last_processed_at=processed_at,
        )

    def restore(self, snapshot: StrategyV2EngineSnapshot | Mapping[str, Any]) -> None:
        restored = (
            StrategyV2EngineSnapshot.from_dict(snapshot)
            if isinstance(snapshot, Mapping)
            else snapshot
        )
        if restored.state == StrategyV2State.LONG and restored.position is None:
            raise ValueError("LONG snapshot requires a virtual position")
        if restored.state != StrategyV2State.LONG and restored.position is not None:
            raise ValueError("only LONG snapshot may contain a virtual position")
        pending_values = (
            restored.pending_signal_at,
            restored.pending_signal_bar_index,
            restored.pending_signal_vwap,
        )
        if restored.state == StrategyV2State.ENTRY_PENDING and any(
            value is None for value in pending_values
        ):
            raise ValueError("ENTRY_PENDING snapshot requires pending-entry metadata")
        if restored.state != StrategyV2State.ENTRY_PENDING and any(
            value is not None for value in pending_values
        ):
            raise ValueError("pending-entry metadata requires ENTRY_PENDING state")
        arm_values = (
            restored.arm_bar_index,
            restored.arm_previous_zscore,
            restored.arm_trough_zscore,
        )
        if restored.state == StrategyV2State.ARMED_LONG and any(
            value is None for value in arm_values
        ):
            raise ValueError("ARMED_LONG snapshot requires arm metadata")
        if restored.state != StrategyV2State.ARMED_LONG and any(
            value is not None for value in arm_values
        ):
            raise ValueError("arm metadata requires ARMED_LONG state")
        if not 0 <= restored.entries_this_session <= self.config.max_entries_per_session:
            raise ValueError("snapshot session entry count is invalid")
        processed_values = (
            restored.last_processed_session_day,
            restored.last_processed_at,
        )
        if (processed_values[0] is None) != (processed_values[1] is None):
            raise ValueError("snapshot last-processed key is incomplete")
        self.state = restored.state
        self.position = restored.position
        self._session_day = restored.session_day
        self._arm_bar_index = restored.arm_bar_index
        self._arm_previous_zscore = restored.arm_previous_zscore
        self._arm_trough_zscore = restored.arm_trough_zscore
        self._pending_entry = (
            _PendingEntry(
                signal_at=restored.pending_signal_at,
                signal_bar_index=int(restored.pending_signal_bar_index),
                signal_vwap=float(restored.pending_signal_vwap),
            )
            if restored.state == StrategyV2State.ENTRY_PENDING
            and restored.pending_signal_at is not None
            and restored.pending_signal_bar_index is not None
            and restored.pending_signal_vwap is not None
            else None
        )
        self._entries_this_session = restored.entries_this_session
        self._last_exit_at = restored.last_exit_at
        self._last_processed_key = (
            (restored.last_processed_session_day, restored.last_processed_at)
            if restored.last_processed_session_day is not None
            and restored.last_processed_at is not None
            else None
        )

    def on_bar(
        self,
        bar: StrategyBar,
        *,
        observed_at: datetime | None = None,
    ) -> StrategyV2Step:
        feature = self.features.on_bar(bar, observed_at=observed_at)
        if feature is None:
            return StrategyV2Step(
                feature=None,
                decisions=(),
                state=self.state,
                position=self.position,
            )
        return self.on_feature(feature)

    def on_feature(self, feature: StrategyV2FeatureSnapshot) -> StrategyV2Step:
        bar = feature.bar
        processed_key = (feature.session_day, bar.timestamp)
        if self._last_processed_key == processed_key:
            return StrategyV2Step(
                feature=feature,
                decisions=(),
                state=self.state,
                position=self.position,
            )
        if self._last_processed_key is not None and processed_key < self._last_processed_key:
            raise ValueError("feature snapshots must be processed in timestamp order")
        decisions: list[StrategyV2Decision] = []
        if feature.session_day != self._session_day:
            decisions.extend(self._start_session(feature))

        if self.state == StrategyV2State.ENTRY_PENDING:
            decisions.append(self._fill_pending_entry(feature))

        if self.state == StrategyV2State.LONG and self.position is not None:
            exit_decision = self._evaluate_exit(feature)
            if exit_decision is not None:
                decisions.append(exit_decision)

        had_position_event = any(
            decision.action
            in {
                StrategyV2Action.FILL_ENTRY,
                StrategyV2Action.CANCEL_ENTRY,
                StrategyV2Action.EXIT_LONG,
            }
            for decision in decisions
        )

        if self.state == StrategyV2State.COLD and feature.ready and not had_position_event:
            self.state = StrategyV2State.READY

        if self.state == StrategyV2State.READY and not had_position_event:
            gate_reasons = self.entry_gate_reasons(feature)
            zscore = feature.zscore_1m
            if not gate_reasons and zscore is not None and zscore <= self.config.breach_zscore_1m:
                before = self.state
                self.state = StrategyV2State.ARMED_LONG
                self._arm_bar_index = feature.bar_index
                self._arm_previous_zscore = zscore
                self._arm_trough_zscore = zscore
                decisions.append(self._decision(
                    feature,
                    StrategyV2Action.ARM_LONG,
                    "ZSCORE_1M_BREACH",
                    before,
                ))
            elif not decisions:
                decisions.append(self._wait(feature, gate_reasons or ("NO_BREACH",)))

        elif self.state == StrategyV2State.ARMED_LONG:
            decisions.append(self._evaluate_armed(feature))
        elif not decisions:
            decisions.append(self._wait(feature, (self.state.value,)))

        step = StrategyV2Step(
            feature=feature,
            decisions=tuple(decisions),
            state=self.state,
            position=self.position,
        )
        self._last_processed_key = processed_key
        return step

    def entry_gate_reasons(self, feature: StrategyV2FeatureSnapshot) -> tuple[str, ...]:
        reasons = list(feature.gate_reasons)
        if not feature.ready and not reasons:
            reasons.append("FEATURES_NOT_READY")
        if feature.residual_sigma_1m is None or feature.residual_sigma_1m < self.config.residual_sigma_min:
            reasons.append("RESIDUAL_SIGMA_1M_TOO_LOW")
        if feature.residual_sigma_5m is None or feature.residual_sigma_5m < self.config.residual_sigma_min:
            reasons.append("RESIDUAL_SIGMA_5M_TOO_LOW")
        if feature.zscore_5m is None or feature.zscore_5m > self.config.five_minute_zscore_max:
            reasons.append("ZSCORE_5M_NOT_OVERSOLD")
        if feature.adx_5m is None or feature.adx_5m > self.config.adx_max:
            reasons.append("ADX_REGIME_BLOCKED")
        realized_vol = feature.realized_vol_1m
        if (
            realized_vol is None
            or realized_vol < self.config.realized_vol_min
            or realized_vol > self.config.realized_vol_max
        ):
            reasons.append("REALIZED_VOL_REGIME_BLOCKED")
        if self._closing_window_reached(
            self.config.market,
            self.config.entry_cutoff_minutes_before_close,
            feature.bar.end_at,
        ):
            reasons.append("ENTRY_CUTOFF")
        if self._entries_this_session >= self.config.max_entries_per_session:
            reasons.append("MAX_SESSION_ENTRIES")
        if (
            self._last_exit_at is not None
            and feature.bar.timestamp
            < self._last_exit_at + timedelta(minutes=self.config.entry_cooldown_minutes)
        ):
            reasons.append("ENTRY_COOLDOWN")
        return tuple(dict.fromkeys(reasons))

    def _start_session(self, feature: StrategyV2FeatureSnapshot) -> list[StrategyV2Decision]:
        before = self.state
        self._session_day = feature.session_day
        self._entries_this_session = 0
        self._last_exit_at = None
        self._clear_arm()
        self._pending_entry = None
        if self.position is None:
            self.state = StrategyV2State.COLD
            return []
        price = self._adverse_sell_price(feature.bar.open)
        quantity = self.position.quantity
        self.position = None
        self.state = StrategyV2State.COLD
        return [
            StrategyV2Decision(
                timestamp=feature.bar.timestamp,
                action=StrategyV2Action.EXIT_LONG,
                reason="OVERNIGHT_SAFETY_FLATTEN",
                state_before=before,
                state_after=self.state,
                price=price,
                quantity=quantity,
            )
        ]

    def _fill_pending_entry(self, feature: StrategyV2FeatureSnapshot) -> StrategyV2Decision:
        pending = self._pending_entry
        if pending is None:
            raise RuntimeError("ENTRY_PENDING state has no pending entry")
        before = self.state
        structure_reasons = self._pending_entry_structure_reasons(feature, pending)
        if structure_reasons:
            self._pending_entry = None
            self.state = StrategyV2State.READY if feature.ready else StrategyV2State.COLD
            return self._decision(
                feature,
                StrategyV2Action.CANCEL_ENTRY,
                structure_reasons[0],
                before,
            )
        price = self._adverse_buy_price(feature.bar.open)
        stop_price = price * (1.0 - self.config.stop_loss_pct / 100.0)
        target_price = max(
            price * (1.0 + self.config.profit_target_pct / 100.0),
            pending.signal_vwap,
        )
        self.position = VirtualPosition(
            entry_price=price,
            entry_at=feature.bar.timestamp,
            quantity=self.config.virtual_quantity,
            stop_price=stop_price,
            target_price=target_price,
            signal_vwap=pending.signal_vwap,
            holding_deadline=feature.bar.timestamp
            + timedelta(minutes=self.config.max_holding_minutes),
            config_version=self.config.version_hash(),
        )
        self._pending_entry = None
        self._entries_this_session += 1
        self.state = StrategyV2State.LONG
        return self._decision(
            feature,
            StrategyV2Action.FILL_ENTRY,
            "NEXT_BAR_OPEN_FILL",
            before,
            price=price,
            quantity=self.position.quantity,
            stop_price=stop_price,
            target_price=target_price,
        )

    def _pending_entry_structure_reasons(
        self,
        feature: StrategyV2FeatureSnapshot,
        pending: _PendingEntry,
    ) -> tuple[str, ...]:
        """Validate only facts available at the pending bar's open."""
        bar = feature.bar
        if feature.session_day != self._session_day:
            return ("SESSION_CHANGED",)
        if (
            feature.bar_index != pending.signal_bar_index + 1
            or bar.timestamp != pending.signal_at + timedelta(minutes=1)
        ):
            return ("NEXT_BAR_NOT_CONTIGUOUS",)
        session = get_session(self.config.market)
        if not session.is_rth(bar.timestamp):
            return ("OUTSIDE_RTH",)
        if self._closing_window_reached(
            self.config.market,
            self.config.entry_cutoff_minutes_before_close,
            bar.timestamp,
        ):
            return ("ENTRY_CUTOFF",)
        if self._entries_this_session >= self.config.max_entries_per_session:
            return ("MAX_SESSION_ENTRIES",)
        if (
            self._last_exit_at is not None
            and bar.timestamp
            < self._last_exit_at + timedelta(minutes=self.config.entry_cooldown_minutes)
        ):
            return ("ENTRY_COOLDOWN",)
        return ()

    def _evaluate_exit(self, feature: StrategyV2FeatureSnapshot) -> StrategyV2Decision | None:
        position = self.position
        if position is None:
            return None
        bar = feature.bar
        reason = ""
        price: float | None = None

        # An OHLC bar cannot reveal whether stop or target traded first. Always
        # choose the stop in that ambiguous case, then evaluate the other exits.
        if bar.low <= position.stop_price:
            reason = "PRICE_STOP"
            price = self._adverse_sell_price(min(bar.open, position.stop_price))
        elif is_closing_window(
            self.config.market,
            self.config.flatten_minutes_before_close,
            bar.timestamp,
        ):
            reason = "EOD_FLATTEN"
            price = self._adverse_sell_price(bar.open)
        elif bar.high >= position.target_price:
            reason = "PROFIT_TARGET"
            price = self._adverse_sell_price(max(bar.open, position.target_price))
        elif bar.timestamp >= position.holding_deadline:
            reason = "MAX_HOLD"
            price = self._adverse_sell_price(bar.open)
        if price is None:
            return None

        before = self.state
        quantity = position.quantity
        self.position = None
        self.state = StrategyV2State.READY if feature.ready else StrategyV2State.COLD
        self._last_exit_at = bar.timestamp
        return self._decision(
            feature,
            StrategyV2Action.EXIT_LONG,
            reason,
            before,
            price=price,
            quantity=quantity,
            stop_price=position.stop_price,
            target_price=position.target_price,
        )

    def _evaluate_armed(self, feature: StrategyV2FeatureSnapshot) -> StrategyV2Decision:
        before = self.state
        zscore = feature.zscore_1m
        if self._arm_bar_index is None or self._arm_previous_zscore is None:
            self.state = StrategyV2State.READY if feature.ready else StrategyV2State.COLD
            self._clear_arm()
            return self._decision(feature, StrategyV2Action.CANCEL_ARM, "ARM_STATE_INVALID", before)
        gate_reasons = self.entry_gate_reasons(feature)
        elapsed_bars = feature.bar_index - self._arm_bar_index
        if gate_reasons:
            self.state = StrategyV2State.READY if feature.ready else StrategyV2State.COLD
            self._clear_arm()
            return self._decision(
                feature,
                StrategyV2Action.CANCEL_ARM,
                gate_reasons[0],
                before,
            )
        if elapsed_bars >= self.config.arm_ttl_bars:
            self.state = StrategyV2State.READY
            self._clear_arm()
            return self._decision(feature, StrategyV2Action.CANCEL_ARM, "ARM_TTL_EXPIRED", before)
        if zscore is None:
            self.state = StrategyV2State.COLD
            self._clear_arm()
            return self._decision(feature, StrategyV2Action.CANCEL_ARM, "ZSCORE_1M_UNAVAILABLE", before)

        previous_zscore = self._arm_previous_zscore
        self._arm_previous_zscore = zscore
        if self._arm_trough_zscore is None or zscore < self._arm_trough_zscore:
            self._arm_trough_zscore = zscore
        if zscore > 0:
            self.state = StrategyV2State.READY
            self._clear_arm()
            return self._decision(
                feature,
                StrategyV2Action.CANCEL_ARM,
                "RECLAIM_CHASE_LIMIT",
                before,
            )
        reclaimed = (
            elapsed_bars >= 1
            and previous_zscore < self.config.reclaim_zscore_1m
            and zscore >= self.config.reclaim_zscore_1m
            and zscore > previous_zscore
        )
        if not reclaimed:
            return self._wait(feature, ("WAITING_FOR_RECLAIM",))

        signal_vwap = feature.session_vwap_1m
        if signal_vwap is None:
            self.state = StrategyV2State.COLD
            self._clear_arm()
            return self._decision(feature, StrategyV2Action.CANCEL_ARM, "VWAP_1M_UNAVAILABLE", before)
        self._pending_entry = _PendingEntry(
            signal_at=feature.bar.timestamp,
            signal_bar_index=feature.bar_index,
            signal_vwap=signal_vwap,
        )
        self.state = StrategyV2State.ENTRY_PENDING
        self._clear_arm()
        return self._decision(feature, StrategyV2Action.SUBMIT_ENTRY, "ZSCORE_1M_RECLAIM", before)

    def _adverse_buy_price(self, price: float) -> float:
        return price * (1.0 + self.config.slippage_bps / 10_000.0)

    def _adverse_sell_price(self, price: float) -> float:
        return price * (1.0 - self.config.slippage_bps / 10_000.0)

    @staticmethod
    def _closing_window_reached(market: str, minutes: int, instant: datetime) -> bool:
        session = get_session(market)
        local = session.local(instant)
        close_at = datetime.combine(
            local.date(),
            session.close_time(local.date()),
            tzinfo=session.timezone,
        )
        return local >= close_at - timedelta(minutes=minutes)

    def _clear_arm(self) -> None:
        self._arm_bar_index = None
        self._arm_previous_zscore = None
        self._arm_trough_zscore = None

    def _wait(
        self,
        feature: StrategyV2FeatureSnapshot,
        reasons: tuple[str, ...],
    ) -> StrategyV2Decision:
        return StrategyV2Decision(
            timestamp=feature.bar.timestamp,
            action=StrategyV2Action.WAIT,
            reason=reasons[0] if reasons else "NO_ACTION",
            state_before=self.state,
            state_after=self.state,
        )

    def _decision(
        self,
        feature: StrategyV2FeatureSnapshot,
        action: StrategyV2Action,
        reason: str,
        state_before: StrategyV2State,
        *,
        price: float | None = None,
        quantity: float = 0.0,
        stop_price: float | None = None,
        target_price: float | None = None,
    ) -> StrategyV2Decision:
        return StrategyV2Decision(
            timestamp=feature.bar.timestamp,
            action=action,
            reason=reason,
            state_before=state_before,
            state_after=self.state,
            price=price,
            quantity=quantity,
            stop_price=stop_price,
            target_price=target_price,
        )
