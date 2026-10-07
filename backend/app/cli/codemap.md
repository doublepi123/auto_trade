# backend/app/cli/

## Responsibility

Operational command-line helpers run as `python -m app.cli.<module>` from
`backend/`. Two kinds: safe local validation/maintenance tools
(`validate_config`, `llm_storage_maintenance`) and offline research drivers
(`spy_monthly_sma10_replay`, plus `opening_momentum_historical_replay`, which
survives only as the imported helper module for that monthly-trend replay).
Nothing here runs inside the API process; `main.py` does not import this
package.

## Design

| Module | Lines | Role |
|---|---|---|
| `validate_config.py` | 366 | Loads `Settings` and reports sanitized ERROR/WARNING issue codes without importing `app.database` (no engine side effects) and without printing secrets. `--json` for machine-readable output; exit 0 unless an ERROR exists. |
| `llm_storage_maintenance.py` | 171 | Prunes LLM interaction history in batches under a `DurableJobLeaseService` lease; `--vacuum` rebuilds the SQLite file but requires `--confirm-service-stopped` (backend must be down). |
| `spy_monthly_sma10_replay.py` | — | Frozen monthly-trend research replay. Imports hardened fetch helpers from `opening_momentum_historical_replay.py`; never submits orders. |
| `opening_momentum_historical_replay.py` | — | Retained helper module for the monthly-trend replay import contract. Not a live path. |

Common patterns: argparse `main(argv) -> int` entry points returning process
exit codes; `from __future__ import annotations`; research CLIs share
`HistoricalCandleProvider` protocols and JSON/gzip caches with explicit cache
versions; Longport credentials are read from the environment
(`_configure_longport_environment`), never hard-coded.

## Flow

`main()` parses args → research CLIs load/extend their caches (downloading
missing bars via Longport when credentials exist) → evaluate → write a JSON
report to `--output` and print a summary. Maintenance/`validate` CLIs touch
only `app.config` / leased DB access. The opening execution CLIs are gone.

## Integration

- Depends on app-root `config.py`/`database.py`/`models.py`,
  `app.core.market_calendar`, and services
  (`LLMInteractionService`, `DurableJobLeaseService`).
- `opening_momentum_historical_replay.py` remains only because
  `spy_monthly_sma10_replay.py` imports its hardened fetch helpers. The
  opening execution CLIs are gone; historical observation tables are retained.
- Not part of the FastAPI app; the prod Docker image ships only
  `scripts/import_historical_order_ledger.py` — these are dev-image tools.
