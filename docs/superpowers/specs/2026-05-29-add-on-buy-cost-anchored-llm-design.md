# Add-On Buy And Cost-Anchored LLM Design

> **Status:** Ready for implementation | **Source:** Brainstorming session 2026-05-29
> **Baseline:** P9 (LLM Prompt Engineering Optimization) delivered. `pytest 549 passed`, `basedpyright` 0 errors.

## Context

The strategy engine currently behaves like a single-position state machine. It buys only from `flat`, sells from `long`, shorts from `flat` when short selling is enabled, and covers from `short`. While already `long`, a price drop below `buy_low` does not trigger another `BUY`, so the system cannot average down or add to a position.

The LLM interval prompt (post-P9 modular architecture) includes position context fields (`position_quantity`, `position_avg_price`, `unrealized_pnl_pct`) in `build_prompt()` signatures, but they are **not yet populated from actual tracked entries** — they default to zero. The `ContextModule` renders them into the prompt, yet the `LLMAdvisorService.analyze()` path does not extract real position data before calling `build_prompt()`.

P9 also introduced `SentimentAnalyzer`, multi-timeframe analysis, and `PerformanceTracker`. The prompt is richer than ever, but the **trading engine still treats every position as a single entry** — preventing the LLM from leveraging its full reasoning power about cost basis, drawdown, and add-on timing.

## Goals

1. Allow additional `BUY` orders while already `long` when price reaches `buy_low`.
2. Keep the existing 60-second cooldown so repeated quote ticks do not spam add-on orders.
3. Use existing margin-based order sizing (`_entry_quantity_from_margin_power`) for add-on buys.
4. Populate LLM prompt with **real** position context from `tracked_entries`: side, quantity, average cost, current price, and unrealized P/L percentage.
5. Prevent LLM interval updates from chasing current price in a way that keeps moving triggers away from the market.
6. Ensure add-on buys integrate cleanly with existing risk controls (daily loss, consecutive losses, kill switch, fee guard).

## Design

### Section 1: Engine State Machine — Add-On Buy Rule

**File:** `backend/app/core/engine.py`

In `StrategyEngine._update_price_locked()`, the `LONG` state should support two triggers:

- If `price >= sell_high`, trigger `SELL` and transition to `FLAT`. (Existing logic, unchanged.)
- If `price <= buy_low`, trigger `BUY` and **remain `LONG`**. (New logic.)

Sell is evaluated before add-on buy. Valid config already enforces `buy_low < sell_high`, so both conditions cannot be true for one price.

**Key consideration:** The cooldown `_cooldown_seconds` (60s) applies globally per engine instance. After an add-on buy, the next add-on or sell must wait 60s. This prevents quote-jitter spam and gives the broker time to fill and report the new position, which will update `tracked_entries`.

**State implications:**
- Engine state remains `LONG` after add-on buy.
- `sync_state()` on runner startup / position reconciliation must correctly recognize a long position regardless of how many add-on buys occurred.
- No new engine states are introduced; `LONG` simply becomes "long with possible multiple entry lots" instead of "long from a single entry".

### Section 2: Position Context for LLM — Real Data Injection

**Files:** `backend/app/services/llm_advisor_service.py`, `backend/app/services/data_aggregator.py`, `backend/app/domain/prompt/context_module.py`

Current state (post-P9):
- `LLMAdvisorService.analyze()` accepts `position_quantity`, `position_avg_price`, `unrealized_pnl_pct` as parameters.
- These are passed to `DataAggregator.build_prompt()` which puts them into the `context` dict.
- `ContextModule.render()` reads them from the dict but currently always renders zeros because upstream never fetches real data.

**Change:** `LLMAdvisorService.analyze()` itself should fetch position data internally before calling `build_prompt()`, so all callers (runner cron, API endpoint) automatically receive real context without duplicating the lookup logic.

```python
# Pseudocode inside LLMAdvisorService.analyze()
position = self._trade_svc.get_tracked_entry(symbol)
if position and position.quantity > 0:
    position_quantity = float(position.quantity)
    position_avg_price = float(position.cost)
    unrealized_pnl_pct = (current_price - position_avg_price) / position_avg_price * 100
else:
    position_quantity = 0.0
    position_avg_price = 0.0
    unrealized_pnl_pct = 0.0
```

**Error handling:** If position lookup fails, analysis continues with flat/unknown context and logs the failure. This is advisory context only; it must not block trading.

**ContextModule rendering update:** When real data is present, render a dedicated section:

```
## 持仓成本
- 持仓方向: LONG
- 持仓数量: {quantity}
- 平均成本: {avg_price:.2f}
- 当前价格: {current_price:.2f}
- 浮盈/浮亏: {unrealized_pnl_pct:+.2f}%
```

When flat (quantity == 0), render a concise line: `当前无持仓。`

### Section 3: LLM Prompt Rules — Cost-Aware Anchoring

**File:** `backend/app/domain/prompt/context_module.py` (or a new `PositionContextModule`)

With real position data available, the LLM prompt guidance should change from pure current-price anchoring to **position-aware anchoring**:

- **When `LONG`:** `buy_low` is the add-on trigger. Guidance should tell the LLM to consider average cost and current drawdown. It must not be raised every refresh just because current price rises.
- **When `LONG`:** `sell_high` should consider average cost. Avoid recommending sell targets below cost unless explicitly describing a stop-loss (which this system does not currently model).
- **When `FLAT`:** Current-price ± ATR / current volatility guidance remains acceptable.
- **When `SHORT`:** Keep existing behavior (cover at `buy_low`). Short add-on is explicitly out of scope.

**Prompt guidance text update (in SystemModule or ContextModule):**

```
当持仓 LONG 时：
- buy_low 用于加仓，应考虑平均成本与当前回撤。如果当前价格已低于平均成本，加仓可降低平均成本，但需谨慎。
- sell_high 应考虑平均成本。除非明确止损，否则不建议将 sell_high 设在成本价以下。
- 不要仅因当前价格上涨就上调 buy_low；buy_low 只有在基本面或技术面发生变化时才应调整。
```

### Section 4: Stable LLM Interval Application — Anti-Chasing Guard

**File:** `backend/app/services/interval_application_service.py`

For automatic application while `LONG`, prevent replacing `buy_low` with a higher value merely because the latest prompt followed current price. A conservative rule:

- In `LONG`, apply `sell_high` using existing logic (`max(old, new)` per Roadmap P7').
- In `LONG`, apply `buy_low` **only when the suggestion is at or below the current `buy_low`**.

This lets the LLM lower an add-on threshold (more aggressive entry) but prevents it from continuously ratcheting the add-on trigger upward (which would make add-on impossible).

**Implementation in `_apply_long()` (mirrors existing P7' sell_high logic):**

```python
if new_sell_high is not None:
    config.sell_high = max(old_sell_high, new_sell_high)
if new_buy_low is not None and new_buy_low <= old_buy_low:
    config.buy_low = new_buy_low
# If new_buy_low > old_buy_low: silently ignore (or log "chasing guard prevented buy_low raise")
```

This is intentionally backend-only; no new strategy fields or frontend controls.

### Section 5: Order Execution — Add-On Quantity Sizing

**File:** `backend/app/services/trade_execution_service.py`

Add-on buys use the **same sizing logic** as initial entry buys:
- `BUY` (initial flat → long): `qty = _entry_quantity_from_margin_power(...)`
- `BUY` (add-on while long): `qty = _entry_quantity_from_margin_power(...)`

The `TradeExecutionService` does not need to distinguish "first buy" vs "add-on buy" for sizing. The broker's `estimate_max_purchase_quantity` already accounts for current holdings and available buying power.

**Tracked entries update:** After an add-on buy fills:
- `_record_entry_price()` updates the weighted average cost in `tracked_entries`.
- The existing weighted-average logic already handles multiple buys: `new_cost = (old_qty * old_cost + fill_qty * fill_price) / (old_qty + fill_qty)`.
- This is existing behavior; no changes needed.

### Section 6: Risk Integration — Add-On Buy Under Risk Controls

**Files:** `backend/app/core/risk.py`, `backend/app/services/daily_pnl_service.py`

Add-on buys must pass the same risk gates as any other order:
- **Daily loss limit:** If the day's realized + unrealized losses are near the limit, the add-on buy may be the last action before hitting the ceiling. The risk controller evaluates this at order submission time; no special handling needed.
- **Consecutive losses:** Add-on buys that immediately lose money (price drops further) will count toward consecutive losses when eventually closed. This is expected behavior.
- **Kill switch / pause:** If the system is paused or kill-switched, add-on buys are blocked by the existing runner gate before the engine is even consulted.
- **Fee guard:** Add-on buys are entry orders, not exit orders. The fee guard (`_profit_guard_for_exit`) applies only to sells. However, users should be aware that add-on buys increase total position size, which increases total round-trip fees on eventual exit. This is a documentation concern, not a code change.

### Section 7: Data Flow Summary

```
AppRunner (LLM cron / manual trigger)
  ├─> BrokerGateway.get_positions(symbol) ──► position data
  ├─> TradeExecutionService.get_tracked_entry(symbol) ──► cost basis
  └─> LLMAdvisorService.analyze(
        position_quantity=qty,
        position_avg_price=cost,
        unrealized_pnl_pct=pnl_pct,
      )
        └─> DataAggregator.build_prompt() ──► context dict
              └─> ContextModule.render() ──► prompt text with cost context
                    └─> DeepSeek API
                          └─> suggestion: {buy_low, sell_high, confidence}
                                └─> IntervalApplicationService.apply_suggestion()
                                      └─> _apply_long() ──► anti-chasing guard
                                            └─> StrategyConfig.buy_low / sell_high updated

AppRunner (price tick)
  └─> StrategyEngine.update_price(price)
        └─> LONG state ──► price <= buy_low ? ──► TriggerResult(action="BUY")
              └─> TradeExecutionService.execute(BUY)
                    └─> _entry_quantity_from_margin_power()
                          └─> BrokerGateway.estimate_margin_max_quantity()
                                └─> Order submitted
                                      └─> On fill: _record_entry_price() updates tracked_entries
```

## Testing Plan

### Backend Unit Tests

Add tests in `backend/tests/test_engine.py`:
- `LONG` + `price <= buy_low` triggers `BUY` and remains `LONG`.
- `LONG` + `price >= sell_high` triggers `SELL` and transitions to `FLAT` (regression).
- Cooldown prevents immediate repeated add-on buys.
- Cooldown after add-on buy also blocks sell for 60s (expected global cooldown behavior).

Add tests in `backend/tests/test_llm_advisor_service.py` or `test_data_aggregator.py`:
- Prompt includes real position quantity, average price, and unrealized P/L when position exists.
- Prompt renders "当前无持仓" when flat.
- Position lookup failure does not crash analysis; falls back to flat context with logged warning.

Add tests in `backend/tests/test_interval_application.py`:
- `_apply_long()` does not raise `buy_low` when LLM suggests a higher value.
- `_apply_long()` does lower `buy_low` when LLM suggests a lower value.
- `_apply_long()` continues to apply `sell_high` using existing logic.

Add tests in `backend/tests/test_trade_execution_service.py`:
- Add-on `BUY` uses margin-based sizing (same path as initial buy).
- Second add-on buy correctly updates `tracked_entries` weighted average cost.
- Close-out `SELL` uses total tracked quantity, not just the last add-on quantity.

### Frontend Tests

No frontend changes are planned for this iteration. Existing Strategy page LLM cards continue to show applied intervals without distinguishing initial vs add-on buy. This can be enhanced in a future iteration.

### Manual Verification

1. Start runner with a configured strategy.
2. Trigger initial `BUY` (flat → long).
3. Lower `buy_low` manually or via LLM preview.
4. Inject a mock price ≤ new `buy_low`.
5. Confirm second `BUY` order is submitted.
6. Confirm `tracked_entries` quantity and cost reflect weighted average.
7. Confirm engine state remains `LONG`.
8. Confirm prompt preview shows real position context.

## Non-Goals

- No frontend controls for add-on buying (scope is backend-only).
- No new persisted strategy fields.
- No stop-loss behavior (remains out of scope per original design).
- No fractional shares.
- No change to short add-on behavior; `SHORT` still only covers at `buy_low`.
- No change to buying-power sizing percentage (already uses `margin_max_qty * 0.9`).
- No changes to existing P9 modules (PromptBuilder, ABTestManager, SentimentAnalyzer, PerformanceTracker) except for real data injection into `analyze()` parameters.

## Scope & Effort

| Component | Files | Complexity | Notes |
|-----------|-------|------------|-------|
| Engine state machine | `engine.py` | Low | 3 lines of new logic + tests |
| Position data fetch | `llm_advisor_service.py`, `runner.py` | Medium | Need to wire tracked entries or broker positions into LLM path |
| Prompt rendering | `context_module.py` | Low | Add position cost section |
| Anti-chasing guard | `interval_application_service.py` | Low | Already has `_apply_long`; tighten buy_low rule |
| Order execution | `trade_execution_service.py` | None | Existing sizing + tracked entries already handle multiple buys |
| Risk integration | `risk.py`, `daily_pnl_service.py` | None | Existing gates apply automatically |
| Tests | `test_engine.py`, `test_interval_application.py`, `test_llm_advisor_service.py` | Medium | ~10 new test cases |

**Estimated effort:** 1–2 days (backend only).

## Open Questions

1. **Max add-on count / position size cap:** Should we limit how many add-on buys can occur in a single day or total position size? The current system has no such cap. This can be added later via a strategy setting.
2. **LLM guidance for "don't chase":** Should the anti-chasing rule be hardcoded (as proposed) or exposed as a strategy setting `llm_buy_low_chasing_guard`? For the first implementation, hardcoded is simpler and safer.
3. **Unrealized PnL in risk calculation:** Currently daily PnL is realized-only (from closed orders). Should unrealized PnL be included in daily loss counting? This is a broader risk question beyond add-on buys.

## Recommendation

This design is the natural next step after P9. It:
- **Activates the real value of P9's LLM modular prompt** by feeding it real position data.
- **Changes core trading behavior** in a controlled, backward-compatible way (engine state set stays the same; only LONG behavior expands).
- **Reuses 100% of existing infrastructure**: margin sizing, tracked entries, risk gates, interval application.
- **Requires no frontend work**, keeping the iteration tight and fast.

Proceeding with this design will turn the system from a "one-shot interval trader" into a "scalable position manager" while keeping all existing safety mechanisms intact.
</content>
