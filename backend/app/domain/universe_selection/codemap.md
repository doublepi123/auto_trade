# backend/app/domain/universe_selection/

## Responsibility

Defines and evaluates the dynamic candidate pool: the index candidate catalog, point-in-time member selection, monthly rotation cohorts, and walk-forward validation of rotation variants. This package decides *which symbols are even candidates* for every shadow and research path, and grades how well those choices would have performed.

## Design

### Catalog & membership (who is even a candidate)

| File | Lines | Role |
|---|---|---|
| `catalog.py` | 480 | `IndexCandidate` catalog constants (`INDEX_CANDIDATE_CATALOG`, historical and rotation-research variants) with `CATALOG_SOURCE_VERSION`; `risk_group_for_sector()` maps sectors into risk groups used by portfolio routing |
| `membership_history.py` | 317 | `IndexMembershipHistory` loaded from `data/index_membership_history.json`: point-in-time `is_active(symbol, date)`, `MembershipHistoryCoverage` authoritative/historical ratios, override parsing. `data/THIRD_PARTY_NOTICES.md` records data provenance |
| `tradeability.py` | 91 | `rank_tradeability()`: dollar-volume / liquidity ranking rows for candidates |

### Selection & evidence

| File | Lines | Role |
|---|---|---|
| `selector.py` | 740 | `DailyBar` is a `Protocol` (structural typing — services pass their own row types; no model import). `UniverseSelectionConfig` (with `UNIVERSE_ALGORITHM_VERSION` / `ROTATION_ALGORITHM_VERSION`), `select_candidates()` percentile-ranks liquidity/volatility/turnover metrics → `CandidateSelection` + embedded `RotationSelectionEvidence`; `parse_frozen_rotation_selection()` round-trips frozen selections; `completed_daily_bars` / `latest_closed_session_date` / `latest_complete_session_date` helpers; `liquidity_spread_proxy_bps()` |
| `rotation_forward.py` | 1285 | Monthly cohort machinery: `RotationCohortRegistration` (frozen, digest-bound) + `RotationCohortSignal`, `build_rotation_cohort_registration()`, validated inverse-volatility / shrinkage target parsing, `evaluate_rotation_forward()` paired forward evaluation, `is_last_us_session_of_month()` / `next_cohort_month()` |
| `rotation_forward_scorecard.py` | 670 | `RotationForwardCohortEvidence` (completeness-aware) → `build_rotation_forward_track_score()` cohort grading with compounded returns |
| `rotation_walk_forward.py` | 1355 | `evaluate_rotation_walk_forward()`: `RotationVariant` grid simulated over `_expanding_validation_folds()` with point-in-time missing-symbol classification, trade costs + turnover, capped weights, Sharpe / drawdown / annualized metrics, `_training_score`, and `_validation_blockers` fail-closed checks |
| `ROTATION_PREREGISTRATION.md` | — | Freezes the rotation research design; same governance style as strategy_v2's contract |

Patterns shared with strategy_v2: frozen registrations with canonical payloads + digests, defensive `_required_*` parsers at every persistence boundary, and preregistered acceptance thresholds rather than post-hoc tuning.

## Flow

1. Daily bars + catalog → `select_candidates()` scores metrics and percentile-ranks them → ranked `CandidateSelection` with `RotationSelectionEvidence` (persisted by `universe_selection_service` as `UniverseSelectionRun` rows).
2. At month boundaries (`is_last_us_session_of_month`), `build_rotation_cohort_registration()` freezes next month's cohort and its target weights before the month starts.
3. Forward: `evaluate_rotation_forward()` scores realized cohort performance into `RotationForwardEvaluation` snapshots; `rotation_forward_scorecard` grades cohorts; `rotation_walk_forward` validates variant choices out-of-sample over expanding folds.
4. Selection runs marked `COMPLETE` + `selected` are the only input the opt-in auto-primary-switch path will read.

## Integration

- **Consumers**: `services/universe_selection_service.py` (selection loop), `services/rotation_forward_scorecard_service.py`, `services/watchlist_quant_service.py`, `services/universe_promotion_service.py`, `services/universe_explainer_service.py`, `services/research_observation_health_service.py`, `services/primary_candidacy_service.py` (also imports `strategy_v2.RISK_GROUP_RELATIVE_MIN_PEERS`).
- **Scripts** (dev image): `scripts/evaluate_rotation_walk_forward.py` drives walk-forward evaluation; `scripts/build_index_membership_snapshot.py` refreshes membership snapshots.
- **Upstream**: `app.core` calendars for session dates; membership JSON shipped in `data/`.
- **Governance**: rotation cohorts and variant grids are frozen by `ROTATION_PREREGISTRATION.md`; changing the design is a preregistration decision, not an edit.
- Evidence-only: selection outputs feed shadow/research symbol pools; the live symbol changes only via the explicit opt-in auto primary switch (`AUTO_TRADE_AUTO_PRIMARY_SWITCH_ENABLED`, default off).
