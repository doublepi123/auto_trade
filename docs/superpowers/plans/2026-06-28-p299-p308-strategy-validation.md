# P299–P308 Strategy Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship 10 pure-Python strategy validation, adaptive intelligence, and capacity diagnostics modules plus 10 read-only `/api/platform/*` endpoints.

**Architecture:** Each P299–P308 feature is a focused `backend/app/platform/*.py` module exposing a frozen result dataclass and a `*_report(...)` pure function. `backend/app/platform/api.py` adds thin endpoint wrappers that validate JSON payloads and convert `(TypeError, ValueError)` into HTTP 422. Tests are split into module tests plus API 200/422 coverage appended to `backend/tests/platform/test_api_risk_portfolio.py`.

**Tech Stack:** Python 3.11+, FastAPI, pytest, basedpyright, existing `app.platform.factor_utils`; no new runtime dependencies; deterministic `random.Random(seed)` for bootstrap.

---

## Files

Create:
- `backend/app/platform/regime_factor_returns.py`
- `backend/app/platform/transfer_entropy.py`
- `backend/app/platform/event_study.py`
- `backend/app/platform/bootstrap_strategy_significance.py`
- `backend/app/platform/dynamic_factor_exposure.py`
- `backend/app/platform/market_impact_model.py`
- `backend/app/platform/vol_forecast_comparison.py`
- `backend/app/platform/strategy_capacity.py`
- `backend/app/platform/momentum_spillover.py`
- `backend/app/platform/tail_dependence.py`
- corresponding `backend/tests/platform/test_*.py` files (10)

Modify:
- `backend/app/platform/api.py` — 10 endpoint handlers + helpers.
- `backend/tests/platform/test_api_risk_portfolio.py` — endpoint tests.
- `README.md`, `CLAUDE.md`, `docs/Roadmap.md` — final docs.

---

### Task 1: P299 Regime Factor Returns

Slice factor IC/returns/win-rate per regime label.

- Module: `regime_factor_returns_report(factor: dict, returns: dict, regimes: list[str])` with `_validate_map` helpers.
- Endpoint: `POST /api/platform/regime-factor-returns` accepting `factor`, `returns`, `regimes`.

### Task 2: P300 Transfer Entropy

Bidirectional transfer entropy between two series with lag.

- Module: `transfer_entropy_report(source, target, *, lag=1, bins=10)` using histogram joint probabilities.
- Endpoint: `POST /api/platform/transfer-entropy`.

### Task 3: P301 Event Study

Abnormal returns, CAR, t-stats over event window.

- Module: `event_study_report(market_returns, stock_returns, event_indices, *, window_before=5, window_after=5)` with mean/cumulative AR and t-stat.
- Endpoint: `POST /api/platform/event-study`.

### Task 4: P302 Bootstrap Strategy Significance

Bootstrap Sharpe under zero-alpha null, p-value and CI.

- Module: `bootstrap_strategy_significance_report(returns, *, n_bootstrap=1000, seed=42)` using `random.Random(seed)` reshuffling.
- Endpoint: `POST /api/platform/bootstrap-significance`.

### Task 5: P303 Dynamic Factor Exposure

Rolling/EW regression of strategy returns on factor returns.

- Module: `dynamic_factor_exposure_report(strategy_returns, factor_panel, *, window=20)` per-factor beta time series.
- Endpoint: `POST /api/platform/dynamic-factor-exposure`.

### Task 6: P304 Market Impact Model

Square-root/power-law temporary + permanent impact.

- Module: `market_impact_model_report(order_qty, adv, volatility, *, participation=0.1, model="square_root")`.
- Endpoint: `POST /api/platform/market-impact`.

### Task 7: P305 Vol Forecast Comparison

Compare realized vol vs EWMA/GARCH/Parkinson via RMSE/QLIKE/directional.

- Module: `vol_forecast_comparison_report(realized_vol, forecasts_panel)` returning per-model metrics and best model.
- Endpoint: `POST /api/platform/vol-forecast-comparison`.

### Task 8: P306 Strategy Capacity

Signal autocorrelation + ADV + turnover → capacity AUM拐点.

- Module: `strategy_capacity_report(signal_autocorr, adv, turnover, *, impact_threshold_bps=10)`.
- Endpoint: `POST /api/platform/strategy-capacity`.

### Task 9: P307 Momentum Spillover

Cross-asset Granger-like causality + lead-lag window.

- Module: `momentum_spillover_report(leader_returns, lagger_returns, *, max_lag=5)` with F-stat approx and best lag.
- Endpoint: `POST /api/platform/momentum-spillover`.

### Task 10: P308 Tail Dependence

Empirical upper/lower tail dependence coefficients.

- Module: `tail_dependence_report(x, y, *, threshold=0.1)` with empirical and parameter (Gumbel Clayton approx) coefficients.
- Endpoint: `POST /api/platform/tail-dependence`.

---

### Task 11: Documentation, Review, Verification

- Update README/CLAUDE/Roadmap with P299–P308.
- Run target tests + basedpyright + full pytest.
- Oracle review, fix to zero Blocking/Critical/Important.
- Inspect git status/diff; commit/push only on explicit request.
