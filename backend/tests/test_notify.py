from unittest.mock import MagicMock, patch

from app.core.notify import ServerChanNotifier


class TestServerChanNotifier:
    def test_send_without_key(self) -> None:
        notifier = ServerChanNotifier("")
        assert notifier.send("test", "content") is False

    @patch("app.core.notify.httpx")
    def test_send_success(self, mock_httpx: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_httpx.post.return_value = mock_resp

        notifier = ServerChanNotifier("test_key")
        result = notifier.send("hello", "world")

        assert result is True
        mock_httpx.post.assert_called_once()

    @patch("app.core.notify.httpx")
    def test_notify_order(self, mock_httpx: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_httpx.post.return_value = mock_resp

        notifier = ServerChanNotifier("test_key")
        result = notifier.notify_order("BUY", "AAPL.US", "10", "150.0", "order-123")

        assert result is True
        call_args = mock_httpx.post.call_args[1]["data"]
        assert "[Auto Trade] BUY" in call_args["title"]

    @patch("app.core.notify.httpx")
    def test_notify_risk_event(self, mock_httpx: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_httpx.post.return_value = mock_resp

        notifier = ServerChanNotifier("test_key")
        result = notifier.notify_risk_event("DAILY_LOSS", "exceeded limit")

        assert result is True
        call_args = mock_httpx.post.call_args[1]["data"]
        assert "DAILY_LOSS" in call_args["title"]

    @patch("app.core.notify.httpx")
    def test_send_http_error(self, mock_httpx: MagicMock) -> None:
        mock_httpx.post.side_effect = Exception("connection refused")

        notifier = ServerChanNotifier("test_key")
        result = notifier.send("hello", "world")

        assert result is False

    @patch("app.core.notify.httpx")
    def test_notify_fill(self, mock_httpx: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_httpx.post.return_value = mock_resp

        notifier = ServerChanNotifier("test_key")
        result = notifier.notify_fill("AAPL.US", "BUY", "10", "150.0")

        assert result is True
        call_args = mock_httpx.post.call_args[1]["data"]
        assert "Order Filled" in call_args["title"]

    def test_send_disabled_when_key_empty(self) -> None:
        notifier = ServerChanNotifier("")
        assert notifier.notify_order("BUY", "AAPL.US", "10", "150.0", "id") is False
        assert notifier.notify_risk_event("X", "y") is False
        assert notifier.notify_fill("A", "B", "1", "2") is False
