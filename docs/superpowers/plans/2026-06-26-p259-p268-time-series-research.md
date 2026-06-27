# P259–P268 Time-Series Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship 10 new `app/platform/` time-series research modules plus 10 `/api/platform/*` endpoints, all pure Python, zero new dependencies, deterministic, and covered by unit + API tests.

**Architecture:** Each round is additive: one focused module under `backend/app/platform/`, one thin endpoint in `backend/app/platform/api.py`, one module-level pytest file, and two endpoint checks appended to `backend/tests/platform/test_api_risk_portfolio.py`. No database, no runner integration, no frontend work.

**Tech Stack:** Python 3.11+, FastAPI, pytest, basedpyright, standard library only.

---

## Conventions for Every Round

- Module starts with `from __future__ import annotations` and a docstring describing the approximation.
- Export public functions and dataclasses via `__all__`.
- Validate empty series, length mismatch, non-finite numbers, and impossible parameters with `ValueError`.
- Dataclasses are `@dataclass(frozen=True)` and expose `to_dict() -> dict[str, Any]`.
- Endpoint catches `(TypeError, ValueError)` and returns `HTTPException(status_code=422, detail=str(exc))`.
- API inputs cap series length at 5000 where relevant.
- Tests use deterministic small arrays and avoid fragile exact floating-point equality except where formulas are exact.
- Do not commit unless the user explicitly requests it.

## Shared API Helpers

Modify `backend/app/platform/api.py` near `_fractional_series()`:

- Add `_numeric_series(payload, key="series", min_len=1, max_len=5000) -> list[float]`.
- Add `_numeric_panel(payload, key, min_len=2, max_len=5000) -> dict[str, list[float]]` for factor/signal panels.
- Reuse existing `_finite_number()`.

These helpers keep endpoint parsing thin and consistent.

---

### Task 1: P259 — Spectral Analysis

**Files:**
- Create: `backend/app/platform/spectral_analysis.py`
- Modify: `backend/app/platform/api.py`
- Test: `backend/tests/platform/test_spectral_analysis.py`
- Modify: `backend/tests/platform/test_api_risk_portfolio.py`

- [ ] Step 1: Write failing tests for `periodogram([0, 1, 0, -1] * 4)` detecting frequency bin 4, spectral entropy in `[0, 1]`, band energy share in `[0, 1]`, empty input raising `ValueError`, and `/api/platform/spectral-analysis` 200/422.
- [ ] Step 2: Run `cd backend && python3 -m pytest tests/platform/test_spectral_analysis.py -v`; expect import failure.
- [ ] Step 3: Implement naive DFT using `math.cos/sin`, `SpectralAnalysisResult`, `periodogram()`, `spectral_report(series, sample_rate=1.0, bands=None)`.
- [ ] Step 4: Add endpoint `/spectral-analysis` returning `spectral_report(...).to_dict()`.
- [ ] Step 5: Run `cd backend && python3 -m pytest tests/platform/test_spectral_analysis.py tests/platform/test_api_risk_portfolio.py -v`.
- [ ] Step 6: Run `cd backend && python3 -m basedpyright app/platform/spectral_analysis.py app/platform/api.py`.

### Task 2: P260 — Cycle Detection

**Files:** create `cycle_detection.py`; modify `api.py`; test `test_cycle_detection.py`; append API tests.

- [ ] Step 1: Write tests for autocorrelation peak on a period-5 sine-like sequence, Ljung-Box approximation non-negative, seasonal strength higher for repeated pattern than noise, invalid `max_lag < 2` raising `ValueError`, and endpoint 200/422.
- [ ] Step 2: Run module tests and confirm failure.
- [ ] Step 3: Implement `autocorrelation(series, max_lag)`, `ljung_box_stat(acf, n)`, `detect_cycles(series, min_period=2, max_period=None)` and `CycleDetectionResult.to_dict()`.
- [ ] Step 4: Add `/cycle-detection` endpoint.
- [ ] Step 5: Run targeted pytest and basedpyright.

### Task 3: P261 — Change Point Detection

**Files:** create `change_point.py`; modify `api.py`; test `test_change_point.py`; append API tests.

- [ ] Step 1: Write tests where `[1]*20 + [5]*20` detects a change near index 20, variance shift score is positive, no-change series returns low confidence, short series raises `ValueError`, and endpoint 200/422.
- [ ] Step 2: Run module tests and confirm failure.
- [ ] Step 3: Implement prefix-sum mean split scoring, variance split scoring, recursive binary segmentation with `min_size`, `ChangePoint` and `ChangePointResult` dataclasses.
- [ ] Step 4: Add `/change-point` endpoint.
- [ ] Step 5: Run targeted pytest and basedpyright.

### Task 4: P262 — Entropy and Complexity

**Files:** create `entropy_complexity.py`; modify `api.py`; test `test_entropy_complexity.py`; append API tests.

- [ ] Step 1: Write tests for Shannon entropy of constant series equal 0, permutation entropy of monotonic series below shuffled pattern, sample entropy finite for repeated values, Hurst exponent in `[0, 1]`, invalid embedding dimension raising `ValueError`, and endpoint 200/422.
- [ ] Step 2: Run module tests and confirm failure.
- [ ] Step 3: Implement binned Shannon entropy, sample entropy with tolerance, permutation entropy via ordinal patterns, rescaled-range Hurst estimate, and `EntropyComplexityResult`.
- [ ] Step 4: Add `/entropy-complexity` endpoint.
- [ ] Step 5: Run targeted pytest and basedpyright.

### Task 5: P263 — Rolling Features

**Files:** create `rolling_features.py`; modify `api.py`; test `test_rolling_features.py`; append API tests.

- [ ] Step 1: Write tests for rolling mean/std/zscore on `[1,2,3,4]`, EWMA matching recursive formula, rolling beta matching identical series near 1, invalid window raising `ValueError`, and endpoint 200/422.
- [ ] Step 2: Run module tests and confirm failure.
- [ ] Step 3: Implement `rolling_mean`, `rolling_std`, `rolling_zscore`, `rolling_skew`, `rolling_kurtosis`, `ewma`, `rolling_beta`, and `RollingFeatureResult`.
- [ ] Step 4: Add `/rolling-features` endpoint.
- [ ] Step 5: Run targeted pytest and basedpyright.

### Task 6: P264 — Factor IC Analysis

**Files:** create `factor_ic.py`; modify `api.py`; test `test_factor_ic.py`; append API tests.

- [ ] Step 1: Write tests for Pearson IC positive on aligned factor/forward returns, Spearman rank IC near 1 on monotonic ranks, quantile spread positive, ICIR finite, mismatched lengths raising `ValueError`, and endpoint 200/422.
- [ ] Step 2: Run module tests and confirm failure.
- [ ] Step 3: Implement `pearson_corr`, `rank_values`, `spearman_corr`, quantile bucket return aggregation, `factor_ic_report(factor, forward_returns, n_quantiles=5)`, and `FactorICResult`.
- [ ] Step 4: Add `/factor-ic` endpoint.
- [ ] Step 5: Run targeted pytest and basedpyright.

### Task 7: P265 — Feature Orthogonalization

**Files:** create `feature_orthogonalization.py`; modify `api.py`; test `test_feature_orthogonalization.py`; append API tests.

- [ ] Step 1: Write tests for Gram-Schmidt producing near-zero dot product, residualization removing linear exposure, correlation pruning dropping duplicate feature, VIF higher for duplicated feature than independent feature, empty panel raising `ValueError`, and endpoint 200/422.
- [ ] Step 2: Run module tests and confirm failure.
- [ ] Step 3: Implement vector dot/norm helpers, `gram_schmidt(panel)`, `residualize(target, exposures)`, `correlation_prune(panel, threshold)`, `vif_scores(panel)`, and `OrthogonalizationResult`.
- [ ] Step 4: Add `/feature-orthogonalization` endpoint.
- [ ] Step 5: Run targeted pytest and basedpyright.

### Task 8: P266 — Signal Combination

**Files:** create `signal_combination.py`; modify `api.py`; test `test_signal_combination.py`; append API tests.

- [ ] Step 1: Write tests for zscore standardization mean near 0, rank-combine ordering, normalized weights summing to 1 by absolute value, risk-budget weighting lowering high-vol signal weight, mismatched signal lengths raising `ValueError`, and endpoint 200/422.
- [ ] Step 2: Run module tests and confirm failure.
- [ ] Step 3: Implement `standardize_signal`, `rank_signal`, `normalize_weights`, `combine_signals(signals, weights=None, method="zscore")`, `risk_budget_weights(signals)`, and `SignalCombinationResult`.
- [ ] Step 4: Add `/signal-combination` endpoint.
- [ ] Step 5: Run targeted pytest and basedpyright.

### Task 9: P267 — Backtest Diagnostics

**Files:** create `backtest_diagnostics.py`; modify `api.py`; test `test_backtest_diagnostics.py`; append API tests.

- [ ] Step 1: Write tests for expectancy, profit factor, payoff ratio, win/loss streaks, deterministic bootstrap confidence interval with seed, zero-loss trades producing infinite profit factor, and endpoint 200/422.
- [ ] Step 2: Run module tests and confirm failure.
- [ ] Step 3: Implement `trade_expectancy`, `profit_factor`, `payoff_ratio`, `streaks`, `bootstrap_expectancy_ci(trades, n_bootstrap, seed)`, and `BacktestDiagnosticsResult`.
- [ ] Step 4: Add `/backtest-diagnostics` endpoint.
- [ ] Step 5: Run targeted pytest and basedpyright.

### Task 10: P268 — Data Quality Diagnostics

**Files:** create `data_quality.py`; modify `api.py`; test `test_data_quality.py`; append API tests.

- [ ] Step 1: Write tests for missing timestamp gaps, duplicate timestamps, stale prices, outlier jumps, OHLC consistency violations, valid bars returning zero critical issues, and endpoint 200/422.
- [ ] Step 2: Run module tests and confirm failure.
- [ ] Step 3: Implement `BarQualityIssue`, `DataQualityResult`, `check_timestamp_quality`, `check_price_quality`, `check_ohlc_consistency`, and `data_quality_report(bars, expected_interval_seconds=None)`.
- [ ] Step 4: Add `/data-quality` endpoint.
- [ ] Step 5: Run targeted pytest and basedpyright.

### Task 11: Final Verification and Docs Sync

**Files:** modify `README.md`, `docs/Roadmap.md`, optionally `CLAUDE.md` if this repository keeps status there.

- [ ] Step 1: Update docs with P259–P268 endpoint list and one-line descriptions.
- [ ] Step 2: Run all new platform tests plus API harness:

```bash
cd backend && python3 -m pytest \
  tests/platform/test_spectral_analysis.py \
  tests/platform/test_cycle_detection.py \
  tests/platform/test_change_point.py \
  tests/platform/test_entropy_complexity.py \
  tests/platform/test_rolling_features.py \
  tests/platform/test_factor_ic.py \
  tests/platform/test_feature_orthogonalization.py \
  tests/platform/test_signal_combination.py \
  tests/platform/test_backtest_diagnostics.py \
  tests/platform/test_data_quality.py \
  tests/platform/test_api_risk_portfolio.py -v
```

- [ ] Step 3: Run `cd backend && python3 -m basedpyright app/platform/`.
- [ ] Step 4: Run broader regression if time permits: `cd backend && python3 -m pytest tests/ -q`.
- [ ] Step 5: Inspect `git diff` and ensure no unrelated edits.

## Self-Review Notes

- Spec coverage: all P259–P268 topics map to Tasks 1–10; API, tests, docs, and verification map to Task 11.
- Placeholder scan: no deferred TBD/TODO items; each task names files, behavior, tests, commands, and validation.
- Type consistency: endpoint names, module names, and test file names match the design document.
