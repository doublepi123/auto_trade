# Account Balance & Positions Display

## Goal

Display the current Longbridge account's cash balances and stock positions on the Dashboard page, enabling users to monitor their account status alongside trading engine state.

## Approach

**Method A: Dashboard inline cards** — Add account info cards directly into the existing Dashboard page.

## Backend Design

### New `BrokerGateway.get_account()` method

Located in `backend/app/core/broker.py`. Calls `TradeContext.account_balance()` and returns structured account data alongside positions.

```python
@dataclass
class CashBalance:
    currency: str
    available_cash: Decimal
    frozen_cash: Decimal

@dataclass
class NetAsset:
    currency: str
    amount: Decimal

@dataclass
class AccountInfo:
    total_assets: Decimal
    cash_balances: list[CashBalance]
    net_assets: list[NetAsset]
```

- `get_account()` → calls `self._trade_ctx.account_balance()`, parses response into `AccountInfo`
- Reuse existing `get_positions()` for position data
- For each position, call `get_quote()` to compute `market_value = quantity * last_price`
- Handle the case where `_trade_ctx` is not initialized (runner not started)

### New API endpoint: `GET /api/account`

Located in `backend/app/api/trade.py`, protected by API key auth.

```python
@router.get("/account", response_model=AccountResponse, dependencies=[Depends(require_api_key())])
def get_account() -> AccountResponse:
    ...
```

### New response schemas in `schemas.py`

```python
class CashBalanceSchema(BaseModel):
    currency: str
    available_cash: float
    frozen_cash: float

class PositionSchema(BaseModel):
    symbol: str
    side: str          # LONG / SHORT
    quantity: float
    avg_price: float
    market_value: float

class AccountResponse(BaseModel):
    total_assets: float
    cash_balances: list[CashBalanceSchema]
    positions: list[PositionSchema]
```

- Return `total_assets` as a single number (primary currency, e.g. USD)
- `cash_balances` lists all currency balances returned by Longbridge
- `positions` includes market value calculated from current quotes
- If broker is unavailable, return empty defaults with `total_assets=0`

## Frontend Design

### New TypeScript types in `types/index.ts`

```typescript
export interface CashBalance {
  currency: string
  available_cash: number
  frozen_cash: number
}

export interface Position {
  symbol: string
  side: string
  quantity: number
  avg_price: number
  market_value: number
}

export interface AccountInfo {
  total_assets: number
  cash_balances: CashBalance[]
  positions: Position[]
}
```

### New API function in `api/index.ts`

```typescript
export async function getAccount(): Promise<AccountInfo> {
  const resp = await api.get('/api/account')
  return resp.data
}
```

### Dashboard.vue changes

Add below the existing status cards row:

1. **Account row** (2 cards side by side):
   - **Total Assets** card: displays `total_assets` formatted with currency, colored green/red based on positive/negative
   - **Cash Balances** card: displays each currency with available/frozen amounts

2. **Positions card** (full width):
   - `el-table` with columns: Symbol, Side, Quantity, Avg Price, Market Value
   - Empty state: "No positions" text when positions list is empty

### Data refresh strategy

- `onMounted`: add `getAccount()` to `Promise.all` alongside `getStrategy()` and `getStatus()`
- `refresh()`: add `getAccount()` call
- Separate `setInterval` at 10-second interval for account data refresh (positions don't need sub-second updates)
- No WebSocket push for account data (avoid broker rate limits)

## Error Handling

- If `get_account()` fails (broker not connected, API error), return response with `total_assets: 0`, empty `cash_balances`, empty `positions`
- Frontend shows a warning alert when account data is unavailable
- Broker calls wrapped in try/except to never crash the endpoint

## Testing

- Backend unit test: mock `BrokerGateway.get_account()` and `get_positions()`, verify `GET /api/account` response schema
- Backend unit test: verify graceful fallback when broker is not available
- Frontend: verify new types, API call, and rendering with mock data