# backend/app/domain/experiment/

## Responsibility

LLM prompt A/B testing: manages prompt versions (`PromptVersion` rows), variant enrollment and per-symbol assignment, result recording (`ExperimentResult` rows), and experiment summaries. Owns all prompt-experiment database logic in one class.

## Design

- **`ABTestManager`** (`ab_test_manager.py`, 152 lines): constructed with a `sqlalchemy.orm.Session` and exposes the full experiment lifecycle:
  - `create_version` (content + metadata, hashed), `list_versions`, `list_experiment_names`
  - `activate_version` / `get_active_version(experiment_name)` — one active version per experiment
  - `enroll_version` — puts a version into the experiment's enrollment set
  - `select_variant(symbol, experiment_name)` — deterministic per-symbol variant assignment
  - `record_result` — writes outcome rows for analysis
  - `get_experiment_summary` — aggregate stats per experiment
- **Documented purity exception**: this is one of only two places in `domain/` that touches the ORM (with `performance/performance_tracker.py`). It predates the hardened purity contract; do not extend the pattern — new DB work belongs in `services/`.
- `__init__.py` re-exports `ABTestManager`.

## Flow

`llm_advisor_service` imports `ABTestManager` lazily inside its functions (no module-level ORM import): at interval time it looks up the active version, `select_variant` picks the prompt variant for the symbol, the LLM call runs with that variant, then `record_result` stores the outcome. `api/experiments.py` constructs `ABTestManager(db)` per request for list/create/activate/summary endpoints.

## Integration

- **Consumers**: `services/llm_advisor_service.py` (two lazy import sites, around variant selection and result recording), `api/experiments.py` (CRUD/activation/summary routes).
- **Models**: `app.models.PromptVersion`, `app.models.ExperimentResult`.
- **Related**: `domain/performance/performance_tracker.py` reads the same `ExperimentResult` rows for statistics and recommendations; `domain/prompt/` renders the variant prompt content itself.
- The A/B loop never influences live orders — the LLM is advisory only (P0).
