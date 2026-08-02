from __future__ import annotations

import inspect
import hashlib
import json
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from functools import cache
from typing import Any

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app import database
from app.domain.universe_selection import (
    INDEX_MEMBERSHIP_HISTORY,
    ROTATION_RESEARCH_CANDIDATE_CATALOG,
)
from app.domain.watchlist_quant_v6 import (
    MAX_QUANT_V6_ARTIFACT_RAW_BYTES,
    QUANT_V6_SEMANTIC_DIGEST,
    QuantV6Bar,
    canonical_quant_v6_json,
    decode_quant_v6_artifact,
    encode_quant_v6_artifact,
    quant_v6_expected_rth_bar_starts,
    quant_v6_payload_sha256,
)
from app.models import (
    WatchlistQuantV6Artifact,
    WatchlistQuantV6Publication,
    WatchlistQuantV6PublicationArtifact,
    WatchlistQuantV6Registration,
)
from app.services import watchlist_quant_v6_publication_service as service_module
from app.services.watchlist_quant_v6_evaluation_service import (
    ASSESSMENT_ROLE,
    EVENT_ROLE,
    SESSION_INPUT_ROLE,
    QuantV6CandidateEvaluation,
    QuantV6PendingArtifactBinding,
    QuantV6RegistrationPlan,
    _build_registration_plan,
    build_latest_quant_v6_registration_plan,
    evaluate_quant_v6_registration,
)
from app.services.watchlist_quant_v6_historical_provider import (
    QuantV6HistoricalBarFetch,
)
from app.services.watchlist_quant_v6_deadline import (
    QuantV6EvaluationCancelledError,
    QuantV6EvaluationDeadline,
    QuantV6EvaluationDeadlineExceededError,
)
from app.services.watchlist_quant_v6_publication_service import (
    QuantV6PublicationConflictError,
    QuantV6PublicationError,
    QuantV6PublicationReceipt,
    WatchlistQuantV6PublicationService,
    _binding_preimage,
    _registration_fields,
)


_NOW = datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc)


class _Provider:
    def __init__(
        self,
        bars: tuple[QuantV6Bar, ...] = (),
        *,
        pages: int = 1,
        raw_rows: int | None = None,
        rejected_rows: int = 0,
    ) -> None:
        self.calls = 0
        self.bars = bars
        self.pages = pages
        self.raw_rows = len(bars) if raw_rows is None else raw_rows
        self.rejected_rows = rejected_rows

    def fetch_five_minute_no_adjust(
        self,
        symbol: str,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> QuantV6HistoricalBarFetch:
        del symbol, start_at, end_at
        self.calls += 1
        return QuantV6HistoricalBarFetch(
            bars=self.bars,
            pages=self.pages,
            raw_rows=self.raw_rows,
            rejected_rows=self.rejected_rows,
        )


def _engine(tmp_path) -> Engine:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'quant-v6-publication.db'}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_contract(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    database._ensure_watchlist_quant_v6_tables(engine)
    return engine


def _factory(engine: Engine) -> Callable[[], Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@cache
def _one_member_plan():
    observed = datetime(2026, 7, 31, 23, 0, tzinfo=timezone.utc)
    full_plan = build_latest_quant_v6_registration_plan(observed_at=observed)
    selected = next(
        candidate
        for candidate in ROTATION_RESEARCH_CANDIDATE_CATALOG
        if candidate.symbol == full_plan.members[0].symbol
    )
    return _build_registration_plan(
        observed_at=observed,
        market="US",
        candidates=(selected,),
        membership_history=INDEX_MEMBERSHIP_HISTORY,
    )


@cache
def _missing_evaluations() -> tuple[QuantV6CandidateEvaluation, ...]:
    plan = _one_member_plan()
    return evaluate_quant_v6_registration(
        registration=plan,
        provider=_Provider(),
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


def _complete_bars(*, eventful: bool) -> tuple[QuantV6Bar, ...]:
    plan = _one_member_plan()
    values: list[QuantV6Bar] = []
    training_dates = set(plan.training_session_dates)
    for session_date in (
        *plan.training_session_dates,
        *plan.target_session_dates,
    ):
        starts = quant_v6_expected_rth_bar_starts("US", session_date)
        bars = [
            _bar(
                start,
                index,
                closed=(
                    None
                    if session_date in training_dates
                    else Decimal("100")
                    + Decimal(index) / Decimal("1000")
                ),
            )
            for index, start in enumerate(starts)
        ]
        if eventful and session_date not in training_dates:
            bars[1] = _bar(starts[1], 1, closed="90")
            bars[8] = _bar(starts[8], 8, opened="102", closed="100")
        values.extend(bars)
    return tuple(values)


@cache
def _covered_evaluations(
    eventful: bool = False,
) -> tuple[QuantV6CandidateEvaluation, ...]:
    plan = _one_member_plan()
    return evaluate_quant_v6_registration(
        registration=plan,
        provider=_Provider(_complete_bars(eventful=eventful)),
    )


def _decoded_binding_payload(
    binding: QuantV6PendingArtifactBinding,
) -> dict[str, object]:
    artifact = binding.artifact
    return decode_quant_v6_artifact(
        digest_sha256=artifact.digest_sha256,
        schema_version=artifact.schema_version,
        kind=artifact.kind,
        codec=artifact.codec,
        raw_size=artifact.raw_size,
        compressed_size=artifact.compressed_size,
        payload=artifact.payload,
    )


def _object_mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    assert all(isinstance(key, str) for key in value)
    return value


def _object_list(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def _string_list(value: object) -> list[str]:
    assert isinstance(value, list)
    assert all(isinstance(item, str) for item in value)
    return value


def _replace_binding_payload(
    evaluation: QuantV6CandidateEvaluation,
    *,
    binding_index: int,
    payload: dict[str, object],
) -> QuantV6CandidateEvaluation:
    plan = _one_member_plan()
    original = evaluation.bindings[binding_index]
    tampered_artifact = encode_quant_v6_artifact(
        payload,
        kind=original.artifact.kind,
    )
    provisional = replace(original, artifact=tampered_artifact)
    tampered_binding = replace(
        provisional,
        binding_sha256=quant_v6_payload_sha256(_binding_preimage(
            registration_identity_sha256=plan.identity_sha256,
            binding=provisional,
        )),
    )
    bindings = list(evaluation.bindings)
    bindings[binding_index] = tampered_binding
    changes: dict[str, object] = {"bindings": tuple(bindings)}
    if original.role == ASSESSMENT_ROLE:
        changes["assessment_artifact_sha256"] = (
            tampered_artifact.digest_sha256
        )
    return replace(evaluation, **changes)


def _replace_assessment_payload(
    evaluation: QuantV6CandidateEvaluation,
    payload: dict[str, object],
) -> QuantV6CandidateEvaluation:
    assessment_index = next(
        index
        for index, binding in enumerate(evaluation.bindings)
        if binding.role == ASSESSMENT_ROLE
    )
    return _replace_binding_payload(
        evaluation,
        binding_index=assessment_index,
        payload=payload,
    )


def _counts(engine: Engine) -> tuple[int, int, int, int]:
    with Session(engine) as session:
        return (
            session.scalar(
                select(func.count()).select_from(WatchlistQuantV6Registration)
            )
            or 0,
            session.scalar(
                select(func.count()).select_from(WatchlistQuantV6Artifact)
            )
            or 0,
            session.scalar(
                select(func.count()).select_from(WatchlistQuantV6Publication)
            )
            or 0,
            session.scalar(select(func.count()).select_from(
                WatchlistQuantV6PublicationArtifact
            ))
            or 0,
        )


def test_registration_is_committed_before_callback_and_survives_failure(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(engine)
    service = WatchlistQuantV6PublicationService(factory, clock=lambda: _NOW)
    observed: list[tuple[int, int]] = []

    class _FailingProvider(_Provider):
        def fetch_five_minute_no_adjust(
            self,
            symbol: str,
            *,
            start_at: datetime,
            end_at: datetime,
        ) -> QuantV6HistoricalBarFetch:
            del symbol, start_at, end_at
            self.calls += 1
            with factory() as session:
                observed.append((
                    session.scalar(select(func.count()).select_from(
                        WatchlistQuantV6Registration
                    ))
                    or 0,
                    session.scalar(select(func.count()).select_from(
                        WatchlistQuantV6Publication
                    ))
                    or 0,
                ))
            raise RuntimeError("provider unavailable")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        service.register_provider_evaluate_publish(
            plan=_one_member_plan(),
            provider=_FailingProvider(),
        )

    assert observed == [(1, 0)]
    assert _counts(engine) == (1, 0, 0, 0)

    original = _one_member_plan()
    selected = next(
        candidate
        for candidate in ROTATION_RESEARCH_CANDIDATE_CATALOG
        if candidate.symbol == original.members[0].symbol
    )
    rebuilt = _build_registration_plan(
        observed_at=datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc),
        market="US",
        candidates=(selected,),
        membership_history=INDEX_MEMBERSHIP_HISTORY,
    )
    assert rebuilt.identity_sha256 == original.identity_sha256
    assert rebuilt.registration_json == original.registration_json
    assert rebuilt.cohort_observed_at == original.cohort_observed_at

    retry_provider = _Provider()
    receipt = service.register_provider_evaluate_publish(
        plan=rebuilt,
        provider=retry_provider,
    )
    assert receipt.created is True
    assert retry_provider.calls == 1
    assert _counts(engine) == (1, 1, 1, 1)


def test_deadline_after_evaluation_never_starts_publication(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(engine)
    deadline = QuantV6EvaluationDeadline(60)

    def finish_then_expire(
        _plan: object,
        _checkpoint: Callable[[], None],
    ) -> tuple[QuantV6CandidateEvaluation, ...]:
        evaluations = _missing_evaluations()
        deadline.expire()
        return evaluations

    with pytest.raises(QuantV6EvaluationDeadlineExceededError):
        WatchlistQuantV6PublicationService(
            factory,
            clock=lambda: _NOW,
        ).register_evaluate_publish(
            plan=_one_member_plan(),
            evaluation_callback=finish_then_expire,
            evaluation_deadline=deadline,
        )

    assert _counts(engine) == (1, 0, 0, 0)


def test_generic_callback_cooperatively_observes_cancellation(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    service = WatchlistQuantV6PublicationService(
        _factory(engine),
        clock=lambda: _NOW,
    )
    deadline = QuantV6EvaluationDeadline(60)
    callback_started = threading.Event()
    cancellation_poll = threading.Event()

    def cooperative_evaluation(
        _plan: QuantV6RegistrationPlan,
        checkpoint: Callable[[], None],
    ) -> tuple[QuantV6CandidateEvaluation, ...]:
        callback_started.set()
        while True:
            checkpoint()
            cancellation_poll.wait(0.01)

    def invoke() -> QuantV6PublicationReceipt:
        return service.register_evaluate_publish(
            plan=_one_member_plan(),
            evaluation_callback=cooperative_evaluation,
            evaluation_deadline=deadline,
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(invoke)
        assert callback_started.wait(2)
        assert _counts(engine) == (1, 0, 0, 0)
        deadline.cancel()
        with pytest.raises(QuantV6EvaluationCancelledError):
            result.result(timeout=2)

    assert _counts(engine) == (1, 0, 0, 0)


def test_atomic_publication_is_complete_p0_and_idempotent(tmp_path) -> None:
    engine = _engine(tmp_path)
    service = WatchlistQuantV6PublicationService(
        _factory(engine),
        clock=lambda: _NOW,
    )
    plan = _one_member_plan()

    first = service.register_evaluate_publish(
        plan=plan,
        evaluation_callback=(
            lambda _plan, _checkpoint: _missing_evaluations()
        ),
    )
    second = service.register_evaluate_publish(
        plan=plan,
        evaluation_callback=(
            lambda _plan, _checkpoint: _missing_evaluations()
        ),
    )

    assert first.created is True
    assert second.created is False
    assert second.publication_id == first.publication_id
    assert second.identity_sha256 == first.identity_sha256
    assert second.manifest_sha256 == first.manifest_sha256
    assert first.binding_count == 1
    assert _counts(engine) == (1, 1, 1, 1)
    with Session(engine) as session:
        publication = session.get(
            WatchlistQuantV6Publication,
            first.publication_id,
        )
        assert publication is not None
        assert publication.assessment_artifact_count == 1
        assert publication.session_input_artifact_count == 0
        assert publication.event_artifact_count == 0
        assert publication.promotion_eligible is False
        assert publication.automatic_promotion_allowed is False
        assert publication.order_submission_allowed is False
        assert publication.short_entry_allowed is False
        assert publication.position_add_on_allowed is False


def test_provider_repeat_replays_persisted_publication_without_quote_io(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    service = WatchlistQuantV6PublicationService(
        _factory(engine),
        clock=lambda: _NOW,
    )
    original = _one_member_plan()
    first_provider = _Provider(raw_rows=3, rejected_rows=2)
    first = service.register_provider_evaluate_publish(
        plan=original,
        provider=first_provider,
    )
    selected = next(
        candidate
        for candidate in ROTATION_RESEARCH_CANDIDATE_CATALOG
        if candidate.symbol == original.members[0].symbol
    )
    weekend_plan = _build_registration_plan(
        observed_at=datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
        market="US",
        candidates=(selected,),
        membership_history=INDEX_MEMBERSHIP_HISTORY,
    )
    assert weekend_plan.identity_sha256 == original.identity_sha256
    repeated_provider = _Provider()

    repeated = service.register_provider_evaluate_publish(
        plan=weekend_plan,
        provider=repeated_provider,
    )

    assert first.created is True
    assert first_provider.calls == 1
    assert repeated.created is False
    assert repeated.publication_id == first.publication_id
    assert repeated_provider.calls == 0
    with Session(engine) as session:
        publication = session.get(
            WatchlistQuantV6Publication,
            first.publication_id,
        )
        assert publication is not None
        payload = json.loads(publication.publication_json)
    acquisition = payload["acquisition_outcome"]
    assert acquisition["request_start_at"].endswith("Z")
    assert acquisition["request_end_at"].endswith("Z")
    assert len(acquisition["members"]) == 1
    outcome = acquisition["members"][0]
    assert {
        key: outcome[key]
        for key in (
            "accepted_bars",
            "complete_session_count",
            "market",
            "member_ordinal",
            "off_grid_accepted_bars",
            "pages",
            "raw_rows",
            "rejected_rows",
            "scheduled_grid_present_bars",
            "symbol",
        )
    } == {
        "accepted_bars": 0,
        "complete_session_count": 0,
        "market": "US",
        "member_ordinal": 0,
        "off_grid_accepted_bars": 0,
        "pages": 1,
        "raw_rows": 3,
        "rejected_rows": 2,
        "scheduled_grid_present_bars": 0,
        "symbol": original.members[0].symbol,
    }
    scheduled_bar_count = sum(
        len(quant_v6_expected_rth_bar_starts("US", session_date))
        for session_date in (
            *original.training_session_dates,
            *original.target_session_dates,
        )
    )
    assert outcome["scheduled_grid_coverage_bitset_hex"] == (
        "00" * ((scheduled_bar_count + 7) // 8)
    )
    assert len(outcome["accepted_bar_starts_sha256"]) == 64
    assert len(outcome["scheduled_grid_present_starts_sha256"]) == 64


def test_provider_fast_path_deadline_during_replay_is_read_only(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    service = WatchlistQuantV6PublicationService(
        _factory(engine),
        clock=lambda: _NOW,
    )
    plan = _one_member_plan()
    service.register_provider_evaluate_publish(
        plan=plan,
        provider=_Provider(),
    )
    before = _counts(engine)
    provider = _Provider()
    deadline = QuantV6EvaluationDeadline(60)
    replay_query_seen = threading.Event()

    def expire_after_binding_query(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = statement.lower()
        if (
            "select" in normalized
            and "from watchlist_quant_v6_publication_artifacts" in normalized
        ):
            replay_query_seen.set()
            deadline.expire()

    event.listen(engine, "after_cursor_execute", expire_after_binding_query)
    try:
        with pytest.raises(QuantV6EvaluationDeadlineExceededError):
            service.register_provider_evaluate_publish(
                plan=plan,
                provider=provider,
                evaluation_deadline=deadline,
            )
    finally:
        event.remove(
            engine,
            "after_cursor_execute",
            expire_after_binding_query,
        )

    assert replay_query_seen.is_set() is True
    assert provider.calls == 0
    assert _counts(engine) == before


def test_provider_fast_path_hard_fails_tampered_publication_before_io(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    service = WatchlistQuantV6PublicationService(
        _factory(engine),
        clock=lambda: _NOW,
    )
    plan = _one_member_plan()
    first = service.register_provider_evaluate_publish(
        plan=plan,
        provider=_Provider(),
    )
    with Session(engine) as session:
        publication = session.get(
            WatchlistQuantV6Publication,
            first.publication_id,
        )
        assert publication is not None
        payload = json.loads(publication.publication_json)
    payload["acquisition_outcome"]["members"][0]["rejected_rows"] = 1
    tampered_json = canonical_quant_v6_json(payload).decode("utf-8")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP TRIGGER trg_watchlist_quant_v6_publications_no_update"
        )
        connection.exec_driver_sql(
            "UPDATE watchlist_quant_v6_publications "
            "SET publication_json = ? WHERE id = ?",
            (tampered_json, first.publication_id),
        )
    provider = _Provider()

    with pytest.raises(
        QuantV6PublicationConflictError,
        match="identity failed canonical replay",
    ):
        service.register_provider_evaluate_publish(
            plan=plan,
            provider=provider,
        )

    assert provider.calls == 0


@pytest.mark.parametrize(
    "tamper",
    ("bitset", "scheduled-digest", "present-count"),
)
def test_provider_fast_path_replays_acquisition_projection_before_io(
    tmp_path,
    tamper: str,
) -> None:
    engine = _engine(tmp_path)
    service = WatchlistQuantV6PublicationService(
        _factory(engine),
        clock=lambda: _NOW,
    )
    plan = _one_member_plan()
    first = service.register_provider_evaluate_publish(
        plan=plan,
        provider=_Provider(),
    )
    with Session(engine) as session:
        publication = session.get(
            WatchlistQuantV6Publication,
            first.publication_id,
        )
        assert publication is not None
        payload = json.loads(publication.publication_json)
    outcome = payload["acquisition_outcome"]["members"][0]
    if tamper == "bitset":
        bitset = outcome["scheduled_grid_coverage_bitset_hex"]
        outcome["scheduled_grid_coverage_bitset_hex"] = f"8{bitset[1:]}"
    elif tamper == "scheduled-digest":
        outcome["scheduled_grid_present_starts_sha256"] = "f" * 64
    else:
        outcome["scheduled_grid_present_bars"] = 1
    tampered_bytes = canonical_quant_v6_json(payload)
    tampered_json = tampered_bytes.decode("utf-8")
    tampered_identity = hashlib.sha256(tampered_bytes).hexdigest()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP TRIGGER trg_watchlist_quant_v6_publications_no_update"
        )
        connection.exec_driver_sql(
            "UPDATE watchlist_quant_v6_publications "
            "SET publication_json = ?, identity_sha256 = ? WHERE id = ?",
            (tampered_json, tampered_identity, first.publication_id),
        )
    provider = _Provider()

    with pytest.raises(
        QuantV6PublicationConflictError,
        match="strict semantic replay",
    ):
        service.register_provider_evaluate_publish(
            plan=plan,
            provider=provider,
        )

    assert provider.calls == 0


def test_acquisition_grid_bitset_rejects_nonzero_padding() -> None:
    with pytest.raises(
        QuantV6PublicationError,
        match="non-zero padding bits",
    ):
        service_module._decode_grid_coverage_bitset(
            "01",
            expected_bits=3,
            label="test bitset",
        )


def test_concurrent_registration_strictly_reuses_one_identity(tmp_path) -> None:
    engine = _engine(tmp_path)
    factory = _factory(engine)
    service = WatchlistQuantV6PublicationService(factory, clock=lambda: _NOW)
    plan = _one_member_plan()
    barrier = threading.Barrier(2)

    def _register():
        barrier.wait()
        return service.register_plan(plan)

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = tuple(executor.map(lambda _index: _register(), range(2)))

    assert sorted(receipt.created for receipt in receipts) == [False, True]
    assert len({receipt.registration_id for receipt in receipts}) == 1
    assert {receipt.identity_sha256 for receipt in receipts} == {
        plan.identity_sha256
    }
    assert _counts(engine) == (1, 0, 0, 0)


def test_concurrent_publication_creates_one_strictly_reused_cohort(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(engine)
    service = WatchlistQuantV6PublicationService(factory, clock=lambda: _NOW)
    plan = _one_member_plan()
    service.register_plan(plan)
    evaluations = _missing_evaluations()
    barrier = threading.Barrier(2)

    def _publish():
        barrier.wait()
        return service.publish_registration(
            plan=plan,
            evaluations=evaluations,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = tuple(executor.map(lambda _index: _publish(), range(2)))

    assert sorted(receipt.created for receipt in receipts) == [False, True]
    assert len({receipt.publication_id for receipt in receipts}) == 1
    assert len({receipt.registration_id for receipt in receipts}) == 1
    assert len({receipt.identity_sha256 for receipt in receipts}) == 1
    assert len({receipt.manifest_sha256 for receipt in receipts}) == 1
    assert {receipt.binding_count for receipt in receipts} == {1}
    assert _counts(engine) == (1, 1, 1, 1)


def test_provider_orchestrator_registers_before_first_provider_io(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(engine)

    class _ObservingProvider(_Provider):
        def fetch_five_minute_no_adjust(
            self,
            symbol: str,
            *,
            start_at: datetime,
            end_at: datetime,
        ) -> QuantV6HistoricalBarFetch:
            with factory() as session:
                assert session.scalar(select(func.count()).select_from(
                    WatchlistQuantV6Registration
                )) == 1
                assert session.scalar(select(func.count()).select_from(
                    WatchlistQuantV6Publication
                )) == 0
            return super().fetch_five_minute_no_adjust(
                symbol,
                start_at=start_at,
                end_at=end_at,
            )

    provider = _ObservingProvider()
    receipt = WatchlistQuantV6PublicationService(
        factory,
        clock=lambda: _NOW,
    ).register_provider_evaluate_publish(
        plan=_one_member_plan(),
        provider=provider,
    )

    assert provider.calls == 1
    assert receipt.created is True
    assert _counts(engine) == (1, 1, 1, 1)


def test_registration_reuse_hard_fails_on_field_drift(tmp_path) -> None:
    engine = _engine(tmp_path)
    factory = _factory(engine)
    plan = _one_member_plan()
    fields = _registration_fields(plan)
    fields["cohort_member_count"] = 2
    with factory() as session:
        session.add(WatchlistQuantV6Registration(
            **fields,
            registered_at=_NOW,
        ))
        session.commit()

    with pytest.raises(
        QuantV6PublicationConflictError,
        match="cohort_member_count",
    ):
        WatchlistQuantV6PublicationService(
            factory,
            clock=lambda: _NOW,
        ).register_plan(plan)


def test_complete_session_binding_closure_rejects_one_missing_binding(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(engine)
    plan = _one_member_plan()
    service = WatchlistQuantV6PublicationService(factory, clock=lambda: _NOW)
    service.register_plan(plan)
    evaluation = _missing_evaluations()[0]
    assessment_binding = evaluation.bindings[0]
    assessment_artifact = assessment_binding.artifact
    payload = decode_quant_v6_artifact(
        digest_sha256=assessment_artifact.digest_sha256,
        schema_version=assessment_artifact.schema_version,
        kind=assessment_artifact.kind,
        codec=assessment_artifact.codec,
        raw_size=assessment_artifact.raw_size,
        compressed_size=assessment_artifact.compressed_size,
        payload=assessment_artifact.payload,
    )
    tampered_payload = deepcopy(payload)
    tampered_payload["aggregates"]["covered_sessions"] = 1
    tampered_payload["leaves"][0]["status"] = "COVERED"
    tampered_payload["leaves"][0][
        "replay_input_artifact_sha256"
    ] = "f" * 64
    tampered = replace(
        _replace_assessment_payload(evaluation, tampered_payload),
        covered_sessions=1,
    )

    with pytest.raises(
        QuantV6PublicationError,
        match="covered declaration|session input closure",
    ):
        service.publish_registration(plan=plan, evaluations=(tampered,))

    assert _counts(engine) == (1, 0, 0, 0)


@pytest.mark.parametrize(
    "tamper",
    ("identity-extra", "window-digest", "event-set-digest"),
)
def test_assessment_window_and_event_set_digests_are_replayed(
    tmp_path,
    tamper: str,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(engine)
    plan = _one_member_plan()
    service = WatchlistQuantV6PublicationService(factory, clock=lambda: _NOW)
    service.register_plan(plan)
    evaluation = _missing_evaluations()[0]
    artifact = evaluation.bindings[0].artifact
    payload = decode_quant_v6_artifact(
        digest_sha256=artifact.digest_sha256,
        schema_version=artifact.schema_version,
        kind=artifact.kind,
        codec=artifact.codec,
        raw_size=artifact.raw_size,
        compressed_size=artifact.compressed_size,
        payload=artifact.payload,
    )
    tampered_payload = deepcopy(payload)
    if tamper == "identity-extra":
        tampered_payload["identity"]["unexpected"] = False
    elif tamper == "window-digest":
        tampered_payload["identity"]["window_digest_sha256"] = "f" * 64
    else:
        tampered_payload["event_set_digest_sha256"] = "f" * 64
    tampered_evaluation = _replace_assessment_payload(
        evaluation,
        tampered_payload,
    )

    with pytest.raises(QuantV6PublicationError, match="assessment"):
        service.publish_registration(
            plan=plan,
            evaluations=(tampered_evaluation,),
        )

    assert _counts(engine) == (1, 0, 0, 0)


@pytest.mark.parametrize("tamper", ("session-bar", "threshold-digest"))
def test_session_input_requires_strict_typed_replay(
    tmp_path,
    tamper: str,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(engine)
    plan = _one_member_plan()
    service = WatchlistQuantV6PublicationService(factory, clock=lambda: _NOW)
    service.register_plan(plan)
    evaluation = _covered_evaluations()[0]
    session_index = next(
        index
        for index, binding in enumerate(evaluation.bindings)
        if binding.role == SESSION_INPUT_ROLE
    )
    session_binding = evaluation.bindings[session_index]
    session_payload = deepcopy(_decoded_binding_payload(session_binding))
    if tamper == "session-bar":
        bars = _object_list(session_payload["bars"])
        _object_mapping(bars[0])["close"] = "100.5"
    else:
        _object_mapping(session_payload["threshold_evidence"])[
            "preimage_digest_sha256"
        ] = "f" * 64
    tampered = _replace_binding_payload(
        evaluation,
        binding_index=session_index,
        payload=session_payload,
    )
    new_session_binding = tampered.bindings[session_index]
    assessment_index = next(
        index
        for index, binding in enumerate(tampered.bindings)
        if binding.role == ASSESSMENT_ROLE
    )
    assessment_payload = deepcopy(_decoded_binding_payload(
        tampered.bindings[assessment_index]
    ))
    leaves = _object_list(assessment_payload["leaves"])
    leaf = _object_mapping(leaves[session_binding.artifact_ordinal])
    leaf["replay_input_artifact_sha256"] = (
        new_session_binding.artifact.digest_sha256
    )
    if tamper == "threshold-digest":
        leaf["threshold_preimage_sha256"] = "f" * 64
    tampered = _replace_binding_payload(
        tampered,
        binding_index=assessment_index,
        payload=assessment_payload,
    )

    with pytest.raises(
        QuantV6PublicationError,
        match="session input|threshold replay|canonical bytes",
    ):
        service.publish_registration(plan=plan, evaluations=(tampered,))

    assert _counts(engine) == (1, 0, 0, 0)


def test_event_return_tamper_is_rejected_by_full_session_replay(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(engine)
    plan = _one_member_plan()
    service = WatchlistQuantV6PublicationService(factory, clock=lambda: _NOW)
    service.register_plan(plan)
    evaluation = _covered_evaluations(eventful=True)[0]
    event_index = next(
        index
        for index, binding in enumerate(evaluation.bindings)
        if binding.role == EVENT_ROLE
    )
    event_binding = evaluation.bindings[event_index]
    event_payload = deepcopy(_decoded_binding_payload(event_binding))
    _object_mapping(event_payload["costs"])["net_return_bps"] = "999"
    tampered = _replace_binding_payload(
        evaluation,
        binding_index=event_index,
        payload=event_payload,
    )
    new_event_binding = tampered.bindings[event_index]
    assessment_index = next(
        index
        for index, binding in enumerate(tampered.bindings)
        if binding.role == ASSESSMENT_ROLE
    )
    assessment_payload = deepcopy(_decoded_binding_payload(
        tampered.bindings[assessment_index]
    ))
    old_digest = event_binding.artifact.digest_sha256
    new_digest = new_event_binding.artifact.digest_sha256
    replaced = False
    all_event_digests: list[str] = []
    for leaf_value in _object_list(assessment_payload["leaves"]):
        leaf = _object_mapping(leaf_value)
        digests = _string_list(leaf["event_artifact_sha256"])
        for index, digest in enumerate(digests):
            if digest == old_digest and not replaced:
                digests[index] = new_digest
                replaced = True
            all_event_digests.append(digests[index])
    assert replaced is True
    identity = _object_mapping(assessment_payload["identity"])
    window_digest_sha256 = identity["window_digest_sha256"]
    assert isinstance(window_digest_sha256, str)
    assessment_payload["event_set_digest_sha256"] = quant_v6_payload_sha256({
        "event_artifact_sha256": all_event_digests,
        "semantic_digest": QUANT_V6_SEMANTIC_DIGEST,
        "window_digest_sha256": window_digest_sha256,
    })
    tampered = _replace_binding_payload(
        tampered,
        binding_index=assessment_index,
        payload=assessment_payload,
    )

    with pytest.raises(
        QuantV6PublicationError,
        match="event.*canonical bytes|event.*replay",
    ):
        service.publish_registration(plan=plan, evaluations=(tampered,))

    assert _counts(engine) == (1, 0, 0, 0)


def test_self_consistent_assessment_aggregate_tamper_is_rejected(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(engine)
    plan = _one_member_plan()
    service = WatchlistQuantV6PublicationService(factory, clock=lambda: _NOW)
    service.register_plan(plan)
    evaluation = _missing_evaluations()[0]
    payload = deepcopy(_decoded_binding_payload(evaluation.bindings[0]))
    _object_mapping(payload["aggregates"])[
        "median_net_return_bps"
    ] = "999"
    tampered = _replace_assessment_payload(evaluation, payload)

    with pytest.raises(
        QuantV6PublicationError,
        match="assessment.*canonical bytes|assessment.*replay",
    ):
        service.publish_registration(plan=plan, evaluations=(tampered,))

    assert _counts(engine) == (1, 0, 0, 0)


def test_covered_session_cannot_be_downgraded_without_acquisition_evidence(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(engine)
    plan = _one_member_plan()
    service = WatchlistQuantV6PublicationService(factory, clock=lambda: _NOW)
    service.register_plan(plan)
    covered = _covered_evaluations()[0]
    removed_start = quant_v6_expected_rth_bar_starts(
        plan.market,
        plan.target_session_dates[0],
    )[0]
    partial_bars = tuple(
        bar
        for bar in _complete_bars(eventful=False)
        if bar.start_at != removed_start
    )
    downgraded = evaluate_quant_v6_registration(
        registration=plan,
        provider=_Provider(partial_bars),
    )[0]
    tampered = replace(
        downgraded,
        fetched_pages=covered.fetched_pages,
        fetched_raw_rows=covered.fetched_raw_rows,
        fetched_accepted_bars=covered.fetched_accepted_bars,
        fetched_bar_starts=covered.fetched_bar_starts,
        rejected_rows=covered.rejected_rows,
    )

    with pytest.raises(
        QuantV6PublicationError,
        match="status or blockers conflict with acquisition grid evidence",
    ):
        service.publish_registration(plan=plan, evaluations=(tampered,))

    assert _counts(engine) == (1, 0, 0, 0)


def test_corrupt_content_addressed_artifact_is_not_trusted(tmp_path) -> None:
    engine = _engine(tmp_path)
    factory = _factory(engine)
    plan = _one_member_plan()
    service = WatchlistQuantV6PublicationService(factory, clock=lambda: _NOW)
    service.register_plan(plan)
    artifact = _missing_evaluations()[0].bindings[0].artifact
    corrupt = bytes([artifact.payload[0] ^ 1]) + artifact.payload[1:]
    with factory() as session:
        session.add(WatchlistQuantV6Artifact(
            digest_sha256=artifact.digest_sha256,
            schema_version=artifact.schema_version,
            kind=artifact.kind,
            codec=artifact.codec,
            compression_level=9,
            raw_size=artifact.raw_size,
            compressed_size=artifact.compressed_size,
            payload=corrupt,
            created_at=_NOW,
        ))
        session.commit()

    with pytest.raises(
        QuantV6PublicationConflictError,
        match="canonical bytes",
    ):
        service.publish_registration(
            plan=plan,
            evaluations=_missing_evaluations(),
        )

    assert _counts(engine) == (1, 1, 0, 0)


def test_binding_insert_failure_rolls_back_header_and_new_artifacts(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(engine)
    plan = _one_member_plan()
    service = WatchlistQuantV6PublicationService(factory, clock=lambda: _NOW)
    service.register_plan(plan)

    def _fail_binding_insert(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if statement.lstrip().startswith(
            "INSERT INTO watchlist_quant_v6_publication_artifacts"
        ):
            raise RuntimeError("forced binding failure")

    event.listen(engine, "before_cursor_execute", _fail_binding_insert)
    try:
        with pytest.raises(RuntimeError, match="forced binding failure"):
            service.publish_registration(
                plan=plan,
                evaluations=_missing_evaluations(),
            )
    finally:
        event.remove(engine, "before_cursor_execute", _fail_binding_insert)

    assert _counts(engine) == (1, 0, 0, 0)


def test_transaction_fence_runs_before_any_publication_dml(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(engine)
    plan = _one_member_plan()
    business_dml: list[str] = []
    fenced_sessions: list[Session] = []

    def _record_business_dml(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        normalized = " ".join(statement.upper().split())
        if any(
            table_name.upper() in normalized
            for table_name in (
                "watchlist_quant_v6_artifacts",
                "watchlist_quant_v6_publications",
                "watchlist_quant_v6_publication_artifacts",
            )
        ) and normalized.startswith(("INSERT ", "UPDATE ", "DELETE ")):
            business_dml.append(statement)

    def _fence(session: Session) -> None:
        assert business_dml == []
        fenced_sessions.append(session)

    service = WatchlistQuantV6PublicationService(
        factory,
        clock=lambda: _NOW,
        transaction_fence=_fence,
    )
    service.register_plan(plan)
    fenced_sessions.clear()
    event.listen(engine, "before_cursor_execute", _record_business_dml)
    try:
        receipt = service.publish_registration(
            plan=plan,
            evaluations=_missing_evaluations(),
        )
    finally:
        event.remove(engine, "before_cursor_execute", _record_business_dml)

    assert receipt.created is True
    assert len(fenced_sessions) == 1
    assert business_dml
    assert _counts(engine) == (1, 1, 1, 1)


def test_lost_transaction_fence_keeps_registration_and_rolls_back_publication(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(engine)
    plan = _one_member_plan()
    fenced_sessions: list[Session] = []

    def _lost_fence(session: Session) -> None:
        fenced_sessions.append(session)
        raise RuntimeError("durable quant-v6 lease was lost")

    WatchlistQuantV6PublicationService(
        factory,
        clock=lambda: _NOW,
    ).register_plan(plan)
    service = WatchlistQuantV6PublicationService(
        factory,
        clock=lambda: _NOW,
        transaction_fence=_lost_fence,
    )

    with pytest.raises(RuntimeError, match="lease was lost"):
        service.publish_registration(
            plan=plan,
            evaluations=_missing_evaluations(),
        )

    assert len(fenced_sessions) == 1
    assert _counts(engine) == (1, 0, 0, 0)


def test_transaction_fence_runs_before_any_registration_dml(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(engine)
    plan = _one_member_plan()
    registration_dml: list[str] = []
    fenced_sessions: list[Session] = []

    def _record_registration_dml(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        normalized = " ".join(statement.upper().split())
        if (
            "WATCHLIST_QUANT_V6_REGISTRATIONS" in normalized
            and normalized.startswith(("INSERT ", "UPDATE ", "DELETE "))
        ):
            registration_dml.append(statement)

    def _fence(session: Session) -> None:
        assert registration_dml == []
        assert session.in_transaction() is False
        fenced_sessions.append(session)

    service = WatchlistQuantV6PublicationService(
        factory,
        clock=lambda: _NOW,
        transaction_fence=_fence,
    )
    event.listen(engine, "before_cursor_execute", _record_registration_dml)
    try:
        receipt = service.register_plan(plan)
    finally:
        event.remove(engine, "before_cursor_execute", _record_registration_dml)

    assert receipt.created is True
    assert len(fenced_sessions) == 1
    assert registration_dml
    assert _counts(engine) == (1, 0, 0, 0)


def test_lost_transaction_fence_rolls_back_registration(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(engine)
    plan = _one_member_plan()
    fenced_sessions: list[Session] = []

    def _lost_fence(session: Session) -> None:
        assert session.in_transaction() is False
        fenced_sessions.append(session)
        raise RuntimeError("durable quant-v6 lease was lost")

    service = WatchlistQuantV6PublicationService(
        factory,
        clock=lambda: _NOW,
        transaction_fence=_lost_fence,
    )

    with pytest.raises(RuntimeError, match="lease was lost"):
        service.register_plan(plan)

    assert len(fenced_sessions) == 1
    assert _counts(engine) == (0, 0, 0, 0)


@pytest.mark.parametrize("expire_after_flush", [1, 2, 3])
def test_deadline_between_publication_flushes_rolls_back_everything(
    tmp_path,
    expire_after_flush: int,
) -> None:
    engine = _engine(tmp_path)
    plan = _one_member_plan()
    normal_factory = _factory(engine)
    WatchlistQuantV6PublicationService(
        normal_factory,
        clock=lambda: _NOW,
    ).register_plan(plan)
    deadline = QuantV6EvaluationDeadline(60)
    flushes = 0

    class _ExpiringSession(Session):
        def flush(self, objects: Sequence[Any] | None = None) -> None:
            nonlocal flushes
            super().flush(objects)
            flushes += 1
            if flushes == expire_after_flush:
                deadline.expire()

    def expiring_factory() -> Session:
        return _ExpiringSession(
            bind=engine,
            autoflush=False,
            expire_on_commit=False,
        )

    service = WatchlistQuantV6PublicationService(
        expiring_factory,
        clock=lambda: _NOW,
    )

    with pytest.raises(QuantV6EvaluationDeadlineExceededError):
        service.publish_registration(
            plan=plan,
            evaluations=_missing_evaluations(),
            evaluation_deadline=deadline,
        )

    assert flushes == expire_after_flush
    assert _counts(engine) == (1, 0, 0, 0)


def test_deadline_at_final_precommit_checkpoint_rolls_back_everything(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(engine)
    plan = _one_member_plan()
    service = WatchlistQuantV6PublicationService(
        factory,
        clock=lambda: _NOW,
    )
    service.register_plan(plan)
    deadline = QuantV6EvaluationDeadline(60)
    original_verify = service._verify_complete_publication

    def expire_after_verify(*args: Any, **kwargs: Any) -> Any:
        receipt = original_verify(*args, **kwargs)
        deadline.expire()
        return receipt

    monkeypatch.setattr(
        service,
        "_verify_complete_publication",
        expire_after_verify,
    )

    with pytest.raises(QuantV6EvaluationDeadlineExceededError):
        service.publish_registration(
            plan=plan,
            evaluations=_missing_evaluations(),
            evaluation_deadline=deadline,
        )

    assert _counts(engine) == (1, 0, 0, 0)


def test_successful_atomic_commit_wins_over_post_commit_deadline(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    plan = _one_member_plan()
    normal_factory = _factory(engine)
    WatchlistQuantV6PublicationService(
        normal_factory,
        clock=lambda: _NOW,
    ).register_plan(plan)
    deadline = QuantV6EvaluationDeadline(60)
    commits = 0

    class _CompletingSession(Session):
        def commit(self) -> None:
            nonlocal commits
            commits += 1
            deadline.expire()
            super().commit()

    def completing_factory() -> Session:
        return _CompletingSession(
            bind=engine,
            autoflush=False,
            expire_on_commit=False,
        )

    receipt = WatchlistQuantV6PublicationService(
        completing_factory,
        clock=lambda: _NOW,
    ).publish_registration(
        plan=plan,
        evaluations=_missing_evaluations(),
        evaluation_deadline=deadline,
    )

    assert commits == 1
    assert receipt.created is True
    assert deadline.is_stopped() is True
    assert _counts(engine) == (1, 1, 1, 1)


def test_complete_publication_batches_artifact_queries_before_short_write(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    factory = _factory(engine)
    plan = _one_member_plan()
    service = WatchlistQuantV6PublicationService(factory, clock=lambda: _NOW)
    service.register_plan(plan)
    statements: list[str] = []

    def _record_statement(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record_statement)
    try:
        receipt = service.publish_registration(
            plan=plan,
            evaluations=_covered_evaluations(),
        )
    finally:
        event.remove(engine, "before_cursor_execute", _record_statement)

    artifact_selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
        and "FROM watchlist_quant_v6_artifacts" in statement
    ]
    assert receipt.created is True
    assert receipt.binding_count == 31
    first_write_index = next(
        index
        for index, statement in enumerate(statements)
        if statement.lstrip().upper().startswith("INSERT")
        and "watchlist_quant_v6_artifacts" in statement
    )
    artifact_select_indexes = tuple(
        index
        for index, statement in enumerate(statements)
        if statement in artifact_selects
    )
    assert len(artifact_selects) <= 1
    assert all(index < first_write_index for index in artifact_select_indexes)
    assert len(statements) <= 12


def test_real_cohort_artifact_lookup_has_a_bounded_query_count(tmp_path) -> None:
    engine = _engine(tmp_path)
    full_plan = build_latest_quant_v6_registration_plan(
        observed_at=datetime(2026, 7, 31, 23, tzinfo=timezone.utc),
    )
    binding_count = len(full_plan.members) * (1 + len(
        full_plan.target_session_dates
    ))
    assert len(full_plan.members) == 123
    assert binding_count == 3_813
    statements: list[str] = []

    def _record_statement(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record_statement)
    try:
        with Session(engine) as session:
            rows = service_module._load_artifact_rows(
                session,
                tuple(f"{index:064x}" for index in range(binding_count)),
            )
    finally:
        event.remove(engine, "before_cursor_execute", _record_statement)

    assert rows == {}
    assert len(statements) == (
        binding_count + service_module._ARTIFACT_QUERY_CHUNK_SIZE - 1
    ) // service_module._ARTIFACT_QUERY_CHUNK_SIZE


def test_real_cohort_compact_acquisition_root_stays_bounded() -> None:
    plan = build_latest_quant_v6_registration_plan(
        observed_at=datetime(2026, 7, 31, 23, tzinfo=timezone.utc),
    )
    grids = service_module._scheduled_session_grids(plan)
    scheduled_bar_count = sum(len(grid) for _session_date, grid in grids)
    complete_bitset = service_module._encode_grid_coverage_bitset(
        (True,) * scheduled_bar_count
    )
    outcomes = tuple({
        "accepted_bars": scheduled_bar_count,
        "accepted_bar_starts_sha256": "a" * 64,
        "complete_session_count": len(grids),
        "market": member.market,
        "member_ordinal": member.ordinal,
        "off_grid_accepted_bars": 0,
        "pages": 4,
        "raw_rows": scheduled_bar_count,
        "rejected_rows": 0,
        "scheduled_grid_coverage_bitset_hex": complete_bitset,
        "scheduled_grid_present_bars": scheduled_bar_count,
        "scheduled_grid_present_starts_sha256": "b" * 64,
        "symbol": member.symbol,
    } for member in plan.members)
    payload = service_module._publication_payload(
        registration_identity_sha256=plan.identity_sha256,
        registered_member_count=len(plan.members),
        manifest_sha256="c" * 64,
        assessment_count=len(plan.members),
        session_input_count=(
            len(plan.members) * len(plan.target_session_dates)
        ),
        event_count=0,
        acquisition_outcomes=outcomes,
        request_start_at=grids[0][1][0],
        request_end_at=plan.data_cutoff_at,
    )
    encoded = canonical_quant_v6_json(payload)

    assert len(plan.members) == 123
    assert len(encoded) < 200_000
    assert len(encoded) < MAX_QUANT_V6_ARTIFACT_RAW_BYTES


def test_publication_service_has_no_execution_or_mutable_selection_imports() -> None:
    source = inspect.getsource(service_module)
    forbidden = (
        "BrokerGateway",
        "UniverseSelectionRun",
        "WatchlistItem",
        "WatchlistScore",
        "get_runner",
        "place_order",
        "submit_order",
    )
    assert all(token not in source for token in forbidden)
    assert "position_add_on_allowed" in source
