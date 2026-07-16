# P2.3 Strategy v2 Same-Sample ADX Challenger Plan

## Evidence

The deployed paper service on `192.168.31.143` is healthy and flat. The
corrected 2026-07-16 session PnL is `-69.28773115` across three losing round
trips. The durable re-entry latch and the daily loss gate both prevent another
P0 entry. Strategy v2 evidence version `c44...` has a contiguous partial
session, but only about 13 gate-eligible bars and no breach, entry, or closed
trade. Its first 139 decisions are expected ADX(14) warm-up, not missing data.

This is not enough evidence to loosen the active gate. The next iteration must
make candidate comparisons reproducible before it changes strategy behavior.

## External Basis

- [LEAN warm-up](https://www.quantconnect.com/docs/v2/writing-algorithms/historical-data/warm-up-periods)
  replays historical bars to prime state and prohibits orders during warm-up.
- [Longbridge historical candlesticks](https://open.longbridge.com/docs/quote/pull/history-candlestick)
  support minute bars and offset requests of up to 1,000 bars.
- [Freqtrade strategy customization](https://www.freqtrade.io/en/stable/strategy-customization/)
  uses crossing events plus trend, band, and volume guards rather than buying
  continuously below a threshold.
- [TA-Lib momentum indicators](https://ta-lib.github.io/ta-lib-python/func_groups/momentum_indicators.html)
  explicitly classify ADX as having an unstable period.
- [Da and Schaumburg, A Closer Look at Short-Term Return Reversal](https://academicweb.nd.edu/~zda/Reversal.pdf)
  distinguish liquidity-driven reversal from fundamental-information moves.
- [Bailey and Lopez de Prado, The Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)
  require correction for multiple testing before selecting among tried models.
- [Alpaca paper-trading limitations](https://docs.alpaca.markets/us/docs/paper-trading)
  document that paper fills omit several live execution effects.

## Scope

1. Add a read-only `POST /api/strategy-shadow/adx-challengers` computation.
2. Resolve an existing immutable source version without creating config,
   version, state, decision, trade, or order rows.
3. Reconstruct raw one-minute OHLCV only from persisted `features_json` and
   reject malformed, conflicting, incomplete, or cross-market evidence.
4. Use only complete sessions: at least 99.5% coverage, both boundaries,
   no internal gap, no outside-session bar, and no incomplete-session marker.
5. Replay the frozen baseline and fixed pre-registered `max_adx` values
   `{20, 25, 30}` over exactly the same sessions and cost assumptions.
6. Compare baseline actions, reasons, gate eligibility, fills, exits, fees,
   and net PnL with persisted evidence. A mismatch blocks all challengers.
7. Bump the algorithm version because frozen holding/cutoff/flatten/entry-limit
   parameters now drive replay behavior; never restore v3 state under v4 semantics.
8. Report diagnostic results before five complete sessions, but label them
   `INSUFFICIENT_EVIDENCE` and expose no apply/promote action.
9. Keep the existing 20-complete-session and 50-eligible-trade promotion gate.

## Acceptance

- The endpoint always returns `persisted=false`, `mode=SHADOW`, and
  `order_submission_allowed=false`.
- Calling it leaves all shadow and real trading tables and the durable
  watermark unchanged.
- A corrupt stored bar returns a validation error; a deterministic baseline
  mismatch returns `BLOCKED / BASELINE_REPLAY_MISMATCH` and no challenger set.
- Replay `eligible_bars` counts unique gate-passing timestamps.
- Lab displays the baseline and challengers without an apply button and does
  not run the CPU-heavy comparison in the 15-second polling loop.
- Backend tests/type checking, frontend type/build, full regression, deployment,
  and remote API verification pass.

## Following Iterations

1. P2.4: add `first_ready_at`, warm-up bars lost, and session-hour eligibility;
   compare a session-local VWAP/z-score engine with a causally pre-warmed trend
   state in shadow only.
2. P2.5: freeze challenger registration before the next sessions and perform
   paired forward/OOS validation; add DSR/PBO only after sample size is useful.
3. Operations: reduce the current roughly 24 LLM analyses/hour with zero order
   actions to a completed-bar/adaptive cadence; expose durable entry blockers;
   suppress duplicate no-op fill events and define the historical ledger epoch.
