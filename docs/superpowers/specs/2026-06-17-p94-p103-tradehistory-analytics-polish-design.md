# P94–P103 TradeHistory Analytics Polish Design

## Goal

Make TradeHistory a stronger read-only trade review surface by adding local summaries, filters, and insights derived from existing closed-trade and analytics responses.

## Scope

| Iteration | Feature | Behavior |
|---|---|---|
| P94 | Round-trip summary strip | Show current round-trip count, winners, losers, total net PnL, estimated fees, and average net PnL. |
| P95 | Round-trip quick filters | Client-side filter chips for all, winners, losers, long, and short. |
| P96 | Round-trip symbol search | Client-side symbol search over currently loaded round trips. |
| P97 | Round-trip insights | Highlight best and worst currently filtered round trips. |
| P98 | Round-trip detail expansion | Expand each round-trip row to show entry/exit order ids, entry/exit times, and fee drag context. |
| P99 | Calendar insights | Summarize best day, worst day, and most active day from trade calendar data. |
| P100 | Hold-duration insights | Summarize best/worst non-empty holding-duration buckets. |
| P101 | PnL-distribution balance | Summarize losing vs profitable bucket counts and net PnL balance. |
| P102 | Monthly trend insight | Summarize latest month, best month, and worst drawdown month. |
| P103 | Weekday attribution insight | Summarize best and worst weekday by net PnL. |

## Non-goals

- No backend API changes.
- No database changes.
- No write actions.
- No chart library changes.
- No changes to broker, order execution, runner, risk, or fee calculations.
- No cross-page persisted filters.

## Data Sources

- `GET /api/trades` via existing `getClosedTrades()`.
- `GET /api/trades/analytics/calendar` via existing `getTradeCalendar()`.
- `GET /api/trades/analytics/hold-duration` via existing `getTradeHoldDuration()`.
- `GET /api/trades/analytics/pnl-distribution` via existing `getTradePnlDistribution()`.
- `GET /api/trades/analytics/monthly` via existing `getTradeMonthlySummary()`.
- `GET /api/trades/analytics/weekday` via existing `getTradeWeekdayAttribution()`.

## UI Placement

- Round-trip summary/search/filter/insights live inside the existing “已实现成交” collapse, above the round-trip table.
- Row details use the Element Plus expandable table row pattern.
- Analytics insights live inside the existing “交易分析（只读）” collapse, above the five existing analytics cards.

## Semantics

- All filters are current-loaded-data only and labeled as such.
- Summary values are computed from filtered round trips, not global backend totals.
- Analytics insight cards are derived from the response currently loaded for the selected date range.
- Empty states remain explicit and do not block existing tables.

## Testing

- Extend `frontend/cypress/e2e/trade_roundtrips.cy.ts` with RED tests for P94–P98.
- Extend `frontend/cypress/e2e/history.cy.ts` analytics test with RED assertions for P99–P103.
- Verify RED before implementation, then run focused Cypress, `npm run type-check`, `npm run build`, and `git diff --check`.
