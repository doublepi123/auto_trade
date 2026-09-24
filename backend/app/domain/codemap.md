# backend/app/domain/

## Responsibility

Pure computation layer: all quantitative and decision logic that can be expressed as *data in, values out*. 9 subpackages plus root-level modules, ~27k lines of Python. This is the research/statistical heart of the system — shadow engines, edge statistics, universe selection, frozen-evidence codecs — with no dependency on infrastructure. Nothing in this tree talks to the broker, the database (two documented exceptions below), the network, or the wall clock.

## Design

### Purity contract

- **Allowed imports**: stdlib, other `domain` modules, and `app.core` pure utilities (`market_calendar`, `holiday_calendar`). Nothing else.
- **Forbidden**: `app.services.*`, `app.api.*`, `app.platform.*`, `SessionLocal` / any ORM session, `BrokerGateway`, `settings.*`, network calls, `datetime.now()` without an injected clock.
- Verified by AST scan: zero imports from `app.services`. This invariant is what makes domain tests fixture-free.
- **Documented exceptions**: `experiment/ab_test_manager.py` and `performance/performance_tracker.py` take a `sqlalchemy.orm.Session` (LLM A/B-test helpers written before the contract hardened). They are the only files in the tree that touch the ORM; do not extend that pattern — new DB work belongs in `services/`.
- Precise statement of the root AGENTS.md "no I/O" rule: *no services/DB/network; core calendars permitted*.

### Recurring patterns

- **Frozen evidence**: `strategy_v2`, `llm_interval_forward`, and `watchlist_quant_v6` each pair computation with a canonical-JSON + SHA-256 artifact codec so results are bit-reproducible and tamper-evident. Decimals are serialized as plain strings; floats, `NaN`, and duplicate JSON keys are rejected on decode.
- **Preregistration governance**: parameter sets are pinned by SHA-256 hash tests (`test_strategy_v2_preregistration.py`); changing them is a governance decision, never a silent edit.
- **Injected clocks**: replay/research code takes `now` or timestamps as parameters, never reads the wall clock, so replay stays deterministic.
- **Defensive parsing**: anything crossing a persistence boundary is re-parsed through `_required_*` / `_validated_*` helpers that fail closed.
- **Fail-closed statistics**: thin evidence yields `INSUFFICIENT_DATA`, never `FAIL`; promotion gates are AND-composed.

### Subpackages

| Subpackage | Size | Job | Deep dive |
|---|---|---|---|
| `strategy_v2/` | 16 files, ~7.7k lines | Shadow engine, bracket/profit-lock/time exits, signal-edge & promotion statistics, frozen v5 negative control | [strategy_v2/codemap.md](strategy_v2/codemap.md) |
| `universe_selection/` | 7 files, ~5.1k lines | Index candidate catalog, member selection, rotation walk-forward & cohort evaluation | [universe_selection/codemap.md](universe_selection/codemap.md) |
| `llm_interval_forward/` | 4 files, ~4.0k lines | Frozen contract + paired replay for rejected-LLM-interval counterfactual evidence | [llm_interval_forward/codemap.md](llm_interval_forward/codemap.md) |
| `watchlist_quant_v6/` | 5 files, ~3.4k lines | Quote-only historical evaluation semantics, session leaves, assessment artifacts | [watchlist_quant_v6/codemap.md](watchlist_quant_v6/codemap.md) |
| `prompt/` | 10 files, ~570 lines | LLM prompt plugin architecture (`PromptModule` + `PromptBuilder` + `FeatureSelector`) | [prompt/codemap.md](prompt/codemap.md) |
| `analysis/` | 2 files, ~620 lines | `TechnicalIndicators` (RSI/MACD/ADX/OBV/VWAP…) and `MarketStateDetector` | [analysis/codemap.md](analysis/codemap.md) |
| `sentiment/` | 1 file, 58 lines | `MarketSentimentAnalyzer`: price-change → sentiment score | [sentiment/codemap.md](sentiment/codemap.md) |
| `performance/` | 1 file, 85 lines | `PerformanceTracker`: LLM experiment stats from DB (ORM exception) | [performance/codemap.md](performance/codemap.md) |
| `experiment/` | 1 file, 152 lines | `ABTestManager`: prompt-version A/B experiments (ORM exception) | [experiment/codemap.md](experiment/codemap.md) |

### Root-level modules

| Module | Lines | Role |
|---|---|---|
| `fill_settlement.py` | 106 | Fill-settlement pure types and verdicts |
| `opening_momentum.py` | 710 | Core opening-momentum logic (config, evaluation, breakout/reversal paths) |
| `opening_momentum_policy.py` | 1253 | Policy research grid / cohort / horizon evaluations |
| `opening_momentum_extension.py` | 622 | Extension-candidate evaluation with cost-stress slices |
| `opening_momentum_universe.py` | 298 | Opening-momentum universe selection + version hashes |
| `opening_momentum_comparison.py` | 166 | Paired opening-momentum variant comparison |
| `opening_research_quiet_window.py` | 43 | Quiet-window predicate for research cron scheduling |

Details:

- **`fill_settlement.py`** — `FillFacts`, `EntryBooking`, `ReductionBooking` dataclasses; `settlement_key(broker_order_id)`; `plan_entry_booking()` / `plan_reduction_booking()`; `compare_repeat(stored, incoming) -> RepeatComparison` with `RepeatVerdict` for fill idempotency (identical repeat, conflicting repeat, new). Consumed by `fill_settlement_service` and `trade_execution_service`'s settlement-intent path.
- **`opening_momentum.py`** — `OpeningMomentumConfig` (validated, with `round_trip_cost_bps` and `version_hash`), `evaluate_opening_momentum`, `evaluate_opening_range_breakout` / `evaluate_stocks_in_play_opening_range_breakout`, `evaluate_opening_reversal`, `evaluate_opening_momentum_path_eligible`, `shadow_round_trip_return_bps`, `opening_path_efficiency`.
- **`opening_momentum_policy.py`** — `evaluate_opening_policy_grid` / `evaluate_opening_policy_cohort` / `evaluate_opening_policy_horizons` over `OpeningPolicySession` inputs, with `_baseline_anchored_split` guarding same-horizon decisions and net-return metrics after cost.
- **`opening_momentum_extension.py`** — `evaluate_opening_extension_candidates` producing `OpeningExtensionResearchReport` with per-candidate slices and cost-stress rows.
- **`opening_momentum_universe.py`** — `select_opening_momentum_universe` plus `opening_momentum_variant_config_version` / `opening_momentum_evidence_config_version` hashes.
- **`opening_momentum_comparison.py`** — `compare_opening_momentum_variants` paired comparison with drawdown metrics.
- **`opening_research_quiet_window.py`** — `is_opening_research_quiet_window()`; `main.py` research crons consult it to yield around the open.

## Flow

Domain code never initiates anything. Services collect raw inputs (bars from the broker/DB, shadow trade rows, LLM responses), call domain functions synchronously, and own all persistence:

1. `services/*` fetch data and construct plain inputs (`StrategyBar`, `DailyBar`, `FillFacts`, prompt context dicts).
2. Domain functions compute decisions/statistics deterministically:
   - engines expose `snapshot()` / `restore()` for stateless rehydration;
   - evidence codecs expose `encode_*` / `decode_*` with digest verification;
   - evaluators hash their own source (`evaluator_manifest`) so artifacts record producer identity.
3. Services persist results and expose them via `/api/*`; research artifacts flow through `research_artifact_retention_service` with checksums kept forever.

Worked example (shadow evaluation): `strategy_v2_shadow_service` collects ticks/bars → `SessionFeatureEngine.on_bar()` → `StrategyV2FeatureSnapshot` → `StrategyV2Engine.on_feature()` → `StrategyV2Step`/`StrategyV2Decision` stream — a decision, never an order.

## Integration

- **Downstream (only)**: `app.core` calendar utilities (26 imports of `market_calendar` / `holiday_calendar`).
- **Primary consumers**:
  - strategy_v2: `strategy_v2_shadow_service`, `signal_edge_service`, `live_exit_challenger_service`, `strategy_v2_bracket_challenger_service`, `primary_candidacy_service`, `universe_promotion_service`, `research_observation_health_service`
  - universe_selection: `universe_selection_service`, `rotation_forward_scorecard_service`, `watchlist_quant_service`, `universe_explainer_service`
  - watchlist_quant_v6: `watchlist_quant_v6_evaluation_service`, `..._publication_service`, `..._reader_service`, `..._spawn_supervisor`, `research_artifact_retention_service`
  - prompt / experiment / performance: `llm_advisor_service`, `api/experiments.py`, `api/performance.py`
  - root modules: `opening_momentum_shadow_service`, `cli/opening_extension_research.py`, `trade_execution_service`, `fill_settlement_service`, `data_aggregator`, `main.py` (quiet window)
- **`llm_interval_forward` is currently test-only** — its contract/replay are exercised by `tests/test_llm_interval_forward_*.py`; no service imports it yet.
- Root AGENTS.md layer graph: `domain → core(26) only; zero services imports`.
- Domain tests need no DB and no fixtures — if a new domain test needs a session, the code is in the wrong layer.
