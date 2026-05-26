from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.core.notifiers.multi_channel import (
    MultiChannelNotifier,
    _severity_for_risk_event,
)


def _ch(success: bool = True) -> MagicMock:
    mock = MagicMock()
    mock.send.return_value = success
    return mock


def test_severity_floor_filters_below_threshold() -> None:
    sc, wh = _ch(), _ch()
    notifier = MultiChannelNotifier([(sc, "INFO"), (wh, "CRITICAL")])
    notifier.send("t", "c", severity="WARNING")
    sc.send.assert_called_once()
    wh.send.assert_not_called()


def test_critical_fans_out_to_all_channels() -> None:
    sc, wh = _ch(), _ch()
    notifier = MultiChannelNotifier([(sc, "INFO"), (wh, "CRITICAL")])
    assert notifier.send("t", "c", severity="CRITICAL") is True
    sc.send.assert_called_once()
    wh.send.assert_called_once()


def test_any_channel_success_returns_true() -> None:
    sc = _ch(success=False)
    wh = _ch(success=True)
    notifier = MultiChannelNotifier([(sc, "INFO"), (wh, "INFO")])
    assert notifier.send("t", "c", severity="INFO") is True


def test_all_channels_failed_returns_false_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    sc = _ch(success=False)
    wh = _ch(success=False)
    notifier = MultiChannelNotifier([(sc, "INFO"), (wh, "INFO")])
    assert notifier.send("t", "c", severity="INFO") is False
    assert "all notifier channels failed" in caplog.text


def test_channel_raises_does_not_break_others() -> None:
    bad = MagicMock()
    bad.send.side_effect = RuntimeError("boom")
    good = _ch(success=True)
    notifier = MultiChannelNotifier([(bad, "INFO"), (good, "INFO")])
    assert notifier.send("t", "c") is True
    good.send.assert_called_once()


def test_notify_risk_event_uses_default_severity_for_known_events() -> None:
    notifier = MultiChannelNotifier([(_ch(), "INFO")])
    sent: list[str] = []

    def capture_send(title: str, content: str, severity: str = "INFO") -> bool:
        sent.append(severity)
        return True

    notifier.send = capture_send  # type: ignore[method-assign]
    notifier.notify_risk_event("KILL_SWITCH", "panic")
    notifier.notify_risk_event("ORDER_PERSISTENCE_FAILED", "db down")
    notifier.notify_risk_event("ORDER_FAILED", "rejected")
    assert sent == ["CRITICAL", "CRITICAL", "WARNING"]


def test_notify_risk_event_explicit_severity_overrides_default() -> None:
    notifier = MultiChannelNotifier([(_ch(), "INFO")])
    sent: list[str] = []

    def capture_send(title: str, content: str, severity: str = "INFO") -> bool:
        sent.append(severity)
        return True

    notifier.send = capture_send  # type: ignore[method-assign]
    notifier.notify_risk_event("REJECTED", "x", severity="CRITICAL")
    assert sent == ["CRITICAL"]


def test_from_credential_config_invalid_json_falls_back_to_serverchan() -> None:
    cred = MagicMock(notification_channels="{not valid", sct_key="abc")
    notifier = MultiChannelNotifier.from_credential_config(cred)
    assert len(notifier._channels) == 1
    from app.core.notifiers.serverchan import ServerChanNotifier

    assert isinstance(notifier._channels[0][0], ServerChanNotifier)


def test_from_credential_config_builds_multiple_channels() -> None:
    import json

    cred = MagicMock(
        notification_channels=json.dumps(
            [
                {"type": "serverchan", "severity_floor": "INFO"},
                {"type": "webhook", "url": "https://x", "severity_floor": "WARNING"},
            ]
        ),
        sct_key="abc",
    )
    notifier = MultiChannelNotifier.from_credential_config(cred)
    assert len(notifier._channels) == 2
    assert notifier._channels[1][1] == "WARNING"


def test_severity_for_risk_event_mapping() -> None:
    assert _severity_for_risk_event("KILL_SWITCH") == "CRITICAL"
    assert _severity_for_risk_event("ORDER_PERSISTENCE_FAILED") == "CRITICAL"
    assert _severity_for_risk_event("ORDER_FAILED") == "WARNING"
