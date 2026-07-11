from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.audit import AuditLogger
from app.config import settings
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
    # X-Forwarded-For is attacker-controlled in the current deployment (no
    # reverse proxy in front of the backend). Audit must use the real socket
    # peer instead. This test pins the secure behavior.
    from starlette.requests import Request

    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"203.0.113.5, 10.0.0.1")],
        "client": ("127.0.0.1", 12345),
    }
    req = Request(scope)
    assert AuditLogger.extract_ip(req) == "127.0.0.1"


def test_extract_ip_falls_back_to_client_host():
    from starlette.requests import Request

    scope = {"type": "http", "headers": [], "client": ("198.51.100.7", 9999)}
    req = Request(scope)
    assert AuditLogger.extract_ip(req) == "198.51.100.7"


def test_extract_ip_accepts_x_real_ip_only_from_trusted_proxy(monkeypatch):
    from starlette.requests import Request

    monkeypatch.setattr(settings, "audit_trusted_proxy_cidrs", "172.16.0.0/12")
    trusted = Request({
        "type": "http",
        "headers": [(b"x-real-ip", b"203.0.113.8")],
        "client": ("172.18.0.3", 12345),
    })
    untrusted = Request({
        "type": "http",
        "headers": [(b"x-real-ip", b"203.0.113.8")],
        "client": ("198.51.100.7", 12345),
    })

    assert AuditLogger.extract_ip(trusted) == "203.0.113.8"
    assert AuditLogger.extract_ip(untrusted) == "198.51.100.7"


def test_extract_ip_rejects_invalid_forwarded_value(monkeypatch):
    from starlette.requests import Request

    monkeypatch.setattr(settings, "audit_trusted_proxy_cidrs", "172.16.0.0/12")
    request = Request({
        "type": "http",
        "headers": [(b"x-real-ip", b"not-an-ip")],
        "client": ("172.18.0.3", 12345),
    })

    assert AuditLogger.extract_ip(request) == "172.18.0.3"


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
    # Audit uses the real socket peer, not the (attacker-controlled) XFF header.
    assert ip == "127.0.0.1"


def test_extract_actor_anonymous_when_no_header():
    from app.api.deps import extract_actor
    from starlette.requests import Request

    scope = {"type": "http", "headers": [], "client": ("127.0.0.1", 999)}
    actor, ip = extract_actor(Request(scope))
    assert actor == "anonymous"
    assert ip == "127.0.0.1"


def test_normalize_summary_truncates_multibyte_safely(logger):
    """Long summaries should be truncated at the byte limit without splitting
    a multi-byte UTF-8 sequence. Regression test for the O(n^2) re-encode
    loop that the previous implementation used.
    """
    # Build a string that exceeds the configured limit. Use a mix of ASCII
    # and 3-byte CJK characters so the truncation point falls in the middle
    # of a multi-byte sequence.
    big = "中" * 5000
    truncated = logger._normalize_summary(big)
    assert len(truncated.encode("utf-8")) <= settings.audit_request_summary_limit
    assert truncated.endswith("...truncated")
    # The first character must survive (no off-by-one trimming).
    assert truncated.startswith("中")


def test_export_audit_logs_csv_includes_all_fields():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.deps import get_audit_logger
    from app import database

    database.init_db()
    client = TestClient(app)
    audit = get_audit_logger()
    audit.record("EXPORT_TEST_START", severity="INFO", actor_hash="actor-1", source_ip="10.0.0.1")
    audit.record("EXPORT_TEST_PAUSE", severity="WARNING", actor_hash="actor-2", source_ip="10.0.0.2")
    resp = client.get("/api/audit-logs/export?format=csv&limit=1000")
    assert resp.status_code == 200
    body = resp.text
    assert "id,action,severity" in body
    assert "EXPORT_TEST_START" in body
    assert "EXPORT_TEST_PAUSE" in body


def test_export_audit_logs_json_filter_by_action():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.deps import get_audit_logger
    from app import database

    database.init_db()
    client = TestClient(app)
    audit = get_audit_logger()
    audit.record("EXPORT_STRATEGY_UPDATE", severity="INFO", actor_hash="x")
    audit.record("EXPORT_CREDENTIALS_UPDATE", severity="INFO", actor_hash="x")
    resp = client.get(
        "/api/audit-logs/export?format=json&action=EXPORT_STRATEGY_UPDATE&limit=1000"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert all(row["action"] == "EXPORT_STRATEGY_UPDATE" for row in data)


def test_export_audit_logs_severity_filter():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.deps import get_audit_logger
    from app import database

    database.init_db()
    client = TestClient(app)
    audit = get_audit_logger()
    audit.record("EXPORT_KILL", severity="CRITICAL", actor_hash="x")
    audit.record("EXPORT_PAUSE", severity="WARNING", actor_hash="x")
    resp = client.get(
        "/api/audit-logs/export?format=json&severity=critical&limit=1000"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert all(row["severity"] == "CRITICAL" for row in data)
