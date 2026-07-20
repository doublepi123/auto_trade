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
                {"type": "webhook", "url": "https://93.184.216.34/hook", "severity_floor": "WARNING"},
            ]
        ),
        sct_key="abc",
    )
    notifier = MultiChannelNotifier.from_credential_config(cred)
    assert len(notifier._channels) == 2
    assert notifier._channels[1][1] == "WARNING"


def test_from_credential_config_builds_telegram_with_severity_floor() -> None:
    import json

    from app.core.notifiers.telegram import TelegramNotifier

    cred = MagicMock(
        notification_channels=json.dumps(
            [
                {
                    "type": "telegram",
                    "bot_token": "123:ABC",
                    "chat_id": "-100123",
                    "severity_floor": "CRITICAL",
                }
            ]
        ),
        sct_key="",
    )
    notifier = MultiChannelNotifier.from_credential_config(cred)

    assert len(notifier._channels) == 1
    channel, floor = notifier._channels[0]
    assert isinstance(channel, TelegramNotifier)
    assert floor == "CRITICAL"


def test_telegram_factory_entry_honors_severity_floor(monkeypatch) -> None:
    import json

    from app.core.notifiers.telegram import TelegramNotifier

    sent: list[str] = []

    def fake_send(
        self: TelegramNotifier,
        title: str,
        content: str,
        severity: str = "INFO",
    ) -> bool:
        del self, title, content
        sent.append(severity)
        return True

    monkeypatch.setattr(TelegramNotifier, "send", fake_send)
    cred = MagicMock(
        notification_channels=json.dumps(
            [
                {
                    "type": "telegram",
                    "bot_token": "123:ABC",
                    "chat_id": "42",
                    "severity_floor": "WARNING",
                }
            ]
        ),
        sct_key="",
    )
    notifier = MultiChannelNotifier.from_credential_config(cred)

    assert notifier.send("info", "body", severity="INFO") is False
    assert notifier.send("warning", "body", severity="WARNING") is True
    assert sent == ["WARNING"]


def test_from_credential_config_skips_malformed_telegram_without_leaking_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import json

    token = "123:SECRET_TOKEN"
    cred = MagicMock(
        notification_channels=json.dumps(
            [
                {
                    "type": "telegram",
                    "bot_token": token,
                    "severity_floor": "INFO",
                }
            ]
        ),
        sct_key="",
    )

    with caplog.at_level("WARNING", logger="auto_trade.notify"):
        notifier = MultiChannelNotifier.from_credential_config(cred)

    assert token not in caplog.text
    assert "telegram" in caplog.text.lower()
    assert len(notifier._channels) == 1
    from app.core.notifiers.serverchan import ServerChanNotifier

    assert isinstance(notifier._channels[0][0], ServerChanNotifier)


def test_severity_for_risk_event_mapping() -> None:
    assert _severity_for_risk_event("KILL_SWITCH") == "CRITICAL"
    assert _severity_for_risk_event("ORDER_PERSISTENCE_FAILED") == "CRITICAL"
    assert _severity_for_risk_event("ORDER_FAILED") == "WARNING"


def test_webhook_template_substitutes_known_tokens():
    from app.core.notifiers.webhook import (
        _render_template,
        _validate_template,
    )

    template = '{"text": "[{severity}] {title}: {content}", "ts": "{timestamp}"}'
    validated = _validate_template(template)
    payload = _render_template(
        validated, title="ORDER", content="BUY 100 AAPL", severity="INFO"
    )
    assert payload["text"].startswith("[INFO] ORDER: BUY 100 AAPL")
    assert "ts" in payload


def test_webhook_template_rejects_unknown_token():
    from app.core.notifiers.webhook import _TemplateError, _validate_template

    bad = '{"leak": "{api_key}"}'
    with pytest.raises(_TemplateError):
        _validate_template(bad)


def test_webhook_template_must_be_json_object_shape():
    from app.core.notifiers.webhook import _TemplateError, _validate_template

    # Bare strings and arrays are valid JSON but useless as a webhook
    # payload. The validator must require the template to start with '{'
    # so the rendered output is unambiguously an object.
    with pytest.raises(_TemplateError):
        _validate_template('"hello"')
    with pytest.raises(_TemplateError):
        _validate_template("[1, 2, 3]")


def test_webhook_template_render_surfaces_json_errors():
    from app.core.notifiers.webhook import _render_template

    # Template that doesn't survive substitution (mismatched brace) should
    # raise so the notifier falls back to the default payload.
    with pytest.raises(Exception):
        _render_template('{"a": "}', title="x", content="y", severity="INFO")


def test_webhook_notifier_falls_back_when_template_invalid():
    from app.core.notifiers.webhook import WebhookNotifier

    # Template with unknown token must be rejected at construction.
    n = WebhookNotifier(
        "https://example.invalid/hook",
        template='{"x": "{api_key}"}',
    )
    assert n._template is None


def test_retry_queue_succeeds_on_second_attempt():
    from app.core.notifiers.retry_queue import NotificationRetryQueue

    calls: list[int] = []

    def flaky_send(title, content, severity):
        calls.append(1)
        return len(calls) >= 2  # fail the first time, succeed the second

    q = NotificationRetryQueue(flaky_send, initial_backoff=0.01, max_backoff=0.05)
    q.start()
    q.enqueue("title", "body", "WARNING")
    delivered = q.drain()
    q.stop()
    assert delivered == 1
    assert len(calls) >= 2


def test_retry_queue_exhausts_after_max_attempts():
    from app.core.notifiers.retry_queue import NotificationRetryQueue

    calls: list[int] = []

    def always_fail(title, content, severity):
        calls.append(1)
        return False

    q = NotificationRetryQueue(
        always_fail, max_attempts=3, initial_backoff=0.01, max_backoff=0.05
    )
    q.enqueue("t", "c", "INFO")
    q.drain()
    q.stop()
    assert len(calls) == 3
    metrics = q.metrics()
    assert metrics["exhausted"] == 1


def test_retry_queue_drops_when_full():
    from app.core.notifiers.retry_queue import NotificationRetryQueue

    def always_fail(title, content, severity):
        return False

    q = NotificationRetryQueue(always_fail, capacity=2, initial_backoff=0.01)
    assert q.enqueue("t1", "c", "INFO") is True
    assert q.enqueue("t2", "c", "INFO") is True
    assert q.enqueue("t3", "c", "INFO") is False
    q.stop()
    assert q.metrics()["dropped_capacity"] == 1


def test_multi_channel_enqueues_to_retry_on_full_failure():
    from app.core.notifiers.multi_channel import MultiChannelNotifier
    from app.core.notifiers.retry_queue import NotificationRetryQueue

    class FailingChannel:
        def __init__(self):
            self.attempts = 0

        def send(self, title, content, severity="INFO"):
            self.attempts += 1
            return False

        def notify_order(self, *args, **kwargs):
            return False

        def notify_fill(self, *args, **kwargs):
            return False

        def notify_risk_event(self, *args, **kwargs):
            return False

    captured = []

    def send_fn(title, content, severity):
        captured.append((title, content, severity))
        return True

    queue = NotificationRetryQueue(send_fn, initial_backoff=0.01)
    failing = FailingChannel()
    notifier = MultiChannelNotifier([(failing, "INFO")], retry_queue=queue)
    assert notifier.send("alert", "body", severity="CRITICAL") is False
    assert failing.attempts == 1
    # Drain should re-dispatch the failed message.
    delivered = queue.drain()
    queue.stop()
    assert delivered == 1
    assert captured == [("alert", "body", "CRITICAL")]
