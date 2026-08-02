from __future__ import annotations

import hashlib
import inspect
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, fields, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from functools import cache
from typing import Any, TypedDict, cast

import pytest

import app.domain.watchlist_quant_v6.assessment as assessment_module
from app.domain.watchlist_quant_v6 import (
    BAR_NEXT_OPEN_STRESSED,
    MAX_QUANT_V6_ARTIFACT_RAW_BYTES,
    MAX_QUANT_V6_ARTIFACT_JSON_DEPTH,
    QUANT_V6_ARTIFACT_CODEC,
    QUANT_V6_ARTIFACT_COMPRESSION_LEVEL,
    QUANT_V6_ARTIFACT_SCHEMA_VERSION,
    QUANT_V6_ACQUISITION_ADJUSTMENT_MODE,
    QUANT_V6_ACQUISITION_PERIOD,
    QUANT_V6_ACQUISITION_SPEC,
    QUANT_V6_ACQUISITION_SPEC_DIGEST,
    QUANT_V6_ASSESSMENT_ARTIFACT_KIND,
    QUANT_V6_ASSESSMENT_CONTRACT,
    QUANT_V6_EVENT_CONTRACT,
    QUANT_V6_EVENT_ARTIFACT_KIND,
    QUANT_V6_PAYLOAD_SCHEMA_VERSION,
    QUANT_V6_SESSION_INPUT_ARTIFACT_KIND,
    QUANT_V6_SESSION_INPUT_CONTRACT,
    QUANT_V6_SEMANTIC_DIGEST,
    QUANT_V6_SEMANTIC_SPEC,
    SESSION_CLUSTER_T90_BY_DF,
    SESSION_COVERED,
    SESSION_MISSING,
    BarNextOpenStressedEvent,
    QuantV6ArtifactError,
    QuantV6Assessment,
    QuantV6AssessmentError,
    QuantV6Bar,
    QuantV6SemanticError,
    QuantV6SessionLeaf,
    QuantV6ThresholdEvidence,
    QuantV6TrainingSession,
    assess_bar_next_open_stressed_window,
    build_bar_next_open_stressed_events,
    build_bar_next_open_stressed_session_events,
    build_quant_v6_threshold_evidence,
    canonical_decimal,
    canonical_quant_v6_json,
    canonical_utc_timestamp,
    decode_quant_v6_artifact,
    encode_quant_v6_artifact,
    quant_v6_consecutive_trading_session_dates,
    quant_v6_expected_rth_bar_starts,
    quant_v6_payload_sha256,
    quant_v6_previous_trading_session_dates,
    quant_v6_session_bars_sha256,
    session_cluster_one_sided_90_lcb,
    validate_bar_next_open_stressed_event,
    validate_quant_v6_threshold_evidence,
)
from app.domain.watchlist_quant_v6 import artifact as artifact_module
from app.domain.watchlist_quant_v6 import semantics as semantics_module


_SYMBOL = "AAPL.US"
_MARKET = "US"
_FEE_RATE = Decimal("0.0005")
_DEFAULT_DAY = date(2026, 3, 2)


class _ArtifactMetadata(TypedDict):
    digest_sha256: str
    schema_version: int
    kind: str
    codec: str
    raw_size: int
    compressed_size: int
    payload: bytes


def _dataclass_init_values(value: Any) -> dict[str, Any]:
    return {
        item.name: getattr(value, item.name)
        for item in fields(value)
    }


def _artifact_payload(
    *,
    kind: str = QUANT_V6_EVENT_ARTIFACT_KIND,
    **values: object,
) -> dict[str, object]:
    contract_by_kind = {
        QUANT_V6_ASSESSMENT_ARTIFACT_KIND: QUANT_V6_ASSESSMENT_CONTRACT,
        QUANT_V6_EVENT_ARTIFACT_KIND: QUANT_V6_EVENT_CONTRACT,
        QUANT_V6_SESSION_INPUT_ARTIFACT_KIND: QUANT_V6_SESSION_INPUT_CONTRACT,
    }
    return {
        **values,
        "contract": contract_by_kind[kind],
        "schema_version": QUANT_V6_PAYLOAD_SCHEMA_VERSION,
    }


def _raw_artifact(
    raw: bytes,
    *,
    kind: str = QUANT_V6_EVENT_ARTIFACT_KIND,
) -> _ArtifactMetadata:
    compressed = zlib.compress(
        raw,
        level=QUANT_V6_ARTIFACT_COMPRESSION_LEVEL,
    )
    return {
        "digest_sha256": hashlib.sha256(raw).hexdigest(),
        "schema_version": QUANT_V6_ARTIFACT_SCHEMA_VERSION,
        "kind": kind,
        "codec": QUANT_V6_ARTIFACT_CODEC,
        "raw_size": len(raw),
        "compressed_size": len(compressed),
        "payload": compressed,
    }


def _bar(
    start_at: datetime,
    *,
    opened: Decimal | int | str = "100",
    closed: Decimal | int | str = "100",
) -> QuantV6Bar:
    open_value = Decimal(opened)
    close_value = Decimal(closed)
    return QuantV6Bar(
        start_at=start_at,
        open=open_value,
        high=max(open_value, close_value) + Decimal("1"),
        low=min(open_value, close_value) - Decimal("1"),
        close=close_value,
        volume=Decimal("100000"),
    )


@cache
def _training_bars(session_day: date) -> tuple[QuantV6Bar, ...]:
    starts = quant_v6_expected_rth_bar_starts(_MARKET, session_day)
    return tuple(
        _bar(
            start,
            opened="100",
            closed="100.1" if index % 2 else "100",
        )
        for index, start in enumerate(starts)
    )


@cache
def _threshold(target_day: date) -> QuantV6ThresholdEvidence:
    prior_days = quant_v6_previous_trading_session_dates(
        _MARKET,
        target_day,
        count=10,
    )
    return build_quant_v6_threshold_evidence(
        symbol=_SYMBOL,
        market=_MARKET,
        target_session_date=target_day,
        training_sessions=tuple(
            QuantV6TrainingSession(day, _training_bars(day))
            for day in prior_days
        ),
    )


def _target_bars(
    session_day: date,
    event_count: int,
    *,
    exit_open: Decimal | int | str = "102",
) -> tuple[QuantV6Bar, ...]:
    starts = quant_v6_expected_rth_bar_starts(_MARKET, session_day)
    bars = [_bar(start) for start in starts]
    signal_indices = [1 + 8 * index for index in range(event_count)]
    for signal_index in signal_indices:
        bars[signal_index] = _bar(
            starts[signal_index],
            opened="100",
            closed="90",
        )
        exit_index = signal_index + 7
        bars[exit_index] = _bar(
            starts[exit_index],
            opened=exit_open,
            closed="100",
        )
    return tuple(bars)


def _events(
    session_day: date,
    event_count: int,
    *,
    exit_open: Decimal | int | str = "102",
):
    bars = _target_bars(session_day, event_count, exit_open=exit_open)
    return build_bar_next_open_stressed_session_events(
        symbol=_SYMBOL,
        market=_MARKET,
        session_date=session_day,
        bars=bars,
        threshold_evidence=_threshold(session_day),
        fee_rate=_FEE_RATE,
    )


def _single_event():
    values = _events(_DEFAULT_DAY, 1)
    assert len(values) == 1
    return values[0]


def _strong_leaves(
    *,
    missing: int = 1,
    event_session_counts: tuple[int, ...] | None = None,
) -> tuple[QuantV6SessionLeaf, ...]:
    dates = quant_v6_consecutive_trading_session_dates(
        _MARKET,
        _DEFAULT_DAY,
        count=30,
    )
    counts = event_session_counts or ((3,) * 20 + (0,) * (30 - missing - 20))
    if len(counts) != 30 - missing:
        raise AssertionError("covered counts do not match missing leaves")
    leaves: list[QuantV6SessionLeaf] = []
    for session_day, event_count in zip(dates[: len(counts)], counts, strict=True):
        bars = _target_bars(session_day, event_count)
        leaves.append(QuantV6SessionLeaf(
            session_date=session_day,
            status=SESSION_COVERED,
            session_bars=bars,
            threshold_evidence=_threshold(session_day),
            fee_rate=_FEE_RATE,
            events=build_bar_next_open_stressed_session_events(
                symbol=_SYMBOL,
                market=_MARKET,
                session_date=session_day,
                bars=bars,
                threshold_evidence=_threshold(session_day),
                fee_rate=_FEE_RATE,
            ),
        ))
    for session_day in dates[len(counts):]:
        leaves.append(QuantV6SessionLeaf(
            session_date=session_day,
            status=SESSION_MISSING,
            blockers=("MISSING_COMPLETE_BAR_INPUT",),
        ))
    return tuple(leaves)


def test_canonical_codec_normalizes_decimals_and_round_trips() -> None:
    payload = _artifact_payload(
        z=Decimal("-0.000"),
        a=[Decimal("1.2300"), {"unicode": "量化"}],
        integer=2,
    )
    raw = canonical_quant_v6_json(payload)
    encoded = encode_quant_v6_artifact(
        payload,
        kind=QUANT_V6_EVENT_ARTIFACT_KIND,
    )
    assert raw == (
        b'{"a":["1.23",{"unicode":"\xe9\x87\x8f\xe5\x8c\x96"}],'
        b'"contract":"watchlist-quant-v6-event-v1","integer":2,'
        b'"schema_version":1,"z":"0"}'
    )
    assert encoded.digest_sha256 == hashlib.sha256(raw).hexdigest()
    assert decode_quant_v6_artifact(**asdict(encoded)) == {
        "a": ["1.23", {"unicode": "量化"}],
        "contract": QUANT_V6_EVENT_CONTRACT,
        "integer": 2,
        "schema_version": QUANT_V6_PAYLOAD_SCHEMA_VERSION,
        "z": "0",
    }


def test_decoder_rejects_boolean_schema_version() -> None:
    encoded = encode_quant_v6_artifact(
        _artifact_payload(value="canonical"),
        kind=QUANT_V6_EVENT_ARTIFACT_KIND,
    )
    metadata = asdict(encoded)
    metadata["schema_version"] = True
    with pytest.raises(QuantV6ArtifactError, match="schema"):
        decode_quant_v6_artifact(**metadata)


def test_semantic_contract_and_t_table_are_runtime_immutable() -> None:
    with pytest.raises(TypeError):
        cast(dict[int, Decimal], SESSION_CLUSTER_T90_BY_DF)[1] = Decimal("0")
    with pytest.raises(TypeError):
        cast(dict[str, object], QUANT_V6_SEMANTIC_SPEC)["schema_version"] = 2
    assessment_spec = cast(
        dict[str, object],
        QUANT_V6_SEMANTIC_SPEC["assessment"],
    )
    with pytest.raises(TypeError):
        assessment_spec["minimum_events"] = 1
    with pytest.raises(TypeError):
        cast(dict[str, object], QUANT_V6_ACQUISITION_SPEC)[
            "fallback_allowed"
        ] = True
    assert QUANT_V6_SEMANTIC_SPEC["acquisition"] == (
        QUANT_V6_ACQUISITION_SPEC
    )
    artifact_envelope = cast(
        dict[str, object],
        QUANT_V6_SEMANTIC_SPEC["artifact_envelope"],
    )
    assert artifact_envelope["codec"] == QUANT_V6_ARTIFACT_CODEC
    assert artifact_envelope["compression_level"] == (
        QUANT_V6_ARTIFACT_COMPRESSION_LEVEL
    )
    assert set(cast(dict[str, str], artifact_envelope["kinds"]).values()) == {
        QUANT_V6_ASSESSMENT_ARTIFACT_KIND,
        QUANT_V6_EVENT_ARTIFACT_KIND,
        QUANT_V6_SESSION_INPUT_ARTIFACT_KIND,
    }
    assert artifact_envelope["limits"] == {
        "compressed_bytes": artifact_module.MAX_QUANT_V6_ARTIFACT_COMPRESSED_BYTES,
        "container_items": artifact_module.MAX_QUANT_V6_ARTIFACT_CONTAINER_ITEMS,
        "decimal_adjusted_exponent": (
            artifact_module.MAX_QUANT_V6_DECIMAL_ADJUSTED_EXPONENT
        ),
        "decimal_digits": artifact_module.MAX_QUANT_V6_DECIMAL_DIGITS,
        "integer_abs": artifact_module.MAX_QUANT_V6_ARTIFACT_INTEGER_ABS,
        "json_depth": artifact_module.MAX_QUANT_V6_ARTIFACT_JSON_DEPTH,
        "json_nodes": artifact_module.MAX_QUANT_V6_ARTIFACT_JSON_NODES,
        "key_bytes": artifact_module.MAX_QUANT_V6_ARTIFACT_KEY_BYTES,
        "raw_bytes": artifact_module.MAX_QUANT_V6_ARTIFACT_RAW_BYTES,
        "string_bytes": artifact_module.MAX_QUANT_V6_ARTIFACT_STRING_BYTES,
    }
    payload_contracts = cast(
        dict[str, object],
        QUANT_V6_SEMANTIC_SPEC["payload_contracts"],
    )
    assert payload_contracts == {
        "assessment": QUANT_V6_ASSESSMENT_CONTRACT,
        "event": QUANT_V6_EVENT_CONTRACT,
        "schema_version": QUANT_V6_PAYLOAD_SCHEMA_VERSION,
        "session_input": QUANT_V6_SESSION_INPUT_CONTRACT,
    }
    event_spec = cast(dict[str, object], QUANT_V6_SEMANTIC_SPEC["event"])
    assert event_spec["fee_rate_by_market"] == {
        "HK": Decimal("0.003"),
        "US": Decimal("0.0005"),
    }
    assert assessment_spec["session_cluster_sample"] == "ALL_COVERED_SESSIONS"
    assert assessment_spec["zero_event_covered_session_return_bps"] == Decimal("0")
    assert assessment_spec["current_capture_mode"] == BAR_NEXT_OPEN_STRESSED
    assert assessment_spec["current_capture_promotion_eligible"] is False
    assert assessment_spec["future_candidate_required_capture_mode"] == (
        "FULL_EVENT_BBO_VERIFIED"
    )
    assert hashlib.sha256(
        canonical_quant_v6_json(QUANT_V6_SEMANTIC_SPEC)
    ).hexdigest() == QUANT_V6_SEMANTIC_DIGEST


def test_artifact_kind_is_bound_to_payload_contract() -> None:
    event_payload = _artifact_payload(value="event")
    with pytest.raises(QuantV6ArtifactError, match="contract.*kind"):
        encode_quant_v6_artifact(
            event_payload,
            kind=QUANT_V6_ASSESSMENT_ARTIFACT_KIND,
        )
    encoded = encode_quant_v6_artifact(
        event_payload,
        kind=QUANT_V6_EVENT_ARTIFACT_KIND,
    )
    confused = asdict(encoded)
    confused["kind"] = QUANT_V6_ASSESSMENT_ARTIFACT_KIND
    with pytest.raises(QuantV6ArtifactError, match="contract.*kind"):
        decode_quant_v6_artifact(**confused)
    invalid_payload_schema = _artifact_payload(value="event")
    invalid_payload_schema["schema_version"] = True
    with pytest.raises(QuantV6ArtifactError, match="payload schema"):
        encode_quant_v6_artifact(
            invalid_payload_schema,
            kind=QUANT_V6_EVENT_ARTIFACT_KIND,
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("0E-20"), "0"),
        (Decimal("-0"), "0"),
        (Decimal("12.34000"), "12.34"),
        (Decimal("1E+3"), "1000"),
        ("0.00100", "0.001"),
    ],
)
def test_canonical_decimal_has_one_exponent_free_spelling(
    value: Decimal | str,
    expected: str,
) -> None:
    assert canonical_decimal(value) == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 1.5])
def test_encoder_rejects_every_native_float(value: float) -> None:
    with pytest.raises(QuantV6ArtifactError, match="float"):
        canonical_quant_v6_json({"value": value})


def test_all_decimal_evidence_inputs_reject_native_float_and_bool() -> None:
    start = quant_v6_expected_rth_bar_starts(_MARKET, _DEFAULT_DAY)[0]
    with pytest.raises(QuantV6SemanticError, match="decimal"):
        QuantV6Bar(
            start_at=start,
            open=100.1,  # type: ignore[arg-type]
            high=101.1,  # type: ignore[arg-type]
            low=99.1,  # type: ignore[arg-type]
            close=100.1,  # type: ignore[arg-type]
            volume=1.0,  # type: ignore[arg-type]
        )
    with pytest.raises(QuantV6ArtifactError, match="native float"):
        canonical_decimal(1.5)  # type: ignore[arg-type]
    with pytest.raises(QuantV6ArtifactError, match="boolean"):
        canonical_decimal(True)  # type: ignore[arg-type]
    with pytest.raises(QuantV6AssessmentError, match="decimals"):
        session_cluster_one_sided_90_lcb([1.0, 2.0])  # type: ignore[list-item]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b'{"a":1,"a":2}', "duplicate key"),
        (b'{"b":1,"a":2}', "not canonical"),
        (b'{ "a":1}', "not canonical"),
        (b'{"a":1.0}', "native JSON float"),
    ],
)
def test_decoder_rejects_duplicate_or_noncanonical_json(
    raw: bytes,
    message: str,
) -> None:
    with pytest.raises(QuantV6ArtifactError, match=message):
        decode_quant_v6_artifact(**_raw_artifact(raw))


def test_decoder_rejects_trailing_truncated_and_overexpanding_zlib() -> None:
    encoded = encode_quant_v6_artifact(
        _artifact_payload(value="canonical"),
        kind=QUANT_V6_EVENT_ARTIFACT_KIND,
    )
    trailing = asdict(encoded)
    trailing["payload"] = encoded.payload + zlib.compress(b"{}")
    trailing["compressed_size"] = len(trailing["payload"])
    with pytest.raises(QuantV6ArtifactError, match="trailing"):
        decode_quant_v6_artifact(**trailing)
    truncated = asdict(encoded)
    truncated["payload"] = encoded.payload[:-1]
    truncated["compressed_size"] = len(truncated["payload"])
    with pytest.raises(QuantV6ArtifactError, match="incomplete|decompression"):
        decode_quant_v6_artifact(**truncated)
    bomb_raw = b'{"value":"' + b"x" * 10_000 + b'"}'
    bomb = _raw_artifact(bomb_raw)
    bomb["raw_size"] = 20
    with pytest.raises(QuantV6ArtifactError, match="exceeds"):
        decode_quant_v6_artifact(**bomb)


def test_codec_enforces_depth_and_both_size_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested: object = "value"
    for _ in range(MAX_QUANT_V6_ARTIFACT_JSON_DEPTH + 2):
        nested = [nested]
    with pytest.raises(QuantV6ArtifactError, match="nesting"):
        canonical_quant_v6_json({"nested": nested})
    monkeypatch.setattr(artifact_module, "MAX_QUANT_V6_ARTIFACT_RAW_BYTES", 16)
    with pytest.raises(QuantV6ArtifactError, match="raw size"):
        encode_quant_v6_artifact(
            _artifact_payload(value="longer than sixteen bytes"),
            kind=QUANT_V6_EVENT_ARTIFACT_KIND,
        )
    monkeypatch.setattr(artifact_module, "MAX_QUANT_V6_ARTIFACT_RAW_BYTES", 10_000)
    monkeypatch.setattr(artifact_module, "MAX_QUANT_V6_ARTIFACT_COMPRESSED_BYTES", 4)
    with pytest.raises(QuantV6ArtifactError, match="compressed size"):
        encode_quant_v6_artifact(
            _artifact_payload(value="compress me"),
            kind=QUANT_V6_EVENT_ARTIFACT_KIND,
        )


def test_timestamp_requires_awareness_and_has_fixed_utc_spelling() -> None:
    aware = datetime(
        2026, 1, 5, 22, 30, 1, 42,
        tzinfo=timezone(timedelta(hours=8)),
    )
    assert canonical_utc_timestamp(aware) == "2026-01-05T14:30:01.000042Z"
    with pytest.raises(QuantV6ArtifactError, match="timezone-aware"):
        canonical_utc_timestamp(datetime(2026, 1, 5))


def test_threshold_is_replayed_from_exact_prior_ten_complete_sessions() -> None:
    evidence = _threshold(_DEFAULT_DAY)
    assert len(evidence.training_sessions) == 10
    assert tuple(item.session_date for item in evidence.training_sessions) == (
        quant_v6_previous_trading_session_dates(
            _MARKET,
            _DEFAULT_DAY,
            count=10,
        )
    )
    assert evidence.shock_threshold_bps > 0
    assert evidence.preimage_digest_sha256 == hashlib.sha256(
        canonical_quant_v6_json(evidence.canonical_preimage())
    ).hexdigest()
    evidence_payload = evidence.canonical_payload()
    assert len(cast(list[object], evidence_payload["training_sessions"])) == 10


def test_threshold_rejects_lookahead_missing_or_arbitrary_training_days() -> None:
    evidence = _threshold(_DEFAULT_DAY)
    sessions = list(evidence.training_sessions)
    sessions[-1] = QuantV6TrainingSession(
        _DEFAULT_DAY,
        _target_bars(_DEFAULT_DAY, 0),
    )
    with pytest.raises(QuantV6SemanticError, match="exact prior 10"):
        build_quant_v6_threshold_evidence(
            symbol=_SYMBOL,
            market=_MARKET,
            target_session_date=_DEFAULT_DAY,
            training_sessions=sessions,
        )


def test_training_session_date_rejects_datetime_subclass() -> None:
    with pytest.raises(QuantV6SemanticError, match="session_date"):
        QuantV6TrainingSession(
            datetime(2026, 2, 27, tzinfo=timezone.utc),  # type: ignore[arg-type]
            _training_bars(date(2026, 2, 27)),
        )


def test_threshold_validation_cache_is_not_unbounded_or_trusted() -> None:
    evidence = _threshold(_DEFAULT_DAY)
    assert not hasattr(
        semantics_module.validate_quant_v6_threshold_evidence,
        "cache_info",
    )
    assert semantics_module._training_session_absolute_returns.cache_info().maxsize == 512
    assert semantics_module._threshold_calculation.cache_info().maxsize == 128
    with pytest.raises(QuantV6SemanticError, match="exactly 10"):
        build_quant_v6_threshold_evidence(
            symbol=_SYMBOL,
            market=_MARKET,
            target_session_date=_DEFAULT_DAY,
            training_sessions=evidence.training_sessions[:-1],
        )


def test_event_rejects_arbitrary_threshold_and_premarket_bars() -> None:
    evidence = _threshold(_DEFAULT_DAY)
    canonical_bars = _target_bars(_DEFAULT_DAY, 1)
    with pytest.raises(QuantV6SemanticError, match="canonical replay"):
        build_bar_next_open_stressed_events(
            symbol=_SYMBOL,
            market=_MARKET,
            session_date=_DEFAULT_DAY,
            bars=canonical_bars[:9],
            threshold_evidence=replace(
                evidence,
                shock_threshold_bps=evidence.shock_threshold_bps / 2,
            ),
            fee_rate=_FEE_RATE,
        )
    premarket_start = quant_v6_expected_rth_bar_starts(
        _MARKET,
        _DEFAULT_DAY,
    )[0] - timedelta(hours=1)
    premarket = tuple(
        _bar(
            premarket_start + timedelta(minutes=5 * index),
            closed="90" if index == 1 else "100",
        )
        for index in range(9)
    )
    with pytest.raises(QuantV6SemanticError, match="inside market RTH"):
        build_bar_next_open_stressed_events(
            symbol=_SYMBOL,
            market=_MARKET,
            session_date=_DEFAULT_DAY,
            bars=premarket,
            threshold_evidence=evidence,
            fee_rate=_FEE_RATE,
        )


def test_symbol_suffix_must_match_market_for_every_replay_input() -> None:
    hk_day = date(2026, 3, 2)
    hk_bars = tuple(
        _bar(start)
        for start in quant_v6_expected_rth_bar_starts("HK", hk_day)
    )
    with pytest.raises(QuantV6SemanticError, match="suffix"):
        quant_v6_session_bars_sha256(
            symbol="AAPL.US",
            market="HK",
            session_date=hk_day,
            bars=hk_bars,
        )


def test_frozen_fee_authority_rejects_zero_or_caller_overrides() -> None:
    assert dict(semantics_module.QUANT_V6_FEE_RATE_BY_MARKET) == {
        "HK": Decimal("0.003"),
        "US": Decimal("0.0005"),
    }
    bars = _target_bars(_DEFAULT_DAY, 1)
    evidence = _threshold(_DEFAULT_DAY)
    with pytest.raises(QuantV6SemanticError, match="frozen market fee"):
        build_bar_next_open_stressed_events(
            symbol=_SYMBOL,
            market=_MARKET,
            session_date=_DEFAULT_DAY,
            bars=bars[:9],
            threshold_evidence=evidence,
            fee_rate=Decimal("0"),
        )
    with pytest.raises(QuantV6SemanticError, match="decimal"):
        build_bar_next_open_stressed_events(
            symbol=_SYMBOL,
            market=_MARKET,
            session_date=_DEFAULT_DAY,
            bars=bars[:9],
            threshold_evidence=evidence,
            fee_rate=0.0005,  # type: ignore[arg-type]
        )
    with pytest.raises(QuantV6AssessmentError, match="frozen market authority"):
        QuantV6SessionLeaf(
            session_date=_DEFAULT_DAY,
            status=SESSION_COVERED,
            session_bars=bars,
            threshold_evidence=evidence,
            fee_rate=Decimal("0"),
        )
    with pytest.raises(QuantV6AssessmentError, match="fee-rate snapshot"):
        QuantV6SessionLeaf(
            session_date=_DEFAULT_DAY,
            status=SESSION_COVERED,
            session_bars=bars,
            threshold_evidence=evidence,
            fee_rate=0.0005,  # type: ignore[arg-type]
        )


def test_bar_event_has_frozen_long_offsets_stress_and_cost_preimage() -> None:
    event = _single_event()
    assert event.entry_at == event.signal_bar.start_at + timedelta(minutes=5)
    assert event.exit_at == event.signal_bar.start_at + timedelta(minutes=35)
    assert len(event.holding_bars) == 6
    assert event.entry_bar == event.holding_bars[0]
    assert event.entry_fill_price == Decimal("100.0800")
    assert event.exit_fill_price == Decimal("101.9184")
    assert event.gross_reference_pnl - event.net_pnl == (
        event.cost_bps * event.entry_reference_notional / Decimal("10000")
    )
    payload = event.canonical_payload()
    execution = cast(dict[str, object], payload["execution"])
    signal = cast(dict[str, object], payload["signal"])
    assert payload["capture"] == {
        "historical_only": True,
        "mode": BAR_NEXT_OPEN_STRESSED,
        "promotion_eligible": False,
    }
    assert execution["side"] == "LONG"
    assert execution["position_add_on_allowed"] is False
    assert execution["overlap_allowed"] is False
    assert payload["p0"] == {
        "automatic_promotion_allowed": False,
        "order_submission_allowed": False,
        "short_entry_allowed": False,
    }
    assert signal["threshold_evidence"] is not None
    costs = cast(dict[str, object], payload["costs"])
    assert costs["exit_fee"] == canonical_decimal(event.exit_fee)
    assert execution["exit_bar"] == event.exit_bar.canonical_payload()
    validate_bar_next_open_stressed_event(event)


def test_event_digest_binds_the_complete_exit_bar_evidence() -> None:
    event = _single_event()
    altered_exit = replace(
        event.exit_bar,
        close=event.exit_bar.close + Decimal("0.5"),
    )
    altered_event = replace(event, exit_bar=altered_exit)
    validate_bar_next_open_stressed_event(altered_event)
    assert altered_event.artifact_digest_sha256 != event.artifact_digest_sha256


def test_event_builder_exposes_no_side_quantity_spread_or_naked_threshold() -> None:
    parameters = inspect.signature(build_bar_next_open_stressed_events).parameters
    assert not {
        "side", "quantity", "spread", "bid", "ask",
        "entry_price", "exit_price", "shock_threshold_bps",
    }.intersection(parameters)


def test_event_builder_is_down_only_non_overlapping_and_requires_full_horizon() -> None:
    starts = quant_v6_expected_rth_bar_starts(_MARKET, _DEFAULT_DAY)
    upward = tuple(
        _bar(
            start,
            closed="110" if index == 1 else "100",
        )
        for index, start in enumerate(starts[:9])
    )
    assert not build_bar_next_open_stressed_events(
        symbol=_SYMBOL,
        market=_MARKET,
        session_date=_DEFAULT_DAY,
        bars=upward,
        threshold_evidence=_threshold(_DEFAULT_DAY),
        fee_rate=_FEE_RATE,
    )
    events = _events(_DEFAULT_DAY, 2)
    assert [event.signal_bar.start_at for event in events] == [
        starts[1],
        starts[9],
    ]
    assert events[0].exit_at < events[1].signal_bar.start_at
    assert events[0].exit_at < events[1].entry_at
    short_bars = list(_target_bars(_DEFAULT_DAY, 0)[:8])
    short_bars[1] = _bar(starts[1], closed="90")
    assert not build_bar_next_open_stressed_events(
        symbol=_SYMBOL,
        market=_MARKET,
        session_date=_DEFAULT_DAY,
        bars=short_bars,
        threshold_evidence=_threshold(_DEFAULT_DAY),
        fee_rate=_FEE_RATE,
    )


def test_event_builder_rejects_cross_segment_gap_and_tampered_event() -> None:
    bars = list(_target_bars(_DEFAULT_DAY, 1)[:9])
    bars[4] = _bar(bars[4].start_at + timedelta(minutes=5))
    with pytest.raises(QuantV6SemanticError, match="contiguous"):
        build_bar_next_open_stressed_events(
            symbol=_SYMBOL,
            market=_MARKET,
            session_date=_DEFAULT_DAY,
            bars=bars,
            threshold_evidence=_threshold(_DEFAULT_DAY),
            fee_rate=_FEE_RATE,
        )
    event = _single_event()
    with pytest.raises(QuantV6SemanticError, match="canonical replay"):
        validate_bar_next_open_stressed_event(
            replace(event, net_pnl=event.net_pnl + Decimal("1"))
        )


def test_event_validator_rejects_subclass_with_false_ne_override() -> None:
    class _FalseNeEvent(BarNextOpenStressedEvent):
        def __ne__(self, other: object) -> bool:
            return False

    event = _single_event()
    values = _dataclass_init_values(event)
    values["net_pnl"] = event.net_pnl + Decimal("1")
    forged = _FalseNeEvent(**values)

    with pytest.raises(QuantV6SemanticError, match="unsupported type"):
        validate_bar_next_open_stressed_event(forged)
    with pytest.raises(QuantV6AssessmentError, match="events.*unsupported type"):
        QuantV6SessionLeaf(
            session_date=_DEFAULT_DAY,
            status=SESSION_COVERED,
            session_bars=_target_bars(_DEFAULT_DAY, 1),
            threshold_evidence=_threshold(_DEFAULT_DAY),
            fee_rate=_FEE_RATE,
            events=(forged,),
        )


def test_evidence_chain_rejects_bar_training_threshold_and_leaf_subclasses() -> None:
    class _BarSubclass(QuantV6Bar):
        pass

    class _TrainingSubclass(QuantV6TrainingSession):
        pass

    class _ThresholdSubclass(QuantV6ThresholdEvidence):
        def __ne__(self, other: object) -> bool:
            return False

    class _LeafSubclass(QuantV6SessionLeaf):
        pass

    target_bars = list(_target_bars(_DEFAULT_DAY, 0))
    target_bars[0] = _BarSubclass(
        **_dataclass_init_values(target_bars[0])
    )
    with pytest.raises(QuantV6SemanticError, match="unsupported QuantV6Bar"):
        quant_v6_session_bars_sha256(
            symbol=_SYMBOL,
            market=_MARKET,
            session_date=_DEFAULT_DAY,
            bars=target_bars,
        )

    threshold = _threshold(_DEFAULT_DAY)
    training_sessions = list(threshold.training_sessions)
    training_sessions[0] = _TrainingSubclass(
        **_dataclass_init_values(training_sessions[0])
    )
    with pytest.raises(QuantV6SemanticError, match="unsupported type"):
        build_quant_v6_threshold_evidence(
            symbol=_SYMBOL,
            market=_MARKET,
            target_session_date=_DEFAULT_DAY,
            training_sessions=training_sessions,
        )

    forged_threshold = _ThresholdSubclass(
        **_dataclass_init_values(threshold)
    )
    with pytest.raises(QuantV6SemanticError, match="unsupported type"):
        validate_quant_v6_threshold_evidence(forged_threshold)
    with pytest.raises(QuantV6SemanticError, match="unsupported type"):
        build_bar_next_open_stressed_events(
            symbol=_SYMBOL,
            market=_MARKET,
            session_date=_DEFAULT_DAY,
            bars=_target_bars(_DEFAULT_DAY, 1)[:9],
            threshold_evidence=forged_threshold,
            fee_rate=_FEE_RATE,
        )

    leaves = list(_strong_leaves())
    forged_leaf = _LeafSubclass(**_dataclass_init_values(leaves[0]))
    with pytest.raises(QuantV6AssessmentError, match="unsupported type"):
        forged_leaf.encoded_replay_input(symbol=_SYMBOL, market=_MARKET)
    leaves[0] = forged_leaf
    with pytest.raises(QuantV6AssessmentError, match="unsupported type"):
        assess_bar_next_open_stressed_window(
            symbol=_SYMBOL,
            market=_MARKET,
            leaves=leaves,
        )


def test_fixed_window_strong_bar_evidence_is_watch_but_never_candidate() -> None:
    assessment = assess_bar_next_open_stressed_window(
        symbol=_SYMBOL,
        market=_MARKET,
        leaves=_strong_leaves(),
    )
    encoded = assessment.encoded_artifact()
    payload = decode_quant_v6_artifact(**asdict(encoded))

    assert assessment.covered_sessions == 29
    assert assessment.event_count == 60
    assert assessment.event_sessions == 20
    assert assessment.median_net_return_bps is not None
    assert assessment.median_net_return_bps > 0
    assert assessment.session_cluster_lcb_90_bps is not None
    assert assessment.session_cluster_lcb_90_bps > 0
    assert assessment.gross_edge_to_cost_ratio is not None
    assert assessment.gross_edge_to_cost_ratio >= Decimal("2")
    assert assessment.candidate_thresholds_met is True
    assert assessment.recommended_action == "WATCH"
    assert assessment.promotion_eligible is False
    assert assessment.automatic_promotion_allowed is False
    assert assessment.order_submission_allowed is False
    assert assessment.blockers == ("HISTORICAL_CAPTURE_PROMOTION_INELIGIBLE",)
    assert len(cast(list[object], payload["leaves"])) == 30
    aggregates = cast(dict[str, object], payload["aggregates"])
    policy = cast(dict[str, object], payload["policy"])
    assert aggregates["session_denominator"] == 30
    assert policy["recommended_action"] == "WATCH"
    assert policy["position_add_on_allowed"] is False
    assert payload["session_cluster_methodology"] == {
        "missing_session_treatment": "EXCLUDED_NOT_ZERO",
        "sample": "ALL_COVERED_SESSIONS",
        "sample_sessions": 29,
        "zero_event_covered_session_return_bps": "0",
    }
    assert payload["contract"] == QUANT_V6_ASSESSMENT_CONTRACT
    assert encoded.kind == QUANT_V6_ASSESSMENT_ARTIFACT_KIND
    assert encoded.digest_sha256 == quant_v6_payload_sha256(payload)
    assert encoded.raw_size < MAX_QUANT_V6_ARTIFACT_RAW_BYTES
    replay_input = assessment.leaves[0].encoded_replay_input(
        symbol=_SYMBOL,
        market=_MARKET,
    )
    assert replay_input.kind == QUANT_V6_SESSION_INPUT_ARTIFACT_KIND
    assert replay_input.raw_size < MAX_QUANT_V6_ARTIFACT_RAW_BYTES
    replay_payload = decode_quant_v6_artifact(**asdict(replay_input))
    assert replay_payload["contract"] == QUANT_V6_SESSION_INPUT_CONTRACT
    assert replay_payload["acquisition"] == {
        "adjustment_mode": QUANT_V6_ACQUISITION_ADJUSTMENT_MODE,
        "bar_period": QUANT_V6_ACQUISITION_PERIOD,
        "contract": "watchlist-quant-v6-acquisition-v1",
        "exact_rth_grid_required": True,
        "fallback_allowed": False,
        "history_direction": "FORWARD_AFTER_CURSOR",
        "quote_context_only": True,
        "schema_version": 1,
        "spec_digest_sha256": QUANT_V6_ACQUISITION_SPEC_DIGEST,
    }


def test_assessment_noop_checkpoint_preserves_canonical_bytes_and_digest() -> None:
    leaves = _strong_leaves(
        missing=29,
        event_session_counts=(2,),
    )
    assessment = assess_bar_next_open_stressed_window(
        symbol=_SYMBOL,
        market=_MARKET,
        leaves=leaves,
    )

    baseline_bytes = canonical_quant_v6_json(assessment.canonical_payload())
    baseline_artifact = assessment.encoded_artifact()
    checkpoint_bytes = canonical_quant_v6_json(
        assessment.canonical_payload(checkpoint=lambda: None)
    )
    checkpoint_artifact = assessment.encoded_artifact(
        checkpoint=lambda: None,
    )

    assert checkpoint_bytes == baseline_bytes
    assert checkpoint_artifact.digest_sha256 == baseline_artifact.digest_sha256
    assert checkpoint_artifact.payload == baseline_artifact.payload


def test_private_fused_assessment_encoding_matches_strict_public_golden() -> None:
    leaves = _strong_leaves(
        missing=29,
        event_session_counts=(2,),
    )
    assessment, fused = (
        assessment_module._assess_and_encode_bar_next_open_stressed_window(
            symbol=_SYMBOL,
            market=_MARKET,
            leaves=leaves,
            checkpoint=lambda: None,
        )
    )
    strict = assessment.encoded_artifact(checkpoint=lambda: None)

    assert fused == strict
    assert zlib.decompress(fused.payload) == canonical_quant_v6_json(
        assessment.canonical_payload()
    )
    assert fused.digest_sha256 == hashlib.sha256(
        zlib.decompress(fused.payload)
    ).hexdigest()


def test_private_fused_reuses_full_session_event_replay_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaves = _strong_leaves(
        missing=29,
        event_session_counts=(2,),
    )
    locally_validated: list[BarNextOpenStressedEvent] = []
    original = assessment_module._validated_event_payload

    def _track_local_validation(
        event: BarNextOpenStressedEvent,
        *,
        checkpoint: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        locally_validated.append(event)
        return original(event, checkpoint=checkpoint)

    monkeypatch.setattr(
        assessment_module,
        "_validated_event_payload",
        _track_local_validation,
    )

    fused, _artifact = (
        assessment_module._assess_and_encode_bar_next_open_stressed_window(
            symbol=_SYMBOL,
            market=_MARKET,
            leaves=leaves,
        )
    )
    assert fused.event_count == 2
    assert locally_validated == []

    strict = assess_bar_next_open_stressed_window(
        symbol=_SYMBOL,
        market=_MARKET,
        leaves=leaves,
    )
    assert strict.event_count == 2
    assert locally_validated == list(leaves[0].events)


@pytest.mark.parametrize("tamper", ("event-field", "event-threshold", "omission"))
def test_private_fused_full_session_bytes_reject_event_tamper(
    tamper: str,
) -> None:
    leaves = list(_strong_leaves(
        missing=29,
        event_session_counts=(1,),
    ))
    first = leaves[0]
    event = first.events[0]
    if tamper == "event-field":
        events = (replace(
            event,
            net_return_bps=event.net_return_bps + Decimal("1"),
        ),)
    elif tamper == "event-threshold":
        events = (replace(
            event,
            threshold_evidence=replace(
                event.threshold_evidence,
                preimage_digest_sha256="f" * 64,
            ),
        ),)
    else:
        events = ()
    leaves[0] = replace(first, events=events)

    with pytest.raises(
        QuantV6AssessmentError,
        match="complete replay event set",
    ):
        assessment_module._assess_and_encode_bar_next_open_stressed_window(
            symbol=_SYMBOL,
            market=_MARKET,
            leaves=leaves,
        )


def test_private_fused_rejects_malformed_event_canonical_type() -> None:
    leaves = list(_strong_leaves(
        missing=29,
        event_session_counts=(1,),
    ))
    first = leaves[0]
    malformed_event = replace(
        first.events[0],
        net_return_bps=cast(Decimal, True),
    )
    leaves[0] = replace(first, events=(malformed_event,))

    with pytest.raises(
        QuantV6AssessmentError,
        match="event failed canonical replay validation",
    ):
        assessment_module._assess_and_encode_bar_next_open_stressed_window(
            symbol=_SYMBOL,
            market=_MARKET,
            leaves=leaves,
        )


@pytest.mark.parametrize(
    "tamper",
    (
        "event-symbol",
        "event-market",
        "event-session-date",
        "threshold-symbol",
        "threshold-market",
        "threshold-target-date",
        "training-session-date",
    ),
)
def test_private_fused_exact_type_guard_matches_public_validator(
    tamper: str,
) -> None:
    class _TextSubclass(str):
        pass

    class _DateSubclass(date):
        pass

    def _subclass_date(value: date) -> date:
        return _DateSubclass(value.year, value.month, value.day)

    leaves = list(_strong_leaves(
        missing=29,
        event_session_counts=(1,),
    ))
    first = leaves[0]
    event = first.events[0]
    evidence = event.threshold_evidence
    if tamper == "event-symbol":
        forged = replace(event, symbol=_TextSubclass(event.symbol))
    elif tamper == "event-market":
        forged = replace(event, market=_TextSubclass(event.market))
    elif tamper == "event-session-date":
        forged = replace(
            event,
            session_date=_subclass_date(event.session_date),
        )
    elif tamper == "threshold-symbol":
        forged = replace(
            event,
            threshold_evidence=replace(
                evidence,
                symbol=_TextSubclass(evidence.symbol),
            ),
        )
    elif tamper == "threshold-market":
        forged = replace(
            event,
            threshold_evidence=replace(
                evidence,
                market=_TextSubclass(evidence.market),
            ),
        )
    elif tamper == "threshold-target-date":
        forged = replace(
            event,
            threshold_evidence=replace(
                evidence,
                target_session_date=_subclass_date(
                    evidence.target_session_date
                ),
            ),
        )
    else:
        training_sessions = list(evidence.training_sessions)
        forged_training_session = replace(training_sessions[0])
        object.__setattr__(
            forged_training_session,
            "session_date",
            _subclass_date(forged_training_session.session_date),
        )
        training_sessions[0] = forged_training_session
        forged = replace(
            event,
            threshold_evidence=replace(
                evidence,
                training_sessions=tuple(training_sessions),
            ),
        )

    with pytest.raises(QuantV6SemanticError):
        validate_bar_next_open_stressed_event(forged)

    leaves[0] = replace(first, events=(forged,))
    with pytest.raises(
        QuantV6AssessmentError,
        match="event failed canonical replay validation",
    ):
        assessment_module._assess_and_encode_bar_next_open_stressed_window(
            symbol=_SYMBOL,
            market=_MARKET,
            leaves=leaves,
        )


def test_private_fused_event_checkpoint_exception_propagates_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaves = _strong_leaves(
        missing=29,
        event_session_counts=(1,),
    )
    sentinel = QuantV6ArtifactError("checkpoint sentinel")
    actual_payload_built = False
    original = BarNextOpenStressedEvent.canonical_payload

    def _track_payload(event: BarNextOpenStressedEvent) -> dict[str, object]:
        nonlocal actual_payload_built
        payload = original(event)
        actual_payload_built = True
        return payload

    def _checkpoint() -> None:
        if actual_payload_built:
            raise sentinel

    monkeypatch.setattr(
        BarNextOpenStressedEvent,
        "canonical_payload",
        _track_payload,
    )

    with pytest.raises(QuantV6ArtifactError) as caught:
        assessment_module._assess_and_encode_bar_next_open_stressed_window(
            symbol=_SYMBOL,
            market=_MARKET,
            leaves=leaves,
            checkpoint=_checkpoint,
        )

    assert caught.value is sentinel


def test_verified_assessment_memo_rejects_an_equal_clone() -> None:
    memo = assessment_module._VerifiedArtifactMemo.create()
    assessment = assessment_module.assess_bar_next_open_stressed_window(
        symbol=_SYMBOL,
        market=_MARKET,
        leaves=_strong_leaves(
            missing=29,
            event_session_counts=(0,),
        ),
        _verified_artifacts=memo,
    )
    equal_clone = replace(assessment)

    assert equal_clone == assessment
    assert equal_clone is not assessment
    memo.require_assessment(assessment, checkpoint=lambda: None)
    with pytest.raises(QuantV6AssessmentError, match="replay identity"):
        memo.require_assessment(equal_clone, checkpoint=lambda: None)


def test_private_fused_path_cannot_be_replaced_by_public_replay_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaves = _strong_leaves(
        missing=29,
        event_session_counts=(0,),
    )
    public_calls = 0
    original = assessment_module.assess_bar_next_open_stressed_window

    def _poison_public_wrapper(**kwargs):
        nonlocal public_calls
        public_calls += 1
        forged = original(**kwargs)
        object.__setattr__(forged, "event_count", forged.event_count + 1)
        return forged

    monkeypatch.setattr(
        assessment_module,
        "assess_bar_next_open_stressed_window",
        _poison_public_wrapper,
    )
    assessment, artifact = (
        assessment_module._assess_and_encode_bar_next_open_stressed_window(
            symbol=_SYMBOL,
            market=_MARKET,
            leaves=leaves,
        )
    )

    assert public_calls == 0
    assert assessment.event_count == 0
    payload = decode_quant_v6_artifact(**asdict(artifact))
    aggregates = cast(dict[str, object], payload["aggregates"])
    assert aggregates["event_count"] == 0
    assert public_calls == 0


def test_private_fused_path_rejects_core_result_with_equal_assessment_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaves = _strong_leaves(
        missing=29,
        event_session_counts=(0,),
    )
    original = assessment_module._assess_bar_next_open_stressed_window_core

    def _replace_verified_result(**kwargs):
        replay = original(**kwargs)
        return replace(replay, assessment=replace(replay.assessment))

    monkeypatch.setattr(
        assessment_module,
        "_assess_bar_next_open_stressed_window_core",
        _replace_verified_result,
    )

    with pytest.raises(QuantV6AssessmentError, match="replay identity"):
        assessment_module._assess_and_encode_bar_next_open_stressed_window(
            symbol=_SYMBOL,
            market=_MARKET,
            leaves=leaves,
        )


def test_private_fused_replay_isolated_from_checkpoint_object_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_leaves = _strong_leaves(
        missing=29,
        event_session_counts=(1,),
    )
    _, baseline_artifact = (
        assessment_module._assess_and_encode_bar_next_open_stressed_window(
            symbol=_SYMBOL,
            market=_MARKET,
            leaves=baseline_leaves,
        )
    )
    leaves = _strong_leaves(
        missing=29,
        event_session_counts=(1,),
    )
    caller_event = leaves[0].events[0]
    original_net_return = caller_event.net_return_bps
    first_leaf_isolated = False
    mutated = False
    original_deepcopy = assessment_module.deepcopy

    def _track_deepcopy(value: object, memo: dict[int, object]):
        nonlocal first_leaf_isolated
        result = original_deepcopy(value, memo)
        if value is leaves[0]:
            first_leaf_isolated = True
        return result

    def _mutating_checkpoint() -> None:
        nonlocal mutated
        if first_leaf_isolated and not mutated:
            object.__setattr__(
                caller_event,
                "net_return_bps",
                original_net_return + Decimal("999"),
            )
            mutated = True

    monkeypatch.setattr(assessment_module, "deepcopy", _track_deepcopy)
    assessment, artifact = (
        assessment_module._assess_and_encode_bar_next_open_stressed_window(
            symbol=_SYMBOL,
            market=_MARKET,
            leaves=leaves,
            checkpoint=_mutating_checkpoint,
        )
    )

    assert mutated is True
    assert caller_event.net_return_bps != original_net_return
    assert assessment.leaves[0].events[0] is not caller_event
    assert assessment.leaves[0].events[0].net_return_bps == original_net_return
    assert artifact == baseline_artifact


@pytest.mark.parametrize("phase", ("canonical", "compression"))
def test_private_fused_assessment_checkpoints_before_and_after_encoding_phase(
    phase: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaves = _strong_leaves(
        missing=29,
        event_session_counts=(0,),
    )
    sentinel = RuntimeError(f"{phase} checkpoint sentinel")
    phase_completed = False
    checkpoint_immediately_before_phase = False
    checkpoint_seen = False

    def _checkpoint() -> None:
        nonlocal checkpoint_seen
        checkpoint_seen = True
        if phase_completed:
            raise sentinel

    if phase == "canonical":
        original_canonical = assessment_module.canonical_quant_v6_json

        def _tracked_canonical(value: Mapping[str, object]) -> bytes:
            nonlocal phase_completed, checkpoint_immediately_before_phase
            checkpoint_immediately_before_phase = checkpoint_seen
            result = original_canonical(value)
            phase_completed = True
            return result

        monkeypatch.setattr(
            assessment_module,
            "canonical_quant_v6_json",
            _tracked_canonical,
        )
    else:
        original_encode = assessment_module._encode_quant_v6_canonical_bytes

        def _tracked_encode(
            *,
            value: Mapping[str, object],
            raw: bytes,
            kind: str,
        ) -> assessment_module.EncodedQuantV6Artifact:
            nonlocal phase_completed, checkpoint_immediately_before_phase
            checkpoint_immediately_before_phase = checkpoint_seen
            result = original_encode(value=value, raw=raw, kind=kind)
            phase_completed = True
            return result

        monkeypatch.setattr(
            assessment_module,
            "_encode_quant_v6_canonical_bytes",
            _tracked_encode,
        )

    with pytest.raises(RuntimeError) as caught:
        assessment_module._assess_and_encode_bar_next_open_stressed_window(
            symbol=_SYMBOL,
            market=_MARKET,
            leaves=leaves,
            checkpoint=_checkpoint,
        )

    assert caught.value is sentinel
    assert phase_completed is True
    assert checkpoint_immediately_before_phase is True


def test_verified_event_digest_memo_is_exact_instance_local_and_cooperative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _single_event()
    equal_clone = replace(event)
    memo = assessment_module._VerifiedArtifactMemo.create()
    validated: list[BarNextOpenStressedEvent] = []
    checkpoint_calls = 0
    original = assessment_module._validated_event_payload

    def _track_validation(
        value: BarNextOpenStressedEvent,
        *,
        checkpoint: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        validated.append(value)
        return original(value, checkpoint=checkpoint)

    def _checkpoint() -> None:
        nonlocal checkpoint_calls
        checkpoint_calls += 1

    monkeypatch.setattr(
        assessment_module,
        "_validated_event_payload",
        _track_validation,
    )
    first = assessment_module._validated_event_artifact_digest(
        event,
        checkpoint=_checkpoint,
        verified_artifacts=memo,
    )
    second = assessment_module._validated_event_artifact_digest(
        event,
        checkpoint=_checkpoint,
        verified_artifacts=memo,
    )
    clone_digest = assessment_module._validated_event_artifact_digest(
        equal_clone,
        checkpoint=_checkpoint,
        verified_artifacts=memo,
    )

    assert first == second == clone_digest
    assert validated == [event, equal_clone]
    assert checkpoint_calls > 2

    sentinel = RuntimeError("memo checkpoint sentinel")

    def _cancelled() -> None:
        raise sentinel

    with pytest.raises(RuntimeError) as caught:
        assessment_module._validated_event_artifact_digest(
            event,
            checkpoint=_cancelled,
            verified_artifacts=memo,
        )
    assert caught.value is sentinel


def test_assessment_verified_event_memo_is_scoped_to_one_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaves = _strong_leaves(
        missing=29,
        event_session_counts=(2,),
    )
    assessment = assess_bar_next_open_stressed_window(
        symbol=_SYMBOL,
        market=_MARKET,
        leaves=leaves,
    )
    validated: list[BarNextOpenStressedEvent] = []
    original = assessment_module._validated_event_payload

    def _track_validation(
        value: BarNextOpenStressedEvent,
        *,
        checkpoint: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        validated.append(value)
        return original(value, checkpoint=checkpoint)

    monkeypatch.setattr(
        assessment_module,
        "_validated_event_payload",
        _track_validation,
    )
    first = canonical_quant_v6_json(assessment.canonical_payload())
    second = canonical_quant_v6_json(assessment.canonical_payload())

    assert first == second
    assert validated == [*leaves[0].events, *leaves[0].events]


def test_checkpoint_sentinel_propagates_from_second_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaf = _strong_leaves(
        missing=29,
        event_session_counts=(2,),
    )[0]
    visited_events: list[BarNextOpenStressedEvent] = []
    checkpoint_calls = 0
    sentinel = RuntimeError("checkpoint sentinel")
    original = assessment_module._validated_event_artifact_digest

    def _track_event(
        event: BarNextOpenStressedEvent,
        *,
        checkpoint: Callable[[], None] | None = None,
    ) -> str:
        visited_events.append(event)
        return original(event, checkpoint=checkpoint)

    def _checkpoint() -> None:
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        if len(visited_events) == 2:
            raise sentinel

    monkeypatch.setattr(
        assessment_module,
        "_validated_event_artifact_digest",
        _track_event,
    )

    with pytest.raises(RuntimeError) as caught:
        leaf.canonical_payload(
            symbol=_SYMBOL,
            market=_MARKET,
            checkpoint=_checkpoint,
        )

    assert caught.value is sentinel
    assert len(visited_events) == 2
    assert checkpoint_calls > 1


def test_checkpoint_sentinel_propagates_from_second_covered_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaves = _strong_leaves(
        missing=28,
        event_session_counts=(0, 0),
    )
    replayed_sessions: list[date] = []
    checkpoint_calls = 0
    sentinel = RuntimeError("checkpoint sentinel")
    original = assessment_module.build_bar_next_open_stressed_session_events

    def _track_session(
        *,
        symbol: str,
        market: str,
        session_date: date,
        bars: Sequence[QuantV6Bar],
        threshold_evidence: QuantV6ThresholdEvidence,
        fee_rate: Decimal | int | str,
    ) -> tuple[BarNextOpenStressedEvent, ...]:
        replayed_sessions.append(session_date)
        return original(
            symbol=symbol,
            market=market,
            session_date=session_date,
            bars=bars,
            threshold_evidence=threshold_evidence,
            fee_rate=fee_rate,
        )

    def _checkpoint() -> None:
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        if len(replayed_sessions) == 2:
            raise sentinel

    monkeypatch.setattr(
        assessment_module,
        "build_bar_next_open_stressed_session_events",
        _track_session,
    )

    with pytest.raises(RuntimeError) as caught:
        assess_bar_next_open_stressed_window(
            symbol=_SYMBOL,
            market=_MARKET,
            leaves=leaves,
            checkpoint=_checkpoint,
        )

    assert caught.value is sentinel
    assert len(replayed_sessions) == 2
    assert checkpoint_calls > 1


@pytest.mark.parametrize("field", ["preimage_digest_sha256", "shock_threshold_bps"])
def test_session_input_encoding_rejects_tampered_threshold_evidence(
    field: str,
) -> None:
    leaf = _strong_leaves()[0]
    assert leaf.threshold_evidence is not None
    replacement: object = (
        "f" * 64
        if field == "preimage_digest_sha256"
        else leaf.threshold_evidence.shock_threshold_bps / 2
    )
    tampered = replace(
        leaf,
        threshold_evidence=replace(
            leaf.threshold_evidence,
            **{field: replacement},
        ),
    )
    with pytest.raises(QuantV6AssessmentError, match="threshold evidence"):
        tampered.encoded_replay_input(symbol=_SYMBOL, market=_MARKET)


def test_assessment_rejects_selective_event_omission() -> None:
    leaves = list(_strong_leaves())
    first = leaves[0]
    leaves[0] = replace(first, events=first.events[:-1])
    with pytest.raises(QuantV6AssessmentError, match="complete replay event set"):
        assess_bar_next_open_stressed_window(
            symbol=_SYMBOL,
            market=_MARKET,
            leaves=leaves,
        )


def test_covered_and_missing_leaf_shapes_are_fail_closed() -> None:
    bars = _target_bars(_DEFAULT_DAY, 0)
    with pytest.raises(QuantV6AssessmentError, match="covered leaf cannot"):
        QuantV6SessionLeaf(
            session_date=_DEFAULT_DAY,
            status=SESSION_COVERED,
            session_bars=bars,
            threshold_evidence=_threshold(_DEFAULT_DAY),
            fee_rate=_FEE_RATE,
            blockers=("IGNORED",),
        )
    with pytest.raises(QuantV6AssessmentError, match="explicit blocker"):
        QuantV6SessionLeaf(
            session_date=_DEFAULT_DAY,
            status=SESSION_MISSING,
        )
    with pytest.raises(QuantV6AssessmentError, match="fee-rate"):
        QuantV6SessionLeaf(
            session_date=_DEFAULT_DAY,
            status=SESSION_COVERED,
            session_bars=bars,
            threshold_evidence=_threshold(_DEFAULT_DAY),
            fee_rate=False,  # type: ignore[arg-type]
        )
    with pytest.raises(QuantV6AssessmentError, match="session_date"):
        QuantV6SessionLeaf(
            session_date=datetime(2026, 3, 2, tzinfo=timezone.utc),  # type: ignore[arg-type]
            status=SESSION_MISSING,
            blockers=("MISSING",),
        )
    with pytest.raises(QuantV6AssessmentError, match="canonical text"):
        QuantV6SessionLeaf(
            session_date=_DEFAULT_DAY,
            status=SESSION_MISSING,
            blockers=(1,),  # type: ignore[arg-type]
        )


def test_assessment_requires_consecutive_market_sessions() -> None:
    leaves = list(_strong_leaves())
    skipped_day = leaves[10].session_date
    leaves[10] = replace(
        leaves[10],
        session_date=leaves[11].session_date,
    )
    leaves[11] = replace(
        leaves[11],
        session_date=skipped_day + timedelta(days=10),
    )
    leaves.sort(key=lambda item: item.session_date)
    with pytest.raises(
        QuantV6AssessmentError,
        match="unique ascending|consecutive market trading sessions",
    ):
        assess_bar_next_open_stressed_window(
            symbol=_SYMBOL,
            market=_MARKET,
            leaves=leaves,
        )


def test_assessment_never_shrinks_denominator_or_relaxes_sample_gates() -> None:
    low_coverage = assess_bar_next_open_stressed_window(
        symbol=_SYMBOL,
        market=_MARKET,
        leaves=_strong_leaves(
            missing=2,
            event_session_counts=(3,) * 20 + (0,) * 8,
        ),
    )
    assert low_coverage.covered_sessions == 28
    assert "INSUFFICIENT_SESSION_COVERAGE" in low_coverage.blockers
    fifty_nine = assess_bar_next_open_stressed_window(
        symbol=_SYMBOL,
        market=_MARKET,
        leaves=_strong_leaves(
            event_session_counts=(3,) * 19 + (2,) + (0,) * 9,
        ),
    )
    assert fifty_nine.event_count == 59
    assert "INSUFFICIENT_EVENTS" in fifty_nine.blockers
    nineteen_sessions = assess_bar_next_open_stressed_window(
        symbol=_SYMBOL,
        market=_MARKET,
        leaves=_strong_leaves(
            event_session_counts=(4,) * 3 + (3,) * 16 + (0,) * 10,
        ),
    )
    assert nineteen_sessions.event_count == 60
    assert nineteen_sessions.event_sessions == 19
    assert "INSUFFICIENT_EVENT_SESSIONS" in nineteen_sessions.blockers


def test_assessment_object_cannot_encode_forged_p0_or_aggregates() -> None:
    assessment = assess_bar_next_open_stressed_window(
        symbol=_SYMBOL,
        market=_MARKET,
        leaves=_strong_leaves(),
    )
    with pytest.raises(QuantV6AssessmentError, match="P0"):
        replace(assessment, automatic_promotion_allowed=True)
    forged = replace(assessment, event_count=assessment.event_count + 1)
    with pytest.raises(QuantV6AssessmentError, match="canonical replay"):
        forged.encoded_artifact()
    with pytest.raises(QuantV6AssessmentError, match="finite Decimal"):
        replace(assessment, session_cluster_lcb_90_bps=1.0)  # type: ignore[arg-type]


def test_assessment_subclass_cannot_bypass_replay_with_false_ne() -> None:
    class _FalseNeAssessment(QuantV6Assessment):
        def __ne__(self, other: object) -> bool:
            return False

    assessment = assess_bar_next_open_stressed_window(
        symbol=_SYMBOL,
        market=_MARKET,
        leaves=_strong_leaves(),
    )
    values = _dataclass_init_values(assessment)
    values["event_count"] = assessment.event_count + 1
    forged = _FalseNeAssessment(**values)
    assert forged.promotion_eligible is False
    assert forged.automatic_promotion_allowed is False
    assert forged.order_submission_allowed is False

    with pytest.raises(QuantV6AssessmentError, match="unsupported type"):
        forged.canonical_payload()
    with pytest.raises(QuantV6AssessmentError, match="unsupported type"):
        forged.encoded_artifact()


def test_assessment_requires_exactly_30_leaves() -> None:
    with pytest.raises(QuantV6AssessmentError, match="exactly 30"):
        assess_bar_next_open_stressed_window(
            symbol=_SYMBOL,
            market=_MARKET,
            leaves=_strong_leaves()[:-1],
        )


def test_assessment_rejects_invalid_symbol_even_when_every_leaf_is_missing() -> None:
    dates = quant_v6_consecutive_trading_session_dates(
        _MARKET,
        _DEFAULT_DAY,
        count=30,
    )
    leaves = tuple(
        QuantV6SessionLeaf(
            session_date=session_day,
            status=SESSION_MISSING,
            blockers=("MISSING_COMPLETE_BAR_INPUT",),
        )
        for session_day in dates
    )
    with pytest.raises(QuantV6AssessmentError, match="invalid assessment"):
        assess_bar_next_open_stressed_window(
            symbol="BAD!.US",
            market=_MARKET,
            leaves=leaves,
        )


def test_session_cluster_lcb_uses_pinned_one_sided_t_contract() -> None:
    values = [Decimal("10"), Decimal("20"), Decimal("30")]
    lcb = session_cluster_one_sided_90_lcb(values)
    assert lcb is not None
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        expected = Decimal("20") - Decimal("1.885618083") * (
            Decimal("10") / context.sqrt(Decimal("3"))
        )
    assert lcb == expected
    assert session_cluster_one_sided_90_lcb([]) is None
    assert session_cluster_one_sided_90_lcb([Decimal("1")]) is None


def test_assessment_lcb_equal_weights_all_covered_sessions_and_zero_event_days() -> None:
    assessment = assess_bar_next_open_stressed_window(
        symbol=_SYMBOL,
        market=_MARKET,
        leaves=_strong_leaves(),
    )
    session_returns: list[Decimal] = []
    event_session_returns: list[Decimal] = []
    for leaf in assessment.leaves:
        if leaf.status != SESSION_COVERED:
            continue
        if not leaf.events:
            session_returns.append(Decimal("0"))
            continue
        total_notional = sum(
            (event.entry_reference_notional for event in leaf.events),
            Decimal("0"),
        )
        total_net = sum((event.net_pnl for event in leaf.events), Decimal("0"))
        value = total_net / total_notional * Decimal("10000")
        session_returns.append(value)
        event_session_returns.append(value)
    assert len(session_returns) == assessment.covered_sessions == 29
    assert len(event_session_returns) == assessment.event_sessions == 20
    assert assessment.session_cluster_lcb_90_bps == (
        session_cluster_one_sided_90_lcb(session_returns)
    )
    assert assessment.session_cluster_lcb_90_bps != (
        session_cluster_one_sided_90_lcb(event_session_returns)
    )


def test_quant_v6_domain_golden_digests() -> None:
    event = _single_event()
    assessment = assess_bar_next_open_stressed_window(
        symbol=_SYMBOL,
        market=_MARKET,
        leaves=_strong_leaves(),
    )
    assert QUANT_V6_SEMANTIC_DIGEST == (
        "f83e10d59aeef07c139569394ce23b0d6a1f799bf8ef40bd49b860f11c143316"
    )
    assert event.artifact_digest_sha256 == (
        "d062e2975f058866d99fe30ae02df761a51f6255061afb50bd6bc38bae6266b4"
    )
    assert assessment.assessment_digest_sha256 == (
        "c092c4dce8678dfce6bfeffd4d4aeb76f3f8541b706b3cfa4a91cb935e3a281b"
    )
