# backend/app/domain/performance/

## Responsibility

Reads LLM prediction experiment results and produces comparison statistics plus human-readable recommendations — the analytics counterpart to `experiment/ABTestManager`'s write path.

## Design

- **`PerformanceTracker`** (`performance_tracker.py`, 85 lines): constructed with a `sqlalchemy.orm.Session`; three methods:
  - `get_overall_stats(experiment_name)` — aggregate accuracy/PnL-style stats across all variants
  - `compare_variants(experiment_name)` — per-variant side-by-side rows
  - `get_recommendations(experiment_name)` — `list[str]` advice lines derived from aggregate deltas (e.g. promote the leading variant)
- **Documented purity exception**: one of only two `domain/` packages touching the ORM (with `experiment/`). Read-mostly; keep new DB logic in `services/`.
- No state beyond the session; safe to construct per request.

## Flow

`api/performance.py` builds a `PerformanceTracker(db)` per request → aggregates over `ExperimentResult` rows (written by `experiment/ABTestManager.record_result`) → JSON response consumed by the frontend performance views. Recommendations strings are rendered directly in the UI.

## Integration

- **Consumer**: `api/performance.py` (sole importer).
- **Model**: `app.models.ExperimentResult` (shared with `domain/experiment/`).
- **Related**: `domain/experiment/` owns the write path (`create_version`, `activate_version`, `select_variant`, `record_result`); this package is the read path over the same rows.
- **Recommendations are advisory strings only** — they inform human review of prompt variants and never touch the live order path.
- No other importers; `__init__.py` is empty.
