# P269–P278 Factor Research Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship 10 new pure-Python factor research and strategy diagnostics modules plus 10 `/api/platform/*` endpoints without changing live trading behavior.

**Architecture:** Each round is additive: one focused module under `backend/app/platform/`, one thin endpoint in `backend/app/platform/api.py`, one module-level pytest file, and endpoint checks appended to `backend/tests/platform/test_api_risk_portfolio.py`. All computations are deterministic, standard-library only, and expose frozen dataclass result objects with `to_dict()`.

**Tech Stack:** Python 3.11+, FastAPI, pytest, basedpyright, standard library only.

---

## File Structure

- Create `backend/app/platform/factor_turnover.py`: rank turnover, bucket retention, factor autocorrelation.
- Create `backend/app/platform/factor_decay.py`: multi-horizon IC/RankIC decay and half-life.
- Create `backend/app/platform/factor_quantiles.py`: quantile forward returns, spread, monotonicity.
- Create `backend/app/platform/ic_diagnostics.py`: IC time-series quality metrics and drawdown.
- Create `backend/app/platform/factor_data_quality.py`: factor panel coverage, missing/constant/outlier/stale diagnostics.
- Create `backend/app/platform/signal_persistence.py`: signal autocorrelation decay, half-life, turnover proxy.
- Create `backend/app/platform/strategy_quality.py`: SQN and trade quality summary.
- Create `backend/app/platform/regime_performance.py`: performance by regime bucket.
- Create `backend/app/platform/strategy_diversification.py`: multi-strategy correlation and redundancy.
- Create `backend/app/platform/backtest_confidence.py`: bootstrap CI, rolling Sharpe stability, fragility score.
- Modify `backend/app/platform/api.py`: add imports and 10 thin `POST` handlers.
- Modify `backend/tests/platform/test_api_risk_portfolio.py`: append 200/422 tests for all new endpoints.
- Create one `backend/tests/platform/test_<module>.py` per new module.
- Modify `README.md`, `docs/Roadmap.md`, and `CLAUDE.md` only for bounded P269–P278 status sync.

## Shared Conventions

- Every module starts with `from __future__ import annotations`.
- Every public result is `@dataclass(frozen=True)` and exposes `to_dict() -> dict[str, Any]`.
- Invalid inputs raise `ValueError` with actionable messages.
- Endpoint handlers catch `(TypeError, ValueError)` and raise `HTTPException(status_code=422, detail=str(exc))`.
- API input lists should be parsed via existing helpers in `api.py` where possible: `_numeric_series`, `_panel_field`, `_finite_number`.
- Do not commit unless the user explicitly requests it.

---

### Task 1: P269 — Factor Turnover

**Files:**
- Create: `backend/app/platform/factor_turnover.py`
- Modify: `backend/app/platform/api.py`
- Test: `backend/tests/platform/test_factor_turnover.py`
- Modify: `backend/tests/platform/test_api_risk_portfolio.py`

- [ ] Step 1: Write failing tests for `factor_turnover_report(panel, bucket_fraction=0.5)` where two snapshots keep most top names, reversed ranks produce high turnover, constant/missing snapshots raise `ValueError`, and `/api/platform/factor-turnover` returns 200/422.
- [ ] Step 2: Run `cd backend && python3 -m pytest tests/platform/test_factor_turnover.py -v`; expected import failure.
- [ ] Step 3: Implement rank ordering per timestamp, consecutive top/bottom bucket retention, average turnover, and lag-1 rank autocorrelation.
- [ ] Step 4: Add `POST /factor-turnover` endpoint accepting `{"snapshots": [{"A": 1.0, "B": 2.0}]}` and optional `bucket_fraction`.
- [ ] Step 5: Run `cd backend && python3 -m pytest tests/platform/test_factor_turnover.py tests/platform/test_api_risk_portfolio.py -v`.

### Task 2: P270 — Factor Decay

**Files:** create `factor_decay.py`; modify `api.py`; test `test_factor_decay.py`; append API tests.

- [ ] Step 1: Write tests for horizons `[1,2,3]` where aligned returns have positive IC at horizon 1, weaker IC at longer horizon, best horizon is 1, invalid length mismatch raises `ValueError`, and endpoint 200/422.
- [ ] Step 2: Run module tests and confirm failure.
- [ ] Step 3: Implement Pearson/Spearman IC per horizon using factor vector and `forward_returns_by_horizon`, plus first horizon below half max as `half_life_horizon`.
- [ ] Step 4: Add `/factor-decay` endpoint accepting `factor`, `forward_returns`, and optional `horizons`.
- [ ] Step 5: Run targeted pytest and basedpyright for touched files.

### Task 3: P271 — Factor Quantiles

**Files:** create `factor_quantiles.py`; modify `api.py`; test `test_factor_quantiles.py`; append API tests.

- [ ] Step 1: Write tests for monotonic factor/returns producing positive top-minus-bottom spread, quantile counts summing to input length, duplicate factor values handled deterministically, invalid `n_quantiles < 2` raises `ValueError`, and endpoint 200/422.
- [ ] Step 2: Run module tests and confirm failure.
- [ ] Step 3: Implement stable rank sorting, bucket assignment, per-quantile average return/count, spread, and monotonicity score.
- [ ] Step 4: Add `/factor-quantiles` endpoint accepting `factor`, `forward_returns`, and optional `n_quantiles`.
- [ ] Step 5: Run targeted pytest and basedpyright.

### Task 4: P272 — IC Diagnostics

**Files:** create `ic_diagnostics.py`; modify `api.py`; test `test_ic_diagnostics.py`; append API tests.

- [ ] Step 1: Write tests for positive IC series producing positive mean and positive ratio, drawdown from cumulative IC, t-like score finite, single value raises `ValueError`, and endpoint 200/422.
- [ ] Step 2: Run module tests and confirm failure.
- [ ] Step 3: Implement mean/std, positive ratio, cumulative IC curve, max drawdown, t-like `mean / std * sqrt(n)`, and stability classification.
- [ ] Step 4: Add `/ic-diagnostics` endpoint accepting `ic_series`.
- [ ] Step 5: Run targeted pytest and basedpyright.

### Task 5: P273 — Factor Data Quality

**Files:** create `factor_data_quality.py`; modify `api.py`; test `test_factor_data_quality.py`; append API tests.

- [ ] Step 1: Write tests for missing values, constant feature, outlier z-score, stale repeated values, valid panel producing no critical issues, invalid empty panel raising `ValueError`, and endpoint 200/422.
- [ ] Step 2: Run module tests and confirm failure.
- [ ] Step 3: Implement per-feature coverage, missing count, constant flag, outlier count, stale run count, and aggregate quality score.
- [ ] Step 4: Add `/factor-data-quality` endpoint accepting `panel` as `{feature: [values_or_null]}`.
- [ ] Step 5: Run targeted pytest and basedpyright.

### Task 6: P274 — Signal Persistence

**Files:** create `signal_persistence.py`; modify `api.py`; test `test_signal_persistence.py`; append API tests.

- [ ] Step 1: Write tests for persistent signal having higher autocorrelation than alternating signal, half-life finite when autocorrelation decays, turnover proxy positive, invalid max_lag raises `ValueError`, and endpoint 200/422.
- [ ] Step 2: Run module tests and confirm failure.
- [ ] Step 3: Implement lag autocorrelation curve, first lag below 0.5 as half-life, average absolute delta as turnover proxy, and decay score.
- [ ] Step 4: Add `/signal-persistence` endpoint accepting `signal` and optional `max_lag`.
- [ ] Step 5: Run targeted pytest and basedpyright.

### Task 7: P275 — Strategy Quality

**Files:** create `strategy_quality.py`; modify `api.py`; test `test_strategy_quality.py`; append API tests.

- [ ] Step 1: Write tests for SQN positive on profitable trades, insufficient samples classified as low confidence, expectancy matches average trade, zero variance trades handled without division crash, and endpoint 200/422.
- [ ] Step 2: Run module tests and confirm failure.
- [ ] Step 3: Implement expectancy, trade std, SQN `sqrt(n) * mean / std`, win rate, payoff ratio, sample confidence label.
- [ ] Step 4: Add `/strategy-quality` endpoint accepting `trades`.
- [ ] Step 5: Run targeted pytest and basedpyright.

### Task 8: P276 — Regime Performance

**Files:** create `regime_performance.py`; modify `api.py`; test `test_regime_performance.py`; append API tests.

- [ ] Step 1: Write tests for bull/bear regimes with different returns, contribution shares summing near 1 by absolute contribution, mismatched lengths raising `ValueError`, unknown labels preserved, and endpoint 200/422.
- [ ] Step 2: Run module tests and confirm failure.
- [ ] Step 3: Implement grouped count, mean return, volatility, win rate, total return, and contribution share.
- [ ] Step 4: Add `/regime-performance` endpoint accepting `returns` and `regimes`.
- [ ] Step 5: Run targeted pytest and basedpyright.

### Task 9: P277 — Strategy Diversification

**Files:** create `strategy_diversification.py`; modify `api.py`; test `test_strategy_diversification.py`; append API tests.

- [ ] Step 1: Write tests for identical strategies flagged redundant, negatively correlated strategies lowering average correlation, diversification score in `[0,1]`, mismatched lengths raising `ValueError`, and endpoint 200/422.
- [ ] Step 2: Run module tests and confirm failure.
- [ ] Step 3: Implement correlation matrix, average pairwise correlation, redundant pairs above threshold, and simple diversification score `1 - average_abs_correlation`.
- [ ] Step 4: Add `/strategy-diversification` endpoint accepting `strategies` panel and optional `redundancy_threshold`.
- [ ] Step 5: Run targeted pytest and basedpyright.

### Task 10: P278 — Backtest Confidence

**Files:** create `backtest_confidence.py`; modify `api.py`; test `test_backtest_confidence.py`; append API tests.

- [ ] Step 1: Write tests for deterministic bootstrap CI with seed, rolling Sharpe stability finite, fragility score higher for return series with large negative outlier, invalid bootstrap count raises `ValueError`, and endpoint 200/422.
- [ ] Step 2: Run module tests and confirm failure.
- [ ] Step 3: Implement seeded bootstrap mean return CI, rolling Sharpe over a window, Sharpe std as instability, downside outlier-based fragility score.
- [ ] Step 4: Add `/backtest-confidence` endpoint accepting `returns`, optional `n_bootstrap`, `seed`, and `window`.
- [ ] Step 5: Run targeted pytest and basedpyright.

### Task 11: Final Verification and Docs Sync

**Files:** modify `README.md`, `docs/Roadmap.md`, `CLAUDE.md` if status tables require sync.

- [ ] Step 1: Update docs with P269–P278 one-line descriptions and endpoint list.
- [ ] Step 2: Run all new platform tests plus API harness:

```bash
cd backend && python3 -m pytest \
  tests/platform/test_factor_turnover.py \
  tests/platform/test_factor_decay.py \
  tests/platform/test_factor_quantiles.py \
  tests/platform/test_ic_diagnostics.py \
  tests/platform/test_factor_data_quality.py \
  tests/platform/test_signal_persistence.py \
  tests/platform/test_strategy_quality.py \
  tests/platform/test_regime_performance.py \
  tests/platform/test_strategy_diversification.py \
  tests/platform/test_backtest_confidence.py \
  tests/platform/test_api_risk_portfolio.py -v
```

- [ ] Step 3: Run `cd backend && python3 -m basedpyright app/platform/`.
- [ ] Step 4: Run broader regression if time permits: `cd backend && python3 -m pytest tests/ -q`.
- [ ] Step 5: Inspect `git diff` and ensure no unrelated edits beyond existing P259–P268 plus this P269–P278 batch.

## Self-Review Notes

- Spec coverage: all P269–P278 topics map to Tasks 1–10; docs and verification map to Task 11.
- Placeholder scan: no deferred TBD items; each task names files, behavior, tests, commands, and validation.
- Type consistency: endpoint names, module names, and test file names match the design document.
