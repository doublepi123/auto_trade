# `backend/app/platform/` — Research / Plugin Layer

## OVERVIEW
259 files, ~52k lines, flat namespace: one module per analytic, exposed under `/api/platform/*`. Pure computation plus a paper-trading runtime. Never touches the live order path.

## THREE-WAY NAME CORRESPONDENCE
`/api/platform/kelly` ↔ `platform/kelly.py` ↔ `tests/platform/test_kelly.py`. Keep all three in sync when adding an analytic.

Prefix families: `backtest_*` `factor_*` (20+) `regime_*` (14) `portfolio_*` `vol_*` `signal_*` `tail_*` `correlation_*`. Infra (~25): `api.py`, `runner.py`, `paper_broker.py`, `registry.py`, `bus.py`, `events.py`, `context.py`, `store.py`, `replay.py`, `execution.py`, `simbroker.py`, `fill_model.py`, `risk_engine.py`, `indicators.py`, `scheduler.py`, `session_filter.py`, `universe.py`, `round_trips.py`, `strategy_plugin_inventory.py`, `portfolio_api.py`, `portfolio_runner.py`, plus 8 `*_service.py`. Shared helpers: `_math_utils.py` (private), `stat_utils.py` (public).

## ADDING AN ENDPOINT (the 5-step pattern, 202 endpoints deep)
`api.py` is one 6835-line `APIRouter` mounted at `/api/platform`. Every endpoint:
1. `@router.post("/x", dependencies=[Depends(require_api_key())])`
2. **sync** handler taking `payload: dict[str, Any]` — no Pydantic request model
3. **lazy import of the pure module inside the function body** (180 of 210 imports are function-local)
4. hand-written validation → `raise HTTPException(422, ...)`; wrap the module call in `except ValueError as exc: raise HTTPException(422, str(exc))`
5. return a plain dict (or `report.to_dict()`)

Reference implementation: the `/kelly` handler. Shared payload parsers: `_to_returns`, `_to_equity`, `_finite_number`, `_numeric_series`. Docstrings carry a `P###` batch number. `422` means missing/invalid input — 237 sites agree.

`portfolio_api.py` is the one exception: separate router, router-level dependencies, DB access, audit writes, portfolio-level kill switch (paper only).

## PLUGIN RUNTIME
- `sdk/__init__.py` (41 lines) is the whole plugin contract: frozen `OrderIntent` + `@runtime_checkable Strategy` Protocol (`params`, `name`, `version`, `parameter_schema`, `on_bar`, `on_quote`, `on_fill` → `list[OrderIntent]`).
- `registry.py`: `discover()` pkgutil-scans `app.strategies`, duck-types the 6 required attributes, raises `ValueError` on duplicate names. `get_default_registry()` is the only entry point.
- `PlatformRunner`: `mode="backtest"|"paper"` builds a `PaperBroker`; `mode="live"` only executes when a `live_order_handler` is **explicitly injected**. `main.py` builds the live runner *without* one, so it merely tracks state for `/api/platform/snapshot`.
- Composition pattern (`backtest_service.py`): `registry.get(name)` → `strategy_cls(params)` → `EventBus` → `PlatformRunner(mode="paper")` → feed bars.

## CONVENTIONS
- **Zero numpy / scipy / pandas** — grep confirms 0 importing files. Pure stdlib (`math`, `statistics`, `decimal`); `stat_utils.py` hand-rolls Hyndman-Fan type-7 quantiles.
- Module skeleton: `from __future__ import annotations` → `P###` docstring with citations → `__all__` → `ValueError` on bad input → dataclass report with `to_dict()`.
- Deterministic pure functions: no I/O, no globals, no clock reads (pass time in).
- Tests: one `test_<module>.py` per module (264 files). Pure-function tests assert known analytic solutions and `pytest.raises(ValueError)`. Endpoint tests override auth via `app.dependency_overrides[require_api_key] = lambda: None` and assert **both** a 200 shape and a 422 rejection.

## ANTI-PATTERNS (THIS DIR)
- Importing `TradeExecutionService`, `BrokerGateway`, or `longport` — today the only match in this tree is a docstring in `runner.py`. Keep it that way.
- Assigning a real `live_order_handler` from inside this layer.
- Adding numpy/scipy/pandas for convenience.
- Pydantic request models or async handlers in `api.py` — the dict payload + sync + 422 pattern is uniform, and consistency is the point in a 202-endpoint file.
- Shipping an endpoint without both the 200 and 422 test.
- `VolumeShareSlippageModel` in cost scenarios: research datasets carry no volume, so it silently reports zero slippage.
- Reusing `PlatformBacktestService` when you need custom broker costs — it hardcodes the default `PaperBroker` and silently discards cost scenarios (`strategy_plugin_inventory.py` copies the driver loop for exactly this reason).
