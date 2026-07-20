from __future__ import annotations

from unittest.mock import MagicMock

from app.core.notifiers import multi_channel
from app.core.notifiers.multi_channel import MultiChannelNotifier


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _channel(success: bool = True) -> MagicMock:
    channel = MagicMock()
    channel.send.return_value = success
    return channel


def test_duplicate_within_window_is_suppressed_and_counted(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(multi_channel.time, "monotonic", clock)
    channel = _channel()
    notifier = MultiChannelNotifier([(channel, "INFO")], dedup_window_seconds=300.0)

    assert notifier.send("title", "content", "INFO") is True
    assert notifier.send("title", "content", "INFO") is True

    assert channel.send.call_count == 1
    assert notifier.dedup_suppressed_total == 1
    assert notifier.dedup_window_seconds == 300.0


def test_expired_duplicate_is_dispatched_again(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(multi_channel.time, "monotonic", clock)
    channel = _channel()
    notifier = MultiChannelNotifier([(channel, "INFO")], dedup_window_seconds=10.0)

    notifier.send("title", "content", "WARNING")
    clock.now += 10.0
    notifier.send("title", "content", "WARNING")

    assert channel.send.call_count == 2
    assert notifier.dedup_suppressed_total == 0


def test_critical_duplicates_always_dispatch(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(multi_channel.time, "monotonic", clock)
    channel = _channel()
    notifier = MultiChannelNotifier([(channel, "INFO")], dedup_window_seconds=300.0)

    notifier.send("title", "content", "CRITICAL")
    notifier.send("title", "content", "CRITICAL")

    assert channel.send.call_count == 2
    assert notifier.dedup_suppressed_total == 0


def test_failed_send_does_not_suppress_next_attempt(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(multi_channel.time, "monotonic", clock)
    channel = _channel(success=False)
    channel.send.side_effect = [False, True]
    notifier = MultiChannelNotifier([(channel, "INFO")], dedup_window_seconds=300.0)

    assert notifier.send("title", "content", "INFO") is False
    assert notifier.send("title", "content", "INFO") is True

    assert channel.send.call_count == 2
    assert notifier.dedup_suppressed_total == 0


def test_zero_window_preserves_repeated_dispatch(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(multi_channel.time, "monotonic", clock)
    channel = _channel()
    notifier = MultiChannelNotifier([(channel, "INFO")], dedup_window_seconds=0.0)

    notifier.send("title", "content", "INFO")
    notifier.send("title", "content", "INFO")

    assert channel.send.call_count == 2
    assert notifier.dedup_suppressed_total == 0


def test_suppressed_duplicate_does_not_write_sink(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(multi_channel.time, "monotonic", clock)
    channel = _channel()
    sink = MagicMock()
    notifier = MultiChannelNotifier(
        [(channel, "INFO")],
        sink=sink,
        dedup_window_seconds=300.0,
    )

    notifier.send("title", "content", "INFO")
    notifier.send("title", "content", "INFO")

    sink.assert_called_once_with("title", "content", "INFO", True, "")


def test_different_title_or_content_is_not_suppressed(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(multi_channel.time, "monotonic", clock)
    channel = _channel()
    notifier = MultiChannelNotifier([(channel, "INFO")], dedup_window_seconds=300.0)

    notifier.send("title-a", "content-a", "INFO")
    notifier.send("title-b", "content-a", "INFO")
    notifier.send("title-a", "content-b", "INFO")

    assert channel.send.call_count == 3
    assert notifier.dedup_suppressed_total == 0
