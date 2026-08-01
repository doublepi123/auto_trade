from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.market_calendar import get_session
from app.domain.strategy_v2.frozen_disproof_queue import (
    FORWARD_CANDIDATE_ALGORITHM_VERSION,
    FROZEN_EVALUATOR_DIGEST,
    FROZEN_QUEUE_ENTRIES,
)
from app.domain.strategy_v2.forward_replay_artifact import (
    encode_forward_replay_artifact,
    forward_replay_artifact_binding_sha256,
)
from app.domain.strategy_v2.trusted_frozen_assessment import (
    TRUSTED_ASSESSMENT_EXPECTED_SESSIONS,
    TRUSTED_ASSESSMENT_WINDOW_DIGEST,
    TrustedAssessmentError,
    TrustedDailyLeaf,
    TrustedSymbolEvidence,
    TrustedTradeSummary,
    build_trusted_assessment_report,
    trusted_assessment_sessions,
    trusted_producer_cutoff,
    validate_replay_trade_track,
)
from app.models import (
    Base,
    StrategyV2ForwardEvidence,
    StrategyV2ForwardEvidenceArtifact,
    StrategyV2ForwardRegistration,
    StrategyV2ForwardReplayArtifact,
    StrategyV2ShadowVersion,
)
from app.services.strategy_v2_shadow_service import StrategyV2ShadowService
from app.services.trusted_frozen_assessment_service import (
    TrustedFrozenAssessmentService,
)
from app.schemas import StrategyV2ReplayBar, TrustedFrozenAssessmentReport


def _refresh_report_digest(report: dict[str, Any]) -> None:
    payload = dict(report)
    payload.pop("report_digest_sha256", None)
    report["report_digest_sha256"] = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _replay_fixture(
    *,
    trade_count: int = 1,
    session_date: date = date(2026, 8, 3),
) -> tuple[dict[str, object], dict[str, object]]:
    decisions: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    for index in range(trade_count):
        entry_at = datetime(
            session_date.year,
            session_date.month,
            session_date.day,
            14,
            index * 20,
            tzinfo=timezone.utc,
        )
        exit_at = entry_at + timedelta(minutes=10)
        entry_price = 100.0 + index
        exit_price = entry_price + 1.0
        quantity = 1.0
        fee_rate = 0.0005
        entry_fee = entry_price * quantity * fee_rate
        exit_fee = exit_price * quantity * fee_rate
        fees = entry_fee + exit_fee
        gross = (exit_price - entry_price) * quantity
        net = gross - fees
        decisions.extend([
            {
                "timestamp": entry_at.isoformat(),
                "action": "FILL_ENTRY",
                "reason": "ENTRY_FILLED",
                "price": entry_price,
                "quantity": quantity,
                "gate_passed": True,
            },
            {
                "timestamp": exit_at.isoformat(),
                "action": "EXIT_LONG",
                "reason": "PROFIT_TARGET",
                "price": exit_price,
                "quantity": quantity,
                "gate_passed": True,
            },
        ])
        trades.append({
            "entry_at": entry_at.isoformat(),
            "exit_at": exit_at.isoformat(),
            "entry_reason": "ENTRY_FILLED",
            "exit_reason": "PROFIT_TARGET",
            "entry_price": entry_price,
            "exit_price": exit_price,
            "quantity": quantity,
            "estimated_fee_rate": fee_rate,
            "fee_source": "ESTIMATED",
            "gross_pnl": gross,
            "fees": fees,
            "net_pnl": net,
            "holding_minutes": 10.0,
            "mae_pct": -0.01,
            "mfe_pct": 0.02,
        })
    net_values = [float(item["net_pnl"]) for item in trades]
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in net_values:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    metrics: dict[str, object] = {
        "bars": len({str(item["timestamp"]) for item in decisions}),
        "eligible_bars": len({
            str(item["timestamp"])
            for item in decisions
            if bool(item["gate_passed"])
        }),
        "breaches": 0,
        "reclaims": 0,
        "entries": trade_count,
        "exits": trade_count,
        "closed_trades": trade_count,
        "win_rate": 1.0 if trade_count else 0.0,
        "gross_pnl": sum(float(item["gross_pnl"]) for item in trades),
        "fees": sum(float(item["fees"]) for item in trades),
        "net_pnl": sum(net_values),
        "max_drawdown": max_drawdown,
        "avg_holding_minutes": 10.0 if trade_count else 0.0,
        "avg_mae_pct": -0.01 if trade_count else 0.0,
        "avg_mfe_pct": 0.02 if trade_count else 0.0,
        "comparison_available": False,
        "live_action_count": None,
        "action_agreement_rate": None,
        "net_pnl_delta_vs_live": None,
    }
    return (
        {"decisions": decisions, "trades": trades, "metrics": metrics},
        {"metrics": dict(metrics), "trade_net_pnl": net_values},
    )


def _validate_fixture(
    replay: dict[str, object],
    result: dict[str, object],
    *,
    max_entries: int = 2,
) -> TrustedTradeSummary:
    return validate_replay_trade_track(
        replay,
        result,
        label="fixture",
        session_date=date(2026, 8, 3),
        expected_fee_rate=0.0005,
        expected_max_entries_per_day=max_entries,
        expected_virtual_quantity=1.0,
        expected_max_holding_minutes=60,
    )


def test_fixed_window_has_252_sessions_and_pinned_digest() -> None:
    sessions = trusted_assessment_sessions()

    assert len(sessions) == TRUSTED_ASSESSMENT_EXPECTED_SESSIONS == 252
    assert sessions[0] == date(2026, 8, 3)
    assert sessions[-1] == date(2027, 8, 3)
    assert TRUSTED_ASSESSMENT_WINDOW_DIGEST == (
        "3378303933970bc8dfc2bfb310ef3d98623d7e6a906e004353cc243a159621d0"
    )


def test_cutoff_uses_close_plus_15_and_caps_at_horizon() -> None:
    before = trusted_producer_cutoff(
        datetime(2026, 8, 3, 20, 14, 59, tzinfo=timezone.utc)
    )
    at_cutoff = trusted_producer_cutoff(
        datetime(2026, 8, 3, 20, 15, tzinfo=timezone.utc)
    )
    half_day_before = trusted_producer_cutoff(
        datetime(2026, 11, 27, 18, 14, 59, tzinfo=timezone.utc)
    )
    half_day_at = trusted_producer_cutoff(
        datetime(2026, 11, 27, 18, 15, tzinfo=timezone.utc)
    )
    after_horizon = trusted_producer_cutoff(
        datetime(2027, 8, 31, tzinfo=timezone.utc)
    )

    assert before.complete_through is None
    assert at_cutoff.complete_through == date(2026, 8, 3)
    assert half_day_before.complete_through != date(2026, 11, 27)
    assert half_day_at.complete_through == date(2026, 11, 27)
    assert after_horizon.complete_through == date(2027, 8, 3)


def test_replay_validator_recomputes_all_metrics_and_trade_preimage() -> None:
    replay, result = _replay_fixture()

    summary = _validate_fixture(replay, result)

    assert summary.closed_trades == 1
    assert summary.entry_notional == Decimal("100.0")
    assert len(summary.ordered_trade_preimage_sha256) == 64

    tampered_replay = json.loads(json.dumps(replay))
    tampered_result = json.loads(json.dumps(result))
    tampered_replay["metrics"]["eligible_bars"] = 1
    tampered_result["metrics"]["eligible_bars"] = 1
    with pytest.raises(TrustedAssessmentError):
        _validate_fixture(tampered_replay, tampered_result)

    tampered_mae = json.loads(json.dumps(replay))
    tampered_mae["trades"][0]["mae_pct"] = 0.01
    with pytest.raises(TrustedAssessmentError):
        _validate_fixture(tampered_mae, result)


def test_replay_validator_rejects_unknown_action_and_third_daily_entry() -> None:
    replay, result = _replay_fixture()
    unknown = json.loads(json.dumps(replay))
    unknown["decisions"].append({
        "timestamp": "2026-08-03T15:00:00+00:00",
        "action": "SELL_SHORT",
        "reason": "INVALID",
        "price": 100.0,
        "quantity": 1.0,
        "gate_passed": True,
    })
    with pytest.raises(TrustedAssessmentError):
        _validate_fixture(unknown, result)

    three_replay, three_result = _replay_fixture(trade_count=3)
    with pytest.raises(TrustedAssessmentError):
        _validate_fixture(three_replay, three_result, max_entries=2)


def _pending_symbols() -> tuple[TrustedSymbolEvidence, ...]:
    sessions = trusted_assessment_sessions()
    return tuple(
        TrustedSymbolEvidence(
            symbol=symbol,
            role=role,
            reason=reason,
            config_hash=config_hash,
            registration_id=None,
            registration_blockers=("CANONICAL_REGISTRATION_MISSING",),
            pre_window_rows_excluded=0,
            post_window_rows_excluded=0,
            leaves=tuple(
                TrustedDailyLeaf(
                    symbol=symbol,
                    role=role,
                    config_hash=config_hash,
                    session_date=session_date,
                    disposition="PENDING",
                )
                for session_date in sessions
            ),
        )
        for symbol, role, reason, config_hash in FROZEN_QUEUE_ENTRIES
    )


def test_report_builder_rejects_bool_ids_negative_counts_and_fake_summary() -> None:
    symbols = list(_pending_symbols())
    cutoff = trusted_producer_cutoff(
        datetime(2026, 8, 1, tzinfo=timezone.utc)
    )
    symbols[0] = replace(symbols[0], pre_window_rows_excluded=-1)
    with pytest.raises(TrustedAssessmentError):
        build_trusted_assessment_report(symbols, producer_cutoff=cutoff)

    symbols = list(_pending_symbols())
    symbols[0] = replace(
        symbols[0],
        registration_id=True,
        candidate_algorithm_version=FORWARD_CANDIDATE_ALGORITHM_VERSION,
        evaluator_digest=FROZEN_EVALUATOR_DIGEST,
        registered_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        eligible_after=datetime(2026, 7, 31, 13, 30, tzinfo=timezone.utc),
    )
    with pytest.raises(TrustedAssessmentError):
        build_trusted_assessment_report(symbols, producer_cutoff=cutoff)

    bad_summary = TrustedTradeSummary(
        closed_trades=1,
        gross_pnl=1.0,
        fees=0.1,
        net_pnl=0.9,
        entry_notional=Decimal("100"),
        ordered_trade_preimage_sha256="NOT_A_DIGEST",
    )
    symbols = list(_pending_symbols())
    first = symbols[0]
    leaves = list(first.leaves)
    leaves[0] = TrustedDailyLeaf(
        symbol=first.symbol,
        role=first.role,
        config_hash=first.config_hash,
        session_date=leaves[0].session_date,
        disposition="INCLUDED",
        evidence_id=1,
        evidence_digest_sha256="0" * 64,
        baseline_result_sha256="0" * 64,
        candidate_result_sha256="0" * 64,
        artifact_digest_sha256="0" * 64,
        artifact_binding_sha256="0" * 64,
        daily_binding_sha256="0" * 64,
        baseline=bad_summary,
        candidate=bad_summary,
    )
    symbols[0] = replace(
        first,
        registration_id=1,
        registration_blockers=(),
        leaves=tuple(leaves),
        candidate_algorithm_version=FORWARD_CANDIDATE_ALGORITHM_VERSION,
        evaluator_digest=FROZEN_EVALUATOR_DIGEST,
        registered_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        eligible_after=datetime(2026, 7, 31, 13, 30, tzinfo=timezone.utc),
    )
    complete = trusted_producer_cutoff(
        datetime(2027, 8, 4, tzinfo=timezone.utc)
    )
    with pytest.raises(TrustedAssessmentError):
        build_trusted_assessment_report(symbols, producer_cutoff=complete)


def _source_config(symbol: str) -> dict[str, object]:
    values: dict[str, object] = {
        "adx_period": 14,
        "algorithm_version": "strategy-v2-rth-mr-v5-causal-entry",
        "allow_position_addons": False,
        "arm_ttl_bars": 10,
        "breach_zscore": -2.0,
        "entry_cooldown_minutes": 15,
        "entry_cutoff_minutes_before_close": 45,
        "estimated_fee_rate_hk": 0.003,
        "estimated_fee_rate_us": 0.0005,
        "five_minute_zscore_max": -0.5,
        "flatten_minutes_before_close": 15,
        "max_adx": 20.0,
        "max_entries_per_day": 2,
        "max_holding_minutes": 60,
        "max_realized_vol": 0.8,
        "min_realized_vol": 0.1,
        "mode": "SHADOW",
        "order_submission_allowed": False,
        "profit_target_pct": 0.8,
        "realized_vol_window_bars": 30,
        "reclaim_zscore": -1.0,
        "short_entries_enabled": False,
        "slippage_bps": 2.0,
        "stop_loss_pct": 0.45,
        "symbol": symbol,
        "zscore_window_1m_bars": 30,
        "zscore_window_5m_bars": 12,
    }
    if symbol == "NVDA.US":
        values["breach_zscore"] = -2.25
        values["max_adx"] = 18.0
    return values


def _source_trace(
    *,
    symbol: str,
    config_hash: str,
    session_date: date,
    bars: list[StrategyV2ReplayBar],
    observed_at: dict[datetime, datetime],
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for bar in bars:
        timestamp = bar.timestamp.astimezone(timezone.utc)
        features = {
            "bar": {
                "timestamp": timestamp.isoformat(),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "duration_minutes": 1,
                "symbol": symbol,
            }
        }
        result.append({
            "symbol": symbol,
            "market": "US",
            "config_version": config_hash,
            "session_date": session_date.isoformat(),
            "bar_at": timestamp.isoformat(),
            "observed_at": observed_at[timestamp].isoformat(),
            "close_price": bar.close,
            "features_json": json.dumps(
                features,
                sort_keys=True,
                separators=(",", ":"),
            ),
        })
    return result


def _add_full_evidence(
    db: Any,
    *,
    registration: StrategyV2ForwardRegistration,
    source_config: dict[str, object],
    target_day: date,
    seed_day: date,
) -> tuple[StrategyV2ForwardEvidence, StrategyV2ForwardReplayArtifact]:
    session = get_session("US")
    target_open = datetime.combine(
        target_day,
        session.rth_open,
        tzinfo=session.timezone,
    ).astimezone(timezone.utc)
    close_at = datetime.combine(
        target_day,
        session.close_time(target_day),
        tzinfo=session.timezone,
    ).astimezone(timezone.utc)
    evaluated_at = close_at + timedelta(minutes=10)
    seed_bars = [
        StrategyV2ReplayBar(
            timestamp=datetime(
                seed_day.year,
                seed_day.month,
                seed_day.day,
                14,
                0,
                tzinfo=timezone.utc,
            ),
            open=99.0,
            high=100.0,
            low=98.5,
            close=99.5,
            volume=1_000.0,
        )
    ]
    target_bars = [
        StrategyV2ReplayBar(
            timestamp=datetime(
                target_day.year,
                target_day.month,
                target_day.day,
                14,
                minute,
                tzinfo=timezone.utc,
            ),
            open=price,
            high=price + 1.0,
            low=price - 0.5,
            close=price,
            volume=1_000.0,
        )
        for minute, price in ((0, 100.0), (10, 101.0))
    ]
    schedule = {
        bar.timestamp.astimezone(timezone.utc): (
            bar.timestamp.astimezone(timezone.utc)
            + timedelta(minutes=1, seconds=5)
        )
        for bar in target_bars
    }
    seed_schedule = {
        bar.timestamp.astimezone(timezone.utc): (
            bar.timestamp.astimezone(timezone.utc)
            + timedelta(minutes=1, seconds=5)
        )
        for bar in seed_bars
    }
    replay, result = _replay_fixture(session_date=target_day)
    result["daily"] = {}
    result_json = json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    target_hash = StrategyV2ShadowService._forward_bars_hash(target_bars)
    seed_hash = StrategyV2ShadowService._forward_bars_hash(seed_bars)
    input_hash = StrategyV2ShadowService._forward_replay_input_hash(
        target_bars,
        schedule,
    )
    evidence = StrategyV2ForwardEvidence(
        registration_id=registration.id,
        target_session_date=target_day,
        seed_session_date=seed_day,
        target_open_at=target_open,
        evaluated_at=evaluated_at,
        disposition="INCLUDED",
        exclusion_reason="",
        structural_failure=False,
        target_bars=len(target_bars),
        target_bars_sha256=target_hash,
        seed_bars_sha256=seed_hash,
        baseline_input_sha256=input_hash,
        candidate_input_sha256=input_hash,
        same_target_bars=True,
        baseline_replay_match=True,
        session_local_invariant=True,
        baseline_result_json=result_json,
        candidate_result_json=result_json,
        baseline_result_sha256=StrategyV2ShadowService._forward_text_hash(
            result_json
        ),
        candidate_result_sha256=StrategyV2ShadowService._forward_text_hash(
            result_json
        ),
        created_at=evaluated_at,
    )
    evidence.evidence_digest_sha256 = (
        StrategyV2ShadowService._forward_evidence_digest(evidence)
    )
    db.add(evidence)
    db.flush()
    replay_payload = {
        "persisted": False,
        "config_version": registration.source_config_version,
        **replay,
    }
    candidate_spec = json.loads(registration.candidate_spec_json)
    artifact_payload: dict[str, object] = {
        "bundle_contract": "strategy-v2-forward-replay-bundle-v1",
        "capture_mode": "FULL_REPLAY_VERIFIED",
        "registration": {
            "id": registration.id,
            "symbol": registration.symbol,
            "market": registration.market,
            "candidate_algorithm_version": (
                registration.candidate_algorithm_version
            ),
            "source_config_version": registration.source_config_version,
            "evaluator_digest": registration.evaluator_digest,
            "candidate_spec": candidate_spec,
            "registered_at": registration.registered_at.astimezone(
                timezone.utc
            ).isoformat(),
            "eligible_after": registration.eligible_after.astimezone(
                timezone.utc
            ).isoformat(),
        },
        "evidence": {
            "target_session_date": target_day.isoformat(),
            "seed_session_date": seed_day.isoformat(),
            "target_open_at": target_open.isoformat(),
            "evaluated_at": evaluated_at.isoformat(),
            "target_bars": len(target_bars),
            "target_bars_sha256": target_hash,
            "seed_bars_sha256": seed_hash,
            "baseline_input_sha256": input_hash,
            "candidate_input_sha256": input_hash,
            "baseline_result_json": result_json,
            "candidate_result_json": result_json,
            "baseline_result_sha256": evidence.baseline_result_sha256,
            "candidate_result_sha256": evidence.candidate_result_sha256,
            "evidence_digest_sha256": evidence.evidence_digest_sha256,
        },
        "source_config": source_config,
        "seed_bars": [item.model_dump(mode="json") for item in seed_bars],
        "target_bars": [item.model_dump(mode="json") for item in target_bars],
        "observation_schedule": [
            {
                "bar_at": bar.timestamp.astimezone(timezone.utc).isoformat(),
                "observed_at": schedule[
                    bar.timestamp.astimezone(timezone.utc)
                ].isoformat(),
            }
            for bar in target_bars
        ],
        "seed_source_trace": _source_trace(
            symbol=registration.symbol,
            config_hash=registration.source_config_version,
            session_date=seed_day,
            bars=seed_bars,
            observed_at=seed_schedule,
        ),
        "target_source_trace": _source_trace(
            symbol=registration.symbol,
            config_hash=registration.source_config_version,
            session_date=target_day,
            bars=target_bars,
            observed_at=schedule,
        ),
        "target_source_trades": [],
        "baseline_replay": replay_payload,
        "candidate_replay": replay_payload,
    }
    encoded = encode_forward_replay_artifact(artifact_payload)
    artifact = StrategyV2ForwardReplayArtifact(
        digest_sha256=encoded.digest_sha256,
        schema_version=encoded.schema_version,
        kind=encoded.kind,
        codec=encoded.codec,
        raw_size=encoded.raw_size,
        compressed_size=encoded.compressed_size,
        payload=encoded.payload,
        created_at=evaluated_at,
    )
    link = StrategyV2ForwardEvidenceArtifact(
        evidence_id=evidence.id,
        role="REPLAY_BUNDLE",
        artifact_sha256=encoded.digest_sha256,
        binding_sha256=forward_replay_artifact_binding_sha256(
            evidence_id=evidence.id,
            evidence_digest_sha256=evidence.evidence_digest_sha256,
            artifact_digest_sha256=encoded.digest_sha256,
        ),
        created_at=evaluated_at,
    )
    db.add_all([artifact, link])
    return evidence, artifact


def _add_frozen_registrations(
    db: Any,
) -> tuple[
    dict[str, StrategyV2ForwardRegistration],
    dict[str, dict[str, object]],
]:
    registrations: dict[str, StrategyV2ForwardRegistration] = {}
    sources: dict[str, dict[str, object]] = {}
    for index, (symbol, _role, _reason, config_hash) in enumerate(
        FROZEN_QUEUE_ENTRIES
    ):
        source = _source_config(symbol)
        encoded_source = json.dumps(
            source,
            sort_keys=True,
            separators=(",", ":"),
        )
        assert StrategyV2ShadowService._forward_text_hash(
            encoded_source
        ) == config_hash
        registered_at = datetime(
            2026,
            7,
            31,
            1,
            40 + index,
            tzinfo=timezone.utc,
        )
        if symbol == "TER.US":
            registered_at = datetime(
                2026,
                7,
                31,
                23,
                25,
                tzinfo=timezone.utc,
            )
        snapshot = StrategyV2ShadowVersion(
            symbol=symbol,
            config_version=config_hash,
            config_json=encoded_source,
            activated_at=registered_at,
        )
        spec = StrategyV2ShadowService._forward_candidate_spec(
            config_hash,
            source,
        )
        registration = StrategyV2ForwardRegistration(
            symbol=symbol,
            market="US",
            candidate_algorithm_version=FORWARD_CANDIDATE_ALGORITHM_VERSION,
            source_config_version=config_hash,
            evaluator_digest=FROZEN_EVALUATOR_DIGEST,
            candidate_spec_json=json.dumps(
                spec,
                sort_keys=True,
                separators=(",", ":"),
            ),
            registered_at=registered_at,
            eligible_after=StrategyV2ShadowService._forward_eligible_after(
                "US",
                registered_at,
            ),
        )
        db.add_all([snapshot, registration])
        registrations[symbol] = registration
        sources[symbol] = source
    db.flush()
    return registrations, sources


def test_service_exact_registration_is_read_only_and_skips_pre_window_blob() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    selected: list[str] = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _params, _context, _many: (
            selected.append(statement)
        ),
    )
    try:
        with factory() as db:
            registration_by_symbol, _sources = _add_frozen_registrations(db)

            pre_window = StrategyV2ForwardEvidence(
                registration_id=registration_by_symbol["AAPL.US"].id,
                target_session_date=date(2026, 7, 31),
                seed_session_date=date(2026, 7, 30),
                target_open_at=datetime(
                    2026, 7, 31, 13, 30, tzinfo=timezone.utc
                ),
                evaluated_at=datetime(
                    2026, 7, 31, 20, 10, tzinfo=timezone.utc
                ),
                disposition="INCLUDED",
                exclusion_reason="",
                structural_failure=False,
                evidence_digest_sha256="0" * 64,
            )
            db.add(pre_window)
            db.flush()
            artifact = StrategyV2ForwardReplayArtifact(
                digest_sha256="1" * 64,
                schema_version=1,
                kind="STRATEGY_V2_FORWARD_REPLAY",
                codec="zlib",
                raw_size=1,
                compressed_size=1,
                payload=b"x",
            )
            link = StrategyV2ForwardEvidenceArtifact(
                evidence_id=pre_window.id,
                role="REPLAY_BUNDLE",
                artifact_sha256=artifact.digest_sha256,
                binding_sha256="2" * 64,
            )
            db.add_all([artifact, link])
            db.commit()
            counts_before = (
                db.query(StrategyV2ForwardRegistration).count(),
                db.query(StrategyV2ForwardEvidence).count(),
                db.query(StrategyV2ForwardEvidenceArtifact).count(),
                db.query(StrategyV2ForwardReplayArtifact).count(),
            )
            selected.clear()

            report = TrustedFrozenAssessmentService(
                db,
                clock=lambda: datetime(2026, 8, 1, tzinfo=timezone.utc),
            ).get_report()
            TrustedFrozenAssessmentReport.model_validate(report)

            report_body: Any = report
            assert len(report_body["symbols"]) == 6
            assert sum(
                len(item["leaves"])
                for item in report_body["symbols"]
            ) == 1_512
            aapl = next(
                item
                for item in report_body["symbols"]
                if item["symbol"] == "AAPL.US"
            )
            assert aapl["pre_window_rows_excluded"] == 1
            assert aapl["registration_blockers"] == []
            assert report_body["order_submission_allowed"] is False
            assert report_body["automatic_promotion_allowed"] is False
            assert report_body["promotion_eligible"] is False
            service_statements = "\n".join(selected).lower()
            assert "strategy_v2_forward_replay_artifacts" not in service_statements
            assert "strategy_v2_forward_evidence_artifacts" not in service_statements
            assert counts_before == (
                db.query(StrategyV2ForwardRegistration).count(),
                db.query(StrategyV2ForwardEvidence).count(),
                db.query(StrategyV2ForwardEvidenceArtifact).count(),
                db.query(StrategyV2ForwardReplayArtifact).count(),
            )
            assert not db.new
            assert not db.dirty
            assert not db.deleted

            malformed: Any = json.loads(json.dumps(report))
            malformed["schema_version"] = "EVIL"
            malformed["algorithm_version"] = "EVIL"
            malformed["policy_version"] = "EVIL"
            malformed["report_digest_sha256"] = "x"
            with pytest.raises(ValueError):
                TrustedFrozenAssessmentReport.model_validate(malformed)

            malformed = json.loads(json.dumps(report))
            malformed["symbols"] = malformed["symbols"][:1]
            malformed["candidates"] = []
            malformed["assessment_window"]["expected_session_dates"] = [
                "bogus"
            ]
            with pytest.raises(ValueError):
                TrustedFrozenAssessmentReport.model_validate(malformed)

            malformed = json.loads(json.dumps(report))
            malformed["status"] = "READY_FOR_MANUAL_DISPROOF_REVIEW"
            malformed["evidence_review_ready"] = False
            malformed["promotion_blockers"] = []
            with pytest.raises(ValueError):
                TrustedFrozenAssessmentReport.model_validate(malformed)

            malformed = json.loads(json.dumps(report))
            malformed["symbols"][0]["registration_id"] = True
            malformed["symbols"][0]["pre_window_rows_excluded"] = True
            malformed["symbols"][0]["config_hash"] = "x"
            malformed["symbols"][0]["evidence_root_sha256"] = "x"
            with pytest.raises(ValueError):
                TrustedFrozenAssessmentReport.model_validate(malformed)

            malformed = json.loads(json.dumps(report))
            malformed["candidates"][0]["role"] = "EVIL"
            _refresh_report_digest(malformed)
            with pytest.raises(
                ValueError,
                match="candidate frozen identity",
            ):
                TrustedFrozenAssessmentReport.model_validate(malformed)

            malformed = json.loads(json.dumps(report))
            malformed["candidates"][:2] = reversed(
                malformed["candidates"][:2]
            )
            _refresh_report_digest(malformed)
            with pytest.raises(ValueError, match="candidate cohort"):
                TrustedFrozenAssessmentReport.model_validate(malformed)

            malformed = json.loads(json.dumps(report))
            malformed["symbols"][0]["leaves"][0][
                "leaf_digest_sha256"
            ] = "f" * 64
            _refresh_report_digest(malformed)
            with pytest.raises(ValueError, match="domain attestation"):
                TrustedFrozenAssessmentReport.model_validate(malformed)

            malformed = json.loads(json.dumps(report))
            malformed["symbols"][0]["evidence_root_sha256"] = "f" * 64
            _refresh_report_digest(malformed)
            with pytest.raises(ValueError, match="canonical domain rebuild"):
                TrustedFrozenAssessmentReport.model_validate(malformed)

            malformed = json.loads(json.dumps(report))
            malformed["candidates"][0]["candidate_included_sessions"] = 1
            _refresh_report_digest(malformed)
            with pytest.raises(ValueError, match="canonical domain rebuild"):
                TrustedFrozenAssessmentReport.model_validate(malformed)

            malformed = json.loads(json.dumps(report))
            malformed["candidates"][0]["within_symbol_candidate"][
                "gross_pnl_decimal"
            ] = "1"
            _refresh_report_digest(malformed)
            with pytest.raises(ValueError, match="canonical domain rebuild"):
                TrustedFrozenAssessmentReport.model_validate(malformed)
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_artifact_validation_is_cache_and_restart_deterministic_and_detects_tamper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.strategy_v2_shadow_service as shadow_module

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    statements: list[tuple[str, object]] = []

    def capture_sql(
        _conn: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _many: bool,
    ) -> None:
        statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture_sql)
    original_decode = shadow_module.decode_forward_replay_artifact
    decode_calls = 0

    def counted_decode(**kwargs: Any) -> dict[str, Any]:
        nonlocal decode_calls
        decode_calls += 1
        return original_decode(**kwargs)

    monkeypatch.setattr(
        shadow_module,
        "decode_forward_replay_artifact",
        counted_decode,
    )
    try:
        with TrustedFrozenAssessmentService._validation_cache_lock:
            TrustedFrozenAssessmentService._validation_cache.clear()
        clock = lambda: datetime(
            2026,
            8,
            11,
            20,
            16,
            tzinfo=timezone.utc,
        )
        with factory() as db:
            registrations, sources = _add_frozen_registrations(db)
            days = list(trusted_assessment_sessions()[:7])
            artifacts: list[StrategyV2ForwardReplayArtifact] = []
            seed_day = date(2026, 7, 31)
            for target_day in days:
                _evidence, artifact = _add_full_evidence(
                    db,
                    registration=registrations["AAPL.US"],
                    source_config=sources["AAPL.US"],
                    target_day=target_day,
                    seed_day=seed_day,
                )
                artifacts.append(artifact)
                seed_day = target_day
            db.commit()
            artifact_digests = [item.digest_sha256 for item in artifacts]
            db.expunge_all()

            statements.clear()
            cold: Any = TrustedFrozenAssessmentService(
                db,
                clock=clock,
            ).get_report()
            cold_aapl = next(
                item for item in cold["symbols"] if item["symbol"] == "AAPL.US"
            )
            assert [
                item["disposition"] for item in cold_aapl["leaves"][:7]
            ] == ["INCLUDED"] * 7
            assert decode_calls == 7
            snapshot_selects = [
                statement
                for statement, _params in statements
                if "strategy_v2_shadow_versions" in statement.lower()
            ]
            assert len(snapshot_selects) == 1
            payload_params = [
                params
                for statement, params in statements
                if "strategy_v2_forward_replay_artifacts.payload"
                in statement.lower().replace('"', "")
            ]
            assert len(payload_params) == 7, [
                statement
                for statement, _params in statements
                if "strategy_v2_forward_replay_artifacts" in statement.lower()
            ]

            statements.clear()
            warm: Any = TrustedFrozenAssessmentService(
                db,
                clock=clock,
            ).get_report()
            assert warm == cold
            assert warm["report_digest_sha256"] == cold["report_digest_sha256"]
            assert decode_calls == 7
            assert len([
                params
                for statement, params in statements
                if "strategy_v2_forward_replay_artifacts.payload"
                in statement.lower().replace('"', "")
            ]) == 7
            assert len([
                statement
                for statement, _params in statements
                if "strategy_v2_shadow_versions" in statement.lower()
            ]) == 1

        with TrustedFrozenAssessmentService._validation_cache_lock:
            TrustedFrozenAssessmentService._validation_cache.clear()
        with factory() as db:
            restarted: Any = TrustedFrozenAssessmentService(
                db,
                clock=clock,
            ).get_report()
            assert restarted == cold
            assert (
                restarted["report_digest_sha256"]
                == cold["report_digest_sha256"]
            )
            assert decode_calls == 14

            first_artifact = db.get(
                StrategyV2ForwardReplayArtifact,
                artifact_digests[0],
            )
            assert first_artifact is not None
            tampered = bytearray(first_artifact.payload)
            tampered[len(tampered) // 2] ^= 1
            first_artifact.payload = bytes(tampered)
            db.commit()
            db.expunge_all()

            tampered_report: Any = TrustedFrozenAssessmentService(
                db,
                clock=clock,
            ).get_report()
            tampered_aapl = next(
                item
                for item in tampered_report["symbols"]
                if item["symbol"] == "AAPL.US"
            )
            assert tampered_aapl["leaves"][0]["disposition"] == "INVALID"
            assert tampered_aapl["leaves"][0]["blockers"] == [
                "REPLAY_ARTIFACT_CHAIN_INVALID"
            ]
            assert decode_calls == 15
    finally:
        with TrustedFrozenAssessmentService._validation_cache_lock:
            TrustedFrozenAssessmentService._validation_cache.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_artifact_resource_cap_marks_all_closed_included_rows_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.strategy_v2_shadow_service as shadow_module

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def unexpected_decode(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("resource-capped artifacts must not be decoded")

    monkeypatch.setattr(
        shadow_module,
        "decode_forward_replay_artifact",
        unexpected_decode,
    )
    try:
        with TrustedFrozenAssessmentService._validation_cache_lock:
            TrustedFrozenAssessmentService._validation_cache.clear()
        with factory() as db:
            registrations, sources = _add_frozen_registrations(db)
            artifacts: list[StrategyV2ForwardReplayArtifact] = []
            seed_day = date(2026, 7, 31)
            for target_day in trusted_assessment_sessions()[:2]:
                _evidence, artifact = _add_full_evidence(
                    db,
                    registration=registrations["AAPL.US"],
                    source_config=sources["AAPL.US"],
                    target_day=target_day,
                    seed_day=seed_day,
                )
                artifacts.append(artifact)
                seed_day = target_day
            aapl_registration_id = registrations["AAPL.US"].id
            assert aapl_registration_id is not None
            db.commit()
            compressed_cap = sum(
                artifact.compressed_size for artifact in artifacts
            ) - 1
            monkeypatch.setattr(
                TrustedFrozenAssessmentService,
                "_MAX_TOTAL_COMPRESSED_BYTES",
                compressed_cap,
            )
            db.expunge_all()
            clock = lambda: datetime(
                2026,
                8,
                4,
                20,
                16,
                tzinfo=timezone.utc,
            )

            first: Any = TrustedFrozenAssessmentService(
                db,
                clock=clock,
            ).get_report()
            aapl = next(
                item for item in first["symbols"] if item["symbol"] == "AAPL.US"
            )
            assert [
                leaf["disposition"] for leaf in aapl["leaves"][:2]
            ] == ["PENDING", "PENDING"]
            assert [
                leaf["blockers"] for leaf in aapl["leaves"][:2]
            ] == [
                ["VERIFIER_RESOURCE_CAP_EXCEEDED"],
                ["VERIFIER_RESOURCE_CAP_EXCEEDED"],
            ]
            TrustedFrozenAssessmentReport.model_validate(first)

            with TrustedFrozenAssessmentService._validation_cache_lock:
                TrustedFrozenAssessmentService._validation_cache.clear()
            db.expunge_all()
            second: Any = TrustedFrozenAssessmentService(
                db,
                clock=clock,
            ).get_report()
            assert second == first

            malformed_row = (
                    db.query(StrategyV2ForwardEvidence)
                    .filter(
                        StrategyV2ForwardEvidence.registration_id
                        == aapl_registration_id
                    )
                .order_by(StrategyV2ForwardEvidence.target_session_date.asc())
                .first()
            )
            assert malformed_row is not None
            malformed_row.target_bars = 0
            malformed_row.evidence_digest_sha256 = (
                StrategyV2ShadowService._forward_evidence_digest(
                    malformed_row
                )
            )
            db.commit()
            db.expunge_all()
            third: Any = TrustedFrozenAssessmentService(
                db,
                clock=clock,
            ).get_report()
            third_aapl = next(
                item for item in third["symbols"] if item["symbol"] == "AAPL.US"
            )
            assert third_aapl["leaves"][0]["disposition"] == "INVALID"
            assert third_aapl["leaves"][0]["blockers"] == [
                "INCLUDED_EVIDENCE_INVARIANT_INVALID"
            ]
            assert third_aapl["leaves"][1]["disposition"] == "PENDING"
            assert third_aapl["leaves"][1]["blockers"] == [
                "VERIFIER_RESOURCE_CAP_EXCEEDED"
            ]
    finally:
        with TrustedFrozenAssessmentService._validation_cache_lock:
            TrustedFrozenAssessmentService._validation_cache.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_malformed_database_digests_fail_closed_without_breaking_report() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    try:
        with TrustedFrozenAssessmentService._validation_cache_lock:
            TrustedFrozenAssessmentService._validation_cache.clear()
        with factory() as db:
            registrations, sources = _add_frozen_registrations(db)
            evidence_rows: list[StrategyV2ForwardEvidence] = []
            seed_day = date(2026, 7, 31)
            for target_day in trusted_assessment_sessions()[:3]:
                evidence, _artifact = _add_full_evidence(
                    db,
                    registration=registrations["AAPL.US"],
                    source_config=sources["AAPL.US"],
                    target_day=target_day,
                    seed_day=seed_day,
                )
                evidence_rows.append(evidence)
                seed_day = target_day

            evidence_rows[0].evidence_digest_sha256 = "malformed"
            second_link = db.get(
                StrategyV2ForwardEvidenceArtifact,
                (evidence_rows[1].id, "REPLAY_BUNDLE"),
            )
            assert second_link is not None
            second_link.binding_sha256 = "f" * 64
            evidence_rows[2].evidence_digest_sha256 = "malformed"
            db.commit()
            db.expunge_all()

            report: Any = TrustedFrozenAssessmentService(
                db,
                clock=lambda: datetime(
                    2026,
                    8,
                    4,
                    20,
                    16,
                    tzinfo=timezone.utc,
                ),
            ).get_report()
            TrustedFrozenAssessmentReport.model_validate(report)
            aapl = next(
                item for item in report["symbols"] if item["symbol"] == "AAPL.US"
            )
            first, second, future = aapl["leaves"][:3]
            assert first["disposition"] == "INVALID"
            assert first["blockers"] == ["EVIDENCE_IDENTITY_INVALID"]
            assert first["evidence_digest_sha256"] is None
            assert second["disposition"] == "INVALID"
            assert second["blockers"] == [
                "REPLAY_ARTIFACT_BINDING_INVALID"
            ]
            assert second["artifact_binding_sha256"] == "f" * 64
            assert future["disposition"] == "PENDING"
            assert future["row_present_after_cutoff"] is True
            assert future["blockers"] == ["EVIDENCE_AFTER_SERVER_CUTOFF"]
            assert future["evidence_digest_sha256"] is None
    finally:
        with TrustedFrozenAssessmentService._validation_cache_lock:
            TrustedFrozenAssessmentService._validation_cache.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
