# backend/app/cli/

## Responsibility

Operational command-line helpers run as `python -m app.cli.<module>` from
`backend/`. Two kinds: safe local validation/maintenance tools
(`validate_config`, `llm_storage_maintenance`, `import_opening_activity`) and
offline opening-momentum research drivers
(`opening_policy_research`, `opening_extension_research`,
`opening_relative_volume_orb_research`). Nothing here runs inside the API
process; `main.py` does not import this package.

## Design

| Module | Lines | Role |
|---|---|---|
| `validate_config.py` | 366 | Loads `Settings` and reports sanitized ERROR/WARNING issue codes without importing `app.database` (no engine side effects) and without printing secrets. `--json` for machine-readable output; exit 0 unless an ERROR exists. |
| `llm_storage_maintenance.py` | 171 | Prunes LLM interaction history in batches under a `DurableJobLeaseService` lease; `--vacuum` rebuilds the SQLite file but requires `--confirm-service-stopped` (backend must be down). |
| `import_opening_activity.py` | 228 | Backfills `opening_activity_observations` from a gzipped research cache (`RESEARCH_CACHE_BACKFILL_V1` source tag); also the shared loader used by the research CLIs. |
| `opening_extension_research.py` | 1,293 | Opening-extension grid research: fetches Longport minute bars (env-configured), maintains an OHLC cache, splits sessions into baseline/discovery blocks, evaluates exit outcomes. Shared bar/cache utilities for the other research CLIs. |
| `opening_policy_research.py` | 1,923 | Paired opening-policy A/B research over cached bars (`--start-date/--end-date`, policy specs, `--discovery-ratio 0.60`, `--cache-path`, `--output`). Imports session builders from `opening_extension_research`. |
| `opening_relative_volume_orb_research.py` | 1,680 | Relative-volume opening-range-breakout research: session preparation, candidate-signal discovery, panel evaluation, cache-extension reporting; `--ohlc-cache` + `--activity-cache` inputs. |

Common patterns: argparse `main(argv) -> int` entry points returning process
exit codes; `from __future__ import annotations`; research CLIs share
`HistoricalCandleProvider` protocols and JSON/gzip caches with explicit cache
versions; Longport credentials are read from the environment
(`_configure_longport_environment`), never hard-coded.

## Flow

`main()` parses args → research CLIs load/extend their caches (downloading
missing bars via Longport when credentials exist) → build per-symbol sessions
from `app.core.market_calendar` → evaluate policies/candidates → write a JSON
report to `--output` and print a summary. `import_opening_activity` writes
directly to SQLite via `SessionLocal` + `init_db()`. Maintenance/`validate`
CLIs touch only `app.config` / leased DB access.

## Integration

- Depends on app-root `config.py`/`database.py`/`models.py`,
  `app.core.market_calendar`, and services
  (`LLMInteractionService`, `DurableJobLeaseService`).
- Research results feed the opening-momentum shadow/execution research layer
  (`domain/opening_momentum*`, `services/opening_momentum_*`); imports land in
  `opening_activity_observations`, which those shadow paths read.
- Not part of the FastAPI app; the prod Docker image ships only
  `scripts/import_historical_order_ledger.py` — these are dev-image tools.
