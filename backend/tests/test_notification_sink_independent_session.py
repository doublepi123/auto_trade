"""The notification sink owns its session on purpose, and must say so.

``_alert_rules_cron`` holds one ``Session`` across
``AlertRuleService.evaluate``, which reaches
``MultiChannelNotifier.send -> _dispatch -> NotificationLogSink.record``, and
that sink opens a SECOND session. Under the pool's definition that is
re-entrancy, and of the twelve warning blocks observed in production it was the
only one that was real -- the other eleven were the thread-identity
misattribution fixed in the commit before this one.

The verdict here is deliberate independence, the same class as
``AuditLogger.record``. A delivery record must survive the rollback of the
alert evaluation that triggered it: borrowing the cron's session would make the
"we notified you" row vanish the moment a later rule in the same loop raised
and rolled the evaluation back. Notification dispatch has already happened by
then -- the user's phone buzzed -- so a log that disappears with the
transaction is a log that lies.

So the fix is to DECLARE it at the site through ``independent_session`` with a
written reason, not to convert it to borrowing (which would be a product
change) and not to add it to an allowlist (which is the "silence it" move the
guard's contract exists to prevent). The scope is kept to the session-owning
region only, so an unmarked nesting introduced next to it still reports --
which the second test here pins.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import (
    SessionReentrancyGuard,
    SessionReentrancyViolation,
)
from app.services.notification_log_service import NotificationLogSink


def _queue_pooled_engine(tmp_path, name: str) -> Engine:
    """File-backed SQLite, which SQLAlchemy serves with ``QueuePool``.

    ``sqlite://`` is served by ``SingletonThreadPool`` and emits no second
    checkout at all, so it cannot exercise this at all.
    """
    return create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False},
        pool_size=5,
        max_overflow=10,
        pool_timeout=10,
    )


@pytest.fixture
def _test_env(monkeypatch: pytest.MonkeyPatch):
    """Pin ``settings.env`` to ``test`` so a strict guard is in raising mode."""
    monkeypatch.setattr(settings, "env", "test")
    return None


def _sink_against(engine: Engine) -> tuple[NotificationLogSink, sessionmaker]:
    from app.models import Base

    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    return NotificationLogSink(factory), factory


def test_notification_sink_under_a_held_session_does_not_report(
    tmp_path,
    _test_env,
) -> None:
    """The real cron shape: a held session, then the sink opening its own.

    Mirrors ``main.py:_alert_rules_cron`` holding ``db`` across
    ``AlertRuleService.evaluate`` down to
    ``NotificationLogSink.record``. Strict + ``env == "test"``, so before the
    annotation this raises ``SessionReentrancyViolation`` out of ``record``'s
    own ``db.add`` path.
    """
    engine = _queue_pooled_engine(tmp_path, "sink_annotated.db")
    guard = SessionReentrancyGuard(strict=True)
    guard.install(engine)
    sink, factory = _sink_against(engine)

    cron_db: Session = factory()
    try:
        cron_db.execute(text("SELECT 1"))
        sink.record(
            title="告警 · range breach",
            content="price crossed the configured threshold",
            severity="WARNING",
            success=True,
        )
    finally:
        cron_db.close()
        engine.dispose()

    assert guard.violation_count == 0, (
        "the notification sink's deliberate independence must be declared at "
        "the site, not reported as an incident"
    )


def test_notification_sink_actually_persists_independently(
    tmp_path,
    _test_env,
) -> None:
    """Declaring independence must not change what the sink does.

    The row has to be committed on the sink's own session and therefore
    survive a rollback of the caller's transaction -- that survival is the
    entire reason the site owns a session instead of borrowing one.
    """
    from app.models import NotificationLog

    engine = _queue_pooled_engine(tmp_path, "sink_persists.db")
    guard = SessionReentrancyGuard(strict=True)
    guard.install(engine)
    sink, factory = _sink_against(engine)

    cron_db: Session = factory()
    try:
        cron_db.execute(text("SELECT 1"))
        sink.record(
            title="告警 · delivered",
            content="the user's phone already buzzed",
            severity="WARNING",
            success=True,
        )
        cron_db.rollback()
    finally:
        cron_db.close()

    verify: Session = factory()
    try:
        titles = [row.title for row in verify.query(NotificationLog).all()]
    finally:
        verify.close()
        engine.dispose()

    assert titles == ["告警 · delivered"], (
        "the delivery record must outlive the rollback of the evaluation that "
        "triggered it"
    )
    assert guard.violation_count == 0


def test_an_unannotated_nesting_beside_the_sink_still_reports(
    tmp_path,
    _test_env,
) -> None:
    """ANTI-BLUNTING. The waiver covers the sink's block and nothing more.

    A different helper opening its own session under the same held cron
    session, a moment after the sink returned, is undeclared re-entrancy and
    must still raise. If the annotation had been placed around
    ``evaluate`` -- or worse, expressed as an allowlist -- this would go quiet
    and the guard would be blind to exactly the shape that hung production for
    65 minutes.
    """
    engine = _queue_pooled_engine(tmp_path, "sink_scope_tight.db")
    guard = SessionReentrancyGuard(strict=True)
    guard.install(engine)
    sink, factory = _sink_against(engine)

    def undeclared_helper() -> Generator[None, None, None]:
        own = factory()
        try:
            own.execute(text("SELECT 1"))
        finally:
            own.close()
        yield

    cron_db: Session = factory()
    try:
        cron_db.execute(text("SELECT 1"))
        sink.record(
            title="告警 · first",
            content="declared",
            severity="WARNING",
            success=True,
        )
        with pytest.raises(SessionReentrancyViolation):
            next(undeclared_helper())
    finally:
        cron_db.close()
        engine.dispose()

    assert guard.violation_count == 1
