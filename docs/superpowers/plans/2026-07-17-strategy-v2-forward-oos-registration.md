# P2.5 Strategy v2 Pre-Registered Forward Validation Plan

## Evidence

The deployed paper service is healthy and flat on commit `f57040f`. Strategy
v2 source version `8923ca492953...` uses the frozen v4 algorithm, but its first
observed session started late and is incomplete. The P2.4 causal trend pre-warm
diagnostic therefore has zero complete causal pairs. Historical replay remains
useful for debugging, but it cannot validate a candidate selected from that
same history.

P2.5 freezes one candidate, `strategy-v2-causal-trend-prewarm-v1`, before its
target sessions occur. It measures whether causal ADX and realized-volatility
pre-warm recovers usable intraday bars while keeping VWAP, z-score, and trading
state session-local. It does not change the active strategy or submit orders.

## External Basis

- [LEAN warm-up](https://www.quantconnect.com/docs/v2/writing-algorithms/historical-data/warm-up-periods)
  replays earlier observations to initialize indicators and prohibits orders
  during that warm-up.
- [LEAN parameter optimization](https://www.quantconnect.com/docs/v2/writing-algorithms/optimization/parameters)
  warns that testing optimized parameters on the same period introduces
  look-ahead and recommends later data for validation.
- [LEAN walk-forward optimization](https://www.quantconnect.com/docs/v2/writing-algorithms/optimization/walk-forward-optimization)
  uses trailing information for a later evaluation window and calls out the
  trade-off between frequent selection and overfitting.
- [Freqtrade lookahead analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/)
  compares a baseline replay with sliced replays to detect future-data drift.
- [Freqtrade recursive analysis](https://docs.freqtrade.io/en/stable/recursive-analysis/)
  compares indicator output across startup-window sizes, matching the prefix
  invariance required by the pre-warm feature engine.
- [MLflow Model Registry workflow](https://mlflow.org/docs/latest/ml/model-registry/workflow)
  separates immutable versions and validation metadata from mutable production
  aliases. P2.5 creates only a frozen review candidate and no champion alias.

## Frozen Contract

1. Registration stores a server-generated UTC timestamp, the next full RTH
   open eligible as a target, symbol/market, immutable source config version,
   baseline and candidate algorithm versions, evaluator version, definition
   digest, and fixed review thresholds.
2. A pre-market registration may use that day's session. A registration at or
   after RTH open begins with the next full session. HK lunch is never treated
   as a full-session open.
3. A target session must open at or after `eligible_after`, be complete under
   the existing 99.5% coverage and boundary rules, and use the registered source
   config version. Pre-registration targets are never backfilled.
4. The immediately preceding complete session from the same source version may
   seed ADX and realized volatility even when it predates registration. It was
   known before the target opened and is context, not an OOS target.
5. Baseline and candidate consume exactly the same target OHLCV set and bar
   hash. VWAP, z-score, and session-local state must match at every timestamp.
6. Each registration/target pair receives one terminal append-only evidence
   row: `INCLUDED` or `EXCLUDED`. A canonical row digest covers disposition,
   reason, timestamps, provenance, and paired result payloads. Restart and
   repeated cron calls are idempotent.
7. A missed or incomplete collection window is recorded as excluded and is not
   repaired later. Structural drift or replay mismatch blocks the registration.
8. Five included pairs means `READY_FOR_REVIEW`; twenty means
   `MATURE_EVIDENCE`. These labels describe sample count only. They do not mean
   statistical significance, approval, promotion, or expected profitability.
9. Registration and evidence writes are isolated from shadow runtime state,
   decisions, trades, real orders, positions, risk state, and LLM state.
10. Each paired result stores its ordered closed-trade net-PnL sequence under
    the result hash. Aggregate drawdown is computed from the concatenated
    forward equity path rather than lossy daily summaries.

## Scope

1. Add immutable forward-validation registration and append-only daily evidence
   tables with uniqueness on registration plus target session date.
2. Add one audited, idempotent registration endpoint and one read-only status
   endpoint. Client input cannot set timestamps, thresholds, safety flags, or
   active strategy configuration.
3. Run a bounded post-close collector from the existing Strategy v2 shadow
   cron. GET and Lab polling never execute replay or write evidence.
4. Persist paired readiness, eligibility, action, and diagnostic deltas along
   with source/output hashes and seed/target provenance.
5. Show registration facts, status, included/excluded counts, remaining pairs,
   and server-paired daily evidence in Lab. Registration requires explicit
   acknowledgement of no historical target backfill and no automatic action.
6. Leave P0 parameters, P2 thresholds, v4 evidence, broker execution, and LLM
   cadence unchanged.

## Acceptance

- Registering twice is idempotent and cannot create competing definitions for
  the same frozen candidate cohort.
- A target before `eligible_after` never appears, including if historical bars
  become available later; a mid-session registration excludes that session.
- Baseline and candidate target bar hashes match. Local-feature drift, source
  version drift, corrupt evidence, or replay mismatch fails closed.
- Seed evidence must have been observed by target open, target evidence by the
  fixed evaluation time, and the target shadow state must be flat.
- Cross-session maximum drawdown is reproduced exactly from hashed ordered
  trade outcomes, including peaks and troughs that occur inside different days.
- Repeated collection and process restart do not add or mutate evidence rows.
- GET calls leave registration/evidence and all trading table counts and
  watermarks unchanged.
- `READY_FOR_REVIEW` and `MATURE_EVIDENCE` expose no apply, promote, config
  update, or order path.
- US DST, HK full-open/lunch behavior, incomplete sessions, missing seed,
  version transitions, threshold boundaries, and duplicate collection have
  deterministic tests.
- Backend tests/type checking, frontend type/build, Cypress, full regression,
  deployment, remote registration, and remote safety verification pass.

## Next Iteration

P2.6 should observe the frozen cohort without changing it. Only after useful
forward evidence exists should a separate, explicit human review decide whether
to create a v5 shadow collector. P2.5 evidence must never be pooled across v4
and v5, and no candidate receives an automatic production path.
