# P2.4 Strategy v2 Causal Warm-up Diagnostics Plan

## Evidence

The deployed paper service is healthy and flat, while the active P0 strategy
remains blocked by the durable re-entry latch and daily-loss controls.  The
complete 2026-07-13 US shadow session contains 390 one-minute bars, but the
session-local ADX(14) does not make the full feature set ready until 11:49 ET:
139 bars, or 35.6% of RTH, are unavailable before the first ready snapshot.
This is expected indicator warm-up rather than missing market data.

The current v4 evidence version must keep collecting unchanged.  P2.4 is a
read-only research diagnostic; it does not change P0, the live shadow tick
path, thresholds, persisted state, or the v4 algorithm version.

## External Basis

- [TradingView VWAP](https://www.tradingview.com/support/solutions/43000502018-volume-weighted-average-price-vwap/)
  defines Session as an anchor/reset period, supporting session-local VWAP and
  residual z-scores.
- [TA-Lib ADX source](https://github.com/TA-Lib/ta-lib/blob/main/src/ta_func/ta_ADX.c)
  uses recursive Wilder state and adjacent bars; a continued RTH series
  therefore includes the overnight gap in the first current-session true
  range.  [TA-Lib also marks ADX unstable](https://ta-lib.github.io/ta-lib-python/func_groups/momentum_indicators.html).
- [LEAN warm-up](https://www.quantconnect.com/docs/v2/writing-algorithms/historical-data/warm-up-periods)
  primes indicators with prior history while prohibiting orders during the
  warm-up period.
- [Freqtrade recursive analysis](https://www.freqtrade.io/en/stable/recursive-analysis/)
  compares multiple startup lengths, and its
  [lookahead analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/)
  motivates prefix-invariance checks for causal features.
- [Longbridge historical candlesticks](https://open.longbridge.com/docs/quote/pull/history-candlestick)
  provide sufficient minute history for future online priming, but this
  iteration deliberately reuses only immutable persisted evidence.

## Frozen Semantics

1. Keep VWAP, 1m/5m residual buffers, z-scores, and strategy entry state local
   to each RTH session.
2. Pre-warm only ADX(5m) and realized volatility(1m) from a complete,
   immediately preceding exchange session in the same immutable config
   version.
3. ADX consumes only completed five-minute RTH bars and retains the classic
   Wilder overnight gap.  Record the overnight gap rather than silently
   inventing a gap-neutral indicator.
4. Realized volatility carries the latest valid one-minute log returns across
   recognized session/lunch gaps, but never annualizes an overnight or lunch
   jump as a one-minute return.  Internal missing minutes remain fail-closed.
5. Historical seed bars update feature context only.  They cannot emit a
   strategy decision, virtual trade, database write, order, or current-session
   coverage observation.
6. The target baseline and pre-warm variant use exactly the same target bars.
   A baseline replay mismatch blocks the diagnostic.
7. The candidate is identified independently as
   `strategy-v2-causal-trend-prewarm-v1`; the active v4 evidence version is not
   bumped or reset.

## Scope

1. Extend daily evaluation evidence with first-ready time, ready bars, warm-up
   bars lost, and exchange-local hourly readiness/eligibility plus gate counts.
   Readiness and gate eligibility remain distinct.
2. Add a pure `CausalTrendPrewarmFeatureEngine` used only by replay diagnostics.
3. Extend the existing read-only ADX challenger response with one warm-up
   diagnostic so raw evidence loading and baseline validation are reused.
4. Compare fixed variants `SESSION_LOCAL` and `CAUSAL_TREND_PREWARM` on paired
   complete sessions only, up to the existing replay window.  Fewer than five
   causal pairs is explicitly `INSUFFICIENT_EVIDENCE`.
5. Show the diagnostic in Lab with exchange-local clocks, hourly buckets,
   recovered-ready counts, blockers, and explicit no-order/no-promotion copy.
   The 15-second poll must not rerun the CPU-heavy replay.

## Acceptance

- A normal US baseline first becomes ready at bar index 139; the pre-warm
  variant remains constrained by session-local 5m z-score and cannot become
  ready before bar index 64.
- Prefix-only and full-input runs produce identical features and decisions at
  every compared timestamp; forming five-minute bars are never consumed.
- Overnight return is excluded from realized volatility, while the classic
  ADX transition includes the prior close gap.
- Duplicate decisions do not double-count bars; malformed readiness evidence
  fails closed; `eligible_bars <= ready_bars <= bars` always holds.
- US DST, HK lunch, incomplete sessions, missing prior sessions, unsupported
  versions, and baseline mismatches have deterministic tests.
- The endpoint remains `persisted=false`, `mode=SHADOW`,
  `order_submission_allowed=false`, and `promotion_eligible=false`, and all
  relevant database row counts and watermarks remain unchanged.
- Backend tests/type checking, frontend type/build, Cypress, full regression,
  deployment, and remote read-only verification pass.

## Next Iteration

P2.5 may pre-register one frozen challenger for future paired OOS sessions.
Only after that forward evidence is useful may the collector move to v5 and
reset; no v4/v5 evidence may be pooled.
