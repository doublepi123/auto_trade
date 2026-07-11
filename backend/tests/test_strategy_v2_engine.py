from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.domain.strategy_v2.engine import (
    StrategyV2Action,
    StrategyV2Config,
    StrategyV2Engine,
    StrategyV2EngineSnapshot,
    StrategyV2State,
    VirtualPosition,
)
from app.domain.strategy_v2.features import StrategyBar, StrategyV2FeatureSnapshot


_NEW_YORK = ZoneInfo("America/New_York")
_START = datetime(2026, 7, 7, 12, 0, tzinfo=_NEW_YORK).astimezone(timezone.utc)


def _feature(
    index: int,
    zscore_1m: float,
    *,
    timestamp: datetime | None = None,
    open_price: float = 100.0,
    high: float | None = None,
    low: float | None = None,
    zscore_5m: float = -0.6,
    adx: float = 15.0,
    realized_vol: float = 0.40,
    sigma_1m: float = 0.001,
    sigma_5m: float = 0.001,
    ready: bool = True,
    gate_reasons: tuple[str, ...] = (),
    volume: float = 1000.0,
) -> StrategyV2FeatureSnapshot:
    at = timestamp or (_START + timedelta(minutes=index))
    bar = StrategyBar(
        timestamp=at,
        open=open_price,
        high=high if high is not None else open_price + 0.1,
        low=low if low is not None else open_price - 0.1,
        close=open_price,
        volume=volume,
        symbol="NVDA.US",
    )
    return StrategyV2FeatureSnapshot(
        bar=bar,
        session_day=at.astimezone(_NEW_YORK).date(),
        bar_index=index,
        bar_timestamp_5m=at - timedelta(minutes=at.minute % 5),
        session_vwap_1m=100.5,
        residual_1m=-0.001,
        residual_mean_1m=0.0,
        residual_sigma_1m=sigma_1m,
        zscore_1m=zscore_1m,
        session_vwap_5m=100.5,
        residual_5m=-0.001,
        residual_mean_5m=0.0,
        residual_sigma_5m=sigma_5m,
        zscore_5m=zscore_5m,
        adx_5m=adx,
        realized_vol_1m=realized_vol,
        ready=ready,
        gate_reasons=gate_reasons,
    )


def _drive_to_pending(engine: StrategyV2Engine) -> StrategyV2FeatureSnapshot:
    engine.on_feature(_feature(0, -1.5))
    breach = engine.on_feature(_feature(1, -2.1))
    assert breach.decisions[-1].action == StrategyV2Action.ARM_LONG
    engine.on_feature(_feature(2, -2.4))
    engine.on_feature(_feature(3, -1.2))
    reclaim_feature = _feature(4, -0.8)
    reclaim = engine.on_feature(reclaim_feature)
    assert reclaim.decisions[-1].action == StrategyV2Action.SUBMIT_ENTRY
    assert reclaim.state == StrategyV2State.ENTRY_PENDING
    return reclaim_feature


def test_breach_must_precede_later_reclaim_and_fill_next_bar_open() -> None:
    engine = StrategyV2Engine()
    signal = _drive_to_pending(engine)

    duplicate = engine.on_feature(signal)
    assert duplicate.decisions == ()
    assert duplicate.state == StrategyV2State.ENTRY_PENDING

    fill_feature = _feature(5, -0.7, open_price=101.0, high=101.3, low=100.8)
    filled = engine.on_feature(fill_feature)
    assert filled.decisions[0].action == StrategyV2Action.FILL_ENTRY
    assert filled.decisions[0].reason == "NEXT_BAR_OPEN_FILL"
    assert filled.decisions[0].price == pytest.approx(101.0 * 1.0002)
    assert filled.state == StrategyV2State.LONG
    assert filled.position is not None
    assert filled.position.holding_deadline == fill_feature.bar.timestamp + timedelta(minutes=60)
    assert len(filled.position.config_version) == 64


def test_entry_pending_cancels_on_noncontiguous_next_bar() -> None:
    engine = StrategyV2Engine()
    _drive_to_pending(engine)
    gap = engine.on_feature(_feature(6, -0.7, timestamp=_START + timedelta(minutes=6)))
    assert gap.decisions[0].action == StrategyV2Action.CANCEL_ENTRY
    assert gap.decisions[0].reason == "NEXT_BAR_NOT_CONTIGUOUS"
    assert gap.position is None


def test_fill_bar_close_features_do_not_cancel_open_fill() -> None:
    engine = StrategyV2Engine()
    _drive_to_pending(engine)
    failed_close_regime = engine.on_feature(_feature(
        5,
        3.0,
        open_price=101.0,
        high=101.2,
        low=100.9,
        zscore_5m=3.0,
        adx=99.0,
        realized_vol=2.0,
        sigma_1m=0.0,
        sigma_5m=0.0,
        ready=False,
        gate_reasons=("NON_POSITIVE_VOLUME", "INCOMPLETE_SESSION"),
        volume=0,
    ))
    assert failed_close_regime.decisions[0].action == StrategyV2Action.FILL_ENTRY
    assert failed_close_regime.decisions[0].reason == "NEXT_BAR_OPEN_FILL"
    assert all(
        decision.action != StrategyV2Action.CANCEL_ENTRY
        for decision in failed_close_regime.decisions
    )
    assert failed_close_regime.state == StrategyV2State.LONG


def test_reclaim_jump_above_zero_is_cancelled_as_chase() -> None:
    engine = StrategyV2Engine()
    engine.on_feature(_feature(0, -1.5))
    engine.on_feature(_feature(1, -2.1))
    result = engine.on_feature(_feature(2, 0.1))
    assert result.decisions[-1].action == StrategyV2Action.CANCEL_ARM
    assert result.decisions[-1].reason == "RECLAIM_CHASE_LIMIT"
    assert result.state == StrategyV2State.READY


def test_long_state_ignores_new_entry_cycles() -> None:
    engine = StrategyV2Engine()
    _drive_to_pending(engine)
    engine.on_feature(_feature(5, -0.7, open_price=101, high=101.2, low=100.8))
    result = engine.on_feature(_feature(6, -2.5, open_price=101, high=101.2, low=100.8))
    assert result.state == StrategyV2State.LONG
    assert all(decision.action != StrategyV2Action.ARM_LONG for decision in result.decisions)
    assert engine.entries_this_session == 1


def _engine_with_position(
    *,
    entry_at: datetime = _START,
    deadline: datetime | None = None,
) -> StrategyV2Engine:
    engine = StrategyV2Engine()
    engine.on_feature(_feature(0, -1.0, timestamp=entry_at))
    engine.state = StrategyV2State.LONG
    engine.position = VirtualPosition(
        entry_price=100.0,
        entry_at=entry_at,
        quantity=3.0,
        stop_price=99.25,
        target_price=100.50,
        signal_vwap=100.50,
        holding_deadline=deadline or (entry_at + timedelta(minutes=60)),
        config_version=engine.config.version_hash(),
    )
    return engine


def test_stop_wins_when_same_bar_also_touches_target() -> None:
    engine = _engine_with_position()
    result = engine.on_feature(_feature(1, -0.5, open_price=100, high=101, low=99))
    exit_decision = result.decisions[0]
    assert exit_decision.action == StrategyV2Action.EXIT_LONG
    assert exit_decision.reason == "PRICE_STOP"
    assert exit_decision.price == pytest.approx(99.25 * 0.9998)
    assert exit_decision.quantity == 3
    assert result.position is None


def test_eod_precedes_target_and_target_precedes_max_hold() -> None:
    eod_engine = _engine_with_position()
    at_eod = datetime(2026, 7, 7, 15, 45, tzinfo=_NEW_YORK).astimezone(timezone.utc)
    eod = eod_engine.on_feature(_feature(
        1, -0.5, timestamp=at_eod, open_price=100, high=101, low=99.5,
    ))
    assert eod.decisions[0].reason == "EOD_FLATTEN"

    target_engine = _engine_with_position(deadline=_START + timedelta(minutes=1))
    target = target_engine.on_feature(_feature(
        1, -0.5, open_price=100, high=100.6, low=99.5,
    ))
    assert target.decisions[0].reason == "PROFIT_TARGET"


def test_half_day_eod_flatten_uses_actual_one_pm_close() -> None:
    before = datetime(2026, 11, 27, 12, 44, tzinfo=_NEW_YORK).astimezone(timezone.utc)
    engine = _engine_with_position(entry_at=before - timedelta(minutes=10))
    at_flatten = datetime(2026, 11, 27, 12, 45, tzinfo=_NEW_YORK).astimezone(timezone.utc)
    result = engine.on_feature(_feature(
        1,
        -0.5,
        timestamp=at_flatten,
        open_price=100,
        high=100.4,
        low=99.5,
    ))
    assert result.decisions[0].reason == "EOD_FLATTEN"


def test_max_holding_boundary_exits_at_bar_open() -> None:
    engine = _engine_with_position(deadline=_START + timedelta(minutes=1))
    result = engine.on_feature(_feature(
        1, -0.5, open_price=100.2, high=100.3, low=99.5,
    ))
    assert result.decisions[0].reason == "MAX_HOLD"
    assert result.decisions[0].price == pytest.approx(100.2 * 0.9998)


def test_entry_cutoff_uses_signal_observability_at_bar_end() -> None:
    engine = StrategyV2Engine()
    at_1514 = datetime(2026, 7, 7, 15, 14, tzinfo=_NEW_YORK).astimezone(timezone.utc)
    feature = _feature(0, -2.1, timestamp=at_1514)
    result = engine.on_feature(feature)
    assert result.state == StrategyV2State.READY
    assert result.decisions[-1].reason == "ENTRY_CUTOFF"

    last_rth_bar = datetime(2026, 7, 7, 15, 59, tzinfo=_NEW_YORK).astimezone(timezone.utc)
    assert "ENTRY_CUTOFF" in engine.entry_gate_reasons(
        _feature(1, -2.1, timestamp=last_rth_bar)
    )


def test_gate_boundaries_and_residual_sigma_floor_are_inclusive() -> None:
    engine = StrategyV2Engine()
    passing = _feature(
        0, -2.1, zscore_5m=-0.5, adx=20.0, realized_vol=0.80,
        sigma_1m=0.0008, sigma_5m=0.0008,
    )
    assert engine.entry_gate_reasons(passing) == ()
    assert "ADX_REGIME_BLOCKED" in engine.entry_gate_reasons(_feature(1, -2.1, adx=20.01))
    assert "REALIZED_VOL_REGIME_BLOCKED" in engine.entry_gate_reasons(
        _feature(2, -2.1, realized_vol=0.8001)
    )
    assert "RESIDUAL_SIGMA_1M_TOO_LOW" in engine.entry_gate_reasons(
        _feature(3, -2.1, sigma_1m=0.00079)
    )


def test_pending_entry_snapshot_round_trip_survives_restart() -> None:
    engine = StrategyV2Engine()
    _drive_to_pending(engine)
    payload = engine.snapshot().to_dict()
    restored_snapshot = StrategyV2EngineSnapshot.from_dict(payload)

    restored = StrategyV2Engine()
    restored.restore(restored_snapshot)
    result = restored.on_feature(_feature(5, -0.7, open_price=101, high=101.3, low=100.8))
    assert result.decisions[0].action == StrategyV2Action.FILL_ENTRY
    assert result.state == StrategyV2State.LONG
    assert restored.snapshot().to_dict()["position"] is not None


def test_session_rollover_cancels_pending_and_flattens_overnight_position() -> None:
    pending_engine = StrategyV2Engine()
    _drive_to_pending(pending_engine)
    next_open = datetime(2026, 7, 8, 9, 30, tzinfo=_NEW_YORK).astimezone(timezone.utc)
    rollover = pending_engine.on_feature(_feature(
        0,
        -0.7,
        timestamp=next_open,
        open_price=102,
    ))
    assert all(decision.action != StrategyV2Action.FILL_ENTRY for decision in rollover.decisions)
    assert rollover.position is None

    long_engine = _engine_with_position()
    assert long_engine.position is not None
    long_engine.position = VirtualPosition(
        entry_price=long_engine.position.entry_price,
        entry_at=long_engine.position.entry_at,
        quantity=7,
        stop_price=long_engine.position.stop_price,
        target_price=long_engine.position.target_price,
        signal_vwap=long_engine.position.signal_vwap,
        holding_deadline=long_engine.position.holding_deadline,
        config_version=long_engine.position.config_version,
    )
    overnight = long_engine.on_feature(_feature(
        0,
        -0.7,
        timestamp=next_open,
        open_price=102,
    ))
    assert overnight.decisions[0].reason == "OVERNIGHT_SAFETY_FLATTEN"
    assert overnight.decisions[0].quantity == 7
    assert overnight.decisions[0].price == pytest.approx(102 * 0.9998)


def test_long_snapshot_preserves_frozen_deadline_across_config_change() -> None:
    original = _engine_with_position(deadline=_START + timedelta(minutes=60))
    snapshot = original.snapshot().to_dict()
    changed = StrategyV2Engine(StrategyV2Config(max_holding_minutes=10))
    changed.restore(snapshot)
    assert changed.position is not None
    assert changed.position.holding_deadline == _START + timedelta(minutes=60)
    assert changed.position.config_version != changed.config.version_hash()

    still_open = changed.on_feature(_feature(
        10,
        -0.5,
        timestamp=_START + timedelta(minutes=10),
        open_price=100,
        high=100.4,
        low=99.5,
    ))
    assert still_open.state == StrategyV2State.LONG
    assert still_open.position is not None


def test_arm_ttl_expires_at_configured_boundary() -> None:
    engine = StrategyV2Engine(StrategyV2Config(arm_ttl_bars=2))
    engine.on_feature(_feature(0, -1.5))
    engine.on_feature(_feature(1, -2.1))
    engine.on_feature(_feature(2, -1.5))
    expired = engine.on_feature(_feature(3, -1.4))
    assert expired.decisions[-1].action == StrategyV2Action.CANCEL_ARM
    assert expired.decisions[-1].reason == "ARM_TTL_EXPIRED"
