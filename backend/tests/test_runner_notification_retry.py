from __future__ import annotations

import pytest

from app import runner as runner_module
from app.core.notifiers import serverchan
from app.core.notifiers.retry_queue import NotificationRetryQueue
from app.core.notifiers.serverchan import ServerChanNotifier
from app.runner import AppRunner
from app.services.credentials_service import PlainCredentials


class _FakeChannel(ServerChanNotifier):
    def __init__(self, sct_key: str = "") -> None:
        super().__init__(sct_key)
        self.calls: list[tuple[str, str, str]] = []
        self.recovers = False
        self.closed = False

    def send(self, title: str, content: str, severity: str = "INFO") -> bool:
        assert not self.closed
        self.calls.append((title, content, severity))
        return self.recovers

    def close(self) -> None:
        self.closed = True


class _FakeSink:
    def __init__(self) -> None:
        self.results: list[bool] = []

    def record(
        self, title: str, content: str, severity: str, success: bool, error: str,
    ) -> None:
        self.results.append(success)


class _FakeBroker:
    def close(self) -> None:
        return None


def _build_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AppRunner, list[_FakeChannel], _FakeSink, list[NotificationRetryQueue]]:
    channels: list[_FakeChannel] = []
    queues: list[NotificationRetryQueue] = []
    sink = _FakeSink()

    def channel_factory(sct_key: str) -> _FakeChannel:
        channel = _FakeChannel(sct_key)
        channels.append(channel)
        return channel

    def capture_worker(queue: NotificationRetryQueue) -> None:
        # Drive the real due-task methods below instead of starting a thread.
        if queue not in queues:
            queues.append(queue)

    monkeypatch.setattr(runner_module, "ServerChanNotifier", channel_factory)
    monkeypatch.setattr(serverchan, "ServerChanNotifier", channel_factory)
    monkeypatch.setattr(runner_module, "get_notification_sink", lambda: sink)
    monkeypatch.setattr(AppRunner, "_build_broker", staticmethod(lambda _audit: _FakeBroker()))
    monkeypatch.setattr(AppRunner, "_load_credentials", lambda _self: PlainCredentials())
    monkeypatch.setattr(NotificationRetryQueue, "_ensure_worker", capture_worker)
    return AppRunner(), channels, sink, queues


def _run_due_tasks(queues: list[NotificationRetryQueue]) -> None:
    for queue in queues:
        pending = queue._pop_due(float("inf"))
        if pending is not None:
            queue._attempt(pending)


def test_runner_retries_failed_critical_notification_after_channel_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, channels, sink, queues = _build_runner(monkeypatch)
    try:
        assert runner.notifier.notify_risk_event(
            "POSITION_RECONCILIATION_FAILED", "broker unavailable", severity="CRITICAL",
        ) is False
        # A queued message must follow refreshed credentials, not a closed channel.
        runner.reload_credentials(broker_identity_change=False)
        channels[-1].recovers = True
        _run_due_tasks(queues)

        calls = [call for channel in channels for call in channel.calls]
        assert len(calls) == 2, "no second delivery happened"
        assert calls[0] == calls[1]
        assert calls[0][2] == "CRITICAL"
        assert sink.results == [False, True]
        assert len(channels[0].calls) == len(channels[-1].calls) == 1
        assert len(queues) == 1
        assert queues[0].pending_count() == 0
        assert queues[0].metrics()["delivered"] == 1
    finally:
        runner.stop()
        runner.notifier.close()
    assert queues[0]._stop_event.is_set()


def test_runner_notification_retry_exhausts_without_reenqueueing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, channels, sink, queues = _build_runner(monkeypatch)
    try:
        # Startup applies credentials through this same production method.
        runner._apply_credentials(PlainCredentials(), resubscribe=False)
        assert runner.notifier.notify_risk_event(
            "POSITION_RECONCILIATION_FAILED", "broker unavailable", severity="CRITICAL",
        ) is False
        _run_due_tasks(queues)
        assert len(channels[-1].calls) == 2, "no second delivery happened"
        for _ in range(10):
            _run_due_tasks(queues)
            assert sum(queue.pending_count() for queue in queues) <= 1

        assert len(channels[-1].calls) == 5
        assert channels[-1].calls == [channels[-1].calls[0]] * 5
        assert sink.results == [False] * 5
        assert len(queues) == 1
        assert queues[0].pending_count() == 0
        assert queues[0].metrics() == {
            "enqueued": 1, "dropped_capacity": 0, "delivered": 0, "exhausted": 1,
        }
    finally:
        runner.stop()
        runner.notifier.close()
    assert queues[0]._stop_event.is_set()
