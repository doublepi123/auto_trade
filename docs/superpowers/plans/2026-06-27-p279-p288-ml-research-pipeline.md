# P279–P288 ML Research Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship 10 pure-Python ML research and fast signal-validation modules plus 10 `/api/platform/*` read-only endpoints.

**Architecture:** Each round is additive: one module under `backend/app/platform/`, one thin endpoint in `backend/app/platform/api.py`, one module-level pytest file, and API checks in `backend/tests/platform/test_api_risk_portfolio.py`. All code is deterministic, standard-library only, and avoids runner/broker/DB write paths.

**Tech Stack:** Python 3.11+, FastAPI, pytest, basedpyright, standard library only.

---

## Shared Conventions

- Every module starts with `from __future__ import annotations`.
- Every result is a frozen dataclass with `to_dict() -> dict[str, Any]`.
- Invalid input raises `ValueError`; endpoint handlers convert `(TypeError, ValueError)` to HTTP 422.
- API handlers reuse `_finite_number`, `_numeric_series`, and `_panel_field` where possible.
- Do not introduce new dependencies, DB tables, runner hooks, broker calls, or frontend code.

## File Map

- Create `forecast_diagnostics.py`, `triple_barrier.py`, `sample_uniqueness.py`, `bar_builder.py`, `factor_neutralization.py`, `factor_tearsheet.py`, `feature_pipeline.py`, `signal_backtest.py`, `rolling_tearsheet.py`, `portfolio_constraints.py`.
- Modify `backend/app/platform/api.py` with P279–P288 endpoint handlers.
- Create `backend/tests/platform/test_<module>.py` for each module.
- Modify `backend/tests/platform/test_api_risk_portfolio.py` with endpoint 200/422 coverage.
- Modify `README.md`, `docs/Roadmap.md`, and `CLAUDE.md` for status sync.

---

### Task 1: P279 Forecast Diagnostics

**Files:** create `backend/app/platform/forecast_diagnostics.py`; modify `api.py`; test `test_forecast_diagnostics.py`; append API tests.

- [ ] Write failing tests for perfect prediction, inverse prediction, bucket spread, benchmark alpha/beta, and length mismatch.
- [ ] Implement MSE, MAE, bias, directional accuracy, Pearson/Spearman IC, bucket returns, optional benchmark alpha/beta/information ratio.
- [ ] Add `/forecast-diagnostics` endpoint accepting `predictions`, `actuals`, optional `benchmark`, `n_buckets`.
- [ ] Run targeted pytest and basedpyright.

### Task 2: P280 Triple Barrier Labels

**Files:** create `triple_barrier.py`; modify `api.py`; test `test_triple_barrier.py`; append API tests.

- [ ] Write failing tests for long profit-take, long stop-loss, short profit-take, timeout, and invalid barrier params.
- [ ] Implement event validation and price-path scanning for `profit_take`, `stop_loss`, `timeout` labels.
- [ ] Add `/triple-barrier-labels` endpoint accepting `prices`, `events`, `profit_take_pct`, `stop_loss_pct`, `max_holding_bars`.
- [ ] Run targeted pytest and basedpyright.

### Task 3: P281 Sample Uniqueness

**Files:** create `sample_uniqueness.py`; modify `api.py`; test `test_sample_uniqueness.py`; append API tests.

- [ ] Write failing tests for non-overlap uniqueness 1.0, overlap lower uniqueness, time-decay weights, invalid ranges.
- [ ] Implement concurrency vector, per-event uniqueness, average concurrency, weighted absolute-return sample weights.
- [ ] Add `/sample-uniqueness` endpoint accepting event ranges and optional `time_decay`.
- [ ] Run targeted pytest and basedpyright.

### Task 4: P282 Bar Builder

**Files:** create `bar_builder.py`; modify `api.py`; test `test_bar_builder.py`; append API tests.

- [ ] Write failing tests for tick bars, volume bars, dollar bars, OHLC correctness, invalid tick order/value.
- [ ] Implement `build_bars(ticks, mode, threshold)` for `tick`, `volume`, `dollar`.
- [ ] Add `/bar-builder` endpoint accepting `ticks`, `mode`, `threshold`.
- [ ] Run targeted pytest and basedpyright.

### Task 5: P283 Factor Neutralization

**Files:** create `factor_neutralization.py`; modify `api.py`; test `test_factor_neutralization.py`; append API tests.

- [ ] Write failing tests for market demean, group demean, group zscore, residualize, key mismatch.
- [ ] Implement cross-sectional neutralization plus small ridge OLS residualization.
- [ ] Add `/factor-neutralization` endpoint accepting `factor`, `method`, optional `groups`/`exposures`.
- [ ] Run targeted pytest and basedpyright.

### Task 6: P284 Factor Tearsheet

**Files:** create `factor_tearsheet.py`; modify `api.py`; test `test_factor_tearsheet.py`; append API tests.

- [ ] Write failing tests for per-date IC, quantile spread, turnover summary, quality summary, empty records.
- [ ] Implement record grouping by date/symbol and aggregate existing factor diagnostics.
- [ ] Add `/factor-tearsheet` endpoint accepting `records`, optional `n_quantiles`, `bucket_fraction`.
- [ ] Run targeted pytest and basedpyright.

### Task 7: P285 Feature Pipeline

**Files:** create `feature_pipeline.py`; modify `api.py`; test `test_feature_pipeline.py`; append API tests.

- [ ] Write failing tests for return, sma, lag, delta, cross-sectional rank, unknown op, missing input.
- [ ] Implement whitelist ops: `return`, `sma`, `lag`, `delta`, `zscore`, `rank`; reject unknown ops.
- [ ] Add `/feature-pipeline` endpoint accepting `price_panel` and `features`.
- [ ] Run targeted pytest and basedpyright.

### Task 8: P286 Signal Backtest

**Files:** create `signal_backtest.py`; modify `api.py`; test `test_signal_backtest.py`; append API tests.

- [ ] Write failing tests for one long trade PnL, fee/slippage effect, target position mode, length mismatch.
- [ ] Implement entry/exit mode and target-position mode equity/trade stats.
- [ ] Add `/signal-backtest` endpoint accepting `prices` plus either `entries`/`exits` or `target_positions`.
- [ ] Run targeted pytest and basedpyright.

### Task 9: P287 Rolling Tearsheet

**Files:** create `rolling_tearsheet.py`; modify `api.py`; test `test_rolling_tearsheet.py`; append API tests.

- [ ] Write failing tests for rolling Sharpe, rolling MDD, rolling beta/alpha with benchmark, invalid window.
- [ ] Implement multi-window rolling metrics with leading `None` values.
- [ ] Add `/rolling-tearsheet` endpoint accepting `returns`, optional `benchmark`, `windows`, `periods_per_year`.
- [ ] Run targeted pytest and basedpyright.

### Task 10: P288 Portfolio Constraints

**Files:** create `portfolio_constraints.py`; modify `api.py`; test `test_portfolio_constraints.py`; append API tests.

- [ ] Write failing tests for passing constraints, group overweight, position overweight, turnover breach, ADV capacity breach.
- [ ] Implement gross/net exposure, turnover, group weights, capacity, violations list.
- [ ] Add `/portfolio-constraints` endpoint accepting `weights`, optional `prev_weights`, `groups`, `adv`, `nav`, `constraints`.
- [ ] Run targeted pytest and basedpyright.

### Task 11: Docs, Review, Final Verification

**Files:** modify `README.md`, `docs/Roadmap.md`, `CLAUDE.md`.

- [ ] Update docs with P279–P288 endpoint list and one-line descriptions.
- [ ] Run `python3 -m pytest --no-cov` for all new module tests and `tests/platform/test_api_risk_portfolio.py`.
- [ ] Run `python3 -m basedpyright app/platform/` or targeted new modules plus `api.py`.
- [ ] Run `python3 -m pytest tests/ -q`.
- [ ] Request Oracle review and fix Critical/Important issues.
- [ ] Inspect git diff, commit, and push main if verification passes.

## Self-Review Notes

- Spec coverage: P279–P288 map to Tasks 1–10; docs/review/verification map to Task 11.
- Placeholder scan: no TBD/TODO placeholders; each task names concrete files, behavior, tests, and commands.
- Type consistency: module names, endpoint names, and test names match the design document.
