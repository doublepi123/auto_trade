# Backtest Parameter Sweep Optimizer — Design

> **Date:** 2026-06-16 · **Author:** autonomous feature iteration (ultracode) ·
> **Selection:** judge-panel workflow winner (36/40; top pick on trader-value 10 + eng-fit 10)

## Problem

`POST /api/backtest/run` (and `BacktestEngine.run`) evaluate **one** config over a CSV.
The system's single highest-frequency pre-deploy decision — *which `buy_low` /
`sell_high` / `min_profit_amount` to run on real capital* — is therefore made by
guessing, then re-running. The engine already computes Sharpe / Sortino / Calmar /
profit-factor / profit-loss-ratio and already does a 4-point mini-grid in
`_fee_sensitivity`; only the multi-config **search** layer is missing.

## Goal

Turn the Backtest page from "verify one config" into "find a good config": given a CSV
+ a numeric grid over the interval/profit axes + a ranking metric, run every valid
combination and return (a) a ranked table of full metrics and (b) a 2-D heatmap of the
best metric per `(buy_low, sell_high)` cell. Clicking a row applies that config to the
form for a full `/run`.

## Non-goals (YAGNI)

- Persisting sweep results / "best config" to DB — sweep is ephemeral; applying is a
  manual row-click into the existing form. (The DB-backed `StrategyExperiment` system
  already covers saved, async, multi-run analysis.)
- Bayesian / SMAC surrogate-model optimization — brute-force Cartesian grid capped at
  `max_combinations` covers realistic interval search. Documented as future work.
- Parallel/concurrent inner loop — per-run cost is low; sync loop is fine for ≤ cap.
- Walk-forward / out-of-sample validation — a separate feature candidate ("Walk-Forward").
- Per-combo equity curve in the response — would bloat it; click-through to `/run` instead.

## Architecture

Three pure-functional layers, **all offline**, all reusing existing surfaces. **No new
DB tables, no `_ensure_*` migration, no broker/runner/order-path coupling.**

### 1. Core — `backend/app/core/backtest.py` (extend; stays pure)

Add a **module-level** function (the engine file already imports only `csv`, `io`,
`dataclasses`, `datetime`, `typing`; we add `itertools`):

```python
def sweep_backtest(
    base_params: BacktestEngineParams,
    param_grid: dict[str, list[float]],
    bars: list[BacktestBar],
    *,
    sort_by: str = "sharpe_ratio",
    max_combinations: int = 2000,
) -> SweepResult
```

- Validates `sort_by` against `SWEEP_SORT_KEYS`, `max_combinations` bounds, and that every
  grid key is in `SWEEP_ALLOWED_GRID_KEYS = {buy_low, sell_high, min_profit_amount,
  quantity, fee_rate, slippage_pct, stop_loss_pct}`. Unknown keys / bad bounds → `ValueError`.
- Computes the Cartesian-product count up front; raises `ValueError` if it exceeds
  `max_combinations` (defence in depth; the schema also caps at 1–10000).
- For each combination: `replace(base_params, **overrides)` → `BacktestEngine(params)`
  (whose `__init__` calls `_validate_params`, reusing the existing gate), then
  `engine.run(bars, include_fee_sensitivity=False)`. Combos failing **only**
  `buy_low >= sell_high` / `buy_low <= 0` are **skipped, not errors** (mirrors
  `ExperimentGridService.expand`'s policy).
- Sorts rows **descending** by `sort_by`; **`None` metric sorts last** (→ `-inf`).
  Tiebreak: `total_return_pct`, then `total_pnl`. Assigns `rank` 1..N after sorting.
- Builds a heatmap: groups rows by `(params.buy_low, params.sell_high)`, cell `value` =
  **max** `sort_by` metric among rows sharing that cell (so a `min_profit` third axis
  collapses to the best per cell). `None` loses to any real value.

Pure helper `expand_numeric_range(start, step, end)` replicates
`ExperimentGridService._expand_item`'s `round(v, 10) + 1e-12` tolerance exactly, so the
core never imports pydantic / services.

### 2. API — `backend/app/api/backtest.py` (add route on the existing `router`)

`POST /api/backtest/sweep` mirrors `/run`'s controller shape:
- Parses bars via the existing `_load_bars` (refactored to take `(csv_text, price_points)`
  so both endpoints share it).
- Expands each pydantic `StrategyExperimentGridItem` axis to `list[float]` by reusing
  `ExperimentGridService._expand_item` (static method — no duplication).
- Calls `sweep_backtest(...)`. Any `ValueError` → HTTP **422** (same envelope as `/run`).
- `require_api_key` dependency identical to `/run`. **No AuditLogger** — sweep is
  read-only analysis (like `/run`, which audits nothing; CLAUDE.md's 9 audited endpoints
  are all writes/control).

Also fix a latent gap: populate the 5 ratio metrics (`sharpe_ratio`, `sortino_ratio`,
`calmar_ratio`, `profit_factor`, `profit_loss_ratio`) in **both** `/run` and `/sweep`
responses — the dataclass computes them but `/run` currently drops them. Additive/safe.

### 3. Schemas — `backend/app/schemas.py`

- Add `sortino_ratio` + `calmar_ratio` to `BacktestMetrics` (Optional, default None).
- `BacktestSweepRequest`: `base: BacktestParams`, `grid: dict[str, StrategyExperimentGridItem]`
  (min_length 1), `sort_by: Literal[...]` (default `sharpe_ratio`),
  `max_combinations: int = Field(2000, ge=1, le=10000)`, `csv_text`/`price_points` like
  `/run`, `extra="forbid"`, and a `validate_price_source`-style validator.
- `BacktestSweepRow`: `params: dict[str, Any]` (**raw engine params** — a grid can push
  `fee_rate>0.1`/`slippage>5`, valid for the engine but rejected by `BacktestParams`
  bounds; returning raw values is honest and avoids a constraint mismatch),
  `metrics: BacktestMetrics`, `rank: int`.
- `BacktestSweepHeatmapCell`, `BacktestSweepHeatmap`, `BacktestSweepResult`.

### 4. Frontend — extend `Backtest.vue` in place (no new route)

- New collapsible **参数扫描 (Parameter Sweep)** section: grid inputs (`start/step/end`
  triplets for `buy_low`/`sell_high`, tag-list for `min_profit_amount`), `sort_by`
  selector, **运行扫描** button (reuses the page's `csvText` + `form` as base).
- Ranked **results table** (top 20: rank, buy_low, sell_high, min_profit, total_pnl,
  return%, drawdown%, sharpe, profit_factor, win_rate). Row click applies that config to
  `form` and scrolls up.
- **Heatmap**: HTML table (buy_low rows × sell_high cols), cell colour green→red by
  `z_metric`; message if only one axis swept.
- `api/backtest.ts`: `runBacktestSweep(payload, { timeout: 120000 })`.
- `types/index.ts`: add the sweep interfaces; extend `BacktestMetrics` with the 5 ratio fields.

## Data flow

`csv_text → _load_bars → list[BacktestBar] → sweep_backtest (iterates BacktestEngine per combo)
→ SweepResult (dataclasses) → BacktestSweepResult (pydantic) → JSON → Vue table + heatmap`.

Determinism: `BacktestEngine.run` sorts bars and uses no RNG ⇒ sweep is reproducible.

## Testing (TDD)

New file `backend/tests/test_backtest_sweep.py` (no DB — matches `test_backtest.py`):
- **Engine**: single-axis sort order + rank; skip invalid `buy_low>=sell_high`; 2-axis
  cartesian count; `sort_by=total_return_pct`; `None`-metric sorts last; unknown grid key
  → ValueError; over-`max_combinations` → ValueError; empty bars → ValueError; determinism;
  heatmap best-per-cell when a third axis is swept; full metric set populated.
- **API** (TestClient): 200 success (`best == rows[0]`, counts match); grid over cap → 422;
  missing csv+points → 422; bad base param → 422; malformed CSV → 422; unknown grid key → 422.
- **Schema**: `sort_by` Literal rejection; `max_combinations` bounds; grid min_length.

Cypress: extend `backtest.cy.ts` — run a sweep, assert ranked table + best-config apply.

## Verification gates

- `pytest tests/test_backtest_sweep.py tests/test_backtest.py -v` green (no regression).
- `pytest tests/ -q` ≥ 954 passed, only the 2 pre-existing `test_watchlist_score` failures.
- `basedpyright`: no NEW non-`reportMissingImports` errors (local env can't resolve `.venv`;
  see `state/baseline-2026-06-16.md`).
- `npm run type-check` 0; `npm run build` pass.

## Risks

| Risk | Mitigation |
|------|------------|
| Large grid × large CSV latency | `max_combinations` default 2000, schema cap 10000, 120s timeout, UI loading state. |
| Heatmap degenerate (1 axis) | 1-D fallback + clear message; backend still returns single-row/col cells. |
| `None` metrics in ranking | `None` → sorts last (a never-trading combo is a bad config → correct). |
| Confusion vs `StrategyExperiment` | UI copy: Sweep = 即时扫描当前 CSV; Experiments = 保存并批量回测. README contrasts them. |
| Row params constraint mismatch | Return raw engine params as `dict`, not `BacktestParams`. |
| Float step off-by-one | Reuse `round(v,10)+1e-12` tolerance verbatim. |

## Files

- **Create:** `backend/tests/test_backtest_sweep.py`, this spec.
- **Modify:** `backend/app/core/backtest.py`, `backend/app/schemas.py`,
  `backend/app/api/backtest.py`, `frontend/src/api/backtest.ts`, `frontend/src/types/index.ts`,
  `frontend/src/views/Backtest.vue`, `README.md`.
