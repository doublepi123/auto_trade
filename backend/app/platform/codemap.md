# backend/app/platform/

## Responsibility

Read-only quant research layer: 259 flat-namespace modules (~52k lines), each implementing one analytic as a pure, deterministic function over caller-supplied data, exposed as HTTP endpoints under `/api/platform/*` (and a small `/api/portfolio/*` router). Plus a self-contained event-driven plugin runtime (paper broker, backtest driver, portfolio rebalancer) that runs strategy plugins from `app.strategies` against simulated or replayed fills.

The layer is **observation and offline research only**: it never imports `TradeExecutionService`, `BrokerGateway`, or `longport`, never touches the live order path, and cannot place live orders (see "Read-only research guarantee" under Design).

## Design

### Flat namespace, one module per analytic

Every research module follows the same skeleton: `from __future__ import annotations` → `P###`-numbered docstring with citations → `__all__` → `ValueError` on bad input → dataclass report with `to_dict()`. Modules are deterministic pure functions: no I/O, no globals, no clock reads (time is passed in). Shared helpers: `_math_utils.py` (private) and `stat_utils.py` (public — hand-rolled Hyndman-Fan type-7 quantiles, incomplete-beta Student-t tails).

**Zero numpy / scipy / pandas** across the whole tree — pure stdlib (`math`, `statistics`, `decimal`).

Three-way name correspondence: `/api/platform/kelly` ↔ `platform/kelly.py` ↔ `tests/platform/test_kelly.py`.

### Module families (representative files)

- **Statistical tests (production code, not tests):** `bds_test.py` (BDS independence), `spa_test.py` (reality check / Superior Predictive Ability), `variance_ratio_test.py`, `sprt.py`, `hac_statistics.py`, `robust_statistics.py`, `multiple_testing.py`, `multivariate_normality.py`.
- **Backtest integrity:** `backtest_confidence.py`, `backtest_diagnostics.py`, `backtest_overlap.py`, `cpcv.py` (combinatorial purged CV), `overfitting.py`, `walk_forward_surface.py`, `sample_uniqueness.py`, `bootstrap_strategy_significance.py`.
- **Factor research (20+):** `factor_ic.py`, `factor_decay.py`, `factor_momentum.py`, `factor_neutralization.py`, `factor_quantiles.py`, `factor_risk.py`, `factor_tearsheet.py`, `factor_timing.py`, `factor_turnover.py`, `fama_macbeth.py`, `quadratic_factor_model.py`, `dynamic_factor_exposure.py`; plus `factor_research_service.py` (persistence, `/factors/snapshots`, `/factors/ic`).
- **Regime (14):** `regime.py`, `regime_hmm.py`, `regime_allocation.py`, `regime_attribution.py`, `regime_backtest_diagnostics.py`, `regime_cointegration.py`, `regime_factor_betas.py`, `regime_factor_returns.py`, `regime_performance.py`, `regime_switching_correlation.py`, `regime_transitions.py`, `volatility_regime.py`.
- **Portfolio construction:** `portfolio.py`, `portfolio_allocator.py`, `portfolio_api.py`, `portfolio_config.py`, `portfolio_constraints.py`, `portfolio_decomposition.py`, `portfolio_risk.py`, `portfolio_runner.py`, `portfolio_service.py`, `mean_variance.py`, `black_litterman.py`, `hrp.py` (hierarchical risk parity), `cvar_optimization.py`, `risk_budgeting.py`, `pareto_optimization.py`, `rebalancing_optimization.py`, `rebalancing_intelligence.py`, `turnover_optimization.py` / `turnover_frontier.py` / `turnover_attribution.py`.
- **Volatility:** `volatility_models.py`, `vol_targeting.py`, `vol_of_vol.py`, `vol_term_structure.py`, `vol_forecast_comparison.py`, `volatility_signature.py`, `vol_surface_arbitrage.py`, `heston.py`, `extreme_value.py`, `fat_tail.py`, `variance_break.py`, `variance_risk_premium.py`.
- **Tail risk / stress:** `tail_dependence.py`, `tail_diversification.py`, `tail_hedge_cost.py`, `copula.py` / `vine_copula.py` / `copula_stress.py`, `stress_scenarios.py` / `historical_scenarios.py` / `stress_report.py`, `reverse_stress.py`, `systemic_risk.py`, `shortfall.py`, `pain_metrics.py`.
- **Drawdown analytics:** `drawdown_analysis.py`, `drawdown_forecast.py`, `drawdown_surface.py`, `capacity_frontier.py`, `capacity_scaling.py`, `strategy_capacity.py`.
- **Execution / microstructure / TCA:** `execution_cost.py`, `execution_quality.py`, `execution_algorithms.py`, `tca.py`, `market_impact_model.py`, `microstructure.py` / `microstructure_noise.py`, `order_book.py`, `smart_order_routing.py`, `pretrade_cost.py`, `latency.py`, `fill_model.py`, `liquidity_metrics.py`, `information_trades.py`, `price_discovery.py`, `intraday_volume_profile.py` / `volume_profile.py`.
- **Signal analytics:** `signal_backtest.py`, `signal_combination.py`, `signal_decay.py`, `signal_information_ratio.py`, `signal_persistence.py`, `stat_arb_signals.py`, `triple_barrier.py` (labeling), `trade_excursion.py`, `event_study.py`, `news_impact_curve.py`, `causal_impact.py` / `causal_analysis.py`.
- **Correlation / dependence:** `correlation_network.py`, `correlation_regime.py`, `correlation_risk_premium.py`, `distance_correlation.py`, `transfer_entropy.py`, `granger_network.py`, `cointegration.py`, `pair_screening.py`, `hedge_ratio_comparison.py`, `implied_correlation.py`.
- **Time-series tooling:** `kalman_filter.py`, `bocpd.py` (Bayesian online changepoint), `change_point.py` / `cusum_detection.py`, `loess.py`, `spectral_analysis.py`, `cycle_detection.py`, `seasonality.py`, `entropy_complexity.py`, `fractional_differencing.py`, `hurst`-style `variance_ratio_test.py`, `stochastic_processes.py` / `levy_processes.py`, `montecarlo.py`.
- **Options:** `options_pricing.py`, `american_options.py`, `greeks_surface.py`, `implied_volatility.py`, `option_implied_moments.py`, `option_strategy_payoff.py`, `variance_risk_premium.py`.
- **Strategy evaluation / combination:** `strategy_quality.py`, `strategy_combinator.py`, `strategy_correlation_bootstrap.py`, `strategy_diversification.py`, `strategy_isolation.py`, `strategy_plugin_inventory.py`, `multi_strategy_risk.py`, `bandits.py`, `ensemble_blending.py`, `adaptive_sizing.py` / `sizers.py`, `tearsheet.py` / `rolling_tearsheet.py`, `returns_analysis.py`, `style_analysis.py` / `dynamic_style.py`, `ic_diagnostics.py`, `information_criteria.py`, `concept_drift.py`, `stability_analysis.py`, `sensitivity.py`, `forecast_diagnostics.py`.

### Infrastructure (~30 files)

- **`api.py`** (6835 lines, 202 routes) — the one big `APIRouter` (see Flow).
- **Plugin runtime:** `sdk/__init__.py` (the 41-line plugin contract: frozen `OrderIntent` + `@runtime_checkable Strategy` Protocol), `registry.py` (`StrategyRegistry.discover()` pkgutil-scans `app.strategies`, duck-types the 6 required attributes, `ValueError` on duplicate names; `get_default_registry()` is the only entry point), `runner.py` (`PlatformRunner`), `bus.py` (`EventBus`), `events.py` (event hierarchy), `context.py` (`StrategyContext`), `store.py` (`EventStore`, persists to `event_logs`), `replay.py` (`EventReplayer` for deterministic replay), `distributed_bus.py` (transport-neutral cross-process bridge, optional).
- **Event taxonomy (`events.py`):** base `Event` + `EventSource` enum, with nine concrete types — `QuoteEvent`, `BarEvent`, `SignalEvent`, `OrderIntentEvent` (strategy decision), `OrderEvent` (broker ack/state), `FillEvent`, `RiskEvent`, `ControlEvent`, `RegimeEvent`. Bus subscription is by type string ("fill", "bar", …), and `event_from_dict` powers store round-trips.
- **Paper trading:** `paper_broker.py` (`PaperBroker` + `PaperBrokerConfig`), `paper_order_state.py`, `simbroker.py` (`SimBroker`), `fill_model.py` (`FillModel` + `FixedSlippageModel` / `VolumeShareSlippageModel` / `FractionalCommissionModel` / `FixedPerShareCommissionModel`), `oms.py` (`OrderManagementSystem`), `cashbook.py`, `position_engine.py`, `round_trips.py` (FIFO `RoundTrip` ledger), `risk_engine.py` (`RiskEngine` → `RiskEvent`), `execution.py` (`ExecutionClient` Protocol + `LiveExecutionClient` adapter).
- **Drivers / services:** `backtest_service.py` (`PlatformBacktestService`: registry → strategy → `EventBus` → `PlatformRunner(mode="paper")` → feed bars), `backtest_run_service.py` (persists runs to `PlatformBacktestRun`), `strategy_plugin_inventory.py` (copies the driver loop so custom broker cost scenarios are not silently discarded), `portfolio_runner.py` (`PortfolioRunner` + module-level paper kill switch), `attribution_service.py`, `transaction_service.py` (`TransactionLogger` → `transactions`), `optimizer_service.py`.
- **Feeds / plumbing:** `indicators.py`, `bar_builder.py` (P282 tick→bar), `data_catalog.py` (`resample_bars`), `scheduler.py`, `session_filter.py` (`MarketSessionFilter`), `universe.py` (`StaticUniverse`, `TopNByVolumeUniverse`), `warmup.py`.

### `api.py` route pattern (202 endpoints: 191 POST + 11 GET)

Every endpoint follows the same five steps:

1. `@router.post("/x", dependencies=[Depends(require_api_key())])`
2. **sync** handler taking `payload: dict[str, Any]` — no Pydantic request model
3. **lazy import of the pure module inside the function body** (most of the ~210 imports are function-local, keeping startup cheap)
4. hand-written validation → `raise HTTPException(422, ...)`; module call wrapped in `except ValueError as exc: raise HTTPException(422, str(exc))`
5. return a plain dict (or `report.to_dict()`)

Reference implementation: `/kelly` (line ~1319). Shared payload parsers: `_to_returns`, `_to_equity`, `_finite_number`, `_numeric_series`. Docstrings carry a `P###` batch number; `422` means missing/invalid input.

The 11 GET routes are the stateful minority: `/strategies`, `/snapshot` (404 when `platform_runner` is not on app state), `/backtest/runs` (+ `/compare`, `/{run_id}`), `/events`, `/bars`, `/transactions`, `/factors/snapshots`, `/factors/ic`, `/tca`.

`portfolio_api.py` (119 lines, 6 routes: GET/PUT `/config…`, GET `/attribution`, GET/POST `/kill-switch…`) is the one structural exception: its own `APIRouter` with **router-level** `require_api_key` dependency, DB access via `get_db`, audit writes, and the portfolio-level kill switch (`/api/portfolio/kill-switch` — paper trading only). Mounted at `/api/portfolio` by `main.py`.

### Route inventory by prefix family (202 total)

| Prefix group | Routes | Representative |
|---|---|---|
| Factor research | 14 | `/factor-ic`, `/factor-decay`, `/factor-tearsheet`, `/factors/ic` |
| Regime | 11 | `/regime-hmm`, `/regime-attribution`, `/regime-transitions` |
| Execution / microstructure / TCA | 11 | `/tca`, `/execution-cost`, `/market-impact`, `/pretrade-cost`, `/smart-order-routing` |
| Backtest runs & integrity | 8 | `/backtest`, `/backtest/runs/*`, `/backtest-confidence`, `/cpcv`, `/bootstrap-significance` |
| Volatility | 9 | `/volatility`, `/vol-targeting`, `/vol-of-vol`, `/heston`, `/vol-term-structure` |
| Stress / tail / EVT | 5+ | `/historical-stress`, `/reverse-stress`, `/evt`, `/shortfall`, `/systemic-risk` |
| Strategy evaluation | 4 | `/strategy-quality`, `/strategy-capacity`, `/strategy-correlation-bootstrap`, `/strategies` |
| Signal analytics | 4 | `/signal-backtest`, `/signal-combination`, `/signal-information-ratio` |
| Drawdown | 2 | `/drawdown-forecast`, `/drawdown-surface` |
| Turnover / rebalancing | 4 | `/turnover-frontier`, `/rebalancing-optimization` |
| Copulas / dependence | 3+ | `/copula`, `/vine-copula`, `/tail-dependence` |
| Everything else | ~120 | singles: `/kelly`, `/montecarlo`, `/kalman-filter`, `/tearsheet`, `/bds-test`, `/spa-test`, `/variance-ratio-test`, `/snapshot`, … |

### `PlatformRunner`

`runner.py` (201 lines) builds a uniform event-driven loop around one strategy:

- `mode="backtest" | "paper"` → constructs a `PaperBroker` (361 lines: LIMIT matching against bar high/low, partial fills via `partial_fill_probability` fraction-of-remaining, slippage and commission through `FillModel`) and subscribes to `fill` events; bars/quotes are fed in externally via `runner.on_bar(bar)` / `runner.on_quote(quote)`.
- `mode="live"` → executes only when a `live_order_handler` is **explicitly injected** (wrapped in a `LiveExecutionClient` adapter so `_execute_intent` keeps one code path); `main.py` builds the live runner *without* one, so it merely tracks state for `/api/platform/snapshot`. During warmup, `_execute_intent` is a no-op.
- Intents flow: strategy returns `list[OrderIntent]` → `OrderIntentEvent` emitted on the bus (and appended to `EventStore`) → broker `submit(intent)` returns an `OrderEvent` → `FillEvent` → runner updates `_positions`, `RiskEngine.on_fill` emits `RiskEvent`s.
- Also injectable: `clock` (determinism — no wall-clock reads), `risk_engine`, `indicators` (`IndicatorService`, SMA/EMA-style `compute(bars)`), `universe`, `scheduler`, `session_filter` with `allowed_sessions` (`rth`/`pre`/`post`).

### Read-only research guarantee

- Nothing in this tree imports `TradeExecutionService`, `BrokerGateway`, or `longport` (the only mention is a docstring in `runner.py`); grep-enforced convention.
- No `live_order_handler` is ever assigned from inside this layer — `main.py` deliberately constructs the live-mode runner without one.
- Every mutation-shaped capability is paper-only: the portfolio kill switch arms/disarms `PortfolioRunner.rebalance()`, which submits to the paper `PlatformRunner`, not to a broker.

### Conventions & guardrails

- Module docstrings cite the source literature and carry a `P###` batch number; the same number appears in the endpoint docstring and the test file.
- Never add Pydantic request models or async handlers to `api.py` — the dict-payload + sync + 422 pattern is uniform across all 202 endpoints, and consistency is the point at this scale.
- Never ship an endpoint without both a 200-shape test and a 422-rejection test.
- `VolumeShareSlippageModel` must not be used in plugin cost scenarios: research datasets carry no volume, so it silently reports zero slippage.
- `PlatformBacktestService` hardcodes the default `PaperBroker` and silently discards custom cost scenarios — `strategy_plugin_inventory.py` exists because it copies the driver loop instead.

## Flow

**Request/response analytics:** client POSTs a JSON dict of series/parameters → `api.py` validates and parses (shared `_to_*` helpers) → function-local import of the pure module → computation → dataclass `to_dict()` → plain JSON back. No DB writes for the 191 POST analytics; persistence exists only for run artifacts (`PlatformBacktestRun`), factor snapshots/IC, events, bars, transactions, TCA.

**Backtest flow (`backtest_service.py`, 112 lines):** `get_default_registry()` → `registry.get(name)` → `strategy_cls(params)` → `EventBus` + `_BacktestCollector` (subscribes `fill`, prices each bar, snapshots NAV) → `PlatformRunner(mode="paper")` → caller-supplied bars are validated per symbol, converted to `BarEvent`s, and fed one by one → `PaperBroker` matches LIMIT orders against bar high/low with `FillModel` slippage/commission → `PerformanceAnalytics().analyze(equity, fills)` → result dict (equity curve, fills, final positions, stats, analytics); `backtest_run_service.py` persists it to `PlatformBacktestRun` rows for `/backtest/runs` comparison. `strategy_plugin_inventory.py` repeats this driver loop with configurable cost models because the shared service hardcodes the default `PaperBroker`.

**Portfolio flow:** `portfolio_api.py` reads/writes `PortfolioConfig` → `PortfolioRunner.rebalance()` computes target weights via `PortfolioAllocator` (mean-variance/HRP/CVaR family) → emits `OrderIntent`s into the paper runner; kill switch short-circuits to `[]`.

**Live-tracking flow:** `main.py` lifespan (when `platform_mode` is set) builds `strategy_cls = registry.get("interval")` with the live strategy config's `buy_low`/`sell_high`/`quantity`, wraps it in `PlatformRunner(mode="live")` with **no** `live_order_handler`, and stores it on `app.state.platform_runner`; `/api/platform/snapshot` reads `_runner_snapshot(runner)` (404 when absent).

## Integration

- **Mounted by `main.py`:** `app.include_router(platform_router, prefix="/api/platform")` and `app.include_router(portfolio_router, prefix="/api/portfolio")`; optional `PlatformRunner` startup behind `settings.platform_mode`; frontend analytics pages (~50 routes) consume these endpoints read-only.
- **Upward reuse is minimal and deliberate:** `platform → api(3) core(1) domain(1)` — it reuses `app.api.auth` (`require_api_key`), `app.api.deps` (`get_db`, audit logger), `app.core.audit`, and app models/DB. It does **not** import services.
- **Downward contract:** `app.strategies` packages are discovered via `registry.discover("app.strategies")`; strategies implement the `sdk.Strategy` Protocol (see `sdk/codemap.md` and `strategies/codemap.md`).
- **Layer boundary:** `services → platform(2)` exists (platform backtest/inventory used as research tools), but platform never calls services — the research layer stays a leaf that could be excised without touching live trading.
- **Events / persistence:** `EventBus` is a type-string pub/sub (`subscribe("fill", handler)` / `publish(event)`); `EventStore` appends serialized events to the `event_logs` table via `SessionLocal`; `EventReplayer` replays persisted events into a fresh bus for deterministic re-simulation; `TransactionService.TransactionLogger` records fills as `transactions` rows (the same `transactions` table the live ledger reconciles against, but written only from platform fills).
- **Frontend:** ~50 single-purpose read-only analytics views (hash routes) each call one `/api/platform/*` endpoint via `src/api/` clients; no platform endpoint mutates trading state.
- **Tests:** `backend/tests/platform/` holds one `test_<module>.py` per module (264 files); endpoint tests override `app.dependency_overrides[require_api_key]` and must assert both a 200 shape and a 422 rejection.
- **Catalog:** `app/api/platform_catalog.py` mounts the platform research catalog for the frontend's analytics route discovery.
