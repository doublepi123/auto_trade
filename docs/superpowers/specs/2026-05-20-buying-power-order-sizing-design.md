# Buying Power Order Sizing Design

## Context

The trading runner currently sizes new entry orders from available cash:

```text
quantity = floor(available_cash * 0.98 / limit_price)
```

That underuses an intraday margin account because available cash does not represent the account's high-leverage buying power. The Longbridge Python SDK available in the backend container exposes `TradeContext.estimate_max_purchase_quantity(symbol, order_type, side, price, currency=None, order_id=None, fractional_shares=False)`, which returns `cash_max_qty` and `margin_max_qty`.

## Goal

Size new entry orders from Longbridge-estimated margin buying power while leaving close-out orders tied to actual position size.

## Design

Add a broker gateway method that estimates max margin quantity for a symbol, side, price, and currency. It will call Longbridge `estimate_max_purchase_quantity()` using limit-order parameters and return `margin_max_qty` as a `Decimal`.

Add an order-sizing helper in `TradeExecutionService` for entry orders:

```text
quantity = floor(margin_max_qty * 0.9)
```

The `0.9` multiplier is a fixed safety buffer for this first implementation. It uses margin buying power but avoids submitting orders at the full broker-estimated maximum. The value can become a strategy setting later if needed.

## Affected Flows

- `BUY`: replace cash-based sizing with margin max quantity sizing.
- `SELL_SHORT`: replace cash-based sizing with margin max quantity sizing.
- `SELL`: keep using the current long position quantity.
- `BUY_TO_COVER`: keep using the current short position quantity.

## Error Handling

If Longbridge estimation fails, the exception should propagate through the existing runner error handling path rather than falling back to cash sizing. Silent cash fallback would hide buying-power estimation failures and could place unexpectedly small orders.

If the estimated margin quantity is zero or becomes zero after the safety multiplier, the service should log the symbol, side, price, and estimate, then skip order submission.

## Tests

Add unit tests for:

- Broker gateway parses `margin_max_qty` from Longbridge estimate responses.
- Broker gateway passes symbol, limit order type, side, price, currency, and `fractional_shares=False` to Longbridge.
- `BUY` uses `floor(margin_max_qty * 0.9)` for submitted quantity.
- `SELL_SHORT` uses `floor(margin_max_qty * 0.9)` for submitted quantity.
- Zero estimated quantity skips order submission.
- Existing close-out flows continue to use actual position quantity.

## Non-Goals

- No frontend configuration in this change.
- No fractional shares.
- No change to risk limits, kill switch behavior, or close-out quantity logic.
- No trading advice or market-specific leverage assumptions beyond using the broker-provided margin estimate.
