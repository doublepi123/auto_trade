# backend/app/platform/sdk/

## Responsibility

The entire plugin contract for the platform research runtime, in one 41-line `__init__.py`: the frozen `OrderIntent` order-request dataclass and the `@runtime_checkable Strategy` Protocol that every strategy plugin implements. This is the narrow waist between `app.strategies` (implementations) and `app.platform` (runtime: runner, brokers, risk engine).

## Design

- **`OrderIntent`** — `@dataclass(frozen=True)` carrying `symbol`, `side` (`"BUY" | "SELL"`), `quantity`, `order_type` (`"MARKET" | "LIMIT" | "STOP" | "TRAILING"`), optional `limit_price` / `stop_price` / `trailing_offset` (all `Decimal`), free-text `reason`, and `linked_order_id`. Frozen because intents are immutable requests, not order state — the broker owns state transitions.
- **`Strategy`** — `@runtime_checkable Protocol` with:
  - `params: dict[str, Any]` (instance attribute)
  - properties `name -> str`, `version -> str`, `parameter_schema -> dict[str, Any]` (JSON-Schema-shaped)
  - `on_bar(ctx: StrategyContext, bar: BarEvent) -> list[OrderIntent]`
  - `on_quote(ctx: StrategyContext, quote: QuoteEvent) -> list[OrderIntent]`
  - `on_fill(ctx: StrategyContext, fill: FillEvent) -> list[OrderIntent]`
- Structural typing only: no base class to inherit, no registration decorator. `StrategyRegistry.discover()` in `platform/registry.py` duck-types classes by the six required attributes (`name`, `version`, `parameter_schema`, `on_bar`, `on_quote`, `on_fill`) and verifies `isinstance(instance, Strategy)` via the runtime-checkable protocol, so any class with the right shape is a plugin.
- Types it borrows from the platform core: `StrategyContext` (`platform/context.py`), `BarEvent` / `QuoteEvent` / `FillEvent` (`platform/events.py`). `Decimal` everywhere for money — no floats on the order path.
- Design motivations: the protocol is the *whole* public surface (one import: `from app.platform.sdk import OrderIntent, Strategy`); strategies stay importable without any broker/DB dependency; and the frozen dataclass keeps intent mutation impossible after the strategy returns it.

## Flow

A strategy is a passive responder: the runtime (`PlatformRunner`) feeds it an event plus a `StrategyContext` (symbol, positions dict, params, clock, indicators); the strategy returns `list[OrderIntent]` (possibly empty). The runner — never the strategy — emits `OrderIntentEvent` on the `EventBus`, submits to the `ExecutionClient` (`PaperBroker` in backtest/paper mode; `LiveExecutionClient(handler)` only when a live handler is explicitly injected), and routes resulting `FillEvent`s back through `on_fill` so the strategy can react to its own fills.

## Integration

- `app.strategies.*` implements this Protocol (see `strategies/codemap.md`).
- `platform/registry.py` discovers implementations and exposes them through `get_default_registry()`; `main.py` and `backtest_service.py` instantiate via `registry.get(name)(params=...)` (the registry tolerates both `cls()` and `cls(params={})` constructors).
- `platform/runner.py`, `paper_broker.py`, `execution.py`, `portfolio_runner.py`, and `risk_engine.py` all consume `OrderIntent` as their common currency — every path from "strategy decided" to "fill recorded" passes through this dataclass.
- Adding a strategy requires no sdk change: drop a module in `app.strategies` with the six attributes and it is discoverable; duplicate `name` values raise `ValueError` at registration.
- Contract stability: changing `OrderIntent`'s fields or the Protocol's method signatures breaks every strategy and every broker at once — treat this file as frozen surface and extend via optional fields (as `linked_order_id` did), not by editing existing ones.
