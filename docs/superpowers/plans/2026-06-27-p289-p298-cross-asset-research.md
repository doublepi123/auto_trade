# P289–P298 Cross-Asset Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship 10 pure-Python cross-asset research, pre-trade intelligence, and signal diagnostics modules plus 10 read-only `/api/platform/*` endpoints.

**Architecture:** Each P289–P298 feature is a focused `backend/app/platform/*.py` module exposing a frozen result dataclass and a `*_report(...)` pure function. `backend/app/platform/api.py` adds thin endpoint wrappers that validate JSON payloads and convert `(TypeError, ValueError)` into HTTP 422. Tests are split into module tests plus API 200/422 coverage appended to `backend/tests/platform/test_api_risk_portfolio.py`.

**Tech Stack:** Python 3.11+, FastAPI, pytest, basedpyright, existing `app.platform.factor_utils`; no new runtime dependencies.

---

## Files

Create:
- `backend/app/platform/cross_sectional_dispersion.py`
- `backend/app/platform/variance_risk_premium.py`
- `backend/app/platform/pretrade_cost.py`
- `backend/app/platform/ensemble_blending.py`
- `backend/app/platform/option_implied_moments.py`
- `backend/app/platform/correlation_regime.py`
- `backend/app/platform/factor_crowding.py`
- `backend/app/platform/curve_spread.py`
- `backend/app/platform/turnover_attribution.py`
- `backend/app/platform/signal_information_ratio.py`
- `backend/tests/platform/test_cross_sectional_dispersion.py`
- `backend/tests/platform/test_variance_risk_premium.py`
- `backend/tests/platform/test_pretrade_cost.py`
- `backend/tests/platform/test_ensemble_blending.py`
- `backend/tests/platform/test_option_implied_moments.py`
- `backend/tests/platform/test_correlation_regime.py`
- `backend/tests/platform/test_factor_crowding.py`
- `backend/tests/platform/test_curve_spread.py`
- `backend/tests/platform/test_turnover_attribution.py`
- `backend/tests/platform/test_signal_information_ratio.py`

Modify:
- `backend/app/platform/api.py` — add 10 endpoint handlers and any small validation helper needed by P289–P298.
- `backend/tests/platform/test_api_risk_portfolio.py` — append P289–P298 endpoint tests.
- `README.md` — add endpoint rows.
- `CLAUDE.md` — add module map lines.
- `docs/Roadmap.md` — add P289–P298 completion section after verification.

---

### Task 1: P289 Cross-Sectional Dispersion

**Files:**
- Create: `backend/app/platform/cross_sectional_dispersion.py`
- Create: `backend/tests/platform/test_cross_sectional_dispersion.py`
- Modify: `backend/app/platform/api.py`
- Modify: `backend/tests/platform/test_api_risk_portfolio.py`

- [ ] **Step 1: Write failing module tests**

```python
from __future__ import annotations

import pytest

from app.platform.cross_sectional_dispersion import cross_sectional_dispersion_report


def test_cross_sectional_dispersion_reports_spread_and_gini():
    body = cross_sectional_dispersion_report({"A": 0.01, "B": -0.02, "C": 0.04}).to_dict()
    assert body["count"] == 3
    assert body["dispersion"]["range"] == pytest.approx(0.06)
    assert body["dispersion"]["iqr"] > 0
    assert body["dispersion"]["gini"] > 0
    assert body["opportunity_score"] > 0


def test_cross_sectional_dispersion_rejects_bad_inputs():
    with pytest.raises(ValueError):
        cross_sectional_dispersion_report({"A": 0.01})
    with pytest.raises(ValueError):
        cross_sectional_dispersion_report({"A": float("nan"), "B": 0.1})
```

- [ ] **Step 2: Verify RED**

Run: `cd backend && python3 -m pytest --no-cov tests/platform/test_cross_sectional_dispersion.py -q`

Expected: import failure for missing `app.platform.cross_sectional_dispersion`.

- [ ] **Step 3: Implement module**

Implement `cross_sectional_dispersion_report(returns: dict[str, float])`, validating at least two finite numeric values. Compute mean, standard deviation, median absolute deviation, min/max/range, Q1/Q3/IQR using sorted nearest-rank positions, Gini over absolute returns, top-bottom spread, and `opportunity_score = std + iqr + abs(top_bottom_spread)`.

- [ ] **Step 4: Add API tests and endpoint**

Append API tests for `POST /api/platform/cross-sectional-dispersion` 200 and 422. Endpoint uses `_dict_float_field(payload, "returns")` and returns `result.to_dict()`.

- [ ] **Step 5: Verify GREEN**

Run: `cd backend && python3 -m pytest --no-cov tests/platform/test_cross_sectional_dispersion.py tests/platform/test_api_risk_portfolio.py::test_cross_sectional_dispersion_endpoint_200_and_422 -q`

Expected: all selected tests pass.

---

### Task 2: P290 Variance Risk Premium

**Files:**
- Create: `backend/app/platform/variance_risk_premium.py`
- Create: `backend/tests/platform/test_variance_risk_premium.py`
- Modify: `backend/app/platform/api.py`
- Modify: `backend/tests/platform/test_api_risk_portfolio.py`

- [ ] **Step 1: Write failing module tests**

```python
from __future__ import annotations

import pytest

from app.platform.variance_risk_premium import variance_risk_premium_report


def test_variance_risk_premium_reports_positive_premium():
    body = variance_risk_premium_report([0.01, -0.02, 0.015], [0.25, 0.24, 0.26], periods_per_year=252).to_dict()
    assert body["latest"]["implied_variance"] > body["latest"]["realized_variance"]
    assert body["latest"]["vrp"] > 0
    assert body["summary"]["mean_vrp"] > 0


def test_variance_risk_premium_rejects_mismatched_inputs():
    with pytest.raises(ValueError):
        variance_risk_premium_report([0.01], [0.2, 0.3])
    with pytest.raises(ValueError):
        variance_risk_premium_report([0.01, 0.02], [0.0, 0.2])
```

- [ ] **Step 2: Verify RED**

Run: `cd backend && python3 -m pytest --no-cov tests/platform/test_variance_risk_premium.py -q`

Expected: import failure for missing module.

- [ ] **Step 3: Implement module**

Implement realized variance as rolling expanding variance of returns annualized by `periods_per_year`; implied variance as `iv ** 2`; VRP as implied minus realized; z-score from VRP history; state label `rich`/`cheap`/`neutral` based on latest z-score ±1.

- [ ] **Step 4: Add API tests and endpoint**

Endpoint `POST /api/platform/variance-risk-premium` accepts `returns`, `implied_vols`, optional int `periods_per_year`; 422 on mismatched or non-positive IV.

- [ ] **Step 5: Verify GREEN**

Run: `cd backend && python3 -m pytest --no-cov tests/platform/test_variance_risk_premium.py tests/platform/test_api_risk_portfolio.py::test_variance_risk_premium_endpoint_200_and_422 -q`

---

### Task 3: P291 Pre-Trade Cost

**Files:**
- Create: `backend/app/platform/pretrade_cost.py`
- Create: `backend/tests/platform/test_pretrade_cost.py`
- Modify: `backend/app/platform/api.py`
- Modify: `backend/tests/platform/test_api_risk_portfolio.py`

- [ ] **Step 1: Write failing module tests**

```python
from __future__ import annotations

import pytest

from app.platform.pretrade_cost import pretrade_cost_report


def test_pretrade_cost_increases_with_participation():
    low = pretrade_cost_report(order_qty=100, adv=10000, price=10, spread_bps=5, volatility=0.2).to_dict()
    high = pretrade_cost_report(order_qty=2000, adv=10000, price=10, spread_bps=5, volatility=0.2).to_dict()
    assert high["total_cost_bps"] > low["total_cost_bps"]
    assert high["notional"] == 20000


def test_pretrade_cost_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        pretrade_cost_report(order_qty=0, adv=10000, price=10)
    with pytest.raises(ValueError):
        pretrade_cost_report(order_qty=100, adv=0, price=10)
```

- [ ] **Step 2: Verify RED**

Run: `cd backend && python3 -m pytest --no-cov tests/platform/test_pretrade_cost.py -q`

- [ ] **Step 3: Implement module**

Compute participation `order_qty / adv`, half-spread cost, square-root market impact `impact_coefficient * volatility * sqrt(participation) * 10000`, timing risk `volatility * participation * 100`, total bps and currency cost. Return an `efficient_frontier` list for participation levels `[0.01, 0.05, 0.10, 0.20]` capped to positive levels.

- [ ] **Step 4: Add API tests and endpoint**

Endpoint `POST /api/platform/pretrade-cost` maps scalar finite fields and returns 422 for non-positive qty/ADV/price.

- [ ] **Step 5: Verify GREEN**

Run selected module and endpoint tests.

---

### Task 4: P292 Ensemble Blending

**Files:**
- Create: `backend/app/platform/ensemble_blending.py`
- Create: `backend/tests/platform/test_ensemble_blending.py`
- Modify: `backend/app/platform/api.py`
- Modify: `backend/tests/platform/test_api_risk_portfolio.py`

- [ ] **Step 1: Write failing module tests**

```python
from __future__ import annotations

import pytest

from app.platform.ensemble_blending import ensemble_blending_report


def test_ensemble_blending_favors_more_accurate_model():
    body = ensemble_blending_report({"good": [1, 2, 3], "bad": [3, 2, 1]}, [1, 2, 3]).to_dict()
    assert body["weights"]["good"] > body["weights"]["bad"]
    assert body["ensemble_r2"] >= body["model_scores"]["bad"]["r2"]


def test_ensemble_blending_rejects_length_mismatch():
    with pytest.raises(ValueError):
        ensemble_blending_report({"m": [1, 2]}, [1, 2, 3])
```

- [ ] **Step 2: Verify RED**

Run module test and expect missing module failure.

- [ ] **Step 3: Implement module**

Validate a non-empty dict of prediction series matching actuals length. Score each model by MSE and R². Convert inverse-MSE to normalized weights. Compute blended predictions, ensemble MSE/R², contributions, and redundant pairs where absolute Pearson correlation exceeds `redundancy_threshold`.

- [ ] **Step 4: Add API tests and endpoint**

Endpoint accepts `predictions_panel`, `actuals`, optional `redundancy_threshold`.

- [ ] **Step 5: Verify GREEN**

Run selected tests.

---

### Task 5: P293 Option Implied Moments

**Files:**
- Create: `backend/app/platform/option_implied_moments.py`
- Create: `backend/tests/platform/test_option_implied_moments.py`
- Modify: `backend/app/platform/api.py`
- Modify: `backend/tests/platform/test_api_risk_portfolio.py`

- [ ] **Step 1: Write failing module tests**

```python
from __future__ import annotations

import pytest

from app.platform.option_implied_moments import option_implied_moments_report


def test_option_implied_moments_reports_skew_and_term_structure():
    options = [
        {"strike": 90, "iv": 0.30, "expiry": 30},
        {"strike": 100, "iv": 0.22, "expiry": 30},
        {"strike": 110, "iv": 0.20, "expiry": 30},
        {"strike": 100, "iv": 0.25, "expiry": 60},
    ]
    body = option_implied_moments_report(options, spot=100).to_dict()
    assert body["smile"]["skew"] < 0
    assert body["term_structure"]["slope"] > 0


def test_option_implied_moments_rejects_invalid_option():
    with pytest.raises(ValueError):
        option_implied_moments_report([{"strike": 0, "iv": 0.2, "expiry": 30}], spot=100)
```

- [ ] **Step 2: Verify RED**

Run module test and expect missing module failure.

- [ ] **Step 3: Implement module**

Validate options list with positive `strike`, `iv`, and `expiry`. Compute moneyness, ATM IV nearest spot, put-wing/call-wing averages, skew, curvature, term slope by average IV of min/max expiries, and approximate risk-neutral variance/skew/kurtosis from IV distribution weights.

- [ ] **Step 4: Add API tests and endpoint**

Endpoint accepts `options` list and positive `spot`.

- [ ] **Step 5: Verify GREEN**

Run selected tests.

---

### Task 6: P294 Correlation Regime

**Files:**
- Create: `backend/app/platform/correlation_regime.py`
- Create: `backend/tests/platform/test_correlation_regime.py`
- Modify: `backend/app/platform/api.py`
- Modify: `backend/tests/platform/test_api_risk_portfolio.py`

- [ ] **Step 1: Write failing module tests**

```python
from __future__ import annotations

import pytest

from app.platform.correlation_regime import correlation_regime_report


def test_correlation_regime_detects_high_correlation():
    panel = {"A": [0.01, 0.02, 0.03], "B": [0.02, 0.04, 0.06], "C": [-0.01, -0.02, -0.03]}
    body = correlation_regime_report(panel).to_dict()
    assert body["average_correlation"] != 0
    assert body["regime"] in {"diversified", "normal", "concentrated", "stress"}


def test_correlation_regime_rejects_small_panel():
    with pytest.raises(ValueError):
        correlation_regime_report({"A": [0.01, 0.02]})
```

- [ ] **Step 2: Verify RED**

Run module test and expect missing module failure.

- [ ] **Step 3: Implement module**

Validate at least two equal-length return series. Build pairwise Pearson correlation matrix. Approximate largest eigenvalue with 25-step power iteration. Return average correlation, concentration ratio `lambda_max / n`, matrix, and regime labels using thresholds.

- [ ] **Step 4: Add API tests and endpoint**

Endpoint accepts `returns_panel` using existing `_panel_field`.

- [ ] **Step 5: Verify GREEN**

Run selected tests.

---

### Task 7: P295 Factor Crowding

**Files:**
- Create: `backend/app/platform/factor_crowding.py`
- Create: `backend/tests/platform/test_factor_crowding.py`
- Modify: `backend/app/platform/api.py`
- Modify: `backend/tests/platform/test_api_risk_portfolio.py`

- [ ] **Step 1: Write failing module tests**

```python
from __future__ import annotations

import pytest

from app.platform.factor_crowding import factor_crowding_report


def test_factor_crowding_reports_high_signal_concentration():
    body = factor_crowding_report({"A": 10, "B": 1, "C": 0.5}, valuations={"A": 50, "B": 10, "C": 8}, flows={"A": 5, "B": 1, "C": 0}).to_dict()
    assert body["crowding_score"] > 0
    assert body["components"]["signal_concentration"] > 0


def test_factor_crowding_rejects_mismatched_optional_maps():
    with pytest.raises(ValueError):
        factor_crowding_report({"A": 1, "B": 2}, valuations={"A": 10})
```

- [ ] **Step 2: Verify RED**

Run module test and expect missing module failure.

- [ ] **Step 3: Implement module**

Validate factor map with at least two assets. Compute normalized absolute signal Herfindahl, top-bottom valuation spread if valuations provided, flow concentration if flows provided, and weighted `crowding_score` average over available components.

- [ ] **Step 4: Add API tests and endpoint**

Endpoint accepts `factor`, optional `valuations`, optional `flows`.

- [ ] **Step 5: Verify GREEN**

Run selected tests.

---

### Task 8: P296 Curve Spread

**Files:**
- Create: `backend/app/platform/curve_spread.py`
- Create: `backend/tests/platform/test_curve_spread.py`
- Modify: `backend/app/platform/api.py`
- Modify: `backend/tests/platform/test_api_risk_portfolio.py`

- [ ] **Step 1: Write failing module tests**

```python
from __future__ import annotations

import pytest

from app.platform.curve_spread import curve_spread_report


def test_curve_spread_reports_slope_and_roll_down():
    body = curve_spread_report({1: 0.03, 5: 0.04, 10: 0.045}, short_tenor=1, long_tenor=10, history=[0.01, 0.012, 0.015]).to_dict()
    assert body["spread"] == pytest.approx(0.015)
    assert body["roll_down"] != 0
    assert "z_score" in body


def test_curve_spread_rejects_missing_tenor():
    with pytest.raises(ValueError):
        curve_spread_report({1: 0.03, 5: 0.04}, short_tenor=1, long_tenor=10)
```

- [ ] **Step 2: Verify RED**

Run module test and expect missing module failure.

- [ ] **Step 3: Implement module**

Validate curve mapping from tenor to yield. Compute spread, nearest-neighbor roll-down for long tenor minus current long yield, carry as spread, z-score versus optional history, and signal `steepener`/`flattener`/`neutral`.

- [ ] **Step 4: Add API tests and endpoint**

Endpoint accepts curve as string-key dict and converts keys to floats.

- [ ] **Step 5: Verify GREEN**

Run selected tests.

---

### Task 9: P297 Turnover Attribution

**Files:**
- Create: `backend/app/platform/turnover_attribution.py`
- Create: `backend/tests/platform/test_turnover_attribution.py`
- Modify: `backend/app/platform/api.py`
- Modify: `backend/tests/platform/test_api_risk_portfolio.py`

- [ ] **Step 1: Write failing module tests**

```python
from __future__ import annotations

import pytest

from app.platform.turnover_attribution import turnover_attribution_report


def test_turnover_attribution_splits_drift_and_rebalance():
    body = turnover_attribution_report({"A": 0.5, "B": 0.5}, {"A": 0.7, "B": 0.3}, drifted_weights={"A": 0.6, "B": 0.4}).to_dict()
    assert body["total_turnover"] == pytest.approx(0.2)
    assert body["components"]["drift_turnover"] == pytest.approx(0.1)
    assert body["components"]["rebalance_turnover"] == pytest.approx(0.1)


def test_turnover_attribution_rejects_empty_current():
    with pytest.raises(ValueError):
        turnover_attribution_report({"A": 1.0}, {})
```

- [ ] **Step 2: Verify RED**

Run module test and expect missing module failure.

- [ ] **Step 3: Implement module**

Validate previous/current weights. Compute half-sum absolute turnover. If drifted weights provided, split previous→drifted and drifted→current; otherwise drift is zero and rebalance equals total. Track entered/exited assets and per-asset deltas.

- [ ] **Step 4: Add API tests and endpoint**

Endpoint accepts `prev_weights`, `current_weights`, optional `drifted_weights`.

- [ ] **Step 5: Verify GREEN**

Run selected tests.

---

### Task 10: P298 Signal Information Ratio

**Files:**
- Create: `backend/app/platform/signal_information_ratio.py`
- Create: `backend/tests/platform/test_signal_information_ratio.py`
- Modify: `backend/app/platform/api.py`
- Modify: `backend/tests/platform/test_api_risk_portfolio.py`

- [ ] **Step 1: Write failing module tests**

```python
from __future__ import annotations

import pytest

from app.platform.signal_information_ratio import signal_information_ratio_report


def test_signal_information_ratio_reports_positive_ir():
    body = signal_information_ratio_report([0.1, 0.2, 0.3, 0.4], [0.01, 0.02, 0.03, 0.04], periods_per_year=252, n_buckets=2).to_dict()
    assert body["information_ratio"] > 0
    assert body["bucket_quality"]["top_bottom_spread"] > 0


def test_signal_information_ratio_rejects_bad_bucket_count():
    with pytest.raises(ValueError):
        signal_information_ratio_report([1, 2], [0.1, 0.2], n_buckets=3)
```

- [ ] **Step 2: Verify RED**

Run module test and expect missing module failure.

- [ ] **Step 3: Implement module**

Validate signal/forward_return equal length. Compute signal-weighted return `signal * forward_return`, mean/std annualized IR, SNR `abs(mean(signal))/std(signal)`, stability as Pearson correlation between first and second halves when possible, and bucket top-bottom spread by sorted signal buckets.

- [ ] **Step 4: Add API tests and endpoint**

Endpoint accepts `signals`, `forward_returns`, optional int `periods_per_year`, optional int `n_buckets`.

- [ ] **Step 5: Verify GREEN**

Run selected tests.

---

### Task 11: Documentation, Review, Verification

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `docs/Roadmap.md`

- [ ] **Step 1: Update docs**

Add P289–P298 descriptions to Roadmap, README endpoint list, and CLAUDE platform module map.

- [ ] **Step 2: Run target tests**

Run:

```bash
cd backend && python3 -m pytest --no-cov \
  tests/platform/test_cross_sectional_dispersion.py \
  tests/platform/test_variance_risk_premium.py \
  tests/platform/test_pretrade_cost.py \
  tests/platform/test_ensemble_blending.py \
  tests/platform/test_option_implied_moments.py \
  tests/platform/test_correlation_regime.py \
  tests/platform/test_factor_crowding.py \
  tests/platform/test_curve_spread.py \
  tests/platform/test_turnover_attribution.py \
  tests/platform/test_signal_information_ratio.py \
  tests/platform/test_api_risk_portfolio.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Run type check**

Run:

```bash
cd backend && python3 -m basedpyright \
  app/platform/cross_sectional_dispersion.py \
  app/platform/variance_risk_premium.py \
  app/platform/pretrade_cost.py \
  app/platform/ensemble_blending.py \
  app/platform/option_implied_moments.py \
  app/platform/correlation_regime.py \
  app/platform/factor_crowding.py \
  app/platform/curve_spread.py \
  app/platform/turnover_attribution.py \
  app/platform/signal_information_ratio.py
```

Expected: `0 errors, 0 warnings, 0 notes`.

- [ ] **Step 4: Request Oracle review**

Ask review to verify no Blocking/Critical/Important issues, no live trading path coupling, and invalid inputs produce ValueError/422.

- [ ] **Step 5: Final verification**

Run `cd backend && python3 -m pytest tests/ -q` and confirm coverage remains at or above 80%.

- [ ] **Step 6: Git handoff**

Inspect `git status --short`, `git diff --stat`, and `git diff --check`. Commit/push only if the user explicitly asks for git integration.

---

## Self-Review

- Spec coverage: P289–P298 each maps to one task; docs/review/verification covered by Task 11.
- Placeholder scan: no TBD/TODO placeholders; every task has concrete files, tests, commands, and expected behavior.
- Type consistency: all modules expose `*_report(...).to_dict()` and endpoint names match the spec.
- Scope check: all work is pure Python, zero dependency, read-only API, and does not touch runner/broker/DB write paths.
