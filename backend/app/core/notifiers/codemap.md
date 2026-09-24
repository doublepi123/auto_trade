# backend/app/core/notifiers/

Notification fan-out subpackage: turns trading and risk events into operator
messages and delivers them across multiple channels with severity routing,
dedup, retry, and an audit sink.

## Responsibility

Deliver order / fill / risk-event notifications to ServerChan, Telegram, and
generic webhook channels. `MultiChannelNotifier` is the single composite
notifier the runner installs; individual channel notifiers are never used
directly by application code.

## Design

| File | Role |
|---|---|
| `__init__.py` | Re-exports `NotifierInterface`, `ServerChanNotifier`, `TelegramNotifier`, `WebhookNotifier`, `MultiChannelNotifier`. |
| `multi_channel.py` | `NotifierInterface` (Protocol: `send` / `notify_order` / `notify_fill` / `notify_risk_event`) and `MultiChannelNotifier` (fan-out, severity floors, dedup window, retry + sink hooks, `from_credential_config()` factory). |
| `_messages.py` | Message rendering (order / fill / risk titles & bodies) and `resolve_risk_severity()` — single source for risk-event severity mapping. |
| `serverchan.py` | `ServerChanNotifier` — posts to ServerChan (`sct_key`); `send()` returns bool, exceptions never escape. |
| `telegram.py` | `TelegramNotifier` — Bot API `sendMessage` (HTML-escaped title/content, 10s httpx timeout); empty token/chat_id → `False` without a request. |
| `webhook.py` | `WebhookNotifier` — POSTs a JSON payload built from an optional user template; placeholders restricted to an allowlist (`title`, `content`, `severity`, `timestamp`, `source`); invalid templates fall back to the fixed schema. URL must pass `url_safety.validate_webhook_url()`; uses a pinned `validated_httpx_client`. |
| `retry_queue.py` | `NotificationRetryQueue` — bounded in-memory deque (capacity 256) with exponential backoff (default 4 attempts, 2s initial → 60s cap) on a lazily-started daemon thread; exposes `drain()` and `metrics()` (`delivered` / `exhausted`). |

Key patterns:

- **Severity routing**: each channel carries a severity floor
  (`INFO | WARNING | CRITICAL`); `_dispatch()` skips channels whose floor
  exceeds the message severity.
- **Dedup window**: `send()` suppresses identical (title, content) SHA-256
  fingerprints within `dedup_window_seconds` — only for INFO/WARNING;
  CRITICAL always goes through. Suppressed count is tracked
  (`dedup_suppressed_total`), mirroring `RepeatedLogThrottle`'s
  "counted, not discarded" idiom.
- **Fail-open semantics**: notification must never break trading — every
  channel exception is caught and logged; `send()` returns `True` if *any*
  channel succeeded. On total failure the message is enqueued into the retry
  queue (when present).
- **Auditability**: an optional dispatch sink `(title, content, severity,
  success, error)` lets the runner persist a notification log; the sink itself
  is best-effort and swallows its own errors.
- **Construction from credentials**: `from_credential_config(cred, ...)` parses
  the credentials row's `notification_channels` JSON (type / severity_floor /
  url / template / bot_token+chat_id), skipping malformed entries and falling
  back to ServerChan when nothing valid remains.

## Flow

1. Caller invokes a typed method (`notify_order` / `notify_fill` /
   `notify_risk_event`) → `_messages` renders title/body and resolves severity.
2. `send()` applies the dedup window (skip if a fingerprint was delivered
   recently), else `_dispatch()`.
3. `_dispatch()` iterates channels above the severity floor; each
   `notifier.send()` result ORs into `success_any`; sink records the outcome.
4. All-failed → `retry_queue.enqueue(...)`; the worker thread retries via
   `MultiChannelNotifier.send_once()` (retry without re-enqueueing) with
   backoff until delivered or attempts exhausted.

## Integration

- Consumed by `runner.py` and notification services via the
  `app.core.notify` shim or direct import; credentials come from
  `CredentialsService` rows (`sct_key`, `notification_channels` JSON).
- The only cross-module dependency inside `core` is `app.core.url_safety`
  (webhook SSRF protection). No imports from `api` / `services` / `domain` /
  `platform`.
