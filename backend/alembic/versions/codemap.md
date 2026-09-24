# backend/alembic/versions/

## Responsibility

The 15 hand-written Alembic revisions (linear chain, one head) that carry the
SQLite schema from the initial baseline to the current head
`20260922_fill_intent`. Each file is a standard Alembic revision module
(`revision` / `down_revision` + `upgrade()` / `downgrade()`); filenames are
`<date>_<slug>.py`, revision IDs equal the filename stem.

## Design

Revisions are additive-only in practice (new tables, new nullable columns,
backfills guarded to be conservative); destructive changes are avoided
because the runtime `_ensure_*` migrations in `app/database.py` must be able
to reproduce the same end state idempotently, and `docker-entrypoint.sh`
must be able to recognize each schema level by inspecting tables/columns
(`advance_added_columns` refuses *partial* column sets — a revision whose
columns can appear one at a time would break stamping).

## Flow

Revision chain, oldest → head (`down_revision` → `revision`):

| Revision | After | Purpose |
|---|---|---|
| `41e077353669` | — | Initial migration: baseline tables |
| `20260602_add_llm_interval_fields` | `41e077353669` | LLM suggestion/analysis fields on `strategy_config` |
| `20260520_add_llm_interval_minutes` | `20260602_add_llm_interval_fields` | `llm_interval_minutes` on `strategy_config` |
| `20260522_add_min_profit_amount` | `20260520_add_llm_interval_minutes` | `min_profit_amount` fee-guard floor on `strategy_config` |
| `20260522_auto_resume_pause` | `20260522_add_min_profit_amount` | Pause auto-resume config + `runtime_state` pause columns |
| `20260522_add_llm_interactions` | `20260522_auto_resume_pause` | `llm_interactions` history table |
| `20260724_opening_momentum` | `20260522_add_llm_interactions` | `opening_momentum_shadow_runs` evidence table |
| `20260726_opening_stop` | `20260724_opening_momentum` | Stop/MFE/MAE columns on opening-momentum runs |
| `20260727_opening_context` | `20260726_opening_stop` | Causal opening-context telemetry columns (gap, benchmarks) |
| `20260727_opening_execution` | `20260727_opening_context` | `opening_momentum_executions` crash-safe journal |
| `20260801_watchlist_quant_v6` | `20260727_opening_execution` | Immutable quant-v6 publication storage |
| `20260801_durable_job_leases` | `20260801_watchlist_quant_v6` | `durable_job_leases` with no-delete trigger + check constraints |
| `20260802_opening_breakout_depth` | `20260801_durable_job_leases` | `candidate_breakout_depth_bps` on shadow runs |
| `20260921_fill_settlements` | `20260802_opening_breakout_depth` | `fill_settlements` durable accounting receipts (+ no-delete trigger) |
| `20260922_fill_intent` (HEAD) | `20260921_fill_settlements` | `persist_position` intent + `cost_basis_opened_at` on `fill_settlements` |

The file dates are not chronological walking down the chain (e.g.
`20260520` revises `20260602`) — trust `down_revision`, not the filename.

`docker-entrypoint.sh` mirrors this chain in its stamp constants
(`HEAD_REVISION = '20260922_fill_intent'`); adding a revision without
updating that script and its column-detection logic makes the entrypoint
refuse to stamp/upgrade. Some schema elements are additionally recreated at
runtime by `database.py`'s `_ensure_*` functions (e.g.
`_ensure_fill_settlements_table`), which is the safety net for databases that
skipped Alembic.

## Integration

- Written by hand against `app/models.Base.metadata` (`alembic/env.py`).
- Consumed by `docker-entrypoint.sh` (`alembic upgrade head`) and by
  migration-integrity tests under `tests/`.
- Paired, for every revision, with runtime parity in `app/database.py`
  (`_ensure_*`) and stamp-recognition in the entrypoint — three places that
  must move together; see [`../codemap.md`](../codemap.md).
