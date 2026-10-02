from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.services import watchlist_quant_v6_historical_provider as provider_module
from app.services.watchlist_quant_v6_historical_provider import (
    QuantV6HistoricalBarProvider, QuantV6HistoricalProviderError,
    _paging_failure_message, parse_quant_v6_paging_failure,
)
from app.services.watchlist_quant_v6_spawn_supervisor import (
    _classify_fetch_failure, _failure_wire, _rebuild_failure,
)

UTC = timezone.utc
START = datetime(2026, 8, 5, 13, 35, tzinfo=UTC)
END = datetime(2026, 9, 30, 20, tzinfo=UTC)
CURSOR = START - timedelta(minutes=5)
OLD = datetime(2026, 8, 4, 19, 55, tzinfo=UTC)
PREFIX = "historical candlestick cursor did not advance"
MARKER = " | qv6_page_diag_v1="


def _row(timestamp: object = OLD, **updates: object) -> SimpleNamespace:
    data: dict[str, object] = dict(timestamp=timestamp, open="98761.125", high="98763.125",
        low="98760.125", close="98762.125", volume="7654321")
    data.update(updates)
    return SimpleNamespace(**data)


class _FakeContext:
    def __init__(self, pages: list[list[object]]) -> None:
        self.pages = pages
        self.calls: list[tuple[Any, ...]] = []

    def history_candlesticks_by_offset(self, *args: Any) -> list[object]:
        self.calls.append(args)
        assert args[:5] == ("EA.US", "MIN5", "RAW", True, 1000)
        return self.pages[len(self.calls) - 1]


def _provider(pages: list[list[object]]) -> tuple[QuantV6HistoricalBarProvider, _FakeContext]:
    context = _FakeContext(pages)
    provider = QuantV6HistoricalBarProvider(module_loader=lambda: pytest.fail("no SDK/context/credentials"))
    provider._module = SimpleNamespace(Period=SimpleNamespace(Min_5="MIN5"),
                                       AdjustType=SimpleNamespace(NoAdjust="RAW"))
    provider._quote_context = context
    return provider, context


@pytest.fixture(autouse=True)
def _utc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_module, "_runtime_local_timezone_is_utc", lambda: True)


def _error(pages: list[list[object]]) -> tuple[QuantV6HistoricalProviderError, _FakeContext]:
    provider, context = _provider(pages)
    with pytest.raises(QuantV6HistoricalProviderError, match=PREFIX) as caught:
        provider.fetch_five_minute_no_adjust("EA.US", start_at=START, end_at=END)
    assert type(caught.value) is QuantV6HistoricalProviderError
    return caught.value, context


def _metadata(error: BaseException) -> Any:
    message = str(error)
    assert message.startswith(PREFIX + MARKER), "old rejection remains correct; missing diagnostic metadata is the gap"
    assert message.isascii() and "\n" not in message
    assert len(message.encode("utf-8")) <= 1800
    data = parse_quant_v6_paging_failure(message)
    assert data is not None
    assert data["v"] == 1
    return data


@pytest.mark.parametrize("timestamp", [OLD, OLD.replace(tzinfo=None)])
def test_first_page_valid_pre_window_singleton_is_empty_evidence(timestamp: datetime) -> None:
    provider, context = _provider([[_row(timestamp)]])
    result = provider.fetch_five_minute_no_adjust("EA.US", start_at=START, end_at=END)
    assert result.bars == ()
    assert (result.pages, result.raw_rows, result.rejected_rows) == (1, 1, 0)
    assert len(context.calls) == 1


@pytest.mark.parametrize("accepted", [False, True])
def test_later_page_old_singleton_stays_fail_closed(accepted: bool) -> None:
    first = _row(START) if accepted else _row(START, close=-1)
    error, context = _error([[first], [_row()]])
    data = _metadata(error)
    assert data["page"] == 2
    assert data["counts"]["accepted_total"] == int(accepted)
    assert len(context.calls) == 2


def test_first_page_acceptance_uses_real_validator_not_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_module, "_coerce_bar", lambda item, timestamp: None)
    error, context = _error([[_row()]])
    assert _metadata(error)["current_boundary"]["descriptor"]["bar_validation"] == "VALID"
    assert len(context.calls) == 1


def test_first_page_success_does_not_build_failure_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(**kwargs: Any) -> str:
        pytest.fail("successful empty evidence must not build a failure marker")
    monkeypatch.setattr(provider_module, "_paging_failure_message", fail)
    provider, context = _provider([[_row()]])
    result = provider.fetch_five_minute_no_adjust("EA.US", start_at=START, end_at=END)
    assert result.bars == ()
    assert (result.pages, result.raw_rows, result.rejected_rows) == (1, 1, 0)
    assert len(context.calls) == 1


@pytest.mark.parametrize("rows,selection,before,equal", [
    ([_row(OLD.replace(minute=45)), _row(OLD.replace(minute=50)), _row()], "MAX_UNIQUE", 3, 0),
    ([_row(CURSOR)], "CURSOR_UNIQUE", 0, 1),
    ([_row(OLD.replace(minute=50)), _row()], "MAX_UNIQUE", 2, 0),
])
def test_first_nonadvance_diagnostic_not_acceptance_fix(rows: list[object], selection: str, before: int, equal: int) -> None:
    error, context = _error([rows])
    data = _metadata(error)
    assert len(context.calls) == 1
    assert data["code"] == "CURSOR_NOT_ADVANCING"
    assert data["diagnostic_status"] == "AVAILABLE"
    assert data["symbol"] == "EA.US" and data["page"] == 1
    assert data["previous_raw_boundary"] is None
    assert data["previous_accepted"] is None
    assert data["current_boundary"]["selection"] == selection
    assert data["request"] == {"start_utc": "2026-08-05T13:35:00Z", "end_utc": "2026-09-30T20:00:00Z",
        "cursor_utc": "2026-08-05T13:30:00Z", "cursor_exchange": context.calls[0][5].isoformat(), "page_size": 1000}
    assert data["counts"] == {"page_rows": len(rows), "raw_total": len(rows), "rejected_total": 0,
        "accepted_total": 0, "before_cursor": before, "at_cursor": equal, "after_cursor": 0, "unparsed": 0}


def test_last_invalid_raw_is_not_last_accepted() -> None:
    later = START + timedelta(minutes=5)
    error, context = _error([[_row(START), _row(later, close=-1)], [_row(later, close=-1)]])
    data = _metadata(error)
    assert len(context.calls) == 2 and data["page"] == 2
    assert data["previous_raw_boundary"]["timestamp_utc"] == "2026-08-05T13:40:00Z"
    assert data["previous_raw_boundary"]["bar_validation"] == "INVALID"
    assert data["previous_accepted"]["timestamp_utc"] == "2026-08-05T13:35:00Z"
    assert data["comparison"] == {"raw_match": True, "accepted_match": False, "changed_fields": []}
    assert data["counts"]["accepted_total"] == 1
    assert data["counts"]["rejected_total"] == 1


@pytest.mark.parametrize("changes,fields", [({"close": "98762.25"}, ["close"]), ({"volume": "7654322"}, ["volume"])])
def test_legal_revisions_only_expose_field_names(changes: dict[str, object], fields: list[str], caplog: pytest.LogCaptureFixture) -> None:
    error, context = _error([[_row(START)], [_row(START, **changes)]])
    data = _metadata(error)
    assert len(context.calls) == 2
    assert data["comparison"] == {"raw_match": False, "accepted_match": False, "changed_fields": fields}
    assert data["current_boundary"]["descriptor"]["bar_validation"] == "VALID"
    for secret in ("98761", "98762", "765432", "fingerprint", "payload-secret", "account-secret"):
        assert secret not in str(error) + caplog.text
    assert caplog.text == ""


@pytest.mark.parametrize("rows,selection", [([_row(), _row()], "AMBIGUOUS"), ([_row(None)], "NONE")])
def test_ambiguous_and_missing_timestamp(rows: list[object], selection: str) -> None:
    error, _ = _error([rows])
    data = _metadata(error)
    assert data["current_boundary"] == {"selection": selection, "descriptor": None}
    assert data["comparison"] == {"raw_match": None, "accepted_match": None, "changed_fields": None}
    assert data["counts"]["unparsed"] == (1 if selection == "NONE" else 0)


def test_cursor_boundary_preferred_over_duplicate_old_rows() -> None:
    error, _ = _error([[_row(), _row(), _row(CURSOR)]])
    data = _metadata(error)
    assert data["current_boundary"]["selection"] == "CURSOR_UNIQUE"
    assert data["current_boundary"]["descriptor"]["timestamp_utc"] == "2026-08-05T13:30:00Z"


@pytest.mark.parametrize("timestamp,kind,naive", [
    (OLD.replace(tzinfo=None), "DATETIME", True),
    (OLD.astimezone(ZoneInfo("America/New_York")), "DATETIME", False),
    ("2026-08-04T19:55:00", "STRING", True),
    (OLD.timestamp(), "NUMBER", None),
])
def test_normalized_timestamp_descriptor(timestamp: object, kind: str, naive: bool | None) -> None:
    error, _ = _error([[_row(OLD.replace(minute=50)), _row(timestamp)]])
    descriptor = _metadata(error)["current_boundary"]["descriptor"]
    assert descriptor == {"timestamp_utc": "2026-08-04T19:55:00Z", "timestamp_kind": kind,
        "naive": naive, "bar_validation": "VALID", "reason": "OK"}


@pytest.mark.parametrize("timestamp,changes,reason", [
    (OLD.replace(second=1), {}, "OFF_GRID"), (OLD, {"close": "NaN"}, "NONFINITE"),
    (OLD, {"high": 1}, "OHLC_INCONSISTENT"), (OLD, {"volume": -1}, "NEGATIVE_VOLUME"),
])
def test_fixed_invalid_reasons(timestamp: object, changes: dict[str, object], reason: str) -> None:
    error, _ = _error([[_row(timestamp, **changes)]])
    descriptor = _metadata(error)["current_boundary"]["descriptor"]
    assert descriptor["bar_validation"] == "INVALID"
    assert descriptor["reason"] == reason


@pytest.mark.parametrize("terminal", [[], [_row(START)]])
def test_success_empty_and_valid_singleton_unchanged(terminal: list[object]) -> None:
    provider, context = _provider([[_row(START)], terminal])
    result = provider.fetch_five_minute_no_adjust("EA.US", start_at=START, end_at=END)
    expected = provider_module._coerce_bar(_row(START), START)
    assert result.bars == (expected,)
    assert (result.pages, result.raw_rows, result.rejected_rows) == (2, 1 + len(terminal), 0)
    assert len(context.calls) == 2


def test_normal_end_success_and_counts_unchanged() -> None:
    provider, context = _provider([[_row(START), _row(START + timedelta(minutes=5), close=-1), _row(END)]])
    result = provider.fetch_five_minute_no_adjust("EA.US", start_at=START, end_at=END)
    assert result.bars == (provider_module._coerce_bar(_row(START), START),)
    assert (result.pages, result.raw_rows, result.rejected_rows) == (1, 3, 1)
    assert len(context.calls) == 1


def test_wire_roundtrip_preserves_metadata_type_and_ordinal() -> None:
    error, _ = _error([[_row(CURSOR)]])
    data = _metadata(error)
    kind = _classify_fetch_failure(error)
    assert kind == "PROVIDER"
    wire = _failure_wire(ordinal=40, kind=kind, exc=error)
    assert wire.exception_type == "QuantV6HistoricalProviderError"
    assert wire.ordinal == 40 and wire.message == str(error)
    rebuilt = _rebuild_failure(wire)
    assert type(rebuilt) is QuantV6HistoricalProviderError
    assert str(rebuilt).startswith("quant-v6 candidate ordinal 40: ")
    assert parse_quant_v6_paging_failure(str(rebuilt)) == data


@pytest.mark.parametrize("mode", ["raise", "size"])
def test_builder_failure_or_oversize_is_complete_tiny_json(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    def bad_builder(**kwargs: Any) -> dict[str, object]:
        if mode == "raise":
            raise ValueError("token-secret account-secret payload-secret")
        return {"v": 1, "code": "CURSOR_NOT_ADVANCING", "diagnostic_status": "AVAILABLE", "symbol": "secret" * 1000}
    monkeypatch.setattr(provider_module, "_paging_failure_payload", bad_builder)
    error, context = _error([[_row(CURSOR)]])
    data = _metadata(error)
    assert data == {"v": 1, "code": "BUILD_FAILED" if mode == "raise" else "SIZE_LIMIT", "diagnostic_status": "UNAVAILABLE"}
    assert len(context.calls) == 1
    assert "secret" not in str(error)


def test_parser_strict_rejection() -> None:
    error, _ = _error([[_row(CURSOR)]])
    data = _metadata(error)
    payload = json.dumps(data, separators=(",", ":"))
    malformed = [str(error)[:-1], str(error) + MARKER + payload, "arbitrary prefix " + str(error),
                 str(error) + " trailing", PREFIX + MARKER + '{"v":1,"v":1}',
                 PREFIX + MARKER + payload.replace('"v":1', '"v":true'),
                 PREFIX + MARKER + payload.replace('"v":1', '"v":2'),
                 PREFIX + MARKER + payload.replace('"page":1', '"page":NaN'),
                 "x" * 2049]
    for message in malformed:
        assert parse_quant_v6_paging_failure(message) is None
    for key, value in [("secret", "raw"), ("page", True), ("symbol", "EA.US secret"), ("diagnostic_status", "COMPLETE")]:
        assert parse_quant_v6_paging_failure(PREFIX + MARKER + json.dumps({**data, key: value})) is None


def test_huge_decimal_and_untrusted_string_are_unknown_without_formatting() -> None:
    class _FakeUntrusted:
        def __str__(self) -> str:
            pytest.fail("diagnostic must not format arbitrary field values")
        def __repr__(self) -> str:
            pytest.fail("diagnostic must not repr arbitrary field values")
    for value in (Decimal("1e999999999"), _FakeUntrusted(), "9" * 10000):
        error, _ = _error([[_row(CURSOR, close=value)]])
        descriptor = _metadata(error)["current_boundary"]["descriptor"]
        assert descriptor["bar_validation"] == "UNKNOWN"
        assert descriptor["reason"] == "UNREADABLE"


def test_formatter_direct_build_failure_keeps_original_prefix() -> None:
    message = _paging_failure_message()
    data = _metadata(QuantV6HistoricalProviderError(message))
    assert data == {"v": 1, "code": "BUILD_FAILED", "diagnostic_status": "UNAVAILABLE"}


def test_largest_legitimate_payload_fits_and_roundtrips(monkeypatch: pytest.MonkeyPatch) -> None:
    error, _ = _error([[_row(START)], [_row(START, close="98762.25")]])
    data = dict(_metadata(error))
    data["symbol"] = "A" * 47 + ".US"
    data["page"] = 16
    data["counts"] = {"page_rows": 1000, "raw_total": 16000, "rejected_total": 6000,
        "accepted_total": 10000, "before_cursor": 998, "at_cursor": 1, "after_cursor": 0, "unparsed": 1}
    data["comparison"]["changed_fields"] = ["open", "high", "low", "close", "volume"]
    monkeypatch.setattr(provider_module, "_paging_failure_payload", lambda **kwargs: data)
    message = _paging_failure_message()
    assert len(message.encode("utf-8")) <= 1800
    assert parse_quant_v6_paging_failure(message) == data
    error = QuantV6HistoricalProviderError(message)
    wire = _failure_wire(ordinal=40, kind=_classify_fetch_failure(error), exc=error)
    assert wire.message == message
    assert parse_quant_v6_paging_failure(str(_rebuild_failure(wire))) == data


def test_helper_failure_cannot_change_existing_success_or_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("token-secret payload-secret")
    monkeypatch.setattr(provider_module, "_describe_boundary", fail)
    provider, context = _provider([[_row(START)], [_row(START)]])
    result = provider.fetch_five_minute_no_adjust("EA.US", start_at=START, end_at=END)
    assert result.bars == (provider_module._coerce_bar(_row(START), START),)
    assert (result.pages, result.raw_rows, result.rejected_rows) == (2, 2, 0)
    assert len(context.calls) == 2
    error, context = _error([[_row(CURSOR)]])
    assert _metadata(error)["diagnostic_status"] == "UNAVAILABLE"
    assert len(context.calls) == 1
    assert "secret" not in str(error)


def test_only_private_bounded_fingerprints_retained() -> None:
    from dataclasses import fields
    from app.services.watchlist_quant_v6_historical_provider import _BoundaryDigest, _remember_boundary

    raw = _row(START)
    digest = _remember_boundary(((raw, START),), START)
    assert digest is not None
    assert [f.name for f in fields(_BoundaryDigest)] == ["descriptor", "fingerprints"]
    assert len(digest.fingerprints) == 5
    assert all(type(value) is str and len(value) == 64 for value in digest.fingerprints)
    assert "9876" not in repr(digest) and "7654321" not in repr(digest)
    error, _ = _error([[_row(START)], [_row(START, close="98762.25")]])
    for fingerprint in digest.fingerprints:
        assert fingerprint is not None and fingerprint not in str(error)
    equivalent = _remember_boundary(((_row(START, open=Decimal("98761.12500")), START),), START)
    assert equivalent is not None and digest.fingerprints == equivalent.fingerprints


@pytest.mark.parametrize("kind", ["unknown_nested", "duplicate_nested", "wrong_bool", "wrong_list", "duplicate_field", "wrong_time", "null_descriptor", "nested_deep", "nonfinite", "escape"])
def test_parser_rejects_nested_and_type_attacks(kind: str) -> None:
    error, _ = _error([[_row(CURSOR)]])
    data = dict(_metadata(error))
    if kind == "unknown_nested":
        data["request"]["secret"] = "token-secret"
    elif kind == "wrong_bool":
        data["comparison"]["raw_match"] = 1
    elif kind == "wrong_list":
        data["comparison"]["changed_fields"] = ["turnover"]
    elif kind == "duplicate_field":
        data["comparison"]["changed_fields"] = ["open", "open"]
    elif kind == "wrong_time":
        data["current_boundary"]["descriptor"]["timestamp_utc"] = "payload-secret"
    elif kind == "null_descriptor":
        data["current_boundary"]["descriptor"] = None
    payload = json.dumps(data, separators=(",", ":"))
    if kind == "duplicate_nested":
        payload = payload.replace('"page_size":1000', '"page_size":1000,"page_size":1000')
    elif kind == "nested_deep":
        payload = "[" * 850 + "]" * 850
    elif kind == "nonfinite":
        payload = payload.replace('"raw_total":1', '"raw_total":Infinity')
    elif kind == "escape":
        payload = payload.replace('"EA.US"', '"EA.US\\u000asecret"')
    assert parse_quant_v6_paging_failure(PREFIX + MARKER + payload) is None


def test_actual_exchange_boundary_is_captured_not_reconstructed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[datetime] = []
    def boundary(symbol: str, timestamp: datetime) -> datetime:
        assert symbol == "EA.US"
        # A fixed-offset stand-in proves metadata uses the actual argument, not
        # a later independent call to the boundary conversion helper.
        result = timestamp.astimezone(timezone(timedelta(hours=-5)))
        calls.append(result)
        return result
    monkeypatch.setattr(provider_module, "_history_boundary", boundary)
    error, context = _error([[_row(CURSOR)]])
    data = _metadata(error)
    assert len(calls) == 1
    assert data["request"]["cursor_exchange"] == context.calls[0][5].isoformat() == calls[0].isoformat()


def test_unparsed_and_duplicate_nonboundary_rows_not_silently_dropped() -> None:
    error, context = _error([[_row(None), _row(), _row(OLD.replace(minute=50)), _row(OLD.replace(minute=50))]])
    data = _metadata(error)
    assert len(context.calls) == 1
    assert data["counts"] == {"page_rows": 4, "raw_total": 4, "rejected_total": 1, "accepted_total": 0,
        "before_cursor": 3, "at_cursor": 0, "after_cursor": 0, "unparsed": 1}
    assert data["current_boundary"]["selection"] == "MAX_UNIQUE"
    assert data["page_time_range"] == {"min_utc": "2026-08-04T19:50:00Z", "max_utc": "2026-08-04T19:55:00Z"}


def test_v4_contract_changes_only_composite_acquisition_spec() -> None:
    from app.domain.watchlist_quant_v6 import QUANT_V6_ACQUISITION_SPEC_DIGEST, quant_v6_payload_sha256
    from app.services.watchlist_quant_v6_evaluation_service import quant_v6_registration_acquisition_spec

    assert provider_module.QUANT_V6_HISTORICAL_PROVIDER_CONTRACT_VERSION == "watchlist-quant-v6-longport-quote-only-history-v4"
    # Domain-only digest is distinct from the registered composite acquisition spec.
    assert QUANT_V6_ACQUISITION_SPEC_DIGEST == "6091b895dd0ddc62e25e5acf27e2c199552da12418d690dc8bcbb80a682305f8"
    assert quant_v6_payload_sha256(quant_v6_registration_acquisition_spec()) == "6259f8bbe2358f61126b21c6f9f561e11804128eefaae9312c66637644f961b3"
    assert not any("diag" in key for key in provider_module.quant_v6_historical_provider_contract())
