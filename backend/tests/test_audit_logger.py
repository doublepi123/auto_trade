from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.audit import AuditLogger
from app.models import AuditLog, Base


@pytest.fixture
def logger(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'audit_logger.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield AuditLogger(session_factory)
    Base.metadata.drop_all(bind=engine)


def test_record_writes_row(logger):
    logger.record(
        "START",
        severity="INFO",
        actor_hash="abc",
        source_ip="10.0.0.1",
        request_summary={"reason": "manual"},
        result="SUCCESS",
    )
    with logger._session_factory() as db:
        rows = db.query(AuditLog).all()
    assert len(rows) == 1
    assert rows[0].action == "START"
    assert rows[0].severity == "INFO"
    assert rows[0].actor_hash == "abc"
    assert rows[0].source_ip == "10.0.0.1"
    assert json.loads(rows[0].request_summary) == {"reason": "manual"}
    assert rows[0].result == "SUCCESS"


def test_record_dict_request_summary_is_jsonified(logger):
    logger.record("STOP", request_summary={"a": 1})
    with logger._session_factory() as db:
        row = db.query(AuditLog).one()
    assert json.loads(row.request_summary) == {"a": 1}


def test_record_truncates_large_summary(logger, monkeypatch):
    monkeypatch.setattr("app.core.audit.settings.audit_request_summary_limit", 64)
    big = {"k": "x" * 1000}
    logger.record("STRATEGY_UPDATE", request_summary=big)
    with logger._session_factory() as db:
        row = db.query(AuditLog).one()
    assert len(row.request_summary.encode("utf-8")) <= 64 + len("...truncated")
    assert row.request_summary.endswith("...truncated")


def test_hash_actor_consistent_and_anonymous_when_missing():
    h1 = AuditLogger.hash_actor("secret-key-123")
    h2 = AuditLogger.hash_actor("secret-key-123")
    assert h1 == h2
    assert len(h1) == 32
    assert AuditLogger.hash_actor(None) == "anonymous"
    assert AuditLogger.hash_actor("") == "anonymous"


def test_extract_ip_prefers_x_forwarded_for():
    from starlette.requests import Request

    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"203.0.113.5, 10.0.0.1")],
        "client": ("127.0.0.1", 12345),
    }
    req = Request(scope)
    assert AuditLogger.extract_ip(req) == "203.0.113.5"


def test_extract_ip_falls_back_to_client_host():
    from starlette.requests import Request

    scope = {"type": "http", "headers": [], "client": ("198.51.100.7", 9999)}
    req = Request(scope)
    assert AuditLogger.extract_ip(req) == "198.51.100.7"


def test_record_swallows_write_errors(monkeypatch, caplog):
    def broken_session():
        raise RuntimeError("db gone")

    bad_logger = AuditLogger(broken_session)
    bad_logger.record("PAUSE")
    assert "audit write failed" in caplog.text.lower()


def test_extract_actor_returns_hash_and_ip():
    from app.api.deps import extract_actor
    from starlette.requests import Request

    scope = {
        "type": "http",
        "headers": [(b"x-api-key", b"key-abc"), (b"x-forwarded-for", b"203.0.113.5")],
        "client": ("127.0.0.1", 12345),
    }
    actor, ip = extract_actor(Request(scope))
    assert actor == AuditLogger.hash_actor("key-abc")
    assert ip == "203.0.113.5"


def test_extract_actor_anonymous_when_no_header():
    from app.api.deps import extract_actor
    from starlette.requests import Request

    scope = {"type": "http", "headers": [], "client": ("127.0.0.1", 999)}
    actor, ip = extract_actor(Request(scope))
    assert actor == "anonymous"
    assert ip == "127.0.0.1"
