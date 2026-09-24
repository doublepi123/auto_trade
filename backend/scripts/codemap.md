# backend/scripts/

## Responsibility

Standalone CLI entry points for research evaluation, evidence collection, and
database/ledger operations. Everything here is run by a human (or cron) from
the repo or the dev image — **the production Docker image ships only
`import_historical_order_ledger.py`** (see the `Dockerfile`); the rest are
DEV IMAGE ONLY. Scripts self-bootstrap `sys.path` where needed and are
included in basedpyright's scope (`pyrightconfig.json`).

## Design

Two safety idioms recur:

- **Read-only by default, mutation behind `--apply`** — mutation commands
  print a preview/digest first; applying requires re-running with the digest
  (`database_maintenance.py`, `import_historical_order_ledger.py`) or an
  explicit `--apply` flag. None of them submit orders or touch the broker
  unless named below.
- **Explicit inputs, JSON outputs** — research evaluators take file paths
  (`--input`/`--data`) and write JSON to `--output`/`--json-out` or stdout,
  keeping results reproducible and diffable.

## Flow

| Script | Purpose | Mode |
|---|---|---|
| `setup_venv.sh` | (Re)create `.venv`: default `requirements.txt`, `--locked` uses `requirements.lock.txt`, `--reset` deletes first | Dev helper |
| `evaluate_rotation_walk_forward.py` | Walk-forward evaluation of universe-rotation selection (`--history-bars`, e.g. 1000) | Read-only research |
| `build_index_membership_snapshot.py` | Fetch/derive index membership history; snapshot for universe selection (`app/domain/universe_selection/data/`) | Read-only research; writes snapshot artifact |
| `evaluate_range_exit_horizons.py` | Range-strategy holding-horizon study on local OHLC CSV (`--input`, `--symbol`, `--market`, `--buy-low`, …); deterministic discovery/holdout day split | Read-only research |
| `evaluate_frozen_disproof_queue.py` | Score a precommitted, research-only Strategy v2 forward disproof queue from explicitly supplied JSON (`--input`; never reads broker or DB) | Read-only research |
| `screen_strategy_plugin_inventory.py` | Offline screen of platform strategy plugins on minute bars (`--data minute-bars.json --symbol NVDA.US`) | Read-only research |
| `backfill_strategy_v2_forward_replay_artifacts.py` | Regenerate replay artifacts for forward shadow trades via `StrategyV2ShadowService` (`--limit`, default 250) | Write (DB artifacts only) |
| `database_maintenance.py` | Retention prune + backup relocation + VACUUM. Default **PREVIEW** (dbstat usage, would-delete counts, projected size); `--apply` to mutate; `--vacuum` additionally requires `--apply`, refuses during any market's RTH, checkpoints WAL first, needs ~DB-size free disk | Preview / `--apply` |
| `import_historical_order_ledger.py` | Import historical broker orders/fills into the ledger (`--symbol --start-at --end-at`); preview prints a digest, `--apply-preview-digest` + `--account-fingerprint` re-run performs the write. **The one script shipped in the prod image** | Preview / digest-gated apply |
| `reconcile_broker_order_ledger.py` | Reconcile local `orders` against the live broker over a window (`--start-at`, `--end-at`, `--database`); needs `LONGPORT_*`; window ≤90 days; exits `2` on `RECONCILIATION_INCOMPLETE` | Read-only (broker + DB reads) |
| `audit_synced_fill_cost_basis.py` | Audit `tracked_entries` for cost-basis writes made by today-order sync between commit `0fd2dcc` and its revert (ownership guard could never fire); `--window-seconds`, `--json-out` | Read-only audit |

## Integration

- Common imports: `app.database` (`SessionLocal`, `init_db`),
  `app.config.settings`, and domain/service modules (e.g.
  `StrategyV2ShadowService`). They run against the same SQLite WAL database
  as the app — prefer running maintenance while the market is closed (the
  script itself refuses `--vacuum` in RTH).
- `reconcile_broker_order_ledger.py` / `import_historical_order_ledger.py`
  need live `LONGPORT_*` credentials and reach the Longbridge broker
  (read paths only).
- Root `AGENTS.md` "Development Commands" lists the canonical invocations;
  `backend/README.md` covers `setup_venv.sh` usage.
