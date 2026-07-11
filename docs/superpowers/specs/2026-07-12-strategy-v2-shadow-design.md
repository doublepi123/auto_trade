# P2: Strategy v2 RTH Mean-Reversion Shadow

## Objective

Run a forward-only, low-frequency intraday mean-reversion strategy beside the
P0 live interval strategy. P2 records hypothetical decisions and fills for
evaluation. It cannot submit, amend, or cancel broker orders and cannot mutate
the live engine, risk controller, position state, or strategy bounds.

## Market Data And Features

- Consume completed regular-hours 1-minute bars only. A bar is not observable
  until its end plus a small settlement grace period.
- Filter weekends, holidays, half days, and the HK lunch break through the
  exchange-aware market calendar.
- Aggregate complete 5-minute bars from the same 1-minute stream, anchored to
  each continuous RTH segment. Never bridge a session boundary or lunch break.
- Reset session VWAP on every exchange-local trading day. Use typical price
  `(high + low + close) / 3` weighted by volume.
- Define residual as `log(close / session_vwap)`.
- Standardize each current residual against the preceding window only. The
  current observation must not enter its own mean or sample standard deviation.
- Use 1-minute residual z-score for breach/reclaim and the latest completed
  5-minute residual z-score, Wilder ADX, and annualized 1-minute realized
  volatility as regime confirmation.
- Invalid OHLCV, non-positive volume, missing bars, insufficient history, and
  near-zero residual variance are explicit gate failures rather than silently
  fabricated values.

## State Machine

The strategy is long-only and follows:

`COLD -> READY -> ARMED_LONG -> ENTRY_PENDING -> LONG -> READY`

- `READY -> ARMED_LONG`: the 1-minute z-score breaches the configured lower
  threshold while every regime and session gate passes.
- `ARMED_LONG -> ENTRY_PENDING`: on a later completed bar, z-score crosses back
  above the reclaim threshold and confirmation still passes.
- `ENTRY_PENDING -> LONG`: the hypothetical entry fills at the next valid
  1-minute bar open. The signal bar can never fill its own action.
- While `LONG`, all new entry and add-on signals are ignored.
- An arm expires after a bounded number of bars, at the entry cutoff, on a
  regime failure, or at the session boundary.
- Each session has a maximum entry count and a post-exit cooldown.

## Deterministic Exits

At the virtual fill, freeze the stop, target, holding deadline, config version,
and entry VWAP. Evaluate each later bar conservatively:

1. Gap or intrabar stop.
2. End-of-day flatten window.
3. Profit target.
4. Maximum holding time.

If one bar touches stop and target, assume the stop happened first. A gap
through the stop fills at the bar open. P2 hard safety invariants are:

- Maximum holding: 60 minutes.
- Stop new entries: 45 minutes before the actual session close.
- Force virtual flat: 15 minutes before the actual session close.
- No add-ons, shorts, overnight positions, or real order submission.

## Persistence And API

Use dedicated Strategy v2 tables for configuration, latest durable state,
per-bar decisions, and completed virtual trades. Do not reuse real orders,
runtime position state, prompt experiments, or offline parameter experiments.

Every processed bar records its features, gates, state transition, decision,
and reason. A unique version/symbol/bar key makes polling and restart replay
idempotent. Virtual PnL records gross PnL, estimated fees, net PnL, MFE, MAE,
holding time, and exit cause; estimated fees must never be presented as broker
actuals.

The operational API exposes configuration, current status and gate counts,
paged decisions, and deterministic replay over supplied 1-minute bars. All
responses identify `mode=SHADOW` and `order_submission_allowed=false`.

## Acceptance

- Unit vectors prove VWAP session reset, causal z-score, complete 5-minute
  alignment, breach-before-reclaim, next-bar fills, regime boundaries, no
  add-ons, stop-first ambiguity handling, holding timeout, and EOD flatten.
- Restarting or polling the same bar does not duplicate a decision or trade.
- Updating tunables cannot loosen the hard `60/45/15` safety windows or expose
  a live mode.
- A shadow tick has no dependency or call path to `TradeExecutionService` or
  broker order APIs.
- Backend tests and type checks, frontend type/build, Cypress, Docker health,
  API replay, and forward shadow collection all pass before P2 is enabled on
  the deployed service.

P2 remains observational. Promotion criteria and live canary sizing belong to
P3/P4 after at least 20 trading days and 50 closed shadow trades.
