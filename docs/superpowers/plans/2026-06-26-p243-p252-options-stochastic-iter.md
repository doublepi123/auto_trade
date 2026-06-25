# P243–P252 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship 10 new `app/platform/` modules + 10 new `/api/platform/*` endpoints (options pricing, implied vol/SVI, Kalman, stochastic processes, stat-arb signals, robust statistics, bandits, LOESS, smart order routing, vine copula), pure Python, zero new deps, deterministic, each with unit + API tests.

**Architecture:** Each round = one module file (`app/platform/<m>.py` with free functions + a frozen dataclass `Result` + `to_dict()`) + one `POST /api/platform/<e>` endpoint in `app/platform/api.py` (require_api_key + 422 validation) + unit tests in `tests/platform/test_<m>.py` + API tests appended to `tests/platform/test_api_risk_portfolio.py`. No new deps, no numpy/scipy. Reuse `risk_metrics` Acklam inverse-normal CDF and existing erf rational approximations.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy, pytest, basedpyright.

---

## Conventions (apply to every round)

- Module header docstring cites the open-source reference and "Pure Python, no scipy/numpy".
- `__all__` exported names.
- Free functions + one frozen `@dataclass` result with `to_dict()`.
- `ValueError` for invalid inputs (length mismatch, empty, non-positive where required).
- Endpoint: `@router.post("/<e>", dependencies=[Depends(require_api_key())])`, lazy `from app.platform.<m> import ...` inside handler, 422 on missing/empty/mismatched via `HTTPException`.
- Tests: numerical correctness vs known closed-form, edge cases, 422 paths.
- After each round: `pytest tests/platform/test_<m>.py tests/platform/test_api_risk_portfolio.py -v` and `basedpyright app/platform/<m>.py app/platform/api.py`.

## Shared helpers (added in P243, reused by P244/P246/P250)

P243 will add `app/platform/_math_utils.py` with `_norm_cdf(x)` (Abramowitz-Stegun 26.2.17), `_norm_inv(p)` (Acklam), `_erf(x)` (A&S 7.1.26 rational). P244/P246/P250 import these. (Verify `risk_metrics` doesn't already export a reusable inverse-normal; if it does, reuse instead of duplicating.)

---

### Task 1: P243 — Options Pricing + Greeks

**Files:**
- Create: `backend/app/platform/_math_utils.py`
- Create: `backend/app/platform/options_pricing.py`
- Modify: `backend/app/platform/api.py` (append endpoint)
- Test: `backend/tests/platform/test_options_pricing.py`
- Modify: `backend/tests/platform/test_api_risk_portfolio.py` (append 200/422 tests)

- [ ] Step 1: Write failing tests for BSM call/put price, put-call parity, Greeks (Δ, Γ, vega, Θ, ρ), and 422 endpoint.
- [ ] Step 2: Run tests — expect fail (module missing).
- [ ] Step 3: Implement `_math_utils.py` (`_norm_cdf`, `_norm_inv`, `_erf`).
- [ ] Step 4: Implement `options_pricing.py` (`black_scholes`, `greeks`, `OptionsResult`).
- [ ] Step 5: Add `/options-pricing` endpoint.
- [ ] Step 6: Append API tests; run all — expect pass.
- [ ] Step 7: `basedpyright app/platform/options_pricing.py app/platform/_math_utils.py app/platform/api.py`.
- [ ] Step 8: Commit.

### Task 2: P244 — Implied Volatility + SVI

**Files:** Create `implied_volatility.py`; modify `api.py`; test `test_implied_volatility.py`; API tests.

- [ ] Steps: tests → implement `implied_volatility` (Brenner-Subrahmanyam init + Newton-Raphson) + `svi_fit` (Gauss-Newton with LM damping, bounds) + `SviFit` dataclass → endpoint `/implied-volatility` → tests pass → basedpyright → commit.

### Task 3: P245 — Kalman Filter + RTS Smoother

**Files:** Create `kalman_filter.py`; modify `api.py`; test `test_kalman_filter.py`; API tests.

- [ ] Steps: tests (constant signal recovery, RTS smoother reduces variance) → implement `KalmanResult`, `kalman_filter`, `rts_smoother` → endpoint `/kalman-filter` → tests → basedpyright → commit.

### Task 4: P246 — Stochastic Processes / SDE

**Files:** Create `stochastic_processes.py`; modify `api.py`; test `test_stochastic_processes.py`; API tests.

- [ ] Steps: tests (GBM mean/var, OU stationary var, CIR positivity, Merton jump count mean) → implement `gbm_simulate`, `ou_simulate`, `cir_simulate`, `merton_jd_simulate` (all `random.Random(seed)`) + moments → endpoint `/stochastic-processes` → tests → basedpyright → commit.

### Task 5: P247 — Stat-Arb Signals

**Files:** Create `stat_arb_signals.py`; modify `api.py`; test `test_stat_arb_signals.py`; API tests.

- [ ] Steps: tests (distance-method spread, z-score thresholds, half-life reuse) → implement `distance_method_spread`, `zscore_signals`, `StatArbResult` (reuse `cointegration.ou_half_life`) → endpoint `/stat-arb-signals` → tests → basedpyright → commit.

### Task 6: P248 — Robust Statistics

**Files:** Create `robust_statistics.py`; modify `api.py`; test `test_robust_statistics.py`; API tests.

- [ ] Steps: tests (MAD vs known, winsorize clip, Theil-Sen slope vs OLS on clean+outlier, Huber convergence) → implement `mad`, `winsorize`, `trimmed_mean`, `theil_sen`, `huber` (IRLS) → endpoint `/robust-statistics` → tests → basedpyright → commit.

### Task 7: P249 — Bandits

**Files:** Create `bandits.py`; modify `api.py`; test `test_bandits.py`; API tests.

- [ ] Steps: tests (ε-greedy picks best arm, UCB1 explores, Thompson Beta posterior mean → true rate, regret monotonic) → implement `EpsilonGreedy`, `UCB1`, `ThompsonSamplingBeta`, `ThompsonSamplingGaussian`, `regret` + `BanditResult` → endpoint `/bandits` → tests → basedpyright → commit.

### Task 8: P250 — LOESS/LOWESS

**Files:** Create `loess.py`; modify `api.py`; test `test_loess.py`; API tests.

- [ ] Steps: tests (linear data recovered, robust down-weights outlier) → implement `lowess` (tricube kernel, local linear, robust reweighting iterations) → endpoint `/loess` → tests → basedpyright → commit.

### Task 9: P251 — Smart Order Routing

**Files:** Create `smart_order_routing.py`; modify `api.py`; test `test_smart_order_routing.py`; API tests.

- [ ] Steps: tests (buy picks lowest ask, sell picks highest bid, split across venues with cost, tick-rule quantization) → implement `route_order` (aggregate L1 across venues, best-price greedy with venue cost, tick quantize) + `SorResult` → endpoint `/smart-order-routing` → tests → basedpyright → commit.

### Task 10: P252 — Vine Copula

**Files:** Create `vine_copula.py`; modify `api.py`; test `test_vine_copula.py`; API tests.

- [ ] Steps: tests (C-vine/D-vine log-lik on 3-asset Gaussian ranks, AIC/BIC ordering) → implement `vine_copula` (reuse `copula` Kendall τ + Gumbel/Clayton/Gaussian pair fits, tree assembly, log-lik) + `VineCopulaResult` → endpoint `/vine-copula` → tests → basedpyright → commit.

### Task 11: Final — docs sync + full regression + merge

- [ ] Update CLAUDE.md (platform tree, API table, P243-P252 bullets).
- [ ] Update README.md API table.
- [ ] Update docs/Roadmap.md.
- [ ] `pytest tests/ -q` ≥ 2114 passed.
- [ ] `basedpyright` 0/0/0.
- [ ] Single commit `P243-P252: 10-round options/stochastic/robust/routing/vine iteration`.