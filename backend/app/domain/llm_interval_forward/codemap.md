# backend/app/domain/llm_interval_forward/

## Responsibility

Frozen contract and paired replay for **counterfactual forward evidence about rejected LLM intervals**: when the advisor proposes an interval but the proposal is rejected (e.g. `LOW_CONFIDENCE`), this package defines exactly what *would have happened* — deterministically, artifact-bound, and diagnostic-only. Package docstring: "Pure, diagnostic-only forward evidence for rejected LLM intervals."

## Design

| File | Lines | Role |
|---|---|---|
| `contract.py` | 1457 | The frozen schema layer. `FrozenIntervalBand`, `FrozenExecutionPolicy` (fee/slippage/risk caps as bounded `Decimal`, with derived per-side bps properties), `CounterfactualPolicyDecision` via `counterfactual_policy_without_confidence()`, SHA-256 preimage-bound freezing (`freeze_proposal_observation`, `freeze_session_slot`, `resolve_session_slot`), `full_session_observation_schedule()` over core calendars. Also the canonical-JSON engine used by this package: strict budgets (2 MB / depth 32 / 20k nodes / 2k container items), `canonical_json_bytes()`, `canonical_sha256()`, bounded `Decimal` parsing |
| `replay.py` | 2164 | Paired baseline-vs-candidate replay. `ForwardBar` validation, `_run_arm()` over the strategy engine, `ReplayRoundTrip`, `VariantSessionResult`, `PairedSessionLeaf` verified by `PairedLeafVerificationInput.verify()`, `replay_paired_session()` / `replay_paired_session_bundle()`, `IntervalForwardAssessment` with fixed 60-session horizon, `MINIMUM_INCLUDED_SESSIONS = 20`, `MINIMUM_CLOSED_ROUND_TRIPS_PER_ARM = 50`, `CONFIDENCE_MULTIPLIER = 2.1`, `MAX_DAILY_DELTA_CONCENTRATION = 0.25`; `evaluator_manifest()` / `evaluator_digest_sha256()` pin evaluator source identity |
| `artifact.py` | 272 | `encode_interval_forward_artifact()` / `decode_interval_forward_artifact()` — bounded decompression, payload identity + SHA-256 validation, float/`NaN`/duplicate-key rejection |
| `__init__.py` | 137 | Re-exports contract + artifact + replay public names |

Two design commitments travel with every result:

- **Honest fidelity**: explicit constants (`DATA_FIDELITY = ONE_MINUTE_OHLCV`, `BBO_COVERAGE = NONE`, `ENTRY_CROSSING_SEMANTICS = BAR_LOCAL_CROSSING_APPROXIMATION`, `FEE_MODEL_FIDELITY = CONFIGURED_FEE_RATE_ESTIMATE`, `TIMESTAMP_SEMANTICS = START_STAMP_PLUS_ONE_MINUTE_OBSERVATION_TIME`) plus `PERMANENT_LIMITATIONS` (`BBO_NONE`, `OHLC_CROSSING_APPROXIMATION`, `LIVE_PARITY_UNPROVEN`, `MANUAL_REVIEW_REQUIRED`, …) — no artifact may overstate what it knows.
- **Fail-closed assessment**: `ASSESSMENT_BLOCKER_CODES` (`FIXED_HORIZON_NOT_COMPLETE`, `MISSING_SESSIONS`, `INSUFFICIENT_*`, `CANDIDATE_NET_NOT_POSITIVE`, `DELTA_CONFIDENCE_LOWER_NOT_POSITIVE`, `DAILY_DELTA_CONCENTRATION_TOO_HIGH`, `PAIRED_COVERAGE_INCOMPLETE`) must all clear before any positive conclusion. The selection rule is frozen as `first-auto-cron-primary-flat-low-confidence-v1`.

## Flow

1. A proposal observation (frozen band + execution policy) is registered with a hash preimage before the session starts.
2. At each scheduled observation (`full_session_observation_schedule`, `TRADING_SESSION_MODE = RTH_ONLY`), 1-minute OHLCV bars are validated and replayed through both arms.
3. `assess_paired_sessions()` folds verified leaves into `IntervalForwardAssessment`; blockers keep conclusions fail-closed. The paired design cancels market regime between arms.
4. Bundles are artifact-encoded for storage with digests binding content and evaluator identity.

## Integration

- **Currently test-only**: exercised by `tests/test_llm_interval_forward_domain.py` and `tests/test_llm_interval_forward_artifact.py`; no `services/` importer yet — a built, not-yet-wired research capability.
- **Upstream**: `app.core.holiday_calendar` (`COVERAGE_*`, `is_market_closed`) and `app.core.market_calendar` (`get_session`, `trade_day_for`) for schedules.
- Diagnostic-only by design: it evaluates rejected proposals and never feeds the live order path.
