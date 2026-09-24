# backend/app/core/

Foundational layer of the backend: the live range-trading state machine, the risk
controller, the Longbridge broker gateway, exit/fee/board-lot computations, exchange
calendars, backtesting, audit logging, credential crypto, and process-liveness
protection. Everything on the live order path that is neither a service, an API
route, nor research computation lives here.

## Responsibility

- **Live trading primitives**: `StrategyEngine` (FLAT/LONG/SHORT range state
  machine), `RiskController` (daily loss / drawdown / consecutive losses / kill
  switch / pause), `BrokerGateway` (the only network I/O to Longbridge).
- **Market clock**: exchange-local trade-day cut (`market_calendar`) plus static
  NYSE/HKEX holiday tables 2024–2027 (`holiday_calendar`). Drives PnL/risk day
  rollover, RTH checks, and session-scoped board-lot validity.
- **Exit economics**: fee-adjusted round-trip edge (fee guard), exit policy
  (price stop / time stop / EOD flatten / profit lock), reference-price selection
  and degraded exit pricing, HK board-lot quantization.
- **Operational safety net**: audit logging, credential encryption, notification
  fan-out (`notifiers/`), log throttling, event-loop liveness watchdog, webhook
  URL SSRF protection.
- **Offline simulation**: `BacktestEngine` and its sweep / walk-forward /
  stress-test drivers.

## Design

### Per-file map

| File | Role |
|---|---|
| `__init__.py` | Empty (namespace marker). |
| `engine.py` | `StrategyEngine` + `StrategyParams` / `TriggerResult` / frozen `EngineSnapshot`. Thread-safe (own `threading.Lock`). |
| `risk.py` | `RiskController`, `RiskConfig`, `TradingState` enum, `DailyLossSnapshot`; idempotent settlement keys; protective-exit permission guard. |
| `broker.py` | `BrokerGateway` (quotes, depth/BBO cache, candlesticks, orders, positions, cash/margin/account) + dataclasses (`Quote`, `BrokerOrder`, `Position`, …) and payload normalizers. |
| `audit.py` | `AuditLogger` — sole `audit_logs` writer; `hash_actor()` (SHA-256, 16 hex), `extract_ip()` with trusted-proxy check. |
| `fees.py` | Decimal round-trip fee/edge/reward-risk evaluation (`LongRoundTripEdge`, `LongRoundTripRewardRisk`). |
| `exit_policy.py` | `evaluate_exit_policy()` → `ReductionDecision`; `ReductionCause`: DAILY_LOSS / PRICE_STOP / EOD_FLATTEN / TIME_STOP / PROFIT_LOCK. |
| `exit_pricing.py` | `select_reference_price()` / `degraded_exit_limit()`; quote-source timestamp parsing. |
| `board_lot.py` | `quantize_to_board_lot()` (`q = L·⌊qty/L⌋`, never up) + session-scoped `BoardLotCache`. |
| `market_calendar.py` | `MarketSession` (US/HK RTH incl. HK lunch break), `trade_day_for()`, `is_trading_hours()`, `session_status()`, `next_session_open()`. |
| `holiday_calendar.py` | Flat tuples of NYSE/HKEX closures and half-days 2024–2027, O(1) lookup, coverage-expiry detection. |
| `execution_session.py` | `resolve_execution_session()` → `ExecutionPhase` (RTH/PRE/POST/UNAVAILABLE/UNKNOWN) independent of entry policy. |
| `backtest.py` | `BacktestEngine.run()` (entry/exit replay with fees, slippage, daily-loss reduction intents, fee sensitivity), `sweep_backtest()`, `walk_forward_backtest()`, `stress_test()`, `parse_backtest_csv()`. |
| `credential_crypto.py` | Hybrid secret encryption: random AES-256-GCM data key wrapped with RSA-OAEP-SHA256; optional PBKDF2 (600k iters) KEK from `CREDENTIAL_MASTER_KEY`; `CredentialIntegrityError` on tamper. |
| `liveness.py` | `LivenessWatchdog`: asyncio heartbeat + independent daemon thread; dumps thread tracebacks, then `os._exit` after hard threshold so Docker can restart. |
| `log_throttle.py` | `RepeatedLogThrottle` (keyed window; suppressed counts reported, not dropped) + `HealthcheckAccessFilter` (drops uvicorn access lines for `/api/ready`, `/api/live` GETs). |
| `position_probe.py` | Subprocess entry point (`python -m app.core.position_probe`) that snapshots positions in an isolated process; writes a length-prefixed JSON protocol payload. |
| `position_probe_diagnostics.py` | Probe error taxonomy (`PositionProbe{Runtime,Connection,Timeout,Protocol}Error`) with redacted diagnostics payloads. |
| `url_safety.py` | `validate_webhook_url()` (blocks cloud-metadata and loopback/link-local targets, incl. IPv6-mapped) + `validated_httpx_client()` with IP-pinned transport. |
| `notify.py` | Backwards-compat re-export shim for `notifiers/` (`MultiChannelNotifier`, `NotifierInterface`, `ServerChanNotifier`). |
| `notifiers/` | Notification subpackage — see `notifiers/codemap.md`. |

### Key patterns

- **Engine state machine** (`engine.py`): edge-triggered long-entry rearm latch —
  once LONG, a new entry requires price to reclaim `buy_low` first; a quote at or
  below the *long entry floor* (`buy_low·(1 − stop_loss_pct/100)`) latches rearm
  without buying (range invalidated). Post-trigger cooldown uses
  `time.monotonic()`; `EngineSnapshot` deliberately excludes the monotonic timer
  so a restore cannot resurrect a stale cooldown or create a retry loop. SHORT
  never adds; LONG add-ons are parameter-gated. `transition_for_action()` is the
  only committed state transition path (returns `"OK"` or a status string).
- **Tiered broker retry** (`broker.py`): `_call_with_retry` retries only
  classified-transient failures — network exceptions (`OSError`,
  `ConnectionError`, `TimeoutError`) always; SDK exceptions only when the message
  matches rate-limit/transient markers (English and Chinese, e.g. 限流/频率).
  Exponential backoff `base_ms·2^attempt`, each retry audited as `BROKER_RETRY`.
  Quote/depth WS pushes merge last-trade price with a ≤30s BBO cache; callback
  exceptions are logged, never propagated. Disconnect hooks run on a daemon
  thread so SDK callback threading cannot deadlock the protective-submission
  guard. `get_positions()` optionally runs in an isolated subprocess probe.
- **Derived `TradingState`** (`risk.py`): `ACTIVE | REDUCING | HALTED` is
  *computed* from existing conditions (kill switch → HALTED; paused, entry
  reconciliation, or any limit hit → REDUCING; else ACTIVE), never persisted, so
  it cannot drift. REDUCING rejects position increases but still permits
  reductions/exits. Trade-day rollover resets only day-scoped counters and never
  clears pause/kill latches. Two separate RLocks (`_lock`,
  `_protective_submission_lock`) avoid a risk-lock → runner-lock inversion.
- **Audit failures swallowed by design** (`audit.py`): `record()` catches all
  exceptions and logs a warning — an audit write must never fail the operation it
  audits. Rows use their own `independent_session` (imported inside the call so
  module scope stays database-free) so they survive rollback of the audited
  transaction. Summaries get single-shot UTF-8-safe truncation.
- **Fee guard** (`fees.py`): non-loss exits require fee-adjusted
  `net_profit ≥ required_profit` (`max(min_profit_amount, min_profit_pct)`);
  all money math in `Decimal`.
- **Session-scoped metadata** (`board_lot.py`, `market_calendar.py`): lot
  validity expires at the exchange-local session boundary; stale/unknown lot
  metadata blocks entries downstream but never blocks proven reductions.
- **Liveness as last resort** (`liveness.py`): the watchdog thread touches no
  asyncio, broker FFI, DB, or normal logging on its hard-exit path.

## Flow

### Quote → trigger (engine hot path)

1. Broker WS push → `BrokerGateway._on_quote_push` / `_on_depth_push`: merge
   last-trade price with cached BBO, build a `Quote`, fan out to callbacks
   (exceptions logged per callback).
2. Runner `_evaluate_quote_trigger` (under runner `_state_lock`) snapshots the
   engine and calls `StrategyEngine.update_price(price)`.
3. `_update_price_locked`: reject non-positive prices; require a configured band
   (`0 < buy_low < sell_high`); honor the rearm latch and cooldown; then the
   state machine — FLAT + `price ≤ buy_low` → `BUY` (unless below the entry
   floor → rearm latch); FLAT + short-selling + `price ≥ sell_high` →
   `SELL_SHORT`; LONG + `price ≥ sell_high` → `SELL`; LONG + add-ons enabled +
   `price ≤ buy_low` → `BUY`; SHORT + `price ≤ buy_low` → `BUY_TO_COVER`.
   **P0 note:** the engine can still emit `SELL_SHORT` when `short_selling` is set,
   but `TradeExecutionService` rejects it (`short_entries_enabled` defaults to
   `False`; `allow_short_entries` is a compatibility field only), so live short
   entries never reach the broker.
4. `TriggerResult{triggered, action, description}` → runner
   `_execute_triggered_order` → services-layer
   `TradeExecutionService.pre_submit_risk_check()` (the single pre-submit
   boundary) → `transition_for_action()` commits the engine transition.

### Risk check

1. `check_with_daily_loss_snapshot()` rolls the exchange-local trade day first,
   then evaluates kill switch → paused (with `pause_reason`) → post-fill entry
   reconciliation → `_check_limits()` (daily loss, drawdown =
   `peak_realized − cumulative_realized`, consecutive losses), returning the
   `RiskResult` plus a frozen `DailyLossSnapshot` atomically.
2. `trading_state()` derives ACTIVE/REDUCING/HALTED for consumers that only need
   the permission level.
3. `record_trade(pnl)` updates counters; `consume_settlement(key)` makes
   per-trade PnL settlement idempotent across replays/reconciles.
4. `permit_protective_exits()` / `protective_submission_guard()` grant an
   exit-only window while entries remain blocked.

### Broker call retry

1. Gateway method → `_call_with_retry(fn, op, max_retries, base_ms)`; candle
   methods go through `_call_candlesticks_with_retry` which additionally
   tolerates persistent incomplete payloads via a 256-entry fingerprint cache.
2. On a retryable exception: sleep `base_ms·2^attempt`, audit `BROKER_RETRY`,
   retry; after `max_retries` the exception propagates to the caller.
3. `_init_clients()` initializes quote and trade contexts independently so a
   transient TradeContext failure cannot leave a permanently tradeless gateway.

## Integration

- **`core` imports nothing upward**: module scope uses only stdlib, third-party,
  and layer-neutral `app.config` / `app.models`; `app.database` is imported only
  inside `AuditLogger.record` (call time), and `notifiers/` reaches only
  `core.url_safety`. No imports from `api`, `services`, `domain`, or `platform`.
- **Consumers**: `runner.py` (quote hot path, reconcile loops, liveness, notify
  wiring), `services/` (~40 references — trade execution, PnL, credentials,
  shadows), `api/` (~27 references — diagnostics, auth/audit), `domain/` (~26
  references via its allowed `core` edge). `TradeExecutionService` composes
  `RiskController`, `BrokerGateway`, `fees`, `exit_policy`, and `board_lot`
  behind `pre_submit_risk_check()`.
- **Cross-layer contracts**: `TradingState` semantics (REDUCING still permits
  reductions) are enforced by the services layer; audit-failure swallowing and
  the board-lot "block entries, never block proven reductions" invariant are
  pinned by tests in `backend/tests/`.
