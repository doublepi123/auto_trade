# backend/app/strategies/

## Responsibility

Strategy plugins for the platform research runtime: five small modules (~430 lines total) that implement the `sdk.Strategy` Protocol (`app/platform/sdk/__init__.py`). Discovered at runtime by `StrategyRegistry.discover("app.strategies")` and driven by `PlatformRunner` in paper/backtest mode; also the package `main.py` instantiates (`registry.get("interval")`) for the optional live-tracking platform runner.

These are **research plugins, not the live trading strategy** — live range trading is `core/engine.py` + `services/trade_execution_service.py`, which never imports this package.

## Design

- One `@dataclass` strategy class per module, each exposing the six required attributes: `params` dict, `name`, `version`, `parameter_schema` (JSON-Schema-shaped `required` + `properties`), and `on_bar` / `on_quote` / `on_fill` returning `list[OrderIntent]`.
- Parameters are read defensively via `self.params.get(key, default)` (except `IntervalStrategy`, which indexes `self.params[...]` directly); prices/levels use `Decimal`, positions are read from `ctx.positions[symbol]["quantity"]`.
- Per-symbol state is kept in instance fields (e.g. `MeanReversionStrategy._closes: dict[str, deque[Decimal]]`), making instances stateful and single-run.
- `on_quote` / `on_fill` are mostly no-ops returning `[]` — decisions are bar-driven; `on_fill` is the hook for reacting to own fills.

| Module | `name` | Logic (long-only) |
|---|---|---|
| `interval_strategy.py` | `"interval"` | flat and `close <= buy_low` → BUY `quantity`; long and `close >= sell_high` → SELL all. Mirrors the live range strategy's bands for research replay. |
| `mean_reversion.py` | `"mean_reversion"` | rolling z-score of closes; enter below `entry_z`, exit at `exit_z`. |
| `momentum_breakout.py` | `"momentum_breakout"` | Donchian upper-channel breakout entry; exits via ATR trailing stop (`highest_close − atr_multiplier·ATR`). |
| `trend_following.py` | `"trend_following"` | fast/slow MA golden-cross entry (optionally gated by an ATR-volatility threshold, `atr_threshold_pct`), death-cross flat exit. |

`__init__.py` re-exports only `IntervalStrategy`.

## Flow

`PlatformRunner` feeds `BarEvent`s → strategy's `on_bar(ctx, bar)` computes against its rolling windows and `ctx.positions` → returns `list[OrderIntent]` → runner emits `OrderIntentEvent` and submits to the paper `PaperBroker` (or the injected `ExecutionClient`) → `FillEvent`s come back through `on_fill`, positions update, next bar sees the new state.

## Integration

- **Discovery:** `platform/registry.py` pkgutil-scans this package, duck-types the six attributes, and registers by `name` (duplicates raise `ValueError`). `get_default_registry()` is the only entry point used by `main.py`, `platform/api.py` (`/api/platform/backtest`, `/optimize`), and `platform/backtest_service.py`.
- **Composition:** `PlatformBacktestService.run()` does `registry.get(name)` → `strategy_cls(params)` → `EventBus` → `PlatformRunner(mode="paper")` → feed bars. `main.py` (behind `settings.platform_mode`) builds `registry.get("interval")` with the live config's `buy_low`/`sell_high`/`quantity` into a `mode="live"` runner with **no** `live_order_handler`, so it only tracks state for `/api/platform/snapshot`.
- **Direction of dependency:** this package imports only `app.platform.sdk`, `app.platform.context`, `app.platform.events` — it knows nothing about brokers, risk, DB, or the live engine, and nothing in the live order path imports it.
- Adding a plugin: drop a module here whose class has the six attributes; it is automatically discoverable via `GET /api/platform/strategies` and usable in backtests by `name`.
