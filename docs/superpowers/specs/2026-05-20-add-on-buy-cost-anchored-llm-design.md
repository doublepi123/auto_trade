# Add-On Buy And Cost-Anchored LLM Design

## Context

The strategy engine currently behaves like a single-position state machine. It buys only from `flat`, sells from `long`, shorts from `flat` when short selling is enabled, and covers from `short`. While already `long`, a price drop below `buy_low` does not trigger another `BUY`, so the system cannot average down or add to a position.

The LLM interval prompt currently asks for `buy_low` and `sell_high` around the current price, with guidance like `buy_low ≈ current_price * 0.99` and `sell_high ≈ current_price * 1.01`. With a 1-minute refresh interval, this can make the strategy chase the market: thresholds keep moving with price, so trades may never trigger.

Current prompt context only includes the coarse engine state (`flat`, `long`, `short`), not the actual position quantity, average cost, or unrealized P/L. For a long position such as `18 NVDA.US @ 255.942`, the LLM cannot reason about adding below cost or selling relative to cost basis.

## Goals

1. Allow additional `BUY` orders while already `long` when price reaches `buy_low`.
2. Keep the existing 60-second cooldown so repeated quote ticks do not spam add-on orders.
3. Use existing buying-power order sizing for add-on buys.
4. Give the LLM concrete position context: side, quantity, average cost, current price, and unrealized P/L percentage.
5. Prevent LLM interval updates from chasing current price in a way that keeps moving triggers away from the market.

## Design

### Add-On Buy State Rule

In `StrategyEngine._update_price_locked()`, the `LONG` state should support two triggers:

- If `price <= buy_low`, trigger `BUY` and remain `LONG`.
- If `price >= sell_high`, trigger `SELL` and transition to `FLAT`.

Sell should be evaluated before add-on buy only if both somehow become true, but valid config already enforces `buy_low < sell_high`, so both conditions cannot be true for one price.

### Position Context For LLM

Add a small position-context structure near the LLM analysis path. It should be derived from `get_runner().broker.get_positions()` for the configured symbol:

- `side`: `LONG`, `SHORT`, or `FLAT`
- `quantity`: absolute position quantity
- `avg_price`: average cost price
- `unrealized_pnl_pct`: `(current_price - avg_price) / avg_price * 100` for long, `(avg_price - current_price) / avg_price * 100` for short

If position lookup fails, analysis should continue with a flat/unknown context and log the failure. This is advisory context only; it should not block trading.

### LLM Prompt Rules

Update `DataAggregator.build_prompt()` so it includes:

- Current position side
- Quantity
- Average cost
- Unrealized P/L percentage

Prompt guidance should change from pure current-price anchoring to position-aware anchoring:

- When `LONG`, `buy_low` is the add-on trigger. It should consider average cost and current drawdown, and must not be raised every refresh just because current price rises.
- When `LONG`, `sell_high` should consider average cost. Avoid recommending sell targets below cost unless explicitly describing a stop-loss, which this system does not currently model.
- When `FLAT`, current-price ± ATR/current volatility guidance is still acceptable.

### Stable LLM Application

For automatic application while `LONG`, avoid replacing `buy_low` with a higher value merely because the latest prompt followed current price. A conservative rule is:

- In `LONG`, apply `sell_high` using existing logic.
- In `LONG`, apply `buy_low` only when the suggestion is at or below the current `buy_low`.

This lets the LLM lower an add-on threshold but prevents it from continuously ratcheting the add-on trigger upward. A future version can make this configurable, but this change should stay backend-only.

## Tests

Add tests for:

- `LONG` + `price <= buy_low` triggers `BUY` and remains `LONG`.
- Cooldown prevents immediate repeated add-on buys.
- Runner can submit a second `BUY` while already long when price crosses `buy_low`.
- Prompt includes position quantity, average price, and unrealized P/L.
- `LONG` interval application does not raise `buy_low`, but does allow lowering it.

## Non-Goals

- No frontend controls for add-on buying.
- No new persisted strategy fields.
- No stop-loss behavior.
- No fractional shares.
- No change to short add-on behavior; `SHORT` still only covers at `buy_low`.
- No change to buying-power sizing percentage.
