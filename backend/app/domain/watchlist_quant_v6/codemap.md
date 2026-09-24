# backend/app/domain/watchlist_quant_v6/

## Responsibility

Quote-only historical evaluation ("quant v6"): frozen research semantics that score watchlist symbols from 1-minute OHLCV evidence — threshold evidence, bar-next-open stress events, session leaves — with tamper-evident artifacts for every published result. Order-incapable by construction (package docstring: "Frozen, order-incapable quant-v6 research semantics").

## Design

| File | Lines | Role |
|---|---|---|
| `semantics.py` | 1147 | The frozen semantics: `QuantV6Bar` / `QuantV6TrainingSession` / `QuantV6ThresholdEvidence` (`build_quant_v6_threshold_evidence`, `validate_quant_v6_threshold_evidence`), stressed events (`build_bar_next_open_stressed_events`, `BarNextOpenStressedEvent`), `quant_v6_session_bars_sha256`, `quant_v6_expected_rth_bar_starts` / `previous` / `consecutive` session helpers, contiguity + completeness validation, deep-freeze of semantic mappings. Decimal-first arithmetic; symbol/market pair validation |
| `assessment.py` | 1426 | `QuantV6SessionLeaf` (per-session canonical replay input, digest-bound) and `QuantV6Assessment` (aggregate with `assessment_digest_sha256`); `_VerifiedArtifactMemo` caches verified decodes and replays; `assess_bar_next_open_stressed_window`, `session_cluster_one_sided_90_lcb`; a `checkpoint` callback enables cooperative cancellation in long evaluations |
| `artifact.py` | 503 | Canonical-JSON codec with strict budgets (`_CanonicalJsonBudget`: depth/nodes/items/bytes caps), duplicate-key and float/`NaN` rejection, SHA-256-bound `encode_quant_v6_artifact()` / `decode_quant_v6_artifact()` with re-encode verification, distinct artifact kinds for event / session-input / assessment |
| `evaluator.py` | 102 | `quant_v6_evaluator_manifest()` / `quant_v6_evaluator_digest_sha256()` — hashes this package's own source modules so every artifact records exactly which evaluator code produced it |
| `__init__.py` | 190 | Re-exports the public surface (constants, codec, semantics, assessment) with a lazy `__getattr__` |

Key patterns:

- Every payload crossing a boundary is canonicalized (UTC timestamps, plain-string decimals via `canonical_decimal`), budget-validated, and digest-bound; decoding re-encodes and compares to catch drift.
- Evaluator identity (source hashes) is part of the evidence, mirroring strategy_v2's `forward_semantics` manifest.
- Session dispositions (`SESSION_COVERED` / `SESSION_MISSING`) keep coverage honest rather than imputing missing sessions.

## Flow

1. The reader service collects complete-session 1-minute bars → `semantics.py` validates contiguity and expected RTH bar starts, builds threshold evidence and bar-next-open stressed events.
2. `assessment.py` folds verified sessions into `QuantV6SessionLeaf` → `QuantV6Assessment` with cluster one-sided lower confidence bounds.
3. Results are `encode_quant_v6_artifact()`-ed; the publication service stores bytes + checksum; `research_artifact_retention_service` expires artifact *bytes* on the 30d replay/quant-v6 window while provenance/checksum rows survive forever.

## Integration

- **Consumers**: `services/watchlist_quant_v6_evaluation_service.py`, `..._publication_service.py`, `..._reader_service.py`, `..._spawn_supervisor.py`, `research_artifact_retention_service`, `research_observation_health_service`.
- Evidence-only: nothing here can express an order; promotion decisions remain human review.
- `0` disables the retention window; default retention windows are owned by settings, not this package.
