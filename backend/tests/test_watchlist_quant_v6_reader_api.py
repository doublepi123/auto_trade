from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Generator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app import database
from app.api.watchlist_quant_v6 import router
from app.config import settings
from app.database import get_db
from app.domain.universe_selection import (
    INDEX_MEMBERSHIP_HISTORY,
    ROTATION_RESEARCH_CANDIDATE_CATALOG,
)
from app.domain.watchlist_quant_v6 import (
    MAX_QUANT_V6_ARTIFACT_RAW_BYTES,
    QUANT_V6_ASSESSMENT_ARTIFACT_KIND,
    QUANT_V6_ASSESSMENT_CONTRACT,
    QUANT_V6_PAYLOAD_SCHEMA_VERSION,
    QuantV6Bar,
    canonical_quant_v6_json,
    canonical_utc_timestamp,
    encode_quant_v6_artifact,
    quant_v6_expected_rth_bar_starts,
    quant_v6_payload_sha256,
)
from app.models import (
    WatchlistQuantV6Artifact,
    WatchlistQuantV6Publication,
    WatchlistQuantV6PublicationArtifact,
)
from app.services import (
    watchlist_quant_v6_evaluation_service as evaluation_service_module,
)
from app.services.watchlist_quant_v6_evaluation_service import (
    QuantV6HistoricalProvider,
    QuantV6RegistrationPlan,
    _build_registration_plan,
    build_latest_quant_v6_registration_plan,
)
from app.services.watchlist_quant_v6_historical_provider import (
    QuantV6HistoricalBarFetch,
)
from app.services.watchlist_quant_v6_publication_service import (
    QuantV6PublicationReceipt,
    WatchlistQuantV6PublicationService,
)
from app.services.watchlist_quant_v6_reader_service import (
    QuantV6ReadIntegrityError,
    _MAX_BINDINGS,
    _validate_provider_contract,
)


_OBSERVED_AT = datetime(2026, 7, 31, 23, 0, tzinfo=timezone.utc)
_PUBLISHED_AT = datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc)
_HISTORICAL_SOURCE_KEYS_V1 = {
    "app.domain.universe_selection.catalog",
    "app.domain.universe_selection.membership_history",
    "app.services.watchlist_quant_v6_evaluation_service",
    "app.services.watchlist_quant_v6_historical_provider",
}
_HISTORICAL_SOURCE_KEYS_V2 = {
    *_HISTORICAL_SOURCE_KEYS_V1,
    "app.services.watchlist_quant_v6_deadline",
}


class _Provider(QuantV6HistoricalProvider):
    def __init__(self, bars: tuple[QuantV6Bar, ...] = ()) -> None:
        self.bars = bars
        self.calls = 0
        self.fail_if_called = False

    def fetch_five_minute_no_adjust(
        self,
        symbol: str,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> QuantV6HistoricalBarFetch:
        del symbol, start_at, end_at
        if self.fail_if_called:
            raise AssertionError("persisted reader called the historical provider")
        self.calls += 1
        page_count = max(1, (len(self.bars) + 999) // 1_000)
        return QuantV6HistoricalBarFetch(
            bars=self.bars,
            pages=page_count,
            raw_rows=len(self.bars),
            rejected_rows=0,
        )


def _bar(
    start_at: datetime,
    index: int,
    *,
    opened: Decimal | str = "100",
    closed: Decimal | str | None = None,
) -> QuantV6Bar:
    open_value = Decimal(opened)
    close_value = (
        Decimal(closed)
        if closed is not None
        else Decimal("100") + Decimal(index % 2) / Decimal("10")
    )
    return QuantV6Bar(
        start_at=start_at,
        open=open_value,
        high=max(open_value, close_value) + Decimal("1"),
        low=min(open_value, close_value) - Decimal("1"),
        close=close_value,
        volume=Decimal("1000"),
    )


def _complete_eventful_bars(
    plan: QuantV6RegistrationPlan,
) -> tuple[QuantV6Bar, ...]:
    bars: list[QuantV6Bar] = []
    training_dates = set(plan.training_session_dates)
    for session_date in (
        *plan.training_session_dates,
        *plan.target_session_dates,
    ):
        starts = quant_v6_expected_rth_bar_starts(plan.market, session_date)
        session_bars = [
            _bar(
                start,
                index,
                closed=(
                    None
                    if session_date in training_dates
                    else Decimal("100") + Decimal(index) / Decimal("1000")
                ),
            )
            for index, start in enumerate(starts)
        ]
        if session_date not in training_dates:
            session_bars[1] = _bar(starts[1], 1, closed="90")
            session_bars[8] = _bar(
                starts[8],
                8,
                opened="102",
                closed="100",
            )
        bars.extend(session_bars)
    return tuple(bars)


def _plan(
    member_count: int,
    *,
    observed_at: datetime = _OBSERVED_AT,
) -> QuantV6RegistrationPlan:
    full = build_latest_quant_v6_registration_plan(
        observed_at=observed_at
    )
    selected_symbols = {
        member.symbol for member in full.members[:member_count]
    }
    candidates = tuple(
        candidate
        for candidate in ROTATION_RESEARCH_CANDIDATE_CATALOG
        if candidate.symbol in selected_symbols
    )
    return _build_registration_plan(
        observed_at=observed_at,
        market="US",
        candidates=candidates,
        membership_history=INDEX_MEMBERSHIP_HISTORY,
    )


def _engine(path: Path) -> Engine:
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_contract(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA recursive_triggers=ON")
        finally:
            cursor.close()

    database._ensure_watchlist_quant_v6_tables(engine)
    return engine


@dataclass
class _ReaderEnvironment:
    engine: Engine
    session_factory: sessionmaker[Session]
    client: TestClient
    provider: _Provider
    plan: QuantV6RegistrationPlan
    receipt: QuantV6PublicationReceipt | None


class _EnvironmentFactory:
    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path
        self._environments: list[_ReaderEnvironment] = []

    def build(
        self,
        *,
        publish: bool = True,
        member_count: int = 2,
        eventful: bool = False,
    ) -> _ReaderEnvironment:
        plan = _plan(member_count)
        provider = _Provider(
            _complete_eventful_bars(plan) if eventful else ()
        )
        engine = _engine(
            self._tmp_path / f"reader-{len(self._environments)}.db"
        )
        factory = sessionmaker(
            bind=engine,
            autoflush=False,
            expire_on_commit=False,
        )
        publication_service = WatchlistQuantV6PublicationService(
            factory,
            clock=lambda: _PUBLISHED_AT,
        )
        receipt: QuantV6PublicationReceipt | None
        if publish:
            receipt = publication_service.register_provider_evaluate_publish(
                plan=plan,
                provider=provider,
            )
        else:
            publication_service.register_plan(plan)
            receipt = None

        api = FastAPI()
        api.include_router(router)

        def override_get_db() -> Generator[Session, None, None]:
            db = factory()
            try:
                yield db
            finally:
                db.close()

        api.dependency_overrides[get_db] = override_get_db
        environment = _ReaderEnvironment(
            engine=engine,
            session_factory=factory,
            client=TestClient(api),
            provider=provider,
            plan=plan,
            receipt=receipt,
        )
        self._environments.append(environment)
        return environment

    def close(self) -> None:
        for environment in self._environments:
            environment.client.close()
            environment.engine.dispose()


@pytest.fixture
def environment_factory(
    tmp_path: Path,
) -> Generator[_EnvironmentFactory, None, None]:
    original_api_key = settings.api_key
    factory = _EnvironmentFactory(tmp_path)
    settings.api_key = ""
    try:
        yield factory
    finally:
        settings.api_key = original_api_key
        factory.close()


def _publication_id(environment: _ReaderEnvironment) -> int:
    assert environment.receipt is not None
    return environment.receipt.publication_id


def _publish_later(
    environment: _ReaderEnvironment,
    *,
    observed_at: datetime,
) -> QuantV6PublicationReceipt:
    service = WatchlistQuantV6PublicationService(
        environment.session_factory,
        clock=lambda: observed_at + timedelta(hours=2),
    )
    return service.register_provider_evaluate_publish(
        plan=_plan(1, observed_at=observed_at),
        provider=_Provider(),
    )


def _assert_select_only(statements: list[str]) -> None:
    assert statements
    assert all(
        statement.lstrip().upper().startswith("SELECT")
        for statement in statements
    )


def _historical_evaluator_manifest_for_reader_test(
    *,
    manifest_version: int,
    source_keys: set[str],
) -> dict[str, object]:
    current = evaluation_service_module.quant_v6_historical_evaluator_manifest()
    current_sources = current["source_sha256"]
    assert isinstance(current_sources, dict)
    source_sha256 = {
        key: current_sources.get(key, "f" * 64)
        for key in source_keys
    }
    return {
        **current,
        "manifest_version": manifest_version,
        "source_sha256": source_sha256,
    }


def _pin_historical_evaluator_manifest(
    monkeypatch: pytest.MonkeyPatch,
    *,
    manifest_version: int,
    source_keys: set[str],
) -> dict[str, object]:
    manifest = _historical_evaluator_manifest_for_reader_test(
        manifest_version=manifest_version,
        source_keys=source_keys,
    )
    digest = quant_v6_payload_sha256(manifest)
    monkeypatch.setattr(
        evaluation_service_module,
        "quant_v6_historical_evaluator_manifest",
        lambda: manifest,
    )
    monkeypatch.setattr(
        evaluation_service_module,
        "quant_v6_historical_evaluator_digest_sha256",
        lambda: digest,
    )
    return manifest


def _binding_manifest_payload(
    binding: WatchlistQuantV6PublicationArtifact,
) -> dict[str, object]:
    return {
        "artifact_kind": binding.artifact_kind,
        "artifact_ordinal": binding.artifact_ordinal,
        "artifact_sha256": binding.artifact_sha256,
        "binding_sha256": binding.binding_sha256,
        "market": binding.market,
        "member_ordinal": binding.member_ordinal,
        "role": binding.role,
        "session_date": (
            binding.session_date.isoformat()
            if binding.session_date is not None
            else None
        ),
        "symbol": binding.symbol,
    }


def _binding_identity(
    binding: WatchlistQuantV6PublicationArtifact,
    *,
    registration_identity_sha256: str,
    artifact_sha256: str | None = None,
    artifact_ordinal: int | None = None,
    session_date: str | None = None,
) -> str:
    payload = _binding_manifest_payload(binding)
    payload.pop("binding_sha256")
    if artifact_sha256 is not None:
        payload["artifact_sha256"] = artifact_sha256
    if artifact_ordinal is not None:
        payload["artifact_ordinal"] = artifact_ordinal
    if session_date is not None:
        payload["session_date"] = session_date
    payload.update({
        "contract": "watchlist-quant-v6-artifact-binding-v1",
        "registration_identity_sha256": registration_identity_sha256,
        "schema_version": 1,
    })
    return quant_v6_payload_sha256(payload)


def _publication_manifest_identity(
    bindings: tuple[WatchlistQuantV6PublicationArtifact, ...],
    *,
    registration_identity_sha256: str,
) -> str:
    role_rank = {"ASSESSMENT": 0, "SESSION_INPUT": 1, "EVENT": 2}
    ordered = sorted(
        bindings,
        key=lambda binding: (
            binding.member_ordinal,
            role_rank[binding.role],
            binding.artifact_ordinal,
        ),
    )
    return quant_v6_payload_sha256({
        "bindings": [
            _binding_manifest_payload(binding) for binding in ordered
        ],
        "contract": "watchlist-quant-v6-binding-manifest-v1",
        "registration_identity_sha256": registration_identity_sha256,
        "schema_version": 1,
    })


def test_all_reader_endpoints_are_persisted_only_and_bounded(
    environment_factory: _EnvironmentFactory,
) -> None:
    environment = environment_factory.build()
    registration_payload = json.loads(environment.plan.registration_json)
    historical_sources = registration_payload["evaluator_manifest"][
        "source_sha256"
    ]
    assert registration_payload["evaluator_manifest"]["manifest_version"] == 2
    assert set(historical_sources) == _HISTORICAL_SOURCE_KEYS_V2
    publication_id = _publication_id(environment)
    environment.provider.fail_if_called = True
    provider_calls = environment.provider.calls
    statements: list[str] = []

    def _capture(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement)

    event.listen(environment.engine, "before_cursor_execute", _capture)
    try:
        response = environment.client.get(
            "/api/watchlist/quant-v6/publications"
        )
        assert response.status_code == 200
        page = response.json()
        assert page["integrity_scope"] == "PERSISTED_HEADERS"
        assert page["total"] == 1
        assert page["items"][0]["publication_id"] == publication_id
        _assert_select_only(statements)
        assert len(statements) <= 3
        assert all("watchlist_quant_v6_artifacts.payload" not in sql for sql in statements)
        assert "LIMIT" in statements[-1].upper()
        assert all("publication_json" not in sql for sql in statements)
        assert all("registration_json" not in sql for sql in statements)

        statements.clear()
        response = environment.client.get(
            f"/api/watchlist/quant-v6/publications/{publication_id}"
        )
        assert response.status_code == 200
        detail = response.json()
        assert detail["validation"] == {
            "integrity_scope": "SELF_CONSISTENT",
            "registration_identity_verified": True,
            "publication_identity_verified": True,
            "binding_manifest_verified": True,
            "artifact_payloads_verified": False,
        }
        assert "registration_payload" not in detail
        assert "publication_payload" not in detail
        assert detail["registration"]["order_submission_allowed"] is False
        _assert_select_only(statements)
        assert len(statements) <= 4
        assert all("watchlist_quant_v6_artifacts.payload" not in sql for sql in statements)
        bounded_counts = [
            sql.upper()
            for sql in statements
            if "COUNT(*)" in sql.upper()
            and "WATCHLIST_QUANT_V6_PUBLICATION_ARTIFACTS" in sql.upper()
        ]
        assert len(bounded_counts) == 1
        assert "LIMIT" in bounded_counts[0]
        assert "ORDER BY" not in bounded_counts[0]
        assert "LIMIT" in statements[-1].upper()

        statements.clear()
        response = environment.client.get(
            f"/api/watchlist/quant-v6/publications/{publication_id}/members"
        )
        assert response.status_code == 200
        members = response.json()
        assert members["integrity_scope"] == "REQUESTED_PAGE"
        assert members["total"] == 2
        assert [item["member_ordinal"] for item in members["items"]] == [0, 1]
        digest = members["items"][0]["assessment_artifact_sha256"]
        _assert_select_only(statements)
        assert len(statements) <= 3
        assert all("watchlist_quant_v6_artifacts.payload" not in sql for sql in statements)

        statements.clear()
        response = environment.client.get(
            f"/api/watchlist/quant-v6/publications/{publication_id}/bindings"
        )
        assert response.status_code == 200
        bindings = response.json()
        assert bindings["integrity_scope"] == "REQUESTED_PAGE"
        assert bindings["total"] == 2
        assert {item["role"] for item in bindings["items"]} == {"ASSESSMENT"}
        _assert_select_only(statements)
        assert len(statements) <= 4
        assert all("watchlist_quant_v6_artifacts.payload" not in sql for sql in statements)

        statements.clear()
        response = environment.client.get(
            f"/api/watchlist/quant-v6/publications/{publication_id}/artifacts/{digest}"
        )
        assert response.status_code == 200
        artifact = response.json()
        assert artifact["integrity_scope"] == "REQUESTED_ARTIFACT"
        assert artifact["kind"] == QUANT_V6_ASSESSMENT_ARTIFACT_KIND
        assert artifact["payload"]["contract"] == QUANT_V6_ASSESSMENT_CONTRACT
        assert artifact["payload_identity_verified"] is True
        _assert_select_only(statements)
        assert len(statements) <= 5
        payload_selects = [
            sql for sql in statements
            if "watchlist_quant_v6_artifacts.payload" in sql
        ]
        assert len(payload_selects) == 2
        assert "length(watchlist_quant_v6_artifacts.payload)" in payload_selects[0]
    finally:
        event.remove(environment.engine, "before_cursor_execute", _capture)

    assert environment.provider.calls == provider_calls


def test_reader_accepts_legacy_v1_historical_evaluator_closure(
    environment_factory: _EnvironmentFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_historical_evaluator_manifest(
        monkeypatch,
        manifest_version=1,
        source_keys=_HISTORICAL_SOURCE_KEYS_V1,
    )
    environment = environment_factory.build(member_count=1)
    registration_payload = json.loads(environment.plan.registration_json)
    evaluator_manifest = registration_payload["evaluator_manifest"]
    assert evaluator_manifest["manifest_version"] == 1
    assert set(evaluator_manifest["source_sha256"]) == (
        _HISTORICAL_SOURCE_KEYS_V1
    )

    response = environment.client.get(
        f"/api/watchlist/quant-v6/publications/{_publication_id(environment)}"
    )

    assert response.status_code == 200
    assert response.json()["validation"]["registration_identity_verified"] is True


@pytest.mark.parametrize(
    ("manifest_version", "source_keys"),
    (
        (1, _HISTORICAL_SOURCE_KEYS_V2),
        (2, _HISTORICAL_SOURCE_KEYS_V1),
        (2, {*_HISTORICAL_SOURCE_KEYS_V2, "app.services.unexpected"}),
    ),
    ids=("v1-new-key-superset", "v2-missing-deadline", "v2-extra-key"),
)
def test_reader_rejects_historical_evaluator_version_closure_mismatch(
    environment_factory: _EnvironmentFactory,
    monkeypatch: pytest.MonkeyPatch,
    manifest_version: int,
    source_keys: set[str],
) -> None:
    _pin_historical_evaluator_manifest(
        monkeypatch,
        manifest_version=manifest_version,
        source_keys=source_keys,
    )
    environment = environment_factory.build(member_count=1)

    response = environment.client.get(
        f"/api/watchlist/quant-v6/publications/{_publication_id(environment)}"
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "persisted quant-v6 evidence failed integrity validation"
    }


def test_registration_only_is_hidden_and_router_requires_api_key(
    environment_factory: _EnvironmentFactory,
) -> None:
    environment = environment_factory.build(publish=False)
    assert environment.provider.calls == 0
    settings.api_key = "reader-key"

    unauthorized = environment.client.get(
        "/api/watchlist/quant-v6/publications"
    )
    assert unauthorized.status_code == 401

    response = environment.client.get(
        "/api/watchlist/quant-v6/publications",
        headers={"X-API-Key": "reader-key"},
    )
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] == 0


def test_database_unavailability_returns_safe_503(
    environment_factory: _EnvironmentFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = environment_factory.build()

    def _unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise OperationalError(
            "SELECT persisted quant-v6 evidence",
            {},
            RuntimeError("database unavailable"),
        )

    monkeypatch.setattr(Session, "execute", _unavailable)
    response = environment.client.get(
        "/api/watchlist/quant-v6/publications"
    )
    assert response.status_code == 503
    assert response.json() == {
        "detail": "persisted quant-v6 evidence is temporarily unavailable"
    }


def test_provider_contract_is_exact_and_strictly_typed() -> None:
    registration = json.loads(_plan(1).registration_json)
    provider_contract = registration["acquisition"]["provider_contract"]
    assert _validate_provider_contract(provider_contract) == provider_contract

    mutations = (
        ("max_pages", True),
        ("quote_context_only", 1),
        ("page_timeout_milliseconds", 4_999),
        ("provider_contract_version", "changed-v1"),
    )
    for key, value in mutations:
        candidate = dict(provider_contract)
        candidate[key] = value
        with pytest.raises(QuantV6ReadIntegrityError):
            _validate_provider_contract(candidate)

    missing_field = dict(provider_contract)
    missing_field.pop("bounded_context_close")
    with pytest.raises(QuantV6ReadIntegrityError):
        _validate_provider_contract(missing_field)


def test_pagination_filters_and_validation_are_deterministic(
    environment_factory: _EnvironmentFactory,
) -> None:
    environment = environment_factory.build()
    publication_id = _publication_id(environment)

    assert environment.client.get(
        "/api/watchlist/quant-v6/publications?market=HK"
    ).json()["items"] == []
    first = environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}/members",
        params={"limit": 1},
    )
    first_cursor = first.json()["next_cursor"]
    assert isinstance(first_cursor, str)
    second = environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}/members",
        params={"limit": 1, "cursor": first_cursor},
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["items"][0]["member_ordinal"] == 0
    assert second.json()["items"][0]["member_ordinal"] == 1

    assessments = environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}/bindings?member_ordinal=1&role=ASSESSMENT"
    )
    assert assessments.status_code == 200
    assert assessments.json()["total"] == 1
    assert assessments.json()["items"][0]["member_ordinal"] == 1

    events = environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}/bindings?role=EVENT"
    )
    assert events.status_code == 200
    assert events.json()["items"] == []

    assert environment.client.get(
        "/api/watchlist/quant-v6/publications?limit=51"
    ).status_code == 422
    assert environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}/artifacts/not-a-digest"
    ).status_code == 422
    assert environment.client.get(
        "/api/watchlist/quant-v6/publications/9223372036854775808"
    ).status_code == 422
    assert environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}/bindings?member_ordinal=1000"
    ).status_code == 422


def test_keyset_cursors_are_snapshot_stable_and_filter_bound(
    environment_factory: _EnvironmentFactory,
) -> None:
    environment = environment_factory.build()
    oldest_id = _publication_id(environment)
    middle = _publish_later(
        environment,
        observed_at=_OBSERVED_AT + timedelta(days=7),
    )

    first = environment.client.get(
        "/api/watchlist/quant-v6/publications",
        params={"limit": 1},
    )
    assert first.status_code == 200
    first_page = first.json()
    assert first_page["total"] == 2
    assert first_page["items"][0]["publication_id"] == middle.publication_id
    publication_cursor = first_page["next_cursor"]
    assert isinstance(publication_cursor, str)

    newest = _publish_later(
        environment,
        observed_at=_OBSERVED_AT + timedelta(days=14),
    )
    second = environment.client.get(
        "/api/watchlist/quant-v6/publications",
        params={"limit": 1, "cursor": publication_cursor},
    )
    assert second.status_code == 200
    second_page = second.json()
    assert second_page["total"] == 2
    assert [item["publication_id"] for item in second_page["items"]] == [
        oldest_id
    ]
    assert second_page["next_cursor"] is None

    fresh = environment.client.get(
        "/api/watchlist/quant-v6/publications",
        params={"limit": 10},
    )
    assert fresh.status_code == 200
    assert [item["publication_id"] for item in fresh.json()["items"]] == [
        newest.publication_id,
        middle.publication_id,
        oldest_id,
    ]
    us_first = environment.client.get(
        "/api/watchlist/quant-v6/publications",
        params={"limit": 1, "market": "US"},
    )
    us_cursor = us_first.json()["next_cursor"]
    assert isinstance(us_cursor, str)
    us_second = environment.client.get(
        "/api/watchlist/quant-v6/publications",
        params={"limit": 1, "market": "US", "cursor": us_cursor},
    )
    assert us_second.status_code == 200
    assert (
        us_second.json()["items"][0]["publication_id"]
        != us_first.json()["items"][0]["publication_id"]
    )

    wrong_filter = environment.client.get(
        "/api/watchlist/quant-v6/publications",
        params={
            "limit": 1,
            "cursor": publication_cursor,
            "market": "US",
        },
    )
    assert wrong_filter.status_code == 422
    assert wrong_filter.json() == {
        "detail": "invalid quant-v6 pagination cursor"
    }

    member_first = environment.client.get(
        f"/api/watchlist/quant-v6/publications/{oldest_id}/members",
        params={"limit": 1},
    )
    member_cursor = member_first.json()["next_cursor"]
    assert isinstance(member_cursor, str)
    member_second = environment.client.get(
        f"/api/watchlist/quant-v6/publications/{oldest_id}/members",
        params={"limit": 1, "cursor": member_cursor},
    )
    assert member_second.status_code == 200
    assert member_second.json()["items"][0]["member_ordinal"] == 1
    assert member_second.json()["next_cursor"] is None

    tamper_index = len(member_cursor) // 2
    replacement = "A" if member_cursor[tamper_index] != "A" else "B"
    tampered = (
        member_cursor[:tamper_index]
        + replacement
        + member_cursor[tamper_index + 1:]
    )
    assert environment.client.get(
        f"/api/watchlist/quant-v6/publications/{oldest_id}/members",
        params={"limit": 1, "cursor": tampered},
    ).status_code == 422
    cursor_raw = base64.urlsafe_b64decode(
        member_cursor + "=" * (-len(member_cursor) % 4)
    )
    cursor_envelope = json.loads(cursor_raw)
    cursor_envelope["v"] = True
    boolean_version_cursor = base64.urlsafe_b64encode(
        canonical_quant_v6_json(cursor_envelope)
    ).decode("ascii").rstrip("=")
    assert environment.client.get(
        f"/api/watchlist/quant-v6/publications/{oldest_id}/members",
        params={"limit": 1, "cursor": boolean_version_cursor},
    ).status_code == 422

    alternate_cursor: str | None = None
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    for character in alphabet:
        candidate = member_cursor[:-1] + character
        if candidate == member_cursor:
            continue
        try:
            candidate_raw = base64.urlsafe_b64decode(
                candidate + "=" * (-len(candidate) % 4)
            )
        except ValueError:
            continue
        if candidate_raw == cursor_raw:
            alternate_cursor = candidate
            break
    assert alternate_cursor is not None
    assert environment.client.get(
        f"/api/watchlist/quant-v6/publications/{oldest_id}/members",
        params={"limit": 1, "cursor": alternate_cursor},
    ).status_code == 422

    other_database = environment_factory.build(member_count=3)
    assert _publication_id(other_database) == oldest_id
    assert environment.receipt is not None
    assert other_database.receipt is not None
    assert (
        environment.receipt.identity_sha256
        != other_database.receipt.identity_sha256
    )
    assert other_database.client.get(
        f"/api/watchlist/quant-v6/publications/{oldest_id}/members",
        params={"limit": 1, "cursor": member_cursor},
    ).status_code == 422
    assert environment.client.get(
        f"/api/watchlist/quant-v6/publications/{middle.publication_id}/members",
        params={"limit": 1, "cursor": member_cursor},
    ).status_code == 422

    binding_first = environment.client.get(
        f"/api/watchlist/quant-v6/publications/{oldest_id}/bindings",
        params={"limit": 1},
    )
    binding_cursor = binding_first.json()["next_cursor"]
    assert isinstance(binding_cursor, str)
    binding_second = environment.client.get(
        f"/api/watchlist/quant-v6/publications/{oldest_id}/bindings",
        params={"limit": 1, "cursor": binding_cursor},
    )
    assert binding_second.status_code == 200
    assert binding_second.json()["items"][0]["member_ordinal"] == 1
    binding_cursor_raw = base64.urlsafe_b64decode(
        binding_cursor + "=" * (-len(binding_cursor) % 4)
    )
    binding_envelope = json.loads(binding_cursor_raw)
    binding_envelope["s"]["m"] = False
    binding_core = {
        "k": binding_envelope["k"],
        "s": binding_envelope["s"],
        "v": binding_envelope["v"],
    }
    binding_envelope["h"] = hashlib.sha256(
        b"watchlist-quant-v6-reader-cursor-v1\0"
        + canonical_quant_v6_json(binding_core)
    ).hexdigest()
    boolean_filter_cursor = base64.urlsafe_b64encode(
        canonical_quant_v6_json(binding_envelope)
    ).decode("ascii").rstrip("=")
    assert environment.client.get(
        f"/api/watchlist/quant-v6/publications/{oldest_id}/bindings",
        params={
            "limit": 1,
            "member_ordinal": 0,
            "cursor": boolean_filter_cursor,
        },
    ).status_code == 422
    assert environment.client.get(
        f"/api/watchlist/quant-v6/publications/{oldest_id}/bindings",
        params={
            "limit": 1,
            "cursor": binding_cursor,
            "role": "EVENT",
        },
    ).status_code == 422
    assert environment.client.get(
        f"/api/watchlist/quant-v6/publications/{oldest_id}/bindings",
        params={"limit": 1, "cursor": member_cursor},
    ).status_code == 422


def test_publication_snapshot_counts_have_a_sql_hard_bound(
    environment_factory: _EnvironmentFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = environment_factory.build()
    _publish_later(
        environment,
        observed_at=_OBSERVED_AT + timedelta(days=7),
    )
    monkeypatch.setattr(
        "app.services.watchlist_quant_v6_reader_service._MAX_PUBLICATIONS",
        2,
    )
    first = environment.client.get(
        "/api/watchlist/quant-v6/publications",
        params={"limit": 1},
    )
    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    assert isinstance(cursor, str)

    monkeypatch.setattr(
        "app.services.watchlist_quant_v6_reader_service._MAX_PUBLICATIONS",
        1,
    )
    for params in (
        {"limit": 1},
        {"limit": 1, "cursor": cursor},
        {"limit": 1, "market": "US"},
    ):
        statements: list[tuple[str, Any]] = []

        def _capture(
            _connection,
            _cursor,
            statement: str,
            parameters: Any,
            _context,
            _executemany,
        ) -> None:
            statements.append((statement, parameters))

        event.listen(environment.engine, "before_cursor_execute", _capture)
        try:
            response = environment.client.get(
                "/api/watchlist/quant-v6/publications",
                params=params,
            )
        finally:
            event.remove(
                environment.engine,
                "before_cursor_execute",
                _capture,
            )

        assert response.status_code == 409
        assert response.json() == {
            "detail": (
                "persisted quant-v6 evidence failed integrity validation"
            )
        }
        _assert_select_only([statement for statement, _ in statements])
        assert len(statements) == 1
        statement, parameters = statements[0]
        normalized = statement.upper()
        subquery_start = normalized.index("FROM (SELECT")
        subquery_end = normalized.index(") AS ANON_1", subquery_start)
        assert (
            subquery_start
            < normalized.index("LIMIT", subquery_start)
            < subquery_end
        )
        assert "ORDER BY" not in normalized
        assert isinstance(parameters, tuple)
        assert parameters[-2:] == (2, 0)


def test_offset_pagination_parameters_are_rejected(
    environment_factory: _EnvironmentFactory,
) -> None:
    environment = environment_factory.build()
    publication_id = _publication_id(environment)
    paths = (
        "/api/watchlist/quant-v6/publications?page=2",
        f"/api/watchlist/quant-v6/publications/{publication_id}/members?page_size=1",
        f"/api/watchlist/quant-v6/publications/{publication_id}/bindings?page=2",
    )
    for path in paths:
        response = environment.client.get(path)
        assert response.status_code == 422
        assert response.json() == {
            "detail": "offset pagination is not supported"
        }
    long_legacy = environment.client.get(
        "/api/watchlist/quant-v6/publications",
        params={"page": "9" * 513},
    )
    assert long_legacy.status_code == 422
    assert long_legacy.json() == {
        "detail": "offset pagination is not supported"
    }

    cursor_paths = (
        "/api/watchlist/quant-v6/publications",
        f"/api/watchlist/quant-v6/publications/{publication_id}/members",
        f"/api/watchlist/quant-v6/publications/{publication_id}/bindings",
    )
    for path in cursor_paths:
        for invalid_cursor in ("", "a" * 513):
            response = environment.client.get(
                path,
                params={"cursor": invalid_cursor},
            )
            assert response.status_code == 422
            assert response.json() == {
                "detail": "invalid quant-v6 pagination cursor"
            }


def test_manifest_replay_uses_semantic_role_order(
    environment_factory: _EnvironmentFactory,
) -> None:
    environment = environment_factory.build(
        member_count=1,
        eventful=True,
    )
    publication_id = _publication_id(environment)

    detail = environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}"
    )
    assert detail.status_code == 200
    bindings = environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}/bindings?limit=500"
    )
    assert bindings.status_code == 200
    roles = [item["role"] for item in bindings.json()["items"]]
    rank = {"ASSESSMENT": 0, "SESSION_INPUT": 1, "EVENT": 2}
    assert set(roles) == set(rank)
    assert [rank[role] for role in roles] == sorted(rank[role] for role in roles)
    digest_by_role: dict[str, str] = {}
    for item in bindings.json()["items"]:
        digest_by_role.setdefault(item["role"], item["artifact_sha256"])
    assert set(digest_by_role) == set(rank)
    for digest in digest_by_role.values():
        artifact = environment.client.get(
            f"/api/watchlist/quant-v6/publications/{publication_id}"
            f"/artifacts/{digest}"
        )
        assert artifact.status_code == 200
        assert artifact.json()["binding_count"] == 1

    assert environment.receipt is not None
    with environment.session_factory() as db:
        binding = db.scalars(
            select(WatchlistQuantV6PublicationArtifact)
            .where(
                WatchlistQuantV6PublicationArtifact.publication_id
                == publication_id,
                WatchlistQuantV6PublicationArtifact.role == "SESSION_INPUT",
            )
            .order_by(WatchlistQuantV6PublicationArtifact.artifact_ordinal)
        ).first()
        assert binding is not None
        wrong_session_date = environment.plan.target_session_dates[-1]
        assert wrong_session_date != binding.session_date
        replacement_digest = quant_v6_payload_sha256({
            "artifact_kind": binding.artifact_kind,
            "artifact_ordinal": binding.artifact_ordinal,
            "artifact_sha256": binding.artifact_sha256,
            "contract": "watchlist-quant-v6-artifact-binding-v1",
            "market": binding.market,
            "member_ordinal": binding.member_ordinal,
            "registration_identity_sha256": (
                environment.receipt.registration_identity_sha256
            ),
            "role": binding.role,
            "schema_version": 1,
            "session_date": wrong_session_date.isoformat(),
            "symbol": binding.symbol,
        })
        binding_key = (
            binding.member_ordinal,
            binding.role,
            binding.artifact_ordinal,
        )
    with environment.engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP TRIGGER trg_watchlist_quant_v6_publication_artifacts_no_update"
        )
        connection.execute(
            text(
                "UPDATE watchlist_quant_v6_publication_artifacts "
                "SET session_date = :session_date, binding_sha256 = :digest "
                "WHERE publication_id = :publication_id "
                "AND member_ordinal = :member_ordinal "
                "AND role = :role AND artifact_ordinal = :artifact_ordinal"
            ),
            {
                "session_date": wrong_session_date,
                "digest": replacement_digest,
                "publication_id": publication_id,
                "member_ordinal": binding_key[0],
                "role": binding_key[1],
                "artifact_ordinal": binding_key[2],
            },
        )
    assert environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}/bindings",
        params={"role": "SESSION_INPUT"},
    ).status_code == 409


def test_event_manifest_closes_ordered_covered_sessions(
    environment_factory: _EnvironmentFactory,
) -> None:
    environment = environment_factory.build(
        member_count=1,
        eventful=True,
    )
    publication_id = _publication_id(environment)
    assert environment.receipt is not None
    registration_identity = (
        environment.receipt.registration_identity_sha256
    )
    with environment.session_factory() as db:
        event_bindings = tuple(db.scalars(
            select(WatchlistQuantV6PublicationArtifact)
            .where(
                WatchlistQuantV6PublicationArtifact.publication_id
                == publication_id,
                WatchlistQuantV6PublicationArtifact.role == "EVENT",
            )
            .order_by(
                WatchlistQuantV6PublicationArtifact.artifact_ordinal
            )
        ))
        assert len(event_bindings) > 1
        first_event = event_bindings[0]
        wrong_session_date = environment.plan.target_session_dates[-1]
        assert first_event.session_date != wrong_session_date
        assert event_bindings[1].session_date is not None
        assert event_bindings[1].session_date < wrong_session_date
        replacement_binding_identity = _binding_identity(
            first_event,
            registration_identity_sha256=registration_identity,
            session_date=wrong_session_date.isoformat(),
        )
        binding_key = (
            first_event.member_ordinal,
            first_event.role,
            first_event.artifact_ordinal,
        )

    with environment.engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP TRIGGER "
            "trg_watchlist_quant_v6_publication_artifacts_no_update"
        )
        connection.execute(
            text(
                "UPDATE watchlist_quant_v6_publication_artifacts "
                "SET session_date = :session_date, "
                "binding_sha256 = :binding_identity "
                "WHERE publication_id = :publication_id "
                "AND member_ordinal = :member_ordinal "
                "AND role = :role AND artifact_ordinal = :artifact_ordinal"
            ),
            {
                "session_date": wrong_session_date,
                "binding_identity": replacement_binding_identity,
                "publication_id": publication_id,
                "member_ordinal": binding_key[0],
                "role": binding_key[1],
                "artifact_ordinal": binding_key[2],
            },
        )

    with environment.session_factory() as db:
        bindings = tuple(db.scalars(
            select(WatchlistQuantV6PublicationArtifact).where(
                WatchlistQuantV6PublicationArtifact.publication_id
                == publication_id
            )
        ))
        manifest_identity = _publication_manifest_identity(
            bindings,
            registration_identity_sha256=registration_identity,
        )
        publication = db.get(WatchlistQuantV6Publication, publication_id)
        assert publication is not None
        publication_payload = json.loads(publication.publication_json)
    publication_payload["manifest_sha256"] = manifest_identity
    canonical_publication = canonical_quant_v6_json(publication_payload)
    with environment.engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP TRIGGER trg_watchlist_quant_v6_publications_no_update"
        )
        connection.execute(
            text(
                "UPDATE watchlist_quant_v6_publications "
                "SET manifest_sha256 = :manifest_identity, "
                "publication_json = :payload, identity_sha256 = :identity "
                "WHERE id = :publication_id"
            ),
            {
                "manifest_identity": manifest_identity,
                "payload": canonical_publication.decode("utf-8"),
                "identity": hashlib.sha256(canonical_publication).hexdigest(),
                "publication_id": publication_id,
            },
        )

    response = environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}"
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": "persisted quant-v6 evidence failed integrity validation"
    }


def test_artifact_payload_identity_rejects_cross_member_rebinding(
    environment_factory: _EnvironmentFactory,
) -> None:
    environment = environment_factory.build(member_count=2)
    publication_id = _publication_id(environment)
    assert environment.receipt is not None
    registration_identity = (
        environment.receipt.registration_identity_sha256
    )
    with environment.session_factory() as db:
        assessments = tuple(db.scalars(
            select(WatchlistQuantV6PublicationArtifact)
            .where(
                WatchlistQuantV6PublicationArtifact.publication_id
                == publication_id,
                WatchlistQuantV6PublicationArtifact.role == "ASSESSMENT",
            )
            .order_by(
                WatchlistQuantV6PublicationArtifact.member_ordinal
            )
        ))
        assert len(assessments) == 2
        first, second = assessments
        assert first.artifact_sha256 != second.artifact_sha256
        first_key = (
            first.member_ordinal,
            first.role,
            first.artifact_ordinal,
        )
        second_key = (
            second.member_ordinal,
            second.role,
            second.artifact_ordinal,
        )
        first_digest = first.artifact_sha256
        second_digest = second.artifact_sha256
        swapped_first_identity = _binding_identity(
            first,
            registration_identity_sha256=registration_identity,
            artifact_sha256=second_digest,
        )
        swapped_second_identity = _binding_identity(
            second,
            registration_identity_sha256=registration_identity,
            artifact_sha256=first_digest,
        )
        duplicated_second_identity = _binding_identity(
            second,
            registration_identity_sha256=registration_identity,
            artifact_sha256=second_digest,
        )

    with environment.engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP TRIGGER "
            "trg_watchlist_quant_v6_publication_artifacts_no_update"
        )
        connection.exec_driver_sql(
            "DROP TRIGGER trg_watchlist_quant_v6_publications_no_update"
        )

    def _update_binding(
        key: tuple[int, str, int],
        *,
        digest: str,
        binding_identity: str,
    ) -> None:
        with environment.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE watchlist_quant_v6_publication_artifacts "
                    "SET artifact_sha256 = :digest, "
                    "binding_sha256 = :binding_identity "
                    "WHERE publication_id = :publication_id "
                    "AND member_ordinal = :member_ordinal "
                    "AND role = :role AND artifact_ordinal = :artifact_ordinal"
                ),
                {
                    "digest": digest,
                    "binding_identity": binding_identity,
                    "publication_id": publication_id,
                    "member_ordinal": key[0],
                    "role": key[1],
                    "artifact_ordinal": key[2],
                },
            )

    def _refresh_publication_manifest() -> None:
        with environment.session_factory() as db:
            bindings = tuple(db.scalars(
                select(WatchlistQuantV6PublicationArtifact).where(
                    WatchlistQuantV6PublicationArtifact.publication_id
                    == publication_id
                )
            ))
            manifest_identity = _publication_manifest_identity(
                bindings,
                registration_identity_sha256=registration_identity,
            )
            publication = db.get(
                WatchlistQuantV6Publication,
                publication_id,
            )
            assert publication is not None
            payload = json.loads(publication.publication_json)
        payload["manifest_sha256"] = manifest_identity
        canonical = canonical_quant_v6_json(payload)
        with environment.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE watchlist_quant_v6_publications "
                    "SET manifest_sha256 = :manifest_identity, "
                    "publication_json = :payload, identity_sha256 = :identity "
                    "WHERE id = :publication_id"
                ),
                {
                    "manifest_identity": manifest_identity,
                    "payload": canonical.decode("utf-8"),
                    "identity": hashlib.sha256(canonical).hexdigest(),
                    "publication_id": publication_id,
                },
            )

    _update_binding(
        first_key,
        digest=second_digest,
        binding_identity=swapped_first_identity,
    )
    _update_binding(
        second_key,
        digest=first_digest,
        binding_identity=swapped_second_identity,
    )
    _refresh_publication_manifest()

    detail = environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}"
    )
    assert detail.status_code == 200
    assert detail.json()["validation"]["artifact_payloads_verified"] is False
    for digest in (first_digest, second_digest):
        artifact = environment.client.get(
            f"/api/watchlist/quant-v6/publications/{publication_id}"
            f"/artifacts/{digest}"
        )
        assert artifact.status_code == 409
        assert artifact.json() == {
            "detail": (
                "persisted quant-v6 evidence failed integrity validation"
            )
        }

    _update_binding(
        second_key,
        digest=second_digest,
        binding_identity=duplicated_second_identity,
    )
    _refresh_publication_manifest()
    assert environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}"
    ).status_code == 409
    assert environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}"
        f"/artifacts/{second_digest}"
    ).status_code == 409


def test_unknown_and_unbound_artifacts_share_404(
    environment_factory: _EnvironmentFactory,
) -> None:
    environment = environment_factory.build()
    publication_id = _publication_id(environment)
    unbound = encode_quant_v6_artifact(
        {
            "contract": QUANT_V6_ASSESSMENT_CONTRACT,
            "schema_version": QUANT_V6_PAYLOAD_SCHEMA_VERSION,
            "unbound": True,
        },
        kind=QUANT_V6_ASSESSMENT_ARTIFACT_KIND,
    )
    with environment.session_factory() as db:
        db.add(WatchlistQuantV6Artifact(
            digest_sha256=unbound.digest_sha256,
            schema_version=unbound.schema_version,
            kind=unbound.kind,
            codec=unbound.codec,
            compression_level=9,
            raw_size=unbound.raw_size,
            compressed_size=unbound.compressed_size,
            payload=unbound.payload,
            created_at=_PUBLISHED_AT,
        ))
        db.commit()

    missing_digest = "f" * 64
    missing = environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}/artifacts/{missing_digest}"
    )
    unbound_response = environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}/artifacts/{unbound.digest_sha256}"
    )
    assert missing.status_code == unbound_response.status_code == 404
    assert missing.json() == unbound_response.json()
    assert environment.client.get(
        "/api/watchlist/quant-v6/publications/999999"
    ).status_code == 404


def test_oversized_root_json_fails_before_value_load(
    environment_factory: _EnvironmentFactory,
) -> None:
    environment = environment_factory.build()
    publication_id = _publication_id(environment)
    oversized = json.dumps(
        {"padding": "x" * MAX_QUANT_V6_ARTIFACT_RAW_BYTES},
        separators=(",", ":"),
    )
    with environment.engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP TRIGGER trg_watchlist_quant_v6_publications_no_update"
        )
        connection.execute(
            text(
                "UPDATE watchlist_quant_v6_publications "
                "SET publication_json = :payload WHERE id = :id"
            ),
            {"payload": oversized, "id": publication_id},
        )

    statements: list[str] = []

    def _capture(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement)

    event.listen(environment.engine, "before_cursor_execute", _capture)
    try:
        response = environment.client.get(
            f"/api/watchlist/quant-v6/publications/{publication_id}"
        )
    finally:
        event.remove(environment.engine, "before_cursor_execute", _capture)
    assert response.status_code == 409
    assert response.json() == {
        "detail": "persisted quant-v6 evidence failed integrity validation"
    }
    assert len(statements) == 1
    assert "CAST(watchlist_quant_v6_publications.publication_json AS BLOB)" in statements[0]


def test_persisted_integer_contract_and_acquisition_bounds_fail_closed(
    environment_factory: _EnvironmentFactory,
) -> None:
    environment = environment_factory.build(member_count=1)
    publication_id = _publication_id(environment)
    with environment.session_factory() as db:
        publication = db.get(WatchlistQuantV6Publication, publication_id)
        assert publication is not None
        baseline = json.loads(publication.publication_json)
    assert baseline["schema_version"] == 1
    assert baseline["registered_member_count"] == 1
    assert baseline["artifact_counts"] == {
        "assessment": 1,
        "binding": 1,
        "event": 0,
        "session_input": 0,
    }
    with environment.engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP TRIGGER trg_watchlist_quant_v6_publications_no_update"
        )

    candidates: list[dict[str, Any]] = []
    for key, value in (
        ("schema_version", True),
        ("registered_member_count", True),
    ):
        candidate = json.loads(json.dumps(baseline))
        candidate[key] = value
        candidates.append(candidate)
    for key, value in (
        ("assessment", True),
        ("binding", True),
        ("event", False),
        ("session_input", False),
    ):
        candidate = json.loads(json.dumps(baseline))
        candidate["artifact_counts"][key] = value
        candidates.append(candidate)

    too_many_pages = json.loads(json.dumps(baseline))
    too_many_pages["acquisition_outcome"]["members"][0]["pages"] = 17
    candidates.append(too_many_pages)

    too_many_rows = json.loads(json.dumps(baseline))
    too_many_rows["acquisition_outcome"]["members"][0]["raw_rows"] = 1_001
    candidates.append(too_many_rows)

    too_many_bars = json.loads(json.dumps(baseline))
    acquisition = too_many_bars["acquisition_outcome"]["members"][0]
    acquisition.update({
        "accepted_bars": 10_001,
        "off_grid_accepted_bars": 10_001,
        "pages": 16,
        "raw_rows": 10_001,
    })
    candidates.append(too_many_bars)

    for candidate in candidates:
        canonical = canonical_quant_v6_json(candidate)
        with environment.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE watchlist_quant_v6_publications "
                    "SET publication_json = :payload, "
                    "identity_sha256 = :identity WHERE id = :id"
                ),
                {
                    "payload": canonical.decode("utf-8"),
                    "identity": hashlib.sha256(canonical).hexdigest(),
                    "id": publication_id,
                },
            )
        response = environment.client.get(
            f"/api/watchlist/quant-v6/publications/{publication_id}"
        )
        assert response.status_code == 409
        assert response.json() == {
            "detail": (
                "persisted quant-v6 evidence failed integrity validation"
            )
        }


def test_binding_and_missing_row_corruption_fail_closed(
    environment_factory: _EnvironmentFactory,
) -> None:
    environment = environment_factory.build()
    publication_id = _publication_id(environment)
    with environment.engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP TRIGGER trg_watchlist_quant_v6_publication_artifacts_no_update"
        )
        connection.execute(
            text(
                "UPDATE watchlist_quant_v6_publication_artifacts "
                "SET binding_sha256 = :digest "
                "WHERE publication_id = :publication_id "
                "AND member_ordinal = 0"
            ),
            {"digest": "0" * 64, "publication_id": publication_id},
        )

    assert environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}"
    ).status_code == 409
    assert environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}/bindings"
    ).status_code == 409

    second = environment_factory.build()
    second_id = _publication_id(second)
    with second.engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP TRIGGER trg_watchlist_quant_v6_publication_artifacts_no_delete"
        )
        connection.execute(
            text(
                "DELETE FROM watchlist_quant_v6_publication_artifacts "
                "WHERE publication_id = :publication_id "
                "AND member_ordinal = 0"
            ),
            {"publication_id": second_id},
        )
    assert second.client.get(
        f"/api/watchlist/quant-v6/publications/{second_id}"
    ).status_code == 409


def test_orphan_binding_fails_detail_without_reading_artifact_payload(
    environment_factory: _EnvironmentFactory,
) -> None:
    environment = environment_factory.build()
    publication_id = _publication_id(environment)
    with environment.engine.connect() as connection:
        digest = connection.scalar(
            text(
                "SELECT artifact_sha256 "
                "FROM watchlist_quant_v6_publication_artifacts "
                "WHERE publication_id = :publication_id "
                "ORDER BY member_ordinal, role, artifact_ordinal LIMIT 1"
            ),
            {"publication_id": publication_id},
        )
        assert isinstance(digest, str)
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql(
            "DROP TRIGGER trg_watchlist_quant_v6_artifacts_no_delete"
        )
        connection.execute(
            text(
                "DELETE FROM watchlist_quant_v6_artifacts "
                "WHERE digest_sha256 = :digest"
            ),
            {"digest": digest},
        )
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    statements: list[str] = []

    def _capture(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement)

    event.listen(environment.engine, "before_cursor_execute", _capture)
    try:
        response = environment.client.get(
            f"/api/watchlist/quant-v6/publications/{publication_id}"
        )
    finally:
        event.remove(environment.engine, "before_cursor_execute", _capture)

    assert response.status_code == 409
    assert response.json() == {
        "detail": "persisted quant-v6 evidence failed integrity validation"
    }
    _assert_select_only(statements)
    assert len(statements) <= 4
    assert all(
        "watchlist_quant_v6_artifacts.payload" not in statement
        for statement in statements
    )


def test_member_assessment_projection_is_sql_bounded(
    environment_factory: _EnvironmentFactory,
) -> None:
    environment = environment_factory.build(member_count=1)
    publication_id = _publication_id(environment)
    with environment.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        for artifact_ordinal in (1, 2, 3):
            connection.execute(
                text(
                    "INSERT INTO watchlist_quant_v6_publication_artifacts ("
                    "publication_id, member_ordinal, symbol, market, role, "
                    "artifact_ordinal, session_date, artifact_sha256, "
                    "artifact_kind, binding_sha256, created_at) "
                    "SELECT publication_id, member_ordinal, symbol, market, "
                    "role, :artifact_ordinal, session_date, artifact_sha256, "
                    "artifact_kind, :binding, created_at "
                    "FROM watchlist_quant_v6_publication_artifacts "
                    "WHERE publication_id = :publication_id "
                    "AND member_ordinal = 0 AND role = 'ASSESSMENT' "
                    "AND artifact_ordinal = 0"
                ),
                {
                    "artifact_ordinal": artifact_ordinal,
                    "binding": hashlib.sha256(
                        f"corrupt-{artifact_ordinal}".encode("ascii")
                    ).hexdigest(),
                    "publication_id": publication_id,
                },
            )
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")

    detail_statements: list[tuple[str, Any]] = []

    def _capture_detail(
        _connection,
        _cursor,
        statement: str,
        parameters: Any,
        _context,
        _executemany,
    ) -> None:
        detail_statements.append((statement, parameters))

    event.listen(
        environment.engine,
        "before_cursor_execute",
        _capture_detail,
    )
    try:
        detail = environment.client.get(
            f"/api/watchlist/quant-v6/publications/{publication_id}"
        )
    finally:
        event.remove(
            environment.engine,
            "before_cursor_execute",
            _capture_detail,
        )

    assert detail.status_code == 409
    _assert_select_only([
        statement for statement, _ in detail_statements
    ])
    assert len(detail_statements) == 3
    bounded_counts = [
        (statement, parameters)
        for statement, parameters in detail_statements
        if "COUNT(*)" in statement.upper()
        and "WATCHLIST_QUANT_V6_PUBLICATION_ARTIFACTS" in statement.upper()
    ]
    assert len(bounded_counts) == 1
    count_statement, count_parameters = bounded_counts[0]
    assert "FROM (SELECT" in count_statement.upper()
    assert "LIMIT" in count_statement.upper()
    assert "ORDER BY" not in count_statement.upper()
    assert isinstance(count_parameters, tuple)
    assert count_parameters[-2:] == (_MAX_BINDINGS + 1, 0)
    assert all(
        "CASE" not in statement.upper()
        and "ORDER BY" not in statement.upper()
        for statement, _ in detail_statements
    )

    member_statements: list[tuple[str, Any]] = []

    def _capture_members(
        _connection,
        _cursor,
        statement: str,
        parameters: Any,
        _context,
        _executemany,
    ) -> None:
        member_statements.append((statement, parameters))

    event.listen(
        environment.engine,
        "before_cursor_execute",
        _capture_members,
    )
    try:
        response = environment.client.get(
            f"/api/watchlist/quant-v6/publications/{publication_id}/members",
            params={"limit": 1},
        )
    finally:
        event.remove(
            environment.engine,
            "before_cursor_execute",
            _capture_members,
        )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "persisted quant-v6 evidence failed integrity validation"
    }
    _assert_select_only([
        statement for statement, _ in member_statements
    ])
    assessment_selects = [
        (statement, parameters)
        for statement, parameters in member_statements
        if "WATCHLIST_QUANT_V6_ARTIFACTS.SCHEMA_VERSION"
        in statement.upper()
    ]
    assert len(assessment_selects) == 1
    assessment_statement, assessment_parameters = assessment_selects[0]
    assert "LIMIT" in assessment_statement.upper()
    assert isinstance(assessment_parameters, tuple)
    assert assessment_parameters[-2:] == (2, 0)


def test_malformed_orm_date_returns_safe_integrity_error(
    environment_factory: _EnvironmentFactory,
) -> None:
    environment = environment_factory.build()
    publication_id = _publication_id(environment)
    assert environment.receipt is not None
    with environment.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.exec_driver_sql(
            "DROP TRIGGER trg_watchlist_quant_v6_registrations_no_update"
        )
        connection.execute(
            text(
                "UPDATE watchlist_quant_v6_registrations "
                "SET first_target_session_date = :malformed "
                "WHERE id = :registration_id"
            ),
            {
                "malformed": b"not-a-date",
                "registration_id": environment.receipt.registration_id,
            },
        )
        connection.commit()
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")

    response = environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}"
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": "persisted quant-v6 evidence failed integrity validation"
    }


def test_self_consistency_replays_acquisition_coverage(
    environment_factory: _EnvironmentFactory,
) -> None:
    environment = environment_factory.build()
    publication_id = _publication_id(environment)
    with environment.session_factory() as db:
        publication = db.get(WatchlistQuantV6Publication, publication_id)
        assert publication is not None
        payload = json.loads(publication.publication_json)
    assert environment.receipt is not None
    starts = tuple(
        start_at
        for session_date in (
            *environment.plan.training_session_dates,
            *environment.plan.target_session_dates,
        )
        for start_at in quant_v6_expected_rth_bar_starts(
            environment.plan.market,
            session_date,
        )
    )
    coverage_bytes = bytearray((len(starts) + 7) // 8)
    for index in range(len(starts)):
        coverage_bytes[index // 8] |= 1 << (7 - (index % 8))

    member = payload["acquisition_outcome"]["members"][0]
    member["accepted_bars"] = len(starts)
    member["complete_session_count"] = 40
    member["off_grid_accepted_bars"] = 0
    member["raw_rows"] = len(starts)
    member["rejected_rows"] = 0
    member["scheduled_grid_coverage_bitset_hex"] = coverage_bytes.hex()
    member["scheduled_grid_present_bars"] = len(starts)
    accepted_digest = hashlib.sha256()
    for value in (
        "watchlist-quant-v6-accepted-bar-starts-digest-v1",
        environment.receipt.registration_identity_sha256,
        member["market"],
        member["symbol"],
        *(canonical_utc_timestamp(start_at) for start_at in starts),
    ):
        accepted_digest.update(value.encode("ascii"))
        accepted_digest.update(b"\n")
    member["accepted_bar_starts_sha256"] = accepted_digest.hexdigest()
    present_digest = hashlib.sha256()
    for value in (
        "watchlist-quant-v6-scheduled-grid-starts-digest-v1",
        environment.receipt.registration_identity_sha256,
        *(canonical_utc_timestamp(start_at) for start_at in starts),
    ):
        present_digest.update(value.encode("ascii"))
        present_digest.update(b"\n")
    member["scheduled_grid_present_starts_sha256"] = (
        present_digest.hexdigest()
    )
    canonical = canonical_quant_v6_json(payload)
    with environment.engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP TRIGGER trg_watchlist_quant_v6_publications_no_update"
        )
        connection.execute(
            text(
                "UPDATE watchlist_quant_v6_publications "
                "SET publication_json = :payload, identity_sha256 = :identity "
                "WHERE id = :id"
            ),
            {
                "payload": canonical.decode("utf-8"),
                "identity": hashlib.sha256(canonical).hexdigest(),
                "id": publication_id,
            },
        )

    assert environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}"
    ).status_code == 409
    assert environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}/members"
    ).status_code == 409


def test_corrupt_artifact_payload_is_409_without_fallback(
    environment_factory: _EnvironmentFactory,
) -> None:
    environment = environment_factory.build()
    publication_id = _publication_id(environment)
    environment.provider.fail_if_called = True
    provider_calls = environment.provider.calls
    with environment.session_factory() as db:
        binding = db.scalar(
            select(WatchlistQuantV6PublicationArtifact).where(
                WatchlistQuantV6PublicationArtifact.publication_id
                == publication_id,
                WatchlistQuantV6PublicationArtifact.member_ordinal == 0,
            )
        )
        assert binding is not None
        digest = binding.artifact_sha256
        artifact = db.get(WatchlistQuantV6Artifact, digest)
        assert artifact is not None
        compressed_size = artifact.compressed_size

    with environment.engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP TRIGGER trg_watchlist_quant_v6_artifacts_no_update"
        )
        connection.execute(
            text(
                "UPDATE watchlist_quant_v6_artifacts "
                "SET payload = :payload WHERE digest_sha256 = :digest"
            ),
            {"payload": bytes(compressed_size), "digest": digest},
        )

    response = environment.client.get(
        f"/api/watchlist/quant-v6/publications/{publication_id}/artifacts/{digest}"
    )
    assert response.status_code == 409
    assert environment.provider.calls == provider_calls


def test_main_mounts_only_get_quant_v6_reader_routes() -> None:
    from app.main import app

    schema = app.openapi()
    paths = {
        path: methods
        for path, methods in schema["paths"].items()
        if path.startswith("/api/watchlist/quant-v6")
    }
    assert set(paths) == {
        "/api/watchlist/quant-v6/publications",
        "/api/watchlist/quant-v6/publications/{publication_id}",
        "/api/watchlist/quant-v6/publications/{publication_id}/members",
        "/api/watchlist/quant-v6/publications/{publication_id}/bindings",
        "/api/watchlist/quant-v6/publications/{publication_id}/artifacts/{digest_sha256}",
    }
    assert all(set(methods) == {"get"} for methods in paths.values())
