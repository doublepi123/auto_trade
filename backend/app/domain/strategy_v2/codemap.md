# backend/app/domain/strategy_v2/

## Responsibility

Strategy v2: the shadow-research engine plus the statistical machinery that decides whether any strategy version deserves promotion. Everything here is evidence-only — the engine emits decisions, never orders. The package also owns the frozen v5 negative-control governance (`PREREGISTRATION.md`, frozen 2026-08-30) and the disproof/trusted-assessment evidence chain.

## Design

### Engine & features (the shadow strategy)

| File | Lines | Role |
|---|---|---|
| `engine.py` | 823 | `StrategyV2Engine` state machine (`StrategyV2State` FLAT/ARMED/LONG), `StrategyV2Config` with `version_hash`, snapshot/restore, `on_feature()` → `StrategyV2Step` of `StrategyV2Decision`s, `entry_gate_reasons()` for rejection explanations |
| `features.py` | 756 | `StrategyBar`, `SessionFeatureEngine` (complete 5-minute bar aggregation, session VWAP, Wilder ADX, leave-one-out z-score, annualized realized vol) plus `CausalTrendPrewarmFeatureEngine` and `BoundaryNeutralCausalTrendPrewarmFeatureEngine` variants |
| `bracket.py` | 144 | `evaluate_bracket_bar()`: stop/target bracket decisions with adverse-price handling (`BracketAction`) |
| `profit_lock.py` | 104 | `evaluate_profit_lock_bar()`: trailing floor activation after a profit threshold (`activation_price` / `floor_price`) |
| `time_exit.py` | 66 | `evaluate_time_exit_bar()`: max-hold time exits |
| `costs.py` | 73 | `estimated_round_trip_cost_pct`, `minimum_profit_target_pct`, `estimated_net_reward_risk_ratio` |

### Edge statistics & promotion gates

| File | Lines | Role |
|---|---|---|
| `signal_edge.py` | 395 | `assess_signal_edge()` → verdict `PASS / FAIL / FEE_BLOCKED / INSUFFICIENT_DATA`; `first_passage_baseline()` = driftless `stop/(stop+target)`; `assess_first_passage()` binomial test restricted to one barrier-version cohort; `assess_promotion()` composes the gates. Judged on **net** CI lower bound > 0; gross reported only as contrast |
| `clustered_returns.py` | 278 | `clustered_t_test()`: day-clustered, trade-weighted t-statistics (`df = D − 1`) — per-trade t-stats overstate edge by ~`sqrt(trades/days)` when trades cluster by day across correlated symbols |
| `futility.py` | 231 | `assess_futility()`: whether a cost-clearing gross edge is `ALIVE`, `FUTILE`, or `INSUFFICIENT_DATA`, with preregistered 10 bps cost / 20 bps daily sigma constants and a fixed `mean + 2.0·SE` upper bound. Read-only; abandonment still requires the written §9.5.4 human decision |
| `selection_power.py` | 146 | `required_trades_one_sample` / `_two_sample`, `assess_selection_power()`, `reach_gate_operating_point()` (reach-rate = share of closed shadow trades whose peak favourable excursion cleared 0.4%) |
| `portfolio_routing.py` | 912 | `rank_portfolio_candidates()` over `PortfolioRoutingCandidate` with multiple scoring families (VWAP-edge, observed-cost, z-score, risk-group-relative, leave-one-out) and `portfolio_candidate_rejection_reasons()` |

### Frozen evidence infrastructure

| File | Lines | Role |
|---|---|---|
| `forward_replay_artifact.py` | 258 | Canonical-JSON + SHA-256 codec for forward replay artifacts: size-validated, duplicate-key/float/`NaN`-rejecting parser, `forward_replay_artifact_binding_sha256` |
| `forward_semantics.py` | 288 | `forward_executable_semantic_manifest()` / `_digest()`: AST-normalized (docstrings stripped) behavioral probe digest proving which executable produced a result |
| `frozen_disproof_queue.py` | 1449 | `FrozenQueueSpec` / `FrozenQueueEntry` / `FrozenEvidenceContext` parsing, `canonical_frozen_queue_manifest()`, daily disposition evaluation against frozen metrics |
| `trusted_frozen_assessment.py` | 1749 | `build_trusted_assessment_report()` over `TrustedDailyLeaf` + `validate_replay_trade_track()`: producer cutoffs, per-leaf digest binding, aggregation into the v5 negative-control report |
| `PREREGISTRATION.md` | — | Governance contract. v5 runs unchanged as a negative control: if the pipeline ever certifies v5 as having edge, the pipeline is wrong |

`__init__.py` (107 lines) re-exports the public surface; services import from the package, rarely from submodules.

## Flow

1. Shadow service feeds 1-minute bars → `SessionFeatureEngine.on_bar()` aggregates complete 5-minute bars → `StrategyV2FeatureSnapshot`.
2. `StrategyV2Engine.on_feature()` runs entry gates → pending entry → causal fill on a later bar (`CAUSAL_ENTRY_FILL_OFFSET_BARS`) → exits via bracket / profit-lock / time-exit → `StrategyV2Decision` stream recorded by the shadow service.
3. Closed shadow trades accumulate → `assess_signal_edge()` (first-passage + clustered t-test) and `assess_futility()` report verdicts; `selection_power` states the sample sizes the gates need.
4. Promotion requires **four ANDs** (PREREGISTRATION §3):
   - net day-clustered 95% CI lower bound > 0;
   - version-specific first-passage beating its own driftless baseline (barrier-version cohorts never merged);
   - ≥60 distinct days and ~180 resolved brackets;
   - deflated Sharpe `distinguishable_from_luck` (`Φ(z) ≥ 0.95`).
   Any parameter change resets the evidence clock.
5. Evidence freezes through the artifact codec + semantic digest; `trusted_frozen_assessment` / `frozen_disproof_queue` grade it against the frozen baseline.

## Integration

- **Consumers**: `strategy_v2_shadow_service` (tick/replay orchestration, snapshot persistence), `signal_edge_service` (`GET /api/strategy-shadow/signal-edge`), `live_exit_challenger_service` and `strategy_v2_bracket_challenger_service` (record-only challengers), `universe_selection_service` / `primary_candidacy_service` / `universe_promotion_service` (routing + gating), `research_observation_health_service`.
- **Governance**: `tests/test_strategy_v2_preregistration.py` pins a SHA-256 over frozen v5 parameters — never bump the hash to silence it; the fix is a preregistration decision plus a new `algorithm_version` in the same commit as the hash and the markdown.
- **P0**: nothing here submits orders or auto-promotes; verdicts are inputs to human review only. Thin evidence is `INSUFFICIENT_DATA`, never `FAIL`.
- Tests: `tests/test_signal_edge.py`, `tests/test_strategy_v2_preregistration.py` — no DB fixtures.
